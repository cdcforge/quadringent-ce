"""Tâche 9 — users/roles/activation : premier admin, reader ne peut pas écrire."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.services.users import (
    ActivationTokenInvalidError,
    AdminAlreadyExistsError,
    InvalidCredentialsError,
    SessionInvalidError,
    UserNotFoundError,
    UsersService,
    UserValidationError,
)


@pytest.fixture()
def users_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'users.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    service = UsersService(engine, org_id="org1", pepper=b"pepper-de-test")
    try:
        yield service
    finally:
        engine.dispose()


def test_create_first_admin_then_activate(users_service) -> None:
    record, activation_token = users_service.create_first_admin(email="admin@example.com")
    assert record.role == "admin"
    assert record.activated_at is None
    activated = users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    assert activated.activated_at is not None


def test_create_first_admin_twice_is_refused(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    with pytest.raises(AdminAlreadyExistsError):
        users_service.create_first_admin(email="autre-admin@example.com")


def test_create_first_admin_rejects_invalid_email(users_service) -> None:
    with pytest.raises(UserValidationError):
        users_service.create_first_admin(email="pas-un-email")


def test_activate_rejects_unknown_token(users_service) -> None:
    with pytest.raises(ActivationTokenInvalidError):
        users_service.activate(activation_token="jeton-invente", password="x" * 12)


def test_activate_rejects_already_used_token(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    with pytest.raises(ActivationTokenInvalidError):
        users_service.activate(activation_token=activation_token, password="autre-mot-de-passe")


def test_activate_rejects_expired_token(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(
        email="admin@example.com", now=datetime.now(timezone.utc) - timedelta(hours=25)
    )
    with pytest.raises(ActivationTokenInvalidError):
        users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")


def test_authenticate_password_succeeds_after_activation(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    authenticated = users_service.authenticate_password(email="admin@example.com", password="un-mot-de-passe-robuste")
    assert authenticated.role == "admin"


def test_authenticate_password_fails_before_activation(users_service) -> None:
    users_service.create_first_admin(email="admin@example.com")
    with pytest.raises(InvalidCredentialsError):
        users_service.authenticate_password(email="admin@example.com", password="peu-importe")


def test_authenticate_password_fails_with_wrong_password(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    with pytest.raises(InvalidCredentialsError):
        users_service.authenticate_password(email="admin@example.com", password="mauvais-mot-de-passe")


def test_invite_reader_defaults_to_reader_role(users_service) -> None:
    _admin, admin_token = users_service.create_first_admin(email="admin@example.com")
    users_service.activate(activation_token=admin_token, password="un-mot-de-passe-robuste")
    record, _token = users_service.invite(email="lecteur@example.com", role="reader")
    assert record.role == "reader"


def test_invite_rejects_unknown_role(users_service) -> None:
    with pytest.raises(UserValidationError):
        users_service.invite(email="x@example.com", role="superadmin")


def test_session_token_round_trip(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    user = users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    session_token = users_service.create_session_token(user)
    resolved = users_service.resolve_session(session_token)
    assert resolved.id == user.id


def test_session_token_rejects_tampering(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    user = users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    session_token = users_service.create_session_token(user)
    tampered = session_token[:-1] + ("0" if session_token[-1] != "0" else "1")
    with pytest.raises(SessionInvalidError):
        users_service.resolve_session(tampered)


def test_session_token_rejects_expired_session(users_service) -> None:
    _record, activation_token = users_service.create_first_admin(email="admin@example.com")
    user = users_service.activate(activation_token=activation_token, password="un-mot-de-passe-robuste")
    past = datetime.now(timezone.utc) - timedelta(hours=13)
    session_token = users_service.create_session_token(user, now=past)
    with pytest.raises(SessionInvalidError):
        users_service.resolve_session(session_token)


def test_get_unknown_user_raises_not_found(users_service) -> None:
    with pytest.raises(UserNotFoundError):
        users_service.get("does-not-exist")
