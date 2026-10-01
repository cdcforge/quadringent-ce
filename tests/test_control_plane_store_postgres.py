"""Tâche 1 — schéma Postgres et connexion.

Les migrations Alembic s'appliquent proprement sur une base éphémère, et un
aller-retour (écriture puis lecture) sur ``sources``/``destinations``/
``tables``/``pipelines`` restitue exactement ce qui a été écrit. Les mêmes
scénarios tournent sur SQLite (rapide, toujours actif) et sur un vrai
Postgres 16 (marqués ``@pytest.mark.postgres``, fixture ``postgres_dsn``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from quadringent_control_plane.v2 import db as v2_db
from quadringent_control_plane.v2 import schema as v2_schema


def _sqlite_dsn(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'control-plane-v2.sqlite3'}"


def _round_trip(dsn: str) -> None:
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    try:
        with engine.begin() as connection:
            connection.execute(
                insert(v2_schema.organizations),
                {"id": "org1", "name": "Client unique"},
            )
            connection.execute(
                insert(v2_schema.sources),
                {
                    "id": "src1",
                    "org_id": "org1",
                    "display_name": "Site principal",
                    "ibmi_host": "as400.example.com",
                    "ibmi_user": "QSVCUSER",
                    "secret_ciphertext": "gAAAAA==chiffre",
                    "tls_fingerprint": None,
                    "detected_timezone": None,
                    "detected_version": None,
                },
            )
            connection.execute(
                insert(v2_schema.destinations),
                {
                    "id": "dst1",
                    "org_id": "org1",
                    "snowflake_account": "acme-sf",
                    "key_pair_ciphertext": "gAAAAA==chiffre-cle",
                    "setup_script": "-- setup.sql",
                    "verification_state": "declared_not_verified",
                },
            )
            connection.execute(
                insert(v2_schema.tables),
                {
                    "id": "tbl1",
                    "source_id": "src1",
                    "schema_name": "PAYSLIB",
                    "table_name": "ORDERS",
                    "journal_status": "unknown",
                    "key_strategy": "unique_index",
                },
            )
            connection.execute(
                insert(v2_schema.pipelines),
                {
                    "id": "ppl1",
                    "table_id": "tbl1",
                    "destination_id": "dst1",
                    "declared_state": "not_started",
                },
            )

        with engine.connect() as connection:
            source_row = connection.execute(
                select(v2_schema.sources).where(v2_schema.sources.c.id == "src1")
            ).mappings().one()
            destination_row = connection.execute(
                select(v2_schema.destinations).where(v2_schema.destinations.c.id == "dst1")
            ).mappings().one()
            table_row = connection.execute(
                select(v2_schema.tables).where(v2_schema.tables.c.id == "tbl1")
            ).mappings().one()
            pipeline_row = connection.execute(
                select(v2_schema.pipelines).where(v2_schema.pipelines.c.id == "ppl1")
            ).mappings().one()
    finally:
        engine.dispose()

    assert source_row["ibmi_host"] == "as400.example.com"
    assert source_row["secret_ciphertext"] == "gAAAAA==chiffre"
    assert destination_row["snowflake_account"] == "acme-sf"
    assert table_row["schema_name"] == "PAYSLIB"
    assert table_row["table_name"] == "ORDERS"
    assert pipeline_row["table_id"] == "tbl1"
    assert pipeline_row["destination_id"] == "dst1"
    assert pipeline_row["declared_state"] == "not_started"


def test_migrations_apply_cleanly_and_round_trip_on_sqlite(tmp_path) -> None:
    _round_trip(_sqlite_dsn(tmp_path))


def test_migrations_are_idempotent_on_sqlite(tmp_path) -> None:
    dsn = _sqlite_dsn(tmp_path)
    v2_db.run_migrations(dsn)
    # Rejouer les migrations sur une base déjà à jour ne doit ni échouer ni
    # dupliquer le schéma (Alembic s'arrête à ``head`` sans effet).
    v2_db.run_migrations(dsn)


@pytest.mark.postgres
def test_migrations_apply_cleanly_and_round_trip_on_postgres(postgres_dsn) -> None:
    _round_trip(postgres_dsn)


@pytest.mark.postgres
def test_migrations_are_idempotent_on_postgres(postgres_dsn) -> None:
    v2_db.run_migrations(postgres_dsn)
    v2_db.run_migrations(postgres_dsn)
