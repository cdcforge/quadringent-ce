"""Tâche 3 — modèle Destination v2.

La création génère une paire de clés RSA 2048 (PKCS8) et un script SQL
Snowflake (rôle dédié, utilisateur de service RSA_PUBLIC_KEY, warehouse XS
auto-suspend 60, base QUADRINGENT, schémas). La clé privée n'est jamais
persistée en clair, et aucun secret admin Snowflake n'est jamais accepté ni
transmis (le script est fait pour être exécuté manuellement par un admin
Snowflake, qui garde le contrôle de ses identifiants).
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.destinations import (
    DestinationNotFoundError,
    DestinationValidationError,
    DestinationsService,
)


@pytest.fixture()
def destinations_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'destinations.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    service = DestinationsService(engine, secret_box, org_id="org1")
    try:
        yield service
    finally:
        engine.dispose()


def test_create_generates_a_valid_rsa_2048_key_pair_shown_once(destinations_service) -> None:
    created, private_key_pem = destinations_service.create(snowflake_account="acme-sf")

    # La clé privée retournée une seule fois doit être PKCS8, RSA, 2048 bits.
    private_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    assert private_key.key_size == 2048
    assert "BEGIN PRIVATE KEY" in private_key_pem

    payload = created.to_dict()
    assert payload["snowflake_account"] == "acme-sf"
    assert "key_pair" not in payload
    assert "private_key" not in payload
    assert private_key_pem not in str(payload)


def test_create_never_persists_or_requires_a_snowflake_admin_secret(destinations_service) -> None:
    created, _private_key_pem = destinations_service.create(snowflake_account="acme-sf")
    fetched = destinations_service.get(created.id)
    assert "admin_password" not in fetched.to_dict()
    assert "admin_secret" not in fetched.to_dict()


def test_setup_script_contains_expected_snowflake_objects(destinations_service) -> None:
    created, _private_key_pem = destinations_service.create(snowflake_account="acme-sf")
    script = destinations_service.setup_script(created.id)
    assert "CREATE ROLE" in script
    assert "RSA_PUBLIC_KEY" in script
    assert "WAREHOUSE" in script and "XSMALL" in script
    assert "AUTO_SUSPEND = 60" in script
    assert "QUADRINGENT" in script
    # La clé privée ne fuit jamais dans le script téléchargeable.
    assert "PRIVATE KEY" not in script


def test_get_unknown_destination_raises_not_found(destinations_service) -> None:
    with pytest.raises(DestinationNotFoundError):
        destinations_service.get("does-not-exist")


def test_create_rejects_invalid_snowflake_account(destinations_service) -> None:
    with pytest.raises(DestinationValidationError):
        destinations_service.create(snowflake_account="compte invalide")


def test_list_orders_by_creation_then_id(destinations_service) -> None:
    first, _ = destinations_service.create(snowflake_account="acme-sf-1")
    second, _ = destinations_service.create(snowflake_account="acme-sf-2")
    listed = destinations_service.list()
    assert [record.id for record in listed] == [first.id, second.id]


def test_setup_script_defaults_to_copy_merge_without_streaming_grants(destinations_service) -> None:
    created, _ = destinations_service.create(snowflake_account="acme-sf")
    script = destinations_service.setup_script(created.id)
    assert "Snowpipe Streaming" not in script
    assert "EXECUTE TASK" not in script
    assert "GRANT USAGE ON SCHEMA QUADRINGENT.CURATED" in script
    assert "GRANT CREATE TABLE ON SCHEMA QUADRINGENT.CURATED" not in script


def test_setup_script_streaming_mode_grants_curated_create_table(destinations_service) -> None:
    created, _ = destinations_service.create(
        snowflake_account="acme-sf", destination_mode="streaming"
    )
    script = destinations_service.setup_script(created.id)
    assert "GRANT CREATE TABLE ON SCHEMA QUADRINGENT.CURATED TO ROLE" in script
    assert "EXECUTE TASK" not in script


def test_setup_script_task_driven_mirror_grants_execute_task(destinations_service) -> None:
    created, _ = destinations_service.create(
        snowflake_account="acme-sf", destination_mode="streaming", mirror_option="task_driven"
    )
    script = destinations_service.setup_script(created.id)
    assert "GRANT EXECUTE TASK ON ACCOUNT TO ROLE" in script


def test_task_driven_mirror_option_requires_streaming_mode(destinations_service) -> None:
    # mirror_option n'a de sens que pour destination_mode=streaming, mais un
    # appelant qui le déclare sans streaming ne doit pas se voir accorder
    # silencieusement EXECUTE TASK : le script reste celui de copy_merge.
    created, _ = destinations_service.create(
        snowflake_account="acme-sf", destination_mode="copy_merge", mirror_option="task_driven"
    )
    script = destinations_service.setup_script(created.id)
    assert "EXECUTE TASK" not in script


def test_create_rejects_unknown_destination_mode(destinations_service) -> None:
    with pytest.raises(DestinationValidationError):
        destinations_service.create(snowflake_account="acme-sf", destination_mode="dynamic_table")


def test_create_rejects_unknown_mirror_option(destinations_service) -> None:
    with pytest.raises(DestinationValidationError):
        destinations_service.create(
            snowflake_account="acme-sf", destination_mode="streaming", mirror_option="unknown"
        )


def test_create_persists_service_user_and_role(destinations_service) -> None:
    created, _ = destinations_service.create(snowflake_account="acme-sf")
    assert created.service_user is not None
    assert created.service_user.startswith("QDT_SVC_")
    assert created.service_role is not None
    assert created.service_role.startswith("QDT_ROLE_")
    # Le script SQL doit référencer exactement le même utilisateur/rôle.
    script = destinations_service.setup_script(created.id)
    assert created.service_user in script
    assert created.service_role in script


def test_setup_script_can_be_read_again_without_the_private_key(tmp_path) -> None:
    """Le script de mise en service ne contient qu'une clé publique : il doit
    rester relisible après la création (constaté : seule la réponse de
    création le portait, un script non sauvegardé était perdu)."""
    from fastapi.testclient import TestClient

    from quadringent_control_plane.v2.app import create_v2_app

    dsn = f"sqlite:///{tmp_path / 'setup_script.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()), token_pepper=b"pepper-test")
    try:
        client = TestClient(app)
        created = client.post("/v2/destinations", json={"snowflake_account": "acme-sf"},
                              headers={"Idempotency-Key": "dest-1"}).json()["after"]
        response = client.get(f"/v2/destinations/{created['id']}/setup-script")
        assert response.status_code == 200
        assert response.json()["setup_script"] == created["setup_script"]
        assert "PRIVATE KEY" not in response.text
        assert client.get("/v2/destinations/absente/setup-script").status_code == 404
    finally:
        engine.dispose()


def test_setup_script_gives_the_client_a_read_role_on_replicated_tables(destinations_service) -> None:
    """Constaté lors de la qualification : les tables répliquées appartiennent
    au rôle de service, même un administrateur ne pouvait pas les lire. Le
    script crée un rôle lecteur (tables actuelles et futures de CURATED), à
    accorder aux utilisateurs du client, sans aucun droit d'écriture."""
    created, _ = destinations_service.create(snowflake_account="acme-sf", destination_mode="streaming")
    script = destinations_service.setup_script(created.id)
    reader = created.service_role.replace("QDT_ROLE_", "QDT_READ_")
    assert f"CREATE ROLE IF NOT EXISTS {reader};" in script
    assert f"GRANT SELECT ON FUTURE TABLES IN SCHEMA QUADRINGENT.CURATED TO ROLE {reader};" in script
    assert f"GRANT SELECT ON ALL TABLES IN SCHEMA QUADRINGENT.CURATED TO ROLE {reader};" in script
    reader_lines = [line for line in script.splitlines() if reader in line]
    assert not any(word in line for line in reader_lines for word in ("INSERT", "CREATE TABLE", "OWNERSHIP"))


def test_explicit_output_scope_is_persisted_and_used_by_native_setup(destinations_service):
    created, _ = destinations_service.create(
        snowflake_account='acme-sf', destination_mode='streaming',
        destination_database='client_data', destination_schema='qual_site',
    )
    fetched = destinations_service.get(created.id)
    assert fetched.destination_database == 'CLIENT_DATA'
    assert fetched.destination_schema == 'QUAL_SITE'
    assert fetched.to_dict()['destination_schema'] == 'QUAL_SITE'
    script = destinations_service.setup_script(created.id)
    assert 'TYPE = SERVICE' in script and 'MUST_CHANGE_PASSWORD' not in script
    assert 'CLIENT_DATA.QUAL_SITE' in script
    assert 'QUADRINGENT.' not in script and '.RAW' not in script and '.CURATED' not in script
    assert f'GRANT USAGE, CREATE TABLE ON SCHEMA CLIENT_DATA.QUAL_SITE TO ROLE {created.service_role};' in script
    assert 'EXECUTE TASK' not in script


@pytest.mark.parametrize('field', ['destination_database', 'destination_schema'])
@pytest.mark.parametrize('value', ['', 'DB.SCHEMA', 'A; DROP DATABASE X', 'A--', '"Mixed"', 'x' * 64, 123])
def test_explicit_scope_rejects_unsafe_identifiers_before_creating_destination(destinations_service, field, value, monkeypatch):
    calls = []
    monkeypatch.setattr("quadringent_control_plane.v2.services.destinations._generate_rsa_key_pair", lambda: calls.append(True))
    with pytest.raises(DestinationValidationError):
        destinations_service.create(snowflake_account='acme-sf', **{field: value})
    assert destinations_service.list() == ()
    assert calls == []


def test_legacy_defaults_and_service_identity_are_preserved(destinations_service):
    record, _ = destinations_service.create(snowflake_account='acme-sf')
    assert record.destination_database == 'QUADRINGENT' and record.destination_schema is None
    script = destinations_service.setup_script(record.id)
    assert 'QUADRINGENT.RAW' in script and 'QUADRINGENT.CURATED' in script
    assert 'TYPE = SERVICE' in script


def test_custom_database_without_schema_keeps_legacy_schemas(destinations_service):
    record, _ = destinations_service.create(snowflake_account='acme-sf', destination_database='client_db')
    assert record.destination_database == 'CLIENT_DB' and record.destination_schema is None
    script = destinations_service.setup_script(record.id)
    assert 'CLIENT_DB.RAW' in script and 'CLIENT_DB.CURATED' in script
    assert 'QUADRINGENT' not in script
    assert record.verification_state == 'declared_not_verified'


def test_destination_scope_migration_backfills_legacy_records_without_changing_evidence(tmp_path):
    from alembic import command
    from sqlalchemy import MetaData, Table, select

    dsn = f"sqlite:///{tmp_path / 'legacy.sqlite3'}"
    command.upgrade(v2_db.alembic_config(dsn), '0015_one_time_secrets')
    engine = v2_db.create_engine_for(dsn)
    old = Table('destinations', MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(v2_schema.organizations.insert(), {'id':'legacy', 'name':'Legacy'})
        for name, sql in [('copy', 'legacy copy SQL unchanged'), ('streaming', 'legacy streaming SQL unchanged')]:
            conn.execute(old.insert(), {'id':name, 'org_id':'legacy', 'snowflake_account':'acme-sf', 'key_pair_ciphertext':'encrypted-'+name, 'setup_script':sql, 'verification_state':'declared_not_verified'})
    v2_db.run_migrations(dsn)
    v2_db.run_migrations(dsn)
    with engine.connect() as conn:
        rows = conn.execute(select(v2_schema.destinations)).mappings().all()
    assert len(rows) == 2
    for row in rows:
        assert row['destination_database'] == 'QUADRINGENT' and row['destination_schema'] is None
        assert row['key_pair_ciphertext'] == 'encrypted-'+row['id']
        assert row['setup_script'] == 'legacy '+row['id']+' SQL unchanged'
        assert row['verification_state'] == 'declared_not_verified'
    engine.dispose()
