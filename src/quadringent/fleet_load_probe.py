"""Mesure en lecture seule du chargement Snowflake d'une flotte de tables.

Le lecteur de flotte réécrit ``fleet/console-snapshot.json`` toutes les
~10 s sans jamais pouvoir joindre une preuve de livraison : le worker écrit
le brut, il ne relit pas la cible. Ce module produit le constat mesuré qui
manque — trois requêtes bornées par table — et compose un bloc
``destination-proof-v1`` posé par l'appelant sur une autre clé,
``fleet/console-proof.json``. Une clé, un écrivain, toujours.

Ce qui n'est pas mesuré n'est pas sain : tuyau illisible, comptes absents ou
receivers hors borne produisent des états dégradés ou bloqués du contrat,
jamais une santé inférée.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any, Mapping, Sequence

from .destination_proof import _batch_count, _checkpoint, _destination_proof_v1
from .fleet_capture import parse_fleet_tables
from .fleet_load_ledger import (
    FleetLoadLedger,
    TableLedger,
    SNAPSHOT_RECEIVER_PREFIX,
    snapshot_declared as _ledger_snapshot_declared,
    ledger_state as _ledger_state,
    missing_events as _ledger_missing_events,
    published_events as _ledger_published_events,
    unexpected_rows as _ledger_unexpected_rows,
)
from .site_config import SiteConfig
from .snowflake_autonomous import _sql_string


# Le groupement par receiver est borné : au-delà, la mesure est refusée,
# jamais tronquée silencieusement.
_MAX_RECEIVERS_PER_TABLE = 512
# Le relevé par fichier source est borné de la même façon : un brut plus
# fragmenté que la borne produit une mesure refusée, jamais partielle.
_MAX_SOURCE_FILES_PER_TABLE = 50_000

_SNAPSHOT_NAME = "fleet/console-snapshot.json"
_PROOF_NAME = "fleet/console-proof.json"


def fleet_console_snapshot_key(site: SiteConfig) -> str:
    """Clé du document console de flotte, écrite par le seul lecteur."""

    return f"{_site(site).raw_prefix_root}/{_SNAPSHOT_NAME}"


def fleet_console_proof_key(site: SiteConfig) -> str:
    """Clé dédiée à la preuve : le lecteur n'y écrit jamais."""

    return f"{_site(site).raw_prefix_root}/{_PROOF_NAME}"


def fleet_console_proof_s3_uri(site: SiteConfig) -> str:
    site = _site(site)
    return f"s3://{site.raw_bucket}/{fleet_console_proof_key(site)}"


@dataclass(frozen=True)
class FleetTableMeasurement:
    """Mesure d'une voie : tuyau, volumes bruts et positions couvertes.

    Tout champ ``None`` est une mesure non obtenue — jamais un zéro inféré.
    ``load_max_by_receiver`` associe chaque receiver présent dans le brut à
    la séquence journal maximale livrée ; ``apply_max_by_receiver`` fait de
    même pour la vue canonique. ``error`` porte une nature bornée d'échec,
    jamais de SQL ni de contenu de ligne.
    """

    table: str
    pipe: str
    pipe_state: str | None
    pending_files: int | None
    raw_rows: int | None
    distinct_events: int | None
    canonical_rows: int | None
    load_max_by_receiver: tuple[tuple[str, int], ...]
    apply_max_by_receiver: tuple[tuple[str, int], ...]
    error: str | None
    file_rows: Mapping[str, int] | None = None
    # Lignes d'image initiale chargées (receivers ``SNAPSHOT:*``) et borne
    # de séquence la plus haute mesurée — le total publié déclaré reste
    # l'affaire du registre de manifestes.
    snapshot_rows: int | None = None
    snapshot_max_sequence: int | None = None
    journal_rows: int | None = None


@dataclass(frozen=True)
class FleetLoadMeasurement:
    """Rapport de mesure de la flotte, horodaté au moment de la lecture."""

    measured_at: datetime
    environment: str
    destination_id: str
    database: str
    schema: str
    tables: tuple[FleetTableMeasurement, ...]


def measure_fleet_load(
    cursor: Any, site: SiteConfig, *, tables: Sequence[str] | None = None
) -> FleetLoadMeasurement:
    """Mesure chaque voie déclarée, sans écrire ni exiger un tuyau RUNNING.

    Un tuyau non RUNNING est constaté, pas une erreur : la décision de santé
    appartient à :func:`attach_fleet_destination_proof`. Les échecs de
    requête sont enregistrés par table et la mesure continue sur les autres.
    """

    site = _site(site)
    names = site.fleet_tables if tables is None else _declared_subset(site, tables)
    measured_at = datetime.now(timezone.utc)
    return FleetLoadMeasurement(
        measured_at=measured_at,
        environment=site.environment,
        destination_id=site.destination_id,
        database=site.destination_database,
        schema=site.destination_schema,
        tables=tuple(_measure_table(cursor, site, table) for table in names),
    )


def _measure_table(
    cursor: Any, site: SiteConfig, table: str
) -> FleetTableMeasurement:
    pipe = site.snowflake_pipe_for(table)
    raw = _quoted_fqn(site, site.snowflake_raw_table_for(table))
    canonical = _quoted_fqn(site, site.snowflake_canonical_for(table))
    like = _sql_string(f"%/{table.lower()}/journal/%")
    error: str | None = None
    pipe_state: str | None = None
    pending_files: int | None = None
    raw_rows: int | None = None
    distinct_events: int | None = None
    canonical_rows: int | None = None
    load_covered: tuple[tuple[str, int], ...] = ()
    apply_covered: tuple[tuple[str, int], ...] = ()
    snapshot_rows: int | None = None
    snapshot_max_sequence: int | None = None

    try:
        cursor.execute(
            f"SELECT SYSTEM$PIPE_STATUS('{site.qualified_name(pipe)}')"
        )
        row = cursor.fetchone()
        if not row or not isinstance(row[0], str):
            error = "pipe_status_unavailable"
        else:
            try:
                status = json.loads(row[0])
            except json.JSONDecodeError:
                status = None
            state = status.get("executionState") if isinstance(status, dict) else None
            pending = status.get("pendingFileCount") if isinstance(status, dict) else None
            if not isinstance(state, str) or not state:
                error = "pipe_status_invalid"
            elif (
                isinstance(pending, bool)
                or not isinstance(pending, int)
                or pending < 0
            ):
                error = "pipe_status_invalid"
            else:
                pipe_state, pending_files = state, pending
    except Exception as exc:  # noqa: BLE001 - nature bornée, jamais de contenu
        error = _query_error("pipe_status", exc)

    try:
        cursor.execute(
            "SELECT PAYLOAD:journal_receiver::VARCHAR, COUNT(*), "
            "COUNT(DISTINCT PAYLOAD:event_id::VARCHAR), "
            "MAX(PAYLOAD:journal_sequence::NUMBER(38, 0)) "
            f"FROM {raw} WHERE SOURCE_FILE LIKE {like} GROUP BY 1"
        )
        rows = cursor.fetchall()
        if len(rows) > _MAX_RECEIVERS_PER_TABLE:
            raise ValueError("receiver bound exceeded")
        (
            raw_rows,
            distinct_events,
            load_covered,
            snapshot_rows,
            snapshot_max_sequence,
        ) = _accumulate_raw(rows)
    except ValueError:
        if error is None:
            error = "raw_measure_invalid"
    except Exception as exc:  # noqa: BLE001
        if error is None:
            error = _query_error("raw_query", exc)

    try:
        cursor.execute(
            "SELECT JOURNAL_RECEIVER, COUNT(*), MAX(JOURNAL_SEQUENCE) "
            f"FROM {canonical} WHERE SOURCE_FILE LIKE {like} GROUP BY 1"
        )
        rows = cursor.fetchall()
        if len(rows) > _MAX_RECEIVERS_PER_TABLE:
            raise ValueError("receiver bound exceeded")
        canonical_rows, apply_covered = _accumulate_canonical(rows)
    except ValueError:
        if error is None:
            error = "canonical_measure_invalid"
    except Exception as exc:  # noqa: BLE001
        if error is None:
            error = _query_error("canonical_query", exc)

    file_rows: dict[str, int] | None = None
    try:
        # Le relevé par fichier source est la moitié de la réconciliation :
        # chaque lot chargé est confronté à son reçu de publication.
        cursor.execute(
            "SELECT SOURCE_FILE, COUNT(*) "
            f"FROM {raw} WHERE SOURCE_FILE LIKE {like} GROUP BY 1"
        )
        rows = cursor.fetchall()
        if len(rows) > _MAX_SOURCE_FILES_PER_TABLE:
            raise ValueError("source file bound exceeded")
        file_rows = {}
        for row in rows:
            name, count = row[0], row[1]
            if (
                not isinstance(name, str)
                or not name
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ValueError("source file measure invalid")
            file_rows[name] = count
    except ValueError:
        if error is None:
            error = "file_measure_invalid"
        file_rows = None
    except Exception as exc:  # noqa: BLE001
        if error is None:
            error = _query_error("file_query", exc)
        file_rows = None

    return FleetTableMeasurement(
        table=table,
        pipe=pipe,
        pipe_state=pipe_state,
        pending_files=pending_files,
        raw_rows=raw_rows,
        distinct_events=distinct_events,
        canonical_rows=canonical_rows,
        load_max_by_receiver=load_covered,
        apply_max_by_receiver=apply_covered,
        error=error,
        file_rows=file_rows,
        snapshot_rows=snapshot_rows,
        snapshot_max_sequence=snapshot_max_sequence,
        journal_rows=(
            raw_rows - snapshot_rows
            if raw_rows is not None and snapshot_rows is not None
            else None
        ),
    )


def _accumulate_raw(
    rows: Sequence[Sequence[object]],
) -> tuple[int, int, tuple[tuple[str, int], ...], int, int | None]:
    """Agrège le brut par receiver : lignes, identités, couverture, split
    image initiale (receivers ``SNAPSHOT:*``) / journal."""

    total = 0
    distinct_total = 0
    covered: list[tuple[str, int]] = []
    snapshot_total = 0
    snapshot_max: int | None = None
    for row in rows:
        if len(row) < 4:
            raise ValueError("raw measure row is incomplete")
        receiver, count, distinct, maximum = row[0], row[1], row[2], row[3]
        if receiver is not None and not isinstance(receiver, str):
            raise ValueError("raw receiver is invalid")
        count = _coerced_count(count)
        total += count
        distinct_total += _coerced_count(distinct)
        if receiver is not None and maximum is not None:
            maximum = _coerced_count(maximum)
            covered.append((receiver, maximum))
            if receiver.startswith(SNAPSHOT_RECEIVER_PREFIX):
                snapshot_total += count
                snapshot_max = (
                    maximum
                    if snapshot_max is None
                    else max(snapshot_max, maximum)
                )
    return (
        total,
        distinct_total,
        tuple(sorted(covered)),
        snapshot_total,
        snapshot_max,
    )


def _accumulate_canonical(
    rows: Sequence[Sequence[object]],
) -> tuple[int, tuple[tuple[str, int], ...]]:
    total = 0
    covered: list[tuple[str, int]] = []
    for row in rows:
        if len(row) < 3:
            raise ValueError("canonical measure row is incomplete")
        receiver, count, maximum = row[0], row[1], row[2]
        if receiver is not None and not isinstance(receiver, str):
            raise ValueError("canonical receiver is invalid")
        total += _coerced_count(count)
        if receiver is not None and maximum is not None:
            covered.append((receiver, _coerced_count(maximum)))
    return total, tuple(sorted(covered))


def _coerced_count(value: object) -> int:
    """Entier de mesure non négatif, ou un refus — jamais une troncature."""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("measured count is invalid")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("measured count is fractional")
    result = int(value)
    if result < 0 or result != value:
        raise ValueError("measured count is out of contract")
    return result


def _query_error(step: str, error: Exception) -> str:
    """Nature bornée d'un échec SQL : aucune donnée, aucun message libre."""

    message = str(error).lower()
    if "does not exist" in message or "not found" in message:
        return f"{step}_missing_object"
    if (
        "not authorized" in message
        or "access denied" in message
        or "permission" in message
        or "privilege" in message
    ):
        return f"{step}_denied"
    return f"{step}_failed"


def attach_fleet_destination_proof(
    document: Mapping[str, object],
    measurement: FleetLoadMeasurement,
    *,
    site: SiteConfig,
    load_ledger: FleetLoadLedger | None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Compose un snapshot avec preuve destination-proof-v1 mesurée.

    Le document d'entrée n'est jamais muté. ``capture_observed_at`` garde
    l'âge réel de la capture ; ``generated_at`` prend l'heure de la mesure.
    La source de vérité du checkpoint est la position du document — la
    mesure ne fait que constater la couverture atteinte.

    ``load_ledger`` est le registre cumulatif des reçus de publication : le
    compteur du snapshot ne couvre que le run courant, alors que le brut
    cumule tous les runs — la réconciliation compare des populations de
    même portée ou déclare l'inconnue, jamais un faux écart.
    """

    if not isinstance(document, Mapping) or document.get("format_version") != "as400-console-v1":
        raise ValueError("capture snapshot format is incompatible")
    if not isinstance(measurement, FleetLoadMeasurement):
        raise ValueError("a typed fleet load measurement is required")
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    if load_ledger is not None and not isinstance(load_ledger, FleetLoadLedger):
        raise ValueError("a typed fleet load ledger is required")
    observed_dt = measurement.measured_at if observed_at is None else observed_at
    if (
        not isinstance(observed_dt, datetime)
        or observed_dt.tzinfo is None
        or observed_dt.utcoffset() is None
    ):
        raise ValueError("observed_at must be timezone-aware")
    if (
        measurement.environment != site.environment
        or measurement.destination_id != site.destination_id
        or measurement.database != site.destination_database
        or measurement.schema != site.destination_schema
    ):
        raise ValueError("measurement escaped the declared destination")
    tables = measurement.tables
    if tuple(entry.table for entry in tables) != site.fleet_tables:
        raise ValueError("measurement does not cover the declared fleet")
    checkpoint = _checkpoint(document)
    # Le compteur du snapshot reste une information de run — la population
    # comparée au brut cumulatif vient du registre de publication.
    run_published = _captured_events(document)
    observed = observed_dt.isoformat()

    measured_all = all(
        entry.error is None
        and entry.pipe_state is not None
        and entry.pending_files is not None
        and entry.raw_rows is not None
        and entry.distinct_events is not None
        and entry.canonical_rows is not None
        and entry.file_rows is not None
        for entry in tables
    )
    pipes_running = measured_all and all(
        entry.pipe_state == "RUNNING" for entry in tables
    )
    any_success = any(
        entry.pipe_state is not None or entry.raw_rows is not None for entry in tables
    )
    loaded = sum(entry.raw_rows for entry in tables) if measured_all else None
    ledger = sum(entry.canonical_rows for entry in tables) if measured_all else None
    distinct = (
        sum(entry.distinct_events for entry in tables) if measured_all else None
    )
    pending_total = (
        sum(entry.pending_files for entry in tables) if measured_all else None
    )
    duplicates = (
        sum(entry.raw_rows - entry.distinct_events for entry in tables)
        if measured_all
        else None
    )

    # Position couverte agrégée : le minimum entre voies, borné par le
    # checkpoint source — une voie jamais alimentée plancherne à 0, jamais
    # au-delà de la source ni en avance sur elle.
    receiver = checkpoint["receiver"]
    sequence = checkpoint["sequence"]
    load_covered = min(
        min(
            dict(entry.load_max_by_receiver).get(receiver, 0) for entry in tables
        ),
        sequence,
    ) if tables else 0
    apply_covered = min(
        min(
            dict(entry.apply_max_by_receiver).get(receiver, 0) for entry in tables
        ),
        load_covered,
    ) if tables else 0
    load_checkpoint = {"receiver": receiver, "sequence": load_covered}
    apply_checkpoint = {"receiver": receiver, "sequence": apply_covered}

    # Réconciliation de même portée : la population déclarée cumulée du
    # registre (reçus + base non-reçue) contre le brut chargé, par voie.
    table_ledgers: dict[str, TableLedger] = {}
    published_parts: list[int] = []
    missing_parts: list[int] = []
    unexpected_parts: list[int] = []
    ledger_complete = load_ledger is not None and measured_all
    if ledger_complete:
        for entry in tables:
            table_ledger = load_ledger.tables.get(entry.table)
            if table_ledger is None or entry.file_rows is None:
                ledger_complete = False
                break
            table_ledgers[entry.table] = table_ledger
            published = _ledger_published_events(table_ledger, entry.file_rows)
            missing_part = _ledger_missing_events(table_ledger, entry.file_rows)
            unexpected_part = _ledger_unexpected_rows(
                table_ledger, entry.file_rows
            )
            if (
                published is None
                or missing_part is None
                or unexpected_part is None
            ):
                ledger_complete = False
                break
            published_parts.append(published)
            missing_parts.append(missing_part)
            unexpected_parts.append(unexpected_part)
    captured = sum(published_parts) if ledger_complete else None
    missing = sum(missing_parts) if ledger_complete else None
    unexpected = sum(unexpected_parts) if ledger_complete else None
    failed_mutations = (
        abs(distinct - ledger) if distinct is not None and ledger is not None else None
    )

    matched = (
        ledger_complete
        and captured is not None
        and loaded == captured
        and ledger == loaded
        and distinct == loaded
        and duplicates == 0
        and pending_total == 0
        and missing == 0
        and unexpected == 0
    )
    # Doublons bruts, canonique divergente du brut ou lignes sans reçu :
    # la population cible n'est plus celle publiée — divergence prouvée.
    violated = measured_all and (
        duplicates > 0
        or ledger != distinct
        or (unexpected or 0) > 0
    )

    checks = _activation_checks(
        tables, measured_all=measured_all, any_success=any_success
    )
    blocker = None if measured_all else _blocker_code(tables)
    if not measured_all:
        activation_state = "blocked"
    elif pipes_running:
        activation_state = "active"
    else:
        activation_state = "unknown"

    batch_count = _batch_count(document)
    # Le bloc reconciliation du contrat est une mapping fermée : seuls les
    # huit compteurs du schéma y entrent — le détail du registre vit dans
    # le bloc destination, hors preuve.
    counters = {
        "captured_event_count": captured,
        "loaded_event_count": loaded,
        "ledger_event_count": ledger,
        "distinct_event_count": distinct,
        "duplicate_event_count": duplicates,
        "missing_event_count": missing,
        "unexpected_event_count": unexpected,
        "failed_mutation_count": failed_mutations,
    }

    if matched and pipes_running:
        # Le seul chemin sain réutilise le constructeur du contrat — le
        # schéma n'est jamais dupliqué ici.
        proof = _destination_proof_v1(
            checkpoint=checkpoint,
            observed_at=observed,
            destination_id=site.destination_id,
            environment=site.environment,
            source_events=captured,
            raw_rows=loaded,
            canonical_rows=ledger,
            distinct_event_ids=distinct,
            duplicates=duplicates,
            batch_count=batch_count,
        )
    elif matched:
        # Livraison complète mais une voie au moins n'est pas RUNNING :
        # l'activation ne peut pas être déclarée active.
        proof = _compose_proof(
            checkpoint=checkpoint,
            observed=observed,
            site=site,
            activation_state="unknown",
            checks=checks,
            blocker_code=None,
            load_state="succeeded",
            load_checkpoint=deepcopy(checkpoint),
            load_event_count=captured,
            load_failed_count=0,
            apply_state="applied",
            apply_checkpoint=deepcopy(checkpoint),
            apply_failed=0,
            recon_state="matched",
            window_to=deepcopy(checkpoint),
            counters=counters,
            batch_count=batch_count,
        )
    elif not measured_all:
        proof = _compose_proof(
            checkpoint=checkpoint,
            observed=observed,
            site=site,
            activation_state=activation_state,
            checks=checks,
            blocker_code=blocker,
            load_state="unknown",
            apply_state="unknown",
            recon_state="unknown",
            counters={"captured_event_count": captured},
            batch_count=batch_count,
        )
    else:
        load_state, load_incident, load_failed = _load_outcome(
            tables,
            duplicates=duplicates,
            unexpected=unexpected,
        )
        apply_state, apply_incident, apply_failed = _apply_outcome(
            ledger=ledger,
            distinct=distinct,
            failed_mutations=failed_mutations,
        )
        if violated:
            recon_state = "mismatch" if captured is not None else "unknown"
        elif captured is None:
            recon_state = "unknown"
        elif pipes_running:
            recon_state = "running"
        else:
            recon_state = "unknown"
        # Les états de progression exigent une position : le plancher
        # agrégé est toujours exprimable, jamais au-delà de la source.
        if load_state in {"running", "planned_stop"}:
            load_cp: dict[str, object] | None = load_checkpoint
        elif load_state == "succeeded":
            load_cp = deepcopy(checkpoint)
        else:
            load_cp = None
        if apply_state in {"applying", "planned_stop"}:
            apply_cp: dict[str, object] | None = apply_checkpoint
        elif apply_state == "applied":
            apply_cp = apply_checkpoint
        else:
            apply_cp = None
        proof = _compose_proof(
            checkpoint=checkpoint,
            observed=observed,
            site=site,
            activation_state=activation_state,
            checks=checks,
            blocker_code=None,
            load_state=load_state,
            load_checkpoint=load_cp,
            load_event_count=loaded if load_state in {"running", "planned_stop"} else None,
            load_failed_count=load_failed,
            load_incident=load_incident,
            apply_state=apply_state,
            apply_checkpoint=apply_cp,
            apply_failed=apply_failed,
            apply_incident=apply_incident,
            recon_state=recon_state,
            window_to=apply_checkpoint if recon_state == "mismatch" else None,
            counters=counters,
            batch_count=batch_count,
        )

    combined = deepcopy(dict(document))
    combined["capture_observed_at"] = document.get(
        "capture_observed_at", document.get("generated_at")
    )
    # Le document combiné est un nouveau snapshot, horodaté à la mesure.
    combined["generated_at"] = observed
    combined["destination"] = {
        "kind": "snowflake",
        "database": site.destination_database,
        "schema": site.destination_schema,
        "table_count": len(tables),
        "observed_at": observed,
        "load_state": proof["load"]["state"],
        "apply_state": proof["destination"]["state"],
        "reconciliation_state": proof["reconciliation"]["state"],
        "load_checkpoint": deepcopy(proof["load"].get("checkpoint")),
        "apply_checkpoint": deepcopy(proof["destination"].get("apply_checkpoint")),
        "source_events": captured,
        "run_published_events": run_published,
        "raw_rows": loaded,
        "canonical_rows": ledger,
        "duplicates": duplicates,
        "pending_files": pending_total,
        "measured_tables": sum(
            1 for entry in tables if entry.error is None
        ),
        "pipes_running": sum(
            1 for entry in tables if entry.pipe_state == "RUNNING"
        ),
        "ledger_complete": ledger_complete,
        "ledger": (
            _ledger_state(
                [
                    (name, load_ledger.tables[name])
                    for name in sorted(load_ledger.tables)
                ]
            )
            if load_ledger is not None
            else None
        ),
        "tables": {
            entry.table: {
                "pipe_state": entry.pipe_state,
                "pending_files": entry.pending_files,
                "raw_rows": entry.raw_rows,
                "canonical_rows": entry.canonical_rows,
                "snapshot_rows": entry.snapshot_rows,
                "journal_rows": entry.journal_rows,
                # Total publié déclaré : la borne du registre prime ; à
                # défaut, la séquence snapshot la plus haute mesurée dans
                # le chargé est un plancher honnête (jamais un plafond).
                "snapshot_published": _snapshot_published(
                    table_ledgers.get(entry.table), entry
                ),
                "loaded_files": (
                    len(entry.file_rows) if entry.file_rows is not None else None
                ),
                "receipted_files": (
                    len(table_ledgers[entry.table].receipted)
                    if entry.table in table_ledgers
                    else None
                ),
                "published_events": (
                    _ledger_published_events(
                        table_ledgers[entry.table], entry.file_rows
                    )
                    if entry.table in table_ledgers and entry.file_rows is not None
                    else None
                ),
                "unreceipted_rows": (
                    table_ledgers[entry.table].unreceipted_rows
                    if entry.table in table_ledgers
                    else None
                ),
                "unreceipted_files": (
                    table_ledgers[entry.table].unreceipted_files
                    if entry.table in table_ledgers
                    else None
                ),
                "pending_manifest_files": (
                    len(table_ledgers[entry.table].pending)
                    if entry.table in table_ledgers
                    else None
                ),
                "error": entry.error,
            }
            for entry in tables
        },
    }
    combined["destination_proof"] = proof
    return combined


def _snapshot_published(
    table_ledger: TableLedger | None, entry: FleetTableMeasurement
) -> int | None:
    """Total publié déclaré des images initiales, ou plancher mesuré."""

    declared = (
        _ledger_snapshot_declared(table_ledger) if table_ledger is not None else None
    )
    measured = entry.snapshot_max_sequence
    if declared is None:
        return measured
    if measured is None:
        return declared
    return max(declared, measured)


def _load_outcome(
    tables: tuple[FleetTableMeasurement, ...],
    *,
    duplicates: int,
    unexpected: int | None,
) -> tuple[str, str | None, int | None]:
    """État du chargement brut : échec de contrat, arrêt ou progression."""

    if duplicates > 0 or (unexpected or 0) > 0:
        return (
            "failed",
            "destination_load_contract_invalid",
            max(duplicates, unexpected or 0),
        )
    paused = any(entry.pipe_state == "PAUSED" for entry in tables)
    running = all(entry.pipe_state == "RUNNING" for entry in tables)
    if running:
        return "running", None, None
    if paused and all(entry.pipe_state in {"RUNNING", "PAUSED"} for entry in tables):
        return "planned_stop", None, None
    return "unknown", None, None


def _apply_outcome(
    *,
    ledger: int,
    distinct: int,
    failed_mutations: int,
) -> tuple[str, str | None, int | None]:
    """État de l'application : la canonique divergée est un échec avéré."""

    if ledger != distinct:
        return "failed", "destination_apply_contract_invalid", failed_mutations
    if ledger > 0:
        return "applied", None, 0
    return "not_started", None, None


def _activation_checks(
    tables: tuple[FleetTableMeasurement, ...], *, measured_all: bool, any_success: bool
) -> dict[str, str]:
    """Checks d'activation honnêtes : seul ce qui a été exercé est positif."""

    if measured_all:
        return {
            "configuration": "valid",
            "credential": "available",
            "connectivity": "reachable",
            "authorization": "allowed",
            "contract": "compatible",
        }
    blocker = _blocker_code(tables)
    checks = {
        "configuration": "unknown",
        "credential": "available" if any_success else "unknown",
        "connectivity": "reachable" if any_success else "unknown",
        "authorization": "allowed" if any_success else "unknown",
        "contract": "unknown",
    }
    if blocker == "destination_configuration_missing":
        checks["configuration"] = "missing"
    elif blocker == "destination_contract_incompatible":
        checks["contract"] = "incompatible"
    elif blocker == "destination_authorization_denied":
        checks["authorization"] = "denied"
    elif blocker == "destination_unreachable":
        checks["connectivity"] = "unreachable"
    return checks


def _blocker_code(tables: tuple[FleetTableMeasurement, ...]) -> str:
    """Premier bloqueur de mesure, sur la liste blanche du contrat."""

    for entry in tables:
        error = entry.error or ""
        if error.endswith("_missing_object"):
            return "destination_configuration_missing"
        if error.endswith("_denied"):
            return "destination_authorization_denied"
        if error.endswith("_invalid") or error.endswith("_unavailable"):
            return "destination_contract_incompatible"
    return "destination_unreachable"


def _compose_proof(
    *,
    checkpoint: Mapping[str, object],
    observed: str,
    site: SiteConfig,
    activation_state: str,
    checks: Mapping[str, str],
    blocker_code: str | None,
    load_state: str,
    apply_state: str,
    recon_state: str,
    counters: Mapping[str, int | None],
    load_checkpoint: Mapping[str, object] | None = None,
    load_event_count: int | None = None,
    load_failed_count: int | None = None,
    load_incident: str | None = None,
    apply_checkpoint: Mapping[str, object] | None = None,
    apply_failed: int | None = None,
    apply_incident: str | None = None,
    window_to: Mapping[str, object] | None = None,
    batch_count: int | None = None,
) -> dict[str, object]:
    """Bloc destination-proof-v1 pour les états non sains, schéma inchangé.

    Les clés optionnelles sont omises, jamais posées à ``None`` : le
    parseur du contrat refuse un compteur présent mais nul.
    """

    proof: dict[str, object] = {
        "schema_version": "destination-proof-v1",
        "observed_at": observed,
        "source_checkpoint": deepcopy(dict(checkpoint)),
        "target": {
            "kind": "snowflake",
            "destination_id": site.destination_id,
            "environment": site.environment,
        },
        "activation": {
            "state": activation_state,
            "observed_at": observed,
            "checks": dict(checks),
            "blocker_code": blocker_code,
        },
        "load": {
            "state": load_state,
            "observed_at": observed,
            "incident_code": load_incident,
        },
        "destination": {
            "state": apply_state,
            "observed_at": observed,
            "incident_code": apply_incident,
        },
        "reconciliation": {
            "state": recon_state,
            "observed_at": observed,
        },
    }
    load = proof["load"]
    apply = proof["destination"]
    reconciliation = proof["reconciliation"]
    if load_checkpoint is not None:
        load["checkpoint"] = deepcopy(dict(load_checkpoint))
    if batch_count is not None:
        load["batch_count"] = batch_count
    if load_event_count is not None:
        load["event_count"] = load_event_count
    if load_failed_count is not None:
        load["failed_event_count"] = load_failed_count
    if apply_checkpoint is not None:
        apply["apply_checkpoint"] = deepcopy(dict(apply_checkpoint))
    if apply_failed is not None:
        apply["failed_mutation_count"] = apply_failed
    if window_to is not None:
        reconciliation["window"] = {
            "from_exclusive": None,
            "to_inclusive": deepcopy(dict(window_to)),
        }
    for name, value in counters.items():
        if value is not None:
            reconciliation[name] = value
    return proof


def _captured_events(document: Mapping[str, object]) -> int | None:
    """Compte source déclaré, ou ``None`` — jamais une exception ici."""

    counters = document.get("counters")
    wrapper = counters.get("events_published") if isinstance(counters, Mapping) else None
    value = wrapper.get("value") if isinstance(wrapper, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _declared_subset(site: SiteConfig, tables: Sequence[str]) -> tuple[str, ...]:
    names = parse_fleet_tables(tables)
    if any(name not in site.fleet_tables for name in names):
        raise ValueError("measurement table is outside the declared fleet manifest")
    return names


def _quoted_fqn(site: SiteConfig, object_name: str) -> str:
    return (
        f'"{site.destination_database}"."{site.destination_schema}"."{object_name}"'
    )


def _site(site: SiteConfig) -> SiteConfig:
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    return site
