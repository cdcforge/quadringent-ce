"""Émission d'un jeton d'agent par ``kubectl exec`` (accès opérateur au cluster).

Un agent doit pouvoir piloter une installation sans session humaine : qui a
déjà ``kubectl exec`` sur le Pod control plane en est de fait administrateur,
il peut donc émettre un jeton borné dans le temps. Le script généré est
exécuté tel quel contre une vraie base v2 ; le jeton n'est jamais affiché,
seulement écrit dans un fichier ``0600``.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from quadringent.installer.cli import run
from quadringent.installer.plan import agent_token_script
from quadringent.installer.runner import CommandResult
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService
from quadringent_control_plane.v2.services.audit import AuditService

ROOT = Path(__file__).resolve().parents[1]


def test_generated_script_issues_a_token_the_service_accepts(tmp_path: Path) -> None:
    dsn = f"sqlite:///{tmp_path / 'tokens.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "qqual", "name": "qqual"})
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "src"),
        "QUADRINGENT_V2_DATABASE_URL": dsn,
        "QUADRINGENT_V2_TOKEN_PEPPER": "pepper-test",
        "QUADRINGENT_SITE_ID": "qqual",
    }
    # Même nom que le paquet installé : le shim local du conteneur ne doit
    # pas masquer les services v2 lors d'une exécution depuis /app.
    (tmp_path / "quadringent_control_plane.py").write_text("# Shim local de test.\n")
    script = agent_token_script("agent-qualification", "admin", days=7)
    unsafe = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path,
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert unsafe.returncode != 0
    assert "'quadringent_control_plane' is not a package" in unsafe.stderr
    result = subprocess.run(
        [sys.executable, "-P", "-c", script], cwd=tmp_path,
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    record = AgentTokensService(engine, org_id="qqual", pepper=b"pepper-test").authenticate(body["token"])
    assert (record.name, record.scope) == ("agent-qualification", "admin")
    assert record.expires_at is not None
    # Visible par l'admin dans le journal d'audit, comme une émission par l'API.
    [entry] = AuditService(engine, org_id="qqual").query(action="agent_token.create")
    assert (entry.actor_id, entry.resource_id, entry.status) == ("kubectl-exec", record.id, "succeeded")
    engine.dispose()


class _ScriptedRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, env=None, cwd=None):  # noqa: ANN001
        self.calls.append(tuple(argv))
        return CommandResult(tuple(argv), 0, self.stdout, "")


def test_cli_writes_a_private_config_and_never_prints_the_token(tmp_path: Path) -> None:
    config = tmp_path / "agent.json"
    runner = _ScriptedRunner(json.dumps({"id": "tok1", "token": "qdt_ad_secret-value"}))
    out = io.StringIO()
    code = run([
        "agent-token", "--name", "qqual", "--namespace", "quadringent-qual",
        "--label", "agent-qualification", "--scope", "admin", "--days", "7",
        "--url", "http://localhost:8844", "--write-config", str(config),
    ], runner=runner, stdout=out)
    assert code == 0
    assert "qdt_ad_secret-value" not in out.getvalue()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert json.loads(config.read_text()) == {"url": "http://localhost:8844", "token": "qdt_ad_secret-value"}
    argv = runner.calls[0]
    assert argv[:10] == ("kubectl", "-n", "quadringent-qual", "exec", "deployment/qqual-quadringent-control-plane",
                         "-c", "control-plane-v2", "--", "python", "-P")


def _issue_to(config: Path):
    runner = _ScriptedRunner(json.dumps({"id": "synthetic-id", "token": "SYNTHETIC_TEST_ONLY"}))
    out = io.StringIO()
    code = run([
        "agent-token", "--name", "qqual", "--namespace", "quadringent-qual",
        "--label", "bootstrap-recovery", "--scope", "admin", "--days", "1",
        "--url", "http://localhost:8844", "--write-config", str(config),
    ], runner=runner, stdout=out)
    assert "SYNTHETIC_TEST_ONLY" not in out.getvalue()
    return code, runner, out.getvalue()


def test_existing_open_config_is_replaced_only_after_private_write_and_close(tmp_path, monkeypatch):
    from quadringent.installer import cli

    config = tmp_path / "agent.json"
    config.write_text("ANCIEN_FICHIER")
    config.chmod(0o644)
    original_dump, original_replace = cli.json.dump, cli.os.replace
    observed = []

    def private_dump(data, handle):
        assert stat.S_IMODE(cli.os.fstat(handle.fileno()).st_mode) == 0o600
        assert config.read_text() == "ANCIEN_FICHIER"
        observed.append(handle)
        original_dump(data, handle)

    def atomic_replace(source, destination):
        assert observed[0].closed
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert config.read_text() == "ANCIEN_FICHIER"
        original_replace(source, destination)

    monkeypatch.setattr(cli.json, "dump", private_dump)
    monkeypatch.setattr(cli.os, "replace", atomic_replace)
    code, _runner, _out = _issue_to(config)
    assert code == 0 and observed[0].closed
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert json.loads(config.read_text())["url"] == "http://localhost:8844"
    assert list(tmp_path.iterdir()) == [config]


def test_symlink_config_is_refused_without_touching_its_private_target(tmp_path):
    target = tmp_path / "autre-site.json"
    target.write_text("AUTRE_SITE")
    target.chmod(0o600)
    config = tmp_path / "agent.json"
    config.symlink_to(target)
    code, runner, _out = _issue_to(config)
    assert code != 0
    assert not runner.calls
    assert config.is_symlink() and target.read_text() == "AUTRE_SITE"


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_output_failure_preserves_existing_config_and_cleans_temporary(tmp_path, monkeypatch, failure):
    from quadringent.installer import cli

    config = tmp_path / "agent.json"
    config.write_text("ANCIEN_FICHIER")
    config.chmod(0o644)

    def fail_operation(*_args):
        raise OSError("échec synthétique de sortie")

    def partial_write(_data, handle):
        handle.write("ECRITURE_PARTIELLE")
        fail_operation()

    if failure == "write":
        monkeypatch.setattr(cli.json, "dump", partial_write)
    else:
        monkeypatch.setattr(cli.os, failure, fail_operation)
    code, _runner, _out = _issue_to(config)
    assert code != 0
    assert config.read_text() == "ANCIEN_FICHIER"
    assert stat.S_IMODE(config.stat().st_mode) == 0o644
    assert list(tmp_path.iterdir()) == [config]
