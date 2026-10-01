"""``PipelinesService.apply_scope_action``/``plan_scope_action`` — marqueur de portée.

Tests de service pur (pas de route HTTP) pour le complément chantier 4 :
distinction entre une pause de portée (source/destination/organisation) et
une pause individuelle, via ``pipelines.paused_by_scope_action``.
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.pipelines import (
    PipelineExecutorProtocol,
    PipelineExecutorUnavailableError,
    PipelinesService,
    UnknownActionError,
)


class _FakeExecutor(PipelineExecutorProtocol):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipelines-scope-marker.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site",
                "ibmi_host": "as400.example.test",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "x",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": "default",
                "snowflake_account": "acct",
                "key_pair_ciphertext": "x",
                "setup_script": "-- x",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "S", "table_name": "T1"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": "copying"},
        )
    try:
        yield engine
    finally:
        engine.dispose()


def test_scope_pause_sets_the_marker(engine) -> None:
    service = PipelinesService(engine)
    executor = _FakeExecutor()
    service.apply_scope_action(("ppl1",), "pause", executor=executor)
    assert service._is_scope_paused("ppl1") is True


def test_scope_resume_clears_the_marker(engine) -> None:
    service = PipelinesService(engine)
    executor = _FakeExecutor()
    service.apply_scope_action(("ppl1",), "pause", executor=executor)
    result = service.apply_scope_action(("ppl1",), "resume", executor=executor)
    assert result["applied"][0]["pipeline_id"] == "ppl1"
    assert service._is_scope_paused("ppl1") is False


def test_individual_pause_never_sets_the_marker(engine) -> None:
    service = PipelinesService(engine)
    executor = _FakeExecutor()
    service.apply_action("ppl1", "pause", executor=executor)
    assert service._is_scope_paused("ppl1") is False


def test_resume_scope_skips_an_individually_paused_pipeline(engine) -> None:
    service = PipelinesService(engine)
    executor = _FakeExecutor()
    service.apply_action("ppl1", "pause", executor=executor)  # pause individuelle

    result = service.apply_scope_action(("ppl1",), "resume", executor=executor)

    assert result["applied"] == []
    assert result["skipped"][0]["pipeline_id"] == "ppl1"
    assert service.get("ppl1").declared_state == "paused"  # inchangé


def test_apply_scope_action_rejects_an_unsupported_verb(engine) -> None:
    service = PipelinesService(engine)
    with pytest.raises(UnknownActionError):
        service.apply_scope_action(("ppl1",), "remove", executor=_FakeExecutor())


def test_apply_scope_action_with_empty_scope_never_requires_an_executor(engine) -> None:
    service = PipelinesService(engine)
    result = service.apply_scope_action((), "pause", executor=None)
    assert result == {"applied": [], "skipped": []}


def test_apply_scope_action_fails_closed_without_an_executor_when_pipelines_exist(engine) -> None:
    service = PipelinesService(engine)
    with pytest.raises(PipelineExecutorUnavailableError):
        service.apply_scope_action(("ppl1",), "pause", executor=None)
