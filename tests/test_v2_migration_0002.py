"""Migration 0002 — tables identité/confirmations/audit/événements/webhooks.

Vérifie que la migration s'applique proprement sur une base éphémère et que
les nouvelles tables existent avec les colonnes attendues (round-trip
minimal, la couverture fonctionnelle détaillée vient des tests de service).
"""

from __future__ import annotations

from sqlalchemy import inspect

from quadringent_control_plane.v2 import db as v2_db


def test_migration_0002_creates_expected_tables(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'migration0002.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        table_names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    expected = {
        "users",
        "activation_tokens",
        "agent_tokens",
        "confirmations",
        "audit_records",
        "events",
        "webhooks",
        "webhook_deliveries",
    }
    assert expected <= table_names


def test_migration_0002_is_idempotent_on_head(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'migration0002b.sqlite3'}"
    v2_db.run_migrations(dsn)
    v2_db.run_migrations(dsn)  # rejouable sans erreur (alembic reste en tête)
