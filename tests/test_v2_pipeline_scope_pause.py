"""Pause/reprise de portée agit réellement sur les pipelines (complément chantier 4).

``0006_source_destination_pause`` (chantier MCP/CLI) ne posait qu'un
marqueur d'intention (``sources.paused_at``/``destinations.paused_at``/
``organizations.paused_at``) sans jamais arrêter les flux — ce module
vérifie le complément : `POST /v2/sources/{id}/actions/pause`,
`/v2/destinations/{id}/actions/pause` et `/v2/actions/pause_all` pausent
réellement chaque pipeline `copying`/`live` concerné via l'exécuteur
injecté (jamais de Kubernetes réel — exécuteur factice), et leur reprise ne
relance jamais une table que l'utilisateur avait mise en pause
individuellement (``pipelines.paused_by_scope_action``). Audit (enveloppe
générique existante) et évènements SSE (`pipeline.state_changed`) couverts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.events import EventsService
from quadringent_control_plane.v2.services.pipelines import PipelineExecutorProtocol


class _FakeExecutor(PipelineExecutorProtocol):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


def _seed(engine) -> None:
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.test",
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
            {"id": "tbl1", "source_id": "src1", "schema_name": "SALES", "table_name": "ORDHDR"},
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl2", "source_id": "src1", "schema_name": "SALES", "table_name": "ORDLIN"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": "copying"},
        )
        # ppl2 est déjà pausé individuellement, AVANT toute pause de portée —
        # jamais relancé par une reprise de source/destination/organisation.
        connection.execute(
            v2_schema.pipelines.insert(),
            {
                "id": "ppl2",
                "table_id": "tbl2",
                "destination_id": "dst1",
                "declared_state": "paused",
                "paused_by_scope_action": False,
            },
        )


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline-scope-pause.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine)
    secret_box = SecretBox(SecretBox.generate_key())
    executor = _FakeExecutor()
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default", pipeline_executor=executor)
    try:
        yield TestClient(app), engine, executor
    finally:
        engine.dispose()


def _declared_state(engine, pipeline_id: str) -> str:
    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == pipeline_id)
        ).mappings().first()
    return row["declared_state"]


# --- Source ------------------------------------------------------------------


def test_pause_source_pauses_live_pipelines_and_skips_already_paused_ones(client) -> None:
    http, engine, executor = client
    response = http.post(
        "/v2/sources/src1/actions/pause", json={}, headers={"Idempotency-Key": "src-pause-1"}
    )
    assert response.status_code == 200
    pipelines = response.json()["after"]["pipelines"]
    assert [item["pipeline_id"] for item in pipelines["applied"]] == ["ppl1"]
    assert pipelines["skipped"][0]["pipeline_id"] == "ppl2"
    assert executor.calls == [("ppl1", "pause")]
    assert _declared_state(engine, "ppl1") == "paused"
    assert _declared_state(engine, "ppl2") == "paused"  # inchangé, déjà pausé


def test_resume_source_does_not_relaunch_an_individually_paused_table(client) -> None:
    http, engine, executor = client
    http.post("/v2/sources/src1/actions/pause", json={}, headers={"Idempotency-Key": "src-pause-2"})
    executor.calls.clear()

    response = http.post(
        "/v2/sources/src1/actions/resume", json={}, headers={"Idempotency-Key": "src-resume-2"}
    )

    pipelines = response.json()["after"]["pipelines"]
    assert [item["pipeline_id"] for item in pipelines["applied"]] == ["ppl1"]
    assert pipelines["skipped"][0]["pipeline_id"] == "ppl2"
    assert _declared_state(engine, "ppl1") == "copying"
    assert _declared_state(engine, "ppl2") == "paused"  # jamais relancé


def test_pause_source_emits_an_sse_event_per_affected_pipeline(client) -> None:
    http, engine, _executor = client
    http.post("/v2/sources/src1/actions/pause", json={}, headers={"Idempotency-Key": "src-pause-3"})

    events = EventsService(engine, org_id="default").events_after(0)
    assert len(events) == 1
    assert events[0].event_type == "pipeline.state_changed"
    assert events[0].payload["pipeline_id"] == "ppl1"
    assert events[0].payload["to"] == "paused"
    assert events[0].payload["cause"] == "source.pause"


def test_pause_source_without_an_executor_is_503(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'no-exec.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed(engine)
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    http = TestClient(app)
    response = http.post(
        "/v2/sources/src1/actions/pause", json={}, headers={"Idempotency-Key": "src-pause-no-exec"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "executor_unavailable"
    engine.dispose()


# --- Destination ---------------------------------------------------------------


def test_pause_destination_pauses_its_live_pipelines(client) -> None:
    http, engine, executor = client
    response = http.post(
        "/v2/destinations/dst1/actions/pause", json={}, headers={"Idempotency-Key": "dst-pause-1"}
    )
    pipelines = response.json()["after"]["pipelines"]
    assert [item["pipeline_id"] for item in pipelines["applied"]] == ["ppl1"]
    assert _declared_state(engine, "ppl1") == "paused"


# --- Organisation (pause_all/resume_all) ----------------------------------------


def _approve_and_apply(http, action: str, key: str) -> dict:
    response = http.post(f"/v2/actions/{action}", json={}, headers={"Idempotency-Key": key})
    assert response.status_code == 409  # confirmation requise (action sensible)
    confirmation_id = response.json()["error"]["message"].split("id=")[1].split(")")[0]
    approve = http.post(
        f"/v2/confirmations/{confirmation_id}/approve",
        json={},
        headers={"Idempotency-Key": key + "-approve"},
    )
    assert approve.status_code == 200
    return http.post(
        f"/v2/actions/{action}",
        json={"confirmation_token": confirmation_id},
        headers={"Idempotency-Key": key + "-confirmed"},
    )


def test_pause_all_pauses_every_live_pipeline_across_the_organization(client) -> None:
    http, engine, executor = client
    response = _approve_and_apply(http, "pause_all", "org-pause-1")
    assert response.status_code == 200
    pipelines = response.json()["after"]["pipelines"]
    assert [item["pipeline_id"] for item in pipelines["applied"]] == ["ppl1"]
    assert _declared_state(engine, "ppl1") == "paused"
    assert _declared_state(engine, "ppl2") == "paused"  # déjà pausé, inchangé


def test_resume_all_never_relaunches_an_individually_paused_pipeline(client) -> None:
    http, engine, executor = client
    _approve_and_apply(http, "pause_all", "org-pause-2")
    response = _approve_and_apply(http, "resume_all", "org-resume-2")
    assert response.status_code == 200
    pipelines = response.json()["after"]["pipelines"]
    assert [item["pipeline_id"] for item in pipelines["applied"]] == ["ppl1"]
    assert _declared_state(engine, "ppl1") == "copying"
    assert _declared_state(engine, "ppl2") == "paused"
