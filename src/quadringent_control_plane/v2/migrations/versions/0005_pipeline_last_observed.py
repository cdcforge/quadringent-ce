"""Dernier état observé connu du pipeline (chantier « observabilité v2 »).

Ajoute ``pipelines.last_observed_state``/``last_observed_at`` : la mémoire
nécessaire à ``services.observation_refresh.ObservationRefreshService`` pour
détecter une transition de l'état *observé* (jamais l'état *déclaré*,
inchangé par ce chantier) et n'émettre ``pipeline.state_changed`` que sur un
vrai changement — jamais à chaque rafraîchissement, quelle que soit la
fréquence à laquelle un futur ordonnanceur appelle le rafraîchissement.

Revision ID: 0005_pipeline_last_observed
Revises: 0004_tables_discovery
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_pipeline_last_observed"
down_revision = "0004_tables_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("last_observed_state", sa.String(32), nullable=True))
    op.add_column("pipelines", sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pipelines", "last_observed_at")
    op.drop_column("pipelines", "last_observed_state")
