"""Unification des deux mécanismes de bail multi-réplicas.

Deux tables jumelles avaient été introduites indépendamment par deux
chantiers menés en parallèle : ``reconciler_leases`` (0007_reconciliation,
``services/reconciler.py``) pour la boucle de réconciliation, et
``scheduler_locks`` (0009_scheduler_locks, ``services/scheduler_lock.py``)
pour l'ordonnanceur de rafraîchissement d'observation. Même mécanisme
(bail portable SQLite/Postgres, ``holder``/``expires_at``, mise à jour SQL
conditionnelle), dupliqué à deux endroits.

Cette migration les remplace par une table unique ``leases`` (voir
``services/lease.py``, source de vérité désormais partagée par les deux
boucles), avec un ajout : ``generation``, un compteur de fencing qui avance
à chaque nouvelle attribution du bail (jamais sur un simple renouvellement
par le même titulaire). Un titulaire qui a perdu le bail sans encore avoir
observé son expiration (ex. long GC pause, thread suspendu) peut ainsi
détecter qu'il n'est plus à jour avant d'agir, au lieu de se fier
uniquement à l'horloge.

Les deux tables jumelles ne portent que des baux transitoires (aucune
donnée métier durable — le pire cas d'une perte de ligne est qu'un bail
soit ré-acquis immédiatement par le prochain appelant) : la migration ne
copie donc aucune ligne, elle crée la table unique et supprime les deux
anciennes.

Revision ID: 0010_unify_leases
Revises: 0009_scheduler_locks
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_unify_leases"
down_revision = "0009_scheduler_locks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leases",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_table("reconciler_leases")
    op.drop_table("scheduler_locks")


def downgrade() -> None:
    op.create_table(
        "reconciler_leases",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "scheduler_locks",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("holder", sa.String(64), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_table("leases")
