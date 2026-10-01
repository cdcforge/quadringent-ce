"""Boucle de réconciliation en arrière-plan (chantier 4, tâche 2).

Couvre le bail portable (acquisition, renouvellement, refus à un tiers non
expiré, reprise après expiration — sûreté à plusieurs réplicas), puis
``ReconciliationLoop.tick`` : preuve de copie -> ``live`` + évènement SSE,
Job en échec -> ``attention`` + raison explicite, jamais de Kubernetes réel
(exécuteur factice).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.events import EventsService
from quadringent_control_plane.v2.services.reconciler import (
    DEFAULT_LEASE_NAME,
    ReconciliationLoop,
    acquire_lease,
)

T0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'reconciler.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    try:
        yield engine
    finally:
        engine.dispose()


# --- Bail portable (plusieurs réplicas) -----------------------------------------


def test_first_replica_acquires_an_unclaimed_lease(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=T0)


def test_a_second_replica_cannot_steal_a_fresh_lease(engine) -> None:
    acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=T0)
    assert acquire_lease(engine, name="loop", holder="replica-b", ttl_seconds=30, now=T0) is False


def test_the_holder_can_renew_its_own_lease(engine) -> None:
    acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=T0)
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=T0 + timedelta(seconds=10))


def test_a_second_replica_takes_over_after_expiry(engine) -> None:
    acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=T0)
    later = T0 + timedelta(seconds=31)
    assert acquire_lease(engine, name="loop", holder="replica-b", ttl_seconds=30, now=later)


# --- ReconciliationLoop.tick -----------------------------------------------------


class _FakeReconcilerExecutor:
    def __init__(
        self,
        *,
        evidence_by_pipeline=None,
        job_outcome_by_pipeline=None,
        failing_sources: frozenset[str] = frozenset(),
        failing_destinations: frozenset[str] = frozenset(),
    ) -> None:
        self._evidence = evidence_by_pipeline or {}
        self._job_outcome = job_outcome_by_pipeline or {}
        self._failing_sources = failing_sources
        self._failing_destinations = failing_destinations
        self.evidence_calls: list[tuple[str, str]] = []
        self.job_outcome_calls: list[tuple[str, str]] = []
        self.reconcile_source_calls: list[str] = []
        self.reconcile_destination_calls: list[str] = []

    def copying_evidence(self, pipeline_id: str, run_id: str):
        self.evidence_calls.append((pipeline_id, run_id))
        return self._evidence.get(pipeline_id)

    def copy_job_outcome(self, pipeline_id: str, run_id: str) -> str:
        self.job_outcome_calls.append((pipeline_id, run_id))
        return self._job_outcome.get(pipeline_id, "running")

    def reconcile_source(self, source_id: str) -> None:
        self.reconcile_source_calls.append(source_id)
        if source_id in self._failing_sources:
            raise RuntimeError(f"reconciliation refusée pour la source {source_id}")

    def reconcile_destination(self, destination_id: str) -> None:
        self.reconcile_destination_calls.append(destination_id)
        if destination_id in self._failing_destinations:
            raise RuntimeError(f"reconciliation refusée pour la destination {destination_id}")


def _seed_pipeline(engine, *, pipeline_id="ppl1", declared_state="copying", active_run_id="run-1") -> None:
    with engine.begin() as connection:
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
            v2_schema.pipelines.insert(),
            {
                "id": pipeline_id,
                "table_id": "tbl1",
                "destination_id": "dst1",
                "declared_state": declared_state,
                "active_run_id": active_run_id,
            },
        )


def test_tick_promotes_a_pipeline_with_evidence_to_live(engine) -> None:
    _seed_pipeline(engine)
    executor = _FakeReconcilerExecutor(evidence_by_pipeline={"ppl1": object()})
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    result = loop.tick()

    assert result.lease_acquired is True
    assert result.promoted_to_live == ("ppl1",)
    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == "ppl1")
        ).mappings().first()
    assert row["declared_state"] == "live"


def test_tick_emits_an_sse_event_on_promotion(engine) -> None:
    _seed_pipeline(engine)
    executor = _FakeReconcilerExecutor(evidence_by_pipeline={"ppl1": object()})
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)
    loop.tick()

    events = EventsService(engine, org_id="default").events_after(0)
    assert len(events) == 1
    assert events[0].event_type == "pipeline.state_changed"
    assert events[0].payload["to"] == "live"


def test_tick_marks_attention_on_failed_job_with_a_next_action(engine) -> None:
    _seed_pipeline(engine)
    executor = _FakeReconcilerExecutor(job_outcome_by_pipeline={"ppl1": "failed"})
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    result = loop.tick()

    assert result.marked_attention == ("ppl1",)
    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == "ppl1")
        ).mappings().first()
    assert row["declared_state"] == "attention"
    assert "restart_initial_copy" in row["attention_reason"]


def test_tick_does_nothing_while_the_job_is_still_running(engine) -> None:
    _seed_pipeline(engine)
    executor = _FakeReconcilerExecutor(job_outcome_by_pipeline={"ppl1": "running"})
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    result = loop.tick()

    assert result.promoted_to_live == ()
    assert result.marked_attention == ()
    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.pipelines.select().where(v2_schema.pipelines.c.id == "ppl1")
        ).mappings().first()
    assert row["declared_state"] == "copying"


def test_tick_ignores_pipelines_without_an_active_run_id(engine) -> None:
    _seed_pipeline(engine, active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    loop.tick()

    assert executor.evidence_calls == []
    assert executor.job_outcome_calls == []


def test_tick_ignores_pipelines_not_in_copying_state(engine) -> None:
    _seed_pipeline(engine, declared_state="live")
    executor = _FakeReconcilerExecutor(evidence_by_pipeline={"ppl1": object()})
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    loop.tick()

    assert executor.evidence_calls == []


def test_tick_skips_all_work_when_lease_is_held_by_another_replica(engine) -> None:
    acquire_lease(engine, name=DEFAULT_LEASE_NAME, holder="another-replica", ttl_seconds=30, now=T0)
    _seed_pipeline(engine)
    executor = _FakeReconcilerExecutor(evidence_by_pipeline={"ppl1": object()})
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", holder_id="this-replica", now=lambda: T0)

    result = loop.tick()

    assert result.lease_acquired is False
    assert executor.evidence_calls == []
    assert executor.reconcile_source_calls == []
    assert executor.reconcile_destination_calls == []


# --- Réconciliation de dérive (tâche 3) -------------------------------------------


def test_tick_reconciles_the_source_and_destination_of_an_active_pipeline(engine) -> None:
    _seed_pipeline(engine, declared_state="live", active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    result = loop.tick()

    assert executor.reconcile_source_calls == ["src1"]
    assert executor.reconcile_destination_calls == ["dst1"]
    assert result.reconciled_sources == ("src1",)
    assert result.reconciled_destinations == ("dst1",)
    assert result.reconcile_errors == ()


def test_tick_reconciles_paused_pipelines_too(engine) -> None:
    """Pausé reste inclus : le manifeste désiré exclut de toute façon les
    tables pausées, mais un Deployment devenu orphelin doit pouvoir être
    retiré même sans nouvelle transition explicite."""

    _seed_pipeline(engine, declared_state="paused", active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    loop.tick()

    assert executor.reconcile_source_calls == ["src1"]
    assert executor.reconcile_destination_calls == ["dst1"]


def test_tick_does_not_reconcile_stopped_or_not_started_pipelines(engine) -> None:
    _seed_pipeline(engine, declared_state="stopped", active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    loop.tick()

    assert executor.reconcile_source_calls == []
    assert executor.reconcile_destination_calls == []


def test_tick_is_a_noop_write_when_the_executor_finds_nothing_to_update(engine) -> None:
    """L'exécuteur factice ne fait jamais d'écriture lui-même ; ce test
    documente seulement que le tour appelle bien la réconciliation même sans
    aucun événement de copie/échec à traiter (aucun ``active_run_id``)."""

    _seed_pipeline(engine, declared_state="live", active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    loop.tick()
    first_call_count = len(executor.reconcile_source_calls)
    loop.tick()

    # Rappelable à chaque tour, toujours le même nombre d'appels (idempotent
    # côté exécuteur réel via la comparaison d'empreinte — ici on vérifie
    # seulement que le tour ne s'arrête pas après un premier passage).
    assert len(executor.reconcile_source_calls) == first_call_count * 2


def test_tick_updates_a_source_image_change_by_calling_reconcile_again(engine) -> None:
    """Le tour ne calcule pas lui-même de diff d'image — c'est le rôle de
    ``reconcile_deployment`` côté exécuteur réel (idempotent par empreinte de
    spec). Ce test vérifie seulement que le tour réappelle bien l'exécuteur à
    chaque tour, qu'il y ait eu un changement de produit ou non : c'est ce
    rappel systématique qui garantit qu'une image mise à jour sera reprise
    sans transition explicite."""

    _seed_pipeline(engine, declared_state="live", active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    loop.tick()
    loop.tick()

    assert executor.reconcile_source_calls == ["src1", "src1"]


def test_tick_reconciliation_error_on_one_source_does_not_block_others(engine) -> None:
    with_working_source = "src-ok"
    with engine.begin() as connection:
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": with_working_source,
                "org_id": "default",
                "display_name": "Site secondaire",
                "ibmi_host": "as400-2.example.test",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl-ok", "source_id": with_working_source, "schema_name": "SALES", "table_name": "ORDLIN"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {
                "id": "ppl-ok",
                "table_id": "tbl-ok",
                "destination_id": "dst1",
                "declared_state": "live",
            },
        )
    _seed_pipeline(engine, declared_state="live", active_run_id=None)
    executor = _FakeReconcilerExecutor(failing_sources=frozenset({"src1"}))
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    result = loop.tick()

    assert sorted(executor.reconcile_source_calls) == ["src-ok", "src1"]
    assert result.reconciled_sources == ("src-ok",)
    assert result.reconcile_errors == ({"source_id": "src1", "error": "reconciliation refusée pour la source src1"},)


def test_tick_reconciliation_error_on_one_destination_does_not_block_others(engine) -> None:
    _seed_pipeline(engine, declared_state="live", active_run_id=None)
    executor = _FakeReconcilerExecutor(failing_destinations=frozenset({"dst1"}))
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", now=lambda: T0)

    result = loop.tick()

    assert result.reconciled_destinations == ()
    assert result.reconcile_errors == (
        {"destination_id": "dst1", "error": "reconciliation refusée pour la destination dst1"},
    )


def test_tick_respects_the_lease_for_drift_reconciliation_too(engine) -> None:
    acquire_lease(engine, name=DEFAULT_LEASE_NAME, holder="another-replica", ttl_seconds=30, now=T0)
    _seed_pipeline(engine, declared_state="live", active_run_id=None)
    executor = _FakeReconcilerExecutor()
    loop = ReconciliationLoop(engine, executor=executor, org_id="default", holder_id="this-replica", now=lambda: T0)

    result = loop.tick()

    assert result.lease_acquired is False
    assert executor.reconcile_source_calls == []
    assert result.reconciled_sources == ()
