"""Expurge les secrets des réponses/audits et isole les clés par organisation.

Pas de rotation : seules les copies JSON sont modifiées. Les clés chiffrées,
hashes d'authentification et métadonnées publiques restent intacts. Une base
historique avec exactement une organisation permet un rattachement certain ;
sinon les anciennes clés restent sans attribution et leur rejeu est refusé.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from quadringent_control_plane.v2.redaction import redact_one_time_secrets

revision = "0015_one_time_secrets"
down_revision = "0014_pipeline_state_before_pause"
branch_labels = None
depends_on = None


def _scrub_json(bind, table_name, identifier, columns):
    table = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    cursor = None
    while True:
        statement = sa.select(table.c[identifier], *(table.c[name] for name in columns))
        if cursor is not None:
            statement = statement.where(table.c[identifier] > cursor)
        rows = bind.execute(statement.order_by(table.c[identifier]).limit(100)).mappings().all()
        if not rows:
            return
        for row in rows:
            sanitized = {name: redact_one_time_secrets(row[name]) for name in columns}
            if any(sanitized[name] != row[name] for name in columns):
                bind.execute(table.update().where(table.c[identifier] == row[identifier]).values(**sanitized))
        cursor = rows[-1][identifier]


def upgrade() -> None:
    bind = op.get_bind()
    _scrub_json(bind, "idempotency_keys", "key", ("response",))
    _scrub_json(bind, "audit_records", "id", ("before", "after"))
    op.add_column("idempotency_keys", sa.Column("org_id", sa.String(32), nullable=False, server_default=""))
    organizations = sa.table("organizations", sa.column("id", sa.String(32)))
    known = bind.execute(sa.select(organizations.c.id).limit(2)).scalars().all()
    if len(known) == 1:
        keys = sa.table("idempotency_keys", sa.column("org_id", sa.String(32)))
        bind.execute(keys.update().values(org_id=known[0]))

    # Le nom historique de la PK est explicite sur Postgres, absent sur
    # SQLite ; la convention donne un nom à celle-ci pendant la copie.
    old_pk = sa.inspect(bind).get_pk_constraint("idempotency_keys")["name"] or "pk_idempotency_keys"
    with op.batch_alter_table("idempotency_keys", naming_convention={"pk": "pk_%(table_name)s"}) as batch:
        batch.drop_constraint(old_pk, type_="primary")
        batch.create_primary_key("pk_idempotency_keys", ("org_id", "key"))


def downgrade() -> None:
    # Les mêmes clés peuvent maintenant appartenir à plusieurs sites. Ne
    # jamais fusionner leurs actions ni réintroduire le stockage de secrets.
    raise RuntimeError("Migration de sécurité irréversible ; conserver la version corrigée")
