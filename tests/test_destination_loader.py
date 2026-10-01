"""Chargeur de destination (scripts/quadringent_destination_loader.py).

Couvre les fonctions pures (parse_table_set, build_plan, raw_prefix_for_
table, _snowflake_role_to_warehouse) et la découverte des lots bruts
(discover_new_batches) contre un FileObjectStore/JsonCheckpointStore réels
— jamais de connexion Snowflake ni de stockage objet réel dans ces tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pytest

import quadringent_destination_loader as loader
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator, publish_raw_batch
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.snowflake_destination import UnsupportedColumnTypeError
from quadringent.snowflake_streaming_loader import (
    FakeStreamingClient,
    HistoryStreamingLoader,
    StreamingLagMetrics,
    encode_offset_token,
)
from quadringent.storage_layout import snapshot_prefix
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef


_COLUMNS = [
    {"name": "ORDER_ID", "kind": "integer", "nullable": False},
    {"name": "LABEL", "kind": "varchar", "length": 60},
]

_TABLE_SET_JSON = json.dumps(
    [
        {
            "table_id": "tbl1",
            "schema": "SALES",
            "table": "ORDHDR",
            "key_columns": ["ORDER_ID"],
            "columns": _COLUMNS,
        }
    ]
)


class _ReceiptCacheStorage:
    def __init__(self, root):
        self.root = root
        self.reads = []
        self.listings = []

    def object_store(self, prefix):
        storage = self

        class Store(FileObjectStore):
            def get_bounded(self, key, max_bytes):
                storage.reads.append((prefix, key))
                return super().get_bounded(key, max_bytes)

            def list_receipt_keys(self, max_keys):
                storage.listings.append((prefix, max_keys))
                return super().list_receipt_keys(max_keys)

        return Store(self.root / "objects" / prefix)

    def checkpoint_store(self, key):
        return JsonCheckpointStore(self.root / "checkpoints" / f"{key}.json")


def _put_cached_receipt(storage, prefix, key, previous, end):
    def position(value):
        return None if value is None else {"receiver": value.receiver, "sequence": value.sequence}

    start = (JournalPosition(end.receiver, previous.sequence + 1)
             if previous is not None and previous.receiver == end.receiver else end)
    content = json.dumps({
        "format_version": "quadringent-scan-receipt-v2", "previous": position(previous),
        "start": position(start), "end": position(end), "raw": None,
    }).encode()
    storage.object_store(prefix).put_once(key, content)
    return content


def test_receipt_cache_is_shared_between_tables_and_idle_cycles(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    (first,) = loader.parse_table_set(_TABLE_SET_JSON)
    second = replace(first, table_id="tbl2", table_name="DETAIL")
    end = JournalPosition("R1", 1)
    previous = None
    for sequence in range(1, 49):
        end = JournalPosition("R1", sequence)
        _put_cached_receipt(storage, "raw", f"receipts/shared-{sequence}.json", previous, end)
        previous = end
    for table in (first, second):
        storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id)).commit(end)
    cache = loader.ImmutableReceiptCache(storage)
    cursor = Mock()
    for _ in range(2):
        loader.run_once(
            storage=storage, tables=(first, second), raw_prefix_root="raw", database="D", schema="S",
            streaming_client_factory=None, cursor=cursor, ensure_schema=False, history_mode="sql",
            receipt_cache=cache,
        )
        assert len(storage.reads) == 48
        assert len(set(storage.reads)) == 48
    assert storage.listings.count(("raw", loader._RECEIPT_LIST_BUDGET)) == 4
    cursor.execute.assert_not_called()


def test_receipt_cache_fresh_listing_rotation_and_checkpoint_resume(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    first, rotated = JournalPosition("Z9", 2), JournalPosition("A1", 1)
    _put_cached_receipt(storage, "raw", "receipts/first.json", None, first)
    checkpoint = storage.checkpoint_store("loader")
    cache = loader.ImmutableReceiptCache(storage)
    store = cache.object_store("raw")
    assert [item.position for item in loader.discover_pending_receipts(store, checkpoint)] == [first]
    checkpoint.commit(first)
    _put_cached_receipt(storage, "raw", "receipts/rotated.json", first, rotated)
    assert [item.position for item in loader.discover_pending_receipts(store, checkpoint)] == [rotated]
    loader.advance_loader_checkpoint(checkpoint, rotated)
    assert loader.discover_pending_receipts(store, checkpoint) == []
    assert len(storage.reads) == 2
    # Une reprise de processus repart de zéro et relit les reçus, avec le checkpoint durable courant.
    restarted = loader.ImmutableReceiptCache(storage)
    assert loader.discover_pending_receipts(restarted.object_store("raw"), checkpoint) == []
    assert len(storage.reads) == 4


def test_receipt_cache_revalidates_predecessors_for_cached_chain(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    end = JournalPosition("R1", 1)
    _put_cached_receipt(storage, "raw", "receipts/one.json", None, end)
    checkpoint = storage.checkpoint_store("loader")
    store = loader.ImmutableReceiptCache(storage).object_store("raw")
    loader.discover_pending_receipts(store, checkpoint)
    with pytest.raises(loader.DestinationLoaderError, match="plusieurs prédécesseurs"):
        loader.discover_pending_receipts(store, checkpoint, predecessors={end: JournalPosition("R0", 9)})
    assert len(storage.reads) == 1


def test_receipt_cache_keeps_chain_conflicts_and_listing_budget_fail_closed(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    first = JournalPosition("R1", 1)
    _put_cached_receipt(storage, "raw", "receipts/first.json", None, first)
    checkpoint = storage.checkpoint_store("loader")
    store = loader.ImmutableReceiptCache(storage).object_store("raw")
    loader.discover_pending_receipts(store, checkpoint)
    _put_cached_receipt(storage, "raw", "receipts/fork.json", None, JournalPosition("R1", 2))
    with pytest.raises(loader.DestinationLoaderError, match="concurrents"):
        loader.discover_pending_receipts(store, checkpoint)
    with pytest.raises(ValueError, match="listing exceeds budget"):
        loader.discover_pending_receipts(store, checkpoint, max_receipts=1)


def test_receipt_cache_isolates_storage_and_prefixes_and_never_caches_other_objects(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path / "one")
    other = _ReceiptCacheStorage(tmp_path / "two")
    key = "receipts/same.json"
    for backend, prefix, sequence in ((storage, "a", 1), (storage, "b", 2), (other, "a", 3)):
        _put_cached_receipt(backend, prefix, key, None, JournalPosition("R1", sequence))
    cache = loader.ImmutableReceiptCache(storage)
    other_cache = loader.ImmutableReceiptCache(other)
    assert json.loads(cache.object_store("a").get_bounded(key, 65_536))["end"]["sequence"] == 1
    assert json.loads(cache.object_store("b").get_bounded(key, 65_536))["end"]["sequence"] == 2
    assert json.loads(other_cache.object_store("a").get_bounded(key, 65_536))["end"]["sequence"] == 3
    with pytest.raises(loader.DestinationLoaderError, match="stockage"):
        loader.load_table_once(
            loader.parse_table_set(_TABLE_SET_JSON)[0], plan=None, storage=other, raw_prefix_root="a",
            streaming_client_factory=None, cursor=None, receipt_cache=cache,
        )
    store = cache.object_store("a")
    for key in ("proof.json", "checkpoint.json", "payload.jsonl", "batch.manifest.json"):
        path = storage.object_store("a").root / key
        path.write_bytes(b"first")
        assert store.get_bounded(key, 100) == b"first"
        path.write_bytes(b"second")
        assert store.get_bounded(key, 100) == b"second"
        assert store.get(key) == b"second"


@pytest.mark.parametrize("limit", ("entries", "bytes"))
def test_receipt_cache_eviction_and_cached_read_budget(tmp_path, limit):
    storage = _ReceiptCacheStorage(tmp_path)
    first = _put_cached_receipt(storage, "raw", "receipts/one.json", None, JournalPosition("R1", 1))
    second = _put_cached_receipt(storage, "raw", "receipts/two.json", None, JournalPosition("R1", 2))
    cache = loader.ImmutableReceiptCache(
        storage, max_entries=1 if limit == "entries" else 10,
        max_bytes=max(len(first), len(second)) if limit == "bytes" else 65_536,
    )
    store = cache.object_store("raw")
    assert store.get_bounded("receipts/one.json", 65_536) == first
    assert store.get_bounded("receipts/two.json", 65_536) == second
    assert store.get_bounded("receipts/two.json", 65_536) == second
    assert store.get_bounded("receipts/one.json", 65_536) == first
    assert len(storage.reads) == 3
    with pytest.raises(ValueError, match="read budget"):
        store.get_bounded("receipts/one.json", len(first) - 1)
    assert len(storage.reads) == 3


def test_receipt_cache_does_not_retain_oversized_objects_or_failed_reads(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    content = _put_cached_receipt(storage, "raw", "receipts/one.json", None, JournalPosition("R1", 1))
    store = loader.ImmutableReceiptCache(storage, max_bytes=1).object_store("raw")
    for _ in range(2):
        assert store.get_bounded("receipts/one.json", 65_536) == content
    assert len(storage.reads) == 2
    store = loader.ImmutableReceiptCache(storage).object_store("raw")
    with pytest.raises(FileNotFoundError):
        store.get_bounded("receipts/new.json", 65_536)
    _put_cached_receipt(storage, "raw", "receipts/new.json", None, JournalPosition("R1", 2))
    assert store.get_bounded("receipts/new.json", 65_536)


def test_receipt_cache_lru_touch_preserves_recent_receipt(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    for sequence in (1, 2, 3):
        _put_cached_receipt(storage, "raw", f"receipts/{sequence}.json", None, JournalPosition("R1", sequence))
    store = loader.ImmutableReceiptCache(storage, max_entries=2).object_store("raw")
    for sequence in (1, 2, 1, 3, 1):
        store.get_bounded(f"receipts/{sequence}.json", 65_536)
    assert len(storage.reads) == 3
    store.get_bounded("receipts/2.json", 65_536)
    assert len(storage.reads) == 4


def test_run_once_emits_measured_mirror_delivery_after_merge(caplog) -> None:
    (table,) = loader.parse_table_set(_TABLE_SET_JSON)
    with (
        patch.object(loader, "load_table_once", return_value=(1, 2)),
        patch.object(loader, "measure_and_log_lag", return_value=StreamingLagMetrics(10.0, 11.2)),
    ):
        with caplog.at_level("INFO", logger="quadringent.destination_loader"):
            loader.run_once(
                storage=None, tables=(table,), raw_prefix_root="raw", database="D", schema="S",
                streaming_client_factory=None, cursor=None, ensure_schema=False,
            )
    assert "livraison table=ORDHDR événements_nouveaux=2 miroir_secondes=11.2" in caplog.text


def test_run_once_does_not_claim_delivery_without_new_batch(caplog) -> None:
    (table,) = loader.parse_table_set(_TABLE_SET_JSON)
    with (
        patch.object(loader, "load_table_once", return_value=(0, 0)),
        patch.object(loader, "measure_and_log_lag", return_value=StreamingLagMetrics(10.0, 11.2)),
    ):
        with caplog.at_level("INFO", logger="quadringent.destination_loader"):
            loader.run_once(
                storage=None, tables=(table,), raw_prefix_root="raw", database="D", schema="S",
                streaming_client_factory=None, cursor=None, ensure_schema=False,
            )
    assert "livraison table=" not in caplog.text


def test_run_once_does_not_query_snowflake_without_new_batch() -> None:
    (table,) = loader.parse_table_set(_TABLE_SET_JSON)
    with (
        patch.object(loader, "load_table_once", return_value=(0, 0)),
        patch.object(loader, "measure_and_log_lag") as measure,
    ):
        loader.run_once(
            storage=None, tables=(table,), raw_prefix_root="raw", database="D", schema="S",
            streaming_client_factory=None, cursor=None, ensure_schema=False,
        )
    measure.assert_not_called()


@pytest.mark.parametrize("snapshot_has_rows", (True, False))
def test_run_once_measures_applied_snapshot_once_then_keeps_idle_without_sql(tmp_path, caplog, snapshot_has_rows) -> None:
    class Storage:
        def object_store(self, prefix: str) -> FileObjectStore:
            return FileObjectStore(tmp_path / "objects" / prefix)

        def checkpoint_store(self, key: str) -> JsonCheckpointStore:
            return JsonCheckpointStore(tmp_path / "checkpoints" / f"{key}.json")

    storage = Storage()
    (base_table,) = loader.parse_table_set(_TABLE_SET_JSON)
    evidence_key = "raw/tbl1/evidence/run-snapshot.json"
    table = replace(base_table, evidence_key=evidence_key)
    boundary = JournalBoundary(
        receiver_library="SALES", receiver_name="RCV0001", last_sequence=20,
        observed_at=datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc),
    )
    snapshot_batches = ()
    if snapshot_has_rows:
        event = _event("c", seq=1, receiver="SNAPSHOT-run", after={"ORDER_ID": 7, "LABEL": "copie"})
        published = publish_raw_batch(
            storage.object_store(snapshot_prefix("raw", table.table_name)), [event], high_watermark=event.position,
        )
        snapshot_batches = (SnapshotBatchRef(published.payload_key, published.manifest_key),)
    evidence = InitialCopyEvidence(
        pipeline_id="pipe1", table_id=table.table_id, run_id="run-snapshot", boundary=boundary,
        rows_copied=int(snapshot_has_rows), completed_at=datetime(2026, 9, 28, 8, 1, tzinfo=timezone.utc),
        snapshot_batches=snapshot_batches,
    )
    storage.object_store("").put_once(evidence_key, json.dumps(evidence.to_dict()).encode())
    cursor = Mock(rowcount=int(snapshot_has_rows))
    cursor.fetchone.side_effect = [(10.0,), (11.2,)] if snapshot_has_rows else [(None,), (None,)]
    arguments = dict(
        storage=storage, tables=(table,), raw_prefix_root="raw", database="D", schema="S",
        streaming_client_factory=None, cursor=cursor, ensure_schema=False, history_mode="sql",
    )

    with caplog.at_level("INFO", logger="quadringent.destination_loader"):
        loader.run_once(**arguments)

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any(sql.startswith("DELETE FROM") for sql in statements)
    assert sum(sql.startswith("SELECT DATEDIFF") for sql in statements) == 2
    assert "retard mesuré table=ORDHDR_HISTORY" in caplog.text
    # Un snapshot ne porte pas une mesure de livraison d'une mutation de journal.
    assert "livraison table=" not in caplog.text
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, evidence_key)).load() == (
        JournalPosition("RCV0001", 20)
    )

    cursor.reset_mock()
    caplog.clear()
    with caplog.at_level("INFO", logger="quadringent.destination_loader"):
        loader.run_once(**arguments)
    cursor.execute.assert_not_called()
    assert "retard mesuré" not in caplog.text
    assert "livraison table=" not in caplog.text


def test_main_does_not_periodically_query_idle_snowflake(monkeypatch) -> None:
    connection = Mock()
    handlers = {}
    sleeps = 0
    ticks = iter((0.0, 31.0, 31.0))

    def sleep_once_then_stop(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            handlers[loader.signal.SIGTERM](loader.signal.SIGTERM, None)

    monkeypatch.setenv("QUADRINGENT_HISTORY_MODE", "sql")
    monkeypatch.setattr(loader, "_env", lambda _name: "test-value")
    monkeypatch.setattr(loader, "parse_table_set", lambda _value: ())
    monkeypatch.setattr(loader.StorageBackend, "from_environment", lambda _env: None)
    monkeypatch.setattr(loader, "_snowflake_role_to_warehouse", lambda _role: "TEST_WH")
    monkeypatch.setattr(loader, "_connect_snowflake", lambda **_kwargs: connection)
    monkeypatch.setattr(loader.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(loader.time, "sleep", sleep_once_then_stop)
    monkeypatch.setattr(loader.time, "monotonic", lambda: next(ticks))
    with patch.object(loader, "run_once") as run:
        assert loader.main([]) == 0
    assert run.call_count == 2
    assert all(call.kwargs.get("measure_lag", True) is False for call in run.call_args_list)
    assert run.call_args_list[0].kwargs["receipt_cache"] is run.call_args_list[1].kwargs["receipt_cache"]


@pytest.mark.parametrize("history_schema,mirror_schema", [("SITE_A", "SITE_A"), ("RAW", "CURATED")])
def test_main_uses_history_scope_for_streaming_and_propagates_mirror_scope(monkeypatch, history_schema, mirror_schema):
    for name, value in {
        "QUADRINGENT_HISTORY_MODE": "streaming", "QUADRINGENT_LOADER_TABLE_SET_JSON": _TABLE_SET_JSON,
        "AS400_RAW_PREFIX": "raw/test", "QUADRINGENT_DESTINATION_DATABASE": "CLIENT_DB",
        "QUADRINGENT_DESTINATION_SCHEMA": history_schema, "QUADRINGENT_MIRROR_SCHEMA": mirror_schema,
        "SNOWFLAKE_ACCOUNT": "test-account", "SNOWFLAKE_USER": "TEST_SVC", "SNOWFLAKE_ROLE": "QDT_ROLE_TEST",
        "SNOWFLAKE_PRIVATE_KEY_PEM": "CLE_SYNTHETIQUE",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(loader.StorageBackend, "from_environment", lambda _env: None)
    bound = []
    monkeypatch.setattr(loader, "bind_loader_scopes", lambda **kwargs: bound.append(kwargs))
    connection = Mock()
    def connect(**_kwargs):
        assert bound and bound[0]["schema"] == history_schema and bound[0]["mirror_schema"] == mirror_schema
        return connection
    monkeypatch.setattr(loader, "_connect_snowflake", connect)
    monkeypatch.setattr(loader.signal, "signal", lambda *_args: None)
    profiles, adapters = [], []
    monkeypatch.setattr(loader, "_write_ephemeral_profile", lambda profile: profiles.append(profile) or "/tmp/test-profile")
    monkeypatch.setattr(loader, "SnowpipeStreamingClientAdapter", lambda **kwargs: adapters.append(kwargs) or Mock())
    def run(**kwargs):
        assert kwargs["schema"] == history_schema
        assert kwargs["mirror_schema"] == mirror_schema
        kwargs["streaming_client_factory"]("ORDHDR_HISTORY")
    monkeypatch.setattr(loader, "run_once", run)
    assert loader.main(["--once"]) == 0
    assert profiles[0]["database"] == "CLIENT_DB" and profiles[0]["schema"] == history_schema
    assert adapters[0]["database"] == "CLIENT_DB" and adapters[0]["schema"] == history_schema


def test_durable_scope_binding_survives_checkpoint_and_process_restart(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    tables = loader.parse_table_set(_TABLE_SET_JSON)
    kwargs = dict(storage=storage, tables=tables, raw_prefix_root="raw", account="test-account", database="CLIENT_DB", schema="RAW", mirror_schema="CURATED")
    loader.bind_loader_scopes(**kwargs)
    checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key("tbl1"))
    checkpoint.commit(JournalPosition("R1", 10))
    loader.bind_loader_scopes(**kwargs)
    # Plus aucun Deployment requis : un redémarrage/pause/reprise garde la liaison.
    for field, value in (("account", "other-account"), ("database", "OTHER_DB"), ("schema", "OTHER_SCHEMA"), ("mirror_schema", "OTHER_MIRROR")):
        with pytest.raises(loader.DestinationLoaderError, match="périmètre.*checkpoint"):
            loader.bind_loader_scopes(**(kwargs | {field: value}))
    assert checkpoint.load() == JournalPosition("R1", 10)


def test_legacy_checkpoint_without_binding_requires_a_new_initial_copy(tmp_path):
    storage = _ReceiptCacheStorage(tmp_path)
    tables = loader.parse_table_set(_TABLE_SET_JSON)
    storage.checkpoint_store(loader.loader_checkpoint_stream_key("tbl1")).commit(JournalPosition("R1", 10))
    kwargs = dict(storage=storage, tables=tables, raw_prefix_root="raw", account="test-account", database="CLIENT_DB", schema="RAW", mirror_schema="CURATED")
    with pytest.raises(loader.DestinationLoaderError, match="copie initiale"):
        loader.bind_loader_scopes(**kwargs)
    new_tables = (replace(tables[0], evidence_key="raw/tbl1/evidence/new-run.json"),)
    loader.bind_loader_scopes(**(kwargs | {"tables": new_tables}))
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key("tbl1")).load() == JournalPosition("R1", 10)


@pytest.mark.parametrize("content", [b"invalid-json", b"{}", b"[]"])
def test_scope_binding_with_unknown_provenance_never_gets_overwritten(tmp_path, content):
    storage = _ReceiptCacheStorage(tmp_path)
    tables = loader.parse_table_set(_TABLE_SET_JSON)
    key = loader.loader_scope_binding_key(tables[0])
    store = storage.object_store("raw")
    store.put_once(key, content)
    with pytest.raises(loader.DestinationLoaderError, match="checkpoint"):
        loader.bind_loader_scopes(storage=storage, tables=tables, raw_prefix_root="raw", account="test-account",
                                 database="CLIENT_DB", schema="RAW", mirror_schema="CURATED")
    assert store.get(key) == content


def test_main_refuses_unbound_legacy_checkpoint_before_snowflake_connection(tmp_path, monkeypatch):
    storage = _ReceiptCacheStorage(tmp_path)
    storage.checkpoint_store(loader.loader_checkpoint_stream_key("tbl1")).commit(JournalPosition("R1", 10))
    for name, value in {
        "QUADRINGENT_HISTORY_MODE": "sql", "QUADRINGENT_LOADER_TABLE_SET_JSON": _TABLE_SET_JSON,
        "AS400_RAW_PREFIX": "raw", "QUADRINGENT_DESTINATION_DATABASE": "CLIENT_DB",
        "QUADRINGENT_DESTINATION_SCHEMA": "RAW", "QUADRINGENT_MIRROR_SCHEMA": "CURATED",
        "SNOWFLAKE_ACCOUNT": "test-account", "SNOWFLAKE_USER": "TEST_SVC", "SNOWFLAKE_ROLE": "QDT_ROLE_TEST",
        "SNOWFLAKE_PRIVATE_KEY_PEM": "CLE_SYNTHETIQUE",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(loader.StorageBackend, "from_environment", lambda _env: storage)
    with patch.object(loader, "_connect_snowflake") as connect:
        with pytest.raises(loader.DestinationLoaderError, match="copie initiale"):
            loader.main(["--once"])
        connect.assert_not_called()


@pytest.mark.parametrize("field,value", [("QUADRINGENT_DESTINATION_DATABASE", "BAD;DROP X"), ("QUADRINGENT_DESTINATION_SCHEMA", ""), ("QUADRINGENT_MIRROR_SCHEMA", "A.B")])
def test_invalid_scope_is_refused_before_remote_connection(monkeypatch, field, value):
    monkeypatch.setenv("QUADRINGENT_LOADER_TABLE_SET_JSON", _TABLE_SET_JSON)
    monkeypatch.setenv("AS400_RAW_PREFIX", "raw/test")
    monkeypatch.setenv("QUADRINGENT_DESTINATION_DATABASE", "CLIENT_DB")
    monkeypatch.setenv("QUADRINGENT_DESTINATION_SCHEMA", "RAW")
    monkeypatch.setenv("QUADRINGENT_MIRROR_SCHEMA", "CURATED")
    monkeypatch.setenv(field, value)
    with patch.object(loader, "_connect_snowflake") as connect:
        with pytest.raises(ValueError if value else loader.DestinationLoaderError):
            loader.main(["--once"])
        connect.assert_not_called()


class ParseTableSetTests(unittest.TestCase):
    def test_parses_a_valid_table_set(self) -> None:
        (table,) = loader.parse_table_set(_TABLE_SET_JSON)
        self.assertEqual(table.table_id, "tbl1")
        self.assertEqual(table.schema_name, "SALES")
        self.assertEqual(table.table_name, "ORDHDR")
        self.assertEqual(table.key_columns, ("ORDER_ID",))
        self.assertEqual(len(table.columns), 2)

    def test_invalid_json_is_rejected(self) -> None:
        with self.assertRaises(loader.DestinationLoaderError):
            loader.parse_table_set("{not json")

    def test_empty_list_is_rejected(self) -> None:
        with self.assertRaises(loader.DestinationLoaderError):
            loader.parse_table_set("[]")

    def test_table_without_columns_is_rejected(self) -> None:
        payload = json.dumps([{"table_id": "t", "schema": "S", "table": "T", "columns": []}])
        with self.assertRaises(loader.DestinationLoaderError):
            loader.parse_table_set(payload)


class BuildPlanTests(unittest.TestCase):
    def test_builds_a_valid_plan(self) -> None:
        (table,) = loader.parse_table_set(_TABLE_SET_JSON)
        plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")
        self.assertEqual(plan.history_table, "ORDHDR_HISTORY")
        self.assertEqual(plan.mirror_table, "ORDHDR_MIRROR")
        self.assertEqual(plan.key_columns, ("ORDER_ID",))

    def test_unsupported_column_type_is_rejected(self) -> None:
        payload = json.dumps(
            [
                {
                    "table_id": "t",
                    "schema": "S",
                    "table": "T",
                    "key_columns": ["X"],
                    "columns": [{"name": "X", "kind": "rowid"}],
                }
            ]
        )
        (table,) = loader.parse_table_set(payload)
        with self.assertRaises(UnsupportedColumnTypeError):
            loader.build_plan(table, database="ACME_RAW", schema="CURATED")

    def test_missing_key_columns_is_rejected(self) -> None:
        payload = json.dumps(
            [{"table_id": "t", "schema": "S", "table": "T", "key_columns": [], "columns": _COLUMNS}]
        )
        (table,) = loader.parse_table_set(payload)
        with self.assertRaises(loader.DestinationLoaderError):
            loader.build_plan(table, database="ACME_RAW", schema="CURATED")


class StreamingSessionPoolTests(unittest.TestCase):
    def test_recreated_channel_checks_history_before_replaying_a_receipt(self) -> None:
        """Un canal sans jeton ne peut pas conclure que l'historique est vide."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Storage:
                def object_store(self, prefix: str) -> FileObjectStore:
                    return FileObjectStore(root / "objects" / prefix)

                def checkpoint_store(self, key: str) -> JsonCheckpointStore:
                    return JsonCheckpointStore(root / "checkpoints" / f"{key}.json")

            class Cursor:
                def __init__(self, existing_event_id: str) -> None:
                    self.existing_event_id = existing_event_id
                    self.statements: list[str] = []

                def execute(self, sql: str) -> None:
                    self.statements.append(sql)

                def fetchall(self) -> list[tuple[str]]:
                    return [(self.existing_event_id,)]

            from quadringent.raw import RawBatchWriter

            storage = Storage()
            (table,) = loader.parse_table_set(_TABLE_SET_JSON)
            plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")
            store = storage.object_store(loader.raw_prefix_for_table("raw", "SALES", "ORDHDR"))
            position = JournalPosition("RCV0001", 1)
            event = _event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A"})
            with tempfile.TemporaryDirectory() as staging:
                manifest = RawBatchWriter(staging).write_batch([event], high_watermark=position)
                payload = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
                metadata = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()
            RawFirstCaptureCoordinator(store, JsonCheckpointStore(root / "capture.json")).capture_receipted_window_result(
                start=position, end=position, previous=None, manifest_content=metadata, payload=payload,
            )

            client = FakeStreamingClient()
            name = HistoryStreamingLoader(plan=plan, client=client, stream_id="SALES/ORDHDR").channel_name
            channel = client.channels[name]
            channel.rows.append({"EVENT_ID": event.event_id})
            cursor = Cursor(event.event_id)

            result = loader.load_table_once(
                table, plan=plan, storage=storage, raw_prefix_root="raw",
                streaming_client_factory=lambda _table: client, cursor=cursor,
            )

            self.assertEqual(result, (1, 0))
            self.assertEqual(len(channel.rows), 1)
            self.assertEqual(len(cursor.statements), 2)
            self.assertTrue(cursor.statements[0].startswith("SELECT EVENT_ID"))
            self.assertEqual(
                storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id)).load(), position
            )

    def test_crash_after_snowpipe_commit_across_receiver_rotation_does_not_duplicate_history(self) -> None:
        """Le jeton du canal peut être dans le receveur suivant le checkpoint local."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Storage:
                def object_store(self, prefix: str) -> FileObjectStore:
                    return FileObjectStore(root / "objects" / prefix)

                def checkpoint_store(self, key: str) -> JsonCheckpointStore:
                    return JsonCheckpointStore(root / "checkpoints" / f"{key}.json")

            class Cursor:
                def __init__(self) -> None:
                    self.statements: list[str] = []

                def execute(self, sql: str) -> None:
                    self.statements.append(sql)

                def fetchall(self) -> list[tuple[str]]:
                    return []

            from quadringent.raw import RawBatchWriter

            storage = Storage()
            (table,) = loader.parse_table_set(_TABLE_SET_JSON)
            plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")
            store = storage.object_store(loader.raw_prefix_for_table("raw", "SALES", "ORDHDR"))
            old_checkpoint = JournalPosition("RCV0001", 9)
            old_position = JournalPosition("RCV0001", 10)
            new_position = JournalPosition("RCV0002", 1)
            capture_checkpoint = JsonCheckpointStore(root / "capture.json")
            capture_checkpoint.commit(old_checkpoint)
            coordinator = RawFirstCaptureCoordinator(store, capture_checkpoint)
            old_event = _event("c", seq=10, after={"ORDER_ID": 10, "LABEL": "A"})
            new_event = _event("c", seq=1, receiver="RCV0002", after={"ORDER_ID": 11, "LABEL": "B"})

            def publish(event: ChangeEvent, previous: JournalPosition) -> None:
                with tempfile.TemporaryDirectory() as staging:
                    manifest = RawBatchWriter(staging).write_batch([event], high_watermark=event.position)
                    payload = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
                    metadata = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()
                coordinator.capture_receipted_window_result(
                    start=event.position, end=event.position, previous=previous,
                    manifest_content=metadata, payload=payload,
                )

            publish(old_event, old_checkpoint)
            publish(new_event, old_position)
            checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id))
            checkpoint.commit(old_checkpoint)

            client = FakeStreamingClient()
            channel_name = HistoryStreamingLoader(plan=plan, client=client, stream_id="SALES/ORDHDR").channel_name
            channel = client.channels[channel_name]
            channel.rows.extend([{"EVENT_ID": old_event.event_id}, {"EVENT_ID": new_event.event_id}])
            channel.latest_committed_offset_token = encode_offset_token(new_position)
            cursor = Cursor()

            result = loader.load_table_once(
                table, plan=plan, storage=storage, raw_prefix_root="raw",
                streaming_client_factory=lambda _table: client, cursor=cursor,
            )

            self.assertEqual(result, (2, 0))
            self.assertEqual([row["EVENT_ID"] for row in channel.rows], [old_event.event_id, new_event.event_id])
            self.assertEqual(len(cursor.statements), 2)  # le miroir est rejoué après le crash
            self.assertEqual(checkpoint.load(), new_position)

    def test_reuses_one_channel_per_table_and_closes_all_sessions(self) -> None:
        clients: list[FakeStreamingClient] = []

        def factory(_history_table: str) -> FakeStreamingClient:
            client = FakeStreamingClient()
            clients.append(client)
            return client

        pool = loader.StreamingSessionPool(factory, flush_each_batch=True)
        (table,) = loader.parse_table_set(_TABLE_SET_JSON)
        plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")

        first = pool.for_table(table, plan)
        self.assertIs(pool.for_table(table, plan), first)
        self.assertEqual(len(clients), 1)

        second_table = replace(table, table_id="tbl2", table_name="ORDDTL")
        second_plan = loader.build_plan(second_table, database="ACME_RAW", schema="CURATED")
        self.assertIsNot(pool.for_table(second_table, second_plan), first)
        self.assertEqual(len(clients), 2)

        pool.close()
        self.assertTrue(all(client.closed for client in clients))
        self.assertTrue(all(channel.closed for client in clients for channel in client.channels.values()))

    def test_loader_reuses_the_channel_for_successive_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Storage:
                def object_store(self, prefix: str) -> FileObjectStore:
                    return FileObjectStore(root / "objects" / prefix)

                def checkpoint_store(self, key: str) -> JsonCheckpointStore:
                    return JsonCheckpointStore(root / "checkpoints" / f"{key}.json")

            class Cursor:
                def __init__(self) -> None:
                    self.statements: list[str] = []

                def execute(self, sql: str) -> None:
                    self.statements.append(sql)

                def fetchall(self) -> list[tuple[str]]:
                    return []

            from quadringent.raw import RawBatchWriter

            storage = Storage()
            (table,) = loader.parse_table_set(_TABLE_SET_JSON)
            plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")
            store = storage.object_store(loader.raw_prefix_for_table("raw", "SALES", "ORDHDR"))
            coordinator = RawFirstCaptureCoordinator(store, JsonCheckpointStore(root / "capture.json"))
            clients: list[FakeStreamingClient] = []

            def factory(_history_table: str) -> FakeStreamingClient:
                client = FakeStreamingClient()
                clients.append(client)
                return client

            def publish(sequence: int) -> None:
                position = JournalPosition("RCV0001", sequence)
                event = _event("c", seq=sequence, after={"ORDER_ID": sequence, "LABEL": "A"})
                with tempfile.TemporaryDirectory() as staging:
                    manifest = RawBatchWriter(staging).write_batch([event], high_watermark=position)
                    payload = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
                    metadata = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()
                coordinator.capture_receipted_window_result(
                    start=position, end=position,
                    previous=JournalPosition("RCV0001", sequence - 1) if sequence > 1 else None,
                    manifest_content=metadata, payload=payload,
                    scan_completed_at=datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
                )

            pool = loader.StreamingSessionPool(factory, flush_each_batch=True)
            cursor = Cursor()

            def load() -> tuple[int, int]:
                return loader.load_table_once(
                    table, plan=plan, storage=storage, raw_prefix_root="raw",
                    streaming_client_factory=factory, cursor=cursor, session_pool=pool,
                )

            publish(1)
            self.assertEqual(load(), (1, 1))
            self.assertEqual(load(), (0, 0))
            publish(2)
            self.assertEqual(load(), (1, 1))
            self.assertEqual(len(clients), 1)
            self.assertEqual(len(next(iter(clients[0].channels.values())).rows), 2)
            self.assertEqual(sum(sql.startswith("MERGE") for sql in cursor.statements), 2)
            pool.close()


def test_sql_history_replays_mirror_after_crash_without_duplicate_history() -> None:
    """Le checkpoint n'avance pas entre les deux MERGE ; le replay finit le miroir."""
    from quadringent.raw import RawBatchWriter

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        class Storage:
            def object_store(self, prefix: str) -> FileObjectStore:
                return FileObjectStore(root / "objects" / prefix)

            def checkpoint_store(self, key: str) -> JsonCheckpointStore:
                return JsonCheckpointStore(root / "checkpoints" / f"{key}.json")

        class Cursor:
            def __init__(self) -> None:
                self.history: set[str] = set()
                self.mirror: set[str] = set()
                self.fail_mirror = True
                self.rowcount = -1

            def execute(self, sql: str, params: tuple | None = None) -> None:
                if '"ORDHDR_HISTORY" AS target' in sql:
                    assert params is not None
                    event_id = params[0]
                    self.rowcount = int(event_id not in self.history)
                    self.history.add(event_id)
                elif '"ORDHDR_MIRROR" AS target' in sql:
                    if self.fail_mirror:
                        self.fail_mirror = False
                        raise RuntimeError("crash before mirror commit")
                    self.mirror.add(event.event_id)

        storage = Storage()
        cursor = Cursor()
        (table,) = loader.parse_table_set(_TABLE_SET_JSON)
        plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")
        event = _event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A"})
        position = event.position
        with tempfile.TemporaryDirectory() as staging:
            manifest = RawBatchWriter(staging).write_batch([event], high_watermark=position)
            payload = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
            metadata = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()
        store = storage.object_store(loader.raw_prefix_for_table("raw", "SALES", "ORDHDR"))
        RawFirstCaptureCoordinator(store, JsonCheckpointStore(root / "capture.json")).capture_receipted_window_result(
            start=position, end=position, previous=None, manifest_content=metadata, payload=payload,
        )
        checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id))

        with unittest.TestCase().assertRaisesRegex(RuntimeError, "crash before mirror"):
            loader.load_table_once(
                table, plan=plan, storage=storage, raw_prefix_root="raw",
                streaming_client_factory=None, cursor=cursor, history_mode="sql",
            )
        assert checkpoint.load() is None
        assert cursor.history == {event.event_id}
        assert cursor.mirror == set()

        result = loader.load_table_once(
            table, plan=plan, storage=storage, raw_prefix_root="raw",
            streaming_client_factory=None, cursor=cursor, history_mode="sql",
        )
        assert result == (1, 0)
        assert cursor.history == cursor.mirror == {event.event_id}
        assert checkpoint.load() == position


class RawPrefixForTableTests(unittest.TestCase):
    """Disposition unique (``quadringent.storage_layout``) : la table est le
    seul segment (minuscule), le schéma n'y figure plus. Corrigé le 24
    septembre 2026 : la convention précédente (``<racine>/<SCHÉMA>/<TABLE>``)
    n'était jamais celle produite par le lecteur v2 (``<racine>/<table>/
    journal``), donc le chargeur ne trouvait jamais rien."""

    def test_joins_root_and_table(self) -> None:
        self.assertEqual(loader.raw_prefix_for_table("ibmi/ledger", "SALES", "ORDHDR"), "ibmi/ledger/ordhdr/journal")

    def test_empty_root_is_handled(self) -> None:
        self.assertEqual(loader.raw_prefix_for_table("", "SALES", "ORDHDR"), "ordhdr/journal")

    def test_trailing_slash_is_stripped(self) -> None:
        self.assertEqual(loader.raw_prefix_for_table("ibmi/ledger/", "SALES", "ORDHDR"), "ibmi/ledger/ordhdr/journal")

    def test_matches_the_fleet_reader_convention(self) -> None:
        """Même formule que le lecteur, mode flotte et mode une seule table
        (``quadringent.fleet_capture.table_object_prefix`` /
        ``v2/executor/manifests.py::build_reader_deployment``)."""

        from quadringent.fleet_capture import table_object_prefix

        self.assertEqual(
            loader.raw_prefix_for_table("ibmi/ledger", "SALES", "ORDHDR"),
            table_object_prefix("ibmi/ledger", "ORDHDR"),
        )


class LoaderCheckpointStreamKeyTests(unittest.TestCase):
    def test_distinct_from_capture_checkpoint(self) -> None:
        self.assertEqual(loader.loader_checkpoint_stream_key("tbl1"), "tbl1-destination-loader")

    def test_each_initial_copy_run_has_its_own_checkpoint(self) -> None:
        first = loader.loader_checkpoint_stream_key("tbl1", "raw/tbl1/evidence/run-1.json")
        second = loader.loader_checkpoint_stream_key("tbl1", "raw/tbl1/evidence/run-2.json")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, loader.loader_checkpoint_stream_key("tbl1"))


class InitialCopyRerunTests(unittest.TestCase):
    def test_new_run_rebuilds_mirror_even_when_an_old_loader_checkpoint_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Storage:
                def object_store(self, prefix: str) -> FileObjectStore:
                    return FileObjectStore(root / "objects" / prefix)

                def checkpoint_store(self, key: str) -> JsonCheckpointStore:
                    return JsonCheckpointStore(root / "checkpoints" / f"{key}.json")

            class Cursor:
                def __init__(self) -> None:
                    self.statements: list[str] = []
                    self.fail_next_merge = True

                def execute(self, sql: str) -> None:
                    self.statements.append(sql)
                    if sql.startswith("MERGE") and self.fail_next_merge:
                        self.fail_next_merge = False
                        raise RuntimeError("crash après validation du canal")

                def fetchall(self) -> list[tuple[str]]:
                    return []

            storage = Storage()
            (base_table,) = loader.parse_table_set(_TABLE_SET_JSON)
            evidence_key = "raw/tbl1/evidence/run-2.json"
            table = replace(base_table, evidence_key=evidence_key)
            plan = loader.build_plan(table, database="ACME_RAW", schema="CURATED")
            old_checkpoint = storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id))
            old_checkpoint.commit(JournalPosition("RCV0001", 10))
            snapshot_event = _event(
                "c", seq=1, receiver="SNAPSHOT-run-2", after={"ORDER_ID": 7, "LABEL": "recopie"}
            )
            snapshot_store = storage.object_store(snapshot_prefix("raw", table.table_name))
            published = publish_raw_batch(snapshot_store, [snapshot_event], high_watermark=snapshot_event.position)
            boundary = JournalBoundary(
                receiver_library="SALES", receiver_name="RCV0001", last_sequence=20,
                observed_at=datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc),
            )
            evidence = InitialCopyEvidence(
                pipeline_id="pipe1", table_id=table.table_id, run_id="run-2", boundary=boundary,
                rows_copied=1, completed_at=datetime(2026, 9, 28, 8, 1, tzinfo=timezone.utc),
                snapshot_batches=(SnapshotBatchRef(published.payload_key, published.manifest_key),),
            )
            client = FakeStreamingClient()
            pool = loader.StreamingSessionPool(lambda _table: client)
            cursor = Cursor()

            # L'ancien checkpoint n'autorise pas le nouveau run à commencer
            # tant que la preuve durable de sa copie n'existe pas.
            self.assertEqual(
                loader.load_table_once(
                    table, plan=plan, storage=storage, raw_prefix_root="raw",
                    streaming_client_factory=lambda _table: client, cursor=cursor, session_pool=pool,
                ),
                (0, 0),
            )
            self.assertEqual(cursor.statements, [])
            storage.object_store("").put_once(evidence_key, json.dumps(evidence.to_dict()).encode())

            with self.assertRaisesRegex(RuntimeError, "crash après validation"):
                loader.load_table_once(
                    table, plan=plan, storage=storage, raw_prefix_root="raw",
                    streaming_client_factory=lambda _table: client, cursor=cursor, session_pool=pool,
                )
            self.assertIsNone(
                storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, evidence_key)).load()
            )

            result = loader.load_table_once(
                table, plan=plan, storage=storage, raw_prefix_root="raw",
                streaming_client_factory=lambda _table: client, cursor=cursor, session_pool=pool,
            )

            self.assertEqual(result, (0, 0))
            self.assertEqual(old_checkpoint.load(), JournalPosition("RCV0001", 10))
            self.assertEqual(
                storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, evidence_key)).load(),
                JournalPosition("RCV0001", 20),
            )
            self.assertTrue(any(sql.startswith("DELETE FROM") for sql in cursor.statements))
            self.assertEqual(
                [row["EVENT_ID"] for channel in client.channels.values() for row in channel.rows],
                [snapshot_event.event_id],
            )

            # Le lecteur partagé était déjà plus loin que la frontière de la
            # copie. Son reçu englobe un événement ancien et un nouveau.
            old_event = _event("c", seq=18, after={"ORDER_ID": 8, "LABEL": "avant"})
            new_event = _event("c", seq=23, after={"ORDER_ID": 9, "LABEL": "après"})
            capture_checkpoint = JsonCheckpointStore(root / "capture.json")
            capture_checkpoint.commit(JournalPosition("RCV0001", 14))
            coordinator = RawFirstCaptureCoordinator(
                storage.object_store(loader.raw_prefix_for_table("raw", "SALES", "ORDHDR")),
                capture_checkpoint,
            )
            with tempfile.TemporaryDirectory() as staging:
                from quadringent.raw import RawBatchWriter

                manifest = RawBatchWriter(staging).write_batch(
                    [old_event, new_event], high_watermark=JournalPosition("RCV0001", 25)
                )
                payload = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
                metadata = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()
            coordinator.capture_receipted_window_result(
                start=JournalPosition("RCV0001", 15), end=JournalPosition("RCV0001", 25),
                previous=JournalPosition("RCV0001", 14), manifest_content=metadata, payload=payload,
            )

            result = loader.load_table_once(
                table, plan=plan, storage=storage, raw_prefix_root="raw",
                streaming_client_factory=lambda _table: client, cursor=cursor, session_pool=pool,
            )
            self.assertEqual(result, (1, 1))
            self.assertEqual(
                [row["EVENT_ID"] for channel in client.channels.values() for row in channel.rows],
                [snapshot_event.event_id, new_event.event_id],
            )
            self.assertEqual(
                storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, evidence_key)).load(),
                JournalPosition("RCV0001", 25),
            )
            pool.close()



class SnowflakeRoleToWarehouseTests(unittest.TestCase):
    def test_derives_warehouse_from_role(self) -> None:
        self.assertEqual(loader._snowflake_role_to_warehouse("QDT_ROLE_ABCD1234"), "QDT_WH_ABCD1234")

    def test_rejects_a_role_outside_convention(self) -> None:
        with self.assertRaises(loader.DestinationLoaderError):
            loader._snowflake_role_to_warehouse("CUSTOM_ROLE")


class StreamingProfileTests(unittest.TestCase):
    def test_profile_carries_expected_keys(self) -> None:
        profile = loader._streaming_profile(
            account="acme-sf",
            user="QDT_SVC_ABCD1234",
            role="QDT_ROLE_ABCD1234",
            private_key_pem='fixture-private-key',
            warehouse="QDT_WH_ABCD1234",
            database="QUADRINGENT",
            schema="CURATED",
        )
        self.assertEqual(
            set(profile.keys()),
            {
                "account",
                "authorization_type",
                "database",
                "host",
                "private_key",
                "role",
                "schema",
                "url",
                "user",
                "warehouse",
            },
        )
        # "JWT" est la valeur attendue par le SDK Snowpipe Streaming pour
        # l'authentification par paire de clés (constaté en vérification
        # réelle contre le compte de qualification : "KEY_PAIR" est refusé
        # avec ConfigError "unsupported authorization type").
        self.assertEqual(profile["authorization_type"], "JWT")

    def test_profile_carries_a_non_empty_host_and_url_consistent_with_the_account(self) -> None:
        """Le profil fournit un hôte et une URL cohérents avec le compte.
        Le SDK Snowpipe Streaming exige ces champs pour construire
        son URL de connexion ; ``account`` seul ne suffit pas."""

        profile = loader._streaming_profile(
            account="EXAMPLEORG-EXAMPLEACCOUNT",
            user="QDT_SVC_ABCD1234",
            role="QDT_ROLE_ABCD1234",
            private_key_pem='fixture-private-key',
            warehouse="QDT_WH_ABCD1234",
            database="QUADRINGENT",
            schema="CURATED",
        )
        self.assertEqual(profile["host"], "exampleorg-exampleaccount.snowflakecomputing.com")
        self.assertEqual(profile["url"], "https://exampleorg-exampleaccount.snowflakecomputing.com")


def _event(operation: str, *, seq: int, before=None, after=None, receiver="RCV0001") -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi-test",
        journal="TRNJRN",
        library="SALES",
        table="ORDHDR",
        operation=operation,
        position=JournalPosition(receiver=receiver, sequence=seq),
        commit_timestamp="2026-09-23T08:00:00.000000",
        schema_version="v1",
        before=before,
        after=after,
    )


class DiscoverNewBatchesTests(unittest.TestCase):
    def _coordinator(self, root: Path) -> tuple[RawFirstCaptureCoordinator, FileObjectStore, JsonCheckpointStore]:
        store = FileObjectStore(root / "objects")
        checkpoint = JsonCheckpointStore(root / "capture-checkpoint.json")
        return RawFirstCaptureCoordinator(store, checkpoint), store, checkpoint

    def test_discovers_a_published_window_after_the_loader_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coordinator, store, _capture_checkpoint = self._coordinator(root)
            events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A"})]
            end = JournalPosition(receiver="RCV0001", sequence=1)
            from quadringent.raw import RawBatchWriter
            import tempfile as _tempfile

            with _tempfile.TemporaryDirectory() as staging:
                manifest = RawBatchWriter(staging).write_batch(events, high_watermark=end)
                payload_content = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
                manifest_content = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()

            coordinator.capture_receipted_window_result(
                start=JournalPosition(receiver="RCV0001", sequence=1),
                end=end,
                previous=None,
                manifest_content=manifest_content,
                payload=payload_content,
                scan_completed_at=datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
            )

            loader_checkpoint = JsonCheckpointStore(root / "loader-checkpoint.json")
            results = list(loader.discover_new_batches(store, loader_checkpoint))

            self.assertEqual(len(results), 1)
            position, batch = results[0]
            self.assertEqual(position, end)
            self.assertEqual(len(batch.events), 1)
            self.assertEqual(batch.events[0].event_id, events[0].event_id)

    def test_already_committed_position_is_not_rediscovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coordinator, store, _capture_checkpoint = self._coordinator(root)
            events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A"})]
            end = JournalPosition(receiver="RCV0001", sequence=1)
            from quadringent.raw import RawBatchWriter
            import tempfile as _tempfile

            with _tempfile.TemporaryDirectory() as staging:
                manifest = RawBatchWriter(staging).write_batch(events, high_watermark=end)
                payload_content = (Path(staging) / f"batch-{manifest.batch_id}.jsonl").read_bytes()
                manifest_content = (Path(staging) / f"batch-{manifest.batch_id}.manifest.json").read_bytes()

            coordinator.capture_receipted_window_result(
                start=end, end=end, previous=None,
                manifest_content=manifest_content, payload=payload_content,
                scan_completed_at=datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
            )

            loader_checkpoint = JsonCheckpointStore(root / "loader-checkpoint.json")
            loader_checkpoint.commit(end)

            results = list(loader.discover_new_batches(store, loader_checkpoint))
            self.assertEqual(results, [])

    def test_empty_scan_window_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coordinator, store, _capture_checkpoint = self._coordinator(root)
            start = JournalPosition(receiver="RCV0001", sequence=1)
            end = JournalPosition(receiver="RCV0001", sequence=1)
            coordinator.capture_receipted_window_result(
                start=start, end=end, previous=None,
                scan_completed_at=datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc),
            )
            loader_checkpoint = JsonCheckpointStore(root / "loader-checkpoint.json")
            results = list(loader.discover_new_batches(store, loader_checkpoint))
            self.assertEqual(results, [])

    def test_loader_follows_empty_receipts_across_receiver_rotation(self) -> None:
        """La rotation doit être prouvée par la chaîne des reçus, pas par
        l'ordre lexical des noms de receveurs ni par le seul lot non vide."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FileObjectStore(root / "objects")
            checkpoint = JsonCheckpointStore(root / "loader-checkpoint.json")
            checkpoint.commit(JournalPosition("RCV0003", 268))

            def receipt(name: str, previous: JournalPosition, end: JournalPosition, raw=None) -> None:
                store.put_once(
                    f"receipts/{name}.json",
                    json.dumps({
                        "format_version": "quadringent-scan-receipt-v2",
                        "previous": {"receiver": previous.receiver, "sequence": previous.sequence},
                        "start": {"receiver": end.receiver, "sequence": (
                            previous.sequence + 1 if previous.receiver == end.receiver else end.sequence
                        )},
                        "end": {"receiver": end.receiver, "sequence": end.sequence},
                        "event_count": 0 if raw is None else 2,
                        "raw": raw,
                        "scan_completed_at": "2026-09-28T11:00:00+00:00",
                    }).encode(),
                )

            receipt("old", JournalPosition("RCV0003", 267), JournalPosition("RCV0003", 268))
            receipt("empty", JournalPosition("RCV0003", 268), JournalPosition("RCV0003", 270))
            receipt("rotation", JournalPosition("RCV0003", 270), JournalPosition("RCV0004", 1))
            receipt("events", JournalPosition("RCV0004", 1), JournalPosition("RCV0004", 4), raw={
                "payload_key": "new.jsonl", "manifest_key": "new.manifest.json",
            })

            predecessors: dict[JournalPosition, JournalPosition | None] = {}
            chain = loader.discover_pending_receipts(store, checkpoint, predecessors=predecessors)
            self.assertEqual(
                [item.position for item in chain],
                [JournalPosition("RCV0003", 270), JournalPosition("RCV0004", 1), JournalPosition("RCV0004", 4)],
            )
            self.assertIsNone(chain[0].raw)
            self.assertIsNone(chain[1].raw)
            self.assertIsNotNone(chain[2].raw)
            self.assertEqual(
                loader.committed_receipt_index(
                    chain, JournalPosition("RCV0004", 4), checkpoint.load(), predecessors
                ),
                2,
            )
            self.assertIsNone(
                loader.committed_receipt_index(
                    chain, JournalPosition("RCV0003", 267), checkpoint.load(), predecessors
                )
            )
            with self.assertRaisesRegex(loader.DestinationLoaderError, "jeton Snowpipe absent"):
                loader.committed_receipt_index(
                    chain, JournalPosition("RCV9999", 1), checkpoint.load(), predecessors
                )

            loader.advance_loader_checkpoint(checkpoint, chain[0].position)
            loader.advance_loader_checkpoint(checkpoint, chain[1].position)
            loader.advance_loader_checkpoint(checkpoint, chain[2].position)
            self.assertEqual(checkpoint.load(), JournalPosition("RCV0004", 4))
            self.assertEqual(loader.discover_pending_receipts(store, checkpoint), [])

    def test_boundary_inside_a_published_receipt_is_a_valid_resume_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileObjectStore(Path(tmp) / "objects")
            checkpoint = JsonCheckpointStore(Path(tmp) / "checkpoint.json")
            checkpoint.commit(JournalPosition("RCV0001", 20))
            store.put_once(
                "receipts/window.json",
                json.dumps({
                    "format_version": "quadringent-scan-receipt-v1",
                    "previous": {"receiver": "RCV0001", "sequence": 14},
                    "start": {"receiver": "RCV0001", "sequence": 15},
                    "end": {"receiver": "RCV0001", "sequence": 25},
                    "event_count": 0,
                    "raw": None,
                }).encode(),
            )

            receipts = loader.discover_pending_receipts(store, checkpoint)

            self.assertEqual([receipt.position for receipt in receipts], [JournalPosition("RCV0001", 25)])


class DiscoverNewBatchesFromManifestKeysTests(unittest.TestCase):
    def test_discovers_via_direct_manifest_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FileObjectStore(root / "objects")
            events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A"})]
            end = JournalPosition(receiver="RCV0001", sequence=1)
            from quadringent.object_store import publish_raw_batch

            result = publish_raw_batch(store, events, high_watermark=end)

            loader_checkpoint = JsonCheckpointStore(root / "loader-checkpoint.json")
            manifest_keys = [result.manifest_key]
            results = list(loader.discover_new_batches_from_manifest_keys(store, loader_checkpoint, manifest_keys))

            self.assertEqual(len(results), 1)
            position, batch = results[0]
            self.assertEqual(position, end)
            self.assertEqual(len(batch.events), 1)

    def test_already_committed_position_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = FileObjectStore(root / "objects")
            events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A"})]
            end = JournalPosition(receiver="RCV0001", sequence=1)
            from quadringent.object_store import publish_raw_batch

            result = publish_raw_batch(store, events, high_watermark=end)

            loader_checkpoint = JsonCheckpointStore(root / "loader-checkpoint.json")
            loader_checkpoint.commit(end)
            results = list(
                loader.discover_new_batches_from_manifest_keys(store, loader_checkpoint, [result.manifest_key])
            )
            self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
