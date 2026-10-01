"""PEM épinglé d'une source (chaîne de confiance TLS, chantier 2026-09-24).

Quand une source parle à une autorité privée/inconnue, la sonde mesure le
certificat présenté (jamais utilisé pour authentifier) et le rend à
l'opérateur (empreinte + PEM). Si l'opérateur épingle cette empreinte
(``tls: {trust: "pinned", fingerprint}``), le control plane doit pouvoir
reproduire cette confiance pour toutes les charges qui parlent ensuite à
cet IBM i (sonde, découverte, lecteur, copie initiale, rejeu) — il lui faut
donc conserver le PEM correspondant, pas seulement l'empreinte déjà
présente (``tls_fingerprint``, 0001_initial_schema).

Stockage en clair (pas de chiffrement Fernet, contrairement à
``secret_ciphertext``) : un certificat public — feuille ou racine d'une
chaîne — n'est par construction jamais un secret (il est présenté en clair
à quiconque ouvre une connexion TLS vers cet hôte) ; le chiffrer
n'ajouterait aucune confidentialité réelle, seulement une étape de plus
avant de le monter en CA pour les Jobs/Deployments.

Revision ID: 0013_source_tls_pin
Revises: 0012_table_columns
Create Date: 2026-09-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013_source_tls_pin"
down_revision = "0012_table_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("tls_trust", sa.String(16), nullable=True))
    op.add_column("sources", sa.Column("tls_pinned_pem", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "tls_pinned_pem")
    op.drop_column("sources", "tls_trust")
