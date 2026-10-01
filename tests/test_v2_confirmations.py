"""Tâche 7 — confirmations : création, approbation humaine/lien/jeton pré-autorisé, expiration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.confirmations import (
    ConfirmationForbiddenError,
    ConfirmationNotFoundError,
    ConfirmationsService,
    ConfirmationStateError,
    ConfirmationTokenInvalidError,
)


@pytest.fixture()
def confirmations_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'confirmations.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    service = ConfirmationsService(engine, org_id="org1", pepper=b"pepper-de-test")
    try:
        yield service
    finally:
        engine.dispose()


def _create(service, **overrides):
    defaults = dict(
        action_ref="pipeline.restart_initial_copy",
        resource_type="pipeline",
        resource_id="pipe-1",
        reason="table volumineuse, relance complète demandée",
        requested_by_kind="human",
        requested_by_id="user-1",
    )
    defaults.update(overrides)
    return service.create(**defaults)


def test_create_returns_pending_confirmation_and_one_time_token(confirmations_service) -> None:
    record, approval_token = _create(confirmations_service)
    assert record.state == "pending"
    assert approval_token
    assert approval_token not in str(record.to_dict())


def test_approve_by_human_identity_transitions_to_approved(confirmations_service) -> None:
    record, _ = _create(confirmations_service)
    approved = confirmations_service.approve(record.id, approver_kind="human", approver_id="admin@example.com")
    assert approved.state == "approved"
    assert approved.approved_by_id == "admin@example.com"


def test_approve_by_signed_link_token(confirmations_service) -> None:
    record, approval_token = _create(confirmations_service)
    approved = confirmations_service.approve(record.id, approval_token=approval_token)
    assert approved.state == "approved"
    assert approved.approved_by_id == "lien-signé"


def test_approve_by_signed_link_rejects_wrong_token(confirmations_service) -> None:
    record, _ = _create(confirmations_service)
    with pytest.raises(ConfirmationTokenInvalidError):
        confirmations_service.approve(record.id, approval_token="jeton-invente")


def test_approve_twice_raises_state_error(confirmations_service) -> None:
    record, _ = _create(confirmations_service)
    confirmations_service.approve(record.id, approver_kind="human", approver_id="admin")
    with pytest.raises(ConfirmationStateError):
        confirmations_service.approve(record.id, approver_kind="human", approver_id="admin")


def test_reject_transitions_to_rejected(confirmations_service) -> None:
    record, _ = _create(confirmations_service)
    rejected = confirmations_service.reject(record.id, approver_kind="human", approver_id="admin@example.com")
    assert rejected.state == "rejected"


def test_expired_confirmation_cannot_be_approved(confirmations_service) -> None:
    record, _ = _create(confirmations_service, ttl=timedelta(seconds=1))
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    expired = confirmations_service.get(record.id, now=later)
    assert expired.state == "expired"
    with pytest.raises(ConfirmationStateError):
        confirmations_service.approve(record.id, approver_kind="human", approver_id="admin", now=later)


def test_pre_authorized_agent_token_can_approve_its_declared_action(confirmations_service) -> None:
    record, _ = _create(confirmations_service, action_ref="pipeline.restart_initial_copy")
    approved = confirmations_service.approve(
        record.id,
        approver_kind="agent",
        approver_id="token-1",
        allowed_action_refs=("pipeline.restart_initial_copy",),
    )
    assert approved.state == "approved"
    assert approved.approved_by_kind == "agent"


def test_agent_token_cannot_approve_action_outside_its_pre_authorization(confirmations_service) -> None:
    record, _ = _create(confirmations_service, action_ref="pipeline.remove")
    with pytest.raises(ConfirmationForbiddenError):
        confirmations_service.approve(
            record.id,
            approver_kind="agent",
            approver_id="token-1",
            allowed_action_refs=("pipeline.restart_initial_copy",),
        )


def test_mark_used_prevents_reuse(confirmations_service) -> None:
    record, _ = _create(confirmations_service)
    confirmations_service.approve(record.id, approver_kind="human", approver_id="admin")
    used = confirmations_service.mark_used(record.id)
    assert used.state == "used"
    with pytest.raises(ConfirmationStateError):
        confirmations_service.mark_used(record.id)


def test_get_unknown_confirmation_raises_not_found(confirmations_service) -> None:
    with pytest.raises(ConfirmationNotFoundError):
        confirmations_service.get("does-not-exist")


def test_list_filters_by_state(confirmations_service) -> None:
    pending, _ = _create(confirmations_service, resource_id="pipe-1")
    approved_record, _ = _create(confirmations_service, resource_id="pipe-2")
    confirmations_service.approve(approved_record.id, approver_kind="human", approver_id="admin")
    pending_list = confirmations_service.list(state="pending")
    approved_list = confirmations_service.list(state="approved")
    assert [record.id for record in pending_list] == [pending.id]
    assert [record.id for record in approved_list] == [approved_record.id]


def test_confirmations_are_isolated_between_organizations(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'multi_org_confirmations.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        with engine.begin() as connection:
            connection.execute(v2_schema.organizations.insert(), [
                {"id": "org1", "name": "Premier site"},
                {"id": "org2", "name": "Second site"},
            ])
        first = ConfirmationsService(engine, org_id="org1", pepper=b"pepper-de-test")
        second = ConfirmationsService(engine, org_id="org2", pepper=b"pepper-de-test")
        own, _ = _create(first, resource_id="own-pipeline")
        foreign, foreign_token = _create(second, resource_id="foreign-pipeline")

        assert [record.id for record in first.list()] == [own.id]
        assert [record.id for record in second.list()] == [foreign.id]
        with pytest.raises(ConfirmationNotFoundError):
            first.get(foreign.id)
        with pytest.raises(ConfirmationNotFoundError):
            first.approve(foreign.id, approval_token=foreign_token)
        with pytest.raises(ConfirmationNotFoundError):
            first.reject(foreign.id, approver_kind="human", approver_id="admin")
        assert second.get(foreign.id).state == "pending"
    finally:
        engine.dispose()
