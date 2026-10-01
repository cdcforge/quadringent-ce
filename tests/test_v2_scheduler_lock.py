"""``SchedulerLock`` (``v2/services/scheduler_lock.py``, chantier
observabilité v2 suite) — bail portable SQLite/Postgres, sûr en
multi-réplicas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2 import db as v2_db
from quadringent_control_plane.v2.services.scheduler_lock import SchedulerLock

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'scheduler_lock.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    try:
        yield eng
    finally:
        eng.dispose()


def test_first_replica_acquires_an_unheld_lock(engine) -> None:
    lock = SchedulerLock(engine, name="observation-refresh", holder="replica-a")
    assert lock.try_acquire(now=NOW) is True


def test_second_replica_cannot_acquire_a_held_lock(engine) -> None:
    a = SchedulerLock(engine, name="observation-refresh", holder="replica-a", lease_seconds=30)
    b = SchedulerLock(engine, name="observation-refresh", holder="replica-b", lease_seconds=30)
    assert a.try_acquire(now=NOW) is True
    assert b.try_acquire(now=NOW + timedelta(seconds=1)) is False


def test_same_holder_renews_its_own_lease(engine) -> None:
    a = SchedulerLock(engine, name="observation-refresh", holder="replica-a", lease_seconds=30)
    assert a.try_acquire(now=NOW) is True
    assert a.try_acquire(now=NOW + timedelta(seconds=10)) is True


def test_expired_lease_can_be_taken_over_by_another_replica(engine) -> None:
    a = SchedulerLock(engine, name="observation-refresh", holder="replica-a", lease_seconds=5)
    b = SchedulerLock(engine, name="observation-refresh", holder="replica-b", lease_seconds=5)
    assert a.try_acquire(now=NOW) is True
    # Le bail de 5s a expiré : un autre réplica peut le reprendre.
    assert b.try_acquire(now=NOW + timedelta(seconds=6)) is True
    assert a.try_acquire(now=NOW + timedelta(seconds=7)) is False


def test_release_frees_the_lock_for_others(engine) -> None:
    a = SchedulerLock(engine, name="observation-refresh", holder="replica-a", lease_seconds=30)
    b = SchedulerLock(engine, name="observation-refresh", holder="replica-b", lease_seconds=30)
    assert a.try_acquire(now=NOW) is True
    a.release()
    assert b.try_acquire(now=NOW + timedelta(seconds=1)) is True


def test_release_by_non_holder_is_a_no_op(engine) -> None:
    a = SchedulerLock(engine, name="observation-refresh", holder="replica-a", lease_seconds=30)
    b = SchedulerLock(engine, name="observation-refresh", holder="replica-b", lease_seconds=30)
    assert a.try_acquire(now=NOW) is True
    b.release()  # b ne détient rien : ne doit pas libérer le bail de a
    assert b.try_acquire(now=NOW + timedelta(seconds=1)) is False


def test_different_lock_names_are_independent(engine) -> None:
    a = SchedulerLock(engine, name="task-a", holder="replica-a", lease_seconds=30)
    b = SchedulerLock(engine, name="task-b", holder="replica-b", lease_seconds=30)
    assert a.try_acquire(now=NOW) is True
    assert b.try_acquire(now=NOW) is True


def test_rejects_non_positive_lease() -> None:
    with pytest.raises(ValueError):
        SchedulerLock(object(), name="x", holder="y", lease_seconds=0)


def _round_trip(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        name = "observation-refresh-pg-roundtrip"
        a = SchedulerLock(engine, name=name, holder="replica-a", lease_seconds=30)
        b = SchedulerLock(engine, name=name, holder="replica-b", lease_seconds=30)
        assert a.try_acquire() is True
        assert b.try_acquire() is False  # un seul réplica détient le bail à la fois, y compris sur un vrai Postgres
        a.release()
        assert b.try_acquire() is True
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_round_trip_on_postgres(postgres_dsn) -> None:
    _round_trip(postgres_dsn)
