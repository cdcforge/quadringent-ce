"""Une CI partielle, étrangère ou ancienne n'autorise aucune image de release."""
from __future__ import annotations

from copy import deepcopy

import pytest
import yaml
from pathlib import Path

from scripts import check_release_ci as gate

REPO = "example/quadringent"
SHA = "a" * 40


def run():
    return {"id": 100, "run_attempt": 2, "head_sha": SHA, "created_at": "2026-01-01T00:00:00Z",
            "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
            "path": ".github/workflows/ci.yml", "event": "push", "head_branch": "main",
            "status": "completed", "conclusion": "success"}


def pages():
    return [{"total_count": 7, "jobs": [
        {"id": number, "run_id": 100, "head_sha": SHA, "name": name,
         "status": "completed", "conclusion": "success"}
        for number, name in enumerate(sorted(gate.REQUIRED_JOBS), start=1)]}]


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("head_branch", "other"),
    ("event", "pull_request"), ("path", ".github/workflows/other.yml"),
    ("status", "in_progress"), ("conclusion", "failure"), ("run_attempt", 0),
    ("repository", {"full_name": "other/quadringent"}),
    ("head_repository", {"full_name": "fork/quadringent"})])
def test_foreign_or_unfinished_ci_is_rejected(field, value):
    candidate = run()
    candidate[field] = value
    with pytest.raises(ValueError):
        gate.validate_run(candidate, REPO, SHA)


@pytest.mark.parametrize("mutation", ("missing", "skipped", "failure", "duplicate", "foreign", "old"))
def test_every_required_job_must_prove_success_for_same_run_and_sha(mutation):
    evidence = pages()
    first = evidence[0]["jobs"][0]
    if mutation == "missing":
        evidence[0]["jobs"].pop()
    elif mutation in {"skipped", "failure"}:
        first["conclusion"] = mutation
    elif mutation == "duplicate":
        evidence[0]["jobs"][-1] = deepcopy(first)
    elif mutation == "foreign":
        first["run_id"] = 200
    else:
        first["head_sha"] = "b" * 40
    with pytest.raises(ValueError):
        gate.validate_jobs(run(), evidence)


def test_gate_observes_existing_attempt_without_starting_or_retrying_ci(monkeypatch):
    calls = []
    def api(endpoint, *, paginated=False):
        calls.append(endpoint)
        if "workflows/ci.yml/runs?" in endpoint:
            return [{"workflow_runs": [run()]}]
        if "/attempts/2/jobs?" in endpoint:
            return pages()
        return run()
    monkeypatch.setattr(gate, "api", api)
    result = gate.check(REPO, SHA)
    assert result["ci_run_id"] == 100 and result["source_sha"] == SHA
    assert len(result["checks"]) == 7
    assert len(calls) == 4 and all("rerun" not in call for call in calls)


@pytest.mark.parametrize("status,conclusion", [("completed", "failure"), ("in_progress", None)])
def test_previous_success_cannot_hide_a_later_failure_or_active_ci(monkeypatch, status, conclusion):
    newer = {**run(), "id": 101, "created_at": "2026-01-01T00:01:00Z",
             "status": status, "conclusion": conclusion}
    monkeypatch.setattr(gate, "api", lambda *args, **kwargs: [{"workflow_runs": [run(), newer]}])
    with pytest.raises(ValueError):
        gate.check(REPO, SHA)


def test_ci_started_during_verification_is_rejected(monkeypatch):
    observations = 0
    def api(endpoint, *, paginated=False):
        nonlocal observations
        if "workflows/ci.yml/runs?" in endpoint:
            observations += 1
            evidence = [run()]
            if observations > 1:
                evidence.append({**run(), "id": 101, "created_at": "2026-01-01T00:01:00Z"})
            return [{"workflow_runs": evidence}]
        if "/attempts/2/jobs?" in endpoint:
            return pages()
        return run()
    monkeypatch.setattr(gate, "api", api)
    with pytest.raises(ValueError, match="nouvelle CI"):
        gate.check(REPO, SHA)


def test_attempt_change_during_observation_is_rejected(monkeypatch):
    def api(endpoint, *, paginated=False):
        if "workflows/ci.yml/runs?" in endpoint:
            return [{"workflow_runs": [run()]}]
        if "/attempts/2/jobs?" in endpoint:
            return pages()
        return {**run(), "run_attempt": 3}
    monkeypatch.setattr(gate, "api", api)
    with pytest.raises(ValueError, match="tentative CI"):
        gate.check(REPO, SHA)


@pytest.mark.parametrize("change", [{"run_attempt": 3}, {"status": "in_progress", "conclusion": None}])
def test_final_observation_rejects_restart_of_the_same_run(monkeypatch, change):
    observations = 0
    def api(endpoint, *, paginated=False):
        nonlocal observations
        if "workflows/ci.yml/runs?" in endpoint:
            observations += 1
            candidate = run() if observations == 1 else {**run(), **change}
            return [{"workflow_runs": [candidate]}]
        if "/attempts/2/jobs?" in endpoint:
            return pages()
        return run()
    monkeypatch.setattr(gate, "api", api)
    with pytest.raises(ValueError):
        gate.check(REPO, SHA)


def test_absent_ci_does_not_launch_paid_work(monkeypatch):
    monkeypatch.setattr(gate, "api", lambda *args, **kwargs: [{"workflow_runs": []}])
    with pytest.raises(ValueError, match="aucune CI"):
        gate.check(REPO, SHA)


def test_release_reuses_exact_ci_before_any_build():
    workflow = yaml.safe_load(Path(".github/workflows/release.yml").read_text())
    jobs = workflow["jobs"]
    assert not any(job.get("uses", "").endswith("ci.yml") for job in jobs.values())
    assert jobs["images"]["needs"] == ["validate"]
    assert jobs["chart"]["needs"] == ["validate"]
    validate = jobs["validate"]
    assert validate["permissions"] == {"contents": "read", "actions": "read"}
    step = next(step for step in validate["steps"] if "check_release_ci.py" in step.get("run", ""))
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert '--repository "$GITHUB_REPOSITORY" --sha "$GITHUB_SHA"' in step["run"]
