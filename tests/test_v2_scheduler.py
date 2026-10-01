"""``ObservationRefreshScheduler`` (``v2/services/scheduler.py``, chantier
observabilité v2 suite) — ``run_once`` est la primitive testée en détail
(pas de thread ni de sommeil réel) ; ``start``/``stop`` sont vérifiés une
fois avec un intervalle court pour prouver que la boucle tourne bien en
tâche de fond, sans bloquer le test."""

from __future__ import annotations

import time

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.observation import PipelineObservation
from quadringent_control_plane.v2.services.observation_refresh import ObservationRefreshService
from quadringent_control_plane.v2.services.scheduler import ObservationRefreshScheduler
from quadringent_control_plane.v2.services.scheduler_lock import SchedulerLock


class _SequencedObservationProvider:
    def __init__(self, states: list[str | None]) -> None:
        self._states = states
        self.calls = 0

    def observe(self, pipeline_id: str) -> PipelineObservation:
        self.calls += 1
        state = self._states.pop(0) if self._states else None
        return PipelineObservation(
            observed_state=state,
            lag_seconds=None,
            throughput_rows_per_second=None,
            rows_source=None,
            rows_destination=None,
            last_arrival_at=None,
            collected_at=None,
        )

    def metrics(self, pipeline_id, window):  # pragma: no cover - non utilisé ici
        raise NotImplementedError


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'scheduler.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    with eng.begin() as connection:
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
    try:
        yield eng
    finally:
        eng.dispose()


def test_run_once_refreshes_when_lock_is_free(engine) -> None:
    provider = _SequencedObservationProvider(["healthy"])
    refresh_service = ObservationRefreshService(engine, org_id="default", observation_provider=provider)
    lock = SchedulerLock(engine, name="observation-refresh", holder="replica-a")
    scheduler = ObservationRefreshScheduler(refresh_service, lock)
    assert scheduler.run_once() is True
    assert provider.calls == 1


def test_run_once_skips_when_another_replica_holds_the_lock(engine) -> None:
    provider = _SequencedObservationProvider(["healthy"])
    refresh_service = ObservationRefreshService(engine, org_id="default", observation_provider=provider)
    other = SchedulerLock(engine, name="observation-refresh", holder="replica-other")
    assert other.try_acquire() is True
    lock = SchedulerLock(engine, name="observation-refresh", holder="replica-a")
    scheduler = ObservationRefreshScheduler(refresh_service, lock)
    assert scheduler.run_once() is False
    assert provider.calls == 0


def test_run_once_does_not_raise_when_refresh_fails(engine) -> None:
    class _BrokenProvider:
        def observe(self, pipeline_id):
            raise RuntimeError("boom")

        def metrics(self, pipeline_id, window):
            raise NotImplementedError

    refresh_service = ObservationRefreshService(engine, org_id="default", observation_provider=_BrokenProvider())
    lock = SchedulerLock(engine, name="observation-refresh", holder="replica-a")
    scheduler = ObservationRefreshScheduler(refresh_service, lock)
    assert scheduler.run_once() is True  # bail acquis, cycle échoué mais pas d'exception propagée


def test_start_and_stop_run_refresh_in_the_background(engine) -> None:
    provider = _SequencedObservationProvider(["healthy", "degraded", "healthy", "degraded", "healthy"])
    refresh_service = ObservationRefreshService(engine, org_id="default", observation_provider=provider)
    lock = SchedulerLock(engine, name="observation-refresh", holder="replica-a")
    scheduler = ObservationRefreshScheduler(refresh_service, lock, interval_seconds=0.02)
    scheduler.start()
    try:
        deadline = time.monotonic() + 2.0
        while provider.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert provider.calls >= 2
    finally:
        scheduler.stop()


def test_rejects_non_positive_interval(engine) -> None:
    lock = SchedulerLock(engine, name="observation-refresh", holder="replica-a")
    with pytest.raises(ValueError):
        ObservationRefreshScheduler(None, lock, interval_seconds=0)
