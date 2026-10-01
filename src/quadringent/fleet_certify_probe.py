"""Mesure profonde de certification par voie — à la demande, jamais en boucle.

La sonde de charge (:mod:`fleet_load_probe`) répond « la livraison est-elle
propre » à chaque cycle ; ce module répond « la voie est-elle prouvée » —
empreinte d'identité figée par ``AT(TIMESTAMP => T)`` sur le brut, même
empreinte recalculée sur la population dédupliquée, vocabulaire journal
mesuré, écarts du registre de publication. Le document produit est embarqué
dans la preuve console (``certify.tables.<voie>``) — même clé, même
écrivain — et relu par le pilote de progression qui en tire le
``ReconciliationProof`` du domaine : ``RECONCILING → CERTIFIED`` n'arrive
que sur mesure complète, jamais sur un statut.

Le gel ``AT(TIMESTAMP => T)`` donne un instantané cohérent des deux côtés de
la comparaison : sans lui, le brut vivant grandirait entre deux requêtes et
la preuve comparerait deux populations différentes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Mapping

from .fleet_capture import parse_fleet_tables

# Les dépendances de mesure (``fleet_load_probe``, ``snowflake_autonomous``)
# sont importées à l'intérieur des fonctions qui en ont besoin : le côté
# document du module — ``TableCertification``, ``certify_document``,
# ``parse_certify_document`` — reste autonome pour le control-plane, qui ne
# mesure jamais lui-même.


CERTIFY_FORMAT = "quadringent-certify-proof-v1"
# Une preuve de certification reste valable tant qu'elle est récente : la
# sonde ne re-mesure une voie qu'au-delà de cet âge — la mesure est un scan
# complet de la voie, jamais un réflexe de boucle.
CERTIFY_MAX_AGE_SECONDS = 3600.0
# Vocabulaire ``journal_entry_type`` borné : au-delà, la mesure est refusée,
# jamais tronquée.
_MAX_ENTRY_TYPES = 64
# Fenêtre de débit : lignes ingérées sur la dernière heure de la fenêtre.
_THROUGHPUT_WINDOW_SECONDS = 3600

_CERTIFY_KEYS = (
    "format_version",
    "table",
    "measured_at",
    "window",
    "source_count",
    "target_count",
    "missing",
    "extra",
    "duplicates",
    "source_hash",
    "target_hash",
    "destination_freshness_seconds",
    "latency_seconds",
    "throughput_rows_per_second",
    "operations",
)


@dataclass(frozen=True)
class TableCertification:
    """Mesure de certification d'une voie, figée à l'instant ``window.end``.

    ``source_*`` mesure le brut chargé (la population publiée livrée) ;
    ``target_*`` mesure la population dédupliquée appliquée. ``missing`` et
    ``extra`` viennent du registre de publication : fichiers déclarés non
    chargés et lignes chargées sans reçu. ``freshness`` vaut 0 quand la
    cible a appliqué tout le publié — une voie silencieuse n'est pas une
    voie en retard ; sinon l'âge du front appliqué, borne inférieure
    honnête de l'arriéré. ``latency`` est le délai de livraison de la
    donnée la plus fraîche. ``operations`` porte le compte par
    ``journal_entry_type`` mesuré dans le brut — preuve
    créations/modifications/suppressions.
    """

    table: str
    measured_at: str
    window_start_utc: str
    window_end_utc: str
    source_count: int
    target_count: int
    missing: int
    extra: int
    duplicates: int
    source_hash: str
    target_hash: str
    destination_freshness_seconds: float
    latency_seconds: float
    throughput_rows_per_second: float
    operations: tuple[tuple[str, int], ...]





def measure_table_certification(
    cursor: Any,
    site: Any,
    table: str,
    *,
    missing: int,
    extra: int,
    pending_files: int | None,
    end: datetime | None = None,
) -> TableCertification | None:
    """Mesure la voie figée à ``end`` ; ``None`` si une mesure est refusée.

    Trois requêtes bornées, toutes ``AT(TIMESTAMP => end)`` : brut (compte,
    identités, empreinte), dédupliqué canonique recalculé (compte,
    empreinte, frontières temporelles) et vocabulaire journal du brut. Un
    échec est une absence de mesure — jamais une valeur estimée.
    """

    from .fleet_load_probe import _quoted_fqn, _site
    from .snowflake_autonomous import _sql_string

    site = _site(site)
    if (
        isinstance(missing, bool)
        or not isinstance(missing, int)
        or missing < 0
        or isinstance(extra, bool)
        or not isinstance(extra, int)
        or extra < 0
        or (pending_files is not None and (isinstance(pending_files, bool) or pending_files < 0))
    ):
        raise ValueError("certification ledger counters are invalid")
    if end is None:
        end = datetime.now(timezone.utc)
    if not isinstance(end, datetime) or end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("certification window end must be timezone-aware")
    end = end.astimezone(timezone.utc).replace(microsecond=0)
    started = datetime.now(timezone.utc).replace(microsecond=0)
    start = started if started < end else end - timedelta(seconds=1)
    stamp = _sql_string(end.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
    raw = _quoted_fqn(site, site.snowflake_raw_table_for(table))
    like = _sql_string(f"%/{table.lower()}/journal/%")
    at = f"AT(TIMESTAMP => TO_TIMESTAMP_TZ({stamp}))"

    try:
        cursor.execute(
            "SELECT COUNT(*), COUNT(DISTINCT PAYLOAD:event_id::VARCHAR), "
            "HASH_AGG(PAYLOAD:event_id::VARCHAR) "
            f"FROM {raw} {at} WHERE SOURCE_FILE LIKE {like}"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        source_count = _measured_count(row[0])
        distinct = _measured_count(row[1])
        source_hash = _measured_hash(row[2])
    except Exception:  # noqa: BLE001 - absence de mesure, jamais estimée
        return None
    if source_count is None or distinct is None or source_hash is None:
        return None
    duplicates = source_count - distinct

    try:
        cursor.execute(
            "SELECT COUNT(*), HASH_AGG(EVENT_ID), "
            "MAX(INGESTED_AT), MAX(COMMIT_TS), "
            f"COUNT_IF(INGESTED_AT >= DATEADD('second', -{_THROUGHPUT_WINDOW_SECONDS}, "
            f"TO_TIMESTAMP_TZ({stamp}))) "
            "FROM ("
            "SELECT PAYLOAD:event_id::VARCHAR AS EVENT_ID, "
            "PAYLOAD:journal_sequence::NUMBER(38, 0) AS SEQ, "
            "PAYLOAD:commit_timestamp::TIMESTAMP_TZ AS COMMIT_TS, "
            "INGESTED_AT, SOURCE_FILE "
            f"FROM {raw} {at} WHERE SOURCE_FILE LIKE {like} "
            "QUALIFY ROW_NUMBER() OVER ("
            "PARTITION BY EVENT_ID "
            "ORDER BY SEQ DESC, INGESTED_AT DESC, SOURCE_FILE DESC"
            ") = 1)"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        target_count = _measured_count(row[0])
        target_hash = _measured_hash(row[1])
        last_ingest = _measured_timestamp(row[2])
        last_commit = _measured_timestamp(row[3])
        recent_rows = _measured_count(row[4])
    except Exception:  # noqa: BLE001
        return None
    if target_count is None or target_hash is None:
        return None

    try:
        cursor.execute(
            "SELECT PAYLOAD:journal_entry_type::VARCHAR, COUNT(*) "
            f"FROM {raw} {at} WHERE SOURCE_FILE LIKE {like} GROUP BY 1"
        )
        rows = cursor.fetchall()
        if len(rows) > _MAX_ENTRY_TYPES:
            return None
        counted = [
            (_measured_token(item[0]), _measured_count(item[1])) for item in rows
        ]
        if any(name is None or count is None for name, count in counted):
            return None
        operations = tuple(sorted(counted))
    except Exception:  # noqa: BLE001
        return None

    applied_complete = (
        missing == 0
        and extra == 0
        and pending_files == 0
        and target_count == distinct
    )
    freshness = 0.0
    if not applied_complete:
        freshness = (
            max(0.0, (end - last_commit).total_seconds())
            if last_commit is not None
            else math.inf
        )
        if not math.isfinite(freshness):
            return None
    latency = 0.0
    if last_ingest is not None and last_commit is not None:
        latency = max(0.0, (last_ingest - last_commit).total_seconds())
    throughput = (
        recent_rows / _THROUGHPUT_WINDOW_SECONDS
        if recent_rows is not None
        else None
    )
    if throughput is None:
        return None

    return TableCertification(
        table=table,
        measured_at=end.isoformat(),
        window_start_utc=start.isoformat(),
        window_end_utc=end.isoformat(),
        source_count=source_count,
        target_count=target_count,
        missing=missing,
        extra=extra,
        duplicates=duplicates,
        source_hash=source_hash,
        target_hash=target_hash,
        destination_freshness_seconds=freshness,
        latency_seconds=latency,
        throughput_rows_per_second=throughput,
        operations=operations,
    )


def certify_document(certification: TableCertification) -> dict[str, object]:
    """Document publié dans ``console-proof.json`` sous ``certify.tables`` —
    schéma fermé."""

    if not isinstance(certification, TableCertification):
        raise ValueError("a typed table certification is required")
    return {
        "format_version": CERTIFY_FORMAT,
        "table": certification.table,
        "measured_at": certification.measured_at,
        "window": {
            "start_utc": certification.window_start_utc,
            "end_utc": certification.window_end_utc,
        },
        "source_count": certification.source_count,
        "target_count": certification.target_count,
        "missing": certification.missing,
        "extra": certification.extra,
        "duplicates": certification.duplicates,
        "source_hash": certification.source_hash,
        "target_hash": certification.target_hash,
        "destination_freshness_seconds": certification.destination_freshness_seconds,
        "latency_seconds": certification.latency_seconds,
        "throughput_rows_per_second": certification.throughput_rows_per_second,
        "operations": dict(certification.operations),
    }


def parse_certify_document(document: object) -> TableCertification | None:
    """Recharge un document de certification ; ``None`` hors contrat."""

    if not isinstance(document, Mapping):
        return None
    if set(document) != set(_CERTIFY_KEYS):
        return None
    if document.get("format_version") != CERTIFY_FORMAT:
        return None
    table = document.get("table")
    if not isinstance(table, str):
        return None
    try:
        table = parse_fleet_tables((table,))[0]
    except ValueError:
        return None
    measured_at = _parse_utc(document.get("measured_at"))
    window = document.get("window")
    if not isinstance(window, Mapping):
        return None
    start = _parse_utc(window.get("start_utc"))
    end = _parse_utc(window.get("end_utc"))
    if measured_at is None or start is None or end is None or end <= start:
        return None
    counts = (
        _non_negative(document.get("source_count")),
        _non_negative(document.get("target_count")),
        _non_negative(document.get("missing")),
        _non_negative(document.get("extra")),
        _non_negative(document.get("duplicates")),
    )
    if any(value is None for value in counts):
        return None
    source_hash = document.get("source_hash")
    target_hash = document.get("target_hash")
    if not isinstance(source_hash, str) or not source_hash:
        return None
    if not isinstance(target_hash, str) or not target_hash:
        return None
    freshness = _non_negative_float(document.get("destination_freshness_seconds"))
    latency = _non_negative_float(document.get("latency_seconds"))
    throughput = _non_negative_float(document.get("throughput_rows_per_second"))
    if freshness is None or latency is None or throughput is None:
        return None
    operations = document.get("operations")
    if not isinstance(operations, Mapping) or len(operations) > _MAX_ENTRY_TYPES:
        return None
    parsed_operations: list[tuple[str, int]] = []
    for name, count in operations.items():
        if not isinstance(name, str) or not name:
            return None
        parsed = _non_negative(count)
        if parsed is None:
            return None
        parsed_operations.append((name, parsed))
    return TableCertification(
        table=table,
        measured_at=measured_at.isoformat(),
        window_start_utc=start.isoformat(),
        window_end_utc=end.isoformat(),
        source_count=counts[0],
        target_count=counts[1],
        missing=counts[2],
        extra=counts[3],
        duplicates=counts[4],
        source_hash=source_hash,
        target_hash=target_hash,
        destination_freshness_seconds=freshness,
        latency_seconds=latency,
        throughput_rows_per_second=throughput,
        operations=tuple(sorted(parsed_operations)),
    )


def _measured_count(value: object) -> int | None:
    """Compteur mesuré non négatif, ou ``None`` — jamais de troncature."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    result = int(value)
    return result if result >= 0 else None


def _measured_hash(value: object) -> str | None:
    """Empreinte ``HASH_AGG`` : entier signé sérialisé, ``None`` si absente."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return str(int(value))


def _measured_token(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _measured_timestamp(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _non_negative(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _non_negative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _parse_utc(value: object) -> datetime | None:
    """Instant UTC aligné à la seconde — la borne de la preuve ne dérive pas."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.microsecond != 0:
        return None
    return parsed.astimezone(timezone.utc)
