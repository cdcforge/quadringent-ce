"""Connexion et migrations du control plane v2.

Le magasin cible est Postgres (déployé par la chart, StatefulSet + volume) ;
les tests unitaires tournent sur SQLite éphémère (fichier temporaire, jamais
``:memory:`` partagé entre connexions) pour rester rapides et toujours
actifs. Les tests d'intégration (marqués ``@pytest.mark.postgres``) visent
un vrai Postgres 16 démarré via Docker le temps de la session pytest.
"""

from __future__ import annotations

import binascii
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Clé stable du verrou advisory Postgres des migrations v2, dérivée d'un nom
# fixe plutôt qu'un magic number (tient sur un bigint signé < 2**63, et ne
# ressemble pas accidentellement à un identifiant de compte AWS à douze
# chiffres). Dédiée à cet usage : aucun autre composant ne doit prendre un
# verrou advisory sur cette même clé.
MIGRATION_LOCK_KEY = binascii.crc32(b"quadringent-control-plane-v2-migrations")


class StoreUnavailableError(RuntimeError):
    """Le magasin Postgres est indisponible (code d'erreur ``store_unavailable``)."""


def create_engine_for(dsn: str) -> Engine:
    """Crée un moteur SQLAlchemy pour ``dsn`` (Postgres en cible, SQLite en test)."""

    connect_args: dict[str, object] = {}
    if dsn.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    return create_engine(dsn, connect_args=connect_args, future=True)


def alembic_config(dsn: str) -> Config:
    """Configuration Alembic pointant vers les migrations de ce paquet."""

    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def run_migrations(dsn: str) -> None:
    """Applique toutes les migrations versionnées sur ``dsn`` — idempotent."""

    command.upgrade(alembic_config(dsn), "head")


def run_migrations_locked(dsn: str) -> None:
    """Applique les migrations avec un verrou advisory Postgres.

    En production, plusieurs répliques du control plane v2 peuvent démarrer
    en même temps (rolling update, redémarrage simultané) : sans coordination,
    chacune lancerait ``alembic upgrade head`` sur le même Postgres, avec un
    risque de DDL concurrent. Ce verrou garantit qu'une seule réplique migre
    à la fois — les autres bloquent sur ``pg_advisory_lock`` jusqu'à ce que la
    première ait terminé et relâché la clé, puis migrent à leur tour (no-op :
    Alembic est idempotent une fois la tête atteinte).

    Sans objet sur SQLite (pas de verrou distribué, jamais plusieurs
    répliques de test concurrentes de toute façon) : délègue directement à
    :func:`run_migrations`.
    """

    if not dsn.startswith("postgresql"):
        run_migrations(dsn)
        return
    engine = create_engine_for(dsn)
    try:
        with engine.connect() as connection:
            # Verrou de session : bloque tant qu'une autre connexion tient la
            # même clé, plutôt qu'un verrou de transaction qui se relâcherait
            # au premier commit interne d'Alembic.
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
            connection.commit()
            try:
                run_migrations(dsn)
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
                connection.commit()
    finally:
        engine.dispose()
