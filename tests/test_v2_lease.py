"""``services/lease.py`` : bail applicatif unifié, portable SQLite/Postgres,
partagé par la boucle de réconciliation et l'ordonnanceur d'observation
(migration 0010_unify_leases, table ``leases``).

Couvre le cycle de vie complet : acquisition, renouvellement par le même
titulaire, refus d'un tiers sur un bail non expiré, reprise après
expiration, libération anticipée — puis le fencing (compteur de
génération) : un titulaire qui a perdu le bail ne doit plus jamais pouvoir
agir comme s'il le détenait encore, même s'il n'a pas observé l'expiration
lui-même.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from quadringent_control_plane.v2 import db as v2_db
from quadringent_control_plane.v2.services.lease import Lease, acquire_lease, release_lease

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'lease.sqlite3'}"
    v2_db.run_migrations(dsn)
    eng = v2_db.create_engine_for(dsn)
    try:
        yield eng
    finally:
        eng.dispose()


# --- Fonctions bas niveau (acquire_lease / release_lease) ------------------


def test_first_replica_acquires_an_unheld_lease(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=NOW) is True


def test_second_replica_cannot_acquire_a_held_lease(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=NOW) is True
    assert acquire_lease(engine, name="loop", holder="replica-b", ttl_seconds=30, now=NOW + timedelta(seconds=1)) is False


def test_same_holder_renews_its_own_lease(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=NOW) is True
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=NOW + timedelta(seconds=10)) is True


def test_expired_lease_can_be_taken_over_by_another_replica(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=5, now=NOW) is True
    later = NOW + timedelta(seconds=6)
    assert acquire_lease(engine, name="loop", holder="replica-b", ttl_seconds=5, now=later) is True
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=5, now=later + timedelta(seconds=1)) is False


def test_release_frees_the_lease_for_others(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=NOW) is True
    release_lease(engine, name="loop", holder="replica-a")
    assert acquire_lease(engine, name="loop", holder="replica-b", ttl_seconds=30, now=NOW + timedelta(seconds=1)) is True


def test_release_by_non_holder_is_a_no_op(engine) -> None:
    assert acquire_lease(engine, name="loop", holder="replica-a", ttl_seconds=30, now=NOW) is True
    release_lease(engine, name="loop", holder="replica-b")  # b ne détient rien
    assert acquire_lease(engine, name="loop", holder="replica-b", ttl_seconds=30, now=NOW + timedelta(seconds=1)) is False


def test_different_lease_names_are_independent(engine) -> None:
    assert acquire_lease(engine, name="task-a", holder="replica-a", ttl_seconds=30, now=NOW) is True
    assert acquire_lease(engine, name="task-b", holder="replica-b", ttl_seconds=30, now=NOW) is True


# --- Classe Lease (état + fencing) ------------------------------------------


def test_lease_class_acquire_and_generation(engine) -> None:
    lease = Lease(engine, name="loop", holder="replica-a", ttl_seconds=30)
    assert lease.acquire(now=NOW) is True
    assert lease.generation == 1


def test_generation_advances_on_takeover_not_on_renewal(engine) -> None:
    a = Lease(engine, name="loop", holder="replica-a", ttl_seconds=5)
    assert a.acquire(now=NOW) is True
    assert a.generation == 1
    # Renouvellement par le même titulaire : la génération ne change pas.
    assert a.acquire(now=NOW + timedelta(seconds=1)) is True
    assert a.generation == 1

    b = Lease(engine, name="loop", holder="replica-b", ttl_seconds=5)
    later = NOW + timedelta(seconds=6)
    assert b.acquire(now=later) is True
    # Nouvelle attribution (bail expiré, autre titulaire) : la génération avance.
    assert b.generation == 2


def test_stale_holder_is_rejected_after_losing_the_lease(engine) -> None:
    a = Lease(engine, name="loop", holder="replica-a", ttl_seconds=5)
    assert a.acquire(now=NOW) is True
    assert a.is_current(now=NOW) is True

    b = Lease(engine, name="loop", holder="replica-b", ttl_seconds=5)
    later = NOW + timedelta(seconds=6)
    assert b.acquire(now=later) is True

    # ``a`` n'a jamais observé l'expiration ni de refus explicite (ex. long
    # GC pause) : le contrôle de fencing doit malgré tout détecter qu'il a
    # perdu le bail, avant toute action sur la ressource protégée.
    assert a.is_current(now=later) is False
    assert b.is_current(now=later) is True


def test_is_current_is_false_before_any_acquire(engine) -> None:
    lease = Lease(engine, name="loop", holder="replica-a", ttl_seconds=30)
    assert lease.is_current(now=NOW) is False


def test_is_current_is_false_once_expired_even_for_the_last_holder(engine) -> None:
    a = Lease(engine, name="loop", holder="replica-a", ttl_seconds=5)
    assert a.acquire(now=NOW) is True
    assert a.is_current(now=NOW + timedelta(seconds=10)) is False


def test_lease_release_clears_generation(engine) -> None:
    a = Lease(engine, name="loop", holder="replica-a", ttl_seconds=30)
    assert a.acquire(now=NOW) is True
    a.release()
    assert a.generation is None
    assert a.is_current(now=NOW) is False


def test_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError):
        Lease(object(), name="x", holder="y", ttl_seconds=0)


# --- Concurrence (deux titulaires distincts, mêmes appels) ------------------


def test_two_concurrent_holders_only_one_ever_holds_the_lease_at_once(engine) -> None:
    a = Lease(engine, name="concurrent", holder="replica-a", ttl_seconds=30)
    b = Lease(engine, name="concurrent", holder="replica-b", ttl_seconds=30)
    results = [a.acquire(now=NOW), b.acquire(now=NOW)]
    assert sorted(results) == [False, True]


# --- Postgres réel -----------------------------------------------------------


def _round_trip(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        name = "lease-pg-roundtrip"
        a = Lease(engine, name=name, holder="replica-a", ttl_seconds=30)
        b = Lease(engine, name=name, holder="replica-b", ttl_seconds=30)
        assert a.acquire() is True
        assert b.acquire() is False
        assert a.is_current() is True
        a.release()
        assert b.acquire() is True
        assert a.is_current() is False
    finally:
        engine.dispose()


def _expiry_and_takeover(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        name = "lease-pg-expiry-takeover"
        a = Lease(engine, name=name, holder="replica-a", ttl_seconds=1)
        b = Lease(engine, name=name, holder="replica-b", ttl_seconds=1)
        assert a.acquire() is True
        assert a.generation == 1
        # Bail tenu et non expiré : b ne peut pas le prendre.
        assert b.acquire() is False
        time.sleep(1.2)
        # Expiration écoulée : b reprend le bail, la génération avance.
        assert b.acquire() is True
        assert b.generation == 2
        # Fencing : a a perdu le bail sans l'avoir observé lui-même — il
        # doit être rejeté, jamais agir comme s'il le détenait encore.
        assert a.is_current() is False
        assert b.is_current() is True
    finally:
        engine.dispose()


def _stale_holder_rejection(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        name = "lease-pg-stale-holder"
        a = Lease(engine, name=name, holder="replica-a", ttl_seconds=1)
        assert a.acquire() is True
        time.sleep(1.2)
        b = Lease(engine, name=name, holder="replica-b", ttl_seconds=30)
        assert b.acquire() is True
        # a n'a jamais rappelé acquire() ni observé l'échec : il tient
        # toujours son ancien LeaseHandle en mémoire, mais is_current()
        # doit refuser toute action au nom de la ressource protégée.
        assert a.is_current() is False
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_round_trip_on_postgres(postgres_dsn) -> None:
    _round_trip(postgres_dsn)


@pytest.mark.postgres
def test_expiry_and_takeover_on_postgres(postgres_dsn) -> None:
    _expiry_and_takeover(postgres_dsn)


@pytest.mark.postgres
def test_stale_holder_rejection_on_postgres(postgres_dsn) -> None:
    _stale_holder_rejection(postgres_dsn)


@pytest.mark.postgres
def test_two_concurrent_holders_on_postgres_only_one_acquires(postgres_dsn) -> None:
    v2_db.run_migrations(postgres_dsn)
    engine = v2_db.create_engine_for(postgres_dsn)
    try:
        name = "lease-pg-concurrent"
        results: list[bool] = []
        lock = threading.Lock()

        def _try_acquire(holder: str) -> None:
            lease = Lease(engine, name=name, holder=holder, ttl_seconds=30)
            outcome = lease.acquire()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=_try_acquire, args=(f"replica-{i}",)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert results.count(True) == 1
        assert results.count(False) == 4
    finally:
        engine.dispose()
