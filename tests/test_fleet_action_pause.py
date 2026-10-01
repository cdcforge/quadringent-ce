"""Pause et reprise vues depuis le contrat d'action du control plane."""

from __future__ import annotations

from pathlib import Path

import pytest

from quadringent_control_plane.fleet_action_executor import (
    EXECUTOR_ENVIRONMENT,
    EXECUTOR_FLEET_ID,
    EXECUTOR_PIPELINE_ID,
    FleetActionExecutor,
)
from quadringent_control_plane.actions import PipelineActionInvocation
from quadringent_control_plane.fleet_pause_runtime import (
    CODE_NOT_PAUSED,
    CODE_NOT_PREPARED,
    CODE_SUSPEND_FAILED,
    PHASE_PAUSED,
    PHASE_RESUMED,
    FleetPauseRuntime,
    PauseOutcome,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from quadringent_control_plane.k8s_jobs import KubernetesJobsClient

from test_fleet_action_executor import (
    FakeHistoryLauncher,
    FakeProvider,
    FakeReaderLauncher,
    RecordingStore,
    _assert_public_projection,
    make_plan,
)
from test_fleet_pause_runtime import (
    HISTORY_INTENT,
    HISTORY_JOB,
    NAMESPACE,
    PREPARE_INTENT,
    READER_JOB,
    FakeJobs,
    job_document,
)
from test_fleet_job_launcher import FakeCluster  # noqa: F401 - partage du harnais


def invocation(action: str) -> PipelineActionInvocation:
    return PipelineActionInvocation(
        pipeline_id=EXECUTOR_PIPELINE_ID,
        action=action,
        fleet_id=EXECUTOR_FLEET_ID,
        environment=EXECUTOR_ENVIRONMENT,
    )


class StubPauseRuntime:
    """Doublure qui rend un résultat choisi, sans Kubernetes.

    ``apply=True`` imite le runtime réel : un résultat sans erreur change
    l'état durable. ``apply=False`` simule un effet non observé.
    """

    def __init__(
        self,
        *,
        outcome: PauseOutcome,
        paused: tuple[bool, str | None] = (False, None),
        apply: bool = True,
    ) -> None:
        self.outcome = outcome
        self._paused = paused
        self.apply = apply
        self.calls: list[str] = []

    def pause(self) -> PauseOutcome:
        self.calls.append("pause")
        if self.apply and self.outcome.error_code is None:
            self._paused = (self.outcome.phase == PHASE_PAUSED, None)
        return self.outcome

    def resume(self) -> PauseOutcome:
        self.calls.append("resume")
        if self.apply and self.outcome.error_code is None:
            self._paused = (self.outcome.phase == PHASE_PAUSED, None)
        return self.outcome

    def is_paused(self) -> tuple[bool, str | None]:
        return self._paused


def outcome(phase: str, *, error_code: str | None = None) -> PauseOutcome:
    return PauseOutcome(
        phase=phase,
        suspended=(),
        already_suspended=(),
        reader_id="reader",
        history_id=None,
        error_code=error_code,
        needs_recovery=False,
    )


def executor(tmp_path: Path, pause_runtime: object | None) -> FleetActionExecutor:
    return FleetActionExecutor(
        make_plan(),
        RecordingStore(AtomicJsonStateStore(tmp_path / "prepare-state.json")),
        RecordingStore(AtomicJsonStateStore(tmp_path / "history-state.json")),
        FakeProvider(),
        FakeReaderLauncher(),
        FakeHistoryLauncher(),
        pause_runtime=pause_runtime,
    )


def prepared_executor(tmp_path: Path, pause_runtime: object | None) -> FleetActionExecutor:
    """Amène l'exécuteur dans un état PREPARED réel, sans doublure d'état."""

    action_executor = executor(tmp_path, pause_runtime)
    prepared = action_executor.execute(invocation("prepare"))
    assert prepared["observed_effect"]["state"] == "succeeded"
    state = action_executor._prepare_store.load()  # noqa: SLF001 - harnais de test
    assert state is not None and state["phase"] == "PREPARED"
    return action_executor


def with_history(action_executor: FleetActionExecutor) -> FleetActionExecutor:
    started = action_executor.execute(invocation("start"))
    assert started["observed_effect"]["state"] == "succeeded"
    state = action_executor._history_store.load()  # noqa: SLF001 - harnais de test
    assert state is not None and state["phase"] == "HISTORICAL"
    return action_executor


def test_pause_and_resume_stay_refused_without_a_pause_runtime(tmp_path: Path) -> None:
    action_executor = executor(tmp_path, None)

    assert action_executor.supports(invocation("pause")) is False
    assert action_executor.supports(invocation("resume")) is False
    assert action_executor.execute(invocation("pause"))["execution"]["code"] == "unsupported_action"
    assert action_executor.execute(invocation("resume"))["execution"]["code"] == "unsupported_action"
    projected = action_executor.project()
    assert projected["capabilities"]["pause"]["reason"] == "unsupported_action"
    assert projected["capabilities"]["resume"]["reason"] == "unsupported_action"


def test_pause_is_offered_only_when_a_fleet_run_exists(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome(PHASE_PAUSED), paused=(False, None))
    action_executor = executor(tmp_path, stub)

    # Aucune préparation persistée : rien à suspendre.
    assert action_executor.supports(invocation("pause")) is False
    assert action_executor.supports(invocation("resume")) is False


def test_a_recorded_pause_is_visible_and_resumable(tmp_path: Path) -> None:
    action_executor = prepared_executor(
        tmp_path,
        StubPauseRuntime(outcome=outcome(PHASE_RESUMED), paused=(True, None)),
    )

    projected = action_executor.project()

    _assert_public_projection(projected, phase=PHASE_PAUSED)
    assert projected["capabilities"]["resume"] == {"state": "available", "reason": None}
    assert projected["capabilities"]["pause"] == {
        "state": "unavailable",
        "reason": "already_paused",
    }
    assert action_executor.supports(invocation("pause")) is False
    assert action_executor.supports(invocation("resume")) is True


def test_pause_success_is_only_reported_when_the_state_is_durable(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome(PHASE_PAUSED))
    action_executor = prepared_executor(tmp_path, stub)

    assert action_executor.supports(invocation("pause")) is True
    result = action_executor.execute(invocation("pause"))

    assert stub.calls == ["pause"]
    assert result["observed_effect"]["state"] == "succeeded"


def test_a_refused_pause_never_reports_a_recorded_intent(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome("", error_code=CODE_NOT_PREPARED), paused=(False, None))
    action_executor = prepared_executor(tmp_path, stub)

    result = action_executor.execute(invocation("pause"))

    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == CODE_NOT_PREPARED
    assert result["observed_effect"]["state"] == "failed"
    _assert_public_projection(action_executor.project(), phase="PREPARED")


def test_resume_success_clears_the_paused_state(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome(PHASE_RESUMED), paused=(True, None))
    action_executor = prepared_executor(tmp_path, stub)

    assert action_executor.supports(invocation("resume")) is True
    result = action_executor.execute(invocation("resume"))

    assert stub.calls == ["resume"]
    assert result["observed_effect"]["state"] == "succeeded"


def test_resume_without_pause_is_refused(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome("", error_code=CODE_NOT_PAUSED), paused=(False, None))
    action_executor = prepared_executor(tmp_path, stub)

    result = action_executor.execute(invocation("resume"))

    assert result["execution"]["code"] == CODE_NOT_PAUSED


def test_a_pause_that_is_not_durable_is_a_failure(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome(PHASE_PAUSED), apply=False)
    action_executor = prepared_executor(tmp_path, stub)

    result = action_executor.execute(invocation("pause"))

    assert result["execution"]["state"] == "failed"


def test_an_unreadable_pause_state_makes_the_runtime_unknown(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome(PHASE_PAUSED), paused=(False, "invalid_runtime_state"))
    action_executor = prepared_executor(tmp_path, stub)

    projected = action_executor.project()

    _assert_public_projection(projected, phase="UNKNOWN")
    assert projected["checkpoint"] is None


def test_the_real_pause_runtime_drives_the_whole_action(tmp_path: Path) -> None:
    """Chaîne complète : intention préparée, Jobs suspendus, reprise observée."""

    jobs = FakeJobs()
    prepared_store = RecordingStore(AtomicJsonStateStore(tmp_path / "prepare-state.json"))
    history_store = RecordingStore(AtomicJsonStateStore(tmp_path / "history-state.json"))
    runtime = FleetPauseRuntime(
        prepared_store,
        history_store,
        AtomicJsonStateStore(tmp_path / "pause-state.json"),
        KubernetesJobsClient(jobs, NAMESPACE),
    )
    action_executor = FleetActionExecutor(
        make_plan(),
        prepared_store,
        history_store,
        FakeProvider(),
        FakeReaderLauncher(),
        FakeHistoryLauncher(),
        pause_runtime=runtime,
    )
    assert action_executor.execute(invocation("prepare"))["observed_effect"]["state"] == "succeeded"
    assert action_executor.execute(invocation("start"))["observed_effect"]["state"] == "succeeded"

    prepare_state_now = prepared_store.load()
    history_state_now = history_store.load()
    assert prepare_state_now is not None and history_state_now is not None
    reader_job = prepare_state_now["receipt"]["reader_id"]
    history_job = history_state_now["receipt"]["orchestrator_id"]
    jobs.jobs[reader_job] = job_document("reader", prepare_state_now["intent_id"])
    jobs.jobs[history_job] = job_document("history", history_state_now["intent_id"])

    paused = action_executor.execute(invocation("pause"))
    projection_when_paused = action_executor.project()
    resumed = action_executor.execute(invocation("resume"))

    assert paused["observed_effect"]["state"] == "succeeded"
    assert projection_when_paused["phase"] == PHASE_PAUSED
    assert projection_when_paused["capabilities"]["resume"] == {
        "state": "available",
        "reason": None,
    }
    assert resumed["observed_effect"]["state"] == "succeeded"
    assert action_executor.project()["phase"] == "HISTORICAL"
    assert [(name, value) for name, value in jobs.patches] == [
        (reader_job, True),
        (history_job, True),
        (reader_job, False),
        (history_job, False),
    ]
    assert action_executor.supports(invocation("pause")) is True
    assert action_executor.supports(invocation("resume")) is False


def test_a_suspend_failure_is_reported_with_a_closed_code(tmp_path: Path) -> None:
    stub = StubPauseRuntime(outcome=outcome("", error_code=CODE_SUSPEND_FAILED), paused=(False, None))
    action_executor = prepared_executor(tmp_path, stub)

    result = action_executor.execute(invocation("pause"))

    assert result["execution"]["code"] == CODE_SUSPEND_FAILED
    assert result["intent"]["state"] == "recorded"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_actions_never_leak_identifiers(tmp_path: Path, action: str) -> None:
    stub = StubPauseRuntime(outcome=outcome(PHASE_PAUSED))
    action_executor = prepared_executor(tmp_path, stub)
    state = action_executor._prepare_store.load()  # noqa: SLF001 - harnais de test
    assert state is not None
    intent_id = state["intent_id"]
    reader_id = state["receipt"]["reader_id"]

    projection = action_executor.project()
    encoded = str(projection)
    assert intent_id not in encoded
    assert reader_id not in encoded
    assert str(action_executor.execute(invocation(action))) .find(intent_id) == -1
