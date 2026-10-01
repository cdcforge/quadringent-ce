"""Registre cumulatif des publications d'une flotte, adossé aux manifestes S3.

Le compteur ``events_published`` du snapshot console est borné au run
courant, alors que le brut Snowflake cumule tous les runs — comparer les
deux est un faux écart structurel. Le seul reçu de publication durable et
indépendant du chargeur est le couple ``batch-*.jsonl`` /
``batch-*.manifest.json`` posé sous ``<table>/journal/`` : le manifeste
déclare ``event_count`` avant tout chargement.

Ce module entretient ``fleet/load-ledger.json`` — un écrivain, le job
d'observation — qui cumule les événements déclarés par manifeste. Trois
populations sont tenues distinctes, jamais confondues :

- ``receipted`` : fichiers dont le manifeste déclare un décompte — seuls
  ceux-là se réconcilient exactement contre les lignes chargées.
- ``unreceipted`` : fichiers publiés sans manifeste lisible (jamais écrit,
  supprimé par la rétention, ou antérieur au registre) — leurs lignes sont
  mesurées via le relevé ``file_rows`` de la destination, jamais estimées.
- ``pending`` : fichiers listés dont le manifeste n'a pas encore été lu —
  leur contribution vient aussi du relevé chargé tant qu'ils attendent.

À la première relevé complète, tout fichier chargé dont la clé n'a jamais
été vue en listing est adopté dans ``unreceipted`` : c'est la ligne de
base historique, gelée une fois — toute ligne non-reçue apparue après
elle est une anomalie déclarée, pas une absorption silencieuse.

Un registre corrompu est refusé, jamais réinitialisé silencieusement :
effacer les reçus fabriquerait de faux « unexpected ».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping, Sequence

from .site_config import SiteConfig


_LEDGER_NAME = "fleet/load-ledger.json"
_SCHEMA_VERSION = "fleet-load-ledger-v2"
_JSONL_SUFFIX = ".jsonl"
_MANIFEST_SUFFIX = ".manifest.json"

# Bornes : au-delà, le registre est refusé plutôt que tronqué.
_MAX_LEDGER_BYTES = 64 * 1024 * 1024
_MAX_RECEIPTED_FILES = 200_000
# Le manifeste embarque la liste complète des event_ids — plusieurs Mo
# pour les gros lots. L'écriture canonique trie les clés : ``event_count``
# précède ``event_ids`` (l'en-tête bornée suffit), tandis que
# ``journal_receiver`` et ``high_watermark`` le suivent — le suffixe borné
# les couvre sans jamais lire la liste.
_MANIFEST_HEAD_BYTES = 8192
_MANIFEST_TAIL_BYTES = 4096
_EVENT_COUNT_PATTERN = re.compile(rb'"event_count"\s*:\s*(\d+)')
_RECEIVER_PATTERN = re.compile(rb'"journal_receiver"\s*:\s*"([^"]{1,256})"')
_HIGH_WATERMARK_PATTERN = re.compile(
    rb'"high_watermark"\s*:\s*\{[^}]*"sequence"\s*:\s*(\d+)'
)
# Préfixe des receivers produits par une copie d'image initiale — tout le
# reste est un receiver de journal.
SNAPSHOT_RECEIVER_PREFIX = "SNAPSHOT:"
# Re-vérifications par cycle des fichiers classés non-reçus à lignes
# nulles : leur manifeste peut arriver quelques secondes après le lot —
# une promotion bornée évite de les figer à jamais.
_UNRECEIPTED_RECHECK_CAP = 100


def fleet_load_ledger_key(site: SiteConfig) -> str:
    """Clé du registre — même racine que la preuve, même écrivain."""

    return f"{site.raw_prefix_root}/{_LEDGER_NAME}"


@dataclass
class TableLedger:
    """Reçus d'une voie : fichiers déclarés, non-reçus, en attente.

    ``receipted`` associe chaque clé ``.jsonl`` au décompte déclaré par son
    manifeste. ``unreceipted`` associe chaque fichier sans reçu aux lignes
    mesurées dans la destination (0 tant qu'il n'est pas chargé). ``pending``
    liste les fichiers vus en listing dont le manifeste attend d'être lu.
    ``baselined`` devient vrai à la première relevé complète : les fichiers
    chargés jamais vus en listing y sont adoptés, une fois, dans
    ``unreceipted``.
    """

    receipted: dict[str, int] = field(default_factory=dict)
    unreceipted: dict[str, int] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    baselined: bool = False
    complete: bool = False
    # Receiver ``SNAPSHOT:*`` → séquence déclarée maximale de ce run de
    # copie (borne haute de son dernier manifeste lu). Le total publié
    # déclaré d'une image initiale est la borne de son receiver.
    snapshot_bounds: dict[str, int] = field(default_factory=dict)

    @property
    def unreceipted_rows(self) -> int:
        return sum(self.unreceipted.values())

    @property
    def unreceipted_files(self) -> int:
        return len(self.unreceipted)

    def declared_events(self, key: str) -> int | None:
        return self.receipted.get(key)


@dataclass
class FleetLoadLedger:
    """Registre des reçus de publication, une entrée par voie déclarée."""

    tables: dict[str, TableLedger] = field(default_factory=dict)
    updated_at: str = ""

    def table(self, name: str) -> TableLedger:
        entry = self.tables.get(name)
        if entry is None:
            entry = TableLedger()
            self.tables[name] = entry
        return entry


def load_fleet_ledger(storage: Any, site: SiteConfig) -> FleetLoadLedger | None:
    """Lit le registre ; ``None`` s'il n'existe pas encore.

    Tout autre état — document invalide, schéma inconnu, entrée hors
    borne — est une erreur : repartir d'un registre vide fabriquerait des
    écarts qui n'existent pas.
    """

    try:
        response = storage.get_object(
            Bucket=site.raw_bucket, Key=fleet_load_ledger_key(site)
        )
    except Exception as error:  # noqa: BLE001 - nature bornée
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    body = response.get("Body")
    if body is None:
        raise ValueError("fleet load ledger is unreadable")
    payload = body.read(_MAX_LEDGER_BYTES + 1)
    if len(payload) > _MAX_LEDGER_BYTES:
        raise ValueError("fleet load ledger exceeds the read budget")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("fleet load ledger is not a JSON document") from error
    return parse_fleet_ledger(document)


def parse_fleet_ledger(document: object) -> FleetLoadLedger:
    """Valide et reconstruit le registre — refus sur toute entrée suspecte."""

    if not isinstance(document, Mapping):
        raise ValueError("fleet load ledger is not a JSON object")
    if document.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("fleet load ledger schema is incompatible")
    raw_tables = document.get("tables")
    if not isinstance(raw_tables, Mapping):
        raise ValueError("fleet load ledger tables are missing")
    ledger = FleetLoadLedger(updated_at=str(document.get("updated_at") or ""))
    for name, raw_entry in raw_tables.items():
        if not isinstance(name, str) or not isinstance(raw_entry, Mapping):
            raise ValueError("fleet load ledger table entry is invalid")
        receipted = _receipt_map(raw_entry.get("receipted"))
        unreceipted = _receipt_map(raw_entry.get("unreceipted"))
        pending = raw_entry.get("pending")
        if not isinstance(pending, list) or any(
            not isinstance(key, str) or not key.endswith(_JSONL_SUFFIX)
            for key in pending
        ):
            raise ValueError("fleet load ledger pending keys are invalid")
        if set(receipted) & set(unreceipted):
            raise ValueError("fleet load ledger receipt sets overlap")
        if set(pending) & (set(receipted) | set(unreceipted)):
            raise ValueError("fleet load ledger pending overlaps receipts")
        if (
            len(receipted) + len(unreceipted) + len(pending)
            > _MAX_RECEIPTED_FILES
        ):
            raise ValueError("fleet load ledger exceeds the receipt bound")
        snapshot_bounds = raw_entry.get("snapshot_bounds")
        if snapshot_bounds is None:
            snapshot_bounds = {}
        if not isinstance(snapshot_bounds, Mapping) or any(
            not isinstance(receiver, str)
            or not receiver.startswith(SNAPSHOT_RECEIVER_PREFIX)
            or isinstance(bound, bool)
            or not isinstance(bound, int)
            or bound < 0
            for receiver, bound in snapshot_bounds.items()
        ):
            raise ValueError("fleet load ledger snapshot bounds are invalid")
        ledger.tables[name] = TableLedger(
            receipted=receipted,
            unreceipted=unreceipted,
            pending=sorted(pending),
            baselined=raw_entry.get("baselined") is True,
            complete=raw_entry.get("complete") is True,
            snapshot_bounds=dict(snapshot_bounds),
        )
    return ledger


def serialize_fleet_ledger(ledger: FleetLoadLedger) -> bytes:
    """Sérialise le registre — déterministe pour une écriture conditionnelle."""

    document = {
        "schema_version": _SCHEMA_VERSION,
        "updated_at": ledger.updated_at,
        "tables": {
            name: {
                "receipted": dict(sorted(entry.receipted.items())),
                "unreceipted": dict(sorted(entry.unreceipted.items())),
                "pending": list(entry.pending),
                "baselined": entry.baselined,
                "complete": entry.complete,
                "snapshot_bounds": dict(sorted(entry.snapshot_bounds.items())),
            }
            for name, entry in sorted(ledger.tables.items())
        },
    }
    return json.dumps(document, sort_keys=True, ensure_ascii=False).encode("utf-8")


def save_fleet_ledger(
    storage: Any, site: SiteConfig, ledger: FleetLoadLedger, *, expected_etag: str | None
) -> None:
    """Écrit le registre — écriture conditionnelle quand une version existe.

    L'écrivain est unique par construction (un seul job d'observation) ; le
    conditionnement sur l'ETag reste un refus net si un second écrivain
    apparaissait, plutôt qu'un écrasement silencieux.
    """

    ledger.updated_at = datetime.now(timezone.utc).isoformat()
    payload = serialize_fleet_ledger(ledger)
    key = fleet_load_ledger_key(site)
    if expected_etag is None:
        storage.put_object(
            Bucket=site.raw_bucket, Key=key, Body=payload, IfNoneMatch="*"
        )
    else:
        storage.put_object(
            Bucket=site.raw_bucket, Key=key, Body=payload, IfMatch=expected_etag
        )


def fleet_ledger_etag(storage: Any, site: SiteConfig) -> str | None:
    """ETag du registre courant, ``None`` s'il n'existe pas — refus sur
    toute autre lecture impossible."""

    try:
        response = storage.head_object(
            Bucket=site.raw_bucket, Key=fleet_load_ledger_key(site)
        )
    except Exception as error:  # noqa: BLE001
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    etag = response.get("ETag")
    return etag if isinstance(etag, str) and etag.strip() else None


def journal_listing(storage: Any, site: SiteConfig, table: str) -> set[str]:
    """Clés ``.jsonl`` du préfixe journal d'une voie — relevé exhaustif.

    La rétention supprime les fichiers déjà chargés : une clé absente ici
    n'efface ni son reçu ni sa ligne de base au registre.
    """

    prefix = f"{site.journal_prefix_for(table)}/"
    keys: set[str] = set()
    token: str | None = None
    while True:
        request: dict[str, object] = {"Bucket": site.raw_bucket, "Prefix": prefix}
        if token is not None:
            request["ContinuationToken"] = token
        response = storage.list_objects_v2(**request)
        for item in response.get("Contents") or ():
            key = item.get("Key") if isinstance(item, Mapping) else None
            if isinstance(key, str) and key.endswith(_JSONL_SUFFIX):
                keys.add(key)
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise ValueError("journal listing pagination is unreadable")


@dataclass(frozen=True)
class ManifestReceipt:
    """Reçu déclaré d'un lot : décompte, receiver et borne haute.

    ``receiver`` et ``sequence`` viennent du suffixe du manifeste — absents
    (``None``) quand le document n'est pas canonique assez pour les
    extraire, jamais estimés.
    """

    events: int
    receiver: str | None = None
    sequence: int | None = None


def manifest_receipt(
    storage: Any, site: SiteConfig, jsonl_key: str
) -> ManifestReceipt | None:
    """Reçu déclaré du manifeste d'un lot ; ``None`` s'il manque.

    Deux lectures bornées par lot : l'en-tête pour ``event_count`` (avant
    ``event_ids`` dans l'ordre canonique des clés), le suffixe pour
    ``journal_receiver`` et ``high_watermark.sequence`` (après). Un
    document assez petit pour tenir dans une fenêtre est tenté en parse
    complet ; tout le reste est un reçu partiel ou inconnu, jamais estimé.
    """

    manifest_key = f"{jsonl_key[: -len(_JSONL_SUFFIX)]}{_MANIFEST_SUFFIX}"
    head = _manifest_range(storage, site, manifest_key, f"bytes=0-{_MANIFEST_HEAD_BYTES - 1}")
    if head is None:
        return None
    payload, content_range = head
    match = _EVENT_COUNT_PATTERN.search(payload)
    events: int | None = int(match.group(1)) if match is not None else None
    whole = isinstance(content_range, str) and content_range.endswith(f"/{len(payload)}")
    if events is None and whole:
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        declared = document.get("event_count") if isinstance(document, Mapping) else None
        if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
            return None
        events = declared
    if events is None:
        return None
    if whole:
        # Le document tenait dans l'en-tête : receiver et borne s'y lisent.
        tail = payload
    else:
        tail_read = _manifest_range(
            storage, site, manifest_key, f"bytes=-{_MANIFEST_TAIL_BYTES}"
        )
        tail = tail_read[0] if tail_read is not None else b""
    receiver = _manifest_receiver(tail)
    sequence = _manifest_high_sequence(tail)
    if (receiver is None or sequence is None) and not whole:
        # Fenêtre insuffisante : si le suffixe couvrait le document entier,
        # le parse complet décide — sinon le reçu reste partiel.
        tail_range = tail_read[1] if tail_read is not None else None
        if isinstance(tail_range, str) and tail_range.endswith(f"/{len(tail)}"):
            receiver, sequence = _manifest_bounds_json(tail)
    return ManifestReceipt(events=events, receiver=receiver, sequence=sequence)


def manifest_event_count(storage: Any, site: SiteConfig, jsonl_key: str) -> int | None:
    """Événements déclarés par le manifeste d'un lot ; ``None`` s'il manque."""

    receipt = manifest_receipt(storage, site, jsonl_key)
    return receipt.events if receipt is not None else None


def _manifest_range(
    storage: Any, site: SiteConfig, manifest_key: str, byte_range: str
) -> tuple[bytes, str | None] | None:
    """Une lecture Range bornée ; ``None`` si l'objet est absent."""

    try:
        response = storage.get_object(
            Bucket=site.raw_bucket, Key=manifest_key, Range=byte_range
        )
    except Exception as error:  # noqa: BLE001
        code = getattr(error, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound", "InvalidRange"):
            return None
        raise
    body = response.get("Body")
    if body is None:
        return None
    payload = body.read(_MANIFEST_HEAD_BYTES + 1)
    return payload, response.get("ContentRange")


def _manifest_receiver(payload: bytes) -> str | None:
    match = _RECEIVER_PATTERN.search(payload)
    if match is None:
        return None
    try:
        return match.group(1).decode("utf-8")
    except UnicodeDecodeError:
        return None


def _manifest_high_sequence(payload: bytes) -> int | None:
    match = _HIGH_WATERMARK_PATTERN.search(payload)
    return int(match.group(1)) if match is not None else None


def _manifest_bounds_json(payload: bytes) -> tuple[str | None, int | None]:
    """Parse complet d'un petit manifeste pour receiver et borne haute."""

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(document, Mapping):
        return None, None
    receiver = document.get("journal_receiver")
    if not isinstance(receiver, str) or not receiver:
        receiver = None
    watermark = document.get("high_watermark")
    sequence = watermark.get("sequence") if isinstance(watermark, Mapping) else None
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        sequence = None
    return receiver, sequence


def refresh_table_ledger(
    entry: TableLedger,
    listing: set[str],
    *,
    fetch: Any,
    budget: int = 800,
    loaded_file_rows: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Avance le registre d'une voie sur le relevé courant.

    ``fetch`` reçoit une clé ``.jsonl`` et rend le reçu déclaré
    (:class:`ManifestReceipt`, ou ``None`` si le manifeste manque) — la
    lecture est déléguée pour que le registre reste testable sans
    transport. Le budget borne les lectures nouvelles par cycle :
    l'amorçage est progressif et mesuré, jamais un trou de coût.

    ``loaded_file_rows`` associe chaque ``SOURCE_FILE`` du brut aux lignes
    chargées : il mesure la contribution des fichiers non-reçus et gèle la
    ligne de base à la première relevé complète.
    """

    if budget < 0:
        raise ValueError("manifest read budget is invalid")
    file_rows = loaded_file_rows or {}
    # Les lignes mesurées des fichiers non-reçus dérivent vers le haut à
    # mesure qu'ils chargent — le registre les suit, sans nouvelle lecture.
    for key in entry.unreceipted:
        if key in file_rows:
            entry.unreceipted[key] = file_rows[key]

    # File d'attente : fichiers jamais classés, puis re-vérification bornée
    # des non-reçus à lignes nulles dont le manifeste peut arriver en
    # différé (les deux objets d'un lot ne sont pas posés atomiquement).
    to_classify = sorted(
        key
        for key in listing
        if key not in entry.receipted and key not in entry.unreceipted
    )
    recheck = sorted(
        key
        for key, rows in entry.unreceipted.items()
        if rows == 0 and key in listing
    )[:_UNRECEIPTED_RECHECK_CAP]
    queue = to_classify + [key for key in recheck if key not in set(to_classify)]

    fetched = 0
    missing_manifest = 0
    for key in queue:
        if fetched >= budget:
            break
        fetched += 1
        receipt = fetch(key)
        if receipt is None:
            entry.unreceipted[key] = file_rows.get(key, 0)
            missing_manifest += 1
        else:
            entry.receipted[key] = receipt.events
            entry.unreceipted.pop(key, None)
            if (
                receipt.receiver is not None
                and receipt.sequence is not None
                and receipt.receiver.startswith(SNAPSHOT_RECEIVER_PREFIX)
            ):
                # La séquence déclarée d'un run snapshot est cumulative :
                # la borne retenue est le maximum lu pour ce receiver.
                current = entry.snapshot_bounds.get(receipt.receiver, -1)
                entry.snapshot_bounds[receipt.receiver] = max(
                    current, receipt.sequence
                )
        if (
            len(entry.receipted) + len(entry.unreceipted)
            > _MAX_RECEIPTED_FILES
        ):
            raise ValueError("fleet load ledger exceeds the receipt bound")

    entry.pending = [
        key
        for key in to_classify
        if key not in entry.receipted and key not in entry.unreceipted
    ]
    entry.complete = not entry.pending
    if (
        entry.complete
        and not entry.baselined
        and loaded_file_rows is not None
    ):
        # Première relevé complète : tout ce qui est chargé sans reçu et
        # jamais vu en listing devient la ligne de base — gelée, déclarée.
        for key, rows in file_rows.items():
            if key not in entry.receipted and key not in entry.unreceipted:
                entry.unreceipted[key] = rows
        entry.baselined = True
    return {
        "new_files": len(to_classify),
        "manifests_read": fetched,
        "manifests_missing": missing_manifest,
        "pending_manifests": len(entry.pending),
    }


def published_events(
    entry: TableLedger, loaded_file_rows: Mapping[str, int]
) -> int | None:
    """Population capturée cumulée ; ``None`` avant la ligne de base.

    Somme des décomptes déclarés (reçus) et des lignes mesurées des
    fichiers non-reçus ou en attente — chaque composante est un relevé,
    jamais une estimation.
    """

    if not entry.baselined:
        return None
    return (
        sum(entry.receipted.values())
        + entry.unreceipted_rows
        + sum(
            loaded_file_rows.get(key, 0)
            for key in entry.pending
        )
    )


def missing_events(entry: TableLedger, loaded_file_rows: Mapping[str, int]) -> int | None:
    """Événements déclarés jamais chargés — lot absent ou lot tronqué."""

    if not entry.baselined:
        return None
    missing = 0
    for key, declared in entry.receipted.items():
        loaded = loaded_file_rows.get(key, 0)
        if loaded < declared:
            missing += declared - loaded
    return missing


def unexpected_rows(
    entry: TableLedger, loaded_file_rows: Mapping[str, int]
) -> int | None:
    """Lignes chargées sans reçu et hors base non-reçue — vraie anomalie.

    Couvre aussi la sur-livraison d'un fichier reçu : charger plus de
    lignes que le manifeste n'en déclare est une divergence, pas un
    arrondi. Avant la ligne de base, la population non-reçue n'est pas
    encore bornée : ``None``, jamais un zéro inféré.
    """

    if not entry.baselined:
        return None
    unexpected = 0
    for key, rows in loaded_file_rows.items():
        declared = entry.receipted.get(key)
        if declared is not None:
            unexpected += max(0, rows - declared)
        elif key not in entry.unreceipted and key not in set(entry.pending):
            unexpected += rows
    return unexpected


def snapshot_declared(entry: TableLedger) -> int | None:
    """Total déclaré des images initiales ; ``None`` si jamais observé.

    Chaque run de copie déclare une séquence cumulative dans son receiver
    ``SNAPSHOT:*`` — la somme des bornes est le total publié attendu.
    """

    if not entry.snapshot_bounds:
        return None
    return sum(entry.snapshot_bounds.values())


def ledger_state(tables: Sequence[tuple[str, TableLedger]]) -> dict[str, object]:
    """État public du registre — compteurs de construction honnêtes."""

    return {
        "tables_complete": sum(
            1 for _name, entry in tables if entry.complete
        ),
        "tables_baselined": sum(
            1 for _name, entry in tables if entry.baselined
        ),
        "receipted_event_count": sum(
            events
            for _name, entry in tables
            for events in entry.receipted.values()
        ),
        "unreceipted_event_count": sum(
            entry.unreceipted_rows for _name, entry in tables
        ),
        "unreceipted_file_count": sum(
            entry.unreceipted_files for _name, entry in tables
        ),
        "pending_file_count": sum(
            len(entry.pending) for _name, entry in tables
        ),
    }


def _receipt_map(raw: object) -> dict[str, int]:
    """Une mapping clé → lignes non négatives, ou un refus."""

    if not isinstance(raw, Mapping):
        raise ValueError("fleet load ledger receipts are invalid")
    result: dict[str, int] = {}
    for key, rows in raw.items():
        if not isinstance(key, str) or not key.endswith(_JSONL_SUFFIX):
            raise ValueError("fleet load ledger receipt key is invalid")
        if (
            isinstance(rows, bool)
            or not isinstance(rows, int)
            or rows < 0
        ):
            raise ValueError("fleet load ledger receipt count is invalid")
        result[key] = rows
    return result
