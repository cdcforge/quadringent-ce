"""Émission unique : API native, SQL persisté, rejeu et reprise d'une base ancienne."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from alembic import command
from fastapi.testclient import TestClient
import pytest
import sqlalchemy as sa

from quadringent_control_plane.v2 import db, schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.audit import AuditService
from quadringent_control_plane.v2.services.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyStore,
)


@pytest.fixture()
def native_app(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'native.sqlite3'}"
    db.run_migrations(dsn)
    engine = db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "default", "name": "Test"})
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        token_pepper=secrets.token_bytes(32),
        require_authentication=True,
    )
    try:
        with TestClient(app) as client:
            yield client, engine
    finally:
        engine.dispose()


def _activate_and_login(client, created, *, email):
    password = secrets.token_urlsafe(32)
    activated = client.post(
        "/v2/users/activate",
        json={"token": created["activation_token"], "password": password},
        headers={"Idempotency-Key": secrets.token_hex(16)},
    )
    assert activated.status_code == 200
    login = client.post("/v2/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200


def _login_admin(client):
    response = client.post(
        "/v2/setup/first-admin",
        json={"email": "admin@example.test"},
        headers={"Idempotency-Key": "initial-admin"},
    )
    assert response.status_code == 201
    _activate_and_login(client, response.json()["after"], email="admin@example.test")


def _assert_secret_absent(value, secret):
    # Ne jamais inclure une valeur générée dans le détail d'une assertion RED.
    absent = json.dumps(secret)[1:-1] not in json.dumps(value)
    assert absent, "Un secret émis une fois ne doit pas être persisté ou rejoué"


def _assert_persisted_clean(engine, secret):
    with engine.connect() as connection:
        responses = connection.execute(sa.select(schema.idempotency_keys.c.response)).scalars().all()
        audits = (
            connection.execute(sa.select(schema.audit_records.c.before, schema.audit_records.c.after)).mappings().all()
        )
    _assert_secret_absent(responses, secret)
    _assert_secret_absent([dict(row) for row in audits], secret)


def test_first_admin_lost_response_replay_does_not_reissue_activation(native_app):
    client, engine = native_app
    payload = {"email": "first@example.test"}
    headers = {"Idempotency-Key": "setup-once"}
    first = client.post("/v2/setup/first-admin", json=payload, headers=headers)
    assert first.status_code == 201
    body = first.json()
    token = body["after"]["activation_token"]
    assert isinstance(token, str) and bool(token)
    _assert_persisted_clean(engine, token)
    replay = client.post("/v2/setup/first-admin", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["after"]["id"] == body["after"]["id"]
    assert "activation_token" not in replay.json()["after"]
    # La première réponse reste exploitable ; aucun deuxième admin n'est créé.
    _activate_and_login(client, body["after"], email=payload["email"])
    assert len(client.get("/v2/users").json()["items"]) == 1


@pytest.mark.parametrize(
    ("route", "payload", "field"),
    [
        ("/v2/destinations", {"snowflake_account": "example-trial"}, "private_key_pem"),
        ("/v2/agent-tokens", {"name": "test", "scope": "operate", "never_expires": True}, "token"),
        ("/v2/users", {"email": "invite@example.test", "role": "reader"}, "activation_token"),
        ("/v2/webhooks", {"url": "https://example.test/hooks", "events": ["pipeline.updated"]}, "secret"),
    ],
)
def test_native_creation_replay_preserves_metadata_without_secrets(native_app, route, payload, field):
    client, engine = native_app
    _login_admin(client)
    headers = {"Idempotency-Key": "create-once"}
    first = client.post(route, json=payload, headers=headers)
    assert first.status_code == 201
    first_body = first.json()
    secret = first_body["after"][field]
    assert isinstance(secret, str) and bool(secret)
    _assert_persisted_clean(engine, secret)
    replay = client.post(route, json=payload, headers=headers)
    assert replay.status_code == 201
    expected = {**first_body, "after": {k: v for k, v in first_body["after"].items() if k != field}}
    clean = replay.json() == expected
    assert clean, "Le rejeu doit conserver les métadonnées sans secret"
    matching = [row for row in client.get(route).json()["items"] if row["id"] == first_body["after"]["id"]]
    assert len(matching) == 1
    _assert_secret_absent(client.get("/v2/audit").json(), secret)
    _assert_secret_absent(client.get(first_body["verify"]["path"]).json(), secret)
    if field == "private_key_pem":
        with engine.connect() as connection:
            ciphertext = connection.execute(sa.select(schema.destinations.c.key_pair_ciphertext)).scalar_one()
        same_key = client.app.state.secret_box.decrypt(ciphertext) == secret
        assert same_key, "La première clé reste celle du keystore chiffré"
    if field == "token":
        client.cookies.clear()
        assert client.get("/v2/sources", headers={"Authorization": f"Bearer {secret}"}).status_code == 200


def test_native_token_rotation_emits_once_without_second_rotation(native_app):
    client, engine = native_app
    _login_admin(client)
    first = client.post(
        "/v2/agent-tokens",
        json={"name": "rotate", "scope": "operate", "never_expires": True},
        headers={"Idempotency-Key": "token-create"},
    )
    route = f"/v2/agent-tokens/{first.json()['after']['id']}/rotate"
    headers = {"Idempotency-Key": "token-rotate"}
    rotated = client.post(route, json={}, headers=headers)
    assert rotated.status_code == 200
    body = rotated.json()
    _assert_persisted_clean(engine, body["after"]["token"])
    replay = client.post(route, json={}, headers=headers)
    assert replay.status_code == 200
    assert "token" not in replay.json()["after"]
    assert replay.json()["after"]["id"] == body["after"]["id"]
    client.cookies.clear()
    assert client.get("/v2/sources", headers={"Authorization": f"Bearer {body['after']['token']}"}).status_code == 200
    assert (
        client.get("/v2/sources", headers={"Authorization": f"Bearer {first.json()['after']['token']}"}).status_code
        == 401
    )


def test_another_authenticated_actor_cannot_replay_destination(native_app):
    client, engine = native_app
    _login_admin(client)
    invited = client.post(
        "/v2/users",
        json={"email": "other@example.test", "role": "admin"},
        headers={"Idempotency-Key": "invite-admin"},
    )
    payload = {"snowflake_account": "example-trial"}
    headers = {"Idempotency-Key": "owned-destination"}
    first = client.post("/v2/destinations", json=payload, headers=headers)
    assert first.status_code == 201
    other = TestClient(client.app)
    try:
        _activate_and_login(other, invited.json()["after"], email="other@example.test")
        denied = other.post("/v2/destinations", json=payload, headers=headers)
    finally:
        other.close()
    assert denied.status_code == 409
    _assert_secret_absent(denied.json(), first.json()["after"]["private_key_pem"])
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(schema.destinations)).scalar() == 1


def test_legacy_replays_and_audit_reads_are_defensively_redacted(native_app):
    _client, engine = native_app
    secret = secrets.token_urlsafe(32)
    response = {"after": {"id": "legacy", "nested": [{"token": secret}], "private_key_pem": secret}}
    with engine.begin() as connection:
        connection.execute(
            schema.idempotency_keys.insert(),
            {
                "org_id": "default",
                "key": "legacy",
                "actor_id": "owner",
                "method": "POST",
                "path": "/v2/destinations",
                "request_hash": "hash",
                "status_code": 201,
                "response": response,
            },
        )
        connection.execute(
            schema.audit_records.insert(),
            {
                "id": "legacy",
                "org_id": "default",
                "actor_kind": "human",
                "actor_id": "owner",
                "actor_display": "Owner",
                "action": "test",
                "resource_type": "destination",
                "status": "succeeded",
                "dry_run": False,
                "before": {"secret": secret},
                "after": response,
            },
        )
    replay = IdempotencyStore(engine).resolve(
        key="legacy",
        actor_id="owner",
        method="POST",
        path="/v2/destinations",
        body_hash="hash",
    )
    assert replay is not None
    _assert_secret_absent(replay.body, secret)
    service = AuditService(engine, org_id="default")
    _assert_secret_absent(service.get("legacy").to_dict(), secret)
    _assert_secret_absent([r.to_dict() for r in service.query()], secret)


def test_store_and_audit_do_not_mutate_initial_nested_response(native_app):
    _client, engine = native_app
    secret = secrets.token_urlsafe(32)
    response = {"before": None, "after": {"id": "test", "nested": [{"token": secret, "token_set": True}]}}
    original = json.dumps(response)
    IdempotencyStore(engine).store(
        key="nested",
        actor_id="owner",
        method="POST",
        path="/test",
        body_hash="hash",
        status_code=201,
        response=response,
    )
    AuditService(engine, org_id="default").record(
        actor_kind="human",
        actor_id="owner",
        actor_display="Owner",
        action="test",
        resource_type="test",
        status="succeeded",
        after=response,
    )
    unchanged = json.dumps(response) == original
    assert unchanged, "La première réponse ne doit pas être mutée"
    _assert_persisted_clean(engine, secret)


def test_upgrade_sanitizes_legacy_json_without_touching_encrypted_or_hashed_auth(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'legacy.sqlite3'}"
    _verify_legacy_upgrade(dsn, restore_dsn=f"sqlite:///{tmp_path / 'restored.sqlite3'}")


def _verify_legacy_upgrade(dsn, *, restore_dsn=None):
    command.upgrade(db.alembic_config(dsn), "0014_pipeline_state_before_pause")
    engine = db.create_engine_for(dsn)
    secret = secrets.token_urlsafe(32)
    ciphertext = SecretBox(SecretBox.generate_key()).encrypt(secret)
    legacy = sa.MetaData()
    legacy.reflect(engine)
    with engine.begin() as connection:
        connection.execute(legacy.tables["organizations"].insert(), {"id": "default", "name": "Test"})
        connection.execute(
            legacy.tables["destinations"].insert(),
            {
                "id": "d1",
                "org_id": "default",
                "snowflake_account": "example-trial",
                "key_pair_ciphertext": ciphertext,
                "setup_script": "-- public",
            },
        )
        connection.execute(
            legacy.tables["idempotency_keys"].insert(),
            {
                "key": "old",
                "actor_id": "setup",
                "method": "POST",
                "path": "/v2/setup/first-admin",
                "request_hash": "hash",
                "status_code": 201,
                "response": {
                    "after": {
                        "id": "u1",
                        "activation_token": secret,
                        "private_key_pem": secret,
                        "secret": secret,
                        "nested": [{"token": secret}],
                    }
                },
                "created_at": datetime.now(timezone.utc),
            },
        )
        connection.execute(
            legacy.tables["audit_records"].insert(),
            {
                "id": "old",
                "org_id": "default",
                "actor_kind": "human",
                "actor_id": "owner",
                "actor_display": "Owner",
                "action": "test",
                "resource_type": "test",
                "status": "succeeded",
                "dry_run": False,
                "before": {"secret": secret},
                "after": {"token": secret},
            },
        )
        connection.execute(
            legacy.tables["agent_tokens"].insert(),
            {
                "id": "a1",
                "org_id": "default",
                "name": "Test",
                "hash": "synthetic-hash",
                "prefix": "qdt_rd",
                "scope": "read",
                "source_restriction": [],
                "pre_authorized_actions": [],
                "created_by": "Owner",
                "never_expires": True,
            },
        )
    if restore_dsn is not None:
        engine.dispose()
        original = sa.engine.make_url(dsn).database
        restored = sa.engine.make_url(restore_dsn).database
        with sqlite3.connect(original) as source, sqlite3.connect(restored) as target:
            source.backup(target)
        dsn = restore_dsn
        engine = db.create_engine_for(dsn)
    try:
        db.run_migrations(dsn)
        db.run_migrations(dsn)
        _assert_persisted_clean(engine, secret)
        with engine.connect() as connection:
            assert connection.execute(sa.select(schema.destinations.c.key_pair_ciphertext)).scalar() == ciphertext
            assert connection.execute(sa.select(schema.agent_tokens.c.hash)).scalar() == "synthetic-hash"
            assert connection.execute(sa.select(schema.idempotency_keys.c.key)).scalar() == "old"
        replay = IdempotencyStore(engine, org_id="default").resolve(
            key="old",
            actor_id="setup",
            method="POST",
            path="/v2/setup/first-admin",
            body_hash="hash",
        )
        assert replay is not None
        assert replay.body == {"after": {"id": "u1", "nested": [{}]}}
    finally:
        engine.dispose()


def test_same_key_and_actor_are_isolated_between_organizations(native_app):
    _client, engine = native_app
    arguments = dict(key="same", actor_id="setup", method="POST", path="/setup", body_hash="same")
    for org_id in ("first", "second"):
        IdempotencyStore(engine, org_id=org_id).store(
            **arguments,
            status_code=201,
            response={"after": {"id": org_id}},
        )
    for org_id in ("first", "second"):
        replay = IdempotencyStore(engine, org_id=org_id).resolve(**arguments)
        assert replay.body == {"after": {"id": org_id}}


def test_app_binds_idempotency_to_its_organization(native_app):
    client, engine = native_app
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "other", "name": "Other"})
    other_app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        org_id="other",
        require_authentication=True,
        token_pepper=secrets.token_bytes(32),
    )
    payload = {"email": "same@example.test"}
    headers = {"Idempotency-Key": "same-setup"}
    first = client.post("/v2/setup/first-admin", json=payload, headers=headers)
    with TestClient(other_app) as other:
        second = other.post("/v2/setup/first-admin", json={"email": "other@example.test"}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["after"]["id"] != second.json()["after"]["id"]


def test_audit_cannot_read_other_organization(native_app):
    _client, engine = native_app
    record = AuditService(engine, org_id="default").record(
        actor_kind="human",
        actor_id="owner",
        actor_display="Owner",
        action="test",
        resource_type="test",
        status="succeeded",
        after={"id": "public"},
    )
    other = AuditService(engine, org_id="other")
    assert other.query() == ()
    with pytest.raises(sa.exc.NoResultFound):
        other.get(record.id)


@pytest.mark.parametrize("organizations", [(), ("first", "second")])
def test_upgrade_does_not_invent_organization_for_ambiguous_legacy_keys(tmp_path, organizations):
    dsn = f"sqlite:///{tmp_path / 'ambiguous.sqlite3'}"
    command.upgrade(db.alembic_config(dsn), "0014_pipeline_state_before_pause")
    engine = db.create_engine_for(dsn)
    legacy = sa.MetaData()
    legacy.reflect(engine)
    with engine.begin() as connection:
        if organizations:
            connection.execute(
                legacy.tables["organizations"].insert(),
                [{"id": org_id, "name": org_id} for org_id in organizations],
            )
        connection.execute(
            legacy.tables["idempotency_keys"].insert(),
            {
                "key": "legacy",
                "actor_id": "setup",
                "method": "POST",
                "path": "/setup",
                "request_hash": "hash",
                "status_code": 201,
                "response": {"after": {"id": "old"}},
                "created_at": datetime.now(timezone.utc) - timedelta(days=2),
            },
        )
    try:
        db.run_migrations(dsn)
        for org_id in ("first", "second"):
            with pytest.raises(IdempotencyKeyConflictError):
                IdempotencyStore(engine, org_id=org_id).resolve(
                    key="legacy",
                    actor_id="setup",
                    method="POST",
                    path="/setup",
                    body_hash="hash",
                )
        with engine.connect() as connection:
            assert connection.execute(sa.select(schema.idempotency_keys.c.org_id)).scalar() == ""
            assert connection.execute(sa.select(sa.func.count()).select_from(schema.idempotency_keys)).scalar() == 1
    finally:
        engine.dispose()


def test_expired_scoped_key_can_be_reused_without_deleting_another_site(native_app):
    _client, engine = native_app
    arguments = dict(key="expired", actor_id="owner", method="POST", path="/test", body_hash="hash")
    now = datetime.now(timezone.utc)
    first = IdempotencyStore(engine, org_id="first")
    first.store(**arguments, now=now - timedelta(days=2), status_code=201, response={"after": {"id": "old"}})
    second = IdempotencyStore(engine, org_id="second")
    second.store(**arguments, now=now, status_code=201, response={"after": {"id": "other"}})
    assert first.resolve(**arguments, now=now) is None
    first.store(**arguments, now=now, status_code=201, response={"after": {"id": "new"}})
    assert first.resolve(**arguments, now=now).body == {"after": {"id": "new"}}
    assert second.resolve(**arguments, now=now).body == {"after": {"id": "other"}}


@pytest.mark.postgres
def test_postgres_legacy_upgrade_and_scoped_primary_key(postgres_dsn):
    # Base propre indépendante des autres tests partageant le conteneur local.
    database = "one_time_" + secrets.token_hex(8)
    administrative = db.create_engine_for(postgres_dsn)
    url = sa.engine.make_url(postgres_dsn).set(database=database)
    dsn = url.render_as_string(hide_password=False)
    with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
    try:
        _verify_legacy_upgrade(dsn)
        engine = db.create_engine_for(dsn)
        try:
            arguments = dict(key="shared", actor_id="setup", method="POST", path="/test", body_hash="hash")
            for org_id in ("first", "second"):
                IdempotencyStore(engine, org_id=org_id).store(
                    **arguments,
                    status_code=201,
                    response={"after": {"id": org_id}},
                )
            for org_id in ("first", "second"):
                assert IdempotencyStore(engine, org_id=org_id).resolve(**arguments).body == {"after": {"id": org_id}}
        finally:
            engine.dispose()
    finally:
        with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE "{database}"'))
        administrative.dispose()


def test_upgrade_scrubs_all_batches_and_preserves_public_identifiers(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'batches.sqlite3'}"
    command.upgrade(db.alembic_config(dsn), "0014_pipeline_state_before_pause")
    engine = db.create_engine_for(dsn)
    legacy = sa.MetaData()
    legacy.reflect(engine)
    secret = secrets.token_urlsafe(32)
    identifiers = [f"item-{number:04}" for number in range(205)]
    with engine.begin() as connection:
        connection.execute(legacy.tables["organizations"].insert(), {"id": "default", "name": "Test"})
        connection.execute(
            legacy.tables["idempotency_keys"].insert(),
            [
                {
                    "key": identifier,
                    "actor_id": "owner",
                    "method": "POST",
                    "path": "/test",
                    "request_hash": "hash",
                    "status_code": 201,
                    "response": {"after": {"id": identifier, "token": secret, "token_set": True}},
                }
                for identifier in identifiers
            ],
        )
        connection.execute(
            legacy.tables["audit_records"].insert(),
            [
                {
                    "id": identifier,
                    "org_id": "default",
                    "actor_kind": "human",
                    "actor_id": "owner",
                    "actor_display": "Owner",
                    "action": "test",
                    "resource_type": "test",
                    "status": "succeeded",
                    "dry_run": False,
                    "before": None,
                    "after": {"id": identifier, "token": secret},
                }
                for identifier in identifiers
            ],
        )
    try:
        db.run_migrations(dsn)
        _assert_persisted_clean(engine, secret)
        with engine.connect() as connection:
            rows = (
                connection.execute(sa.select(schema.idempotency_keys).order_by(schema.idempotency_keys.c.key))
                .mappings()
                .all()
            )
            assert [row["key"] for row in rows] == identifiers
            assert all(row["response"] == {"after": {"id": row["key"], "token_set": True}} for row in rows)
            assert connection.execute(sa.select(sa.func.count()).select_from(schema.audit_records)).scalar() == 205
    finally:
        engine.dispose()
