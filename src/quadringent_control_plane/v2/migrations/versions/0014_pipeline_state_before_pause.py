"""Reprise après pause revient à l'état d'avant pause (chantier réconciliation, 24/09/2026).

Constat du premier pipeline réel sur GKE : ``POST /v2/pipelines/{id}/actions/
resume`` sur un pipeline qui était ``live`` avant la pause le faisait
repasser en ``copying`` — ``_event_for_action`` traduisait ``resume`` en
``resume_copying`` sans jamais regarder l'état d'avant pause, alors que la
machine à états (``state_machine.py``) distingue déjà ``resume_live`` de
``resume_copying``. Une nouvelle copie complète doit rester une action
explicite (``restart_initial_copy``), jamais un effet de bord de la reprise.

``pipelines.state_before_pause`` mémorise ``declared_state`` au moment de la
transition ``pause`` (``copying`` ou ``live``) ; ``resume`` le relit pour
choisir le bon événement puis l'efface — ``NULL`` pour tout pipeline jamais
pausé, ou pausé avant ce correctif (repli conservateur vers ``resume_
copying``, comportement inchangé pour ces lignes historiques).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014_pipeline_state_before_pause"
down_revision = "0013_source_tls_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("state_before_pause", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("pipelines", "state_before_pause")
