"""Relevé du catalogue de flotte : mesure durable, plan échangé seulement sûr."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import site_fixture
from test_fleet_action_executor import (
    _executor,
    _invocation,
    catalog_payload,
    make_plan,
)

from quadringent_control_plane.fleet_action_executor import (  # noqa: E402
    PLAN_ACTION_IN_FLIGHT,
    PLAN_APPLIED,
    PLAN_INVALID,
    PLAN_KEPT_INCOMPATIBLE,
)
from quadringent_control_plane.fleet_catalog_refresh import (  # noqa: E402
    CatalogRefreshOutcome,
    FleetCatalogRefresher,
    seed_fleet_state,
)
from quadringent_control_plane.fleet_plan import (  # noqa: E402
    FleetPlan,
    build_fleet_plan,
    parse_fleet_catalog,
)
from quadringent_control_plane.fleet_sidecar import (  # noqa: E402
    build_fleet_ui_sidecar,
    load_fleet_catalog_file,
    parse_fleet_ui_sidecar,
)


ROOT = Path(__file__).resolve().parents[1]
SECRET = "super-secret-token-value"
SCRIPT_PATH = ROOT / "src" / "quadringent_control_plane" / "cli.py"


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["java"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _refresher(tmp_path: Path, runner, **kwargs: object) -> FleetCatalogRefresher:
    return FleetCatalogRefresher(
        state_directory=tmp_path,
        java="java",
        classpath="probe.jar:lib/*",
        runner=runner,
        **kwargs,
    )


def _ok_runner(payload: dict[str, object] | None = None):
    document = catalog_payload() if payload is None else payload
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return _completed(stdout=json.dumps(document))

    runner.calls = calls
    return runner


def _load_script() -> ModuleType:
    """Charge le script par chemin ; son retrait de sys.path est neutralisé."""

    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("control_plane_script", SCRIPT_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved


def test_probe_success_writes_catalog_and_sidecar_atomically(tmp_path: Path) -> None:
    runner = _ok_runner()
    refresher = _refresher(tmp_path, runner)

    outcome = refresher.refresh()

    assert outcome.status == "refreshed"
    assert outcome.reason is None
    assert type(outcome.plan) is FleetPlan
    assert outcome.catalog_written and outcome.sidecar_written
    assert outcome.observed_at == "2026-09-13T12:00:00Z"
    assert outcome.receiver_count == 2
    assert outcome.continuity == "uncertain"
    command = runner.calls[0]
    assert command[:2] == ["java", "-cp"]
    assert command[-1] == "io.quadringent.as400.FleetCatalogProbe"
    catalog = load_fleet_catalog_file(tmp_path / "fleet-catalog.json")
    assert catalog.observed_at == outcome.observed_at
    sidecar_payload = json.loads((tmp_path / "fleet-sidecar.json").read_text("utf-8"))
    sidecar = parse_fleet_ui_sidecar(sidecar_payload)
    assert sidecar["fleet"]["observed_at"].startswith("2026-09-13T12:00:00")


def test_probe_failure_keeps_last_good_and_leaks_nothing(tmp_path: Path) -> None:
    refresher = _refresher(
        tmp_path,
        lambda *a, **k: _completed(
            returncode=1,
            stderr=f"fleet_catalog_error=SOURCE_UNREACHABLE password={SECRET}",
        ),
    )

    outcome = refresher.refresh()

    assert outcome.status == "kept"
    assert outcome.reason == "source_unreachable"
    assert outcome.plan is None
    assert not (tmp_path / "fleet-catalog.json").exists()
    assert not (tmp_path / "fleet-sidecar.json").exists()
    encoded = json.dumps(outcome.__dict__, default=str)
    assert SECRET not in encoded


def test_probe_failure_without_marker_is_a_safe_code(tmp_path: Path) -> None:
    refresher = _refresher(
        tmp_path, lambda *a, **k: _completed(returncode=3, stderr=f"boom {SECRET}")
    )
    outcome = refresher.refresh()
    assert outcome.status == "kept"
    assert outcome.reason == "catalog_probe_failed"


def test_probe_timeout_and_missing_binary_are_kept(tmp_path: Path) -> None:
    def timeout_runner(*a, **k):
        raise subprocess.TimeoutExpired(cmd="java", timeout=1)

    outcome = _refresher(tmp_path, timeout_runner).refresh()
    assert outcome.status == "kept"
    assert outcome.reason == "catalog_probe_timeout"

    def missing_runner(*a, **k):
        raise FileNotFoundError("java")

    outcome = _refresher(tmp_path, missing_runner).refresh()
    assert outcome.status == "kept"
    assert outcome.reason == "catalog_probe_unavailable"


def test_invalid_probe_output_is_kept(tmp_path: Path) -> None:
    refresher = _refresher(tmp_path, lambda *a, **k: _completed(stdout="{not-json"))
    outcome = refresher.refresh()
    assert outcome.status == "kept"
    assert outcome.reason == "invalid_catalog"

    wrong_environment = catalog_payload()
    wrong_environment["environment"] = "prod"
    outcome = _refresher(tmp_path, _ok_runner(wrong_environment)).refresh()
    assert outcome.status == "kept"
    assert outcome.reason == "invalid_environment"
    assert not (tmp_path / "fleet-catalog.json").exists()


def test_write_failure_keeps_last_good(tmp_path: Path) -> None:
    # Un répertoire à la place du catalogue force l'échec de l'écriture atomique.
    (tmp_path / "fleet-catalog.json").mkdir()
    outcome = _refresher(tmp_path, _ok_runner()).refresh()
    assert outcome.status == "kept"
    assert outcome.reason == "catalog_write_failed"
    assert not (tmp_path / "fleet-sidecar.json").exists()


def test_emit_receives_bounded_outcomes(tmp_path: Path) -> None:
    seen: list[CatalogRefreshOutcome] = []
    refresher = _refresher(tmp_path, _ok_runner(), emit=seen.append)
    refresher.refresh()
    assert len(seen) == 1 and seen[0].status == "refreshed"


def test_seed_copies_configmap_files_when_state_is_absent(tmp_path: Path) -> None:
    launch = tmp_path / "launch"
    launch.mkdir()
    catalog = catalog_payload()
    (launch / "fleet-catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    sidecar = build_fleet_ui_sidecar(parse_fleet_catalog(catalog))
    (launch / "fleet-sidecar.json").write_text(json.dumps(sidecar), encoding="utf-8")
    state = tmp_path / "state"

    result = seed_fleet_state(
        catalog_source=launch / "fleet-catalog.json",
        sidecar_source=launch / "fleet-sidecar.json",
        state_directory=state,
    )

    assert result == {"catalog": "copied", "sidecar": "copied"}
    assert load_fleet_catalog_file(state / "fleet-catalog.json").observed_at
    assert parse_fleet_ui_sidecar(json.loads((state / "fleet-sidecar.json").read_text("utf-8")))


def test_seed_never_overwrites_existing_state(tmp_path: Path) -> None:
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "fleet-catalog.json").write_text(json.dumps(catalog_payload()), "utf-8")
    (launch / "fleet-sidecar.json").write_text(json.dumps({"seed": True}), "utf-8")
    state = tmp_path / "state"
    state.mkdir()
    marker = b'{"durable": true}'
    (state / "fleet-catalog.json").write_bytes(marker)
    (state / "fleet-sidecar.json").write_bytes(marker)

    result = seed_fleet_state(
        catalog_source=launch / "fleet-catalog.json",
        sidecar_source=launch / "fleet-sidecar.json",
        state_directory=state,
    )

    assert result == {"catalog": "kept", "sidecar": "kept"}
    assert (state / "fleet-catalog.json").read_bytes() == marker
    assert (state / "fleet-sidecar.json").read_bytes() == marker


def test_seed_refuses_invalid_or_missing_sources(tmp_path: Path) -> None:
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "fleet-catalog.json").write_text("{not json", "utf-8")
    state = tmp_path / "state"

    result = seed_fleet_state(
        catalog_source=launch / "fleet-catalog.json",
        sidecar_source=launch / "absent.json",
        state_directory=state,
    )

    assert result == {"catalog": "invalid", "sidecar": "missing"}
    assert not (state / "fleet-catalog.json").exists()


def test_update_plan_applies_outside_guarded_phases(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    other = make_plan(journal_name="ALTJRN", attached_name="ALTJRN0100")
    assert executor.update_plan(other) == PLAN_APPLIED
    assert executor.plan is other
    assert executor.update_plan("pas un plan") == PLAN_INVALID
    assert executor.plan is other


def test_update_plan_defers_while_an_action_is_in_flight(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    candidate = make_plan(journal_name="ALTJRN", attached_name="ALTJRN0100")
    executor._active_actions = 1
    try:
        assert executor.update_plan(candidate) == PLAN_ACTION_IN_FLIGHT
    finally:
        executor._active_actions = 0
    assert executor.plan is plan
    assert executor.update_plan(candidate) == PLAN_APPLIED


def test_update_plan_holds_incompatible_swap_when_prepared(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    assert executor.project()["phase"] == "PREPARED"

    incompatible = make_plan(journal_name="ALTJRN", attached_name="ALTJRN0100")
    assert executor.update_plan(incompatible) == PLAN_KEPT_INCOMPATIBLE
    assert executor.plan is plan
    assert executor.project()["phase"] == "PREPARED"

    compatible_payload = catalog_payload()
    compatible_payload["observed_at"] = "2026-09-13T13:00:00Z"
    compatible = build_fleet_plan(parse_fleet_catalog(compatible_payload))
    assert executor.update_plan(compatible) == PLAN_APPLIED
    assert executor.plan is compatible
    assert executor.project()["phase"] == "PREPARED"


def test_update_plan_holds_incompatible_swap_when_historical(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    executor.execute(_invocation("start"))
    assert executor.project()["phase"] == "HISTORICAL"

    different_lanes = make_plan(journal_name="ALTJRN", attached_name="ALTJRN0100")
    assert executor.update_plan(different_lanes) == PLAN_KEPT_INCOMPATIBLE
    assert executor.plan is plan
    assert executor.project()["phase"] == "HISTORICAL"


def test_refresh_action_runs_a_bounded_cycle_and_swaps(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    state_dir = tmp_path / "fleet-state"
    state_dir.mkdir()
    refresher = _refresher(state_dir, _ok_runner())
    executor.attach_catalog_refresher(refresher)

    assert executor.supports(_invocation("refresh")) is True
    assert executor.project()["capabilities"]["refresh"]["state"] == "available"

    result = executor.execute(_invocation("refresh"))

    assert result["execution"]["state"] == "completed"
    assert result["observed_effect"]["state"] == "succeeded"
    assert result["observed_effect"]["code"] == "catalog_refreshed"
    assert SECRET not in json.dumps(result)
    assert executor.plan is not plan
    assert type(executor.plan) is FleetPlan
    assert (state_dir / "fleet-catalog.json").exists()
    assert (state_dir / "fleet-sidecar.json").exists()


def test_refresh_action_reports_kept_plan(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    assert executor.project()["phase"] == "PREPARED"
    incompatible_payload = catalog_payload(
        journal_name="ALTJRN", journal_library="JRNLIB2", attached_name="ALTJRN0100"
    )
    refresher = _refresher(tmp_path / "state", _ok_runner(incompatible_payload))
    executor.attach_catalog_refresher(refresher)

    result = executor.execute(_invocation("refresh"))

    assert result["observed_effect"]["state"] == "succeeded"
    assert result["observed_effect"]["code"] == "catalog_kept"
    assert executor.plan is plan
    # La mesure reste durable même quand le plan actif est conservé.
    assert (tmp_path / "state" / "fleet-sidecar.json").exists()


def test_refresh_action_failure_keeps_plan_and_stays_safe(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)

    class _FailingRefresher:
        def refresh(self) -> CatalogRefreshOutcome:
            return CatalogRefreshOutcome(
                status="kept",
                reason="source_unreachable",
                observed_at=None,
                receiver_count=None,
                continuity=None,
                plan=None,
                catalog_written=False,
                sidecar_written=False,
            )

    executor.attach_catalog_refresher(_FailingRefresher())
    result = executor.execute(_invocation("refresh"))
    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == "source_unreachable"
    assert executor.plan is plan

    class _ExplodingRefresher:
        def refresh(self):
            raise RuntimeError(SECRET)

    executor.attach_catalog_refresher(_ExplodingRefresher())
    result = executor.execute(_invocation("refresh"))
    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == "catalog_refresh_failed"
    assert SECRET not in json.dumps(result)


def test_refresh_is_unavailable_without_a_refresher(tmp_path: Path) -> None:
    executor, *_ = _executor(tmp_path)
    assert executor.supports(_invocation("refresh")) is False
    assert executor.project()["capabilities"]["refresh"]["state"] == "unavailable"
    result = executor.execute(_invocation("refresh"))
    assert result["execution"]["state"] == "failed"
    assert result["execution"]["code"] == "unsupported_action"


def test_refresh_keeps_working_after_an_incompatible_swap(tmp_path: Path) -> None:
    executor, plan, *_ = _executor(tmp_path)
    executor.execute(_invocation("prepare"))
    assert executor.project()["phase"] == "PREPARED"
    refresher = _refresher(tmp_path / "state", _ok_runner())
    executor.attach_catalog_refresher(refresher)
    first = executor.execute(_invocation("refresh"))
    assert first["observed_effect"]["code"] == "catalog_refreshed"
    executor.execute(_invocation("start"))
    assert executor.project()["phase"] == "HISTORICAL"
    second = executor.execute(_invocation("refresh"))
    assert second["observed_effect"]["state"] == "succeeded"


def test_script_seed_and_catalog_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "fleet-catalog.json").write_text(json.dumps(catalog_payload()), "utf-8")
    sidecar = build_fleet_ui_sidecar(parse_fleet_catalog(catalog_payload()))
    (launch / "fleet-sidecar.json").write_text(json.dumps(sidecar), "utf-8")
    state = tmp_path / "state"
    arguments = argparse.Namespace(
        fleet_catalog=str(launch / "fleet-catalog.json"),
        fleet_state_dir=str(state),
    )

    monkeypatch.delenv("QUADRINGENT_FLEET_STATE_PERSISTENT", raising=False)
    script._seed_fleet_state(arguments)
    assert not (state / "fleet-catalog.json").exists()

    monkeypatch.setenv("QUADRINGENT_FLEET_STATE_PERSISTENT", "true")
    script._seed_fleet_state(arguments)
    assert (state / "fleet-catalog.json").exists()
    assert (state / "fleet-sidecar.json").exists()

    resolved = script._launch_catalog(str(launch / "fleet-catalog.json"), state)
    assert resolved == str(state / "fleet-catalog.json")
    resolved = script._launch_catalog(str(launch / "fleet-catalog.json"), tmp_path / "vide")
    assert resolved == str(launch / "fleet-catalog.json")


def test_script_builds_refresher_only_with_persistent_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script()
    executor, *_ = _executor(tmp_path)
    arguments = argparse.Namespace(fleet_state_dir=str(tmp_path / "state"))

    monkeypatch.delenv("QUADRINGENT_FLEET_STATE_PERSISTENT", raising=False)
    assert script._build_catalog_refresher(arguments, executor) is None

    monkeypatch.setenv("QUADRINGENT_FLEET_STATE_PERSISTENT", "true")
    monkeypatch.delenv("AS400_JAVA_CLASSPATH", raising=False)
    assert script._build_catalog_refresher(arguments, executor) is None
    assert executor.supports(_invocation("refresh")) is False

    monkeypatch.setenv("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    monkeypatch.delenv("QUADRINGENT_CATALOG_REFRESH_SECONDS", raising=False)
    built = script._build_catalog_refresher(arguments, executor)
    assert built is not None
    refresher, interval = built
    assert interval == 300.0
    assert executor.supports(_invocation("refresh")) is True

    monkeypatch.setenv("QUADRINGENT_CATALOG_REFRESH_SECONDS", "0")
    executor2, *_ = _executor(tmp_path / "autre")
    built = script._build_catalog_refresher(arguments, executor2)
    assert built is not None and built[1] == 0.0
    assert executor2.supports(_invocation("refresh")) is True


def test_catalog_refresh_loop_swaps_then_stops(tmp_path: Path) -> None:
    script = _load_script()
    executor, plan, *_ = _executor(tmp_path)
    refresher = _refresher(tmp_path / "state", _ok_runner())

    class _StopAfterCycles:
        def __init__(self, cycles: int) -> None:
            self.calls = 0
            self.cycles = cycles

        def wait(self, seconds: float) -> bool:
            self.calls += 1
            return self.calls > self.cycles

    script._catalog_refresh_loop(refresher, executor, _StopAfterCycles(1), 300.0)
    assert executor.plan is not plan

    # Un relevé qui lève ne tue jamais la boucle : le dernier plan valide reste.
    def boom(*a, **k):
        raise RuntimeError(SECRET)

    failing = _refresher(tmp_path / "autre", boom)
    current = executor.plan
    script._catalog_refresh_loop(failing, executor, _StopAfterCycles(2), 1.0)
    assert executor.plan is current
