"""Tâche 13 (routes) — CRUD ``/v2/webhooks`` et rejeu manuel."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'webhooks_routes.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_create_list_delete_webhook(client) -> None:
    created = client.post(
        "/v2/webhooks",
        json={"url": "https://example.com/hook", "events": ["alert.fired"]},
        headers={"Idempotency-Key": "wh-create-1"},
    )
    assert created.status_code == 201
    body = created.json()["after"]
    assert body["secret"]
    webhook_id = body["id"]

    listed = client.get("/v2/webhooks")
    assert len(listed.json()["items"]) == 1
    assert "secret" not in listed.json()["items"][0]

    deleted = client.delete(f"/v2/webhooks/{webhook_id}", headers={"Idempotency-Key": "wh-delete-1"})
    assert deleted.status_code == 200
    assert client.get("/v2/webhooks").json()["items"] == []


def test_create_rejects_invalid_url(client) -> None:
    response = client.post(
        "/v2/webhooks",
        json={"url": "not-a-url", "events": ["alert.fired"]},
        headers={"Idempotency-Key": "wh-create-2"},
    )
    assert response.status_code == 400


def test_redeliver_is_idempotent_for_the_same_event(client) -> None:
    created = client.post(
        "/v2/webhooks",
        json={"url": "https://example.com/hook", "events": ["alert.fired"]},
        headers={"Idempotency-Key": "wh-create-3"},
    )
    webhook_id = created.json()["after"]["id"]

    first = client.post(
        f"/v2/webhooks/{webhook_id}/redeliver/evt-1",
        json={"event_type": "alert.fired", "payload": {"alert_id": "a1"}},
        headers={"Idempotency-Key": "wh-redeliver-1"},
    )
    assert first.status_code == 202
    assert first.json()["after"]["status"] == "pending"

    second = client.post(
        f"/v2/webhooks/{webhook_id}/redeliver/evt-1",
        json={"event_type": "alert.fired", "payload": {"alert_id": "a1"}},
        headers={"Idempotency-Key": "wh-redeliver-2"},
    )
    assert second.status_code == 202
    # Même livraison réutilisée (id stable) — pas de doublon en base.
    assert second.json()["after"]["id"] == first.json()["after"]["id"]
