"""Tâche 8 (suite) — dépendance d'auth ``/v2`` : jeton d'agent Bearer.

Vérifie que le jeton d'agent est reconnu prioritairement, que le scope du
jeton est respecté, que la restriction de source est appliquée, et que sans
aucune configuration d'auth le mode anonyme historique reste actif.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.auth import require_scope, require_source_scope
from quadringent_control_plane.v2.errors import ApiError
from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService


@pytest.fixture()
def app_and_tokens(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'auth.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    tokens_service = AgentTokensService(engine, org_id="org1", pepper=b"pepper-de-test")

    app = FastAPI()
    app.state.auth_config = None
    app.state.users_service = None
    app.state.agent_tokens_service = tokens_service

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.get("/read-only")
    def read_only(identity=__import__("fastapi").Depends(require_scope("read"))):
        return {"subject": identity.subject, "actor_kind": identity.actor_kind}

    @app.get("/operate-only")
    def operate_only(identity=__import__("fastapi").Depends(require_scope("operate"))):
        return {"subject": identity.subject}

    @app.get("/sources/{source_id}/restricted")
    def restricted(source_id: str, identity=__import__("fastapi").Depends(require_source_scope("read"))):
        return {"source_id": source_id}

    try:
        yield app, tokens_service
    finally:
        engine.dispose()


def test_no_config_falls_back_to_anonymous_admin(app_and_tokens) -> None:
    app, _ = app_and_tokens
    client = TestClient(app)
    response = client.get("/operate-only")
    assert response.status_code == 200


def test_valid_bearer_token_is_accepted(app_and_tokens) -> None:
    app, tokens_service = app_and_tokens
    _, token_value = tokens_service.create(
        name="agent-ci", scope="read", created_by="admin", never_expires=True
    )
    client = TestClient(app)
    response = client.get("/read-only", headers={"Authorization": f"Bearer {token_value}"})
    assert response.status_code == 200
    assert response.json()["actor_kind"] == "agent"


def test_insufficient_token_scope_is_refused(app_and_tokens) -> None:
    app, tokens_service = app_and_tokens
    _, token_value = tokens_service.create(
        name="agent-ci", scope="read", created_by="admin", never_expires=True
    )
    client = TestClient(app)
    response = client.get("/operate-only", headers={"Authorization": f"Bearer {token_value}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_role"


def test_revoked_token_is_refused_with_401(app_and_tokens) -> None:
    app, tokens_service = app_and_tokens
    record, token_value = tokens_service.create(
        name="agent-ci", scope="read", created_by="admin", never_expires=True
    )
    tokens_service.revoke(record.id)
    client = TestClient(app)
    response = client.get("/read-only", headers={"Authorization": f"Bearer {token_value}"})
    assert response.status_code == 401


def test_source_restriction_blocks_out_of_scope_source(app_and_tokens) -> None:
    app, tokens_service = app_and_tokens
    _, token_value = tokens_service.create(
        name="agent-ci",
        scope="read",
        created_by="admin",
        never_expires=True,
        source_restriction=["source-a"],
    )
    client = TestClient(app)
    ok = client.get("/sources/source-a/restricted", headers={"Authorization": f"Bearer {token_value}"})
    forbidden = client.get(
        "/sources/source-b/restricted", headers={"Authorization": f"Bearer {token_value}"}
    )
    assert ok.status_code == 200
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "wrong_environment"


def test_unrestricted_token_reaches_any_source(app_and_tokens) -> None:
    app, tokens_service = app_and_tokens
    _, token_value = tokens_service.create(name="agent-ci", scope="read", created_by="admin", never_expires=True)
    client = TestClient(app)
    response = client.get("/sources/anything/restricted", headers={"Authorization": f"Bearer {token_value}"})
    assert response.status_code == 200
