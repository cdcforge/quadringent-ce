"""Un chemin d'activation incorrect ne doit modifier aucun utilisateur ni lien."""

import pytest
import sqlalchemy as sa

from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.services.users import ActivationTokenInvalidError, UsersService
from test_v2_one_time_secrets import native_app  # noqa: F401 — fixture native partagée


def _pending(client):
    reply = client.post(
        "/v2/setup/first-admin", json={"email": "pending@example.test"},
        headers={"Idempotency-Key": "setup"},
    )
    assert reply.status_code == 201
    return reply.json()["after"]


def _unchanged_pending(engine, user_id):
    with engine.connect() as connection:
        user = connection.execute(
            sa.select(schema.users).where(schema.users.c.id == user_id)
        ).mappings().one()
        assert user["activated_at"] is None and user["password_hash"] is None
        links = connection.execute(
            sa.select(schema.activation_tokens.c.used_at).where(schema.activation_tokens.c.user_id == user_id)
        ).scalars().all()
        assert links == [None]


@pytest.mark.parametrize("wrong_target", ["local-user", "unknown-user", "foreign-user"])
@pytest.mark.parametrize("retry_route", ["by-id", "by-token"])
def test_wrong_path_preserves_user_and_token_for_correct_activation(native_app, wrong_target, retry_route):
    client, engine = native_app
    pending = _pending(client)
    service = client.app.state.users_service
    if wrong_target == "local-user":
        other, _token = service.invite(email="reader@example.test", role="reader")
        wrong_id = other.id
    elif wrong_target == "foreign-user":
        with engine.begin() as connection:
            connection.execute(schema.organizations.insert(), {"id": "foreign", "name": "Foreign"})
        foreign = UsersService(engine, org_id="foreign", pepper=client.app.state.token_pepper)
        other, _token = foreign.create_first_admin(email="foreign@example.test")
        wrong_id = other.id
    else:
        wrong_id = "unknown-user"
    rejected = client.post(
        f"/v2/users/{wrong_id}/activate",
        json={"activation_token": pending["activation_token"], "password": "synthetic-password"},
        headers={"Idempotency-Key": "wrong-path"},
    )
    assert rejected.status_code == 404
    _unchanged_pending(engine, pending["id"])
    if wrong_target != "unknown-user":
        _unchanged_pending(engine, wrong_id)
    route = f"/v2/users/{pending['id']}/activate" if retry_route == "by-id" else "/v2/users/activate"
    accepted = client.post(
        route, json={"activation_token": pending["activation_token"], "password": "synthetic-password"},
        headers={"Idempotency-Key": "correct-path"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["after"]["id"] == pending["id"]
    assert accepted.json()["after"]["activated_at"] is not None


def test_expected_user_is_checked_before_password_hashing(native_app, monkeypatch):
    from quadringent_control_plane.v2.services import users

    client, engine = native_app
    pending = _pending(client)

    def forbidden_hash(_password):
        pytest.fail("Le mauvais identifiant doit être refusé avant le hash du mot de passe")

    monkeypatch.setattr(users, "hash_password", forbidden_hash)
    with pytest.raises(ActivationTokenInvalidError):
        client.app.state.users_service.activate(
            activation_token=pending["activation_token"], password="synthetic-password", expected_user_id="wrong-user",
        )
    _unchanged_pending(engine, pending["id"])


def test_foreign_token_cannot_activate_local_target_or_be_consumed(native_app):
    client, engine = native_app
    local = _pending(client)
    with engine.begin() as connection:
        connection.execute(schema.organizations.insert(), {"id": "foreign", "name": "Foreign"})
    foreign = UsersService(engine, org_id="foreign", pepper=client.app.state.token_pepper)
    other, foreign_token = foreign.create_first_admin(email="foreign@example.test")
    rejected = client.post(
        f"/v2/users/{local['id']}/activate",
        json={"activation_token": foreign_token, "password": "synthetic-password"},
        headers={"Idempotency-Key": "foreign-token"},
    )
    assert rejected.status_code == 400
    _unchanged_pending(engine, local["id"])
    _unchanged_pending(engine, other.id)
