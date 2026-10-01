"""Webhooks : secret chiffré (réversible) plutôt que haché (tâche 13).

``webhooks.secret_hash`` est remplacé par ``webhooks.secret_ciphertext`` :
le secret doit rester déchiffrable pour signer chaque livraison
(HMAC-sha256 sur ``timestamp + "." + body``), contrairement à un jeton
d'agent ou un mot de passe qui n'ont besoin que d'être vérifiés. Chiffré
avec ``SecretBox`` (Fernet), la même primitive que les secrets de source et
la clé privée de destination.

Revision ID: 0003_webhooks_secret_ciphertext
Revises: 0002_identity_conf_audit
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_webhooks_secret_ciphertext"
down_revision = "0002_identity_conf_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("webhooks") as batch_op:
        batch_op.add_column(sa.Column("secret_ciphertext", sa.Text(), nullable=True))
    op.execute("UPDATE webhooks SET secret_ciphertext = ''")
    with op.batch_alter_table("webhooks") as batch_op:
        batch_op.alter_column("secret_ciphertext", nullable=False)
        batch_op.drop_column("secret_hash")


def downgrade() -> None:
    with op.batch_alter_table("webhooks") as batch_op:
        batch_op.add_column(sa.Column("secret_hash", sa.String(64), nullable=True))
    op.execute("UPDATE webhooks SET secret_hash = ''")
    with op.batch_alter_table("webhooks") as batch_op:
        batch_op.alter_column("secret_hash", nullable=False)
        batch_op.drop_column("secret_ciphertext")
