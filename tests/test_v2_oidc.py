"""OIDC (Authorization Code + PKCE), optionnel — off par défaut (tâche 10, contrat §6.2).

Un faux fournisseur d'identité (``_FakeIdp``) signe ses jetons avec une clé
RSA de test (jamais de réseau réel — ``OidcHttpClient`` est un protocole
injecté, même discipline que ``services/webhooks.py``). Couvre : absence
de routes sans ``oidc_config``, flux complet réussi (création d'un nouvel
utilisateur, cookie de session identique à la connexion par mot de passe),
liaison d'un utilisateur existant par email, rejet nonce/state/signature.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt as pyjwt
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.oidc import OidcConfig

_ISSUER = "https://idp.example.test"
_CLIENT_ID = "quadringent-test-client"
_CLIENT_SECRET = "test-client-secret"
_REDIRECT_URI = "https://control-plane.example.test/v2/auth/oidc/callback"


class _FakeIdp:
    """Sert découverte OIDC, échange de code et JWKS — sans aucun réseau réel."""

    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key_id = "test-key-1"
        self.issued_codes: dict[str, dict[str, str]] = {}

    def issue_authorization_code(self, *, subject: str, email: str | None, nonce: str) -> str:
        code = f"code-{subject}-{len(self.issued_codes)}"
        self.issued_codes[code] = {"subject": subject, "email": email, "nonce": nonce}
        return code

    def get_json(self, url: str) -> dict[str, object]:
        if url == f"{_ISSUER}/.well-known/openid-configuration":
            return {
                "authorization_endpoint": f"{_ISSUER}/authorize",
                "token_endpoint": f"{_ISSUER}/token",
                "jwks_uri": f"{_ISSUER}/jwks",
            }
        if url == f"{_ISSUER}/jwks":
            jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key()))
            jwk["kid"] = self.key_id
            jwk["alg"] = "RS256"
            jwk["use"] = "sig"
            return {"keys": [jwk]}
        raise AssertionError(f"URL GET inattendue en test : {url}")

    def post_form(self, url: str, *, data: dict[str, str]) -> dict[str, object]:
        assert url == f"{_ISSUER}/token"
        assert data["client_id"] == _CLIENT_ID
        assert data["client_secret"] == _CLIENT_SECRET
        code = data["code"]
        record = self.issued_codes.get(code)
        if record is None:
            return {"error": "invalid_grant"}
        now = int(time.time())
        claims = {
            "iss": _ISSUER,
            "aud": _CLIENT_ID,
            "sub": record["subject"],
            "iat": now,
            "exp": now + 300,
            "nonce": record["nonce"],
        }
        if record["email"]:
            claims["email"] = record["email"]
        id_token = pyjwt.encode(
            claims, self._private_key, algorithm="RS256", headers={"kid": self.key_id}
        )
        return {"access_token": "unused-in-this-chantier", "id_token": id_token, "token_type": "Bearer"}


@pytest.fixture()
def idp() -> _FakeIdp:
    return _FakeIdp()


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'oidc.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    try:
        yield engine
    finally:
        engine.dispose()


def _oidc_config() -> OidcConfig:
    return OidcConfig(
        issuer=_ISSUER, client_id=_CLIENT_ID, client_secret=_CLIENT_SECRET, redirect_uri=_REDIRECT_URI
    )


def test_oidc_routes_absent_without_config(engine) -> None:
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()), enable_mcp=False)
    with TestClient(app) as client:
        response = client.get("/v2/auth/oidc/login", follow_redirects=False)
    assert response.status_code == 404


def test_oidc_login_then_callback_creates_user_and_session(engine, idp) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        enable_mcp=False,
        oidc_config=_oidc_config(),
        oidc_http_client=idp,
    )
    with TestClient(app) as client:
        login_response = client.get("/v2/auth/oidc/login", follow_redirects=False)
        assert login_response.status_code == 302
        authorize_url = login_response.headers["location"]
        assert authorize_url.startswith(f"{_ISSUER}/authorize?")
        state = dict(part.split("=", 1) for part in authorize_url.split("?", 1)[1].split("&"))["state"]
        nonce = dict(part.split("=", 1) for part in authorize_url.split("?", 1)[1].split("&"))["nonce"]
        state_cookie = login_response.cookies.get("quadringent_oidc_state")
        assert state_cookie

        code = idp.issue_authorization_code(subject="idp-sub-1", email="agent.humain@example.test", nonce=nonce)
        client.cookies.set("quadringent_oidc_state", state_cookie)
        callback_response = client.get("/v2/auth/oidc/callback", params={"code": code, "state": state})

    assert callback_response.status_code == 200
    body = callback_response.json()
    assert body["user"]["email"] == "agent.humain@example.test"
    assert body["user"]["role"] == "reader"
    assert "quadringent_session" in callback_response.cookies

    with engine.connect() as connection:
        row = connection.execute(
            v2_schema.users.select().where(v2_schema.users.c.email == "agent.humain@example.test")
        ).mappings().one()
    assert row["oidc_subject"] == "idp-sub-1"
    assert row["activated_at"] is not None


def test_oidc_callback_links_existing_user_by_email(engine, idp) -> None:
    from quadringent_control_plane.v2.services.users import UsersService

    users_service = UsersService(engine, org_id="default", pepper=b"0" * 32)
    invited, _activation_token = users_service.invite(email="deja.invite@example.test", role="admin")

    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        enable_mcp=False,
        oidc_config=_oidc_config(),
        oidc_http_client=idp,
        token_pepper=b"0" * 32,
    )
    with TestClient(app) as client:
        login_response = client.get("/v2/auth/oidc/login", follow_redirects=False)
        authorize_url = login_response.headers["location"]
        params = dict(part.split("=", 1) for part in authorize_url.split("?", 1)[1].split("&"))
        code = idp.issue_authorization_code(
            subject="idp-sub-existing", email="deja.invite@example.test", nonce=params["nonce"]
        )
        client.cookies.set("quadringent_oidc_state", login_response.cookies.get("quadringent_oidc_state"))
        callback_response = client.get(
            "/v2/auth/oidc/callback", params={"code": code, "state": params["state"]}
        )

    assert callback_response.status_code == 200
    assert callback_response.json()["user"]["id"] == invited.id
    assert callback_response.json()["user"]["role"] == "admin"  # rôle d'invitation préservé, pas écrasé par OIDC


def test_oidc_callback_rejects_state_mismatch(engine, idp) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        enable_mcp=False,
        oidc_config=_oidc_config(),
        oidc_http_client=idp,
    )
    with TestClient(app) as client:
        login_response = client.get("/v2/auth/oidc/login", follow_redirects=False)
        authorize_url = login_response.headers["location"]
        params = dict(part.split("=", 1) for part in authorize_url.split("?", 1)[1].split("&"))
        code = idp.issue_authorization_code(subject="idp-sub-2", email="x@example.test", nonce=params["nonce"])
        client.cookies.set("quadringent_oidc_state", login_response.cookies.get("quadringent_oidc_state"))
        response = client.get("/v2/auth/oidc/callback", params={"code": code, "state": "wrong-state-value"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_oidc_callback_rejects_missing_state_cookie(engine, idp) -> None:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        enable_mcp=False,
        oidc_config=_oidc_config(),
        oidc_http_client=idp,
    )
    with TestClient(app) as client:
        response = client.get("/v2/auth/oidc/callback", params={"code": "whatever", "state": "whatever"})
    assert response.status_code == 400
