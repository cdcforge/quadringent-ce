"""Tâche 11 (routes) — ``GET /v2/audit`` et écriture automatique via ``idempotent_write``."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'audit_routes.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        token_pepper=b"pepper-de-test",
    )
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_creating_an_agent_token_is_audited(client) -> None:
    client.post(
        "/v2/agent-tokens",
        json={"name": "agent-ci", "scope": "operate", "never_expires": True},
        headers={"Idempotency-Key": "create-audit-1", "X-MCP-Client": "claude-desktop"},
    )
    audit = client.get("/v2/audit")
    assert audit.status_code == 200
    items = audit.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "agent_token.create"
    assert items[0]["mcp_client"] == "claude-desktop"
    assert items[0]["status"] == "succeeded"
    assert items[0]["actor_kind"] == "human"  # mode anonyme (pas de config d'auth)


def test_replayed_idempotent_request_is_not_audited_twice(client) -> None:
    body = {"name": "agent-ci", "scope": "read", "never_expires": True}
    headers = {"Idempotency-Key": "create-audit-2"}
    client.post("/v2/agent-tokens", json=body, headers=headers)
    client.post("/v2/agent-tokens", json=body, headers=headers)
    audit = client.get("/v2/audit")
    assert len(audit.json()["items"]) == 1


def test_audit_filters_by_action(client) -> None:
    client.post(
        "/v2/agent-tokens",
        json={"name": "a", "scope": "read", "never_expires": True},
        headers={"Idempotency-Key": "k1"},
    )
    token_id = client.get("/v2/agent-tokens").json()["items"][0]["id"]
    client.post(f"/v2/agent-tokens/{token_id}/rotate", headers={"Idempotency-Key": "k2"})
    audit = client.get("/v2/audit", params={"action": "agent_token.rotate"})
    items = audit.json()["items"]
    assert len(items) == 1
    assert items[0]["action"] == "agent_token.rotate"
