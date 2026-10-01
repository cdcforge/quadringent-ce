"""Reprise après pause revient à l'état d'avant pause (chantier réconciliation, 24/09/2026).

Panne constatée sur le premier pipeline réel sur GKE : ``POST /v2/pipelines/
{id}/actions/resume`` sur un pipeline qui était ``live`` avant la pause le
faisait repasser en ``copying`` — ``_event_for_action`` traduisait ``resume``
en ``resume_copying`` sans jamais regarder l'état d'avant pause. Une reprise
doit revenir à ``live`` et reprendre la capture au checkpoint ; une nouvelle
copie initiale complète reste une action explicite (``restart_initial_copy``).
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.pipelines import PipelineExecutorProtocol, PipelinesService


class _FakeExecutor(PipelineExecutorProtocol):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline-resume.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
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
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "SALES", "table_name": "ORDHDR"},
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
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_pipeline(engine, *, declared_state: str, pipeline_id: str = "pipe1") -> None:
    with engine.begin() as connection:
        connection.execute(
            v2_schema.pipelines.insert(),
            {
                "id": pipeline_id,
                "table_id": "tbl1",
                "destination_id": "dst1",
                "declared_state": declared_state,
            },
        )


def test_pause_from_live_then_resume_returns_to_live_not_copying(engine) -> None:
    _seed_pipeline(engine, declared_state="live")
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    _, paused = service.apply_action("pipe1", "pause", executor=executor)
    assert paused.declared_state == "paused"

    _, resumed = service.apply_action("pipe1", "resume", executor=executor)

    assert resumed.declared_state == "live"
    assert executor.calls == [("pipe1", "pause"), ("pipe1", "resume_live")]


def test_pause_from_copying_then_resume_returns_to_copying(engine) -> None:
    _seed_pipeline(engine, declared_state="copying")
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    service.apply_action("pipe1", "pause", executor=executor)
    _, resumed = service.apply_action("pipe1", "resume", executor=executor)

    assert resumed.declared_state == "copying"
    assert executor.calls[-1] == ("pipe1", "resume_copying")


def test_state_before_pause_is_cleared_after_resume(engine) -> None:
    _seed_pipeline(engine, declared_state="live")
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    service.apply_action("pipe1", "pause", executor=executor)
    assert service.get("pipe1").state_before_pause == "live"

    service.apply_action("pipe1", "resume", executor=executor)
    assert service.get("pipe1").state_before_pause is None


def test_pause_resume_pause_resume_round_trip_tracks_the_latest_pre_pause_state(engine) -> None:
    """Un pipeline promu de ``copying`` à ``live`` entre deux pauses reprend
    à chaque fois au bon état — jamais l'ancienne valeur mémorisée."""

    _seed_pipeline(engine, declared_state="copying")
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    service.apply_action("pipe1", "pause", executor=executor)
    _, resumed = service.apply_action("pipe1", "resume", executor=executor)
    assert resumed.declared_state == "copying"

    # Promotion applicative (réconciliation/bootstrap), hors service pipeline :
    # simule la table passant à ``live`` avant la deuxième pause.
    with engine.begin() as connection:
        connection.execute(
            v2_schema.pipelines.update()
            .where(v2_schema.pipelines.c.id == "pipe1")
            .values(declared_state="live")
        )

    service.apply_action("pipe1", "pause", executor=executor)
    _, resumed_again = service.apply_action("pipe1", "resume", executor=executor)
    assert resumed_again.declared_state == "live"


def test_restart_initial_copy_remains_an_explicit_action_never_a_side_effect_of_resume(engine) -> None:
    _seed_pipeline(engine, declared_state="live")
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    service.apply_action("pipe1", "pause", executor=executor)
    service.apply_action("pipe1", "resume", executor=executor)

    assert ("pipe1", "restart_initial_copy") not in executor.calls


def test_plan_action_resume_dry_run_matches_applied_transition(engine) -> None:
    _seed_pipeline(engine, declared_state="live")
    executor = _FakeExecutor()
    service = PipelinesService(engine)

    service.apply_action("pipe1", "pause", executor=executor)
    plan = service.plan_action("pipe1", "resume")

    assert plan == {"would_transition": {"from": "paused", "to": "live"}}
