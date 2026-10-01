"""Tâche 9 (routes) — premier admin, activation, connexion par cookie de session, reader en lecture seule."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'users_routes.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()), token_pepper=b"pepper-test")
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_first_admin_setup_then_activation_then_login(client) -> None:
    created = client.post(
        "/v2/setup/first-admin",
        json={"email": "admin@example.com"},
        headers={"Idempotency-Key": "setup-1"},
    )
    assert created.status_code == 201
    body = created.json()["after"]
    user_id = body["id"]
    activation_token = body["activation_token"]

    activated = client.post(
        f"/v2/users/{user_id}/activate",
        json={"activation_token": activation_token, "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-1"},
    )
    assert activated.status_code == 200
    assert activated.json()["after"]["activated_at"] is not None

    login = client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"})
    assert login.status_code == 200
    assert "quadringent_session" in login.cookies

    # Le cookie de session authentifie maintenant les routes protégées.
    listed = client.get("/v2/users")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1


def test_second_first_admin_setup_is_refused(client) -> None:
    created = client.post(
        "/v2/setup/first-admin", json={"email": "admin@example.com"}, headers={"Idempotency-Key": "setup-2"}
    )
    body = created.json()["after"]
    client.post(
        f"/v2/users/{body['id']}/activate",
        json={"activation_token": body["activation_token"], "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-2"},
    )
    second = client.post(
        "/v2/setup/first-admin", json={"email": "autre@example.com"}, headers={"Idempotency-Key": "setup-3"}
    )
    assert second.status_code == 409


def test_login_with_wrong_password_is_refused(client) -> None:
    created = client.post(
        "/v2/setup/first-admin", json={"email": "admin@example.com"}, headers={"Idempotency-Key": "setup-4"}
    )
    body = created.json()["after"]
    client.post(
        f"/v2/users/{body['id']}/activate",
        json={"activation_token": body["activation_token"], "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-4"},
    )
    login = client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "faux-mot-de-passe"})
    assert login.status_code == 401


def test_reader_cannot_invite_users(client) -> None:
    created = client.post(
        "/v2/setup/first-admin", json={"email": "admin@example.com"}, headers={"Idempotency-Key": "setup-5"}
    )
    admin_body = created.json()["after"]
    client.post(
        f"/v2/users/{admin_body['id']}/activate",
        json={"activation_token": admin_body["activation_token"], "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-5"},
    )
    client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"})

    invited = client.post(
        "/v2/users",
        json={"email": "lecteur@example.com", "role": "reader"},
        headers={"Idempotency-Key": "invite-1"},
    )
    assert invited.status_code == 201
    reader_body = invited.json()["after"]
    reader_activation = client.post(
        f"/v2/users/{reader_body['id']}/activate",
        json={"activation_token": reader_body["activation_token"], "password": "mot-de-passe-lecteur"},
        headers={"Idempotency-Key": "activate-reader"},
    )
    assert reader_activation.status_code == 200

    reader_client = TestClient(client.app)
    login = reader_client.post(
        "/v2/auth/login", json={"email": "lecteur@example.com", "password": "mot-de-passe-lecteur"}
    )
    assert login.status_code == 200

    attempt = reader_client.post(
        "/v2/users",
        json={"email": "autre@example.com", "role": "reader"},
        headers={"Idempotency-Key": "invite-2"},
    )
    assert attempt.status_code == 403
    assert attempt.json()["error"]["code"] == "insufficient_role"

    # Un reader garde le scope "read" — la liste des utilisateurs exige
    # "admin" dans ce chantier, donc un reader ne peut pas la lire non plus.
    read_attempt = reader_client.get("/v2/users")
    assert read_attempt.status_code == 403


def test_activation_by_token_alone_matches_the_installer_link(client) -> None:
    """Le lien de l'installeur ne porte que le jeton (``/activate?token=…``) :
    l'UI appelle ``POST /v2/users/activate`` avec ``{token, password}``."""
    created = client.post(
        "/v2/setup/first-admin", json={"email": "admin@example.com"}, headers={"Idempotency-Key": "setup-t1"}
    )
    token = created.json()["after"]["activation_token"]
    activated = client.post(
        "/v2/users/activate",
        json={"token": token, "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-t1"},
    )
    assert activated.status_code == 200
    assert activated.json()["after"]["email"] == "admin@example.com"
    assert activated.json()["after"]["activated_at"] is not None
    login = client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"})
    assert login.status_code == 200


def test_activation_by_token_refuses_reuse_and_missing_fields(client) -> None:
    created = client.post(
        "/v2/setup/first-admin", json={"email": "admin@example.com"}, headers={"Idempotency-Key": "setup-t2"}
    )
    token = created.json()["after"]["activation_token"]
    body = {"token": token, "password": "un-mot-de-passe-robuste"}
    assert client.post("/v2/users/activate", json=body, headers={"Idempotency-Key": "a-1"}).status_code == 200
    assert client.post("/v2/users/activate", json=body, headers={"Idempotency-Key": "a-2"}).status_code == 400
    missing = client.post("/v2/users/activate", json={"password": "x"}, headers={"Idempotency-Key": "a-3"})
    assert missing.status_code == 400
