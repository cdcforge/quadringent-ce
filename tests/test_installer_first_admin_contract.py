"""Contrat réel entre l'installeur et le control plane v2 pour le premier admin.

Constaté sur GKE le 23 septembre 2026 : le script exécuté dans le Pod postait
``{}`` sans ``Idempotency-Key`` ; l'API répondait 400 et l'installeur
n'affichait jamais de lien d'activation. Ce test exécute le script généré,
tel quel, contre l'app v2 réelle servie par uvicorn en loopback.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn

from quadringent.installer.plan import InstallInputs, InvalidInstallInputs, first_admin_script
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def v2_port(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'first_admin.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()), token_pepper=b"pepper-test")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        engine.dispose()


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)


def test_generated_script_obtains_a_real_activation_token(v2_port: int) -> None:
    result = _run(first_admin_script("admin@example.com", "quadringent-install-demo", port=v2_port))
    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(result.stdout)["after"]
    assert after["email"] == "admin@example.com"
    assert after["activation_token"]


def test_rerun_preserves_user_metadata_without_reissuing_activation(v2_port: int) -> None:
    script = first_admin_script("admin@example.com", "quadringent-install-demo", port=v2_port)
    first, second = _run(script), _run(script)
    assert first.returncode == second.returncode == 0, second.stdout
    first_after, replay_after = json.loads(first.stdout)["after"], json.loads(second.stdout)["after"]
    assert first_after["activation_token"]
    assert "activation_token" not in replay_after
    assert replay_after["id"] == first_after["id"]


@pytest.mark.parametrize("email", ["pas-un-email", "a@b'); import os; os.system('x')#", "x" * 321 + "@e.com"])
def test_admin_email_is_validated_before_reaching_a_script(email: str) -> None:
    with pytest.raises(InvalidInstallInputs):
        InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int", admin_email=email)
