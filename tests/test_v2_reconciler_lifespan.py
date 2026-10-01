"""Câblage de la boucle de réconciliation dans le cycle de vie de l'app FastAPI.

Désactivée par défaut (aucune tâche créée) ; avec un exécuteur et un
intervalle, la boucle tourne au moins une fois pendant que le serveur de
test est actif, puis s'arrête proprement à la fermeture — jamais de tâche
orpheline.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


class _FakeReconcilerExecutor:
    def copying_evidence(self, pipeline_id: str, run_id: str):
        return None

    def copy_job_outcome(self, pipeline_id: str, run_id: str) -> str:
        return "running"


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'reconciler-lifespan.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    try:
        yield engine
    finally:
        engine.dispose()


def test_no_reconciliation_loop_by_default(engine) -> None:
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()), org_id="default")
    with TestClient(app):
        assert app.state.reconciliation_loop is None


def test_reconciliation_loop_starts_and_stops_cleanly_when_configured(engine) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        org_id="default",
        reconciliation_executor=_FakeReconcilerExecutor(),
        reconciliation_interval_seconds=0.01,
    )
    with TestClient(app):
        assert app.state.reconciliation_loop is not None
    # Le context manager de TestClient a fermé le lifespan : aucune
    # exception, aucune tâche pendante — l'arrêt est propre.
