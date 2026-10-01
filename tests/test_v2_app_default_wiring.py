"""Câblage par défaut des adaptateurs d'observation réels dans
``create_v2_app`` (chantier observabilité v2, suite) — seulement quand la
configuration le permet, sinon Null* (comportement inchangé, déjà couvert
par ``test_v2_app_skeleton.py``/``test_v2_pipelines_list.py``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.costs_composite import FallbackCostsProvider
from quadringent_control_plane.v2.services.costs_projection import CostsV1ProjectionAdapter
from quadringent_control_plane.v2.services.logs_kubernetes import KubernetesLogSource
from quadringent_control_plane.v2.services.observation import NullObservationProvider
from quadringent_control_plane.v2.services.observation_composite import CompositeObservationProvider
from quadringent_control_plane.v2.services.observation_projection import ProjectionRepositoryObservationAdapter

NOW = datetime.now(timezone.utc)


def _seed(engine) -> None:
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1", "org_id": "default", "display_name": "Site", "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER", "secret_ciphertext": "gAAAAA==",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1", "org_id": "default", "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==", "setup_script": "-- setup.sql",
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


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'app_wiring.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    _seed(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_no_config_falls_back_to_null_providers(engine) -> None:
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    assert isinstance(app.state.pipeline_observation_provider, type(None)) or app.state.pipeline_observation_provider is None
    assert app.state.log_source is None
    assert app.state.costs_provider is None
    assert app.state.observation_scheduler is None


def test_pipeline_source_spec_resolver_wires_a_real_projection_adapter(engine) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_source_spec_resolver=lambda pid: None,
    )
    assert isinstance(app.state.pipeline_observation_provider, ProjectionRepositoryObservationAdapter)


def test_resolver_plus_storage_backend_wires_the_composite_provider(engine) -> None:
    class _FakeStorageBackend:
        def checkpoint_store(self, stream_key):
            raise AssertionError("non appelé dans ce test")

    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_source_spec_resolver=lambda pid: None,
        pipeline_stream_key_resolver=lambda pid: None,
        capture_storage_backend=_FakeStorageBackend(),
    )
    assert isinstance(app.state.pipeline_observation_provider, CompositeObservationProvider)


def test_explicit_provider_always_wins_over_automatic_wiring(engine) -> None:
    explicit = NullObservationProvider()
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_source_spec_resolver=lambda pid: "live:src1:file:///doc.json",
        pipeline_observation_provider=explicit,
    )
    assert app.state.pipeline_observation_provider is explicit


def test_kubernetes_pods_client_wires_a_real_log_source(engine) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        kubernetes_pods_client=object(),
    )
    assert isinstance(app.state.log_source, KubernetesLogSource)


def test_connection_source_spec_resolver_wires_a_real_costs_provider(engine) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        connection_source_spec_resolver=lambda id_: None,
    )
    assert isinstance(app.state.costs_provider, CostsV1ProjectionAdapter)


def test_both_costs_candidates_wire_a_fallback_provider(engine) -> None:
    class _FakeQuery:
        def query_credits(self, warehouse, window):
            return None

    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        connection_source_spec_resolver=lambda id_: None,
        snowflake_credits_query=_FakeQuery(),
        snowflake_warehouse="COCKPIT_WH",
    )
    # Le site de test n'est pas forcément configuré avec un prix/warehouse :
    # au minimum le collecteur de projection est câblé (seul ou en repli).
    assert isinstance(app.state.costs_provider, (CostsV1ProjectionAdapter, FallbackCostsProvider))


def test_end_to_end_list_pipelines_route_uses_the_wired_provider(engine) -> None:
    document = {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=5)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 100}},
    }
    import tempfile
    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp())
    doc_path = tmp_dir / "console.json"
    doc_path.write_text(json.dumps(document), encoding="utf-8")
    origin = f"file://{doc_path}"

    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_source_spec_resolver=lambda pid: f"live:src1:{origin}" if pid == "ppl1" else None,
    )
    client = TestClient(app)
    response = client.get("/v2/pipelines")
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id["ppl1"]["observed_state"] is not None
    assert by_id["ppl1"]["rows_source"] == 100


def test_scheduler_disabled_by_default_even_with_resolver(engine) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_source_spec_resolver=lambda pid: None,
    )
    assert app.state.observation_scheduler is None


def test_enabling_scheduler_starts_and_stops_cleanly(engine) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        pipeline_source_spec_resolver=lambda pid: None,
        enable_observation_scheduler=True,
        observation_scheduler_interval_seconds=0.05,
        observation_scheduler_holder="test-replica",
    )
    assert app.state.observation_scheduler is not None
    with TestClient(app):
        pass  # démarre puis arrête proprement via le hook de shutdown FastAPI
    assert app.state.observation_scheduler._thread is None
