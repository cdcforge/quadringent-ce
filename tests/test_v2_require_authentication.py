"""Tâche « auth-login » — mode « authentification exigée » de ``create_v2_app``.

En production (``entrypoint.build_app``), l'identité anonyme implicite
(administrateur, cf. ``auth.py::resolve_identity``) ne doit plus jamais être
accordée : ``require_authentication=True`` désactive ce secours pour toute
route qui résout une identité (``require_scope``/``require_source_scope``),
sans toucher aux routes publiques qui ne résolvent aucune identité
(``/v2/healthz``, ``/v2/openapi.json``, ``/v2/setup/first-admin``,
``/v2/users/activate``, ``/v2/users/{id}/activate``, ``/v2/auth/login``,
``/v2/auth/logout``). Le comportement par défaut (``require_authentication=
False``) reste inchangé pour ne pas casser les tests existants qui en
dépendent (mode développement/loopback historique)."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'require_auth.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    try:
        yield engine
    finally:
        engine.dispose()


def _client(engine, *, require_authentication: bool) -> TestClient:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        token_pepper=b"pepper-test",
        require_authentication=require_authentication,
    )
    return TestClient(app)


def test_default_mode_still_falls_back_to_anonymous_admin(engine) -> None:
    client = _client(engine, require_authentication=False)
    response = client.get("/v2/users")
    assert response.status_code == 200


def test_required_mode_refuses_anonymous_access_to_protected_routes(engine) -> None:
    client = _client(engine, require_authentication=True)
    response = client.get("/v2/users")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["retryable"] is False


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("get", "/v2/healthz", None),
        ("get", "/v2/openapi.json", None),
    ],
)
def test_required_mode_leaves_health_and_schema_public(engine, method, path, json_body) -> None:
    client = _client(engine, require_authentication=True)
    response = getattr(client, method)(path, json=json_body) if json_body is not None else getattr(client, method)(path)
    assert response.status_code == 200


def test_required_mode_leaves_setup_and_activation_public(engine) -> None:
    client = _client(engine, require_authentication=True)
    created = client.post(
        "/v2/setup/first-admin",
        json={"email": "admin@example.com"},
        headers={"Idempotency-Key": "setup-req-1"},
    )
    assert created.status_code == 201
    body = created.json()["after"]

    activated = client.post(
        "/v2/users/activate",
        json={"token": body["activation_token"], "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-req-1"},
    )
    assert activated.status_code == 200

    login = client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"})
    assert login.status_code == 200

    logout = client.post("/v2/auth/logout")
    assert logout.status_code == 200


def test_required_mode_allows_access_after_login(engine) -> None:
    client = _client(engine, require_authentication=True)
    created = client.post(
        "/v2/setup/first-admin",
        json={"email": "admin@example.com"},
        headers={"Idempotency-Key": "setup-req-2"},
    )
    body = created.json()["after"]
    client.post(
        "/v2/users/activate",
        json={"token": body["activation_token"], "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-req-2"},
    )
    login = client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"})
    assert login.status_code == 200

    listed = client.get("/v2/users")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1


def test_required_mode_still_accepts_a_valid_agent_token(engine) -> None:
    from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService

    tokens_service = AgentTokensService(engine, org_id="default", pepper=b"pepper-test")
    _, token_value = tokens_service.create(name="agent-ci", scope="read", created_by="admin", never_expires=True)
    client = _client(engine, require_authentication=True)
    response = client.get("/v2/sources", headers={"Authorization": f"Bearer {token_value}"})
    assert response.status_code == 200


def test_get_auth_me_requires_a_session_in_required_mode(engine) -> None:
    client = _client(engine, require_authentication=True)
    anonymous = client.get("/v2/auth/me")
    assert anonymous.status_code == 401


def test_get_auth_me_reports_the_current_session(engine) -> None:
    client = _client(engine, require_authentication=True)
    created = client.post(
        "/v2/setup/first-admin",
        json={"email": "admin@example.com"},
        headers={"Idempotency-Key": "setup-req-3"},
    )
    body = created.json()["after"]
    client.post(
        "/v2/users/activate",
        json={"token": body["activation_token"], "password": "un-mot-de-passe-robuste"},
        headers={"Idempotency-Key": "activate-req-3"},
    )
    client.post("/v2/auth/login", json={"email": "admin@example.com", "password": "un-mot-de-passe-robuste"})

    me = client.get("/v2/auth/me")
    assert me.status_code == 200
    payload = me.json()
    assert payload["email"] == "admin@example.com"
    assert payload["role"] == "admin"


def test_get_auth_me_works_in_default_anonymous_mode_too(engine) -> None:
    client = _client(engine, require_authentication=False)
    me = client.get("/v2/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "admin"
