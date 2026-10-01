"""Boucle de réconciliation en arrière-plan (chantier 4, tâche 2).

``pipelines.active_run_id`` : identifiant de run (UUID) de la copie
initiale en cours, posé par l'exécuteur au moment de ``start``/
``restart_initial_copy`` — c'est la clé que la boucle de fond utilise pour
retrouver la preuve de fin de copie (``evidence.py``) et le statut du Job
sans avoir à énumérer les objets Kubernetes. ``pipelines.attention_reason`` :
texte explicatif posé quand la boucle bascule un pipeline en ``attention``
(Job en échec, régression de bascule) — jamais une valeur inventée, ``NULL``
tant qu'aucune détection automatique n'a eu lieu.

``reconciler_leases`` : bail (lease) portable Postgres/SQLite pour garantir
qu'un seul réplica du control plane exécute la boucle à la fois — une ligne
par nom de boucle, ``holder`` et ``expires_at`` mis à jour par
``services/reconciler.py::acquire_lease``. Choix documenté : pas de
``pg_advisory_lock`` (spécifique Postgres, incompatible avec les tests
SQLite de ce dépôt) — un bail applicatif à courte durée, revalidé à chaque
tour, est suffisant pour une boucle qui tolère un tour manqué.

Revision ID: 0007_reconciliation
Revises: 0006_source_destination_pause
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0007_reconciliation"
down_revision = "0006_source_destination_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("active_run_id", sa.String(64), nullable=True))
    op.add_column("pipelines", sa.Column("attention_reason", sa.Text(), nullable=True))
    op.create_table(
        "reconciler_leases",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("reconciler_leases")
    op.drop_column("pipelines", "attention_reason")
    op.drop_column("pipelines", "active_run_id")
