"""Identité de service Snowflake persistée par destination (chargeur de
destination, chantier « historique + miroir Snowpipe Streaming »).

``destinations.py::_build_setup_script`` générait déjà un rôle
(``QDT_ROLE_<hex>``) et un utilisateur de service (``QDT_SVC_<hex>``)
aléatoires par destination, mais ne les persistait qu'à l'intérieur du texte
du script SQL — aucune colonne interrogeable. Le chargeur de destination (un
consommateur Kubernetes distinct de l'API v2) doit pouvoir reconstruire une
connexion Snowflake (``snowflake-connector-python`` pour le MERGE miroir, le
profil Snowpipe Streaming pour l'historique) à partir du seul Secret
provisionné — qui n'exposait jusqu'ici que compte + clé privée
(``secrets_provisioner.py``), sans utilisateur ni rôle. ``service_user``/
``service_role`` comblent ce manque, remplis à la création (voir
``services/destinations.py``) ; ``NULL`` pour toute destination créée avant
cette migration — refusée explicitement par le provisionneur de Secret
plutôt que de deviner un nom.

Revision ID: 0011_dest_service_identity
Revises: 0010_unify_leases
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_dest_service_identity"
down_revision = "0010_unify_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("destinations", sa.Column("service_user", sa.String(32), nullable=True))
    op.add_column("destinations", sa.Column("service_role", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("destinations", "service_role")
    op.drop_column("destinations", "service_user")
