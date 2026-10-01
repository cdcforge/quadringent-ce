"""Fixtures partagées — notamment le Postgres éphémère des tests marqués
``@pytest.mark.postgres`` (control plane v2, chantier 3).

Un seul conteneur Postgres 16 est démarré pour toute la session pytest
(port aléatoire libre), et arrêté à la fin. Docker est attendu disponible
dans cet environnement : l'absence de Docker fait échouer explicitement la
collecte des tests Postgres plutôt que de les sauter silencieusement,
conformément à la consigne (« pas de skip silencieux »). Un skip explicite
n'intervient que si l'administrateur a positionné
``QUADRINGENT_TEST_ALLOW_SKIP_POSTGRES=1``, échappatoire volontaire et
visible pour un poste sans Docker.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: nécessite un Postgres 16 réel démarré via Docker"
    )


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _docker_binary() -> str | None:
    return shutil.which("docker")


def _wait_until_ready(docker: str, container: str, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            [docker, "exec", container, "pg_isready", "-U", "quadringent"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("Postgres de test n'est jamais devenu prêt (pg_isready)")


def _wait_until_reachable(dsn: str, timeout_seconds: float = 30.0) -> None:
    """``pg_isready`` dans le conteneur ne garantit pas la joignabilité côté
    hôte (mapping de port pas encore stable) : on confirme par une vraie
    connexion avant de rendre la main aux tests.
    """

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            engine = create_engine(dsn, future=True)
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                return
            finally:
                engine.dispose()
        except OperationalError as error:
            last_error = error
            time.sleep(0.5)
    raise RuntimeError(f"Postgres de test injoignable depuis l'hôte : {last_error}")


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    docker = _docker_binary()
    if docker is None:
        if os.environ.get("QUADRINGENT_TEST_ALLOW_SKIP_POSTGRES") == "1":
            pytest.skip(
                "Docker indisponible et QUADRINGENT_TEST_ALLOW_SKIP_POSTGRES=1 : "
                "skip explicite des tests Postgres"
            )
        raise RuntimeError(
            "Docker est requis pour les tests marqués @pytest.mark.postgres. "
            "Positionner QUADRINGENT_TEST_ALLOW_SKIP_POSTGRES=1 pour un skip "
            "explicite sur un poste sans Docker."
        )
    port = _free_tcp_port()
    container = f"quadringent-v2-test-pg-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-e",
            "POSTGRES_USER=quadringent",
            "-e",
            "POSTGRES_PASSWORD=quadringent-test",
            "-e",
            "POSTGRES_DB=quadringent",
            "-p",
            f"127.0.0.1:{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    try:
        _wait_until_ready(docker, container)
        dsn = f"postgresql+psycopg://quadringent:quadringent-test@127.0.0.1:{port}/quadringent"
        _wait_until_reachable(dsn)
        yield dsn
    finally:
        subprocess.run([docker, "rm", "-f", container], capture_output=True)
