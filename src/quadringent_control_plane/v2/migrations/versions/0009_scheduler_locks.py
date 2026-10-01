"""Verrou à bail pour l'ordonnanceur de rafraîchissement d'observation
(chantier « observabilité v2 », suite).

``scheduler_locks`` porte un bail (``holder``/``expires_at``) par nom de
tâche planifiée — voir ``services/scheduler_lock.py``. Portable SQLite/
Postgres (mise à jour conditionnelle en SQL simple, pas de
``pg_advisory_lock`` propriétaire) : un bail expiré est repris par le
prochain réplica qui essaie, sans jamais bloquer indéfiniment sur un
réplica mort.

Revision ID: 0009_scheduler_locks
Revises: 0008_pipeline_scope_pause_marker
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_scheduler_locks"
down_revision = "0008_pipeline_scope_pause_marker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_locks",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("holder", sa.String(64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("scheduler_locks")
