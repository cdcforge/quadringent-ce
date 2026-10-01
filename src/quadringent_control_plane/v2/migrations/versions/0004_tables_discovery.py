"""Découverte de tables (tâche 4 du contrat, §2.3).

Étend ``tables`` avec les colonnes issues du catalogue IBM i renvoyé par
``discover`` (worker Java) : ``readiness`` (état déclaré par
``table_discovery.classify_table``), ``key_columns`` (CSV — clé primaire ou
index unique le cas échéant), ``journal_library``/``journal_name``/
``images`` (état de journalisation), ``cl_fix_commands`` (JSON — les
commandes CL correctives proposées à l'admin IBM i pour amener la table à
``ready``).

Numérotée ``0002`` à la suite de ``0001_initial_schema``. Si une autre
branche ajoute une migration ``0002`` concurrente, cette révision devra être
renumérotée (``0003``) et son ``down_revision`` réenchaîné au merge — pas de
renumérotation silencieuse d'une migration déjà appliquée.

Revision ID: 0004_tables_discovery
Revises: 0003_webhooks_secret_ciphertext
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_tables_discovery"
down_revision = "0003_webhooks_secret_ciphertext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tables",
        sa.Column("readiness", sa.String(32), nullable=False, server_default="not_journaled"),
    )
    op.add_column("tables", sa.Column("key_columns", sa.Text(), nullable=True))
    op.add_column("tables", sa.Column("journal_library", sa.String(30), nullable=True))
    op.add_column("tables", sa.Column("journal_name", sa.String(30), nullable=True))
    op.add_column("tables", sa.Column("images", sa.String(16), nullable=True))
    op.add_column("tables", sa.Column("cl_fix_commands", sa.JSON(), nullable=True))
    op.create_index("ix_tables_readiness", "tables", ["readiness"])


def downgrade() -> None:
    op.drop_index("ix_tables_readiness", table_name="tables")
    op.drop_column("tables", "cl_fix_commands")
    op.drop_column("tables", "images")
    op.drop_column("tables", "journal_name")
    op.drop_column("tables", "journal_library")
    op.drop_column("tables", "key_columns")
    op.drop_column("tables", "readiness")
