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
    CODE_CHECKPOINT_UNAVAILABLE,
    CODE_INVALID_PLAN,
    CODE_INVALID_RUNTIME_STATE,
    CODE_LAUNCH_FAILED,
    CODE_NEEDS_RECOVERY,
    CODE_RECEIPT_MISMATCH,
    CODE_UNSAFE_READER_ID,
    PHASE_PREPARED,
    PHASE_PREPARE_FAILED,
    PHASE_PREPARING,
    PREPARE_FORMAT_VERSION,
    PrepareRuntime,
    ReaderLaunchRequest,
    ReaderReceipt,
    RECEIPT_RUNNING,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore


FRESH = JournalCheckpoint(receiver="DEMOJRN0100", sequence=250)
CUTOVER_SEQUENCE = 99
SECRET = "super-secret-token-value"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
PREPARE_SOURCE = Path(__file__).resolve().parents[1] / "src/quadringent_control_plane/fleet_prepare_runtime.py"


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


def _table(name: str, journal_library: str, journal_name: str) -> dict[str, object]:
    return {
        "name": name,
        "row_count": 1,
        "data_size": 1,
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
        "tables": [_table(name, journal_library, journal_name) for name in MANIFEST],
    }


def make_plan(**kwargs):
    return build_fleet_plan(parse_fleet_catalog(catalog_payload(**kwargs)))


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


class FakeLauncher:
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
        self.calls: list[ReaderLaunchRequest] = []
        self.phase_at_launch: list[object] = []
        self.state_at_launch: list[dict[str, object] | None] = []

    def launch(self, request: ReaderLaunchRequest) -> object:
        if self.delay:
            time.sleep(self.delay)
        state = self.store.load() if hasattr(self.store, "load") else None
        self.phase_at_launch.append(None if state is None else state.get("phase"))
        self.state_at_launch.append(None if state is None else dict(state))
        self.calls.append(request)
        self.events.append(("launch", request))
        if self.error is not None:
            raise self.error
        receipt = ReaderReceipt(
            status=RECEIPT_RUNNING,
            intent_id=request.intent_id,
            checkpoint=request.checkpoint,
            manifest=request.manifest,
            reader_id="reader-" + request.intent_id[:12],
        )
        if self.receipt_factory is not None:
            return self.receipt_factory(request, receipt)
        if self.mutate is not None:
            return self.mutate(receipt)
        return receipt


def _runtime(tmp_path: Path, *, provider=None, launcher=None, plan=None, events=None, store=None):
    plan = make_plan() if plan is None else plan
    events = [] if events is None else events
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json") if store is None else store
    recording = inner if isinstance(inner, RecordingStore) else RecordingStore(inner, events)
    provider = FakeProvider() if provider is None else provider
    launcher = FakeLauncher(recording, events) if launcher is None else launcher
    runtime = PrepareRuntime(plan, recording, provider, launcher)
    return runtime, plan, recording, provider, launcher, events


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


def test_save_preparing_happens_before_single_launch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, plan, store, provider, launcher, _events = _runtime(tmp_path, events=events)
    outcome = runtime.prepare()
    launch_at = next(index for index, event in enumerate(events) if event[0] == "launch")
    saves_before = [event for event in events[:launch_at] if event[0] == "save"]
    saves_after = [event for event in events[launch_at:] if event[0] == "save"]
    assert saves_before[0][1] == PHASE_PREPARING
    assert PHASE_PREPARED not in [event[1] for event in saves_before]
    assert saves_after[-1][1] == PHASE_PREPARED
    assert launcher.phase_at_launch == [PHASE_PREPARING]
    preparing = launcher.state_at_launch[0]
    assert preparing is not None
    assert preparing["phase"] == PHASE_PREPARING
    assert preparing["intent_id"] == outcome.intent_id
    assert preparing["checkpoint"] == FRESH.to_dict()
    assert preparing["manifest"] == list(MANIFEST)
    assert preparing["receipt"] is None
    assert len(provider.calls) == 1
    assert len(launcher.calls) == 1
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARED
    assert persisted["receipt"]["status"] == RECEIPT_RUNNING
    assert outcome.phase == PHASE_PREPARED
    assert plan.cutover_checkpoint.sequence == CUTOVER_SEQUENCE


def test_fresh_checkpoint_is_used_and_plan_cutover_is_ignored(tmp_path: Path) -> None:
    runtime, plan, store, provider, launcher, _events = _runtime(tmp_path)
    assert plan.cutover_checkpoint != FRESH
    outcome = runtime.prepare()
    request = launcher.calls[0]
    assert request.checkpoint == FRESH
    assert request.checkpoint != plan.cutover_checkpoint
    assert outcome.checkpoint == FRESH
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["checkpoint"] == FRESH.to_dict()
    assert persisted["checkpoint"] != plan.cutover_checkpoint.to_dict()
    source = PREPARE_SOURCE.read_text(encoding="utf-8")
    assert "cutover_checkpoint" not in source


def test_one_multi_object_reader_covers_exact_thirteen_tables(tmp_path: Path) -> None:
    runtime, plan, store, _provider, launcher, _events = _runtime(tmp_path)
    outcome = runtime.prepare()
    assert TABLE_COUNT == 13
    assert len(MANIFEST) == 13
    assert len(launcher.calls) == 1
    request = launcher.calls[0]
    assert type(request) is ReaderLaunchRequest
    assert request.reader_kind == READER_KIND == "multi_object"
    assert request.reader_count == 1
    assert request.manifest == MANIFEST
    assert len(request.manifest) == 13
    group = plan.journal_groups[0]
    assert request.journal_library == group.library
    assert request.journal_name == group.name
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["reader_kind"] == "multi_object"
    assert persisted["reader_count"] == 1
    assert persisted["manifest"] == list(MANIFEST)
    assert outcome.manifest == MANIFEST
    assert len(outcome.receipt.manifest) == 13


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda receipt: replace(receipt, status="PENDING"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, intent_id="other-intent"), CODE_RECEIPT_MISMATCH),
        (
            lambda receipt: replace(receipt, checkpoint=JournalCheckpoint("OTHER", 1)),
            CODE_RECEIPT_MISMATCH,
        ),
        (lambda receipt: replace(receipt, manifest=receipt.manifest[:-1]), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, manifest=tuple(reversed(receipt.manifest))), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, reader_id=""), CODE_UNSAFE_READER_ID),
        (lambda receipt: replace(receipt, reader_id="token-secret-value"), CODE_UNSAFE_READER_ID),
        (lambda receipt: replace(receipt, reader_id="host.user.password"), CODE_UNSAFE_READER_ID),
    ],
)
def test_receipt_field_mismatch_persists_prepare_failed(tmp_path: Path, mutator, code: str) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeLauncher(store, events, mutate=mutator)
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == code
    assert captured.value.__cause__ is None
    assert len(launcher.calls) == 1
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["error_code"] == code
    assert persisted["needs_recovery"] is True
    assert persisted["receipt"] is None
    assert persisted["checkpoint"] == FRESH.to_dict()
    assert persisted["manifest"] == list(MANIFEST)


def test_crash_residual_preparing_fails_closed_without_relaunch(tmp_path: Path) -> None:
    plan = make_plan()
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    inner.save(
        {
            "format_version": PREPARE_FORMAT_VERSION,
            "environment": ENVIRONMENT,
            "phase": PHASE_PREPARING,
            "intent_id": "intentcrash01",
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
    store = RecordingStore(inner, events)
    provider = FakeProvider(error=RuntimeError(SECRET))
    launcher = FakeLauncher(store, events, error=RuntimeError(SECRET))
    runtime = PrepareRuntime(plan, store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == CODE_NEEDS_RECOVERY
    assert captured.value.__cause__ is None
    assert provider.calls == []
    assert launcher.calls == []
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["needs_recovery"] is True
    assert persisted["error_code"] == CODE_NEEDS_RECOVERY
    assert persisted["intent_id"] == "intentcrash01"
    _assert_redacted(persisted, captured.value)


def test_second_prepare_when_prepared_is_idempotent(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    runtime, _plan, store, provider, launcher, _events = _runtime(tmp_path, events=events)
    first = runtime.prepare()
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    second = runtime.prepare()
    assert second.phase == PHASE_PREPARED
    assert second.intent_id == first.intent_id
    assert second.checkpoint == first.checkpoint
    assert second.receipt == first.receipt
    assert len(provider.calls) == 1
    assert len(launcher.calls) == 1
    persisted = store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARED
    assert persisted["intent_id"] == first.intent_id


def test_concurrent_threads_single_provider_and_launch(tmp_path: Path) -> None:
    plan = make_plan()
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    events: list[tuple[str, object]] = []
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeLauncher(store, events, delay=0.05)
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            runtime = PrepareRuntime(plan, store, provider, launcher)
            results.append(runtime.prepare())
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
    assert len(launcher.calls) == 1
    assert results[0].intent_id == results[1].intent_id
    assert {result.phase for result in results} == {PHASE_PREPARED}
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARED


def test_provider_error_is_redacted_and_persisted(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider(error=RuntimeError(f"password={SECRET} aws_key={AWS_KEY}"))
    launcher = FakeLauncher(store, events)
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == CODE_CHECKPOINT_UNAVAILABLE
    _assert_redacted(inner.load(), captured.value)
    assert launcher.calls == []
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["error_code"] == CODE_CHECKPOINT_UNAVAILABLE
    assert persisted["needs_recovery"] is False
    assert persisted["receipt"] is None
    assert type(captured.value) is FleetError
    assert SECRET not in captured.value.safe_message


def test_launcher_error_is_redacted_and_persisted(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeLauncher(store, events, error=RuntimeError(f"password={SECRET} aws_key={AWS_KEY}"))
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == CODE_LAUNCH_FAILED
    _assert_redacted(inner.load(), captured.value)
    assert len(launcher.calls) == 1
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["error_code"] == CODE_LAUNCH_FAILED
    assert persisted["needs_recovery"] is True
    assert persisted["checkpoint"] == FRESH.to_dict()
    preparing_saves = [event for event in events if event[0] == "save" and event[1] == PHASE_PREPARING]
    assert preparing_saves


def test_rejects_non_dev_plan_without_provider_or_launch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeLauncher(store, events)
    runtime = PrepareRuntime(SimpleNamespace(environment="PROD"), store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == CODE_INVALID_PLAN
    assert provider.calls == []
    assert launcher.calls == []
    assert inner.load() is None


def test_provider_non_checkpoint_is_failed_closed(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider(checkpoint={"receiver": "DEMOJRN0100", "sequence": 250})
    launcher = FakeLauncher(store, events)
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == CODE_CHECKPOINT_UNAVAILABLE
    assert launcher.calls == []
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["needs_recovery"] is False


def test_prepare_failed_second_call_does_not_relaunch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeLauncher(store, events, mutate=lambda receipt: replace(receipt, status="STOPPED"))
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as first:
        runtime.prepare()
    assert first.value.code == CODE_RECEIPT_MISMATCH
    failed = inner.load()
    assert failed is not None
    assert failed["needs_recovery"] is True
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as second:
        runtime.prepare()
    assert second.value.code == CODE_NEEDS_RECOVERY
    assert len(provider.calls) == 1
    assert len(launcher.calls) == 1


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (None, CODE_LAUNCH_FAILED),
        (lambda receipt: replace(receipt, status="STOPPED"), CODE_RECEIPT_MISMATCH),
        (lambda receipt: replace(receipt, reader_id=""), CODE_UNSAFE_READER_ID),
    ],
)
def test_post_launch_failure_persists_needs_recovery_without_relaunch(
    tmp_path: Path,
    mutator,
    code: str,
) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    error = RuntimeError(SECRET) if mutator is None else None
    launcher = FakeLauncher(store, events, mutate=mutator, error=error)
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as first:
        runtime.prepare()
    assert first.value.code == code
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["needs_recovery"] is True
    assert persisted["error_code"] == code
    assert len(launcher.calls) == 1
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as second:
        runtime.prepare()
    assert second.value.code == CODE_NEEDS_RECOVERY
    assert len(provider.calls) == 1
    assert len(launcher.calls) == 1
    assert inner.load()["needs_recovery"] is True


def test_provider_failure_before_launch_keeps_needs_recovery_false(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider(error=RuntimeError(SECRET))
    launcher = FakeLauncher(store, events)
    runtime = PrepareRuntime(make_plan(), store, provider, launcher)
    with pytest.raises(FleetError) as first:
        runtime.prepare()
    assert first.value.code == CODE_CHECKPOINT_UNAVAILABLE
    persisted = inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    assert persisted["needs_recovery"] is False
    assert launcher.calls == []
    with pytest.raises(FleetError) as second:
        runtime.prepare()
    assert second.value.code == CODE_CHECKPOINT_UNAVAILABLE
    assert len(provider.calls) == 1
    assert launcher.calls == []
    assert inner.load()["needs_recovery"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("journal_library", "OTHERLIB"),
        ("journal_name", "OTHERJRN"),
        ("reader_kind", "single_object"),
        ("reader_count", 13),
    ],
)
def test_idempotent_prepared_rejects_journal_group_mismatch(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    events: list[tuple[str, object]] = []
    runtime, plan, store, provider, launcher, _events = _runtime(tmp_path, events=events)
    first = runtime.prepare()
    assert first.phase == PHASE_PREPARED
    persisted = store.inner.load()
    assert persisted is not None
    group = plan.journal_groups[0]
    assert persisted["journal_library"] == group.library
    assert persisted["journal_name"] == group.name
    assert persisted["reader_kind"] == group.reader_kind
    assert persisted["reader_count"] == group.reader_count
    persisted[field] = value
    store.inner.save(persisted)
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        runtime.prepare()
    assert captured.value.code == CODE_INVALID_RUNTIME_STATE
    failed = store.inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_PREPARE_FAILED
    assert failed["needs_recovery"] is True
    assert failed["error_code"] == CODE_INVALID_RUNTIME_STATE
    assert len(provider.calls) == 1
    assert len(launcher.calls) == 1


def test_idempotent_prepared_rejects_current_plan_journal_group_mismatch(tmp_path: Path) -> None:
    events: list[tuple[str, object]] = []
    plan = make_plan(journal_library="JRNLIB1", journal_name="DEMOJRN")
    inner = AtomicJsonStateStore(tmp_path / "prepare-state.json")
    store = RecordingStore(inner, events)
    provider = FakeProvider()
    launcher = FakeLauncher(store, events)
    first = PrepareRuntime(plan, store, provider, launcher).prepare()
    assert first.phase == PHASE_PREPARED
    other = make_plan(journal_library="JRNLIB2", journal_name="APPJRN")
    assert other.journal_groups[0].library != plan.journal_groups[0].library
    assert other.journal_groups[0].name != plan.journal_groups[0].name
    provider.error = RuntimeError(SECRET)
    launcher.error = RuntimeError(SECRET)
    with pytest.raises(FleetError) as captured:
        PrepareRuntime(other, store, provider, launcher).prepare()
    assert captured.value.code == CODE_INVALID_RUNTIME_STATE
    failed = inner.load()
    assert failed is not None
    assert failed["phase"] == PHASE_PREPARE_FAILED
    assert failed["needs_recovery"] is True
    assert len(provider.calls) == 1
    assert len(launcher.calls) == 1


def test_runtime_source_has_no_cloud_clients(tmp_path: Path) -> None:
    source = PREPARE_SOURCE.read_text(encoding="utf-8").lower()
    for token in ("boto", "kubernetes", "snowflake", "eks", "iam", "s3://"):
        assert token not in source
    runtime, _plan, _store, _provider, _launcher, _events = _runtime(tmp_path)
    runtime.prepare()
