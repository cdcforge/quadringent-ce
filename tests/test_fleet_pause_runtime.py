from __future__ import annotations

import pytest
from pathlib import Path

from quadringent_control_plane.fleet import ENVIRONMENT as FLEET_ENVIRONMENT, FleetError
from quadringent_control_plane.fleet_job_launcher import (
    KIND_HISTORY,
    KIND_READER,
    LABEL_ENVIRONMENT,
    LABEL_INTENT,
    LABEL_KIND,
    ENVIRONMENT as JOB_ENVIRONMENT,
)
from quadringent_control_plane.fleet_pause_runtime import (
    CODE_ALREADY_PAUSED,
    CODE_INVALID_RUNTIME_STATE,
    CODE_JOB_UNAVAILABLE,
    CODE_NEEDS_RECOVERY,
    CODE_NOT_PAUSED,
    CODE_NOT_PREPARED,
    CODE_SUSPEND_FAILED,
    PHASE_PAUSED,
    PHASE_RESUMED,
    FleetPauseRuntime,
)
from quadringent_control_plane.fleet_history_runtime import (
    HISTORY_FORMAT_VERSION,
    PHASE_HISTORICAL,
)
from quadringent_control_plane.fleet_prepare_runtime import PREPARE_FORMAT_VERSION, PHASE_PREPARED
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from quadringent_control_plane.k8s_jobs import CODE_UNAVAILABLE, JobsApiError, KubernetesJobsClient


NAMESPACE = "quadringent-demo"
CHECKPOINT = {"receiver": "DEMOJRN0100", "sequence": 250}
PREPARE_INTENT = "4f2a1c9b8d1e4a7f9c3b5d6e8a0f1c2d"
HISTORY_INTENT = "9c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f"
READER_JOB = "quadringent-reader-abcdef123456"
HISTORY_JOB = "quadringent-history-fedcba654321"


class FakeJobs:
    """API Jobs simulée : Jobs présents, suspension mémorisée."""

    def __init__(self, jobs: dict[str, dict[str, object]] | None = None) -> None:
        self.jobs = jobs if jobs is not None else {}
        self.patches: list[tuple[str, bool]] = []
        self.fail_patch: str | None = None
        self.ignore_patch = False

    def __call__(self, method: str, path: str, body: bytes | None, content_type: str = ""):
        from quadringent_control_plane.k8s_jobs import JobsResponse

        name = path.rsplit("/", 1)[-1]
        if method == "GET":
            job = self.jobs.get(name)
            return JobsResponse(status=200, body=job) if job else JobsResponse(status=404, body=None)
        if method == "PATCH":
            if self.fail_patch is not None:
                raise JobsApiError(self.fail_patch, "API injoignable")
            suspend = "true" in (body or b"").decode("utf-8")
            self.patches.append((name, suspend))
            if not self.ignore_patch:
                job = self.jobs.get(name)
                if job is not None:
                    job["spec"] = {**job.get("spec", {}), "suspend": suspend}  # type: ignore[dict-item]
            return JobsResponse(status=200, body=self.jobs.get(name, {}))
        raise AssertionError(method)


def job_document(kind: str, intent: str, *, suspend: bool = False) -> dict[str, object]:
    return {
        "metadata": {
            "name": READER_JOB if kind == KIND_READER else HISTORY_JOB,
            "labels": {LABEL_KIND: kind, LABEL_INTENT: intent, LABEL_ENVIRONMENT: JOB_ENVIRONMENT},
        },
        "spec": {"suspend": suspend} if suspend else {},
    }


def prepare_state(*, receipt_reader: str = READER_JOB) -> dict[str, object]:
    return {
        "format_version": PREPARE_FORMAT_VERSION,
        "environment": FLEET_ENVIRONMENT,
        "phase": PHASE_PREPARED,
        "intent_id": PREPARE_INTENT,
        "manifest": [],
        "checkpoint": dict(CHECKPOINT),
        "journal_library": "JRNLIB1",
        "journal_name": "DEMOJRN",
        "reader_kind": "multi_object",
        "reader_count": 1,
        "receipt": {"status": "RUNNING", "intent_id": PREPARE_INTENT, "reader_id": receipt_reader},
        "error_code": None,
        "needs_recovery": False,
    }


def history_state(*, receipt_orchestrator: str = HISTORY_JOB) -> dict[str, object]:
    return {
        "format_version": HISTORY_FORMAT_VERSION,
        "environment": FLEET_ENVIRONMENT,
        "phase": PHASE_HISTORICAL,
        "intent_id": HISTORY_INTENT,
        "prepare_intent_id": PREPARE_INTENT,
        "reader_id": READER_JOB,
        "checkpoint": dict(CHECKPOINT),
        "manifest": [],
        "lanes": [],
        "max_concurrency": 1,
        "receipt": {
            "status": "RUNNING",
            "intent_id": HISTORY_INTENT,
            "prepare_intent_id": PREPARE_INTENT,
            "reader_id": READER_JOB,
            "orchestrator_id": receipt_orchestrator,
        },
        "error_code": None,
        "needs_recovery": False,
    }


def store(tmp_path: Path, name: str, content: dict[str, object] | None) -> AtomicJsonStateStore:
    target = AtomicJsonStateStore(tmp_path / name)
    if content is not None:
        target.save(content)
    return target


def runtime(
    tmp_path: Path,
    jobs: FakeJobs,
    *,
    prepare: dict[str, object] | None = None,
    history: dict[str, object] | None = None,
) -> FleetPauseRuntime:
    return FleetPauseRuntime(
        store(tmp_path, "prepare.json", prepare),
        store(tmp_path, "history.json", history),
        store(tmp_path, "pause.json", None),
        KubernetesJobsClient(jobs, NAMESPACE),
    )


def test_pause_suspends_only_the_jobs_of_the_recorded_intent(tmp_path: Path) -> None:
    jobs = FakeJobs(
        {
            READER_JOB: job_document(KIND_READER, PREPARE_INTENT),
            HISTORY_JOB: job_document(KIND_HISTORY, HISTORY_INTENT),
        }
    )
    fleet = runtime(tmp_path, jobs, prepare=prepare_state(), history=history_state())

    outcome = fleet.pause()

    assert outcome.error_code is None
    assert outcome.phase == PHASE_PAUSED
    assert set(outcome.suspended) == {READER_JOB, HISTORY_JOB}
    assert [name for name, _value in jobs.patches] == [READER_JOB, HISTORY_JOB]
    assert all(value is True for _name, value in jobs.patches)


def test_pause_covers_a_prepared_fleet_without_history(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    outcome = fleet.pause()

    assert outcome.error_code is None
    assert outcome.suspended == (READER_JOB,)
    assert outcome.history_id is None


def test_pause_is_idempotent(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    first = fleet.pause()
    second = fleet.pause()

    assert first.error_code is None
    assert second.error_code is None
    assert second.already_suspended == (READER_JOB,)
    assert len(jobs.patches) == 1


def test_pause_without_prepare_is_refused(tmp_path: Path) -> None:
    fleet = runtime(tmp_path, FakeJobs())

    outcome = fleet.pause()

    assert outcome.error_code == CODE_NOT_PREPARED
    assert outcome.needs_recovery is False


def test_pause_refuses_a_missing_job(tmp_path: Path) -> None:
    fleet = runtime(tmp_path, FakeJobs(), prepare=prepare_state())

    outcome = fleet.pause()

    assert outcome.error_code == CODE_JOB_UNAVAILABLE


def test_pause_refuses_a_job_from_another_intent(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, "a" * 32)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    outcome = fleet.pause()

    assert outcome.error_code == CODE_NEEDS_RECOVERY
    assert jobs.patches == []


def test_pause_refuses_a_kind_mismatch(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_HISTORY, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    assert fleet.pause().error_code == CODE_NEEDS_RECOVERY


def test_suspension_without_observed_effect_is_a_failure(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    jobs.ignore_patch = True
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    outcome = fleet.pause()

    assert outcome.error_code == CODE_SUSPEND_FAILED
    assert outcome.needs_recovery is False


def test_a_transport_failure_never_reports_success(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    jobs.fail_patch = CODE_UNAVAILABLE
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    assert fleet.pause().error_code == CODE_SUSPEND_FAILED


def test_history_state_without_matching_prepare_is_refused(tmp_path: Path) -> None:
    history = history_state()
    history["prepare_intent_id"] = "0" * 32
    jobs = FakeJobs(
        {
            READER_JOB: job_document(KIND_READER, PREPARE_INTENT),
            HISTORY_JOB: job_document(KIND_HISTORY, HISTORY_INTENT),
        }
    )
    fleet = runtime(tmp_path, jobs, prepare=prepare_state(), history=history)

    outcome = fleet.pause()

    assert outcome.error_code == CODE_NEEDS_RECOVERY
    assert jobs.patches == []


def test_resume_requires_a_recorded_pause(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())

    outcome = fleet.resume()

    assert outcome.error_code == CODE_NOT_PAUSED
    assert jobs.patches == []


def test_resume_unsuspends_and_records_the_transition(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())
    fleet.pause()

    outcome = fleet.resume()

    assert outcome.error_code is None
    assert outcome.phase == PHASE_RESUMED
    assert jobs.patches == [(READER_JOB, True), (READER_JOB, False)]
    assert fleet.is_paused() == (False, None)


def test_resume_refuses_when_the_job_disappeared(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())
    fleet.pause()
    del jobs.jobs[READER_JOB]

    outcome = fleet.resume()

    assert outcome.error_code == CODE_JOB_UNAVAILABLE


def test_resume_refuses_a_new_intent_while_paused(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    fleet = runtime(tmp_path, jobs, prepare=prepare_state())
    fleet.pause()

    renewed = prepare_state()
    renewed["intent_id"] = "b" * 32
    renewed["receipt"] = {"status": "RUNNING", "intent_id": "b" * 32, "reader_id": READER_JOB}
    renewed_fleet = FleetPauseRuntime(
        store(tmp_path, "prepare.json", renewed),
        store(tmp_path, "history.json", None),
        store(tmp_path, "pause.json", fleet_state(tmp_path)),
        KubernetesJobsClient(jobs, NAMESPACE),
    )

    assert renewed_fleet.resume().error_code == CODE_NEEDS_RECOVERY


def fleet_state(tmp_path: Path) -> dict[str, object]:
    return AtomicJsonStateStore(tmp_path / "pause.json").load() or {}


def test_a_corrupted_pause_state_is_never_trusted(tmp_path: Path) -> None:
    jobs = FakeJobs({READER_JOB: job_document(KIND_READER, PREPARE_INTENT)})
    corrupted = AtomicJsonStateStore(tmp_path / "pause.json")
    corrupted.save({"phase": PHASE_PAUSED})

    fleet = FleetPauseRuntime(
        store(tmp_path, "prepare.json", prepare_state()),
        store(tmp_path, "history.json", None),
        corrupted,
        KubernetesJobsClient(jobs, NAMESPACE),
    )

    outcome = fleet.pause()

    assert outcome.error_code == CODE_INVALID_RUNTIME_STATE
    assert outcome.needs_recovery is True
    assert jobs.patches == []


def test_runtime_requires_complete_stores_and_a_real_client(tmp_path: Path) -> None:
    with pytest.raises(FleetError):
        FleetPauseRuntime(object(), object(), object(), object())
    with pytest.raises(FleetError):
        FleetPauseRuntime(
            store(tmp_path, "prepare.json", None),
            store(tmp_path, "history.json", None),
            store(tmp_path, "pause.json", None),
            object(),
        )
