"""Réémission explicite du premier admin pending, sans secret persisté/rejoué."""

import io
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from quadringent.installer.cli import _build_parser
from quadringent.installer.v2_commands import run_v2_command
from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2 import db
from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService
from quadringent_control_plane.v2.services.users import ActivationTokenInvalidError, UsersService
from test_v2_one_time_secrets import (
    _activate_and_login,
    _assert_persisted_clean,
    native_app,  # noqa: F401 — fixture native partagée
)


def _pending(client):
    created = client.post(
        "/v2/setup/first-admin",
        json={"email": "pending@example.test"},
        headers={"Idempotency-Key": "setup"},
    )
    assert created.status_code == 201
    return created.json()["after"]


def _bootstrap_operator(client, scope="admin"):
    _record, token = client.app.state.agent_tokens_service.create(
        name="Bootstrap",
        scope=scope,
        created_by="test",
        never_expires=True,
    )
    return token


def test_explicit_reissue_invalidates_old_link_and_preserves_same_pending_user(native_app):
    client, engine = native_app
    pending = _pending(client)
    token = _bootstrap_operator(client)
    route = f"/v2/users/{pending['id']}/activation/reissue"
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "reissue"}
    reference = datetime.now(timezone.utc)
    first = client.post(route, json={}, headers=headers)
    assert first.status_code == 201
    after = first.json()["after"]
    assert after["id"] == pending["id"]
    assert after["activated_at"] is None
    assert isinstance(after["activation_token"], str) and bool(after["activation_token"])
    _assert_persisted_clean(engine, after["activation_token"])
    replay = client.post(route, json={}, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["after"]["id"] == pending["id"]
    assert "activation_token" not in replay.json()["after"]
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(schema.users)).scalar() == 1
        links = connection.execute(sa.select(schema.activation_tokens)).mappings().all()
        assert len(links) == 2
        assert sum(row["used_at"] is None for row in links) == 1
        expiry = next(row["expires_at"] for row in links if row["used_at"] is None).replace(tzinfo=timezone.utc)
        assert reference + timedelta(hours=24) <= expiry < reference + timedelta(hours=24, seconds=10)
        audits = (
            connection.execute(
                sa.select(schema.audit_records).where(
                    schema.audit_records.c.action == "user.activation.reissue",
                )
            )
            .mappings()
            .all()
        )
        assert len(audits) == 1 and "activation_token" not in audits[0]["after"]
    assert (
        client.post(
            "/v2/users/activate",
            json={"token": pending["activation_token"], "password": "synthetic-password"},
            headers={"Idempotency-Key": "old-activation"},
        ).status_code
        == 400
    )
    _activate_and_login(client, after, email=after["email"])
    refused = client.post(route, json={}, headers={**headers, "Idempotency-Key": "after-activation"})
    assert refused.status_code == 409


@pytest.mark.parametrize("scope", [None, "read", "operate", "admin"])
def test_reissue_requires_authenticated_admin_before_replay(native_app, scope):
    client, _engine = native_app
    pending = _pending(client)
    route = f"/v2/users/{pending['id']}/activation/reissue"
    operator = _bootstrap_operator(client)
    key = {"Idempotency-Key": "protected-reissue"}
    first = client.post(route, json={}, headers={**key, "Authorization": f"Bearer {operator}"})
    assert first.status_code == 201
    headers = {**key, "x-request-actor": "spoof"}
    if scope is not None:
        headers["Authorization"] = f"Bearer {_bootstrap_operator(client, scope)}"
    denied = client.post(route, json={}, headers=headers)
    expected_status = 401 if scope is None else (409 if scope == "admin" else 403)
    assert denied.status_code == expected_status


def test_cli_users_reissue_activation_calls_native_protected_route(native_app, tmp_path, monkeypatch):
    client, engine = native_app
    pending = _pending(client)
    token = _bootstrap_operator(client)

    def handler(request):
        response = client.request(
            request.method, str(request.url), content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)

    config_path = tmp_path / "operator.json"
    config_path.write_text(json.dumps({"url": "http://testserver", "token": token}))
    config_path.chmod(0o600)
    monkeypatch.setenv("QUADRINGENT_URL", "http://wrong-site.example.test")
    monkeypatch.setenv("QUADRINGENT_TOKEN", "wrong-site-token")
    transport = httpx.MockTransport(handler)
    args = _build_parser().parse_args(
        [
            "users",
            "--config",
            str(config_path),
            "reissue-activation",
            pending["id"],
            "--idempotency-key",
            "cli-reissue",
        ]
    )
    first_output = io.StringIO()
    assert run_v2_command(args, transport=transport, stdout=first_output) == 0
    first = json.loads(first_output.getvalue())["result"]["after"]
    _assert_persisted_clean(engine, first["activation_token"])
    replay_output = io.StringIO()
    assert run_v2_command(args, transport=transport, stdout=replay_output) == 0
    assert "activation_token" not in json.loads(replay_output.getvalue())["result"]["after"]


def test_foreign_agent_token_cannot_authenticate_recovery_even_with_same_pepper(native_app):
    client, engine = native_app
    pending = _pending(client)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "other", "name": "Other"})
    foreign = AgentTokensService(engine, org_id="other", pepper=client.app.state.token_pepper)
    _record, token = foreign.create(name="Foreign", scope="admin", created_by="test", never_expires=True)
    response = client.post(
        f"/v2/users/{pending['id']}/activation/reissue",
        json={},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "foreign",
        },
    )
    assert response.status_code == 401


def test_recovery_cannot_target_foreign_or_reader_users(native_app):
    client, engine = native_app
    _pending(client)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "other", "name": "Other"})
    foreign = UsersService(engine, org_id="other", pepper=client.app.state.token_pepper)
    foreign_user, _token = foreign.create_first_admin(email="foreign@example.test")
    local = client.app.state.users_service
    reader, _token = local.invite(email="reader@example.test", role="reader")
    operator = _bootstrap_operator(client)
    for user_id, status in ((foreign_user.id, 404), (reader.id, 409)):
        response = client.post(
            f"/v2/users/{user_id}/activation/reissue",
            json={},
            headers={
                "Authorization": f"Bearer {operator}",
                "Idempotency-Key": user_id,
            },
        )
        assert response.status_code == status


def test_old_link_read_before_reissue_cannot_activate_after_invalidation(native_app, monkeypatch):
    from quadringent_control_plane.v2.services import users

    client, engine = native_app
    pending = _pending(client)
    service = client.app.state.users_service
    original_hash_password = users.hash_password

    def reissue_between_read_and_consume(password):
        service.reissue_first_admin_activation(pending["id"])
        return original_hash_password(password)

    monkeypatch.setattr(users, "hash_password", reissue_between_read_and_consume)
    with pytest.raises(ActivationTokenInvalidError):
        service.activate(activation_token=pending["activation_token"], password="synthetic-password")
    with engine.connect() as connection:
        row = connection.execute(sa.select(schema.users)).mappings().one()
        assert row["activated_at"] is None and row["password_hash"] is None
        links = connection.execute(sa.select(schema.activation_tokens)).mappings().all()
        assert len(links) == 2 and sum(row["used_at"] is None for row in links) == 1


def test_failed_reissue_rolls_back_invalidation_of_existing_link(native_app):
    client, engine = native_app
    pending = _pending(client)

    def reject_new_link(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("INSERT INTO activation_tokens"):
            raise RuntimeError("échec synthétique d'insertion")

    sa.event.listen(engine, "before_cursor_execute", reject_new_link)
    try:
        with pytest.raises(RuntimeError, match="synthétique"):
            client.app.state.users_service.reissue_first_admin_activation(pending["id"])
    finally:
        sa.event.remove(engine, "before_cursor_execute", reject_new_link)
    with engine.connect() as connection:
        links = connection.execute(sa.select(schema.activation_tokens)).mappings().all()
        assert len(links) == 1 and links[0]["used_at"] is None


def test_foreign_session_is_refused_and_foreign_active_admin_does_not_block_site(native_app):
    client, engine = native_app
    pending = _pending(client)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "other", "name": "Other"})
    foreign = UsersService(engine, org_id="other", pepper=client.app.state.token_pepper)
    foreign_user, activation_token = foreign.create_first_admin(email="foreign@example.test")
    active = foreign.activate(activation_token=activation_token, password="synthetic-password")
    client.cookies.set("quadringent_session", foreign.create_session_token(active))
    route = f"/v2/users/{pending['id']}/activation/reissue"
    denied = client.post(route, json={}, headers={"Idempotency-Key": "foreign-cookie"})
    assert denied.status_code == 401
    client.cookies.clear()
    local_token = _bootstrap_operator(client)
    allowed = client.post(
        route,
        json={},
        headers={
            "Idempotency-Key": "local-reissue",
            "Authorization": f"Bearer {local_token}",
        },
    )
    assert allowed.status_code == 201
    assert foreign_user.id != allowed.json()["after"]["id"]


def test_another_active_admin_blocks_reissue_of_pending_target(native_app):
    client, _engine = native_app
    pending = _pending(client)
    service = client.app.state.users_service
    other, token = service.create_first_admin(email="other-admin@example.test")
    service.activate(activation_token=token, password="synthetic-password")
    operator = _bootstrap_operator(client)
    refused = client.post(
        f"/v2/users/{pending['id']}/activation/reissue",
        json={},
        headers={
            "Idempotency-Key": "blocked-reissue",
            "Authorization": f"Bearer {operator}",
        },
    )
    assert refused.status_code == 409
    assert other.id != pending["id"]
    assert service.get(pending["id"]).activated_at is None


@pytest.mark.postgres
def test_postgres_reissue_wins_over_inflight_activation_without_reset(postgres_dsn, monkeypatch):
    from quadringent_control_plane.v2.services import users

    database = "activation_" + secrets.token_hex(8)
    administrative = db.create_engine_for(postgres_dsn)
    dsn = sa.engine.make_url(postgres_dsn).set(database=database).render_as_string(hide_password=False)
    with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{database}"'))
    engine = None
    try:
        db.run_migrations(dsn)
        engine = db.create_engine_for(dsn)
        with engine.begin() as connection:
            connection.execute(schema.organizations.insert(), {"id": "default", "name": "Test"})
        service = UsersService(engine, org_id="default", pepper=secrets.token_bytes(32))
        pending, old_token = service.create_first_admin(email="pending@example.test")
        read_finished, continue_activation = threading.Event(), threading.Event()
        original_hash_password = users.hash_password

        def hold_after_token_read(password):
            read_finished.set()
            if not continue_activation.wait(timeout=5):
                raise RuntimeError("délai de synchronisation synthétique dépassé")
            return original_hash_password(password)

        monkeypatch.setattr(users, "hash_password", hold_after_token_read)
        with ThreadPoolExecutor(max_workers=1) as executor:
            inflight = executor.submit(service.activate, activation_token=old_token, password="synthetic-password")
            try:
                assert read_finished.wait(timeout=5)
                same_user, fresh_token = service.reissue_first_admin_activation(pending.id)
                assert same_user.id == pending.id
            finally:
                continue_activation.set()
            with pytest.raises(ActivationTokenInvalidError):
                inflight.result(timeout=5)
        monkeypatch.setattr(users, "hash_password", original_hash_password)
        assert service.get(pending.id).activated_at is None
        active = service.activate(activation_token=fresh_token, password="synthetic-password")
        assert active.id == pending.id and active.activated_at is not None
        with pytest.raises(ActivationTokenInvalidError):
            service.activate(activation_token=fresh_token, password="another-synthetic-password")
    finally:
        if engine is not None:
            engine.dispose()
        with administrative.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE "{database}"'))
        administrative.dispose()
