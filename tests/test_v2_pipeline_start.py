"""``POST /v2/tables/{id}/pipeline`` — démarrage automatique (contrat §2.3).

Deux niveaux : le service pur (``PipelinesService.start_table_pipeline``/
``plan_start``), puis la route HTTP bout-en-bout (dry_run, en-tête
``Idempotency-Key`` obligatoire, audit, exécuteur factice — jamais de
Kubernetes réel).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.pipelines import (
    AmbiguousDestinationError,
    DestinationNotFoundError,
    PipelineExecutorProtocol,
    PipelineExecutorUnavailableError,
    PipelinesService,
    TableNotFoundError,
)
from quadringent_control_plane.v2.services.state_machine import ForbiddenTransitionError


class _FakeExecutor(PipelineExecutorProtocol):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


def _seed_source_and_table(engine, *, table_id="tbl1", source_id="src1") -> None:
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": source_id,
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.test",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": table_id, "source_id": source_id, "schema_name": "SALES", "table_name": "ORDHDR"},
        )


def _seed_destination(engine, *, destination_id="dst1") -> None:
    with engine.begin() as connection:
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": destination_id,
                "org_id": "default",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )


# --- Service pur -------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline-start.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    try:
        yield engine
    finally:
        engine.dispose()


def test_start_creates_a_pipeline_and_triggers_the_start_event(engine) -> None:
    _seed_source_and_table(engine)
    _seed_destination(engine)
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    before, after = service.start_table_pipeline(
        "tbl1", destination_id=None, executor=executor
    )

    assert before is None
    assert after.declared_state == "copying"
    assert executor.calls == [(after.id, "start")]


def test_start_resolves_the_sole_destination_when_omitted(engine) -> None:
    _seed_source_and_table(engine)
    _seed_destination(engine, destination_id="dst-only")
    service = PipelinesService(engine)

    plan = service.plan_start("tbl1", destination_id=None)

    assert plan["destination_id"] == "dst-only"
    assert plan["would_create"] is True


def test_start_with_multiple_destinations_requires_explicit_choice(engine) -> None:
    _seed_source_and_table(engine)
    _seed_destination(engine, destination_id="dst-a")
    _seed_destination(engine, destination_id="dst-b")
    service = PipelinesService(engine)

    with pytest.raises(AmbiguousDestinationError):
        service.plan_start("tbl1", destination_id=None)

    with pytest.raises(AmbiguousDestinationError):
        service.start_table_pipeline("tbl1", destination_id=None, executor=_FakeExecutor())


def test_start_with_unknown_destination_id_fails_closed(engine) -> None:
    _seed_source_and_table(engine)
    _seed_destination(engine)
    service = PipelinesService(engine)

    with pytest.raises(DestinationNotFoundError):
        service.start_table_pipeline("tbl1", destination_id="not-a-real-id", executor=_FakeExecutor())


def test_start_unknown_table_fails_closed(engine) -> None:
    service = PipelinesService(engine)
    with pytest.raises(TableNotFoundError):
        service.start_table_pipeline("no-such-table", destination_id=None, executor=_FakeExecutor())


def test_start_without_an_executor_is_a_503_style_error(engine) -> None:
    _seed_source_and_table(engine)
    _seed_destination(engine)
    service = PipelinesService(engine)
    with pytest.raises(PipelineExecutorUnavailableError):
        service.start_table_pipeline("tbl1", destination_id=None, executor=None)


def test_starting_an_already_started_pipeline_is_a_forbidden_transition(engine) -> None:
    _seed_source_and_table(engine)
    _seed_destination(engine)
    service = PipelinesService(engine)
    service.start_table_pipeline("tbl1", destination_id=None, executor=_FakeExecutor())

    with pytest.raises(ForbiddenTransitionError):
        service.start_table_pipeline("tbl1", destination_id=None, executor=_FakeExecutor())


# --- Route HTTP ----------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline-start-http.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    _seed_source_and_table(engine)
    _seed_destination(engine)
    secret_box = SecretBox(SecretBox.generate_key())
    executor = _FakeExecutor()
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default", pipeline_executor=executor)
    try:
        yield TestClient(app), engine, executor
    finally:
        engine.dispose()


def test_dry_run_never_calls_the_executor_nor_creates_a_pipeline(client) -> None:
    http, engine, executor = client
    response = http.post(
        "/v2/tables/tbl1/pipeline",
        json={"dry_run": True},
        headers={"Idempotency-Key": "start-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"]["would_transition"] == {"from": "not_started", "to": "copying"}
    assert executor.calls == []
    with engine.connect() as connection:
        rows = connection.execute(v2_schema.pipelines.select()).all()
    assert rows == []


def test_start_route_creates_pipeline_and_returns_before_null(client) -> None:
    http, engine, executor = client
    response = http.post(
        "/v2/tables/tbl1/pipeline",
        json={"dry_run": False},
        headers={"Idempotency-Key": "start-2"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["before"] is None
    assert body["after"]["declared_state"] == "copying"
    assert len(executor.calls) == 1


def test_start_route_requires_idempotency_key(client) -> None:
    http, _engine, _executor = client
    response = http.post("/v2/tables/tbl1/pipeline", json={"dry_run": False})
    assert response.status_code == 400


def test_start_route_replays_identical_response_for_the_same_key(client) -> None:
    http, _engine, executor = client
    headers = {"Idempotency-Key": "start-3"}
    first = http.post("/v2/tables/tbl1/pipeline", json={"dry_run": False}, headers=headers)
    second = http.post("/v2/tables/tbl1/pipeline", json={"dry_run": False}, headers=headers)
    assert first.json() == second.json()
    assert len(executor.calls) == 1  # jamais rejoué contre l'exécuteur


def test_start_route_on_unknown_table_is_404(client) -> None:
    http, _engine, _executor = client
    response = http.post(
        "/v2/tables/does-not-exist/pipeline",
        json={"dry_run": False},
        headers={"Idempotency-Key": "start-4"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_start_route_without_executor_is_503(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'pipeline-start-no-exec.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    _seed_source_and_table(engine)
    _seed_destination(engine)
    secret_box = SecretBox(SecretBox.generate_key())
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default", pipeline_executor=None)
    http = TestClient(app)
    response = http.post(
        "/v2/tables/tbl1/pipeline",
        json={"dry_run": False},
        headers={"Idempotency-Key": "start-5"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "executor_unavailable"
    engine.dispose()
