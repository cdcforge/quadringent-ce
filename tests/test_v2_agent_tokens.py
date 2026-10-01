"""Tâche 8 — jetons d'agent (format, hash, scope, restriction, rotation)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.agent_tokens import (
    AgentTokenInvalidError,
    AgentTokenNotFoundError,
    AgentTokenValidationError,
    AgentTokensService,
)


@pytest.fixture()
def tokens_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'tokens.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    service = AgentTokensService(engine, org_id="org1", pepper=b"pepper-de-test")
    try:
        yield service
    finally:
        engine.dispose()


def test_create_returns_the_plaintext_token_once(tokens_service) -> None:
    record, token_value = tokens_service.create(
        name="agent-onboarding",
        scope="operate",
        created_by="admin@example.com",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    assert token_value.startswith("qdt_op_")
    stored = tokens_service.get(record.id).to_dict()
    assert "hash" not in stored
    assert token_value not in str(stored)


def test_create_requires_expiry_unless_never_expires(tokens_service) -> None:
    with pytest.raises(AgentTokenValidationError):
        tokens_service.create(name="x", scope="read", created_by="admin")


def test_create_accepts_never_expires_without_expiry(tokens_service) -> None:
    record, _ = tokens_service.create(
        name="x", scope="read", created_by="admin", never_expires=True
    )
    assert record.never_expires is True
    assert record.expires_at is None


def test_create_rejects_unknown_scope(tokens_service) -> None:
    with pytest.raises(AgentTokenValidationError):
        tokens_service.create(name="x", scope="superadmin", created_by="admin", never_expires=True)


def test_authenticate_returns_record_for_valid_token(tokens_service) -> None:
    record, token_value = tokens_service.create(
        name="agent", scope="read", created_by="admin", never_expires=True
    )
    resolved = tokens_service.authenticate(token_value)
    assert resolved.id == record.id
    assert resolved.last_used_at is not None


def test_authenticate_rejects_unknown_token(tokens_service) -> None:
    with pytest.raises(AgentTokenInvalidError):
        tokens_service.authenticate("qdt_op_ceci-nexiste-pas")


def test_authenticate_rejects_revoked_token(tokens_service) -> None:
    record, token_value = tokens_service.create(
        name="agent", scope="read", created_by="admin", never_expires=True
    )
    tokens_service.revoke(record.id)
    with pytest.raises(AgentTokenInvalidError):
        tokens_service.authenticate(token_value)


def test_authenticate_rejects_expired_token(tokens_service) -> None:
    record, token_value = tokens_service.create(
        name="agent",
        scope="read",
        created_by="admin",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    with pytest.raises(AgentTokenInvalidError):
        tokens_service.authenticate(token_value)


def test_rotate_invalidates_the_old_token_value(tokens_service) -> None:
    record, old_token = tokens_service.create(
        name="agent", scope="operate", created_by="admin", never_expires=True
    )
    _, new_token = tokens_service.rotate(record.id)
    assert new_token != old_token
    with pytest.raises(AgentTokenInvalidError):
        tokens_service.authenticate(old_token)
    resolved = tokens_service.authenticate(new_token)
    assert resolved.id == record.id


def test_source_restriction_is_persisted_and_returned(tokens_service) -> None:
    record, _ = tokens_service.create(
        name="agent",
        scope="operate",
        created_by="admin",
        never_expires=True,
        source_restriction=["source-a", "source-b"],
    )
    assert tokens_service.get(record.id).source_restriction == ("source-a", "source-b")


def test_get_unknown_token_raises_not_found(tokens_service) -> None:
    with pytest.raises(AgentTokenNotFoundError):
        tokens_service.get("does-not-exist")


def test_list_orders_by_creation(tokens_service) -> None:
    first, _ = tokens_service.create(name="a", scope="read", created_by="admin", never_expires=True)
    second, _ = tokens_service.create(name="b", scope="read", created_by="admin", never_expires=True)
    assert [record.id for record in tokens_service.list()] == [first.id, second.id]
