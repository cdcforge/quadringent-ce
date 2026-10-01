"""Identité, confirmations, audit, SSE persisté, webhooks (tâches 7, 8, 9, 11, 12, 13).

Ajoute ``users``, ``activation_tokens``, ``agent_tokens``, ``confirmations``,
``audit_records``, ``events`` (flux SSE persisté pour ``Last-Event-ID``),
``webhooks`` et ``webhook_deliveries`` — voir
``docs/plans/2026-09-23-control-plane-v2-contract.md`` §7.1 et §9.2.

Revision ID: 0002_identity_conf_audit
Revises: 0001_initial_schema
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_identity_conf_audit"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("oidc_subject", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "activation_tokens",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "agent_tokens",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("prefix", sa.String(8), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
        sa.Column("source_restriction", sa.JSON(), nullable=False),
        sa.Column("pre_authorized_actions", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("never_expires", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "confirmations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("action_ref", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("risk_estimate", sa.JSON(), nullable=True),
        sa.Column("requested_by_kind", sa.String(8), nullable=False),
        sa.Column("requested_by_id", sa.String(200), nullable=False),
        sa.Column("approval_token_hash", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("approved_by_kind", sa.String(8), nullable=True),
        sa.Column("approved_by_id", sa.String(200), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_confirmations_state", "confirmations", ["state"])
    op.create_table(
        "audit_records",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("actor_kind", sa.String(8), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("actor_display", sa.String(200), nullable=False),
        sa.Column("mcp_client", sa.String(200), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(32), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("confirmation_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_records_actor_kind_at", "audit_records", ["actor_kind", "at"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "webhooks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("org_id", sa.String(32), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("webhook_id", sa.String(32), sa.ForeignKey("webhooks.id"), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("webhook_id", "event_id", name="uq_webhook_deliveries_webhook_event"),
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
    op.drop_table("events")
    op.drop_index("ix_audit_records_actor_kind_at", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_index("ix_confirmations_state", table_name="confirmations")
    op.drop_table("confirmations")
    op.drop_table("agent_tokens")
    op.drop_table("activation_tokens")
    op.drop_table("users")
