"""Environnement Alembic du control plane v2.

Les migrations sont écrites à la main (pas d'``autogenerate``) : ce module
se contente d'appliquer la révision demandée sur l'URL fournie par
``quadringent_control_plane.v2.db.alembic_config``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

# Pas de métadonnées cibles : les migrations sont écrites explicitement
# (voir ``versions/``), le schéma de référence pour les tests reste
# ``quadringent_control_plane.v2.schema``.
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
