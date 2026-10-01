"""OIDC (Authorization Code + PKCE) — optionnel, désactivé par défaut (tâche 10, contrat §6.2).

Off par défaut : sans ``OidcConfig`` déclarée sur ``create_v2_app``, aucune
route ``/v2/auth/oidc/*`` n'est montée — la connexion par mot de passe
(``services/users.py``) reste le seul mode d'identité humaine, comme
avant ce chantier. Quand elle est déclarée, l'identifiant applicatif
(``session_token``) posé après un callback réussi est **exactement** celui
de la connexion par mot de passe (``UsersService.create_session_token`` —
même cookie, même TTL, même signature) : OIDC n'est qu'une autre façon
d'obtenir un ``UserRecord`` authentifié, jamais un second mécanisme de
session.

Pas de table « flux en cours » : l'état PKCE (``state``, ``code_verifier``,
``nonce``) est porté par un cookie ``HttpOnly`` signé (HMAC, même pepper
que les sessions) posé à ``/v2/auth/oidc/login`` et vérifié au callback —
cohérent avec le choix déjà fait pour les sessions utilisateur (« pas
d'état serveur », cf. ``services/users.py``).

Le client de découverte/échange (``OidcHttpClient``) est injecté — jamais
de réseau réel dans les tests (même discipline que
``services/webhooks.py::WebhookHttpClient``, ``services/tables.py``
``TableDiscoveryClientProtocol``) ; les tests câblent un faux fournisseur
d'identité (« fake IdP ») qui signe ses jetons avec une clé RSA de test.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Protocol

import jwt

from .crypto import sign_payload, verify_signature


class OidcError(ValueError):
    """Échec de découverte, d'échange de code, ou de vérification du jeton — fail-closed."""


@dataclass(frozen=True)
class OidcConfig:
    """Configuration optionnelle — absente par défaut (``create_v2_app(oidc_config=None)``)."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email")
    state_ttl_seconds: int = 600


class OidcHttpClient(Protocol):
    """Contrat minimal du client réseau injecté — jamais de réseau réel en test."""

    def get_json(self, url: str) -> dict[str, object]: ...

    def post_form(self, url: str, *, data: dict[str, str]) -> dict[str, object]: ...


@dataclass(frozen=True)
class OidcClaims:
    subject: str
    email: str | None


class OidcService:
    """Découverte, construction de l'URL d'autorisation, échange du code, vérification du jeton."""

    def __init__(self, config: OidcConfig, *, http_client: OidcHttpClient, pepper: bytes) -> None:
        self._config = config
        self._http = http_client
        self._pepper = pepper
        self._discovery_cache: dict[str, object] | None = None

    def _discovery(self) -> dict[str, object]:
        if self._discovery_cache is None:
            url = f"{self._config.issuer.rstrip('/')}/.well-known/openid-configuration"
            self._discovery_cache = self._http.get_json(url)
        return self._discovery_cache

    def build_authorization_request(self) -> tuple[str, str]:
        """Construit l'URL d'autorisation et le cookie d'état signé — ``(url, state_cookie_value)``."""

        discovery = self._discovery()
        authorization_endpoint = discovery.get("authorization_endpoint")
        if not isinstance(authorization_endpoint, str):
            raise OidcError("document de découverte OIDC sans authorization_endpoint")

        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(48)
        code_challenge = _code_challenge_s256(code_verifier)

        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": self._config.redirect_uri,
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        query = "&".join(f"{key}={_url_quote(value)}" for key, value in params.items())
        url = f"{authorization_endpoint}?{query}"

        expires_at = int(time.time()) + self._config.state_ttl_seconds
        state_cookie = _pack_state_cookie(
            state=state, nonce=nonce, code_verifier=code_verifier, expires_at=expires_at, pepper=self._pepper
        )
        return url, state_cookie

    def complete_callback(self, *, code: str, state: str, state_cookie_value: str | None) -> OidcClaims:
        """Vérifie l'état PKCE, échange le code, vérifie le jeton d'identité — fail-closed à chaque étape."""

        if not state_cookie_value:
            raise OidcError("cookie d'état OIDC absent — flux non initié par ce serveur, ou expiré")
        packed = _unpack_state_cookie(state_cookie_value, pepper=self._pepper)
        if packed is None:
            raise OidcError("cookie d'état OIDC invalide ou signature incorrecte")
        if packed["expires_at"] < int(time.time()):
            raise OidcError("flux OIDC expiré — relancer la connexion")
        if not secrets.compare_digest(packed["state"], state):
            raise OidcError("paramètre state OIDC ne correspond pas au cookie — rejeu ou CSRF potentiel")

        discovery = self._discovery()
        token_endpoint = discovery.get("token_endpoint")
        if not isinstance(token_endpoint, str):
            raise OidcError("document de découverte OIDC sans token_endpoint")

        token_response = self._http.post_form(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._config.redirect_uri,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "code_verifier": packed["code_verifier"],
            },
        )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str):
            raise OidcError("réponse du fournisseur d'identité sans id_token")

        claims = self._verify_id_token(id_token, expected_nonce=packed["nonce"])
        return claims

    def _verify_id_token(self, id_token: str, *, expected_nonce: str) -> OidcClaims:
        discovery = self._discovery()
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise OidcError("document de découverte OIDC sans jwks_uri")
        jwks = self._http.get_json(jwks_uri)
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not keys:
            raise OidcError("jeu de clés (JWKS) du fournisseur d'identité vide ou absent")

        header = jwt.get_unverified_header(id_token)
        key_id = header.get("kid")
        matching = [key for key in keys if key.get("kid") == key_id] or keys
        try:
            public_key = jwt.PyJWK.from_dict(matching[0]).key
            claims = jwt.decode(
                id_token,
                key=public_key,
                algorithms=["RS256"],
                audience=self._config.client_id,
                issuer=self._config.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise OidcError(f"id_token invalide : {error}") from error

        if claims.get("nonce") != expected_nonce:
            raise OidcError("nonce du id_token ne correspond pas au flux — rejeu potentiel")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OidcError("id_token sans sub")
        email = claims.get("email")
        return OidcClaims(subject=subject, email=email if isinstance(email, str) else None)


def _code_challenge_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _url_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _pack_state_cookie(*, state: str, nonce: str, code_verifier: str, expires_at: int, pepper: bytes) -> str:
    payload = json.dumps(
        {"state": state, "nonce": nonce, "code_verifier": code_verifier, "expires_at": expires_at},
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    signature = sign_payload(encoded, pepper)
    return f"{encoded}.{signature}"


def _unpack_state_cookie(value: str, *, pepper: bytes) -> dict[str, object] | None:
    parts = value.split(".")
    if len(parts) != 2:
        return None
    encoded, signature = parts
    if not verify_signature(encoded, signature, pepper):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload
