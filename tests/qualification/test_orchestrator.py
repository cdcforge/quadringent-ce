"""Tests de l'orchestrateur (orchestrator.py), entièrement avec des fakes."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quadringent_qualification.config import (
    CaptureConfig,
    RunConfig,
    SourceConfig,
    StorageConfig,
    WarehouseConfig,
)
from quadringent_qualification.orchestrator import Orchestrator, measure_freshness
from quadringent_qualification.adapters import CaptureBoundary, CaptureResult, SourceResult, RawReplayEvidence
from quadringent_qualification.generator import generated_row
from quadringent_qualification.published_probes import PublishedProbe
from quadringent_qualification.schema import Column, TableSchema

from .fakes import FakeCaptureRunner, FakeSourceDriver, FakeStorageBackend, FakeWarehouseLoader

SCHEMA = TableSchema(
    qualified_name="QUALIF_LIB.QUALIF_ORDERS",
    primary_key="ORDER_ID",
    columns=(
        Column("ORDER_ID", "integer"),
        Column("LABEL", "varchar", length=40),
        Column("CODE", "char", length=8),
        Column("AMOUNT", "decimal", precision=11, scale=2),
        Column("EVENT_DATE", "date"),
        Column("UPDATED_AT", "timestamp", timestamp_precision=6),
        Column("NOTE", "varchar", length=80),
    ),
)


def make_config(steps: tuple[str, ...]) -> RunConfig:
    return RunConfig(
        run_id="qual-test-1",
        table=SCHEMA,
        source=SourceConfig("ibmi_java", ("QUALIF_LIB",), "/secrets/source.json", "QUALIF_LIB", "QUALJRN"),
        capture=CaptureConfig(image="quadringent-capture:test", max_seconds=30),
        storage=StorageConfig("gcs", "qualif-bucket", "qualification/qual-test-1"),
        warehouse=WarehouseConfig("snowflake", "/secrets/sf.json", "QUALIF_DB", "QUALIF_SCHEMA"),
        steps=steps,
        bootstrap_sequence=1,
        bootstrap_receiver="R1",
    )


def make_orchestrator(steps, **overrides):
    config = make_config(steps)
    source = overrides.get("source") or FakeSourceDriver(primary_key="ORDER_ID")
    capture = overrides.get("capture") or FakeCaptureRunner()
    storage = overrides.get("storage") or FakeStorageBackend()
    warehouse = overrides.get("warehouse") or FakeWarehouseLoader()
    return Orchestrator(config, source=source, capture=capture, storage=storage, warehouse=warehouse)


# --- Étapes DML -------------------------------------------------------------

def test_run_seed_populates_oracle_and_source():
    orch = make_orchestrator(("seed",))
    report = orch.run(("seed",))
    assert report.status == "PASS"
    assert len(orch.oracle) == 100
    assert len(orch.source.rows) == 100
    assert report.steps[0].details["statements"] == 100


def test_run_seed_then_changes1_reflects_deletes_and_updates():
    orch = make_orchestrator(("seed", "changes1"))
    orch.run(("seed", "changes1"))
    assert set(range(91, 96)).isdisjoint(orch.oracle)
    assert orch.oracle[1]["LABEL"] == "Modifié n°1"


def test_run_dml_step_failure_marks_fail_and_stops():
    source = FakeSourceDriver(exec_fails=True)
    orch = make_orchestrator(("seed", "changes1"), source=source)
    report = orch.run(("seed", "changes1"))
    assert report.status == "FAIL"
    assert report.steps[0].status == "FAIL"
    assert len(report.steps) == 1  # s'arrête après le premier échec


def test_adapter_exception_fails_without_exposing_its_message():
    class CrashingSource(FakeSourceDriver):
        def execute(self, statements):
            raise RuntimeError("secret in adapter exception")

    report = make_orchestrator(("seed",), source=CrashingSource()).run(("seed",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "qualification_runtime_error"
    assert "secret in adapter exception" not in str(report.as_dict())


# --- Capture et snapshot ------------------------------------------------------

def test_run_capture_computes_bootstrap_from_tail():
    source = FakeSourceDriver(receiver="R1", last_sequence=42)
    capture = FakeCaptureRunner(scripted_events=[[]])
    orch = make_orchestrator(("capture",), source=source, capture=capture)
    orch.run(("capture",))
    assert capture.calls[0]["bootstrap"] == ("R1", 43)


def test_capture_refuses_a_failed_tail_without_starting_the_reader():
    class FailedTailSource(FakeSourceDriver):
        def tail(self):
            return SourceResult(exit_code=5)

    capture = FakeCaptureRunner()
    orch = make_orchestrator(("capture",), source=FailedTailSource(), capture=capture)
    report = orch.run(("capture",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "source_tail_failed"
    assert capture.calls == []


def test_capture_refuses_an_invalid_tail_position_without_starting_the_reader():
    class InvalidTailSource(FakeSourceDriver):
        def __init__(self, receiver, sequence):
            super().__init__()
            self.tail_receiver = receiver
            self.tail_sequence = sequence

        def tail(self):
            import json
            return SourceResult(exit_code=0, checks=(
                "SRC_TAIL=" + json.dumps({
                    "JOURNAL_RECEIVER_NAME": self.tail_receiver,
                    "LAST_SEQUENCE_NUMBER": self.tail_sequence,
                }),
            ))

    for receiver, sequence in ((None, "42"), ("R1", -1), ("R1", True)):
        capture = FakeCaptureRunner()
        orch = make_orchestrator(("capture",), source=InvalidTailSource(receiver, sequence), capture=capture)
        report = orch.run(("capture",))
        assert report.status == "FAIL"
        assert report.steps[0].details["reason"] == "source_tail_invalid"
        assert capture.calls == []


def test_run_snapshot_accumulates_events_as_snapshot():
    capture = FakeCaptureRunner(scripted_events=[[{"sequence": 1, "after": {"ORDER_ID": "1"}}]])
    orch = make_orchestrator(("snapshot",), capture=capture)
    report = orch.run(("snapshot",))
    assert report.status == "PASS"
    assert orch.events[0].is_snapshot is True
    assert capture.calls[0]["bootstrap"] == ("R1", 1)


def test_snapshot_boundary_keeps_library_time_and_exact_start_for_reader_restart():
    class BoundedTailSource(FakeSourceDriver):
        def tail(self):
            return SourceResult(
                exit_code=0,
                checks=(
                    'SRC_TAIL={"JOURNAL_RECEIVER_LIBRARY":"QUALIF_LIB",'
                    '"JOURNAL_RECEIVER_NAME":"R1","LAST_SEQUENCE_NUMBER":"100"}',
                ),
            )

    class BoundaryCapture(FakeCaptureRunner):
        def __init__(self):
            super().__init__()
            self.boundaries = []

        def run(self, *, label, max_seconds, bootstrap, env):
            self.boundaries.append(bootstrap)
            return super().run(label=label, max_seconds=max_seconds, bootstrap=bootstrap, env=env)

    capture = BoundaryCapture()
    orch = make_orchestrator(("snapshot", "capture"), source=BoundedTailSource(), capture=capture)
    assert orch._run_step("snapshot").status == "PASS"
    assert orch._run_step("capture").status == "PASS"
    assert orch._run_step("capture").status == "PASS"
    assert capture.boundaries[0] is capture.boundaries[1] is capture.boundaries[2]
    boundary = capture.boundaries[0]
    assert boundary.receiver_library == "QUALIF_LIB"
    assert boundary.receiver_name == "R1"
    assert boundary.last_sequence == 100
    assert boundary.capture_start == ("R1", 101)
    assert boundary.observed_at.tzinfo is not None


def test_snapshot_refuses_tail_without_receiver_library():
    class MissingLibrary(FakeSourceDriver):
        def tail(self):
            return SourceResult(exit_code=0, checks=(
                'SRC_TAIL={"JOURNAL_RECEIVER_NAME":"R1","LAST_SEQUENCE_NUMBER":"100"}',
            ))

    capture = FakeCaptureRunner()
    report = make_orchestrator(("snapshot",), source=MissingLibrary(), capture=capture).run(("snapshot",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "source_tail_invalid"
    assert capture.calls == []


def test_snapshot_uses_observed_row_count_without_inventing_events():
    class ReceiptedCapture(FakeCaptureRunner):
        def run(self, *, label, max_seconds, bootstrap, env):
            super().run(label=label, max_seconds=max_seconds, bootstrap=bootstrap, env=env)
            return CaptureResult(exit_code=0, events=(), log="", observed_count=3)

    capture = ReceiptedCapture()
    orch = make_orchestrator(("snapshot",), capture=capture)
    report = orch.run(("snapshot",))
    assert report.status == "PASS"
    assert report.steps[0].details["rows"] == 3
    assert orch.events == []


def test_snapshot_rejects_invalid_observed_count():
    class InvalidReceipt(FakeCaptureRunner):
        def run(self, *, label, max_seconds, bootstrap, env):
            return CaptureResult(exit_code=0, events=(), log="", observed_count=-1)

    report = make_orchestrator(("snapshot",), capture=InvalidReceipt()).run(("snapshot",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "qualification_runtime_error"


def test_first_capture_uses_snapshot_boundary_even_after_writes_and_rotation():
    source = FakeSourceDriver(receiver="R1", last_sequence=100)
    capture = FakeCaptureRunner(scripted_events=[[], [], []])
    orch = make_orchestrator(("snapshot", "capture"), source=source, capture=capture)
    assert orch._run_step("snapshot").status == "PASS"
    source.receiver = "R2"
    source.last_sequence = 3
    assert orch._run_step("capture").status == "PASS"
    assert capture.calls[1]["bootstrap"] == ("R1", 101)
    assert orch._run_step("capture").status == "PASS"
    # Le checkpoint durable prime ; la frontière reste un secours si aucun
    # checkpoint n'a été écrit (premier passage vide ou panne précoce).
    assert capture.calls[2]["bootstrap"] == ("R1", 101)


def test_capture_retry_keeps_boundary_when_first_attempt_wrote_no_checkpoint():
    class FlakyCapture(FakeCaptureRunner):
        def run(self, *, label, max_seconds, bootstrap, env):
            result = super().run(label=label, max_seconds=max_seconds,
                                 bootstrap=bootstrap, env=env)
            return replace(result, exit_code=5) if label == "capture-1" else result

    source = FakeSourceDriver(receiver="R1", last_sequence=100)
    capture = FlakyCapture(scripted_events=[[], [], []])
    orch = make_orchestrator(("snapshot", "capture"), source=source, capture=capture)
    assert orch._run_step("snapshot").status == "PASS"
    assert orch._run_step("capture").status == "FAIL"
    assert orch._run_step("capture").status == "PASS"
    assert capture.calls[1]["bootstrap"] == capture.calls[2]["bootstrap"] == ("R1", 101)


def test_snapshot_refuses_failed_tail_before_running_capture():
    class FailedTailSource(FakeSourceDriver):
        def tail(self):
            return SourceResult(exit_code=5)

    capture = FakeCaptureRunner()
    orch = make_orchestrator(("snapshot",), source=FailedTailSource(), capture=capture)
    report = orch.run(("snapshot",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "source_tail_failed"
    assert capture.calls == []


def test_run_rotate_calls_source_rotate():
    source = FakeSourceDriver()
    orch = make_orchestrator(("rotate",), source=source)
    orch.run(("rotate",))
    assert source.rotate_calls == 1


# --- Reconciliation end-to-end via fakes --------------------------------------

def test_full_scenario_reconcile_pass():
    source = FakeSourceDriver(primary_key="ORDER_ID")
    warehouse = FakeWarehouseLoader()
    orch = make_orchestrator(("seed", "reconcile"), source=source, warehouse=warehouse)

    # Étape 1 : peupler la source (via l'orchestrateur, qui peuple aussi l'oracle)
    orch._run_step("seed")

    # Construire l'état "chargé côté entrepôt" attendu : une image "après" par
    # ligne de l'oracle, en JSON (comme le chargeur produit le ferait).
    warehouse.events = [
        {"event_id": f"observed-snapshot-id-{key}",
         "receiver": f"SNAPSHOT:{orch.config.run_id}", "sequence": key, "operation": "c",
         "payload": {"after": {k: str(v) if v is not None else None for k, v in row.items()}}, "is_snapshot": True}
        for key, row in ((i, source.rows[i]) for i in source.rows)
    ]
    warehouse.raw_rows = len(warehouse.events)
    warehouse.raw_distinct = len(warehouse.events)
    warehouse.mirror_rows = list(source.rows.values())
    source.last_sequence = 0  # aucune ligne de journal à comparer dans ce test

    report = orch.run(("reconcile",))
    assert report.reconciliation is not None
    assert report.reconciliation.oracle_vs_destination.equal
    assert report.reconciliation.oracle_vs_source.equal
    assert warehouse.load_calls == [orch.config.storage.raw_prefix]


def test_reconcile_reports_loader_failure_without_echoing_adapter_message():
    class FailedLoader(FakeWarehouseLoader):
        def load(self, *, raw_prefix: str) -> None:
            raise RuntimeError("sensitive adapter message")

    orch = make_orchestrator(("reconcile",), warehouse=FailedLoader())
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "warehouse_load_failed"
    assert "sensitive adapter message" not in str(report.as_dict())


def test_reconcile_refuses_failed_source_reads():
    class FailedDumpSource(FakeSourceDriver):
        def dump(self):
            return SourceResult(exit_code=7)

    class FailedSequencesSource(FakeSourceDriver):
        def row_positions(self, starting: tuple[str, int]):
            return SourceResult(exit_code=8)

    for source, reason in (
        (FailedDumpSource(), "source_dump_failed"),
        (FailedSequencesSource(), "source_row_positions_failed"),
    ):
        report = make_orchestrator(("reconcile",), source=source).run(("reconcile",))
        assert report.status == "FAIL"
        assert report.steps[0].details["reason"] == reason


def _snapshot_boundary_reconciliation(*, receiver: str, journal_sequence: int,
                                      source_last_sequence: int,
                                      configured_bootstrap: int = 1):
    row = generated_row(1)
    source = FakeSourceDriver(rows={1: row}, receiver="R1", last_sequence=100)
    warehouse = FakeWarehouseLoader()
    capture = FakeCaptureRunner(scripted_events=[[]])
    orch = make_orchestrator(("snapshot", "reconcile"), source=source,
                             capture=capture, warehouse=warehouse)
    orch.config = replace(orch.config, bootstrap_sequence=configured_bootstrap)
    orch.oracle[1] = row
    assert orch._run_step("snapshot").status == "PASS"
    source.receiver = receiver
    source.last_sequence = source_last_sequence
    after = {key: str(value) if value is not None else None for key, value in row.items()}
    warehouse.events = [
        {"event_id": "observed-snapshot-id", "receiver": "SNAPSHOT:test", "sequence": 1, "operation": "c",
         "payload": {"after": after}, "is_snapshot": True},
        {"event_id": "observed-journal-id", "receiver": receiver, "sequence": journal_sequence, "operation": "u_after",
         "payload": {"after": after}},
    ]
    warehouse.raw_rows = warehouse.raw_distinct = 2
    warehouse.mirror_rows = [row]
    return orch


def test_reconcile_uses_snapshot_boundary_instead_of_configured_old_sequence():
    orch = _snapshot_boundary_reconciliation(receiver="R1", journal_sequence=1,
                                             source_last_sequence=1)
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.reconciliation is not None
    assert report.reconciliation.boundary.bootstrap_sequence == 101
    assert report.reconciliation.boundary.events_before_bootstrap == 1


def test_reconcile_cannot_certify_rotation_with_sequence_only_source_oracle():
    class LegacySource(FakeSourceDriver):
        def row_positions(self, starting):
            return SourceResult(exit_code=0, checks=(
                'SRC_ROWPOS={"SEQUENCE_NUMBER":"1"}',
            ))

    orch = _snapshot_boundary_reconciliation(receiver="R2", journal_sequence=1,
                                             source_last_sequence=1,
                                             configured_bootstrap=101)
    orch.source = LegacySource(rows=orch.source.rows, receiver="R2", last_sequence=1)
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "source_row_positions_invalid"


def test_reconcile_certifies_rotation_with_ordered_receiver_aware_source_oracle():
    orch = _snapshot_boundary_reconciliation(receiver="R2", journal_sequence=1,
                                             source_last_sequence=1,
                                             configured_bootstrap=101)
    report = orch.run(("reconcile",))
    assert report.status == "PASS"
    assert report.reconciliation is not None
    assert report.reconciliation.journal_continuity.receivers == ("R1", "R2")
    assert report.reconciliation.boundary.events_before_bootstrap == 0


def test_reconcile_rejects_null_receiver_in_source_oracle():
    class InvalidSource(FakeSourceDriver):
        def row_positions(self, starting):
            return SourceResult(exit_code=0, checks=(
                'SRC_RECEIVER={"JOURNAL_RECEIVER_NAME":"R1"}',
                'SRC_RECEIVER={"JOURNAL_RECEIVER_NAME":null}',
                'SRC_ROWPOS={"JOURNAL_RECEIVER_NAME":null,"SEQUENCE_NUMBER":"1"}',
            ))

    orch = _snapshot_boundary_reconciliation(receiver="R2", journal_sequence=1,
                                             source_last_sequence=1)
    orch.source = InvalidSource(rows=orch.source.rows, receiver="R2", last_sequence=1)
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "source_row_positions_invalid"


def test_reconcile_fails_when_warehouse_missing_a_key():
    source = FakeSourceDriver(primary_key="ORDER_ID")
    warehouse = FakeWarehouseLoader()
    orch = make_orchestrator(("seed", "reconcile"), source=source, warehouse=warehouse)
    orch._run_step("seed")

    rows = list(source.rows.items())[:-1]  # une ligne manquante côté entrepôt
    warehouse.events = [
        {"event_id": f"observed-snapshot-id-{key}", "receiver": "SNAPSHOT:x", "sequence": key, "operation": "c",
         "payload": {"after": {k: str(v) if v is not None else None for k, v in row.items()}}, "is_snapshot": True}
        for key, row in rows
    ]
    warehouse.raw_rows = len(warehouse.events)
    warehouse.raw_distinct = len(warehouse.events)

    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.reconciliation.oracle_vs_destination.missing_keys


# --- measure_freshness ---------------------------------------------------------

def _ready_reconciliation():
    row = generated_row(1)
    warehouse = FakeWarehouseLoader(events=[{
        "event_id": "observed-snapshot-id",
        "receiver": "SNAPSHOT:test", "sequence": 1, "operation": "c",
        "payload": {"after": row}, "is_snapshot": True,
    }], raw_rows=1, raw_distinct=1)
    warehouse.mirror_rows = [row]
    orch = make_orchestrator(("reconcile",),
                             source=FakeSourceDriver(rows={1: row}), warehouse=warehouse)
    orch.oracle[1] = row
    return orch, warehouse


@pytest.mark.parametrize("mirror", [[], [dict(generated_row(1), LABEL="wrong")],
                                  [generated_row(1), generated_row(1)],
                                  [generated_row(1), generated_row(2)]])
def test_correct_history_cannot_hide_missing_wrong_or_duplicate_mirror(mirror):
    orch, warehouse = _ready_reconciliation()
    warehouse.mirror_rows = mirror
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.reconciliation.oracle_vs_destination.equal
    assert report.reconciliation.as_dict()["mirror"] is not None


def test_reconcile_preserves_measured_divergent_replay_in_the_verdict():
    orch, warehouse = _ready_reconciliation()
    warehouse.replayed_divergent = 1
    warehouse.raw_rows = 2
    warehouse.divergent_event_ids = ("conflicting-id",)
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.reconciliation.replay.replayed_divergent == 1
    assert report.reconciliation.replay.as_dict()["divergent_event_ids"] == ["conflicting-id"]


def test_loader_failure_keeps_raw_conflict_evidence_without_a_false_zero():
    orch, warehouse = _ready_reconciliation()
    warehouse.raw_rows = 2
    warehouse.replayed_divergent = 1
    warehouse.divergent_event_ids = ("conflicting-id",)

    def fail(**kwargs):
        raise ValueError("sensitive details")

    warehouse.load = fail
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.steps[0].details["raw_replay"]["divergent_event_ids"] == ["conflicting-id"]
    assert "sensitive details" not in str(report.as_dict())


def test_matching_mirror_and_measured_identical_replay_can_pass():
    orch, warehouse = _ready_reconciliation()
    warehouse.raw_rows = 2
    warehouse.replayed_identical = 1
    warehouse.identical_event_ids = ("same-id",)
    report = orch.run(("reconcile",))
    assert report.status == "PASS"
    assert report.reconciliation.replay.replayed_identical == 1
    assert report.reconciliation.replay.as_dict()["identical_event_ids"] == ["same-id"]
    assert report.reconciliation.history.as_dict() == {
        "rows": 1, "distinct_event_ids": 1, "duplicate_rows": 0,
        "duplicate_event_ids": [], "equal": True,
    }


def test_duplicate_physical_snapshot_history_cannot_pass_even_with_a_correct_mirror():
    orch, warehouse = _ready_reconciliation()
    warehouse.events[0]["event_id"] = "observed-snapshot-id"
    warehouse.events.append(dict(warehouse.events[0]))

    report = orch.run(("reconcile",))

    assert report.status == "FAIL"
    assert report.reconciliation.oracle_vs_destination.equal
    assert report.reconciliation.oracle_vs_mirror.equal
    assert report.reconciliation.journal_continuity.duplicate_positions == 0
    assert report.reconciliation.counts["snapshot_events"] == 2
    assert report.reconciliation.counts["raw_distinct_events"] == 1
    assert report.reconciliation.as_dict()["history"] == {
        "rows": 2, "distinct_event_ids": 1, "duplicate_rows": 1,
        "duplicate_event_ids": ["observed-snapshot-id"], "equal": False,
    }


def test_inconsistent_raw_counts_cannot_pass_or_be_reported_as_zero_replays():
    orch, warehouse = _ready_reconciliation()
    warehouse.raw_rows = 2
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "raw_replay_read_failed"
    assert warehouse.load_calls == []


@pytest.mark.parametrize("counts,ids", [
    ((1, 0, 1, 0), (("id",), ())),
    ((2, 1, 1, 0), ((), ())),
    ((2, 1, 0, 1), ((), ())),
])
def test_raw_replay_evidence_requires_consistent_counts_and_observed_ids(counts, ids):
    with pytest.raises(ValueError):
        RawReplayEvidence(*counts, *ids)

def test_measure_freshness_uses_low_bound():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 23, 8, 0, 0, tzinfo=timezone.utc)
    writes = [(base, base + timedelta(seconds=1))]
    published = [base + timedelta(seconds=5)]
    summary = measure_freshness(writes, published)
    assert summary.count == 1
    assert summary.p50 == 4.0


def test_measure_freshness_skips_unpublished_writes():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 23, 8, 0, 0, tzinfo=timezone.utc)
    writes = [(base, base), (base, base)]
    published = [None, base + timedelta(seconds=2)]
    summary = measure_freshness(writes, published)
    assert summary.count == 1


def test_unmeasured_freshness_cannot_pass_a_nightly_run():
    orch = make_orchestrator(("freshness",))
    report = orch.run(("freshness",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "freshness_not_measured"
    assert report.freshness is not None and report.freshness.count == 0


def _freshness_orchestrator(monkeypatch, *, published=3, capture_exit=0):
    base = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    timing = SimpleNamespace(seconds=0.0)
    publication_count = 0
    source = FakeSourceDriver(rows={1: generated_row(1)}, primary_key="ORDER_ID")
    capture = FakeCaptureRunner(exit_code=capture_exit)
    warehouse = FakeWarehouseLoader()
    orch = make_orchestrator(("freshness",), source=source, capture=capture, warehouse=warehouse)
    orch.oracle[1] = generated_row(1)
    orch._capture_bootstrap = CaptureBoundary("QUALIF_LIB", "R1", 1, base)
    orch._last_reconciliation = SimpleNamespace(status="PASS", as_dict=lambda: {"status": "PASS"})
    def clock():
        observed = base + timedelta(seconds=timing.seconds)
        timing.seconds += 0.1
        return observed

    orch._clock = clock
    orch._monotonic = lambda: timing.seconds - 0.1
    orch._sleep = lambda duration: setattr(timing, "seconds", timing.seconds + duration)
    warehouse.fetch_mirror_value = lambda **kwargs: source.rows[1]["LABEL"]

    def publications(_storage, *, raw_prefix, schema, row_key, markers):
        nonlocal publication_count
        publication_count += 1
        assert raw_prefix == orch.config.storage.raw_prefix
        assert schema == SCHEMA and row_key == 1
        assert len(markers) == 1
        return {
            marker: PublishedProbe(
                event_id=f"event-{publication_count}", object_key=f"raw-{publication_count}",
                created_at=base + timedelta(seconds=timing.seconds - 0.1),
            )
            for marker in markers if publication_count <= published
        }

    monkeypatch.setattr("quadringent_qualification.orchestrator.find_receipted_probes", publications)
    monkeypatch.setattr(orch, "_reconcile", lambda **kwargs: SimpleNamespace(
        status="PASS", as_dict=lambda: {"status": "PASS"},
    ))
    return orch, source, capture, warehouse


def test_freshness_links_three_isolated_writes_to_raw_and_reconciles_again(monkeypatch):
    orch, source, capture, warehouse = _freshness_orchestrator(monkeypatch)
    report = orch.run(("freshness",))
    assert report.status == "PASS"
    assert report.freshness is not None and report.freshness.count == 3
    assert report.freshness.maximum == pytest.approx(0.5)
    assert [call["label"] for call in capture.calls] == ["capture-1", "capture-2", "capture-3"]
    assert warehouse.load_calls == [orch.config.storage.raw_prefix] * 3
    assert source.rows[1]["LABEL"] == orch.oracle[1]["LABEL"]
    assert source.last_sequence == 3
    assert report.steps[0].details["samples"] == 3


@pytest.mark.parametrize("published,capture_exit,reason", [
    (2, 0, "freshness_unpublished"),
    (3, 5, "freshness_capture_failed"),
])
def test_freshness_rejects_missing_proof_or_failed_capture(
    monkeypatch, published, capture_exit, reason,
):
    orch, _, _, warehouse = _freshness_orchestrator(
        monkeypatch, published=published, capture_exit=capture_exit,
    )
    report = orch.run(("freshness",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == reason
    assert len(warehouse.load_calls) == (2 if published == 2 and capture_exit == 0 else 0)


def test_freshness_cannot_keep_a_stale_reconciliation_after_source_error(monkeypatch):
    orch, source, _, _ = _freshness_orchestrator(monkeypatch)
    source.exec_fails = True
    report = orch.run(("freshness",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "freshness_write_failed"
    assert report.reconciliation is None


def test_freshness_fails_when_final_destination_differs(monkeypatch):
    orch, _, _, warehouse = _freshness_orchestrator(monkeypatch)
    monkeypatch.setattr(orch, "_reconcile", lambda **kwargs: SimpleNamespace(
        status="FAIL", as_dict=lambda: {"status": "FAIL"},
    ))
    report = orch.run(("freshness",))
    assert report.status == "FAIL"
    assert report.steps[0].details["reason"] == "freshness_destination_differs"
    assert report.freshness is not None and report.freshness.count == 3
    assert warehouse.load_calls == [orch.config.storage.raw_prefix] * 3


def test_freshness_loader_failure_keeps_the_new_raw_conflict_proof(monkeypatch):
    orch, _, _, warehouse = _freshness_orchestrator(monkeypatch)
    warehouse.raw_rows = 2
    warehouse.raw_distinct = 1
    warehouse.replayed_divergent = 1
    warehouse.divergent_event_ids = ("freshness-conflict",)

    def fail(**kwargs):
        raise ValueError("sensitive details")

    warehouse.load = fail
    report = orch.run(("freshness",))
    assert report.status == "FAIL"
    assert report.steps[0].details["raw_replay"]["divergent_event_ids"] == ["freshness-conflict"]
    assert report.reconciliation is None


def test_failed_reconciliation_does_not_keep_an_older_verdict():
    orch, warehouse = _ready_reconciliation()
    assert orch.run(("reconcile",)).status == "PASS"
    warehouse.raw_rows = 2
    report = orch.run(("reconcile",))
    assert report.status == "FAIL"
    assert report.reconciliation is None
