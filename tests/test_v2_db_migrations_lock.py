"""``run_migrations_locked`` : une seule réplique migre à la fois.

Le control plane v2 tourne potentiellement à plusieurs répliques pendant un
rolling update (le temps que l'ancien pod se termine). Sans verrou, deux
processus lançant ``alembic upgrade head`` en même temps sur le même Postgres
peuvent se marcher dessus (DDL concurrent). Ce module vérifie :

- sur SQLite (pas de verrou distribué possible, une seule réplique de test à
  la fois de toute façon) : comportement inchangé, délègue à
  ``run_migrations``.
- sur un vrai Postgres (``@pytest.mark.postgres``) : le verrou advisory est
  bien tenu pendant la migration (une deuxième tentative concurrente de
  verrouillage sur la même clé est bloquée tant que la première ne l'a pas
  relâché), et les migrations s'appliquent malgré tout une fois le tour
  précédent terminé.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from quadringent_control_plane.v2 import db as v2_db


def test_sqlite_delegates_to_plain_migrations_without_locking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dsn = f"sqlite:///{Path(tmp) / 'v2.sqlite3'}"
        v2_db.run_migrations_locked(dsn)
        engine = v2_db.create_engine_for(dsn)
        try:
            with engine.connect() as connection:
                # La migration 0001 crée bien la table organizations.
                connection.execute(text("SELECT count(*) FROM organizations"))
        finally:
            engine.dispose()


@pytest.mark.postgres
def test_postgres_advisory_lock_serializes_concurrent_migration_attempts(postgres_dsn: str) -> None:
    engine = v2_db.create_engine_for(postgres_dsn)
    try:
        holder = engine.connect()
        holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": v2_db.MIGRATION_LOCK_KEY})
        holder.commit()
        try:
            second_acquired = threading.Event()
            second_thread_done = threading.Event()

            def _try_migrate() -> None:
                v2_db.run_migrations_locked(postgres_dsn)
                second_acquired.set()
                second_thread_done.set()

            thread = threading.Thread(target=_try_migrate, daemon=True)
            thread.start()
            # Le verrou est tenu par `holder` : la migration concurrente ne
            # doit pas se terminer tant qu'on ne l'a pas relâché.
            assert not second_acquired.wait(timeout=1.0)
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": v2_db.MIGRATION_LOCK_KEY})
            holder.commit()
            holder.close()

        # Une fois relâché, la migration concurrente doit se terminer.
        assert second_thread_done.wait(timeout=15.0)

        with engine.connect() as connection:
            connection.execute(text("SELECT count(*) FROM organizations"))
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_postgres_migration_lock_is_released_after_success(postgres_dsn: str) -> None:
    v2_db.run_migrations_locked(postgres_dsn)
    engine = v2_db.create_engine_for(postgres_dsn)
    try:
        with engine.connect() as connection:
            # pg_try_advisory_lock réussit : le verrou n'est plus tenu.
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": v2_db.MIGRATION_LOCK_KEY}
            ).scalar()
            assert acquired is True
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": v2_db.MIGRATION_LOCK_KEY})
    finally:
        engine.dispose()
