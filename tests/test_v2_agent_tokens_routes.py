"""Tâche 8 (routes) — ``/v2/agent-tokens`` via ``create_v2_app``."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'agent_tokens_routes.sqlite3'}"
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


def test_create_list_rotate_revoke_agent_token(client) -> None:
    created = client.post(
        "/v2/agent-tokens",
        json={"name": "agent-ci", "scope": "operate", "never_expires": True},
        headers={"Idempotency-Key": "create-1"},
    )
    assert created.status_code == 201
    body = created.json()["after"]
    token_value = body["token"]
    assert token_value.startswith("qdt_op_")
    token_id = body["id"]

    listed = client.get("/v2/agent-tokens")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1
    assert "token" not in listed.json()["items"][0]

    rotated = client.post(f"/v2/agent-tokens/{token_id}/rotate", headers={"Idempotency-Key": "rotate-1"})
    assert rotated.status_code == 200
    new_token = rotated.json()["after"]["token"]
    assert new_token != token_value

    # L'ancien jeton n'authentifie plus rien (scope read insuffisant testé ailleurs) —
    # ici on vérifie juste que le nouveau jeton fonctionne bien pour une route protégée.
    check = client.get("/v2/agent-tokens", headers={"Authorization": f"Bearer {new_token}"})
    assert check.status_code == 403  # scope "operate" insuffisant pour une route "admin"

    revoked = client.delete(f"/v2/agent-tokens/{token_id}", headers={"Idempotency-Key": "revoke-1"})
    assert revoked.status_code == 200
    assert revoked.json()["after"]["revoked_at"] is not None


def test_create_without_expiry_or_never_expires_is_rejected(client) -> None:
    response = client.post(
        "/v2/agent-tokens",
        json={"name": "agent-ci", "scope": "read"},
        headers={"Idempotency-Key": "create-2"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
