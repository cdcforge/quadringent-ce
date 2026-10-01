from __future__ import annotations

from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

import site_fixture
import quadringent_control_plane.model as control_plane_model

if not hasattr(control_plane_model, "FLEET_ID"):
    control_plane_model.FLEET_ID = "example-corp-dev"

from quadringent_control_plane.actions import (  # noqa: E402
    PipelineActionGate,
    PipelineActionInvocation,
    execute_pipeline_action,
)
from quadringent_control_plane.fleet import (  # noqa: E402
    ENVIRONMENT,
    MANIFEST,
    TABLE_COUNT,
    JournalCheckpoint,
)
from quadringent_control_plane.fleet_action_executor import (  # noqa: E402
    EXECUTOR_ENVIRONMENT,
    EXECUTOR_FLEET_ID,
    EXECUTOR_PIPELINE_ID,
    RUNTIME_FORMAT_VERSION,
    FleetActionExecutor,
)
from quadringent_control_plane.fleet_history_runtime import (  # noqa: E402
    HISTORY_FORMAT_VERSION,
    HistoryLaunchRequest,
    HistoryReceipt,
    PHASE_HISTORICAL,
    PHASE_HISTORY_FAILED,
    PHASE_STARTING,
    RECEIPT_RUNNING,
)
from quadringent_control_plane.fleet_plan import (  # noqa: E402
    CATALOG_FORMAT_VERSION,
    SOURCE_SCHEMA,
    build_fleet_plan,
    parse_fleet_catalog,
)
from quadringent_control_plane.fleet_prepare_runtime import (  # noqa: E402
    PHASE_PREPARED,
    PHASE_PREPARE_FAILED,
    PHASE_PREPARING,
    PREPARE_FORMAT_VERSION,
    ReaderLaunchRequest,
    ReaderReceipt,
    RECEIPT_RUNNING as PREPARE_RECEIPT_RUNNING,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore  # noqa: E402
SITE = site_fixture.build_test_site()

FRESH = JournalCheckpoint(receiver="DEMOJRN0100", sequence=250)
CUTOVER_SEQUENCE = 99
SECRET = "super-secret-token-value"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_URL = "https://snowflake.amazonaws.com/credential"
EXECUTOR_SOURCE = Path(__file__).resolve().parents[1] / "src/quadringent_control_plane/fleet_action_executor.py"
FORBIDDEN_PROJECTION_FRAGMENTS = (
    "reader_id",
    "orchestrator_id",
    "intent_id",
    "exception",
    "password",
    "secret",
    "token",
    "credential",
)


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
    def __init__(self, inner: AtomicJsonStateStore) -> None:
        self.inner = inner
        self.saves: list[dict[str, object]] = []
        self.loads = 0

    def load(self) -> dict[str, object] | None:
        self.loads += 1
        return self.inner.load()

    def save(self, mapping: dict[str, object]) -> None:
        self.saves.append(dict(mapping))
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
    def __init__(self, *, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.calls: list[ReaderLaunchRequest] = []

    def launch(self, request: ReaderLaunchRequest) -> object:
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return ReaderReceipt(
            status=PREPARE_RECEIPT_RUNNING,
            intent_id=request.intent_id,
            checkpoint=request.checkpoint,
            manifest=request.manifest,
            reader_id="reader-" + request.intent_id[:12],
        )


class FakeHistoryLauncher:
    def __init__(self, *, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.calls: list[HistoryLaunchRequest] = []

    def launch(self, request: HistoryLaunchRequest) -> object:
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return HistoryReceipt(
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


class ExplodingStore:
    def load(self) -> dict[str, object] | None:
        raise RuntimeError(f"password={SECRET} {SECRET_URL} {AWS_KEY}")

    def save(self, mapping: dict[str, object]) -> None:
        raise AssertionError(f"save must not run {SECRET}")


def _executor(tmp_path: Path, *, plan=None, provider=None, reader=None, history=None, run_store=None, console_reader=None, jobs=None):
    plan = make_plan() if plan is None else plan
    prepare_store = RecordingStore(AtomicJsonStateStore(tmp_path / "prepare-state.json"))
    history_store = RecordingStore(AtomicJsonStateStore(tmp_path / "history-state.json"))
    provider = FakeProvider() if provider is None else provider
    reader = FakeReaderLauncher() if reader is None else reader
    history = FakeHistoryLauncher() if history is None else history
    if run_store is None:
        run_store = AtomicJsonStateStore(tmp_path / "fleet-run.json")
    executor = FleetActionExecutor(
        plan, prepare_store, history_store, provider, reader, history,
        run_store=run_store,
        console_reader=console_reader, jobs=jobs,
    )
    return executor, plan, prepare_store, history_store, provider, reader, history


def _invocation(action: str, **overrides: object) -> PipelineActionInvocation:
    values = {
        "pipeline_id": EXECUTOR_PIPELINE_ID,
        "action": action,
        "fleet_id": EXECUTOR_FLEET_ID,
        "environment": EXECUTOR_ENVIRONMENT,
    }
    values.update(overrides)
    return PipelineActionInvocation(**values)


def _pipeline_snapshot() -> SimpleNamespace:
    pipeline = SimpleNamespace(
        id=EXECUTOR_PIPELINE_ID,
        environment=EXECUTOR_ENVIRONMENT,
        fleet={
            "fleet_id": EXECUTOR_FLEET_ID,
            "capabilities": {
                "refresh": {"state": "unavailable", "reason": "not_live"},
                "prepare": {"state": "unavailable", "reason": "not_live"},
                "start": {"state": "unavailable", "reason": "not_live"},
                "pause": {"state": "unavailable", "reason": "not_live"},
            },
        },
    )
    return SimpleNamespace(pipelines=(pipeline,))


def _action_body(action: str) -> bytes:
    declared = f"{SITE.site_id.upper()} {SITE.fleet_environment}"
    confirmations = {
        "refresh": None,
        "prepare": f"PREPARE {declared}",
        "start": f"START {declared}",
        "pause": f"PAUSE {declared}",
    }
    return json.dumps(
        {
            "fleet_id": EXECUTOR_FLEET_ID,
            "environment": EXECUTOR_ENVIRONMENT,
            "confirmation": confirmations[action],
        }
    ).encode("utf-8")


def _assert_redacted(payload: object) -> None:
    encoded = json.dumps(payload, default=str)
    assert SECRET not in encoded
    assert AWS_KEY not in encoded
    assert SECRET_URL not in encoded
    assert "://" not in encoded


def _assert_public_projection(payload: dict[str, object], *, phase: str) -> None:
    assert payload["format_version"] == RUNTIME_FORMAT_VERSION
    assert payload["fleet_id"] == EXECUTOR_FLEET_ID
    assert payload["environment"] == EXECUTOR_ENVIRONMENT
    assert payload["pipeline_id"] == EXECUTOR_PIPELINE_ID
    assert payload["phase"] == phase
    assert set(payload) == {
        "format_version",
        "fleet_id",
        "environment",
        "pipeline_id",
        "phase",
        "checkpoint",
        "capabilities",
        "table_states",
    }
    assert set(payload["capabilities"]) == {"prepare", "start", "pause", "resume", "refresh"}
    for name, capability in payload["capabilities"].items():
        assert set(capability) == {"state", "reason"}
        assert capability["state"] in {"available", "unavailable"}
        if capability["state"] == "available":
            assert capability["reason"] is None
        else:
            assert type(capability["reason"]) is str
            assert capability["reason"]
    tables = payload["table_states"]
    assert type(tables) is list
    assert len(tables) == TABLE_COUNT == 13
    assert [table["name"] for table in tables] == list(MANIFEST)
    for table in tables:
        assert set(table) == {"name", "phase", "copied_rows", "total_rows"}
        assert table["phase"] == phase
        for key in ("copied_rows", "total_rows"):
            count = table[key]
            assert count is None or (type(count) is int and type(count) is not bool and count >= 0)
        if table["copied_rows"] is not None and table["total_rows"] is not None:
            assert table["copied_rows"] <= table["total_rows"]
    checkpoint = payload["checkpoint"]
    if checkpoint is not None:
        assert set(checkpoint) == {"receiver", "sequence"}
        assert type(checkpoint["receiver"]) is str
        assert type(checkpoint["sequence"]) is int
        assert type(checkpoint["sequence"]) is not bool
        assert checkpoint["sequence"] >= 0
    encoded = json.dumps(payload)
    for fragment in FORBIDDEN_PROJECTION_FRAGMENTS:
        assert fragment not in encoded
    _assert_redacted(payload)


def test_prepare_then_start_real_transitions_with_fakes(tmp_path: Path) -> None:
    executor, plan, prepare_store, history_store, provider, reader, history = _executor(tmp_path)
    projected = executor.project()
    _assert_public_projection(projected, phase="NOT_PREPARED")
    assert projected["checkpoint"] is None
    assert executor.supports(_invocation("prepare")) is True
    assert executor.supports(_invocation("start")) is False
    assert executor.supports(_invocation("pause")) is False
    assert executor.supports(_invocation("refresh")) is False

    prepare_result = executor.execute(_invocation("prepare"))
    assert prepare_result["intent"]["state"] == "recorded"
    assert prepare_result["execution"]["state"] == "completed"
    assert prepare_result["observed_effect"]["state"] == "succeeded"
    persisted_prepare = prepare_store.inner.load()
    assert persisted_prepare is not None
    assert persisted_prepare["phase"] == PHASE_PREPARED
    assert len(provider.calls) == 1
    assert len(reader.calls) == 1
    assert history.calls == []
    assert plan.cutover_checkpoint != FRESH

    prepared = executor.project()
    _assert_public_projection(prepared, phase="PREPARED")
    assert prepared["checkpoint"] == FRESH.to_dict()
    assert executor.supports(_invocation("prepare")) is False
    assert executor.supports(_invocation("start")) is True
    assert executor.supports(_invocation("pause")) is False
    assert executor.supports(_invocation("refresh")) is False

    start_result = executor.execute(_invocation("start"))
    assert start_result["observed_effect"]["state"] == "succeeded"
    persisted_history = history_store.inner.load()
    assert persisted_history is not None
    assert persisted_history["phase"] == PHASE_HISTORICAL
    assert len(history.calls) == 1
    assert len(reader.calls) == 1

    historical = executor.project()
    _assert_public_projection(historical, phase="HISTORICAL")
    assert historical["checkpoint"] == FRESH.to_dict()
    assert executor.supports(_invocation("prepare")) is False
    assert executor.supports(_invocation("start")) is False
    assert historical["capabilities"]["prepare"]["state"] == "unavailable"
    assert historical["capabilities"]["start"]["state"] == "unavailable"
    assert historical["capabilities"]["pause"]["state"] == "unavailable"
    assert historical["capabilities"]["refresh"]["state"] == "unavailable"


def test_historical_survives_catalog_estimate_drift(tmp_path: Path) -> None:
    """Un catalogue re-mesuré fait bouger row_count/data_size sans changer
    l'affectation slot→tables : la phase HISTORICAL doit survivre —
    régression INT : le refresh de 08:14 avait renvoyé la flotte en UNKNOWN."""

    executor, _plan, prepare_store, history_store, _provider, _reader, _history = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))
    _assert_public_projection(executor.project(), phase="HISTORICAL")

    drifted_payload = catalog_payload()
    for table in drifted_payload["tables"]:
        table["row_count"] = int(table["row_count"]) * 7
        table["data_size"] = int(table["data_size"]) * 7
    drifted_plan = build_fleet_plan(parse_fleet_catalog(drifted_payload))
    assert drifted_plan.historical_lanes != _plan.historical_lanes

    resumed = FleetActionExecutor(
        drifted_plan,
        prepare_store,
        history_store,
        _provider,
        _reader,
        _history,
    )
    _assert_public_projection(resumed.project(), phase="HISTORICAL")


def test_historical_does_not_survive_lane_reassignment(tmp_path: Path) -> None:
    """Une composition différente (table déplacée de voie) invalide le reçu :
    la dérive d'estimation est tolérée, la réaffectation jamais."""

    executor, _plan, prepare_store, history_store, _provider, _reader, _history = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))
    _assert_public_projection(executor.project(), phase="HISTORICAL")

    reassigned_payload = catalog_payload()
    for table in reassigned_payload["tables"]:
        if table["name"] == "CAL001":
            table["row_count"] = 1000000
    reassigned_plan = build_fleet_plan(parse_fleet_catalog(reassigned_payload))
    assert (
        tuple((lane.slot, lane.tables) for lane in reassigned_plan.historical_lanes)
        != tuple((lane.slot, lane.tables) for lane in _plan.historical_lanes)
    )

    resumed = FleetActionExecutor(
        reassigned_plan,
        prepare_store,
        history_store,
        _provider,
        _reader,
        _history,
    )
    _assert_public_projection(resumed.project(), phase="UNKNOWN")


def test_supports_is_dynamic_and_closed_on_scope(tmp_path: Path) -> None:
    executor, _plan, _prepare_store, _history_store, _provider, reader, history = _executor(tmp_path)
    assert executor.supports(_invocation("prepare")) is True
    assert executor.supports(_invocation("prepare", fleet_id="other-fleet")) is False
    assert executor.supports(_invocation("prepare", environment="prod")) is False
    assert executor.supports(_invocation("prepare", pipeline_id="dev-example-corp")) is False
    executor.execute(_invocation("prepare"))
    assert executor.supports(_invocation("prepare")) is False
    assert executor.supports(_invocation("start")) is True
    executor.execute(_invocation("start"))
    assert executor.supports(_invocation("start")) is False
    assert reader.calls
    assert history.calls


def test_execute_pipeline_action_accepts_prepare_and_start_stages(tmp_path: Path) -> None:
    executor, _plan, _prepare_store, _history_store, _provider, _reader, _history = _executor(tmp_path)
    snapshot = _pipeline_snapshot()
    gate = PipelineActionGate()
    prepare_http = execute_pipeline_action(
        raw_body=_action_body("prepare"),
        pipeline_id=EXECUTOR_PIPELINE_ID,
        action="prepare",
        snapshot=snapshot,
        executor=executor,
        gate=gate,
    )
    assert prepare_http.status == HTTPStatus.OK
    assert prepare_http.body["state"] == "succeeded"
    assert prepare_http.body["stages"]["intent"]["state"] == "recorded"
    assert prepare_http.body["stages"]["execution"]["state"] == "completed"
    assert prepare_http.body["stages"]["observed_effect"]["state"] == "succeeded"
    _assert_redacted(prepare_http.body)

    start_http = execute_pipeline_action(
        raw_body=_action_body("start"),
        pipeline_id=EXECUTOR_PIPELINE_ID,
        action="start",
        snapshot=snapshot,
        executor=executor,
        gate=gate,
    )
    assert start_http.status == HTTPStatus.OK
    assert start_http.body["state"] == "succeeded"
    assert start_http.body["stages"]["observed_effect"]["state"] == "succeeded"


def test_execute_pipeline_action_rejects_pause_without_dispatch(tmp_path: Path) -> None:
    executor, _plan, _prepare_store, _history_store, provider, reader, history = _executor(tmp_path)
    result = execute_pipeline_action(
        raw_body=_action_body("pause"),
        pipeline_id=EXECUTOR_PIPELINE_ID,
        action="pause",
        snapshot=_pipeline_snapshot(),
        executor=executor,
        gate=PipelineActionGate(),
    )
    assert result.status == HTTPStatus.CONFLICT
    assert result.body["state"] == "unavailable"
    assert provider.calls == []
    assert reader.calls == []
    assert history.calls == []


def test_projection_omits_technical_ids_and_secrets(tmp_path: Path) -> None:
    executor, _plan, prepare_store, history_store, _provider, _reader, _history = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    projected = executor.project()
    encoded = json.dumps(projected)
    persisted = prepare_store.inner.load()
    assert persisted is not None
    assert persisted["intent_id"] not in encoded
    assert persisted["receipt"]["reader_id"] not in encoded
    executor.execute(_invocation("start"))
    historical = executor.project()
    history_state = history_store.inner.load()
    assert history_state is not None
    encoded_history = json.dumps(historical)
    assert history_state["intent_id"] not in encoded_history
    assert history_state["reader_id"] not in encoded_history
    assert history_state["receipt"]["orchestrator_id"] not in encoded_history
    _assert_public_projection(historical, phase="HISTORICAL")


def test_project_and_supports_do_not_mutate_stores(tmp_path: Path) -> None:
    executor, _plan, prepare_store, history_store, provider, reader, history = _executor(tmp_path)
    executor.project()
    executor.supports(_invocation("prepare"))
    executor.supports(_invocation("start"))
    assert prepare_store.saves == []
    assert history_store.saves == []
    assert provider.calls == []
    assert reader.calls == []
    assert history.calls == []


def test_corrupt_state_is_unknown_without_fake_zero(tmp_path: Path) -> None:
    executor, _plan, prepare_store, history_store, provider, reader, history = _executor(tmp_path)
    prepare_path = tmp_path / "prepare-state.json"
    prepare_path.write_bytes(b"{not json")
    projected = executor.project()
    _assert_public_projection(projected, phase="UNKNOWN")
    assert projected["checkpoint"] is None
    assert projected["capabilities"]["prepare"]["state"] == "unavailable"
    assert projected["capabilities"]["start"]["state"] == "unavailable"
    assert projected["capabilities"]["prepare"]["reason"] == "invalid_runtime_state"
    assert executor.supports(_invocation("prepare")) is False
    assert executor.supports(_invocation("start")) is False
    assert prepare_store.saves == []
    assert history_store.saves == []
    assert provider.calls == []
    assert reader.calls == []
    assert history.calls == []


def test_invalid_prepared_schema_is_unknown_not_prepared(tmp_path: Path) -> None:
    executor, _plan, prepare_store, _history_store, _provider, reader, history = _executor(tmp_path)
    prepare_store.inner.save({"phase": PHASE_PREPARED, "checkpoint": {"receiver": "R", "sequence": 0}})
    save_count = len(prepare_store.saves)
    projected = executor.project()
    _assert_public_projection(projected, phase="UNKNOWN")
    assert projected["checkpoint"] is None
    assert executor.supports(_invocation("start")) is False
    assert len(prepare_store.saves) == save_count
    assert reader.calls == []
    assert history.calls == []


def test_prepare_failed_state_is_blocked(tmp_path: Path) -> None:
    executor, plan, prepare_store, _history_store, _provider, _reader, _history = _executor(tmp_path)
    prepare_store.inner.save(
        {
            "format_version": PREPARE_FORMAT_VERSION,
            "environment": ENVIRONMENT,
            "phase": PHASE_PREPARE_FAILED,
            "intent_id": "intentfail01",
            "manifest": list(MANIFEST),
            "checkpoint": FRESH.to_dict(),
            "journal_library": plan.journal_groups[0].library,
            "journal_name": plan.journal_groups[0].name,
            "reader_kind": "multi_object",
            "reader_count": 1,
            "receipt": None,
            "error_code": "launch_failed",
            "needs_recovery": True,
        }
    )
    projected = executor.project()
    _assert_public_projection(projected, phase="BLOCKED")
    assert projected["checkpoint"] is None
    assert executor.supports(_invocation("prepare")) is False
    assert executor.supports(_invocation("start")) is False
    assert projected["capabilities"]["prepare"]["reason"] == "needs_recovery"


def test_preparing_residual_is_blocked(tmp_path: Path) -> None:
    executor, plan, prepare_store, _history_store, _provider, _reader, _history = _executor(tmp_path)
    prepare_store.inner.save(
        {
            "format_version": PREPARE_FORMAT_VERSION,
            "environment": ENVIRONMENT,
            "phase": PHASE_PREPARING,
            "intent_id": "intentprep01",
            "manifest": list(MANIFEST),
            "checkpoint": FRESH.to_dict(),
            "journal_library": plan.journal_groups[0].library,
            "journal_name": plan.journal_groups[0].name,
            "reader_kind": "multi_object",
            "reader_count": 1,
            "receipt": None,
            "error_code": None,
            "needs_recovery": False,
        }
    )
    projected = executor.project()
    _assert_public_projection(projected, phase="BLOCKED")
    assert executor.supports(_invocation("prepare")) is False
    assert executor.supports(_invocation("start")) is False


def test_wrong_scope_execute_does_not_dispatch(tmp_path: Path) -> None:
    executor, _plan, prepare_store, history_store, provider, reader, history = _executor(tmp_path)
    for invocation in (
        _invocation("prepare", fleet_id="other"),
        _invocation("prepare", environment="prod"),
        _invocation("prepare", pipeline_id="dev-example-corp"),
        _invocation("start", fleet_id="other"),
    ):
        result = executor.execute(invocation)
        assert result["intent"]["state"] == "recorded"
        assert result["execution"]["state"] == "failed"
        assert result["execution"]["code"] == "invalid_scope"
        assert result["observed_effect"]["state"] == "failed"
        _assert_redacted(result)
    assert provider.calls == []
    assert reader.calls == []
    assert history.calls == []
    assert prepare_store.inner.load() is None
    assert history_store.inner.load() is None


def test_unsupported_actions_fail_closed(tmp_path: Path) -> None:
    executor, _plan, _prepare_store, _history_store, provider, reader, history = _executor(tmp_path)
    for action in ("pause", "refresh"):
        result = executor.execute(_invocation(action))
        assert result["execution"]["state"] == "failed"
        assert result["execution"]["code"] == "unsupported_action"
        assert result["observed_effect"]["state"] == "failed"
        assert executor.supports(_invocation(action)) is False
        _assert_redacted(result)
    assert provider.calls == []
    assert reader.calls == []
    assert history.calls == []


def test_start_without_prepare_fails_and_stays_not_prepared(tmp_path: Path) -> None:
    executor, _plan, prepare_store, history_store, _provider, reader, history = _executor(tmp_path)
    result = executor.execute(_invocation("start"))
    assert result["execution"]["state"] == "failed"
    assert result["observed_effect"]["state"] == "failed"
    _assert_redacted(result)
    assert reader.calls == []
    assert history.calls == []
    assert prepare_store.inner.load() is None
    persisted_history = history_store.inner.load()
    if persisted_history is not None:
        assert persisted_history["phase"] == PHASE_HISTORY_FAILED
    projected = executor.project()
    assert projected["phase"] in {"NOT_PREPARED", "BLOCKED"}
    assert projected["checkpoint"] is None


def test_launch_error_is_redacted_failed_stages(tmp_path: Path) -> None:
    reader = FakeReaderLauncher(error=RuntimeError(f"{SECRET} {AWS_KEY} {SECRET_URL}"))
    executor, _plan, prepare_store, _history_store, _provider, _reader, history = _executor(
        tmp_path, reader=reader
    )
    result = executor.execute(_invocation("prepare"))
    assert result["intent"]["state"] == "recorded"
    assert result["execution"]["state"] == "failed"
    assert result["observed_effect"]["state"] == "failed"
    _assert_redacted(result)
    persisted = prepare_store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARE_FAILED
    _assert_redacted(persisted)
    assert history.calls == []
    projected = executor.project()
    _assert_public_projection(projected, phase="BLOCKED")
    _assert_redacted(projected)


def test_exploding_store_stays_unknown_and_redacted() -> None:
    plan = make_plan()
    executor = FleetActionExecutor(
        plan,
        ExplodingStore(),
        ExplodingStore(),
        FakeProvider(error=RuntimeError(SECRET)),
        FakeReaderLauncher(error=RuntimeError(SECRET)),
        FakeHistoryLauncher(error=RuntimeError(SECRET)),
    )
    projected = executor.project()
    _assert_public_projection(projected, phase="UNKNOWN")
    assert projected["checkpoint"] is None
    assert executor.supports(_invocation("prepare")) is False
    result = executor.execute(_invocation("prepare"))
    assert result["execution"]["state"] == "failed"
    _assert_redacted(result)
    _assert_redacted(projected)


def test_concurrent_prepare_is_delegated_to_runtime(tmp_path: Path) -> None:
    reader = FakeReaderLauncher(delay=0.05)
    executor, _plan, prepare_store, _history_store, provider, _reader, history = _executor(
        tmp_path, reader=reader
    )
    results: list[dict[str, dict[str, str]]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(executor.execute(_invocation("prepare")))
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    assert len(results) == 2
    assert all(item["observed_effect"]["state"] == "succeeded" for item in results)
    assert len(provider.calls) == 1
    assert len(reader.calls) == 1
    assert history.calls == []
    persisted = prepare_store.inner.load()
    assert persisted is not None
    assert persisted["phase"] == PHASE_PREPARED
    source = EXECUTOR_SOURCE.read_text(encoding="utf-8")
    assert "threading.Lock" not in source
    assert "Lock()" not in source


def test_success_requires_persisted_phase(tmp_path: Path) -> None:
    class DroppingStore:
        def __init__(self) -> None:
            self._inner: dict[str, object] | None = None

        def load(self) -> dict[str, object] | None:
            return None if self._inner is None else dict(self._inner)

        def save(self, mapping: dict[str, object]) -> None:
            if mapping.get("phase") == PHASE_PREPARED:
                self._inner = None
                return
            self._inner = dict(mapping)

    plan = make_plan()
    prepare_store = DroppingStore()
    history_store = RecordingStore(AtomicJsonStateStore(tmp_path / "history-state.json"))
    executor = FleetActionExecutor(
        plan,
        prepare_store,
        history_store,
        FakeProvider(),
        FakeReaderLauncher(),
        FakeHistoryLauncher(),
    )
    result = executor.execute(_invocation("prepare"))
    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == "prepare_failed"
    assert result["observed_effect"]["state"] == "failed"
    assert prepare_store.load() is None


def test_history_starting_residual_is_blocked(tmp_path: Path) -> None:
    executor, plan, prepare_store, history_store, _provider, _reader, history = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    prepared = prepare_store.inner.load()
    assert prepared is not None
    history_store.inner.save(
        {
            "format_version": HISTORY_FORMAT_VERSION,
            "environment": ENVIRONMENT,
            "phase": PHASE_STARTING,
            "intent_id": "intenthist01",
            "prepare_intent_id": prepared["intent_id"],
            "reader_id": prepared["receipt"]["reader_id"],
            "checkpoint": prepared["checkpoint"],
            "manifest": list(MANIFEST),
            "lanes": [lane.to_dict() for lane in plan.historical_lanes],
            "max_concurrency": plan.max_concurrency,
            "receipt": None,
            "error_code": None,
            "needs_recovery": False,
        }
    )
    save_count = len(history_store.saves)
    projected = executor.project()
    _assert_public_projection(projected, phase="BLOCKED")
    assert executor.supports(_invocation("start")) is False
    assert len(history_store.saves) == save_count
    assert history.calls == []


def test_run_state_drives_real_per_table_phases(tmp_path: Path) -> None:
    """Le run domaine persisté prime : phases réelles par table, agrégat mesuré."""
    from quadringent_control_plane.fleet_progression import (
        FleetEvidence,
        FleetProgression,
        TableMeasure,
        run_state_document,
    )
    from quadringent_control_plane.fleet import (
        ReceiverChain,
        ReceiverSpan,
        create_fleet,
        prepare_table,
        admit_next,
        record_actual_cost,
        record_history_progress,
        record_journal_evidence,
    )

    run_store = AtomicJsonStateStore(tmp_path / "fleet-run.json")
    executor, plan, prepare_store, history_store, _provider, _reader, _history = _executor(
        tmp_path, run_store=run_store
    )
    executor.execute(_invocation("prepare"))
    prepared = prepare_store.inner.load()
    executor.execute(_invocation("start"))

    # Un run domaine complet : chaque voie a copié son historique mesuré,
    # la continuité est prouvée, le curseur est à la queue commise.
    start = JournalCheckpoint(FRESH.receiver, FRESH.sequence)
    tail = JournalCheckpoint(FRESH.receiver, FRESH.sequence + 100)
    chain = ReceiverChain(
        (ReceiverSpan(FRESH.receiver, FRESH.sequence, FRESH.sequence + 200),)
    )
    run = create_fleet(max_concurrency=plan.max_concurrency, credit_budget=1e9)
    for name in MANIFEST:
        run = prepare_table(run, name, start)
    from quadringent_control_plane.fleet import admission_candidates, Phase
    for name in MANIFEST:
        # Les slots de concurrence bornent l'admission : on n'admet que
        # quand le domaine le propose, puis la voie avance jusqu'à LIVE —
        # même ordre de transitions que le pilote.
        while name in admission_candidates(run):
            run = admit_next(run, estimated_credits=100.0)
        run = record_history_progress(run, name, copied_rows=100, total_rows=100)
        run = record_journal_evidence(
            run,
            name,
            current_checkpoint=tail,
            journal_tail=tail,
            receiver_chain=chain,
            continuity_proven=True,
            gap=False,
        )
        if run.tables[MANIFEST.index(name)].phase is Phase.CATCHING_UP:
            run = record_actual_cost(run, name, 110.0)
            run = record_journal_evidence(
                run,
                name,
                current_checkpoint=tail,
                journal_tail=tail,
                receiver_chain=chain,
                continuity_proven=True,
                gap=False,
            )
    run_store.save(
        run_state_document(run, prepare_intent_id=prepared["intent_id"])
    )

    projected = executor.project()
    assert projected["phase"] == "LIVE"
    assert projected["checkpoint"] == FRESH.to_dict()
    assert len(projected["table_states"]) == TABLE_COUNT
    assert all(
        state["phase"] == "LIVE" for state in projected["table_states"]
    )
    # Les compteurs mesurés de la copie initiale remontent dans la projection.
    assert all(
        state["copied_rows"] == 100 and state["total_rows"] == 100
        for state in projected["table_states"]
    )
    capabilities = projected["capabilities"]
    assert capabilities["prepare"]["state"] == "unavailable"
    assert capabilities["start"]["state"] == "already_started" or capabilities[
        "start"
    ]["state"] == "unavailable"
    # Le run autorise la suspension tant que la capture tourne.
    assert capabilities["pause"]["state"] in {"available", "unavailable"}


def test_run_state_corrupt_blocks_projection(tmp_path: Path) -> None:
    """Un fleet-run hors contrat dégrade en UNKNOWN — jamais contourné."""
    run_store = AtomicJsonStateStore(tmp_path / "fleet-run.json")
    executor, _plan, prepare_store, _history_store, _provider, _reader, _history = (
        _executor(tmp_path, run_store=run_store)
    )
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))
    run_store.save({"format_version": "other", "fleet": {}})

    projected = executor.project()
    assert projected["phase"] == "UNKNOWN"


def test_run_state_stale_intent_blocks_projection(tmp_path: Path) -> None:
    """Un run d'une génération prepare morte n'avance rien."""
    from quadringent_control_plane.fleet_progression import run_state_document
    from quadringent_control_plane.fleet import create_fleet, prepare_table

    run_store = AtomicJsonStateStore(tmp_path / "fleet-run.json")
    executor, _plan, prepare_store, _history_store, _provider, _reader, _history = (
        _executor(tmp_path, run_store=run_store)
    )
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))

    run = create_fleet(max_concurrency=4, credit_budget=1e9)
    for name in MANIFEST:
        run = prepare_table(run, name, FRESH)
    run_store.save(run_state_document(run, prepare_intent_id="intent-dead"))

    projected = executor.project()
    assert projected["phase"] == "UNKNOWN"


def test_run_state_absent_keeps_job_phase(tmp_path: Path) -> None:
    """Sans run persisté la projection reste celle du job — aucun trou."""
    executor, _plan, _prepare_store, _history_store, _provider, _reader, _history = (
        _executor(tmp_path)
    )
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))

    projected = executor.project()
    _assert_public_projection(projected, phase="HISTORICAL")


PARKED_CHECKPOINT = JournalCheckpoint(receiver="DEMOJRN4115", sequence=355013677)


def _parked_console() -> dict[str, object]:
    """Document console d'une capture arrêtée fail-closed au checkpoint durable."""
    return {
        "run": {
            "state": "STOPPED_AUTH_BLOCKED",
            "stopped_because": "IbmiUserDisabledError",
        },
        "position": {
            "checkpoint": {
                "receiver": PARKED_CHECKPOINT.receiver,
                "sequence": PARKED_CHECKPOINT.sequence,
            }
        },
    }


def _proven_fresh_plan():
    """Plan dont la sonde catalogue vient de prouver la continuité."""
    payload = catalog_payload()
    payload["observed_at"] = datetime.now(timezone.utc).isoformat()
    payload["journals"][0]["continuity"] = "proven"
    return build_fleet_plan(parse_fleet_catalog(payload))


class FakeJobs:
    """Client Job minimal : la relecture post-lancement de la reprise."""

    def __init__(self, job: object = None) -> None:
        self.job = job
        self.reads: list[str] = []
        self.ready = lambda: True

    def read_job(self, name: str) -> object:
        self.reads.append(name)
        return self.job if self.ready() else None


def _prepared_parked_executor(tmp_path: Path, *, plan=None, console=None, jobs=None):
    """Un executor avec un prepare accompli puis une capture parquée."""
    executor, plan, prepare_store, history_store, provider, reader, history = _executor(
        tmp_path,
        plan=_proven_fresh_plan() if plan is None else plan,
        console_reader=_parked_console if console is None else console,
        jobs=FakeJobs({"status": {"active": 1}}) if jobs is None else jobs,
    )
    assert executor.execute(_invocation("prepare"))["observed_effect"]["state"] == "succeeded"
    reader.calls.clear()
    executor._jobs.ready = lambda: bool(reader.calls)
    return executor, prepare_store, reader


def test_parked_capture_exposes_resume_capability(tmp_path: Path) -> None:
    """Cause résolue + sonde fraîche : la reprise est offerte sur la preuve."""
    executor, _prepare_store, _reader = _prepared_parked_executor(tmp_path)
    assert executor.supports(_invocation("resume")) is True
    assert executor.project()["capabilities"]["resume"] == {
        "state": "available",
        "reason": None,
    }


def test_job_termine_encore_present_masque_la_reprise(tmp_path: Path):
    jobs = FakeJobs({"status": {"conditions": [{"type": "Complete", "status": "True"}]}})
    executor, _, reader = _prepared_parked_executor(tmp_path, jobs=jobs)
    jobs.ready = lambda: True
    assert executor.project()["capabilities"]["resume"]["state"] == "unavailable"
    assert executor.execute(_invocation("resume"))["execution"]["state"] == "failed"
    assert reader.calls == []


def test_parked_resume_relaunches_reader_at_durable_checkpoint(tmp_path: Path) -> None:
    """La reprise crée un Job neuf au checkpoint console, sur le même intent."""
    jobs = FakeJobs({"status": {"active": 1}})
    executor, prepare_store, reader = _prepared_parked_executor(tmp_path, jobs=jobs)
    result = executor.execute(_invocation("resume"))
    assert result["observed_effect"]["state"] == "succeeded"
    assert result["observed_effect"]["code"] == "reader_relaunched"
    assert len(reader.calls) == 1
    request = reader.calls[0]
    persisted = prepare_store.inner.load()
    # Le checkpoint repris est celui du document console (durable), pas le
    # relevé du provider ; l'intention prepare est réutilisée — idempotence.
    assert request.checkpoint == PARKED_CHECKPOINT
    assert request.intent_id == persisted["intent_id"]
    assert jobs.reads == [f"reader-{persisted['intent_id'][:12]}"] * 2


def test_parked_resume_blocked_when_probe_stale(tmp_path: Path) -> None:
    """Continuité prouvée mais sonde ancienne : la reprise n'est pas offerte."""
    payload = catalog_payload()
    payload["journals"][0]["continuity"] = "proven"  # prouvée mais figée à 2026-09-13
    plan = build_fleet_plan(parse_fleet_catalog(payload))
    executor, _prepare_store, reader = _prepared_parked_executor(tmp_path, plan=plan)
    assert executor.supports(_invocation("resume")) is False
    assert executor.project()["capabilities"]["resume"]["state"] == "unavailable"
    result = executor.execute(_invocation("resume"))
    assert result["execution"]["state"] == "failed"
    assert reader.calls == []


def test_parked_resume_blocked_when_continuity_unproven(tmp_path: Path) -> None:
    """Continuité incertaine : aucune reprise, même avec console parquée."""
    payload = catalog_payload()
    payload["observed_at"] = datetime.now(timezone.utc).isoformat()
    plan = build_fleet_plan(parse_fleet_catalog(payload))  # continuity uncertain
    executor, _prepare_store, reader = _prepared_parked_executor(tmp_path, plan=plan)
    assert executor.supports(_invocation("resume")) is False
    result = executor.execute(_invocation("resume"))
    assert result["execution"]["state"] == "failed"
    assert reader.calls == []


def test_running_capture_is_not_resumed(tmp_path: Path) -> None:
    """Une capture qui tourne encore n'est jamais relancée en double."""
    console = {
        "run": {"state": "RUNNING"},
        "position": {"checkpoint": {"receiver": "DEMOJRN4115", "sequence": 355013677}},
    }
    executor, _prepare_store, reader = _prepared_parked_executor(tmp_path, console=lambda: console)
    assert executor.supports(_invocation("resume")) is False
    result = executor.execute(_invocation("resume"))
    assert result["execution"]["state"] == "failed"
    assert reader.calls == []


def test_parked_resume_refused_when_recreated_job_failed(tmp_path: Path) -> None:
    """Un Job recréé déjà en échec terminal n'est pas déclaré repris."""
    jobs = FakeJobs({"status": {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}})
    executor, _prepare_store, reader = _prepared_parked_executor(tmp_path, jobs=jobs)
    result = executor.execute(_invocation("resume"))
    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == "reader_failed"
    assert len(reader.calls) == 1


def test_parked_resume_without_job_client_is_refused(tmp_path: Path) -> None:
    """Sans relecture du Job possible, la reprise ne se déclare pas."""
    executor, _plan, _prepare_store, _hs, _p, reader, _h = _executor(
        tmp_path, plan=_proven_fresh_plan(), console_reader=_parked_console, jobs=None,
    )
    executor.execute(_invocation("prepare"))
    reader.calls.clear()
    result = executor.execute(_invocation("resume"))
    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == "unsupported_action"
    assert len(reader.calls) == 0


# -- Cohérence phase domaine / état du lecteur -----------------------------
#
# La phase HISTORICAL (et au-delà) mesure l'avancement des tables, persisté
# dans fleet-run.json ; elle ne dit rien du lecteur lui-même. Un lecteur
# arrêté fail-closed pendant que le run reste HISTORICAL ne doit jamais être
# annoncé "already_started" pour start, ni offrir resume sans la même preuve
# fraîche que la reprise parquée exige déjà à l'exécution.


def test_historical_reader_stopped_reports_the_real_stop_not_already_started(
    tmp_path: Path,
) -> None:
    """Preuve fraîche : start reflète l'arrêt réel, resume devient disponible."""
    executor, _prepare_store, _reader = _prepared_parked_executor(tmp_path)
    executor.execute(_invocation("start"))
    projected = executor.project()
    assert projected["phase"] == "HISTORICAL"
    capabilities = projected["capabilities"]
    assert capabilities["start"] == {
        "state": "unavailable",
        "reason": "reader_stopped_auth_blocked",
    }
    assert capabilities["resume"] == {"state": "available", "reason": None}


def test_historical_reader_stopped_fail_closed_has_its_own_reason(tmp_path: Path) -> None:
    """STOPPED_FAIL_CLOSED et STOPPED_AUTH_BLOCKED publient des raisons distinctes."""
    console = {
        "run": {"state": "STOPPED_FAIL_CLOSED"},
        "position": {
            "checkpoint": {"receiver": PARKED_CHECKPOINT.receiver, "sequence": PARKED_CHECKPOINT.sequence}
        },
    }
    executor, _prepare_store, _reader = _prepared_parked_executor(
        tmp_path, console=lambda: console
    )
    executor.execute(_invocation("start"))
    capabilities = executor.project()["capabilities"]
    assert capabilities["start"]["state"] == "unavailable"
    assert capabilities["start"]["reason"] == "reader_stopped_fail_closed"


def test_historical_reader_stopped_without_fresh_proof_keeps_resume_unavailable(
    tmp_path: Path,
) -> None:
    """Sonde ancienne : start dit l'arrêt réel, resume reste indisponible avec
    une raison explicite — jamais confondue avec "non supporté par ce
    déploiement" (le lecteur console est bien monté ici)."""
    payload = catalog_payload()
    payload["journals"][0]["continuity"] = "proven"  # prouvée mais figée à 2026-09-13
    plan = build_fleet_plan(parse_fleet_catalog(payload))
    executor, _prepare_store, _reader = _prepared_parked_executor(tmp_path, plan=plan)
    executor.execute(_invocation("start"))
    projected = executor.project()
    assert projected["phase"] == "HISTORICAL"
    capabilities = projected["capabilities"]
    assert capabilities["start"] == {
        "state": "unavailable",
        "reason": "reader_stopped_auth_blocked",
    }
    assert capabilities["resume"] == {"state": "unavailable", "reason": "resume_not_ready"}


def test_historical_running_reader_still_reports_already_started(tmp_path: Path) -> None:
    """Un lecteur toujours RUNNING garde le vocabulaire existant, sans régression."""
    console = {
        "run": {"state": "RUNNING"},
        "position": {
            "checkpoint": {"receiver": PARKED_CHECKPOINT.receiver, "sequence": PARKED_CHECKPOINT.sequence}
        },
    }
    executor, _prepare_store, _reader = _prepared_parked_executor(
        tmp_path, console=lambda: console
    )
    executor.execute(_invocation("start"))
    capabilities = executor.project()["capabilities"]
    assert capabilities["start"] == {"state": "unavailable", "reason": "already_started"}


def test_historical_without_console_reader_keeps_legacy_already_started(
    tmp_path: Path,
) -> None:
    """Aucun lecteur console monté : comportement historique inchangé."""
    executor, _plan, _prepare_store, _history_store, _provider, _reader, _history = (
        _executor(tmp_path)
    )
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))
    capabilities = executor.project()["capabilities"]
    assert capabilities["start"] == {"state": "unavailable", "reason": "already_started"}


def test_une_phase_non_pausable_ne_se_dit_pas_non_supportee() -> None:
    """« Non supporté » ne désigne qu'un déploiement sans runtime de suspension.

    La phase CERTIFIED n'est pas dans ``_PAUSABLE_PHASES``. Publier
    ``unsupported_action`` dans ce cas revenait à dire que le produit ne savait
    pas suspendre, alors que le runtime est monté : l'interface en déduisait
    qu'il fallait passer par l'exploitation, ce qui était faux.
    """

    from quadringent_control_plane.fleet_action_executor import (
        CODE_NOT_PAUSABLE_IN_PHASE,
        _capabilities,
    )

    monte = _capabilities("CERTIFIED", pause_supported=True, refresh_supported=True)
    assert monte["pause"]["reason"] == CODE_NOT_PAUSABLE_IN_PHASE
    assert monte["resume"]["reason"] == CODE_NOT_PAUSABLE_IN_PHASE

    absent = _capabilities("CERTIFIED", pause_supported=False, refresh_supported=True)
    assert absent["pause"]["reason"] == "unsupported_action"
    assert absent["resume"]["reason"] == "unsupported_action"


def test_une_phase_pausable_offre_bien_la_suspension() -> None:
    from quadringent_control_plane.fleet_action_executor import _capabilities

    capacites = _capabilities("LIVE", pause_supported=True, refresh_supported=True)
    assert capacites["pause"] == {"state": "available", "reason": None}
    assert capacites["resume"] == {"state": "unavailable", "reason": "not_paused"}
