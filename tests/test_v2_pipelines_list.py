"""``GET /v2/pipelines`` (chantier observabilité v2, contrat §2.4).

Liste paginée joignant l'état déclaré (base v2) et les figures en direct
d'un fournisseur d'observation injecté — jamais fusionnées en dur : sans
fournisseur câblé, les champs observés restent ``None`` avec une raison
(``NullObservationProvider``, voir ``services/observation.py``).

Même principe que ``test_v2_store_postgres_identity.py`` : un round-trip
partagé (``_seed_and_list``) rejoué sur SQLite (rapide, toujours actif) et
sur un vrai Postgres 16 (``@pytest.mark.postgres``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.observation import (
    NullObservationProvider,
    PipelineObservation,
)
from quadringent_control_plane.v2.services.pipelines import InvalidListFilterError, PipelinesService


def _seed(engine, *, org_id: str = "default", id_prefix: str = "") -> None:
    """``id_prefix`` évite les collisions d'identifiants sur le Postgres de
    test partagé entre tests (session-scoped — voir ``tests/conftest.py``,
    même précaution que ``e04fa7f`` pour ``org1``)."""

    src1, src2 = f"{id_prefix}src1", f"{id_prefix}src2"
    dst1 = f"{id_prefix}dst1"
    tbl1, tbl2 = f"{id_prefix}tbl1", f"{id_prefix}tbl2"
    ppl1, ppl2 = f"{id_prefix}ppl1", f"{id_prefix}ppl2"
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": org_id, "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            [
                {
                    "id": src1,
                    "org_id": org_id,
                    "display_name": "Site principal",
                    "ibmi_host": "as400.example.com",
                    "ibmi_user": "QSVCUSER",
                    "secret_ciphertext": "gAAAAA==",
                },
                {
                    "id": src2,
                    "org_id": org_id,
                    "display_name": "Site secondaire",
                    "ibmi_host": "as400b.example.com",
                    "ibmi_user": "QSVCUSER2",
                    "secret_ciphertext": "gAAAAA==",
                },
            ],
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": dst1,
                "org_id": org_id,
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            [
                {"id": tbl1, "source_id": src1, "schema_name": "PAYSLIB", "table_name": "ORDERS"},
                {"id": tbl2, "source_id": src2, "schema_name": "PAYSLIB", "table_name": "INVOICES"},
            ],
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            [
                {"id": ppl1, "table_id": tbl1, "destination_id": dst1, "declared_state": "live"},
                {"id": ppl2, "table_id": tbl2, "destination_id": dst1, "declared_state": "paused"},
            ],
        )


class _FakeObservationProvider:
    def __init__(self, by_id: dict[str, PipelineObservation]) -> None:
        self._by_id = by_id

    def observe(self, pipeline_id: str) -> PipelineObservation:
        return self._by_id.get(pipeline_id, NullObservationProvider().observe(pipeline_id))

    def metrics(self, pipeline_id, window):  # pragma: no cover - non utilisé ici
        raise NotImplementedError


def _round_trip(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        prefix = "pgroundtrip-"
        _seed(engine, org_id="org-pipelines-list", id_prefix=prefix)
        service = PipelinesService(engine)
        ppl1, ppl2 = f"{prefix}ppl1", f"{prefix}ppl2"
        src2 = f"{prefix}src2"

        # Filtré par destination (propre à ce round-trip) pour ignorer
        # d'éventuelles autres lignes du Postgres de test partagé.
        records, next_cursor = service.list(destination_id=f"{prefix}dst1")
        assert next_cursor is None
        assert {record.id for record in records} == {ppl1, ppl2}
        # jointure table -> source correcte
        by_id = {record.id: record for record in records}
        assert by_id[ppl1].source_id == f"{prefix}src1"
        assert by_id[ppl2].source_id == src2
        # sans fournisseur injecté : observation absente, jamais 0
        assert by_id[ppl1].observation.lag_seconds is None
        assert by_id[ppl1].observation.rows_source is None

        live_only, _ = service.list(state="live", destination_id=f"{prefix}dst1")
        assert [record.id for record in live_only] == [ppl1]

        src2_only, _ = service.list(source_id=src2)
        assert [record.id for record in src2_only] == [ppl2]
    finally:
        engine.dispose()


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipelines_list.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    _seed(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_list_joins_declared_state_with_source_via_table(engine) -> None:
    service = PipelinesService(engine)
    records, next_cursor = service.list()
    assert next_cursor is None
    by_id = {record.id: record for record in records}
    assert by_id["ppl1"].source_id == "src1"
    assert by_id["ppl1"].destination_id == "dst1"
    assert by_id["ppl1"].declared_state == "live"


def test_list_filters_by_state(engine) -> None:
    service = PipelinesService(engine)
    records, _ = service.list(state="paused")
    assert [record.id for record in records] == ["ppl2"]


def test_list_filters_by_source_id(engine) -> None:
    service = PipelinesService(engine)
    records, _ = service.list(source_id="src2")
    assert [record.id for record in records] == ["ppl2"]


def test_list_filters_by_destination_id(engine) -> None:
    service = PipelinesService(engine)
    records, _ = service.list(destination_id="dst1")
    assert {record.id for record in records} == {"ppl1", "ppl2"}


def test_list_rejects_unknown_state_filter(engine) -> None:
    service = PipelinesService(engine)
    with pytest.raises(InvalidListFilterError):
        service.list(state="not-a-state")


def test_list_paginates_with_opaque_cursor(engine) -> None:
    service = PipelinesService(engine)
    first_page, cursor = service.list(limit=1)
    assert [record.id for record in first_page] == ["ppl1"]
    assert cursor == "ppl1"
    second_page, cursor2 = service.list(limit=1, cursor=cursor)
    assert [record.id for record in second_page] == ["ppl2"]
    assert cursor2 is None


def test_list_without_provider_leaves_observed_fields_null_with_reason(engine) -> None:
    service = PipelinesService(engine)
    records, _ = service.list()
    record = next(record for record in records if record.id == "ppl1")
    assert record.observation.observed_state is None
    assert record.observation.lag_seconds is None
    assert record.observation.rows_source is None
    assert record.observation.absent_reasons["lag_seconds"]


def test_list_merges_injected_observation(engine) -> None:
    observation = PipelineObservation(
        observed_state="healthy",
        lag_seconds=3.5,
        throughput_rows_per_second=120.0,
        rows_source=1000,
        rows_destination=998,
        last_arrival_at="2026-09-23T10:00:00Z",
        collected_at="2026-09-23T10:00:05Z",
    )
    service = PipelinesService(engine)
    records, _ = service.list(observation_provider=_FakeObservationProvider({"ppl1": observation}))
    record = next(record for record in records if record.id == "ppl1")
    payload = record.to_dict()
    assert payload["observed_state"] == "healthy"
    assert payload["lag_seconds"] == 3.5
    assert payload["rows_source"] == 1000
    assert payload["declared_state"] == "live"


# --- Route HTTP --------------------------------------------------------


@pytest.fixture()
def client_and_engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipelines_list_app.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    _seed(eng)
    secret_box = SecretBox(SecretBox.generate_key())
    observation = PipelineObservation(
        observed_state="healthy",
        lag_seconds=1.0,
        throughput_rows_per_second=10.0,
        rows_source=100,
        rows_destination=100,
        last_arrival_at="2026-09-23T10:00:00Z",
        collected_at="2026-09-23T10:00:00Z",
    )
    app = create_v2_app(
        engine=eng,
        secret_box=secret_box,
        pipeline_observation_provider=_FakeObservationProvider({"ppl1": observation}),
    )
    try:
        yield TestClient(app), eng
    finally:
        eng.dispose()


def test_route_lists_pipelines_with_envelope_shape(client_and_engine) -> None:
    client, _engine = client_and_engine
    response = client.get("/v2/pipelines")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    ids = {item["id"] for item in body["items"]}
    assert ids == {"ppl1", "ppl2"}
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["ppl1"]["observed_state"] == "healthy"
    assert by_id["ppl1"]["lag_seconds"] == 1.0
    assert by_id["ppl2"]["observed_state"] is None
    assert by_id["ppl2"]["rows_source"] is None


def test_route_filters_by_state_query_param(client_and_engine) -> None:
    client, _engine = client_and_engine
    response = client.get("/v2/pipelines", params={"state": "paused"})
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == ["ppl2"]


def test_route_rejects_unknown_state_with_invalid_request(client_and_engine) -> None:
    client, _engine = client_and_engine
    response = client.get("/v2/pipelines", params={"state": "bogus"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_route_paginates_with_limit_and_cursor(client_and_engine) -> None:
    client, _engine = client_and_engine
    first = client.get("/v2/pipelines", params={"limit": 1})
    assert first.status_code == 200
    first_body = first.json()
    assert [item["id"] for item in first_body["items"]] == ["ppl1"]
    assert first_body["next_cursor"] == "ppl1"

    second = client.get("/v2/pipelines", params={"limit": 1, "cursor": first_body["next_cursor"]})
    second_body = second.json()
    assert [item["id"] for item in second_body["items"]] == ["ppl2"]
    assert second_body["next_cursor"] is None


@pytest.mark.postgres
def test_round_trip_on_postgres(postgres_dsn) -> None:
    _round_trip(postgres_dsn)
