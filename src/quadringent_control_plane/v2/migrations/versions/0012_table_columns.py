"""Colonnes métier déclarées par table (chargeur de destination).

Aucune découverte de colonne n'existe encore côté worker IBM i
(``table_discovery.py`` ne rapporte que des métadonnées de table : clé,
journalisation, images). ``discovered_columns`` porte une liste déclarée
explicitement (nom, type IBM i, longueur/précision/échelle, nullabilité,
CCSID) — voir ``TablesService.set_discovered_columns``. ``NULL`` tant
qu'elle n'a pas été déclarée : le chargeur de destination refuse alors de
créer les tables Snowflake plutôt que de deviner un schéma.

Revision ID: 0012_table_columns
Revises: 0011_dest_service_identity
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012_table_columns"
down_revision = "0011_dest_service_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tables", sa.Column("discovered_columns", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tables", "discovered_columns")
