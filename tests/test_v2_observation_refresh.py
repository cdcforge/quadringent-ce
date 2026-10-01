"""Rafraîchissement d'observation -> SSE (contrat §3.1, migration
``0005_pipeline_last_observed``).

Vérifie : la migration ajoute bien les colonnes, un premier rafraîchissement
(``None`` -> état) publie ``pipeline.state_changed``, un rafraîchissement
sans changement ne publie rien (pas de bruit SSE à chaque appel), une
entrée/sortie d'incident publie ``alert.fired``/``alert.resolved``, et
``refresh`` sur un pipeline inconnu échoue fermé.
"""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.events import EventsService
from quadringent_control_plane.v2.services.observation import PipelineObservation
from quadringent_control_plane.v2.services.observation_refresh import (
    ObservationRefreshService,
    PipelineNotFoundError,
)


def _absent_observation(state: str | None) -> PipelineObservation:
    return PipelineObservation(
        observed_state=state,
        lag_seconds=None,
        throughput_rows_per_second=None,
        rows_source=None,
        rows_destination=None,
        last_arrival_at=None,
        collected_at=None,
    )


class _SequencedObservationProvider:
    """Renvoie l'observation suivante de la séquence à chaque appel."""

    def __init__(self, states: list[str | None]) -> None:
        self._states = states
        self.calls: list[str] = []

    def observe(self, pipeline_id: str) -> PipelineObservation:
        self.calls.append(pipeline_id)
        state = self._states.pop(0)
        return _absent_observation(state)

    def metrics(self, pipeline_id, window):  # pragma: no cover - non utilisé ici
        raise NotImplementedError


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'observation_refresh.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    with eng.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
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


def test_migration_adds_last_observed_columns(engine) -> None:
    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == "ppl1")
        ).mappings().one()
    assert row["last_observed_state"] is None
    assert row["last_observed_at"] is None


def test_first_refresh_publishes_state_changed(engine) -> None:
    provider = _SequencedObservationProvider(["healthy"])
    events = EventsService(engine, org_id="default")
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider, events_service=events)

    service.refresh("ppl1")

    published = events.events_after(0)
    assert len(published) == 1
    assert published[0].event_type == "pipeline.state_changed"
    assert published[0].payload == {"pipeline_id": "ppl1", "from": None, "to": "healthy"}


def test_refresh_without_change_publishes_nothing(engine) -> None:
    provider = _SequencedObservationProvider(["healthy", "healthy"])
    events = EventsService(engine, org_id="default")
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider, events_service=events)

    service.refresh("ppl1")
    service.refresh("ppl1")

    published = events.events_after(0)
    assert len(published) == 1  # un seul événement, pas de bruit au second appel


def test_refresh_persists_last_observed_state(engine) -> None:
    provider = _SequencedObservationProvider(["degraded"])
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider)
    service.refresh("ppl1")
    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == "ppl1")
        ).mappings().one()
    assert row["last_observed_state"] == "degraded"
    assert row["last_observed_at"] is not None


def test_entering_incident_fires_alert(engine) -> None:
    provider = _SequencedObservationProvider(["healthy", "incident"])
    events = EventsService(engine, org_id="default")
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider, events_service=events)

    service.refresh("ppl1")
    service.refresh("ppl1")

    types = [event.event_type for event in events.events_after(0)]
    assert types == ["pipeline.state_changed", "pipeline.state_changed", "alert.fired"]


def test_leaving_incident_resolves_alert(engine) -> None:
    provider = _SequencedObservationProvider(["incident", "healthy"])
    events = EventsService(engine, org_id="default")
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider, events_service=events)

    service.refresh("ppl1")  # None -> incident : publie aussi alert.fired
    service.refresh("ppl1")  # incident -> healthy : publie alert.resolved

    types = [event.event_type for event in events.events_after(0)]
    assert types == [
        "pipeline.state_changed",
        "alert.fired",
        "pipeline.state_changed",
        "alert.resolved",
    ]


def test_refresh_unknown_pipeline_raises_not_found(engine) -> None:
    provider = _SequencedObservationProvider(["healthy"])
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider)
    with pytest.raises(PipelineNotFoundError):
        service.refresh("does-not-exist")


def test_refresh_all_iterates_every_pipeline(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl2", "source_id": "src1", "schema_name": "PAYSLIB", "table_name": "INVOICES"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl2", "table_id": "tbl2", "destination_id": "dst1", "declared_state": "paused"},
        )
    provider = _SequencedObservationProvider(["healthy", "degraded"])
    service = ObservationRefreshService(engine, org_id="default", observation_provider=provider)
    service.refresh_all()
    assert set(provider.calls) == {"ppl1", "ppl2"}
