"""``GET /v2/pipelines/{id}/metrics?window=1h|24h`` (contrat §2.4).

Reprend la discipline ``model.LagSeriesProjection`` (résolution fixe,
absence explicite) sans dépendre de son type — voir
``services/observation.py``. Sans fournisseur injecté, la série est vide
avec une provenance ``absent`` (jamais des points inventés).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.observation import MetricPoint, MetricsSeries


def _seed_pipeline(engine, *, org_id: str = "default") -> None:
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": org_id, "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": org_id,
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
                "org_id": org_id,
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "PAYSLIB", "table_name": "ORDERS"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": "live"},
        )


class _FakeObservationProvider:
    def observe(self, pipeline_id):  # pragma: no cover - non utilisé ici
        raise NotImplementedError

    def metrics(self, pipeline_id: str, window: str) -> MetricsSeries:
        return MetricsSeries(
            window=window,
            points=(
                MetricPoint(at="2026-09-23T10:00:00Z", lag_seconds=2.0, throughput_rows_per_second=50.0),
                MetricPoint(at="2026-09-23T10:05:00Z", lag_seconds=None, throughput_rows_per_second=None),
            ),
            provenance="lag_series_projection",
            freshness="fresh",
            collected_at="2026-09-23T10:05:01Z",
        )


@pytest.fixture()
def client_and_engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline_metrics.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed_pipeline(engine)
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_observation_provider=_FakeObservationProvider(),
    )
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_route_returns_series_with_default_window_1h(client_and_engine) -> None:
    response = client_and_engine.get("/v2/pipelines/ppl1/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1h"
    assert len(body["points"]) == 2
    assert body["points"][0]["lag_seconds"] == 2.0
    assert body["points"][1]["lag_seconds"] is None
    assert body["provenance"] == "lag_series_projection"
    assert body["freshness"] == "fresh"


def test_route_accepts_window_24h(client_and_engine) -> None:
    response = client_and_engine.get("/v2/pipelines/ppl1/metrics", params={"window": "24h"})
    assert response.status_code == 200
    assert response.json()["window"] == "24h"


def test_route_rejects_unknown_window(client_and_engine) -> None:
    response = client_and_engine.get("/v2/pipelines/ppl1/metrics", params={"window": "7d"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_route_returns_404_for_unknown_pipeline(client_and_engine) -> None:
    response = client_and_engine.get("/v2/pipelines/does-not-exist/metrics")
    assert response.status_code == 404


def test_route_without_provider_returns_empty_series(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'pipeline_metrics_null.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed_pipeline(engine)
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    try:
        client = TestClient(app)
        response = client.get("/v2/pipelines/ppl1/metrics")
        assert response.status_code == 200
        body = response.json()
        assert body["points"] == []
        assert body["provenance"] == "absent"
    finally:
        engine.dispose()
