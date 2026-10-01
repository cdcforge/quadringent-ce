"""Tâche 6 — enveloppe d'action générique.

Deux niveaux : le magasin d'idempotence pur (``services/idempotency.py``,
sans FastAPI), puis l'enveloppe HTTP bout-en-bout via l'application `/v2`
(dry_run, header ``Idempotency-Key`` obligatoire, rejeu idempotent
identique, conflit 409 sur clé réutilisée avec un corps différent,
before/after/verify).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyKeyMissingError,
    IdempotencyStore,
    request_hash,
    validate_key,
)
from quadringent_control_plane.v2.services.pipelines import PipelineExecutorProtocol


# --- Magasin d'idempotence pur ----------------------------------------------


@pytest.fixture()
def idempotency_store(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'idempotency.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        yield IdempotencyStore(engine)
    finally:
        engine.dispose()


def test_unseen_key_resolves_to_none(idempotency_store) -> None:
    assert (
        idempotency_store.resolve(
            key="unseen", actor_id="op1", method="POST", path="/v2/sources", body_hash="abc"
        )
        is None
    )


def test_same_key_same_body_replays_the_stored_response(idempotency_store) -> None:
    idempotency_store.store(
        key="k1",
        actor_id="op1",
        method="POST",
        path="/v2/sources",
        body_hash="abc",
        status_code=201,
        response={"after": {"id": "src1"}},
    )
    replay = idempotency_store.resolve(
        key="k1", actor_id="op1", method="POST", path="/v2/sources", body_hash="abc"
    )
    assert replay is not None
    assert replay.status_code == 201
    assert replay.body == {"after": {"id": "src1"}}


def test_same_key_different_body_conflicts(idempotency_store) -> None:
    idempotency_store.store(
        key="k1",
        actor_id="op1",
        method="POST",
        path="/v2/sources",
        body_hash="abc",
        status_code=201,
        response={"after": {"id": "src1"}},
    )
    with pytest.raises(IdempotencyKeyConflictError):
        idempotency_store.resolve(
            key="k1", actor_id="op1", method="POST", path="/v2/sources", body_hash="different"
        )


def test_validate_key_rejects_missing_or_blank() -> None:
    with pytest.raises(IdempotencyKeyMissingError):
        validate_key(None)
    with pytest.raises(IdempotencyKeyMissingError):
        validate_key("   ")


def test_validate_key_rejects_too_long() -> None:
    with pytest.raises(IdempotencyKeyMissingError):
        validate_key("x" * 129)


def test_request_hash_differs_when_body_differs() -> None:
    h1 = request_hash("POST", "/v2/sources", b'{"a":1}')
    h2 = request_hash("POST", "/v2/sources", b'{"a":2}')
    assert h1 != h2


def test_request_hash_is_stable_for_identical_input() -> None:
    h1 = request_hash("POST", "/v2/sources", b'{"a":1}')
    h2 = request_hash("POST", "/v2/sources", b'{"a":1}')
    assert h1 == h2


# --- Enveloppe HTTP bout-en-bout --------------------------------------------


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'app.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default")
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


_SOURCE_BODY = {
    "display_name": "Site principal",
    "ibmi_host": "as400.example.com",
    "ibmi_user": "QSVCUSER",
    "secret": {"kind": "inline", "value": "un-mot-de-passe-tres-secret"},
}


def test_write_without_idempotency_key_is_rejected(client) -> None:
    response = client.post("/v2/sources", json=_SOURCE_BODY)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_create_response_has_the_generic_envelope_shape(client) -> None:
    response = client.post(
        "/v2/sources", json=_SOURCE_BODY, headers={"Idempotency-Key": "create-1"}
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"before", "after", "verify", "dry_run"}
    assert body["before"] is None
    assert body["after"]["display_name"] == "Site principal"
    assert body["verify"] == {"method": "GET", "path": f"/v2/sources/{body['after']['id']}"}
    assert body["dry_run"] is None


def test_replaying_the_same_key_and_body_returns_the_identical_response(client) -> None:
    first = client.post("/v2/sources", json=_SOURCE_BODY, headers={"Idempotency-Key": "replay-1"})
    second = client.post("/v2/sources", json=_SOURCE_BODY, headers={"Idempotency-Key": "replay-1"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()

    # Un seul enregistrement a réellement été créé (pas de doublon silencieux).
    listing = client.get("/v2/sources")
    matching = [item for item in listing.json()["items"] if item["id"] == first.json()["after"]["id"]]
    assert len(matching) == 1


def test_reusing_the_same_key_with_a_different_body_conflicts(client) -> None:
    client.post("/v2/sources", json=_SOURCE_BODY, headers={"Idempotency-Key": "conflict-1"})
    other_body = {**_SOURCE_BODY, "display_name": "Autre site"}
    response = client.post("/v2/sources", json=other_body, headers={"Idempotency-Key": "conflict-1"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_conflict"


def test_dry_run_returns_a_plan_without_persisting(client) -> None:
    body = {**_SOURCE_BODY, "dry_run": True}
    response = client.post("/v2/sources", json=body, headers={"Idempotency-Key": "dry-run-1"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["after"] is None
    assert payload["dry_run"]["would_create"]["display_name"] == "Site principal"

    listing = client.get("/v2/sources")
    assert listing.json()["items"] == []


# --- Enveloppe appliquée aux actions de pipeline (tâches 5 + 6 combinées) ---


class _FakeExecutor(PipelineExecutorProtocol):
    """Exécuteur factice — jamais de câblage Kubernetes dans ces tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


def _seed_pipeline(engine, *, declared_state: str) -> str:
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": "default",
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {
                "id": "tbl1",
                "source_id": "src1",
                "schema_name": "PAYSLIB",
                "table_name": "ORDERS",
            },
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": declared_state},
        )
    return "ppl1"


@pytest.fixture()
def pipeline_app(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline-app.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    executor = _FakeExecutor()
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default", pipeline_executor=executor)
    try:
        yield app, engine, executor
    finally:
        engine.dispose()


def test_pause_action_transitions_the_declared_state_and_calls_the_fake_executor(pipeline_app) -> None:
    app, engine, executor = pipeline_app
    _seed_pipeline(engine, declared_state="copying")
    with TestClient(app) as client:
        response = client.post(
            "/v2/pipelines/ppl1/actions/pause", json={}, headers={"Idempotency-Key": "pause-1"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["before"]["declared_state"] == "copying"
    assert body["after"]["declared_state"] == "paused"
    assert executor.calls == [("ppl1", "pause")]


def test_action_dry_run_never_calls_the_executor(pipeline_app) -> None:
    app, engine, executor = pipeline_app
    _seed_pipeline(engine, declared_state="copying")
    with TestClient(app) as client:
        response = client.post(
            "/v2/pipelines/ppl1/actions/pause",
            json={"dry_run": True},
            headers={"Idempotency-Key": "pause-dry-1"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["after"] is None
    assert body["dry_run"]["would_transition"] == {"from": "copying", "to": "paused"}
    assert executor.calls == []


def test_forbidden_transition_returns_capability_unavailable_conflict(pipeline_app) -> None:
    app, engine, executor = pipeline_app
    # ``stopped`` est terminal : aucune action ne le fait redevenir actif.
    _seed_pipeline(engine, declared_state="stopped")
    with TestClient(app) as client:
        response = client.post(
            "/v2/pipelines/ppl1/actions/pause", json={}, headers={"Idempotency-Key": "forbidden-1"}
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capability_unavailable"
    assert executor.calls == []


def test_action_without_idempotency_key_is_rejected(pipeline_app) -> None:
    app, engine, _executor = pipeline_app
    _seed_pipeline(engine, declared_state="copying")
    with TestClient(app) as client:
        response = client.post("/v2/pipelines/ppl1/actions/pause", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
