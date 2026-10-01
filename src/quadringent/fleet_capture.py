"""Une lecture du journal, treize flux bruts de table.

La lecture reste unique : une position, une chaîne de receivers, un reçu de
plage. Ce qui change est la sortie : chaque événement est routé vers le préfixe
de sa table, avec son propre manifeste et son propre curseur de couverture.

Deux garanties portent la correction :

- le curseur du journal n'avance qu'après la publication des tables. Un arrêt
  entre les deux fait relire la même fenêtre ; les objets déjà écrits sont
  identiques, donc republiés sans effet, et les curseurs de table déjà avancés
  sont vérifiés au lieu d'être réécrits ;
- une ligne dont la table sort du manifeste, ou dont l'identité d'événement ne
  correspond pas à sa position native, fait échouer la fenêtre entière. Aucune
  table n'est devinée et aucun événement n'est attribué deux fois.

La couverture d'une table signifie « tous les événements de cette table dans la
plage certifiée sont présents ». Le curseur de table suit donc la fin de la
fenêtre lue, même quand la table n'a rien produit : c'est ce qui rend le retard
destination calculable sur une période calme.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import re
from typing import Callable, Mapping, Protocol, Sequence

from .contract import ChangeEvent, JournalPosition
from .object_store import (
    CheckpointStore,
    ObjectStore,
    ReceiptCaptureResult,
    publish_raw_artifacts,
    read_raw_batch,
)
from .raw import RawBatchWriter, _batch_id
from .storage_layout import journal_prefix


TABLE_PREFIX_SEGMENT = "journal"
_TABLE_IDENTIFIER = re.compile(r"^[A-Z0-9_]{1,64}$")
_EVENT_KEYS = ("event_id", "table")


class FleetRoutingError(RuntimeError):
    """La fenêtre certifiée ne peut pas être routée sans ambiguïté."""


class FleetConfigurationError(ValueError):
    """Réglage de flotte invalide.

    Le message est écrit dans ce module et ne reprend aucune donnée reçue : il
    peut donc être journalisé par le pod sans risque de fuite.
    """


def parse_fleet_tables(tables: Sequence[str]) -> tuple[str, ...]:
    """Valide la liste de tables d'une flotte, sans jamais la trier."""

    if isinstance(tables, (str, bytes)):
        raise ValueError("fleet tables must be an explicit sequence")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in tables:
        table = str(item).strip().upper()
        if _TABLE_IDENTIFIER.fullmatch(table) is None:
            raise ValueError("unsafe fleet table identifier")
        if table in seen:
            raise ValueError("fleet tables must be unique")
        seen.add(table)
        normalized.append(table)
    if not normalized:
        raise ValueError("fleet tables must not be empty")
    return tuple(normalized)


ENV_TABLE_ROOT = "AS400_FLEET_TABLE_ROOT"
ENV_TABLES = "AS400_FLEET_TABLES"


@dataclass(frozen=True)
class FleetMode:
    """Réglages de flotte lus dans l'environnement du Job."""

    table_root: str
    tables: tuple[str, ...]


def fleet_mode_from_environment(environ: Mapping[str, str]) -> FleetMode | None:
    """Retourne le mode flotte, ou None pour une capture mono-flux.

    Les deux variables vont ensemble : une seule des deux serait un Job
    mal configuré, pas une capture à une table. Une flotte lit au moins deux
    tables et son préfixe doit rester relatif.
    """

    declared_root = str(environ.get(ENV_TABLE_ROOT, "") or "").strip()
    raw_tables = str(environ.get(ENV_TABLES, "") or "").strip()
    if not declared_root and not raw_tables:
        return None
    if not declared_root or not raw_tables:
        raise FleetConfigurationError(
            f"{ENV_TABLE_ROOT} and {ENV_TABLES} must be supplied together"
        )
    root = declared_root.strip("/")
    if declared_root.startswith("/") or not root or ".." in root.split("/"):
        raise FleetConfigurationError("fleet table root must stay relative")
    tables = parse_fleet_tables(raw_tables.split(","))
    if len(tables) < 2:
        raise FleetConfigurationError("fleet mode requires at least two tables")
    return FleetMode(table_root=root, tables=tables)


def table_object_prefix(root: str, table: str) -> str:
    """Préfixe objet stable d'une table, indépendant du run de lecture.

    Convention du dépôt (unique source de vérité :
    ``quadringent.storage_layout``) : la table est le premier segment, le
    type de flux vient ensuite, comme `<racine>/<table>/snapshot/...` ou
    `<racine>/<table>/journal/...`. Une table de flotte occupe donc
    `<root>/<table>/journal/`, et non un répertoire commun aux flux du site.
    Le mode une seule table (``build_reader_deployment`` sans flotte) utilise
    désormais la même formule — voir ``storage_layout.journal_prefix``.
    """

    cleaned_root = root.strip().strip("/")
    if not cleaned_root or ".." in cleaned_root.split("/"):
        raise ValueError("fleet table root is unsafe")
    normalized = parse_fleet_tables([table])[0]
    return journal_prefix(cleaned_root, normalized)


def table_checkpoint_key(root: str, table: str) -> str:
    """Clé de curseur durable, alignée sur le préfixe objet de la table."""

    return table_object_prefix(root, table)


@dataclass(frozen=True)
class TableWindow:
    """Les octets exacts d'une table pour une fenêtre certifiée."""

    table: str
    payload: bytes
    manifest: bytes
    event_count: int
    batch_id: str

    @property
    def payload_key(self) -> str:
        return f"batch-{self.batch_id}.jsonl"

    @property
    def manifest_key(self) -> str:
        return f"batch-{self.batch_id}.manifest.json"


@dataclass(frozen=True)
class RoutingReport:
    routed_tables: int
    published_tables: int
    reused_tables: int
    routed_events: int
    payload_bytes: int


class FleetStoreFactory(Protocol):
    def __call__(self, prefix: str) -> ObjectStore: ...


class FleetCheckpointFactory(Protocol):
    def __call__(self, stream_key: str) -> CheckpointStore: ...


class FleetTableRouter:
    """Publie chaque table dans son préfixe, sous curseur dédié."""

    def __init__(
        self,
        *,
        root: str,
        tables: Sequence[str],
        store_factory: FleetStoreFactory,
        checkpoint_factory: FleetCheckpointFactory,
    ) -> None:
        if not callable(store_factory) or not callable(checkpoint_factory):
            raise ValueError("fleet table router requires explicit factories")
        self.root = root.strip().strip("/")
        if not self.root:
            raise ValueError("fleet table root is required")
        self.tables = parse_fleet_tables(tables)
        self._store_factory = store_factory
        self._checkpoint_factory = checkpoint_factory

    def route_window(
        self,
        *,
        end: JournalPosition,
        manifest_content: bytes | None = None,
        payload: bytes | None = None,
    ) -> RoutingReport:
        """Route une fenêtre certifiée, ou couvre son absence d'événement."""

        if (manifest_content is None) != (payload is None):
            raise ValueError("raw payload and manifest must be supplied together")
        windows: dict[str, TableWindow] = {}
        if manifest_content is not None:
            windows = {
                window.table: window
                for window in split_window_batch(
                    manifest_content,
                    payload or b"",
                    tables=self.tables,
                    expected_end=end,
                )
            }
        published = 0
        reused = 0
        events = 0
        payload_bytes = 0
        for table in self.tables:
            window = windows.get(table)
            if window is not None:
                events += window.event_count
                payload_bytes += len(window.payload)
            if self._cover(table, end, window) == "published":
                published += 1
            else:
                reused += 1
        return RoutingReport(
            routed_tables=len(self.tables),
            published_tables=published,
            reused_tables=reused,
            routed_events=events,
            payload_bytes=payload_bytes,
        )

    def _cover(self, table: str, end: JournalPosition, window: TableWindow | None) -> str:
        store = self._store_factory(table_object_prefix(self.root, table))
        checkpoint = self._checkpoint_factory(table_checkpoint_key(self.root, table))
        current = checkpoint.load()
        if current is not None and current.receiver == end.receiver and current.sequence == end.sequence:
            if window is not None:
                self._assert_published(store, window)
            return "reused"
        if current is not None and current.receiver == end.receiver and current.sequence > end.sequence:
            raise FleetRoutingError("table cursor is ahead of the certified journal window")
        if window is not None:
            publish_raw_artifacts(
                store,
                window.manifest,
                window.payload,
                payload_key=window.payload_key,
                manifest_key=window.manifest_key,
            )
        if current is None:
            checkpoint.compare_and_set(None, end)
        elif current.receiver != end.receiver:
            checkpoint.transition(current, end)
        else:
            checkpoint.compare_and_set(current, end)
        return "published"

    def _assert_published(self, store: ObjectStore, window: TableWindow) -> None:
        try:
            published_payload = store.get(window.payload_key)
            published_manifest = store.get(window.manifest_key)
        except FileNotFoundError:
            raise FleetRoutingError(
                "a certified table window is marked covered but its objects are missing"
            ) from None
        if published_payload != window.payload or published_manifest != window.manifest:
            raise FleetRoutingError(
                "a certified table window cannot be replayed with different content"
            )


def split_window_batch(
    manifest_content: bytes,
    payload: bytes,
    *,
    tables: Sequence[str],
    expected_end: JournalPosition | None = None,
) -> tuple[TableWindow, ...]:
    """Découpe une fenêtre certifiée par table, octets d'origine préservés."""

    allowed = parse_fleet_tables(tables)
    batch = read_raw_batch(manifest_content, payload)
    if expected_end is not None and batch.manifest.high_watermark != expected_end:
        raise FleetRoutingError("raw high watermark does not match the certified window end")
    grouped: dict[str, list[tuple[bytes, str]]] = {}
    order: list[str] = []
    # Les lignes sont découpées sur le seul terminateur JSONL et jamais
    # réencodées : une table reçoit exactement les octets produits par le
    # lecteur Java, sans repasser par une sérialisation Python.
    for raw_line in payload.split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise FleetRoutingError("raw payload line is not valid JSON") from None
        if not isinstance(record, Mapping):
            raise FleetRoutingError("raw payload line is not an object")
        missing = [key for key in _EVENT_KEYS if key not in record]
        if missing:
            raise FleetRoutingError("raw payload line is missing an event identity")
        table = str(record["table"]).strip().upper()
        if table not in allowed:
            raise FleetRoutingError("raw payload contains a table outside the fleet manifest")
        event = ChangeEvent.from_record(record)
        if str(record["event_id"]) != event.event_id:
            raise FleetRoutingError("raw payload event identity does not match its position")
        if table not in grouped:
            grouped[table] = []
            order.append(table)
        grouped[table].append((raw_line, event.event_id))
    total_lines = sum(len(lines) for lines in grouped.values())
    if total_lines != batch.manifest.event_count:
        raise FleetRoutingError("raw payload line count differs from its manifest")
    windows = []
    for table in order:
        lines = grouped[table]
        table_payload = b"".join(line + b"\n" for line, _event_id in lines)
        event_ids = [event_id for _line, event_id in lines]
        payload_sha256 = hashlib.sha256(table_payload).hexdigest()
        batch_id = _batch_id(
            batch.manifest.format_version,
            event_ids,
            batch.manifest.high_watermark,
            payload_sha256,
        )
        manifest = {
            "batch_id": batch_id,
            "format_version": batch.manifest.format_version,
            "event_count": len(lines),
            "event_ids": event_ids,
            "high_watermark": {
                "receiver": batch.manifest.high_watermark.receiver,
                "sequence": batch.manifest.high_watermark.sequence,
            },
            "payload_sha256": payload_sha256,
        }
        manifest_content_for_table = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        # Le document produit est relu par le lecteur du dépôt : une
        # reconstruction fautive échoue ici plutôt que dans le lake.
        read_raw_batch(manifest_content_for_table, table_payload)
        windows.append(
            TableWindow(
                table=table,
                payload=table_payload,
                manifest=manifest_content_for_table,
                event_count=len(lines),
                batch_id=batch_id,
            )
        )
    return tuple(windows)


class FleetWindowCoordinator:
    """Route une fenêtre vers les tables avant d'avancer le curseur du journal.

    L'ordre est la garantie : les treize tables d'abord, le curseur du journal
    ensuite. Un arrêt au milieu fait relire la fenêtre, jamais sauter un lot.
    """

    def __init__(self, inner: object, router: FleetTableRouter) -> None:
        for attribute in ("store", "checkpoint_store"):
            if getattr(inner, attribute, None) is None:
                raise ValueError("fleet coordinator requires a complete inner coordinator")
        required = (
            "capture_receipted_window_result",
            "capture_raw_transition",
            "capture_raw",
            "advance_without_raw",
        )
        for name in required:
            if not callable(getattr(inner, name, None)):
                raise ValueError("fleet coordinator requires the full capture contract")
        self._inner = inner
        self._router = router
        self.last_report: RoutingReport | None = None

    def _route(
        self,
        *,
        end: JournalPosition,
        manifest_content: bytes | None = None,
        payload: bytes | None = None,
    ) -> RoutingReport:
        report = self._router.route_window(
            end=end,
            manifest_content=manifest_content,
            payload=payload,
        )
        self.last_report = report
        return report

    @property
    def store(self) -> object:
        return self._inner.store

    @property
    def checkpoint_store(self) -> object:
        return self._inner.checkpoint_store

    @property
    def tables(self) -> tuple[str, ...]:
        return self._router.tables

    def capture_receipted_window_result(
        self,
        *,
        start: JournalPosition,
        end: JournalPosition,
        previous: JournalPosition | None,
        manifest_content: bytes | None = None,
        payload: bytes | None = None,
        scan_completed_at: object | None = None,
    ) -> ReceiptCaptureResult:
        self._route(end=end, manifest_content=manifest_content, payload=payload)
        return self._inner.capture_receipted_window_result(
            start=start,
            end=end,
            previous=previous,
            manifest_content=manifest_content,
            payload=payload,
            scan_completed_at=scan_completed_at,
        )

    def capture_raw_transition(
        self,
        manifest_content: bytes,
        payload: bytes,
        *,
        previous: JournalPosition | None,
    ) -> object:
        batch = read_raw_batch(manifest_content, payload)
        self._route(
            end=batch.manifest.high_watermark,
            manifest_content=manifest_content,
            payload=payload,
        )
        return self._inner.capture_raw_transition(
            manifest_content,
            payload,
            previous=previous,
        )

    def capture_raw(
        self,
        manifest_content: bytes,
        payload: bytes,
        *,
        payload_key: str,
        manifest_key: str,
    ) -> object:
        batch = read_raw_batch(manifest_content, payload)
        self._route(
            end=batch.manifest.high_watermark,
            manifest_content=manifest_content,
            payload=payload,
        )
        return self._inner.capture_raw(
            manifest_content,
            payload,
            payload_key=payload_key,
            manifest_key=manifest_key,
        )

    def advance_without_raw(
        self,
        position: JournalPosition,
        *,
        previous: JournalPosition | None = None,
    ) -> None:
        self._route(end=position)
        self._inner.advance_without_raw(position, previous=previous)
