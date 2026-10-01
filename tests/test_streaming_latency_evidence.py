"""Durées du cycle Streaming, sans requête ni donnée métier supplémentaire."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
from unittest.mock import patch

import pytest

import quadringent_destination_loader as loader
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator, publish_raw_batch
from quadringent.raw import RawBatchWriter
from quadringent.snowflake_streaming_loader import FakeStreamingClient, StreamingCycleMetrics
from quadringent.storage_layout import snapshot_prefix
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef
from quadringent_control_plane.v2.services.loader_telemetry import KubernetesLoaderTelemetry, LoaderIdentity


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def monotonic(self):
        return self.seconds

    def utc(self):
        return datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(seconds=self.seconds)

    def delay(self, seconds):
        self.seconds += seconds


def _cycle(tmp_path, clock, fail=None):
    class Storage:
        def object_store(self, prefix):
            return FileObjectStore(tmp_path / "objects" / prefix)

        def checkpoint_store(self, key):
            class Checkpoint(JsonCheckpointStore):
                def commit(self, position):
                    clock.delay(8)
                    if fail == "checkpoint":
                        raise RuntimeError("credential=never-expose")
                    super().commit(position)

            return Checkpoint(tmp_path / "checkpoints" / f"{key}.json")

    table = loader.LoaderTable(
        "private-table-id", "TESTLIB", "SALE", ("ID",), ({"name": "ID", "kind": "integer", "nullable": False},)
    )
    plan = loader.build_plan(table, database="TEST", schema="TEST")
    storage = Storage()
    event = ChangeEvent(
        source_system="test",
        journal="TESTJRN",
        library="TESTLIB",
        table="SALE",
        operation="c",
        position=JournalPosition("R1", 1),
        commit_timestamp="2026-09-30T00:00:00Z",
        schema_version="v1",
        before=None,
        after={"ID": 999},
    )
    staging = tmp_path / "staging"
    manifest = RawBatchWriter(staging).write_batch([event], high_watermark=event.position)
    RawFirstCaptureCoordinator(
        storage.object_store(loader.raw_prefix_for_table("raw", "TESTLIB", "SALE")),
        JsonCheckpointStore(tmp_path / "capture.json"),
    ).capture_receipted_window_result(
        start=event.position,
        end=event.position,
        previous=None,
        manifest_content=(staging / f"batch-{manifest.batch_id}.manifest.json").read_bytes(),
        payload=(staging / f"batch-{manifest.batch_id}.jsonl").read_bytes(),
    )
    client = FakeStreamingClient()
    statements = []

    class Cursor:
        rowcount = 1

        def execute(self, sql, params=None):
            statements.append(sql)
            if sql.startswith(("MERGE", "DELETE")):
                clock.delay(7)
                if fail == "merge":
                    raise RuntimeError("credential=never-expose")

        def fetchall(self):
            return []

        def fetchone(self):
            return (1,)

    def factory(_history):
        clock.delay(3)
        if fail == "open_channel":
            raise RuntimeError("credential=never-expose")
        original = client.open_channel

        def opened(name):
            channel = original(name)
            for method, seconds, stage in (
                ("append_rows", 4, "append"),
                ("initiate_flush", 5, "flush"),
                ("wait_for_commit", 6, "commit"),
            ):
                call = getattr(channel, method)

                def delayed(*args, call=call, seconds=seconds, stage=stage, **kwargs):
                    clock.delay(seconds)
                    if fail == stage:
                        raise RuntimeError("credential=never-expose")
                    return call(*args, **kwargs)

                setattr(channel, method, delayed)
            return channel

        client.open_channel = opened
        return client

    return table, plan, storage, factory, Cursor(), statements


def _records(caplog):
    return [
        json.loads(record.getMessage().split("loader_cycle ", 1)[1])
        for record in caplog.records
        if "loader_cycle " in record.getMessage()
    ]


def _run(tmp_path, caplog, fail=None, flush=True, snapshot=False):
    clock = Clock()
    table, plan, storage, factory, cursor, statements = _cycle(tmp_path, clock, fail)
    if snapshot:
        table = replace(table, evidence_key="proof/new-copy.json")
        event = ChangeEvent(
            source_system="test",
            journal="TESTJRN",
            library="TESTLIB",
            table="SALE",
            operation="c",
            position=JournalPosition("SNAPSHOT-run", 1),
            commit_timestamp="2026-09-30T00:00:00Z",
            schema_version="v1",
            before=None,
            after={"ID": 999},
        )
        batch = publish_raw_batch(
            storage.object_store(snapshot_prefix("raw", table.table_name)), [event], high_watermark=event.position
        )
        evidence = InitialCopyEvidence(
            pipeline_id="test-pipeline",
            table_id=table.table_id,
            run_id="test-run",
            boundary=JournalBoundary(
                receiver_library="TESTLIB", receiver_name="R1", last_sequence=1, observed_at=clock.utc()
            ),
            rows_copied=1,
            completed_at=clock.utc(),
            snapshot_batches=(SnapshotBatchRef(batch.payload_key, batch.manifest_key),),
        )
        storage.object_store("").put_once(table.evidence_key, json.dumps(evidence.to_dict()).encode())
    metrics = StreamingCycleMetrics(table.table_id, monotonic=clock.monotonic, utc_now=clock.utc)
    original_discovery, original_read = loader.discover_pending_receipts, loader.read_published_batch

    def discovery(*args, **kwargs):
        clock.delay(1)
        if fail == "discovery_raw":
            raise RuntimeError("credential=never-expose")
        return original_discovery(*args, **kwargs)

    def read(*args, **kwargs):
        clock.delay(2)
        return original_read(*args, **kwargs)

    caplog.set_level(logging.INFO, logger=loader.LOG.name)
    with (
        patch.object(loader, "discover_pending_receipts", discovery),
        patch.object(loader, "read_published_batch", read),
    ):
        arguments = dict(
            plan=plan,
            storage=storage,
            raw_prefix_root="raw",
            streaming_client_factory=factory,
            cursor=cursor,
            flush_each_batch=flush,
            cycle_metrics=metrics,
        )
        if fail:
            with pytest.raises(RuntimeError, match="never-expose"):
                loader.load_table_once(table, **arguments)
        else:
            assert loader.load_table_once(table, **arguments) == ((0, 0) if snapshot else (1, 1))
    return table, plan, storage, cursor, statements, _records(caplog)[0]


def test_stage_delays_are_attributed_and_idle_has_no_sql_or_record(tmp_path, caplog):
    table, plan, storage, cursor, statements, record = _run(tmp_path, caplog)
    assert record["stages_ms"] == dict(
        discovery_raw=4000, open_channel=3000, append=4000, flush=5000, commit=6000, merge=7000, checkpoint=8000
    )
    assert record["cycle_ms"] == 37000
    assert record["started_at"] == "2026-09-30T00:00:00Z"
    assert record["finished_at"] == "2026-09-30T00:00:37Z"
    assert record["status"] == "success" and record["failed_stage"] is None
    assert len(statements) == 2  # La recherche EVENT_ID existante puis le MERGE, aucune sonde ajoutée.
    assert statements[0].startswith("SELECT EVENT_ID") and statements[1].startswith("MERGE")
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id)).load() == JournalPosition(
        "R1", 1
    )
    caplog.clear()
    assert loader.load_table_once(
        table, plan=plan, storage=storage, raw_prefix_root="raw", streaming_client_factory=None, cursor=cursor
    ) == (0, 0)
    assert len(statements) == 2 and _records(caplog) == []
    assert table.table_id not in json.dumps(record)
    assert all(value not in json.dumps(record) for value in ("TESTLIB", "SALE", "999", "R1"))


def test_flush_not_requested_remains_unmeasured(tmp_path, caplog):
    *_, record = _run(tmp_path, caplog, flush=False)
    assert record["stages_ms"]["flush"] is None
    assert record["cycle_ms"] == 32000


@pytest.mark.parametrize("stage", ["discovery_raw", "open_channel", "append", "flush", "commit", "merge", "checkpoint"])
def test_failure_reports_exact_stage_without_advancing_checkpoint(tmp_path, caplog, stage):
    table, _, storage, _, _, record = _run(tmp_path, caplog, fail=stage)
    assert record["status"] == "failed" and record["failed_stage"] == stage
    assert record["stages_ms"][stage] is not None
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id)).load() is None
    assert "never-expose" not in json.dumps(record)


def test_actual_cycle_is_visible_only_for_exact_table_via_existing_log_service(tmp_path, caplog):
    table, _, _, _, _, record = _run(tmp_path, caplog)

    class Pods:
        def list_pod_names(self, *, label_selector, limit):
            assert label_selector == "quadringent.io/component=destination-loader,quadringent.io/destination-id=dst1"
            assert limit == 3
            return ("loader-1",)

        def read_pod_log(self, name, *, since_time, tail_lines):
            assert tail_lines == 2000
            return "2026-09-30T00:00:37Z INFO loader_cycle " + json.dumps(record)

    telemetry = KubernetesLoaderTelemetry(
        Pods(),
        resolve=lambda pipeline: LoaderIdentity(
            "dst1", table.table_name, table.table_id if pipeline == "correct" else "another-id-with-same-table-name"
        ),
        now=lambda: datetime(2026, 9, 30, 0, 1, tzinfo=timezone.utc),
    )
    entries = telemetry.fetch("correct", since=None)
    assert len(entries) == 1
    assert "Cycle Streaming" in entries[0].message and "commit=6000.0 ms" in entries[0].message
    assert "2026-09-30T00:00:00Z" in entries[0].message and "2026-09-30T00:00:37Z" in entries[0].message
    assert telemetry.fetch("another", since=None) == ()
    assert telemetry.metrics("correct", "1h").points == ()
    assert telemetry.observe("correct").lag_seconds is None
    assert telemetry.observe("correct").last_arrival_at is None
    for mutate in (
        lambda data: data.update(token="never-expose"),
        lambda data: data["stages_ms"].update(commit=-1),
        lambda data: data["stages_ms"].update(commit=True),
        lambda data: data["stages_ms"].update(commit=float("nan")),
        lambda data: data["stages_ms"].update(commit=10**1000),
        lambda data: data.update(status="secret"),
        lambda data: data.update(finished_at="2026-09-29T00:00:00Z"),
        lambda data: data.update(started_at="0001-01-01T00:00:00+01:00"),
    ):
        saved = json.loads(json.dumps(record))
        mutate(record)
        assert telemetry.fetch("correct", since=None) == ()
        record.clear()
        record.update(saved)


def test_reused_channel_does_not_claim_a_new_open(tmp_path):
    clock = Clock()
    table, plan, _, factory, _, _ = _cycle(tmp_path, clock)
    with_pool = loader.StreamingSessionPool(factory)
    cold = StreamingCycleMetrics(table.table_id, monotonic=clock.monotonic, utc_now=clock.utc)
    first = with_pool.for_table(table, plan, cycle_metrics=cold)
    warm = StreamingCycleMetrics(table.table_id, monotonic=clock.monotonic, utc_now=clock.utc)
    assert with_pool.for_table(table, plan, cycle_metrics=warm) is first
    assert cold.stages_ms["open_channel"] == 3000 and warm.stages_ms["open_channel"] is None
    with_pool.close()


def test_empty_receipt_advances_checkpoint_without_idle_record(tmp_path, caplog):
    clock = Clock()
    table, plan, storage, factory, cursor, statements = _cycle(tmp_path, clock)
    # Le reçu existant est traité, puis une fenêtre vide avance seulement le journal.
    loader.load_table_once(
        table, plan=plan, storage=storage, raw_prefix_root="raw", streaming_client_factory=factory, cursor=cursor
    )
    capture = JsonCheckpointStore(tmp_path / "capture.json")
    position = JournalPosition("R1", 2)
    RawFirstCaptureCoordinator(
        storage.object_store(loader.raw_prefix_for_table("raw", "TESTLIB", "SALE")), capture
    ).capture_receipted_window_result(
        start=position, end=position, previous=JournalPosition("R1", 1), manifest_content=None, payload=None
    )
    caplog.set_level(logging.INFO, logger=loader.LOG.name)
    metrics = StreamingCycleMetrics(table.table_id, monotonic=clock.monotonic, utc_now=clock.utc)
    assert loader.load_table_once(
        table,
        plan=plan,
        storage=storage,
        raw_prefix_root="raw",
        streaming_client_factory=lambda _: FakeStreamingClient(),
        cursor=cursor,
        cycle_metrics=metrics,
    ) == (0, 0)
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id)).load() == position
    assert len(statements) == 2 and _records(caplog) == []


def test_sql_mode_does_not_emit_a_streaming_measurement(tmp_path, caplog):
    clock = Clock()
    table, plan, storage, _, cursor, _ = _cycle(tmp_path, clock)
    caplog.set_level(logging.INFO, logger=loader.LOG.name)
    assert loader.load_table_once(
        table,
        plan=plan,
        storage=storage,
        raw_prefix_root="raw",
        streaming_client_factory=None,
        cursor=cursor,
        history_mode="sql",
        cycle_metrics=StreamingCycleMetrics(table.table_id),
    ) == (1, 1)
    assert _records(caplog) == []


def test_initial_copy_includes_delete_merge_and_boundary_checkpoint(tmp_path, caplog):
    table, _, storage, _, statements, record = _run(tmp_path, caplog, snapshot=True)
    assert record["stages_ms"] == dict(
        discovery_raw=4000, open_channel=3000, append=4000, flush=5000, commit=6000, merge=14000, checkpoint=8000
    )
    assert record["cycle_ms"] == 44000
    assert len(statements) == 3
    assert statements[0].startswith("DELETE")
    assert statements[1].startswith("SELECT EVENT_ID") and statements[2].startswith("MERGE")
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id, table.evidence_key)).load() == (
        JournalPosition("R1", 1)
    )


def test_metrics_from_another_table_are_rejected_before_loading(tmp_path):
    clock = Clock()
    table, plan, storage, _, cursor, statements = _cycle(tmp_path, clock)
    with pytest.raises(loader.DestinationLoaderError, match="autre table"):
        loader.load_table_once(
            table,
            plan=plan,
            storage=storage,
            raw_prefix_root="raw",
            streaming_client_factory=None,
            cursor=cursor,
            cycle_metrics=StreamingCycleMetrics("different-table"),
        )
    assert statements == []
    assert storage.checkpoint_store(loader.loader_checkpoint_stream_key(table.table_id)).load() is None
