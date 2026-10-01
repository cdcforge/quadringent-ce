"""Périmètre Snowflake déclaré ; anciennes destinations conservées sans élargissement."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_destination_scope"
down_revision = "0015_one_time_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("destinations", sa.Column("destination_database", sa.String(63), nullable=False, server_default="QUADRINGENT"))
    op.add_column("destinations", sa.Column("destination_schema", sa.String(63), nullable=True))


def downgrade() -> None:
    # Une cible isolée ne doit jamais redevenir implicitement RAW/CURATED.
    raise RuntimeError("Le périmètre de destination déclaré doit être conservé")
