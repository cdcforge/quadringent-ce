"""Pause/reprise au niveau source, destination et organisation (chantier MCP/CLI, §9.2 tâche 18).

Le contrat de la tâche 18 demande des outils MCP ``pause_source``,
``resume_source``, ``pause_destination``, ``resume_destination``,
``pause_all``, ``resume_all`` — mais ni le modèle de tâche 5/6
(``pipelines.declared_state``) ni ``sources``/``destinations`` ne portaient
de notion de pause à ce niveau (seul le pipeline a un état déclaré). Cette
migration ajoute une colonne ``paused_at`` (nullable — ``NULL`` = actif) sur
``sources`` et ``destinations`` : une pause à ce niveau est une intention
opérateur distincte de ``declared_state`` du pipeline (elle n'écrit pas les
pipelines eux-mêmes, cf. ``services/sources.py``/``services/destinations.py``
tâche 18) et ``organizations`` pour ``pause_all``/``resume_all`` (portée
organisation entière, cf. contrat §5 « pause_all/resume_all »).

Revision ID: 0006_source_destination_pause
Revises: 0005_pipeline_last_observed
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_source_destination_pause"
down_revision = "0005_pipeline_last_observed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("destinations", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "paused_at")
    op.drop_column("destinations", "paused_at")
    op.drop_column("sources", "paused_at")
