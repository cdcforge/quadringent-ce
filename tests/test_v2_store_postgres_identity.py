"""Tâches 7, 8, 9, 11, 12, 13 — round-trip Postgres des tables ajoutées en 0002/0003.

Même principe que ``test_control_plane_store_postgres.py`` (tâche 1) :
mêmes scénarios sur SQLite (rapide, toujours actif) et sur un vrai
Postgres 16 (``@pytest.mark.postgres``) — vérifie que les migrations 0002
et 0003 s'appliquent proprement et que les services de plus haut niveau
(déjà couverts unitairement sur SQLite) fonctionnent aussi sur Postgres.
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService
from quadringent_control_plane.v2.services.audit import AuditService
from quadringent_control_plane.v2.services.confirmations import ConfirmationsService
from quadringent_control_plane.v2.services.events import EventsService
from quadringent_control_plane.v2.services.users import UsersService
from quadringent_control_plane.v2.services.webhooks import WebhooksService


def _round_trip(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        with engine.begin() as connection:
            connection.execute(v2_schema.organizations.insert(), {"id": "org-identity-v2", "name": "Client unique"})

        pepper = b"pepper-de-test-postgres"

        tokens_service = AgentTokensService(engine, org_id="org-identity-v2", pepper=pepper)
        token_record, token_value = tokens_service.create(
            name="agent-ci", scope="operate", created_by="admin", never_expires=True
        )
        assert tokens_service.authenticate(token_value).id == token_record.id

        users_service = UsersService(engine, org_id="org-identity-v2", pepper=pepper)
        user_record, activation_token = users_service.create_first_admin(email="admin@example.com")
        activated = users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
        assert activated.id == user_record.id
        session_token = users_service.create_session_token(activated)
        assert users_service.resolve_session(session_token).id == activated.id

        confirmations_service = ConfirmationsService(engine, org_id="org-identity-v2", pepper=pepper)
        confirmation_record, _approval_token = confirmations_service.create(
            action_ref="pipeline.remove",
            resource_type="pipeline",
            resource_id="pipe1",
            reason="retrait demandé",
            requested_by_kind="human",
            requested_by_id=activated.id,
        )
        approved = confirmations_service.approve(
            confirmation_record.id, approver_kind="human", approver_id=activated.id
        )
        assert approved.state == "approved"

        audit_service = AuditService(engine, org_id="org-identity-v2")
        audit_record = audit_service.record(
            actor_kind="human",
            actor_id=activated.id,
            actor_display=activated.email,
            action="pipeline.remove",
            resource_type="pipeline",
            resource_id="pipe1",
            status="succeeded",
            confirmation_id=confirmation_record.id,
        )
        assert audit_service.get(audit_record.id).confirmation_id == confirmation_record.id

        events_service = EventsService(engine, org_id="org-identity-v2")
        event_record = events_service.publish("pipeline.state_changed", {"pipeline_id": "pipe1"})
        assert events_service.events_after(0)[-1].id == event_record.id

        webhooks_service = WebhooksService(engine, org_id="org-identity-v2", secret_box=SecretBox(SecretBox.generate_key()))
        webhook_record, secret = webhooks_service.create(url="https://example.com/hook", events=["alert.fired"])
        assert webhooks_service.secret_for(webhook_record.id) == secret
        delivery = webhooks_service.enqueue_delivery(
            webhook_record.id, event_id="evt-1", event_type="alert.fired", payload={"alert_id": "a1"}
        )
        assert delivery is not None
        assert webhooks_service.enqueue_delivery(
            webhook_record.id, event_id="evt-1", event_type="alert.fired", payload={"alert_id": "a1"}
        ) is None
    finally:
        engine.dispose()


def test_round_trip_on_sqlite(tmp_path) -> None:
    _round_trip(f"sqlite:///{tmp_path / 'identity-v2.sqlite3'}")


@pytest.mark.postgres
def test_round_trip_on_postgres(postgres_dsn) -> None:
    _round_trip(postgres_dsn)


@pytest.mark.postgres
def test_migration_0003_drops_secret_hash_and_adds_secret_ciphertext_on_postgres(postgres_dsn) -> None:
    from sqlalchemy import inspect

    v2_db.run_migrations(postgres_dsn)
    engine = v2_db.create_engine_for(postgres_dsn)
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("webhooks")}
        assert "secret_ciphertext" in columns
        assert "secret_hash" not in columns
    finally:
        engine.dispose()
