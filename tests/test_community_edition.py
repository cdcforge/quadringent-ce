"""L'édition communautaire garde les protections runtime, sans quota commercial."""

from pathlib import Path

import pytest

from quadringent.site_config import uninstall
from quadringent_control_plane.fleet_action_executor import FleetActionExecutor
from quadringent_control_plane.fleet_plan import build_fleet_plan, parse_fleet_catalog
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from site_fixture import install_test_site
from test_fleet_action_executor import (
    FakeHistoryLauncher,
    FakeProvider,
    FakeReaderLauncher,
    _invocation,
    _table,
    catalog_payload,
)


def _executor(tmp_path: Path, names: tuple[str, ...], *, provider=None):
    payload = catalog_payload()
    payload["tables"] = [_table(name, "JRNLIB1", "DEMOJRN") for name in names]
    plan = build_fleet_plan(parse_fleet_catalog(payload))
    reader, history = FakeReaderLauncher(), FakeHistoryLauncher()
    return FleetActionExecutor(
        plan,
        AtomicJsonStateStore(tmp_path / "prepare.json"),
        AtomicJsonStateStore(tmp_path / "history.json"),
        provider if provider is not None else FakeProvider(),
        reader,
        history,
    ), reader, history


@pytest.mark.parametrize("table_count", (5, 6, 13, 64))
@pytest.mark.parametrize("legacy_token", ("", "ancien-jeton-invalide"))
def test_preparer_puis_demarrer_sans_licence(tmp_path, monkeypatch, table_count, legacy_token):
    """La préparation et le lancement couvrent toute la flotte, même au-delà de cinq tables."""
    monkeypatch.setenv("QUADRINGENT_LICENSE_KEY", legacy_token)
    names = tuple(f"TABLE{i:03d}" for i in range(table_count))
    install_test_site(fleet_tables=names, proof_table=names[0], keyed_tables=(), provisioned_stages=(), reservable_tables=(names[0],))
    try:
        executor, reader, history = _executor(tmp_path, names)
        prepared = executor.execute(_invocation("prepare"))
        assert prepared["observed_effect"]["state"] == "succeeded"
        assert len(reader.calls) == 1
        assert reader.calls[0].manifest == names
        started = executor.execute(_invocation("start"))
        assert started["observed_effect"]["state"] == "succeeded"
        assert len(history.calls) == 1
        assert len(executor.project()["table_states"]) == table_count
    finally:
        uninstall()


def test_le_lancement_exige_toujours_une_preparation(tmp_path):
    names = tuple(f"TABLE{i:03d}" for i in range(13))
    install_test_site(fleet_tables=names, proof_table=names[0], keyed_tables=(), provisioned_stages=(), reservable_tables=(names[0],))
    try:
        executor, reader, history = _executor(tmp_path, names)
        result = executor.execute(_invocation("start"))
        assert result["execution"]["state"] == "failed"
        assert result["execution"]["code"] == "not_prepared"
        assert not reader.calls and not history.calls
    finally:
        uninstall()


def test_un_echec_de_checkpoint_ne_lance_aucun_lecteur(tmp_path):
    names = tuple(f"TABLE{i:03d}" for i in range(13))
    install_test_site(fleet_tables=names, proof_table=names[0], keyed_tables=(), provisioned_stages=(), reservable_tables=(names[0],))
    try:
        executor, reader, history = _executor(tmp_path, names, provider=FakeProvider(error=OSError("indisponible")))
        result = executor.execute(_invocation("prepare"))
        assert result["execution"]["state"] == "failed"
        assert result["execution"]["code"] == "checkpoint_unavailable"
        assert not reader.calls and not history.calls
    finally:
        uninstall()
