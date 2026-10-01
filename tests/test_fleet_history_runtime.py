from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import threading
import time
from types import SimpleNamespace

import pytest

from quadringent_control_plane.fleet import (
    ENVIRONMENT,
    MANIFEST,
    MAX_CONCURRENCY,
    MIN_CONCURRENCY,
    TABLE_COUNT,
    FleetError,
    JournalCheckpoint,
)
from quadringent_control_plane.fleet_plan import (
    CATALOG_FORMAT_VERSION,
    READER_KIND,
    SOURCE_SCHEMA,
    build_fleet_plan,
    parse_fleet_catalog,
)
from quadringent_control_plane.fleet_prepare_runtime import (
    PHASE_PREPARED,
    PHASE_PREPARING,
    PREPARE_FORMAT_VERSION,
    PrepareRuntime,
    ReaderLaunchRequest,
    ReaderReceipt,
    RECEIPT_RUNNING as PREPARE_RECEIPT_RUNNING,
)
from quadringent_control_plane.fleet_history_runtime import (
    CODE_HISTORY_FAILED,
    CODE_INVALID_CONCURRENCY,
    CODE_INVALID_PLAN,
    CODE_INVALID_RUNTIME_STATE,
    CODE_LAUNCH_FAILED,
    CODE_NEEDS_RECOVERY,
    CODE_NOT_PREPARED,
    CODE_RECEIPT_MISMATCH,
    CODE_UNSAFE_ORCHESTRATOR_ID,
    CODE_UNSAFE_READER_ID,
    HISTORY_FORMAT_VERSION,
    HistoryLaunchRequest,
    HistoryReceipt,
    HistoryRuntime,
    PHASE_HISTORICAL,
    PHASE_HISTORY_FAILED,
    PHASE_STARTING,
    RECEIPT_RUNNING,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore


FRESH = JournalCheckpoint(receiver="DEMOJRN0100", sequence=250)
CUTOVER_SEQUENCE = 99
SECRET = "super-secret-token-value"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
HISTORY_SOURCE = Path(__file__).resolve().parents[1] / "src/quadringent_control_plane/fleet_history_runtime.py"


def _column(name: str = "COL1", ordinal: int = 1) -> dict[str, object]:
    return {
        "name": name,
        "type": "CHAR",
        "length": 10,
        "numeric_precision": None,
        "numeric_scale": None,
        "ccsid": None,
        "nullable": False,
        "ordinal": ordinal,
    }


def _table(name: str, journal_library: str, journal_name: str, *, data_size: int = 1) -> dict[str, object]:
    return {
        "name": name,
        "row_count": 1,
        "data_size": data_size,
        "member_count": 1,
        "journal_library": journal_library,
        "journal_name": journal_name,
        "journal_images": "*AFTER",
        "columns": [_column()],
        "constraints": [],
        "indexes": [],
    }


def catalog_payload(
    *,
    journal_library: str = "JRNLIB1",
    journal_name: str = "DEMOJRN",
    attached_name: str = "DEMOJRN0100",
    attached_tail: int = CUTOVER_SEQUENCE,
    data_size: int = 1,
) -> dict[str, object]:
    return {
        "format_version": CATALOG_FORMAT_VERSION,
        "observed_at": "2026-09-13T12:00:00Z",
        "environment": ENVIRONMENT,
        "source_schema": SOURCE_SCHEMA,
        "journals": [
            {
                "library": journal_library,
                "name": journal_name,
                "continuity": "uncertain",
                "receivers": [
                    {
                        "library": journal_library,
                        "name": "DEMOJRN0099",
                        "status": "ONLINE",
                        "first_sequence": "1",
                        "last_sequence": "50",
                        "attach_timestamp": "2026-09-13T00:00:00.000000",
                        "detach_timestamp": "2026-09-13T01:00:00.000000",
                        "previous_library": None,
                        "previous_name": None,
                    },
                    {
                        "library": journal_library,
                        "name": attached_name,
                        "status": "ATTACHED",
                        "first_sequence": "1",
                        "last_sequence": str(attached_tail),
                        "attach_timestamp": "2026-09-13T01:00:00.000000",
                        "detach_timestamp": None,
                        "previous_library": None,
                        "previous_name": None,
                    },
                ],
            }
        ],
        "tables": [_table(name, journal_library, journal_name, data_size=data_size) for name in MANIFEST],
    }


def make_plan(**kwargs):
    catalog_kwargs = {
        key: kwargs.pop(key)
        for key in ("journal_library", "journal_name", "attached_name", "attached_tail", "data_size")
        if key in kwargs
    }
    return build_fleet_plan(parse_fleet_catalog(catalog_payload(**catalog_kwargs)), **kwargs)


class RecordingStore:
    def __init__(self, inner: AtomicJsonStateStore, events: list[tuple[str, object]]) -> None:
        self.inner = inner
        self.events = events

    def load(self) -> dict[str, object] | None:
        payload = self.inner.load()
        self.events.append(("load", None if payload is None else payload.get("phase")))
        return payload

    def save(self, mapping: dict[str, object]) -> None:
        self.events.append(("save", mapping["phase"], dict(mapping)))
        self.inner.save(mapping)


class FakeProvider:
    def __init__(self, checkpoint: object = FRESH, *, error: BaseException | None = None) -> None:
        self.checkpoint = checkpoint
        self.error = error
        self.calls: list[object] = []

    def current(self, plan: object) -> object:
        self.calls.append(plan)
        if self.error is not None:
            raise self.error
        return self.checkpoint


class FakeReaderLauncher:
    def __init__(self, store: RecordingStore | AtomicJsonStateStore, events: list[tuple[str, object]]) -> None:
        self.store = store
        self.events = events
        self.calls: list[ReaderLaunchRequest] = []

    def launch(self, request: ReaderLaunchRequest) -> object:
        self.calls.append(request)
        self.events.append(("prepare-launch", request))
        return ReaderReceipt(
            status=PREPARE_RECEIPT_RUNNING,
            intent_id=request.intent_id,
            checkpoint=request.checkpoint,
            manifest=request.manifest,
            reader_id="reader-" + request.intent_id[:12],
        )


class FakeHistoryLauncher:
    def __init__(
        self,
        store: RecordingStore | AtomicJsonStateStore,
        events: list[tuple[str, object]],
        *,
        mutate=None,
        error: BaseException | None = None,
        delay: float = 0.0,
        receipt_factory=None,
    ) -> None:
        self.store = store
        self.events = events
        self.mutate = mutate
        self.error = error
        self.delay = delay
        self.receipt_factory = receipt_factory
        self.calls: list[HistoryLaunchRequest] = []
        self.phase_at_launch: list[object] = []
        self.state_at_launch: list[dict[str, object] | None] = []

    def launch(self, request: HistoryLaunchRequest) -> object:
        if self.delay:
            time.sleep(self.delay)
        state = self.store.load() if hasattr(self.store, "load") else None
        self.phase_at_launch.append(None if state is None else state.get("phase"))
        self.state_at_launch.append(None if state is None else dict(state))
        self.calls.append(request)
        self.events.append(("launch", request))
        if self.error is not None:
            raise self.error
        receipt = HistoryReceipt(
            status=RECEIPT_RUNNING,
            intent_id=request.intent_id,
            prepare_intent_id=request.prepare_intent_id,
            reader_id=request.reader_id,
            checkpoint=request.checkpoint,
            manifest=request.manifest,
            lanes=request.lanes,
            max_concurrency=request.max_concurrency,
            orchestrator_id="orch-" + request.intent_id[:12],
        )
        if self.receipt_factory is not None:
            return self.receipt_factory(request, receipt)
        if self.mutate is not None:
            return self.mutate(receipt)
        return receipt


def _assert_redacted(payload: object, error: BaseException | None = None) -> None:
    encoded = json.dumps(payload, default=str)
    assert SECRET not in encoded
    assert AWS_KEY not in encoded
    assert "password" not in encoded.lower()
    if error is not None:
        message = str(error)
        assert SECRET not in message
        assert AWS_KEY not in message
        assert error.__cause__ is None
        assert error.__context__ is None


def _prepare(tmp_path: Path, *, plan=None, events=None):
    plan = make_plan() if plan is None else plan
    events = [] if events is None else events
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeReaderLauncher(store, events)
    outcome = PrepareRuntime(plan, store, provider, launcher).prepare()
    return plan, store, provider, launcher, outcome, events


def _history_runtime(
    tmp_path: Path,
    *,
    plan=None,
    events=None,
    launcher=None,
    mutate=None,
    error=None,
    delay: float = 0.0,
):
    events = [] if events is None else events
    plan, prepare_store, provider, reader_launcher, prepared, events = _prepare(
        tmp_path, plan=plan, events=events
    )
    inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    store = RecordingStore(inner, events)
    launcher = (
        FakeHistoryLauncher(store, events, mutate=mutate, error=error, delay=delay)
        if launcher is None
        else launcher
    )
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    return runtime, plan, prepare_store, store, provider, reader_launcher, launcher, prepared, events


def _lane_tables(plan) -> list[str]:
    return [name for lane in plan.historical_lanes for name in lane.tables]


def test_save_starting_happens_before_single_launch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, plan, _prepare_store, store, provider, reader_launcher, launcher, prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    outcome = runtime.start()
    launch_at = next(index for index, event in enumerate(events) if event[0] == "launch")
    saves_before = [event for event in events[:launch_at] if event[0] == "save"]
    saves_after = [event for event in events[launch_at:] if event[0] == "save"]
    starting_saves = [event for event in saves_before if event[1] == PHASE_STARTING]
    assert starting_saves
    assert PHASE_HISTORICAL not in [event[1] for event in saves_before]
    assert saves_after[-1][1] == PHASE_HISTORICAL
    assert launcher.phase_at_launch == [PHASE_STARTING]
    starting = launcher.state_at_launch[0]
    assert starting is not None
    assert starting["phase"] == PHASE_STARTING
    assert starting["intent_id"] == outcome.intent_id
    assert starting["prepare_intent_id"] == prepared.intent_id
    assert starting["reader_id"] == prepared.receipt.reader_id
    assert starting["checkpoint"] == FRESH.to_dict()
    assert starting["manifest"] == list(MANIFEST)
    assert starting["lanes"] == [lane.to_dict() for lane in plan.historical_lanes]
    assert starting["max_concurrency"] == plan.max_concurrency
    assert starting["receipt"] is None
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORICAL
    assert persisted["receipt"]["status"] == RECEIPT_RUNNING
    assert outcome.phase == PHASE_HISTORICAL
    assert plan.cutover_checkpoint.sequence == CUTOVER_SEQUENCE


def test_one_orchestrator_covers_exact_thirteen_tables(tmp_path: Path) -> None:
    runtime, plan, _prepare_store, store, _provider, reader_launcher, launcher, prepared, _events = _history_runtime(
        tmp_path
    )
    outcome = runtime.start()
    assert TABLE_COUNT == 13
    assert len(MANIFEST) == 13
    assert MIN_CONCURRENCY <= plan.max_concurrency <= MAX_CONCURRENCY
    assigned = _lane_tables(plan)
    assert len(assigned) == 13
    assert len(set(assigned)) == 13
    assert set(assigned) == set(MANIFEST)
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    request = launcher.calls[0]
    assert type(request) is HistoryLaunchRequest
    assert request.manifest == MANIFEST
    assert request.lanes == plan.historical_lanes
    assert request.max_concurrency == plan.max_concurrency
    assert request.prepare_intent_id == prepared.intent_id
    assert request.reader_id == prepared.receipt.reader_id
    assert request.checkpoint == FRESH
    request_tables = [name for lane in request.lanes for name in lane.tables]
    assert request_tables == assigned
    persisted = store.inner.load()
    assert persisted is not None
    persisted_tables = [name for lane in persisted["lanes"] for name in lane["tables"]]
    assert persisted_tables == assigned
    assert outcome.manifest == MANIFEST
    assert len(outcome.receipt.manifest) == 13
    assert outcome.lanes == plan.historical_lanes


def test_prepared_checkpoint_and_reader_are_reused(tmp_path: Path) -> None:
    runtime, plan, prepare_store, store, provider, reader_launcher, launcher, prepared, _events = _history_runtime(
        tmp_path
    )
    outcome = runtime.start()
    assert outcome.checkpoint == FRESH == prepared.checkpoint
    assert outcome.checkpoint != plan.cutover_checkpoint
    assert outcome.reader_id == prepared.receipt.reader_id
    assert outcome.prepare_intent_id == prepared.intent_id
    assert launcher.calls[0].checkpoint == prepared.checkpoint
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    prepare_state = prepare_store.inner.load()
    assert prepare_state is not None
    assert prepare_state["phase"] == PHASE_PREPARED
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["checkpoint"] == FRESH.to_dict()
    assert persisted["reader_id"] == prepared.receipt.reader_id


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda receipt: replace(receipt, status="PENDING"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, intent_id="other-intent"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, prepare_intent_id="other-prepare"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, reader_id="other-reader"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, reader_id=""), CODE_UNSAFE_READER_ID),
        (lambda receipt: replace(receipt, reader_id="token-secret-value"), CODE_UNSAFE_READER_ID),
        (lambda receipt: replace(receipt, reader_id="host.user.password"), CODE_UNSAFE_READER_ID),
        (lambda receipt: replace(receipt, reader_id="reader id"), CODE_UNSAFE_READER_ID),
        (
            lambda receipt: replace(receipt, checkpoint=JournalCheckpoint("OTHER", 1)),
            CODE_RECEIPT_MISMATCH,
        ),
        (lambda receipt: replace(receipt, manifest=receipt.manifest[:-1]), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, manifest=tuple(reversed(receipt.manifest))), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, lanes=receipt.lanes[:-1]), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, max_concurrency=1 if receipt.max_concurrency != 1 else 2), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, orchestrator_id=""), CODE_UNSAFE_ORCHESTRATOR_ID),
        (lambda receipt: replace(receipt, orchestrator_id="token-secret-value"), CODE_UNSAFE_ORCHESTRATOR_ID),
        (lambda receipt: replace(receipt, orchestrator_id="host.user.password"), CODE_UNSAFE_ORCHESTRATOR_ID),
    ],
)
def test_receipt_field_mismatch_persists_history_failed(tmp_path: Path, mutator, code: str) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, _prepare_store, store, _provider, reader_launcher, launcher, prepared, _events = _history_runtime(
        tmp_path, events=events, mutate=mutator
    )
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == code
    assert captured.value.__cause__ is None
    assert len(launcher.calls) == 1
    assert len(reader_launcher.calls) == 1
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["error_code"] == code
    assert persisted["needs_recovery"] is True
    assert persisted["receipt"] is None
    assert persisted["checkpoint"] == FRESH.to_dict()
    assert persisted["prepare_intent_id"] == prepared.intent_id
    assert persisted["manifest"] == list(MANIFEST)
    _assert_redacted(persisted, captured.value)
    assert "token-secret-value" not in json.dumps(persisted)
    assert "host.user.password" not in json.dumps(persisted)


def test_crash_residual_starting_fails_closed_without_relaunch(tmp_path: Path) -> None:
    plan, prepare_store, provider, reader_launcher, prepared, events = _prepare(tmp_path)
    inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    inner.save(
        {
            "format_version": HISTORY_FORMAT_VERSION,
            "environment": ENVIRONMENT,
            "phase": PHASE_STARTING,
            "intent_id": "intentcrash01",
            "prepare_intent_id": prepared.intent_id,
            "reader_id": prepared.receipt.reader_id,
            "checkpoint": FRESH.to_dict(),
            "manifest": list(MANIFEST),
            "lanes": [lane.to_dict() for lane in plan.historical_lanes],
            "max_concurrency": plan.max_concurrency,
            "receipt": None,
            "error_code": None,
            "needs_recovery": False,
        }
    )
    store = RecordingStore(inner, events)
    launcher = FakeHistoryLauncher(store, events, error=RuntimeError(SECRET))
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_NEEDS_RECOVERY
    assert captured.value.__cause__ is None
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert launcher.calls == []
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["needs_recovery"] is True
    assert persisted["error_code"] == CODE_NEEDS_RECOVERY
    assert persisted["intent_id"] == "intentcrash01"
    _assert_redacted(persisted, captured.value)


def test_second_start_when_historical_is_idempotent(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, _prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    launcher.error = RuntimeError(SECRET)
    provider.error = RuntimeError(SECRET)
    second = runtime.start()
    assert second.phase == PHASE_HISTORICAL
    assert second.intent_id == first.intent_id
    assert second.checkpoint == first.checkpoint
    assert second.receipt == first.receipt
    assert second.prepare_intent_id == first.prepare_intent_id
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORICAL
    assert persisted["intent_id"] == first.intent_id


def test_concurrent_threads_single_launch(tmp_path: Path) -> None:
    plan, prepare_store, provider, reader_launcher, _prepared, events = _prepare(tmp_path)
    inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    store = RecordingStore(inner, events)
    launcher = FakeHistoryLauncher(store, events, delay=0.05)
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            runtime = HistoryRuntime(plan, prepare_store, store, launcher)
            results.append(runtime.start())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert len(results) == 2
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    assert results[0].intent_id == results[1].intent_id
    assert {result.phase for result in results} == {PHASE_HISTORICAL}
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORICAL


def test_launcher_error_is_redacted_and_persisted(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, _prepare_store, store, _provider, _reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events, error=RuntimeError(f"password={SECRET} aws_key={AWS_KEY}")
    )
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_LAUNCH_FAILED
    _assert_redacted(store.inner.load(), captured.value)
    assert len(launcher.calls) == 1
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["error_code"] == CODE_LAUNCH_FAILED
    assert persisted["needs_recovery"] is True
    assert persisted["checkpoint"] == FRESH.to_dict()
    starting_saves = [event for event in events if event[0] == "save" and event[1] == PHASE_STARTING]
    assert starting_saves


def test_missing_prepare_fails_closed_without_launch(tmp_path: Path) -> None:
    plan = make_plan()
    prepare_inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    history_inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    events: list[tuple[str, object]] = []
    prepare_store = RecordingStore(prepare_inner, events)
    store = RecordingStore(history_inner, events)
    launcher = FakeHistoryLauncher(store, events)
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_NOT_PREPARED
    assert launcher.calls == []
    assert prepare_inner.load() is None
    persisted = history_inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["error_code"] == CODE_NOT_PREPARED
    assert persisted["needs_recovery"] is False
    assert persisted["receipt"] is None
    _assert_redacted(persisted, captured.value)


def test_preparing_residual_is_not_prepared(tmp_path: Path) -> None:
    plan = make_plan()
    prepare_inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    prepare_inner.save(
        {
            "format_version": PREPARE_FORMAT_VERSION,
            "environment": ENVIRONMENT,
            "phase": PHASE_PREPARING,
            "intent_id": "preparecrash01",
            "manifest": list(MANIFEST),
            "checkpoint": FRESH.to_dict(),
            "journal_library": plan.journal_groups[0].library,
            "journal_name": plan.journal_groups[0].name,
            "reader_kind": READER_KIND,
            "reader_count": 1,
            "receipt": None,
            "error_code": None,
            "needs_recovery": False,
        }
    )
    events: list[tuple[str, object]] = []
    prepare_store = RecordingStore(prepare_inner, events)
    history_inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    store = RecordingStore(history_inner, events)
    launcher = FakeHistoryLauncher(store, events)
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_NOT_PREPARED
    assert launcher.calls == []
    prepare_state = prepare_inner.load()
    assert prepare_state is not None
    assert prepare_state["phase"] == PHASE_PREPARING
    persisted = history_inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["needs_recovery"] is False


def test_rejects_non_dev_plan_without_launch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    prepare_inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    history_inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    prepare_store = RecordingStore(prepare_inner, events)
    store = RecordingStore(history_inner, events)
    launcher = FakeHistoryLauncher(store, events)
    runtime = HistoryRuntime(SimpleNamespace(environment="PROD"), prepare_store, store, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_INVALID_PLAN
    assert launcher.calls == []
    assert history_inner.load() is None
    assert prepare_inner.load() is None


def test_partial_lanes_are_rejected_before_launch(tmp_path: Path) -> None:
    plan = make_plan(historical_byte_budget=1)
    assigned = _lane_tables(plan)
    assert assigned != list(MANIFEST)
    assert set(assigned) != set(MANIFEST)
    _plan, prepare_store, provider, reader_launcher, _prepared, events = _prepare(tmp_path, plan=plan)
    inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    store = RecordingStore(inner, events)
    launcher = FakeHistoryLauncher(store, events)
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_INVALID_PLAN
    assert launcher.calls == []
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["error_code"] == CODE_INVALID_PLAN
    assert persisted["needs_recovery"] is False


def test_history_failed_second_call_does_not_relaunch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, _prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events, mutate=lambda receipt: replace(receipt, status="STOPPED")
    )
    with pytest.raises(FleetError) as first:
        runtime.start()
    assert first.value.code == CODE_RECEIPT_MISMATCH
    failed = store.inner.load()
    assert failed is not None
    assert failed["needs_recovery"] is True
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as second:
        runtime.start()
    assert second.value.code == CODE_NEEDS_RECOVERY
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (None, CODE_LAUNCH_FAILED),
        (lambda receipt: replace(receipt, status="STOPPED"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, orchestrator_id=""), CODE_UNSAFE_ORCHESTRATOR_ID),
    ],
)
def test_post_launch_failure_persists_needs_recovery_without_relaunch(
    tmp_path: Path,
    mutator,
    code: str,
) -> None:
    events: list[tuple[str, object]] = []
    error = RuntimeError(SECRET) if mutator is None else None
    runtime, _plan, _prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events, mutate=mutator, error=error
    )
    with pytest.raises(FleetError) as first:
        runtime.start()
    assert first.value.code == code
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["needs_recovery"] is True
    assert persisted["error_code"] == code
    assert len(launcher.calls) == 1
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as second:
        runtime.start()
    assert second.value.code == CODE_NEEDS_RECOVERY
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    assert store.inner.load()["needs_recovery"] is True


def test_precondition_failure_keeps_needs_recovery_false(tmp_path: Path) -> None:
    plan = make_plan()
    prepare_inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    history_inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    events: list[tuple[str, object]] = []
    prepare_store = RecordingStore(prepare_inner, events)
    store = RecordingStore(history_inner, events)
    launcher = FakeHistoryLauncher(store, events)
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    with pytest.raises(FleetError) as first:
        runtime.start()
    assert first.value.code == CODE_NOT_PREPARED
    persisted = history_inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["needs_recovery"] is False
    assert launcher.calls == []
    with pytest.raises(FleetError) as second:
        runtime.start()
    assert second.value.code == CODE_NOT_PREPARED
    assert launcher.calls == []
    assert history_inner.load()["needs_recovery"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reader_id", "other-reader"),
        ("checkpoint", {"receiver": "OTHER", "sequence": 1}),
        ("lanes", []),
        ("max_concurrency", 99),
        ("max_concurrency", 0),
    ],
)
def test_idempotent_historical_rejects_reader_checkpoint_lanes_concurrency_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    events: list[tuple[str, object]] = []
    runtime, plan, _prepare_store, store, provider, reader_launcher, launcher, prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    assert first.phase == PHASE_HISTORICAL
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["reader_id"] == prepared.receipt.reader_id
    assert persisted["checkpoint"] == FRESH.to_dict()
    assert persisted["lanes"] == [lane.to_dict() for lane in plan.historical_lanes]
    assert persisted["max_concurrency"] == plan.max_concurrency
    persisted[field] = value
    store.inner.save(persisted)
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code in {
        CODE_INVALID_RUNTIME_STATE,
        CODE_RECEIPT_MISMATCH,
        CODE_INVALID_CONCURRENCY,
        CODE_INVALID_PLAN,
    }
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_HISTORY_FAILED
    assert failed["needs_recovery"] is True
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1


@pytest.mark.parametrize(
    "mutator",
    [
        lambda state: {**state, "checkpoint": {"receiver": "OTHER", "sequence": 1}},
        lambda state: {
            **state,
            "receipt": {**state["receipt"], "reader_id": "token-secret-value"},
        },
        lambda state: {**state, "journal_library": "OTHERLIB"},
        lambda state: {**state, "reader_kind": "single_object"},
        lambda state: {
            **state,
            "manifest": list(reversed(state["manifest"])),
            "receipt": {**state["receipt"], "manifest": list(reversed(state["receipt"]["manifest"]))},
        },
    ],
)
def test_altered_prepared_reader_checkpoint_manifest_is_rejected(
    tmp_path: Path,
    mutator,
) -> None:
    events: list[tuple[str, object]] = []
    plan, prepare_store, provider, reader_launcher, _prepared, events = _prepare(tmp_path, events=events)
    prepared_state = prepare_store.inner.load()
    assert prepared_state is not None
    prepare_store.inner.save(mutator(prepared_state))
    inner = AtomicJsonStateStore(tmp_path / "history-state.json")
    store = RecordingStore(inner, events)
    launcher = FakeHistoryLauncher(store, events)
    runtime = HistoryRuntime(plan, prepare_store, store, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code in {
        CODE_NOT_PREPARED,
        CODE_RECEIPT_MISMATCH,
        CODE_INVALID_RUNTIME_STATE,
        CODE_UNSAFE_READER_ID,
    }
    assert launcher.calls == []
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_HISTORY_FAILED
    assert persisted["needs_recovery"] is False
    _assert_redacted(persisted, captured.value)


@pytest.mark.parametrize(
    "unsafe_reader_id",
    ["", "token-secret-value", "host.user.password", "reader id", "reader@id"],
)
def test_falsified_historical_reader_id_fails_closed_without_relaunch(
    tmp_path: Path,
    unsafe_reader_id: str,
) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    assert first.phase == PHASE_HISTORICAL
    persisted = store.inner.load()
    assert persisted is not None
    persisted["reader_id"] = unsafe_reader_id
    persisted["receipt"] = {**persisted["receipt"], "reader_id": unsafe_reader_id}
    store.inner.save(persisted)
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_UNSAFE_READER_ID
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_HISTORY_FAILED
    assert failed["needs_recovery"] is True
    assert failed["error_code"] == CODE_UNSAFE_READER_ID
    assert failed["receipt"] is None
    assert failed.get("reader_id") != unsafe_reader_id
    if unsafe_reader_id:
        assert unsafe_reader_id not in json.dumps(failed)
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    prepare_state = prepare_store.inner.load()
    assert prepare_state is not None
    assert prepare_state["phase"] == PHASE_PREPARED
    _assert_redacted(failed, captured.value)


def test_idempotent_historical_rejects_missing_prepared_without_relaunch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    assert first.phase == PHASE_HISTORICAL
    prepare_store.inner._path.unlink()
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_NOT_PREPARED
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_HISTORY_FAILED
    assert failed["needs_recovery"] is True
    assert failed["error_code"] == CODE_NOT_PREPARED
    assert failed["intent_id"] == first.intent_id
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    _assert_redacted(failed, captured.value)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda state: {
            **state,
            "intent_id": "otherprepare01",
            "receipt": {**state["receipt"], "intent_id": "otherprepare01"},
        },
        lambda state: {
            **state,
            "receipt": {**state["receipt"], "reader_id": "reader-other"},
        },
        lambda state: {
            **state,
            "checkpoint": {"receiver": "OTHER", "sequence": 1},
            "receipt": {
                **state["receipt"],
                "checkpoint": {"receiver": "OTHER", "sequence": 1},
            },
        },
        lambda state: {**state, "phase": PHASE_PREPARING, "receipt": None},
    ],
)
def test_idempotent_historical_rejects_prepared_divergence_without_relaunch(
    tmp_path: Path,
    mutator,
) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, prepare_store, store, provider, reader_launcher, launcher, prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    assert first.phase == PHASE_HISTORICAL
    prepare_state = prepare_store.inner.load()
    assert prepare_state is not None
    prepare_store.inner.save(mutator(prepare_state))
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code in {
        CODE_NOT_PREPARED,
        CODE_RECEIPT_MISMATCH,
        CODE_INVALID_RUNTIME_STATE,
        CODE_UNSAFE_READER_ID,
    }
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_HISTORY_FAILED
    assert failed["needs_recovery"] is True
    assert failed["intent_id"] == first.intent_id
    assert failed["prepare_intent_id"] == prepared.intent_id
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1
    _assert_redacted(failed, captured.value)


def test_idempotent_historical_aligned_reader_id_still_requires_current_prepared(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, _prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    persisted = store.inner.load()
    assert persisted is not None
    persisted["reader_id"] = "reader-other"
    persisted["receipt"] = {**persisted["receipt"], "reader_id": "reader-other"}
    store.inner.save(persisted)
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_INVALID_RUNTIME_STATE
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_HISTORY_FAILED
    assert failed["needs_recovery"] is True
    assert len(launcher.calls) == 1
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    _assert_redacted(failed, captured.value)


def test_current_plan_lane_mismatch_on_idempotent_start(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, plan, prepare_store, store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events
    )
    first = runtime.start()
    assert first.phase == PHASE_HISTORICAL
    other = make_plan(max_concurrency=1)
    assert other.historical_lanes != plan.historical_lanes
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        HistoryRuntime(other, prepare_store, store, launcher).start()
    assert captured.value.code == CODE_INVALID_RUNTIME_STATE
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_HISTORY_FAILED
    assert failed["needs_recovery"] is True
    assert len(provider.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(launcher.calls) == 1


def test_runtime_source_has_no_cloud_clients_and_reuses_prepare(tmp_path: Path) -> None:
    source = HISTORY_SOURCE.read_text(encoding="utf-8")
    lowered = source.lower()
    for token in ("boto", "kubernetes", "snowflake", "eks", "iam", "s3://"):
        assert token not in lowered
    assert "PrepareRuntime" in source
    assert "_SentinelCheckpointProvider" in source
    assert "_SentinelReaderLauncher" in source
    runtime, _plan, _prepare_store, _store, provider, reader_launcher, launcher, _prepared, _events = _history_runtime(
        tmp_path
    )
    runtime.start()
    assert len(launcher.calls) == 1
    assert len(reader_launcher.calls) == 1
    assert len(provider.calls) == 1


def test_history_failed_code_is_safe(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, _prepare_store, store, _provider, _reader_launcher, _launcher, _prepared, _events = _history_runtime(
        tmp_path, events=events, error=RuntimeError(SECRET)
    )
    with pytest.raises(FleetError) as captured:
        runtime.start()
    assert captured.value.code == CODE_LAUNCH_FAILED
    assert CODE_HISTORY_FAILED in HISTORY_SOURCE.read_text(encoding="utf-8")
    persisted = store.inner.load()
    assert persisted is not None
    assert "password" not in json.dumps(persisted).lower()
