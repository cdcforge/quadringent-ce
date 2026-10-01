"""Tâche 12 — service d'événements SSE persistés (types, ordre, curseur)."""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.events import EventsService, KNOWN_EVENT_TYPES


@pytest.fixture()
def events_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'events.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    service = EventsService(engine, org_id="org1")
    try:
        yield service
    finally:
        engine.dispose()


def test_publish_then_events_after_zero_returns_full_backlog(events_service) -> None:
    first = events_service.publish("pipeline.state_changed", {"pipeline_id": "p1", "from": "copying", "to": "live"})
    second = events_service.publish("alert.fired", {"alert_id": "a1", "severity": "warning"})
    backlog = events_service.events_after(0)
    assert [record.id for record in backlog] == [first.id, second.id]


def test_events_after_cursor_only_returns_newer_events(events_service) -> None:
    first = events_service.publish("pipeline.state_changed", {"pipeline_id": "p1"})
    second = events_service.publish("pipeline.state_changed", {"pipeline_id": "p2"})
    resumed = events_service.events_after(first.id)
    assert [record.id for record in resumed] == [second.id]


def test_events_after_future_cursor_returns_nothing(events_service) -> None:
    events_service.publish("pipeline.state_changed", {"pipeline_id": "p1"})
    assert events_service.events_after(9999) == ()


def test_latest_id_tracks_the_last_published_event(events_service) -> None:
    assert events_service.latest_id() == 0
    record = events_service.publish("alert.fired", {"alert_id": "a1"})
    assert events_service.latest_id() == record.id


def test_to_sse_formats_id_event_and_data_lines(events_service) -> None:
    record = events_service.publish("action.completed", {"action_id": "act1", "state": "succeeded"})
    formatted = record.to_sse()
    assert formatted.startswith(f"id: {record.id}\n")
    assert "event: action.completed\n" in formatted
    assert '"action_id": "act1"' in formatted
    assert formatted.endswith("\n\n")


def test_known_event_types_cover_the_contract_catalogue() -> None:
    assert KNOWN_EVENT_TYPES == {
        "pipeline.state_changed",
        "action.pending_confirmation",
        "action.completed",
        "alert.fired",
        "alert.resolved",
    }
