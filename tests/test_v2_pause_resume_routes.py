"""Tâche 18 (MCP/CLI) — pause/reprise de source, destination et organisation.

Ces routes n'existaient pas avant le chantier MCP/CLI (§9.2 tâche 18) : le
contrat demande des outils ``pause_source``/``resume_source``,
``pause_destination``/``resume_destination`` et ``pause_all``/
``resume_all``, mais seul le pipeline avait un état déclaré. Ce test
vérifie les nouvelles routes ``/v2/sources/{id}/actions/*``,
``/v2/destinations/{id}/actions/*`` et ``/v2/actions/{pause_all,resume_all}``
ajoutées en même temps (migration 0005, ``services/bulk.py``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def app_client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pause_resume.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "x",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dest1",
                "org_id": "default",
                "snowflake_account": "acct123",
                "key_pair_ciphertext": "x",
                "setup_script": "-- x",
            },
        )
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_pause_then_resume_source(app_client) -> None:
    client = app_client
    response = client.post(
        "/v2/sources/src1/actions/pause", json={}, headers={"Idempotency-Key": "pause-src-1"}
    )
    assert response.status_code == 200
    assert response.json()["after"]["paused"] is True

    response = client.get("/v2/sources/src1")
    assert response.json()["paused"] is True

    response = client.post(
        "/v2/sources/src1/actions/resume", json={}, headers={"Idempotency-Key": "resume-src-1"}
    )
    assert response.status_code == 200
    assert response.json()["after"]["paused"] is False


def test_pause_source_dry_run_does_not_persist(app_client) -> None:
    client = app_client
    response = client.post(
        "/v2/sources/src1/actions/pause",
        json={"dry_run": True},
        headers={"Idempotency-Key": "pause-src-dry-1"},
    )
    assert response.status_code == 200
    # Chantier 4 : le dry_run détaille aussi les pipelines qui seraient
    # pausés (`pipelines.would_transition`/`skipped`) — aucun pipeline
    # n'existe dans cette fixture (source sans table), donc les deux
    # listes sont vides ; voir `test_v2_pipeline_scope_pause.py` pour le
    # cas avec des pipelines réels.
    assert response.json()["dry_run"] == {
        "would_transition": {"paused": True},
        "pipelines": {"would_transition": [], "skipped": []},
    }
    assert client.get("/v2/sources/src1").json()["paused"] is False


def test_pause_unknown_source_returns_404(app_client) -> None:
    response = app_client.post(
        "/v2/sources/does-not-exist/actions/pause", json={}, headers={"Idempotency-Key": "pause-unknown-1"}
    )
    assert response.status_code == 404


def test_pause_then_resume_destination(app_client) -> None:
    client = app_client
    response = client.post(
        "/v2/destinations/dest1/actions/pause", json={}, headers={"Idempotency-Key": "pause-dest-1"}
    )
    assert response.status_code == 200
    assert response.json()["after"]["paused"] is True

    response = client.post(
        "/v2/destinations/dest1/actions/resume", json={}, headers={"Idempotency-Key": "resume-dest-1"}
    )
    assert response.status_code == 200
    assert response.json()["after"]["paused"] is False


def test_pause_all_requires_confirmation_then_applies(app_client) -> None:
    client = app_client
    response = client.post("/v2/actions/pause_all", json={}, headers={"Idempotency-Key": "pause-all-1"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pending_confirmation_required"

    pending = client.get("/v2/confirmations", params={"state": "pending"}).json()["items"]
    assert len(pending) == 1
    confirmation_id = pending[0]["id"]

    approved = client.post(
        f"/v2/confirmations/{confirmation_id}/approve", json={}, headers={"Idempotency-Key": "approve-1"}
    )
    assert approved.status_code == 200
    assert approved.json()["after"]["state"] == "approved"

    response = client.post(
        "/v2/actions/pause_all",
        json={"confirmation_token": confirmation_id},
        headers={"Idempotency-Key": "pause-all-2"},
    )
    assert response.status_code == 200
    assert response.json()["after"]["paused_sources"] == ["src1"]
    assert response.json()["after"]["paused_destinations"] == ["dest1"]
    assert client.get("/v2/sources/src1").json()["paused"] is True
    assert client.get("/v2/destinations/dest1").json()["paused"] is True


def test_bulk_action_dry_run_creates_no_confirmation(app_client) -> None:
    client = app_client
    response = client.post(
        "/v2/actions/resume_all", json={"dry_run": True}, headers={"Idempotency-Key": "resume-all-dry-1"}
    )
    assert response.status_code == 200
    assert response.json()["dry_run"] == {"would_apply": "resume_all"}
    pending = client.get("/v2/confirmations", params={"state": "pending"}).json()["items"]
    assert pending == []
