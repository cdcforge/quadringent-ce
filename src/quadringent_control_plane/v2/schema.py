"""Schéma Postgres du control plane v2 (SQLAlchemy Core, pas d'ORM lourd).

Ce module décrit les tables nécessaires aux chantiers 3 (``organizations``,
``sources``, ``destinations``, ``tables``, ``pipelines``,
``idempotency_keys``) et 4 (colonnes de découverte de tables sur
``tables`` : ``readiness``, ``key_columns``, ``journal_library``,
``journal_name``, ``images``, ``cl_fix_commands`` — voir
``migrations/versions/0004_tables_discovery.py``). Les migrations Alembic
sous ``v2/migrations`` sont la source de vérité appliquée en base ; ce
module sert de référence commune pour écrire ces migrations et pour les
tests unitaires (SQLite éphémère).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()


def new_id() -> str:
    """Identifiant opaque, hexadécimal, cohérent avec ``connection_id`` v1."""

    return uuid.uuid4().hex


organizations = Table(
    "organizations",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # 0006_source_destination_pause : portée de ``pause_all``/``resume_all``.
    Column("paused_at", DateTime(timezone=True), nullable=True),
)

sources = Table(
    "sources",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("display_name", String(200), nullable=False),
    Column("ibmi_host", String(253), nullable=False),
    Column("ibmi_user", String(30), nullable=False),
    # Chiffré (Fernet) avant toute écriture — jamais de mot de passe en clair.
    Column("secret_ciphertext", Text(), nullable=False),
    Column("tls_fingerprint", String(200), nullable=True),
    Column("detected_timezone", String(64), nullable=True),
    Column("detected_version", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # 0006_source_destination_pause : NULL = actif, sinon horodatage de pause.
    Column("paused_at", DateTime(timezone=True), nullable=True),
    # 0013_source_tls_pin : décision de confiance TLS retenue pour cette
    # source ("system" | "pinned" | "unknown" | NULL tant que jamais
    # testée) et, si épinglée, le PEM du certificat correspondant — jamais
    # chiffré, un certificat public n'est pas un secret (voir la migration).
    Column("tls_trust", String(16), nullable=True),
    Column("tls_pinned_pem", Text(), nullable=True),
)

destinations = Table(
    "destinations",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("snowflake_account", String(63), nullable=False),
    # 0016_destination_scope : NULL conserve les schémas historiques RAW/CURATED.
    Column("destination_database", String(63), nullable=False, server_default="QUADRINGENT"),
    Column("destination_schema", String(63), nullable=True),
    # Clé privée RSA générée côté serveur, chiffrée (Fernet) avant stockage.
    Column("key_pair_ciphertext", Text(), nullable=False),
    Column("setup_script", Text(), nullable=False),
    Column("verification_state", String(32), nullable=False, server_default="declared_not_verified"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # 0006_source_destination_pause : NULL = actif, sinon horodatage de pause.
    Column("paused_at", DateTime(timezone=True), nullable=True),
    # 0011_destination_service_identity : rôle/utilisateur de service générés
    # à la création (déjà dans setup_script en texte), persistés ici pour que
    # le chargeur de destination puisse reconstruire une connexion Snowflake
    # depuis le Secret provisionné. NULL pour une destination créée avant
    # cette migration.
    Column("service_user", String(32), nullable=True),
    Column("service_role", String(32), nullable=True),
)

tables = Table(
    "tables",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("source_id", String(32), ForeignKey("sources.id"), nullable=False),
    Column("schema_name", String(30), nullable=False),
    Column("table_name", String(30), nullable=False),
    Column("journal_status", String(32), nullable=False, server_default="unknown"),
    Column("key_strategy", String(32), nullable=False, server_default="unique_index"),
    Column("discovered_row_count", BigInteger(), nullable=True),
    Column("discovered_size_bytes", BigInteger(), nullable=True),
    Column("discovered_at", DateTime(timezone=True), nullable=True),
    # Ajoutés par la tâche 4 (découverte de tables) — 0004_tables_discovery.
    Column("readiness", String(32), nullable=False, server_default="not_journaled"),
    Column("key_columns", Text(), nullable=True),
    Column("journal_library", String(30), nullable=True),
    Column("journal_name", String(30), nullable=True),
    Column("images", String(16), nullable=True),
    Column("cl_fix_commands", JSON(), nullable=True),
    # 0012_table_discovered_columns : colonnes métier déclarées (nom, type
    # IBM i, longueur/précision/échelle, nullabilité, CCSID) — aucune
    # découverte automatique de type de colonne n'existe encore (le worker
    # ``discover`` ne rapporte que des métadonnées de table, jamais de
    # colonne : voir table_discovery.py). Rempli par déclaration explicite
    # (``TablesService.set_discovered_columns``), jamais deviné ; le
    # chargeur de destination refuse de créer les tables Snowflake tant que
    # ce champ est NULL pour une table donnée.
    Column("discovered_columns", JSON(), nullable=True),
    UniqueConstraint("source_id", "schema_name", "table_name", name="uq_tables_source_schema_table"),
)

pipelines = Table(
    "pipelines",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("table_id", String(32), ForeignKey("tables.id"), nullable=False, unique=True),
    Column("destination_id", String(32), ForeignKey("destinations.id"), nullable=False),
    Column("declared_state", String(32), nullable=False, server_default="not_started"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Ajoutés par 0005_pipeline_last_observed (chantier « observabilité v2 ») :
    # dernier état *observé* connu — jamais l'état déclaré — mémorisé pour
    # que le rafraîchissement d'observation ne publie un événement SSE
    # ``pipeline.state_changed`` que sur un vrai changement.
    Column("last_observed_state", String(32), nullable=True),
    Column("last_observed_at", DateTime(timezone=True), nullable=True),
    # Chantier 4, tâche 2 (0007_reconciliation) — voir la migration pour le détail.
    Column("active_run_id", String(64), nullable=True),
    Column("attention_reason", Text(), nullable=True),
    # Chantier 4, complément MCP/CLI (0008_pipeline_scope_pause_marker) :
    # vrai si ce pipeline a été pausé par une action de portée (source/
    # destination/organisation), pas individuellement — seule cette pause
    # est relancée automatiquement par la reprise de portée correspondante.
    Column("paused_by_scope_action", Boolean(), nullable=False, default=False),
    # 0014_pipeline_state_before_pause : ``declared_state`` mémorisé au moment
    # de la transition ``pause`` (``copying`` ou ``live``) — relu par
    # ``resume`` pour reprendre au bon état plutôt que de toujours relancer
    # une copie initiale. ``NULL`` pour un pipeline jamais pausé, ou pausé
    # avant ce correctif.
    Column("state_before_pause", String(32), nullable=True),
)

# Bail (lease) applicatif unifié — table unique remplaçant depuis la
# migration 0010_unify_leases les deux tables jumelles introduites en
# parallèle par les chantiers réconciliation (``reconciler_leases``,
# 0007) et ordonnancement (``scheduler_locks``, 0009) : même mécanisme, une
# seule table, un seul module (``services/lease.py``). ``generation`` est le
# jeton de fencing — il avance à chaque nouvelle attribution (jamais sur un
# simple renouvellement par le même titulaire), ce qui permet à un appelant
# de détecter qu'il a perdu le bail même s'il n'a pas encore observé
# l'expiration (ex. long GC pause).
leases = Table(
    "leases",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("holder", String(128), nullable=False),
    Column("generation", Integer(), nullable=False, default=1),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    # Vide uniquement pour les anciennes lignes sans attribution certaine.
    # Ne pas les rejouer ni les considérer comme une requête inédite.
    Column("org_id", String(32), primary_key=True, server_default=""),
    Column("key", String(128), primary_key=True),
    Column("actor_id", String(200), nullable=False),
    Column("method", String(10), nullable=False),
    Column("path", String(300), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status_code", Integer(), nullable=False),
    Column("response", JSON(), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# --- Identité, confirmations, audit, SSE, webhooks (tâches 7, 8, 9, 11, 12, 13) --

users = Table(
    "users",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("email", String(320), nullable=False, unique=True),
    Column("role", String(16), nullable=False),  # admin|reader
    # Scrypt (stdlib hashlib) — jamais le mot de passe en clair.
    Column("password_hash", Text(), nullable=True),
    Column("oidc_subject", String(200), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("activated_at", DateTime(timezone=True), nullable=True),
)

activation_tokens = Table(
    "activation_tokens",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(32), ForeignKey("users.id"), nullable=False),
    Column("hash", String(128), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

agent_tokens = Table(
    "agent_tokens",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("name", String(200), nullable=False),
    Column("scope", String(16), nullable=False),  # read|operate|admin
    Column("prefix", String(8), nullable=False),  # qdt_rd|qdt_op|qdt_ad — utile au scan de fuite
    # sha256(pepper || jeton) — jamais le jeton en clair (voir crypto.py::hash_agent_token).
    Column("hash", String(64), nullable=False, unique=True),
    Column("source_restriction", JSON(), nullable=False, default=list),
    Column("pre_authorized_actions", JSON(), nullable=False, default=list),
    Column("created_by", String(200), nullable=False),
    Column("never_expires", Boolean(), nullable=False, default=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

confirmations = Table(
    "confirmations",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("action_ref", String(64), nullable=False),  # ex. "pipeline.restart_initial_copy"
    Column("resource_type", String(32), nullable=False),
    Column("resource_id", String(32), nullable=False),
    Column("reason", Text(), nullable=False),
    Column("risk_estimate", JSON(), nullable=True),
    Column("requested_by_kind", String(8), nullable=False),  # human|agent
    Column("requested_by_id", String(200), nullable=False),
    Column("approval_token_hash", String(64), nullable=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String(16), nullable=False, default="pending"),  # pending|approved|rejected|expired
    Column("approved_by_kind", String(8), nullable=True),
    Column("approved_by_id", String(200), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

audit_records = Table(
    "audit_records",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("actor_kind", String(8), nullable=False),  # human|agent
    Column("actor_id", String(200), nullable=False),
    Column("actor_display", String(200), nullable=False),
    Column("mcp_client", String(200), nullable=True),
    Column("action", String(64), nullable=False),
    Column("resource_type", String(32), nullable=False),
    Column("resource_id", String(32), nullable=True),
    Column("request_id", String(64), nullable=True),
    Column("idempotency_key", String(128), nullable=True),
    Column("dry_run", Boolean(), nullable=False, default=False),
    Column("confirmation_id", String(32), nullable=True),
    Column("status", String(16), nullable=False),  # succeeded|failed|pending_confirmation
    Column("before", JSON(), nullable=True),
    Column("after", JSON(), nullable=True),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer(), primary_key=True, autoincrement=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSON(), nullable=False),
)

webhooks = Table(
    "webhooks",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("org_id", String(32), ForeignKey("organizations.id"), nullable=False),
    Column("url", String(2048), nullable=False),
    # Chiffré (Fernet, même SecretBox que les sources/destinations) — le
    # secret en clair n'est affiché qu'à la création, mais il doit rester
    # déchiffrable pour signer chaque livraison (HMAC-sha256), contrairement
    # aux jetons d'agent/mots de passe qui ne sont jamais qu'un hash.
    Column("secret_ciphertext", Text(), nullable=False),
    Column("events", JSON(), nullable=False),
    Column("state", String(16), nullable=False, default="active"),  # active|disabled
    Column("consecutive_failures", Integer(), nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

webhook_deliveries = Table(
    "webhook_deliveries",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("webhook_id", String(32), ForeignKey("webhooks.id"), nullable=False),
    Column("event_id", String(64), nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSON(), nullable=False),
    Column("status", String(16), nullable=False, default="pending"),  # pending|delivered|failed|disabled
    Column("attempt_count", Integer(), nullable=False, default=0),
    Column("next_attempt_at", DateTime(timezone=True), nullable=True),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("webhook_id", "event_id", name="uq_webhook_deliveries_webhook_event"),
)
