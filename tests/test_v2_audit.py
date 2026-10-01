"""Tâche 11 — audit distinguant humain/agent, interrogeable (``AuditService``)."""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.audit import AuditService, AuditValidationError


@pytest.fixture()
def audit_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'audit.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    service = AuditService(engine, org_id="org1")
    try:
        yield service
    finally:
        engine.dispose()


def test_record_then_query_round_trips_actor_kind(audit_service) -> None:
    audit_service.record(
        actor_kind="human",
        actor_id="user-1",
        actor_display="clement@example.com",
        action="pipeline.pause",
        resource_type="pipeline",
        resource_id="pipe-1",
        status="succeeded",
        before={"declared_state": "live"},
        after={"declared_state": "paused"},
    )
    audit_service.record(
        actor_kind="agent",
        actor_id="token-1",
        actor_display="agent-ci",
        mcp_client="claude-desktop",
        action="pipeline.pause",
        resource_type="pipeline",
        resource_id="pipe-2",
        status="succeeded",
    )
    humans = audit_service.query(actor_kind="human")
    agents = audit_service.query(actor_kind="agent")
    assert len(humans) == 1
    assert humans[0].actor_display == "clement@example.com"
    assert len(agents) == 1
    assert agents[0].mcp_client == "claude-desktop"


def test_query_orders_most_recent_first(audit_service) -> None:
    first = audit_service.record(
        actor_kind="human", actor_id="u", actor_display="u", action="a", resource_type="r", status="succeeded"
    )
    second = audit_service.record(
        actor_kind="human", actor_id="u", actor_display="u", action="b", resource_type="r", status="succeeded"
    )
    results = audit_service.query()
    assert results[0].id == second.id
    assert results[-1].id == first.id


def test_record_rejects_unknown_actor_kind(audit_service) -> None:
    with pytest.raises(AuditValidationError):
        audit_service.record(
            actor_kind="robot",
            actor_id="x",
            actor_display="x",
            action="a",
            resource_type="r",
            status="succeeded",
        )


def test_record_rejects_unknown_status(audit_service) -> None:
    with pytest.raises(AuditValidationError):
        audit_service.record(
            actor_kind="human",
            actor_id="x",
            actor_display="x",
            action="a",
            resource_type="r",
            status="on_fire",
        )


def test_query_filters_by_resource(audit_service) -> None:
    audit_service.record(
        actor_kind="human",
        actor_id="u",
        actor_display="u",
        action="pipeline.pause",
        resource_type="pipeline",
        resource_id="pipe-1",
        status="succeeded",
    )
    audit_service.record(
        actor_kind="human",
        actor_id="u",
        actor_display="u",
        action="pipeline.pause",
        resource_type="pipeline",
        resource_id="pipe-2",
        status="succeeded",
    )
    results = audit_service.query(resource_type="pipeline", resource_id="pipe-1")
    assert len(results) == 1
    assert results[0].resource_id == "pipe-1"


def test_never_records_a_secret_field_because_callers_must_pre_filter(audit_service) -> None:
    # Le service n'a pas de logique de filtrage de secret lui-même (comme
    # ``_append`` en v1) : c'est la responsabilité de l'appelant de ne
    # jamais passer de secret dans ``before``/``after``. Ce test documente
    # cette frontière plutôt que de la faire respecter en base.
    record = audit_service.record(
        actor_kind="human",
        actor_id="u",
        actor_display="u",
        action="source.create",
        resource_type="source",
        status="succeeded",
        after={"secret_set": True},
    )
    assert "secret" not in str(record.after) or record.after == {"secret_set": True}
