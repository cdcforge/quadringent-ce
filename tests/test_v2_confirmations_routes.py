"""Tâche 7 (bout-en-bout) — action sensible de pipeline sans confirmation puis avec.

Vérifie le cycle complet §6.4 du contrat : ``POST .../actions/remove`` sans
``confirmation_token`` crée une confirmation et renvoie
``409 pending_confirmation_required`` ; ``POST /v2/confirmations/{id}/approve``
l'approuve ; rejouer l'action avec ``confirmation_token`` l'exécute, et la
réutiliser une seconde fois est refusée (``wrong_confirmation``).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


@pytest.fixture()
def app_client_and_pipeline(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'confirmations_routes.sqlite3'}"
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
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "SCHEMA1", "table_name": "TABLE1"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "pipe1", "table_id": "tbl1", "destination_id": "dest1", "declared_state": "live"},
        )
    executor = _FakeExecutor()
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_executor=executor,
    )
    try:
        yield TestClient(app), executor
    finally:
        engine.dispose()


def test_sensitive_action_without_confirmation_returns_409(app_client_and_pipeline) -> None:
    client, _executor = app_client_and_pipeline
    response = client.post(
        "/v2/pipelines/pipe1/actions/remove",
        json={},
        headers={"Idempotency-Key": "remove-1"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pending_confirmation_required"

    pending = client.get("/v2/confirmations", params={"state": "pending"})
    items = pending.json()["items"]
    assert len(items) == 1
    assert items[0]["action_ref"] == "pipeline.remove"
    assert items[0]["resource_id"] == "pipe1"
    assert items[0]["available"] == {"approve": True, "reject": True, "execute": False}


def test_confirmation_availability_respects_reader_and_agent_scope() -> None:
    from quadringent_control_plane.v2.auth import Identity
    from quadringent_control_plane.v2.routes.confirmations import _present
    from quadringent_control_plane.v2.services.confirmations import ConfirmationRecord

    record = ConfirmationRecord(
        id="cf_1", action_ref="pipeline.replay", resource_type="pipeline", resource_id="pipe1",
        reason="Rejouer", risk_estimate=None, requested_by_kind="human", requested_by_id="op",
        expires_at="2026-09-29T00:00:00+00:00", state="pending", approved_by_kind=None,
        approved_by_id=None, approved_at=None, created_at="2026-09-28T00:00:00+00:00",
    )
    assert _present(record, Identity(subject="reader", role="reader"))["available"] == {
        "approve": False, "reject": False, "execute": False,
    }
    assert _present(record, Identity(subject="agent", role="operate", actor_kind="agent"))["available"] == {
        "approve": False, "reject": True, "execute": False,
    }
    assert _present(record, Identity(subject="agent", role="operate", actor_kind="agent",
                                     pre_authorized_actions=("pipeline.replay",)))["available"] == {
        "approve": True, "reject": True, "execute": False,
    }
    approved_copy = ConfirmationRecord(**{**record.__dict__, "state": "approved", "action_ref": "pipeline.restart_initial_copy"})
    assert _present(approved_copy, Identity(subject="agent", role="operate", actor_kind="agent"))["available"]["execute"] is False
    assert _present(approved_copy, Identity(subject="agent", role="operate", actor_kind="agent",
                                            pre_authorized_actions=("pipeline.restart_initial_copy",)))["available"]["execute"] is True


def test_approve_then_replay_action_with_confirmation_token_executes_it(app_client_and_pipeline) -> None:
    client, executor = app_client_and_pipeline
    client.post("/v2/pipelines/pipe1/actions/remove", json={}, headers={"Idempotency-Key": "remove-2"})
    confirmation_id = client.get("/v2/confirmations", params={"state": "pending"}).json()["items"][0]["id"]

    approved = client.post(f"/v2/confirmations/{confirmation_id}/approve", headers={"Idempotency-Key": "approve-1"})
    assert approved.status_code == 200
    assert approved.json()["after"]["state"] == "approved"
    detail = client.get(f"/v2/confirmations/{confirmation_id}").json()
    assert detail["available"] == {"approve": False, "reject": False, "execute": True}
    assert any(item["id"] == confirmation_id for item in client.get("/v2/confirmations", params={"state": "all"}).json()["items"])

    executed = client.post(
        "/v2/pipelines/pipe1/actions/remove",
        json={"confirmation_token": confirmation_id},
        headers={"Idempotency-Key": "remove-3"},
    )
    assert executed.status_code == 200
    assert executed.json()["after"]["declared_state"] == "stopped"
    assert executor.calls == [("pipe1", "remove")]


def test_reusing_a_confirmation_token_a_second_time_is_refused(app_client_and_pipeline) -> None:
    client, _executor = app_client_and_pipeline
    client.post("/v2/pipelines/pipe1/actions/remove", json={}, headers={"Idempotency-Key": "remove-4"})
    confirmation_id = client.get("/v2/confirmations", params={"state": "pending"}).json()["items"][0]["id"]
    client.post(f"/v2/confirmations/{confirmation_id}/approve", headers={"Idempotency-Key": "approve-2"})
    client.post(
        "/v2/pipelines/pipe1/actions/remove",
        json={"confirmation_token": confirmation_id},
        headers={"Idempotency-Key": "remove-5"},
    )
    # Rejouer la même action sensible avec le même jeton de confirmation
    # (nouvelle clé d'idempotence, sinon on ne testerait que le rejeu
    # d'idempotence) — la confirmation est déjà "used", donc refusée.
    replay_remove = client.post(
        "/v2/pipelines/pipe1/actions/remove",
        json={"confirmation_token": confirmation_id},
        headers={"Idempotency-Key": "remove-6"},
    )
    assert replay_remove.status_code == 403
    assert replay_remove.json()["error"]["code"] == "wrong_confirmation"


def test_restart_initial_copy_also_requires_confirmation(app_client_and_pipeline) -> None:
    client, executor = app_client_and_pipeline
    first = client.post(
        "/v2/pipelines/pipe1/actions/restart_initial_copy", json={}, headers={"Idempotency-Key": "ric-1"}
    )
    assert first.status_code == 409
    confirmation_id = client.get("/v2/confirmations", params={"state": "pending"}).json()["items"][0]["id"]
    assert confirmation_id
    client.post(f"/v2/confirmations/{confirmation_id}/approve", headers={"Idempotency-Key": "ric-approve"})
    second = client.post(
        "/v2/pipelines/pipe1/actions/restart_initial_copy",
        json={"confirmation_token": confirmation_id},
        headers={"Idempotency-Key": "ric-2"},
    )
    assert second.status_code == 200
    assert second.json()["after"]["declared_state"] == "copying"
    assert executor.calls == [("pipe1", "restart_initial_copy")]
