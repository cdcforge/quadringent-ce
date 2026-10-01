#!/usr/bin/env python
"""Chargeur de destination : historique Snowpipe/SQL + MERGE miroir.

Un processus par destination (voir ``quadringent_control_plane.v2.executor.
manifests.build_loader_deployment``, un Deployment K8s par destination,
réplique unique). Pour chaque table déclarée (``QUADRINGENT_LOADER_TABLE_SET_
JSON``) : lit les lots bruts déjà rendus durables par la capture (jamais un
lot en cours d'écriture — voir ``object_store.publish_raw_artifacts``, le
manifeste est publié en dernier), dans l'ordre de position de journal, après
son propre checkpoint (distinct de celui de la capture, isolé par run de
copie initiale) ; charge chaque lot dans l'historique via Snowpipe Streaming
ou un MERGE SQL synchrone, puis
matérialise le miroir par un ``MERGE`` (:class:`MirrorMergePlan`) ; mesure et
publie le retard (:class:`LagQueryPlan`).

Découverte des lots bruts : ce module s'appuie sur les reçus de fenêtre
(``object_store.ObjectStore.list_receipt_keys``/``RawFirstCaptureCoordinator.
capture_receipted_window_result``) — chaque reçu porte la position de fin de
fenêtre et, si la fenêtre a produit des événements, la paire de clés
``payload_key``/``manifest_key`` du lot brut correspondant. C'est le seul
mécanisme de découverte déjà borné et portable AWS/GCS dans ce dépôt (voir
``docs/product/snowflake-destination.md``) ; un site qui publie sans reçus
(chemin ``capture()`` historique, sans fenêtres de preuve) n'est pas encore
couvert par ce chargeur.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import ExitStack
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterator

from quadringent.contract import JournalPosition
from quadringent.object_store import CheckpointStore, ObjectStore, read_published_batch
from quadringent.raw import RawBatch, read_raw_batch
from quadringent.site_config import SnowflakeScope
from quadringent.snowflake_destination import (
    ColumnDefinition,
    IbmiColumnType,
    TableDestinationPlan,
    default_history_table_name,
    default_mirror_table_name,
)
from quadringent.snowflake_streaming_loader import (
    HistoryStreamingLoader,
    HistoryStreamingLoadResult,
    LagQueryPlan,
    MirrorMergePlan,
    SnowpipeStreamingClientAdapter,
    StreamingLagMetrics,
    StreamingCycleMetrics,
    loader_table_tag,
    measure_loader_stage,
    account_url_host,
)
from quadringent.snowflake_sql_loader import HistorySqlLoader
from quadringent.storage_backend import StorageBackend
from quadringent.storage_layout import journal_prefix, snapshot_prefix

# Ce script tourne dans l'image du control plane (voir docker/control-plane.
# Dockerfile) : il peut importer ce paquet, contrairement à l'image de
# capture, qui ne le porte pas (la preuve de copie initiale est un concept du
# control plane — voir quadringent.storage_layout, en tête de module).
from quadringent_control_plane.v2.executor.evidence import EvidenceReader, InitialCopyEvidence

LOG = logging.getLogger("quadringent.destination_loader")

_RECEIPT_READ_BUDGET_BYTES = 65_536
_RECEIPT_LIST_BUDGET = 5_000
_RECEIPT_CACHE_BYTES = 16 * 1024 * 1024


class DestinationLoaderError(RuntimeError):
    """Configuration ou état invalide — le processus doit s'arrêter (fail-closed)."""


class ImmutableReceiptCache:
    """LRU d'octets de reçus ``put_once``, limité à un backend et au processus.

    Le listing et la validation de chaîne restent exécutés à chaque passage.
    Une éviction ou un redémarrage impose simplement une nouvelle lecture.
    Le chargeur est séquentiel ; ce cache n'est pas partagé entre threads.
    """

    def __init__(self, storage: StorageBackend, *, max_entries: int = _RECEIPT_LIST_BUDGET,
                 max_bytes: int = _RECEIPT_CACHE_BYTES) -> None:
        if any(type(limit) is not int or limit <= 0 for limit in (max_entries, max_bytes)):
            raise ValueError("limites du cache de reçus invalides")
        self.storage = storage
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._bytes = 0

    def object_store(self, prefix: str) -> ObjectStore:
        return _CachedReceiptStore(self, self.storage.object_store(prefix), prefix)

    def _read(self, store: ObjectStore, prefix: str, key: str, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("invalid object read budget")
        identity = (prefix, key)
        content = self._entries.get(identity)
        if content is not None:
            if len(content) > max_bytes:
                raise ValueError("object exceeds read budget")
            self._entries.move_to_end(identity)
            return content
        content = store.get_bounded(key, max_bytes)
        if len(content) <= self._max_bytes:
            while self._entries and (len(self._entries) >= self._max_entries
                                     or self._bytes + len(content) > self._max_bytes):
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)
            self._entries[identity] = content
            self._bytes += len(content)
        return content


class _CachedReceiptStore:
    """Décore seulement les lectures bornées de ``receipts/*.json``."""

    def __init__(self, cache: ImmutableReceiptCache, store: ObjectStore, prefix: str) -> None:
        self._cache, self._store, self._prefix = cache, store, prefix

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        if key.startswith("receipts/") and key.endswith(".json") and "/" not in key[len("receipts/"):]:
            return self._cache._read(self._store, self._prefix, key, max_bytes)
        return self._store.get_bounded(key, max_bytes)

    def get(self, key: str) -> bytes:
        return self._store.get(key)

    def put_once(self, key: str, content: bytes) -> bool:
        return self._store.put_once(key, content)

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        return self._store.list_receipt_keys(max_keys)


@dataclass(frozen=True)
class LoaderTable:
    """Une table du lot ``QUADRINGENT_LOADER_TABLE_SET_JSON``, déjà validée.

    ``evidence_key`` : clé de la preuve de copie initiale
    (``quadringent_control_plane.v2.executor.evidence.evidence_key``), posée
    par le control plane une fois ``active_run_id`` connu pour cette table
    (voir ``v2/executor/kubernetes.py::_desired_loader_manifest``). Absente
    tant que la copie n'a pas commencé — le chargeur charge alors seulement
    les événements de journal, comme avant cette disposition."""

    table_id: str
    schema_name: str
    table_name: str
    key_columns: tuple[str, ...]
    columns: tuple[dict[str, Any], ...]
    evidence_key: str | None = None


class StreamingSessionPool:
    """Garde un canal Snowpipe par table pendant toute la vie du chargeur.

    Le SDK recommande des canaux durables : rouvrir le canal à chaque reçu
    ajoute une reprise distante et rend la latence sensible aux périodes
    calmes. Le Deployment redémarre si la définition des tables change.
    """

    def __init__(self, client_factory: Any, *, flush_each_batch: bool = False) -> None:
        self._client_factory = client_factory
        self._flush_each_batch = flush_each_batch
        self._sessions: dict[str, tuple[TableDestinationPlan, Any, HistoryStreamingLoader]] = {}
        self._closed = False

    def for_table(
        self, table: LoaderTable, plan: TableDestinationPlan,
        *, cycle_metrics: StreamingCycleMetrics | None = None,
    ) -> HistoryStreamingLoader:
        if self._closed:
            raise DestinationLoaderError("pool Snowpipe fermé")
        existing = self._sessions.get(table.table_id)
        if existing is not None:
            if existing[0] != plan:
                raise DestinationLoaderError("schéma de table modifié sans redémarrage du chargeur")
            return existing[2]
        with measure_loader_stage(cycle_metrics, "open_channel"):
            client = self._client_factory(plan.history_table)
            try:
                history_loader = HistoryStreamingLoader(
                    plan=plan, client=client, stream_id=loader_stream_id(table),
                    flush_each_batch=self._flush_each_batch,
                )
            except BaseException:
                client.close()
                raise
        self._sessions[table.table_id] = (plan, client, history_loader)
        return history_loader

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with ExitStack() as closing:
            for _plan, client, history_loader in self._sessions.values():
                closing.callback(client.close)
                closing.callback(history_loader.close)
            self._sessions.clear()


def parse_table_set(raw_json: str) -> tuple[LoaderTable, ...]:
    """Parse ``QUADRINGENT_LOADER_TABLE_SET_JSON`` — jamais de valeur par défaut.

    Chaque entrée doit porter des colonnes non vides (voir ``services/tables.
    py::set_discovered_columns`` côté control plane) : ce module refuse de
    démarrer une table sans schéma déclaré plutôt que d'en deviner un.
    """

    try:
        entries = json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise DestinationLoaderError(f"QUADRINGENT_LOADER_TABLE_SET_JSON invalide : {error}") from error
    if not isinstance(entries, list) or not entries:
        raise DestinationLoaderError("QUADRINGENT_LOADER_TABLE_SET_JSON doit lister au moins une table")
    tables = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DestinationLoaderError("chaque table doit être un objet")
        columns = entry.get("columns")
        if not isinstance(columns, list) or not columns:
            raise DestinationLoaderError(
                f"table {entry.get('table')!r} sans colonnes déclarées — voir "
                "PUT /v2/tables/{id}/discovered-columns"
            )
        evidence_key = entry.get("evidence_key")
        tables.append(
            LoaderTable(
                table_id=str(entry["table_id"]),
                schema_name=str(entry["schema"]),
                table_name=str(entry["table"]),
                key_columns=tuple(entry.get("key_columns", ())),
                columns=tuple(columns),
                evidence_key=str(evidence_key) if evidence_key else None,
            )
        )
    return tuple(tables)


def build_plan(table: LoaderTable, *, database: str, schema: str, mirror_schema: str | None = None) -> TableDestinationPlan:
    """Traduit une table déclarée en plan DDL/MERGE — jamais de type deviné."""

    scope = SnowflakeScope(database=database, schema=schema)
    columns = tuple(
        ColumnDefinition(
            name=column["name"],
            type=IbmiColumnType(
                kind=column["kind"],
                length=column.get("length"),
                precision=column.get("precision"),
                scale=column.get("scale"),
                timestamp_precision=column.get("timestamp_precision", 6) or 6,
                ccsid=column.get("ccsid"),
            ),
            nullable=bool(column.get("nullable", True)),
        )
        for column in table.columns
    )
    if not table.key_columns:
        raise DestinationLoaderError(f"table {table.table_name!r} sans clé déclarée (key_columns)")
    return TableDestinationPlan(
        scope=scope,
        history_table=default_history_table_name(table.table_name),
        mirror_table=default_mirror_table_name(table.table_name),
        columns=columns,
        key_columns=table.key_columns,
        mirror_scope=SnowflakeScope(database=database, schema=mirror_schema) if mirror_schema is not None else None,
    )


def raw_prefix_for_table(raw_prefix_root: str, schema_name: str, table_name: str) -> str:
    """Préfixe brut d'une table sous la racine du site.

    Délègue à la disposition unique (``quadringent.storage_layout``), la
    même que le lecteur (mode une seule table *et* mode flotte, voir
    ``v2/executor/manifests.py::build_reader_deployment`` et
    ``quadringent.fleet_capture.table_object_prefix``) : ``<racine>/<table
    minuscule>/journal``. ``schema_name`` n'est plus utilisé dans la clé —
    conservé dans la signature pour ne pas changer l'appelant — depuis le
    constat du 24 septembre 2026 : ce module cherchait auparavant sous
    ``<racine>/<SCHÉMA>/<TABLE>`` (convention « v1 », jamais produite par le
    lecteur v2), donc ne trouvait jamais rien."""

    del schema_name
    return journal_prefix(raw_prefix_root, table_name)


def loader_checkpoint_stream_key(table_id: str, evidence_key: str | None = None) -> str:
    """Une copie initiale nouvelle possède un checkpoint de chargeur neuf."""

    base = f"{table_id}-destination-loader"
    return base if not evidence_key else f"{base}-{hashlib.sha256(evidence_key.encode()).hexdigest()[:16]}"


def loader_scope_binding_key(table: LoaderTable) -> str:
    checkpoint_key = loader_checkpoint_stream_key(table.table_id, table.evidence_key)
    return f"loader-scopes/{hashlib.sha256(checkpoint_key.encode()).hexdigest()}.json"


def bind_loader_scopes(*, storage: StorageBackend, tables: tuple[LoaderTable, ...], raw_prefix_root: str,
                       account: str, database: str, schema: str, mirror_schema: str) -> None:
    """Lie durablement chaque checkpoint au scope avant toute écriture Snowflake.

    Le marqueur put_once précède le premier chargement et survit au Deployment.
    Un checkpoint historique sans provenance ne peut pas être adopté : une
    nouvelle copie initiale crée un autre run, sans effacer l'ancien curseur.
    Les anciens processus doivent être arrêtés avant l'upgrade.
    """
    for table in tables:
        plan = build_plan(table, database=database, schema=schema, mirror_schema=mirror_schema)
        store = storage.object_store(raw_prefix_root)
        key = loader_scope_binding_key(table)
        expected = {
            "format_version": "quadringent-loader-scope-v1",
            "account": account.upper(),
            "history": plan.qualified_history_table,
            "mirror": plan.qualified_mirror_table,
        }
        try:
            raw = store.get_bounded(key, 4096)
        except FileNotFoundError:
            checkpoint = storage.checkpoint_store(loader_checkpoint_stream_key(table.table_id, table.evidence_key))
            if checkpoint.load() is not None:
                raise DestinationLoaderError(
                    "checkpoint sans périmètre Snowflake prouvé : nouvelle copie initiale requise"
                ) from None
            # Relire après put_once couvre aussi une création concurrente refusée.
            store.put_once(key, json.dumps(expected, sort_keys=True, separators=(",", ":")).encode())
            raw = store.get_bounded(key, 4096)
        try:
            observed = json.loads(raw)
        except (ValueError, UnicodeError):
            raise DestinationLoaderError("périmètre Snowflake du checkpoint illisible") from None
        if observed != expected:
            raise DestinationLoaderError("périmètre Snowflake du checkpoint différent : nouvelle copie initiale requise")


def loader_stream_id(table: LoaderTable) -> str:
    """Canal Snowpipe stable au sein d'un run, isolé entre deux copies."""

    base = f"{table.schema_name}/{table.table_name}"
    if table.evidence_key:
        return f"{base}/{hashlib.sha256(table.evidence_key.encode()).hexdigest()[:16]}"
    return base


@dataclass(frozen=True)
class PendingReceipt:
    """Une fenêtre prouvée, y compris vide, dans l'ordre du journal."""

    previous: JournalPosition | None
    start: JournalPosition
    position: JournalPosition
    raw: dict[str, str] | None
    store: ObjectStore
    fleet: bool


def _receipt_position(value: Any) -> JournalPosition | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DestinationLoaderError("position de reçu invalide")
    try:
        return JournalPosition(receiver=str(value["receiver"]), sequence=int(value["sequence"]))
    except (KeyError, TypeError, ValueError) as error:
        raise DestinationLoaderError("position de reçu invalide") from error


def discover_pending_receipts(
    store: ObjectStore, checkpoint_store: CheckpointStore, *, fleet_store: ObjectStore | None = None,
    max_receipts: int = _RECEIPT_LIST_BUDGET,
    predecessors: dict[JournalPosition, JournalPosition | None] | None = None,
) -> list[PendingReceipt]:
    """Suit les liens ``previous`` des reçus, sans ordonner les receveurs par nom.

    Une fenêtre vide peut porter la rotation de receveur. Elle doit donc
    avancer le checkpoint du chargeur avant le prochain lot non vide. Les
    reçus antérieurs au checkpoint sont exclus même après une rotation.
    """

    by_previous: dict[JournalPosition | None, PendingReceipt] = {}
    ends: set[JournalPosition] = set()
    sources = ((store, False),) if fleet_store is None else ((store, False), (fleet_store, True))
    for source, fleet in sources:
        for key in source.list_receipt_keys(max_receipts):
            receipt = json.loads(source.get_bounded(key, _RECEIPT_READ_BUDGET_BYTES))
            if receipt.get("format_version") not in ("quadringent-scan-receipt-v1", "quadringent-scan-receipt-v2"):
                raise DestinationLoaderError("format de reçu inconnu")
            previous = _receipt_position(receipt.get("previous"))
            start = _receipt_position(receipt.get("start"))
            position = _receipt_position(receipt.get("end"))
            if start is None or position is None or position == previous:
                raise DestinationLoaderError("fenêtre de reçu sans progression")
            if start.receiver != position.receiver or start.sequence > position.sequence:
                raise DestinationLoaderError("plage de reçu invalide")
            if previous is not None and previous.receiver == start.receiver and start.sequence != previous.sequence + 1:
                raise DestinationLoaderError("rupture dans la chaîne de reçus")
            raw = receipt.get("raw")
            if raw is not None and (not isinstance(raw, dict) or not all(
                isinstance(raw.get(name), str) for name in ("payload_key", "manifest_key")
            )):
                raise DestinationLoaderError("référence de lot brut invalide")
            if previous in by_previous:
                raise DestinationLoaderError("reçus concurrents pour un même prédécesseur")
            if predecessors is not None:
                if position in predecessors and predecessors[position] != previous:
                    raise DestinationLoaderError("position de reçu avec plusieurs prédécesseurs")
                predecessors[position] = previous
            by_previous[previous] = PendingReceipt(previous, start, position, raw, source, fleet)
            ends.add(position)

    if not by_previous:
        return []
    resume = checkpoint_store.load()
    if resume is None:
        roots = [previous for previous in by_previous if previous not in ends]
        if len(roots) != 1:
            raise DestinationLoaderError("chaîne de reçus sans origine unique")
        resume = roots[0]
    pending: list[PendingReceipt] = []
    if resume is not None and resume not in by_previous:
        # Un ancien lecteur peut publier sa première fenêtre après la copie
        # avec previous=null. La frontière de copie donne alors le seul lien
        # sûr : même receveur et première séquence exactement suivante.
        first = by_previous.get(None)
        if first is not None and first.start.receiver == resume.receiver and first.start.sequence == resume.sequence + 1:
            pending.append(first)
            resume = first.position
        else:
            # Une nouvelle copie peut placer sa frontière au milieu d'une
            # fenêtre déjà publiée par le lecteur partagé. Le manifeste brut
            # sera validé puis filtré à la frontière avant le chargement.
            overlapping = [
                item for item in by_previous.values()
                if item.start.receiver == resume.receiver == item.position.receiver
                and item.start.sequence <= resume.sequence < item.position.sequence
            ]
            if len(overlapping) > 1:
                raise DestinationLoaderError("plusieurs reçus couvrent la frontière de copie")
            if overlapping:
                pending.append(overlapping[0])
                resume = overlapping[0].position
            elif any(
                item.start.receiver == resume.receiver and item.start.sequence > resume.sequence + 1
                for item in by_previous.values()
            ):
                raise DestinationLoaderError("trou après la frontière de copie initiale")
    visited: set[JournalPosition | None] = set()
    while resume in by_previous:
        if resume in visited:
            raise DestinationLoaderError("cycle dans la chaîne de reçus")
        visited.add(resume)
        item = by_previous[resume]
        pending.append(item)
        resume = item.position
    return pending


def committed_receipt_index(
    pending: list[PendingReceipt],
    committed: JournalPosition | None,
    checkpoint: JournalPosition | None,
    predecessors: dict[JournalPosition, JournalPosition | None],
) -> int | None:
    """Repère le reçu contenant le jeton Snowpipe dans la chaîne prouvée.

    Les numéros de séquence ne sont comparables qu'au sein d'un receveur.
    Si le canal a confirmé un reçu futur après rotation, les reçus antérieurs
    ne doivent pas être réinsérés ; leur MERGE miroir doit tout de même être
    rejoué avant de déplacer le checkpoint local.
    """

    if committed is None:
        return None
    if committed.receiver.startswith("SNAPSHOT-") and checkpoint is not None:
        # La copie initiale utilise un receveur synthétique, distinct du
        # journal IBM i. Son checkpoint est posé à la frontière réelle avant
        # la découverte des reçus du journal.
        return None
    # Le jeton peut précéder le checkpoint, notamment si une rotation vide a
    # été validée après la dernière ligne streamée. Prouver ce cas en remontant
    # les liens exacts plutôt qu'en ordonnant les noms de receveur.
    position = checkpoint
    visited: set[JournalPosition] = set()
    while position is not None and position not in visited:
        if position.receiver == committed.receiver and position.sequence >= committed.sequence:
            return None
        visited.add(position)
        position = predecessors.get(position)
    for index, receipt in enumerate(pending):
        if receipt.position.receiver == committed.receiver and receipt.position.sequence >= committed.sequence:
            return index
    raise DestinationLoaderError("jeton Snowpipe absent de la chaîne de reçus et du checkpoint")


def existing_history_event_ids(plan: TableDestinationPlan, batch: RawBatch, cursor: Any) -> frozenset[str]:
    """Vérifie l'historique quand un canal recréé a perdu son jeton d'offset."""

    ids = tuple(dict.fromkeys(event.event_id for event in batch.events))
    if any(re.fullmatch(r"[0-9a-f]{64}", event_id) is None for event_id in ids):
        raise DestinationLoaderError("EVENT_ID invalide dans le lot brut")
    found: set[str] = set()
    for start in range(0, len(ids), 1_000):
        chunk = ids[start : start + 1_000]
        values = ", ".join(f"'{event_id}'" for event_id in chunk)
        cursor.execute(f"SELECT EVENT_ID FROM {plan.qualified_history_table} WHERE EVENT_ID IN ({values})")
        found.update(str(row[0]) for row in cursor.fetchall())
    if not found.issubset(ids):
        raise DestinationLoaderError("historique incohérent avec les EVENT_ID demandés")
    return frozenset(found)


def advance_loader_checkpoint(checkpoint_store: CheckpointStore, position: JournalPosition) -> None:
    """Valide explicitement la rotation contre le prédécesseur exact."""

    previous = checkpoint_store.load()
    if previous is not None and previous.receiver != position.receiver:
        checkpoint_store.transition(previous, position)
    else:
        checkpoint_store.commit(position)


def discover_new_batches(
    store: ObjectStore, checkpoint_store: CheckpointStore, *, max_receipts: int = _RECEIPT_LIST_BUDGET
) -> Iterator[tuple[JournalPosition, RawBatch]]:
    """Énumère les lots bruts publiés après le dernier checkpoint du chargeur.

    S'appuie sur les reçus de fenêtre (``receipts/`` — voir
    ``RawFirstCaptureCoordinator.capture_receipted_window_result``) : chaque
    reçu porte la position de fin de fenêtre et, si la fenêtre a produit des
    événements, la référence du lot brut correspondant. Les fenêtres vides
    (``raw`` absent) sont ignorées — rien à charger. Trié par position
    (receveur, séquence) avant filtrage : les clés de reçu sont des hachages,
    jamais dans l'ordre chronologique.
    """

    resume = checkpoint_store.load()
    entries: list[tuple[JournalPosition, dict[str, str]]] = []
    for key in store.list_receipt_keys(max_receipts):
        receipt = json.loads(store.get_bounded(key, _RECEIPT_READ_BUDGET_BYTES))
        raw = receipt.get("raw")
        end = receipt.get("end")
        if raw is None or end is None:
            continue
        position = JournalPosition(receiver=str(end["receiver"]), sequence=int(end["sequence"]))
        if resume is not None and position.receiver == resume.receiver and position.sequence <= resume.sequence:
            continue
        entries.append((position, raw))
    entries.sort(key=lambda item: (item[0].receiver, item[0].sequence))
    for position, raw in entries:
        batch = read_published_batch(store, raw["payload_key"], raw["manifest_key"])
        yield position, batch


def discover_new_fleet_batches(
    root_store: ObjectStore, checkpoint_store: CheckpointStore, table_name: str,
    *, max_receipts: int = _RECEIPT_LIST_BUDGET,
) -> Iterator[tuple[JournalPosition, RawBatch]]:
    """Découvre les lots d'une table dont le journal est partagé (mode flotte).

    Constat en réel sur GKE après la correction de disposition du 24
    septembre 2026 : deux tables sur un même journal (mode flotte,
    ``AS400_FLEET_TABLES``) font écrire le lecteur ainsi (voir
    ``quadringent.fleet_capture.FleetWindowCoordinator``) :

    - le lot combiné de la fenêtre (toutes les tables de la flotte, octets
      d'origine) et le reçu de fenêtre, tous deux à la **racine** du
      préfixe du journal (``RawFirstCaptureCoordinator`` interne) — un seul
      reçu par position scannée, puisqu'une lecture de journal ne fait
      qu'une passe pour toutes les tables ;
    - le lot **routé par table** (``FleetTableRouter``), publié sous
      ``<racine>/<table>/journal/`` — jamais accompagné d'un reçu à cet
      endroit : la couverture d'une table est un sous-produit de la fenêtre
      couverte à la racine, pas un évènement séparé.

    Le lot combiné à la racine n'est donc pas un doublon fautif : c'est la
    preuve de fenêtre, commune à toute la flotte. :func:`discover_new_batches`
    (reçus *sous* le préfixe de table) ne le voit jamais ; cette fonction lit
    les reçus à la racine et retrouve les événements de la table en
    ré-appliquant le même découpage que le lecteur
    (``fleet_capture.split_window_batch``, déterministe : les octets et
    l'identité de lot obtenus ici sont ceux déjà publiés sous
    ``<racine>/<table>/journal/`` — jamais relus depuis là, pour épargner un
    aller-retour objet supplémentaire).

    Appelée inconditionnellement en plus de :func:`discover_new_batches` —
    une table seule (jamais en flotte) n'a pas de reçu à la racine, cet
    appel ne renvoie alors simplement rien.
    """

    from quadringent.fleet_capture import split_window_batch

    resume = checkpoint_store.load()
    target = table_name.strip().upper()
    entries: list[tuple[JournalPosition, dict[str, str]]] = []
    for key in root_store.list_receipt_keys(max_receipts):
        receipt = json.loads(root_store.get_bounded(key, _RECEIPT_READ_BUDGET_BYTES))
        raw = receipt.get("raw")
        end = receipt.get("end")
        if raw is None or end is None:
            continue
        position = JournalPosition(receiver=str(end["receiver"]), sequence=int(end["sequence"]))
        if resume is not None and position.receiver == resume.receiver and position.sequence <= resume.sequence:
            continue
        entries.append((position, raw))
    entries.sort(key=lambda item: (item[0].receiver, item[0].sequence))
    for position, raw in entries:
        manifest_content = root_store.get(raw["manifest_key"])
        payload = root_store.get(raw["payload_key"])
        combined = read_raw_batch(manifest_content, payload, payload_name=raw["payload_key"])
        fleet_tables = sorted({event.table.strip().upper() for event in combined.events} | {target})
        windows = split_window_batch(manifest_content, payload, tables=fleet_tables, expected_end=position)
        window = next((w for w in windows if w.table == target), None)
        if window is None:
            continue  # la fenêtre n'a rien produit pour cette table
        yield position, read_raw_batch(window.manifest, window.payload)


def discover_new_batches_from_manifest_keys(
    store: ObjectStore, checkpoint_store: CheckpointStore, manifest_keys: list[str]
) -> Iterator[tuple[JournalPosition, RawBatch]]:
    """Repli sans reçus : ``manifest_keys`` déjà listées par l'appelant.

    ``ObjectStore`` n'expose qu'un listage borné aux reçus
    (``list_receipt_keys``), pas un listage général — un site qui publie via
    ``capture()``/``publish_raw_batch`` sans fenêtres de preuve n'a pas de
    ``receipts/``. L'appelant fournit alors la liste des clés de manifeste
    (``batch-*.manifest.json``) par un moyen propre à son backend (listage
    S3/GCS direct, hors de ce module). Chaque manifeste porte son
    ``high_watermark`` : trié par position avant filtrage, comme
    :func:`discover_new_batches`.
    """

    resume = checkpoint_store.load()
    entries: list[tuple[JournalPosition, str]] = []
    for manifest_key in manifest_keys:
        manifest_content = store.get(manifest_key)
        record = json.loads(manifest_content.decode("utf-8"))
        watermark = record["high_watermark"]
        position = JournalPosition(receiver=str(watermark["receiver"]), sequence=int(watermark["sequence"]))
        if resume is not None and position.receiver == resume.receiver and position.sequence <= resume.sequence:
            continue
        batch_id = str(record["batch_id"])
        entries.append((position, f"batch-{batch_id}.jsonl"))
    entries.sort(key=lambda item: (item[0].receiver, item[0].sequence))
    for position, payload_key in entries:
        manifest_key = payload_key.replace(".jsonl", ".manifest.json")
        batch = read_published_batch(store, payload_key, manifest_key)
        yield position, batch


def _load_history_batch(
    history_loader: HistoryStreamingLoader | HistorySqlLoader, batch: RawBatch, existing: Collection[str],
    cycle_metrics: StreamingCycleMetrics | None,
) -> HistoryStreamingLoadResult:
    if isinstance(history_loader, HistoryStreamingLoader):
        return history_loader.load_batch(batch, already_present_ids=existing, cycle_metrics=cycle_metrics)
    return history_loader.load_batch(batch, already_present_ids=existing)


def load_snapshot_once(
    table: LoaderTable,
    *,
    storage: StorageBackend,
    raw_prefix_root: str,
    checkpoint_store: CheckpointStore,
    history_loader: HistoryStreamingLoader | HistorySqlLoader,
    merge: MirrorMergePlan,
    cursor: Any,
    cycle_metrics: StreamingCycleMetrics | None = None,
) -> bool:
    """Charge l'instantané de copie initiale avant tout événement de journal.

    Idempotent au checkpoint propre à ce run : si celui-ci porte déjà une
    position, rien n'est rechargé. Une nouvelle copie a un nouveau checkpoint
    et remplace le miroir avec son instantané prouvé. Sans preuve lisible,
    le run ne commence pas.

    Renvoie ``True`` si l'instantané a été chargé (ou l'était déjà par un
    passage précédent), pour que l'appelant sache que le checkpoint porte
    désormais au moins la frontière de bascule.
    """

    if not table.evidence_key:
        return False
    if checkpoint_store.load() is not None:
        # Ce checkpoint appartient exclusivement au run courant. Il prouve
        # donc que l'instantané de ce run a déjà été appliqué.
        return True
    with measure_loader_stage(cycle_metrics, "discovery_raw"):
        root_store = storage.object_store("")
        evidence = EvidenceReader(root_store).read(table.evidence_key)
    if evidence is None:
        return False
    if evidence.table_id != table.table_id:
        raise DestinationLoaderError("preuve de copie initiale liée à une autre table")
    # Un nouveau run remplace l'état matérialisé, y compris les clés absentes
    # du nouvel instantané. L'historique garde les époques précédentes.
    # Si le processus s'arrête ici, son checkpoint de run reste absent et le
    # redémarrage efface puis reconstruit de nouveau le miroir.
    with measure_loader_stage(cycle_metrics, "merge"):
        cursor.execute(f"DELETE FROM {merge.plan.qualified_mirror_table}")
    snapshot_store = storage.object_store(snapshot_prefix(raw_prefix_root, table.table_name))
    rows_loaded = 0
    for batch_ref in evidence.snapshot_batches:
        with measure_loader_stage(cycle_metrics, "discovery_raw"):
            batch = read_published_batch(
                snapshot_store, batch_ref.payload_key, batch_ref.manifest_key,
                preserve_decimals=isinstance(history_loader, HistorySqlLoader),
            )
        rows_loaded += len(batch.events)
        existing = existing_history_event_ids(merge.plan, batch, cursor) if history_loader.needs_history_lookup else ()
        _load_history_batch(history_loader, batch, existing, cycle_metrics)
        if batch.events:
            # Les lignes déjà présentes après un crash doivent quand même
            # restaurer le miroir qui vient d'être vidé.
            with measure_loader_stage(cycle_metrics, "merge"):
                merge.execute(cursor, event_ids=tuple(event.event_id for event in batch.events))
    if rows_loaded != evidence.rows_copied:
        raise DestinationLoaderError("preuve de copie initiale incohérente avec ses lots")
    boundary_position = JournalPosition(
        receiver=evidence.boundary.receiver_name, sequence=evidence.boundary.last_sequence
    )
    # Seed le curseur du chargeur à la frontière de bascule : les événements
    # de journal à cette position ou avant (même receiver) sont déjà couverts
    # par l'instantané et ne seront jamais rechargés (voir
    # ``discover_new_batches``) ; ceux d'un autre receiver (rotation après la
    # bascule) restent chargés normalement, ``JournalPosition`` n'ordonnant
    # jamais deux receivers différents entre eux.
    with measure_loader_stage(cycle_metrics, "checkpoint"):
        checkpoint_store.commit(boundary_position)
    return True


def load_table_once(
    table: LoaderTable,
    *,
    plan: TableDestinationPlan,
    storage: StorageBackend,
    raw_prefix_root: str,
    streaming_client_factory: Any,
    cursor: Any,
    flush_each_batch: bool = False,
    session_pool: StreamingSessionPool | None = None,
    receipt_cache: ImmutableReceiptCache | None = None,
    history_mode: str = "streaming",
    on_snapshot_applied: Callable[[], None] | None = None,
    cycle_metrics: StreamingCycleMetrics | None = None,
) -> tuple[int, int]:
    """Charge une table et publie un relevé borné seulement en mode Streaming.

    Le cycle n'est pas une mesure source -> miroir. Un cycle au repos ne
    produit aucun relevé ; une panne ne journalise pas son message sensible.
    """

    metrics = (cycle_metrics or StreamingCycleMetrics(table.table_id)) if history_mode == "streaming" else None
    if metrics is not None and metrics.table_tag != loader_table_tag(table.table_id):
        raise DestinationLoaderError("mesures liées à une autre table")
    try:
        result = _load_table_once(
            table, plan=plan, storage=storage, raw_prefix_root=raw_prefix_root,
            streaming_client_factory=streaming_client_factory, cursor=cursor,
            flush_each_batch=flush_each_batch, session_pool=session_pool, receipt_cache=receipt_cache,
            history_mode=history_mode, on_snapshot_applied=on_snapshot_applied, cycle_metrics=metrics,
        )
    except BaseException:
        if metrics is not None:
            LOG.info("loader_cycle %s", json.dumps(metrics.record(failed=True), separators=(",", ":")))
        raise
    if metrics is not None and metrics.has_work:
        LOG.info("loader_cycle %s", json.dumps(metrics.record(), separators=(",", ":")))
    return result


def _load_table_once(
    table: LoaderTable,
    *,
    plan: TableDestinationPlan,
    storage: StorageBackend,
    raw_prefix_root: str,
    streaming_client_factory: Any,
    cursor: Any,
    flush_each_batch: bool = False,
    session_pool: StreamingSessionPool | None = None,
    receipt_cache: ImmutableReceiptCache | None = None,
    history_mode: str = "streaming",
    on_snapshot_applied: Callable[[], None] | None = None,
    cycle_metrics: StreamingCycleMetrics | None = None,
) -> tuple[int, int]:
    """Charge tous les lots en attente pour une table — un passage, pas une boucle.

    Renvoie ``(lots_traités, événements_ajoutés)``. Idempotent : rappelable
    sans effet si rien de nouveau n'est publié. ``streaming_client_factory``
    construit un client Snowpipe Streaming lié à ``plan.history_table`` — le
    SDK réel lie un client à une table précise (``StreamingIngestClient.
    from_table``), donc un client par table d'historique, jamais partagé.

    Charge d'abord l'instantané de copie initiale (``load_snapshot_once``,
    quand la copie est prouvée), puis les lots de journal strictement après
    sa frontière. ``on_snapshot_applied`` signale uniquement une application
    nouvelle de cet instantané, y compris vide ; le tuple reste limité aux
    lots et événements de journal.
    """

    if history_mode not in {"streaming", "sql"}:
        raise DestinationLoaderError(f"mode historique invalide : {history_mode!r}")
    if history_mode == "sql" and session_pool is not None:
        raise DestinationLoaderError("le mode SQL ne peut pas ouvrir de canal Snowpipe")
    if receipt_cache is not None and receipt_cache.storage is not storage:
        raise DestinationLoaderError("cache de reçus lié à un autre stockage")
    with measure_loader_stage(cycle_metrics, "discovery_raw"):
        object_store_factory = storage.object_store if receipt_cache is None else receipt_cache.object_store
        object_store = object_store_factory(raw_prefix_for_table(raw_prefix_root, table.schema_name, table.table_name))
        root_store = object_store_factory(raw_prefix_root)
        checkpoint_store = storage.checkpoint_store(loader_checkpoint_stream_key(table.table_id, table.evidence_key))
        initial_checkpoint = checkpoint_store.load()
        if table.evidence_key and initial_checkpoint is None:
            # Pendant le nouveau Job de copie, l'ancien lecteur peut déjà avoir
            # publié des reçus. Ne jamais avancer le checkpoint du nouveau run
            # avant que sa preuve d'instantané soit durable.
            if EvidenceReader(storage.object_store("")).read(table.evidence_key) is None:
                return 0, 0
        predecessors: dict[JournalPosition, JournalPosition | None] = {}
        pending = discover_pending_receipts(
            object_store, checkpoint_store, fleet_store=root_store, predecessors=predecessors
        )
    if not pending and (initial_checkpoint is not None or not table.evidence_key):
        # L'ouverture d'un canal Snowpipe implique une session réseau et des
        # ressources côté destination. Un cycle sans reçu nouveau n'en a pas
        # besoin ; run_once mesure seulement après un chargement réel.
        return 0, 0
    streaming_client = None
    if history_mode == "sql":
        history_loader = HistorySqlLoader(plan=plan, cursor=cursor)
    elif session_pool is not None:
        history_loader = session_pool.for_table(table, plan, cycle_metrics=cycle_metrics)
    else:
        if streaming_client_factory is None:
            raise DestinationLoaderError("client Snowpipe absent en mode streaming")
        with measure_loader_stage(cycle_metrics, "open_channel"):
            streaming_client = streaming_client_factory(plan.history_table)
            try:
                history_loader = HistoryStreamingLoader(
                    plan=plan, client=streaming_client, stream_id=loader_stream_id(table),
                    flush_each_batch=flush_each_batch,
                )
            except BaseException:
                streaming_client.close()
                raise
    merge = MirrorMergePlan(plan=plan)

    try:
        snapshot_loaded = load_snapshot_once(
            table, storage=storage, raw_prefix_root=raw_prefix_root, checkpoint_store=checkpoint_store,
            history_loader=history_loader, merge=merge, cursor=cursor, cycle_metrics=cycle_metrics,
        )
        if snapshot_loaded and initial_checkpoint is None and on_snapshot_applied is not None:
            on_snapshot_applied()

        # Les reçus portent le lien vers leur prédécesseur, même quand la
        # fenêtre est vide. Ce lien prouve l'ordre lors d'une rotation.
        if initial_checkpoint is None:
            with measure_loader_stage(cycle_metrics, "discovery_raw"):
                predecessors.clear()
                pending = discover_pending_receipts(
                    object_store, checkpoint_store, fleet_store=root_store, predecessors=predecessors
                )

        snapshot_boundary: JournalPosition | None = None
        if table.evidence_key:
            with measure_loader_stage(cycle_metrics, "discovery_raw"):
                evidence = EvidenceReader(storage.object_store("")).read(table.evidence_key)
                if evidence is None:
                    raise DestinationLoaderError("preuve de copie initiale disparue pendant le chargement")
                snapshot_boundary = JournalPosition(
                    receiver=evidence.boundary.receiver_name, sequence=evidence.boundary.last_sequence
                )

        committed_index = committed_receipt_index(
            pending, history_loader.resume_position(), checkpoint_store.load(), predecessors
        )

        batches_processed = 0
        events_appended = 0
        for index, receipt in enumerate(pending):
            batch: RawBatch | None = None
            with measure_loader_stage(cycle_metrics, "discovery_raw"):
                if receipt.raw is not None:
                    if receipt.fleet:
                        from quadringent.fleet_capture import split_window_batch

                        raw = receipt.raw
                        manifest_content = receipt.store.get(raw["manifest_key"])
                        payload = receipt.store.get(raw["payload_key"])
                        combined = read_raw_batch(manifest_content, payload, payload_name=raw["payload_key"])
                        target = table.table_name.strip().upper()
                        fleet_tables = sorted({event.table.strip().upper() for event in combined.events} | {target})
                        windows = split_window_batch(
                            manifest_content, payload, tables=fleet_tables, expected_end=receipt.position
                        )
                        window = next((candidate for candidate in windows if candidate.table == target), None)
                        if window is not None:
                            batch = read_raw_batch(
                                window.manifest, window.payload, preserve_decimals=history_mode == "sql"
                            )
                    else:
                        batch = read_published_batch(
                            receipt.store, receipt.raw["payload_key"], receipt.raw["manifest_key"],
                            preserve_decimals=history_mode == "sql",
                        )
            if batch is not None and snapshot_boundary is not None:
                # Le reçu peut commencer avant la frontière de snapshot.
                # Son manifeste a déjà été contrôlé par read_raw_batch ; on
                # ne charge que la vue en mémoire postérieure à la copie.
                batch = RawBatch(
                    batch.manifest,
                    tuple(
                        event for event in batch.events
                        if event.position.receiver != snapshot_boundary.receiver
                        or event.position.sequence > snapshot_boundary.sequence
                    ),
                )
            if batch is not None and batch.events:
                if committed_index is None or index >= committed_index:
                    existing = (
                        existing_history_event_ids(plan, batch, cursor)
                        if history_loader.needs_history_lookup else ()
                    )
                    result = _load_history_batch(history_loader, batch, existing, cycle_metrics)
                    events_appended += result.events_appended
                # Une panne entre la validation du canal Snowpipe et le MERGE
                # laisse l'historique présent mais le miroir en retard. Le
                # replay du lot doit refaire le MERGE même si le canal saute
                # tous les événements déjà validés.
                with measure_loader_stage(cycle_metrics, "merge"):
                    merge.execute(cursor, event_ids=tuple(event.event_id for event in batch.events))
                batches_processed += 1
            with measure_loader_stage(cycle_metrics, "checkpoint"):
                advance_loader_checkpoint(checkpoint_store, receipt.position)
    finally:
        if streaming_client is not None:
            try:
                history_loader.close()
            finally:
                streaming_client.close()
    return batches_processed, events_appended


def ensure_tables_exist(plan: TableDestinationPlan, cursor: Any) -> None:
    cursor.execute(plan.history_ddl())
    cursor.execute(plan.mirror_ddl())


def measure_and_log_lag(plan: TableDestinationPlan, cursor: Any) -> StreamingLagMetrics:
    metrics = LagQueryPlan(plan=plan).read(cursor)
    LOG.info(
        "retard mesuré table=%s historique=%ss miroir=%ss",
        plan.history_table,
        metrics.history_lag_seconds,
        metrics.mirror_lag_seconds,
    )
    return metrics


def _snowflake_role_to_warehouse(role: str) -> str:
    """Nom du warehouse dédié — même convention que ``services/destinations.
    py::_build_setup_script`` (``QDT_WH_<suffixe du rôle>``), jamais une
    valeur devinée séparément."""

    prefix = "QDT_ROLE_"
    if not role.startswith(prefix):
        raise DestinationLoaderError(f"SNOWFLAKE_ROLE inattendu (hors convention QDT_ROLE_*) : {role!r}")
    return f"QDT_WH_{role[len(prefix):]}"


def _connector_kwargs(*, account: str, user: str, role: str, private_key_der: bytes, warehouse: str) -> dict[str, Any]:
    """Construit les arguments de ``snowflake.connector.connect`` — pure, sans I/O.

    Séparé de :func:`_connect_snowflake` pour que le contrat (``host`` non
    vide et cohérent avec ``account``) soit vérifiable sans ouvrir de
    connexion réseau — voir ``tests/test_v2_executor_manifest_script_contracts.py``.
    """

    return {
        "account": account,
        "user": user,
        "role": role,
        "private_key": private_key_der,
        "warehouse": warehouse,
        # ``host`` explicite plutôt que laissé au driver : même hôte standard
        # que le profil Snowpipe Streaming (``account_url_host``), pour ne
        # jamais faire diverger les deux clients sur la résolution d'URL de
        # compte — voir la panne constatée le 24 septembre 2026.
        "host": account_url_host(account),
        "session_parameters": {"QUERY_TAG": "quadringent-destination-loader"},
    }


def _connect_snowflake(*, account: str, user: str, role: str, private_key_pem: str, warehouse: str,
                       timeout_seconds: int | None = None) -> Any:
    if timeout_seconds is not None and (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10):
        raise ValueError("Snowflake probe timeout must be within 1..10 seconds")
    import snowflake.connector  # import différé : jamais requis hors production/qualification
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    der = key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    kwargs = _connector_kwargs(account=account, user=user, role=role, private_key_der=der, warehouse=warehouse)
    if timeout_seconds is not None:
        # Options natives du connecteur ; ses retries empêchent une promesse
        # de deadline murale stricte. La qualification contrôle aussi le temps observé.
        kwargs.update(login_timeout=timeout_seconds, network_timeout=timeout_seconds, socket_timeout=timeout_seconds)
    return snowflake.connector.connect(**kwargs)


def _streaming_profile(*, account: str, user: str, role: str, private_key_pem: str, warehouse: str,
                        database: str, schema: str) -> dict[str, str]:
    """Profil du SDK Snowpipe Streaming, construit depuis le même Secret que
    la connexion MERGE — voir docs/product/snowflake-destination.md.

    Constat du 24 septembre 2026 (premier pipeline réel sur GKE) : sans
    ``host``, le SDK échoue au démarrage (``StreamingIngestError: ConfigError:
    ... Missing host for account URL construction``) — ``account`` seul ne
    suffit pas à ce client (contrairement à ``snowflake-connector-python``,
    qui sait dériver l'hôte lui-même sans qu'on le lui transmette). ``host``
    et ``url`` sont posés tous les deux : les versions du SDK observées ne
    s'accordent pas toujours sur la clé qu'elles lisent en premier.
    """

    host = account_url_host(account)
    return {
        "account": account,
        "authorization_type": "JWT",
        "database": database,
        "host": host,
        "url": f"https://{host}",
        "private_key": private_key_pem,
        "role": role,
        "schema": schema,
        "user": user,
        "warehouse": warehouse,
    }


def _env(name: str) -> str:
    """Variable d'environnement obligatoire — jamais un défaut deviné."""

    value = os.environ.get(name, "")
    if not value:
        raise DestinationLoaderError(f"{name} est obligatoire")
    return value


def run_once(*, storage: StorageBackend, tables: tuple[LoaderTable, ...], raw_prefix_root: str,
             database: str, schema: str, streaming_client_factory: Any, cursor: Any,
             ensure_schema: bool = True, measure_lag: bool = False,
             flush_each_batch: bool = False,
             session_pool: StreamingSessionPool | None = None,
             receipt_cache: ImmutableReceiptCache | None = None,
             history_mode: str = "streaming", mirror_schema: str | None = None) -> None:
    for table in tables:
        plan = build_plan(table, database=database, schema=schema, mirror_schema=mirror_schema)
        if ensure_schema:
            ensure_tables_exist(plan, cursor)
        snapshot_applied = False

        def mark_snapshot_applied() -> None:
            nonlocal snapshot_applied
            snapshot_applied = True

        processed, appended = load_table_once(
            table, plan=plan, storage=storage, raw_prefix_root=raw_prefix_root,
            streaming_client_factory=streaming_client_factory, cursor=cursor,
            flush_each_batch=flush_each_batch,
            session_pool=session_pool,
            receipt_cache=receipt_cache,
            history_mode=history_mode,
            on_snapshot_applied=mark_snapshot_applied,
        )
        if processed:
            LOG.info("table=%s lots=%d événements=%d", table.table_name, processed, appended)
        if processed or snapshot_applied or measure_lag:
            lag = measure_and_log_lag(plan, cursor)
            # Ce relevé suit le MERGE synchrone du lot. Quand la table a
            # avancé, l'âge de sa dernière mutation dans le miroir mesure
            # le délai de livraison IBM i -> miroir de ce lot. Au repos le
            # même âge augmente : ne jamais le publier comme une nouvelle
            # livraison sans lot traité.
            if processed and lag.mirror_lag_seconds is not None and lag.mirror_lag_seconds >= 0:
                LOG.info(
                    "livraison table=%s événements_nouveaux=%d miroir_secondes=%s",
                    table.table_name, appended, lag.mirror_lag_seconds,
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=float(os.environ.get("QUADRINGENT_LOADER_POLL_SECONDS", "10")))
    parser.add_argument("--once", action="store_true", help="un seul passage, puis quitte (qualification/débogage)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    flush_each_batch = os.environ.get("QUADRINGENT_STREAMING_FLUSH_EACH_BATCH", "false").strip().lower() == "true"
    history_mode = os.environ.get("QUADRINGENT_HISTORY_MODE", "streaming").strip().lower()
    if history_mode not in {"streaming", "sql"}:
        raise DestinationLoaderError("QUADRINGENT_HISTORY_MODE doit être streaming ou sql")

    tables = parse_table_set(_env("QUADRINGENT_LOADER_TABLE_SET_JSON"))
    raw_prefix_root = _env("AS400_RAW_PREFIX")
    database = _env("QUADRINGENT_DESTINATION_DATABASE")
    schema = _env("QUADRINGENT_DESTINATION_SCHEMA")
    # L'absence conserve le contrat des anciens manifestes à schéma unique.
    # Une valeur présente mais vide reste une erreur de configuration.
    mirror_schema = _env("QUADRINGENT_MIRROR_SCHEMA") if "QUADRINGENT_MIRROR_SCHEMA" in os.environ else schema
    for table in tables:
        build_plan(table, database=database, schema=schema, mirror_schema=mirror_schema)
    account = _env("SNOWFLAKE_ACCOUNT")
    user = _env("SNOWFLAKE_USER")
    role = _env("SNOWFLAKE_ROLE")
    private_key_pem = _env("SNOWFLAKE_PRIVATE_KEY_PEM")
    warehouse = _snowflake_role_to_warehouse(role)

    storage = StorageBackend.from_environment(os.environ)
    bind_loader_scopes(storage=storage, tables=tables, raw_prefix_root=raw_prefix_root,
                       account=account, database=database, schema=schema, mirror_schema=mirror_schema)
    receipt_cache = ImmutableReceiptCache(storage)
    connection = _connect_snowflake(
        account=account, user=user, role=role, private_key_pem=private_key_pem, warehouse=warehouse
    )
    if history_mode == "streaming":
        profile = _streaming_profile(
            account=account, user=user, role=role, private_key_pem=private_key_pem,
            warehouse=warehouse, database=database, schema=schema,
        )
        profile_json_path = _write_ephemeral_profile(profile)

        def streaming_client_factory(history_table: str) -> SnowpipeStreamingClientAdapter:
            return SnowpipeStreamingClientAdapter(
                client_name=f"quadringent-destination-loader-{history_table.lower()}",
                database=database, schema=schema, table=history_table,
                profile_json=profile_json_path,
            )

        session_pool = StreamingSessionPool(streaming_client_factory, flush_each_batch=flush_each_batch)
    else:
        streaming_client_factory = None
        session_pool = None

    cursor = connection.cursor()

    running = True

    def _stop(signum: int, frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        run_once(
            storage=storage, tables=tables, raw_prefix_root=raw_prefix_root,
            database=database, schema=schema, mirror_schema=mirror_schema,
            streaming_client_factory=streaming_client_factory, cursor=cursor,
            measure_lag=False,
            flush_each_batch=flush_each_batch,
            session_pool=session_pool,
            receipt_cache=receipt_cache,
            history_mode=history_mode,
        )
        while running and not args.once:
            time.sleep(args.poll_seconds)
            if not running:
                break
            run_once(
                storage=storage, tables=tables, raw_prefix_root=raw_prefix_root,
                database=database, schema=schema, mirror_schema=mirror_schema,
                streaming_client_factory=streaming_client_factory, cursor=cursor,
                ensure_schema=False, measure_lag=False,
                flush_each_batch=flush_each_batch,
                session_pool=session_pool,
                receipt_cache=receipt_cache,
                history_mode=history_mode,
            )
    finally:
        with ExitStack() as closing:
            closing.callback(connection.close)
            closing.callback(cursor.close)
            if session_pool is not None:
                closing.callback(session_pool.close)
    return 0


def _write_ephemeral_profile(profile: dict[str, str]) -> str:
    import stat
    import tempfile

    fd, path = tempfile.mkstemp(prefix="qdt-streaming-profile-", suffix=".json")
    try:
        os.write(fd, json.dumps(profile).encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


if __name__ == "__main__":
    sys.exit(main())
