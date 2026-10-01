"""Pause/reprise de portée agit réellement sur les pipelines (chantier 4, complément MCP/CLI).

``0006_source_destination_pause`` n'a posé qu'un marqueur d'intention
(``sources.paused_at``/``destinations.paused_at``/``organizations.paused_at``)
sans jamais arrêter les flux — décision produit : ces pauses doivent
réellement pauser/reprendre chaque pipeline concerné via l'exécuteur.

``pipelines.paused_by_scope_action`` (booléen, défaut faux) distingue une
pause déclenchée par une action de portée (source/destination/organisation)
d'une pause individuelle (``POST /v2/pipelines/{id}/actions/pause``) : seule
la première est automatiquement relancée par la reprise de portée
correspondante — une table que l'utilisateur avait mise en pause
individuellement avant la pause de sa source reste en pause après la
reprise de la source.

Revision ID: 0008_pipeline_scope_pause_marker
Revises: 0007_reconciliation
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0008_pipeline_scope_pause_marker"
down_revision = "0007_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pipelines",
        sa.Column("paused_by_scope_action", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("pipelines", "paused_by_scope_action")
