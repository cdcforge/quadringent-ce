"""Tâche 12 (route) — ``GET /v2/events`` : backlog, Last-Event-ID, types nommés."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.events import EventsService


@pytest.fixture()
def client_and_events(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'events_routes.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        sse_max_iterations=2,
        sse_poll_interval=0.001,
    )
    events_service = EventsService(engine, org_id="default")
    try:
        yield TestClient(app), events_service
    finally:
        engine.dispose()


def test_stream_replays_backlog_events_in_order(client_and_events) -> None:
    client, events_service = client_and_events
    events_service.publish("pipeline.state_changed", {"pipeline_id": "p1", "from": "copying", "to": "live"})
    events_service.publish("alert.fired", {"alert_id": "a1", "severity": "warning"})

    with client.stream("GET", "/v2/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: pipeline.state_changed" in body
    assert "event: alert.fired" in body
    assert body.index("pipeline.state_changed") < body.index("alert.fired")


def test_stream_resumes_after_last_event_id(client_and_events) -> None:
    client, events_service = client_and_events
    first = events_service.publish("pipeline.state_changed", {"pipeline_id": "p1"})
    events_service.publish("pipeline.state_changed", {"pipeline_id": "p2"})

    with client.stream("GET", "/v2/events", headers={"Last-Event-ID": str(first.id)}) as response:
        body = "".join(response.iter_text())

    assert '"pipeline_id": "p2"' in body
    assert '"pipeline_id": "p1"' not in body
