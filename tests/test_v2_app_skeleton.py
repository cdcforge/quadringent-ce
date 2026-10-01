"""Squelette FastAPI `/v2` : OpenAPI 3.1 exposé, v1 intact à côté.

Vérifie que ``/v2/openapi.json`` est un document OpenAPI 3.1 valide listant
les routes implémentées, et que l'import de ``quadringent_control_plane.v2``
n'altère en rien ``quadringent_control_plane.server`` (le serveur v1
continue de s'importer et fonctionner sans dépendre de FastAPI).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.openapi.utils import get_openapi
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def app(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'skeleton.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    application = create_v2_app(engine=engine, secret_box=secret_box, org_id="default")
    try:
        yield application
    finally:
        engine.dispose()


def test_openapi_document_is_version_3_1(app) -> None:
    spec = app.openapi()
    assert spec["openapi"].startswith("3.1")


def test_openapi_document_generation_does_not_raise_via_fastapi_helper(app) -> None:
    # ``get_openapi`` valide la cohérence interne des schémas Pydantic déjà
    # collectés par FastAPI — un document mal formé lève ici.
    spec = get_openapi(title=app.title, version=app.version, routes=app.routes, openapi_version="3.1.0")
    assert spec["openapi"] == "3.1.0"


def test_openapi_document_is_served_and_lists_implemented_routes(app) -> None:
    with TestClient(app) as client:
        response = client.get("/v2/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"].startswith("3.1")
    paths = set(spec["paths"])
    expected = {
        "/v2/sources",
        "/v2/sources/{source_id}",
        "/v2/sources/{source_id}/test",
        "/v2/destinations",
        "/v2/destinations/{destination_id}",
        "/v2/pipelines/{pipeline_id}",
        "/v2/pipelines/{pipeline_id}/actions/{action}",
    }
    assert expected <= paths


def test_sources_route_declares_get_and_post(app) -> None:
    spec = app.openapi()
    assert set(spec["paths"]["/v2/sources"]) >= {"get", "post"}


def test_healthz_reports_ok_when_the_database_is_reachable(app) -> None:
    with TestClient(app) as client:
        response = client.get("/v2/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # La sonde ne doit jamais apparaître dans le document OpenAPI public.
    assert "/v2/healthz" not in app.openapi()["paths"]


def test_healthz_reports_unavailable_when_the_database_is_unreachable(app) -> None:
    from sqlalchemy import create_engine

    # Répertoire inexistant : SQLite échoue à ouvrir le fichier plutôt que de
    # le créer — simule une base inaccessible sans dépendre d'un vrai Postgres.
    broken = create_engine("sqlite:////nonexistent-dir-quadringent-v2-healthz-test/db.sqlite3")
    app.state.engine = broken
    try:
        with TestClient(app) as client:
            response = client.get("/v2/healthz")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
    finally:
        broken.dispose()


def test_importing_v2_does_not_break_the_v1_server_module() -> None:
    server_module = importlib.import_module("quadringent_control_plane.server")
    importlib.reload(server_module)
    assert hasattr(server_module, "serve")


def test_v1_server_module_does_not_import_fastapi() -> None:
    import sys

    server_module = sys.modules["quadringent_control_plane.server"]
    source = server_module.__file__
    with open(source, encoding="utf-8") as handle:
        content = handle.read()
    assert "fastapi" not in content.lower()
