"""Schéma initial control plane v2.

Crée ``organizations``, ``sources``, ``destinations``, ``tables``,
``pipelines`` et ``idempotency_keys`` — le périmètre strict des tâches 1, 2,
3, 5 et 6 du contrat (``docs/plans/2026-09-23-control-plane-v2-contract.md``,
§7.1 et §9.2). Les autres tables du schéma cible (§7.1 : users, agent_tokens,
confirmations, audit_records, webhooks, alerts, costs_snapshots…) seront
ajoutées par des migrations ultérieures, hors périmètre de ce chantier.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("ibmi_host", sa.String(253), nullable=False),
        sa.Column("ibmi_user", sa.String(30), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("tls_fingerprint", sa.String(200), nullable=True),
        sa.Column("detected_timezone", sa.String(64), nullable=True),
        sa.Column("detected_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "destinations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("snowflake_account", sa.String(63), nullable=False),
        sa.Column("key_pair_ciphertext", sa.Text(), nullable=False),
        sa.Column("setup_script", sa.Text(), nullable=False),
        sa.Column(
            "verification_state",
            sa.String(32),
            nullable=False,
            server_default="declared_not_verified",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "tables",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("source_id", sa.String(32), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("schema_name", sa.String(30), nullable=False),
        sa.Column("table_name", sa.String(30), nullable=False),
        sa.Column("journal_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("key_strategy", sa.String(32), nullable=False, server_default="unique_index"),
        sa.Column("discovered_row_count", sa.BigInteger(), nullable=True),
        sa.Column("discovered_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "source_id", "schema_name", "table_name", name="uq_tables_source_schema_table"
        ),
    )
    op.create_table(
        "pipelines",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("table_id", sa.String(32), sa.ForeignKey("tables.id"), nullable=False, unique=True),
        sa.Column("destination_id", sa.String(32), sa.ForeignKey("destinations.id"), nullable=False),
        sa.Column("declared_state", sa.String(32), nullable=False, server_default="not_started"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pipelines_declared_state", "pipelines", ["declared_state"])
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_index("ix_pipelines_declared_state", table_name="pipelines")
    op.drop_table("pipelines")
    op.drop_table("tables")
    op.drop_table("destinations")
    op.drop_table("sources")
    op.drop_table("organizations")
