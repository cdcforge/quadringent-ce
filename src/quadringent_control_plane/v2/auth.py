"""Dépendance FastAPI de scope (read/operate/admin) — trois voies d'entrée.

Trois façons d'être identifié sur ``/v2``, essayées dans cet ordre :

1. **Jeton d'agent** (tâche 8) : en-tête ``Authorization: Bearer qdt_...``.
   Résolu via ``AgentTokensService.authenticate`` — jamais de log du jeton en
   clair. Produit une identité ``actor_kind="agent"``.
2. **Session utilisateur** (tâche 9) : cookie de session (``HttpOnly``) posé
   après connexion par mot de passe — résolu via ``UsersService`` si
   déclaré sur l'application. Produit ``actor_kind="human"``.
3. **Mode proxy de confiance** (v1, ``auth.py::AuthConfig``) : en-têtes posés
   par un reverse-proxy d'authentification (oauth2-proxy, IAP...). Conservé
   pour compatibilité (contrat §6.2 : « reste utilisable en secours »).

Sans aucune de ces trois configurations, l'identité est un administrateur
implicite (mode développement/loopback historique, même posture que
``server.py`` v1) — l'accès réseau est alors la seule barrière.

**Mode « authentification exigée »** (``app.state.require_authentication``,
posé par ``entrypoint.build_app`` en production — tâche « auth-login ») :
désactive ce secours anonyme-admin pour toute route qui résout une
identité. Les routes qui n'en résolvent aucune restent accessibles sans
identité (``/v2/healthz``, ``/v2/openapi.json``/``/docs``/``/redoc``,
``/v2/setup/first-admin``, ``/v2/users/activate``,
``/v2/users/{id}/activate``, ``/v2/auth/login``, ``/v2/auth/logout``) —
voir ``docs/api-v2.md``. Défaut ``False`` : inchangé pour les tests/usages
existants qui dépendent de l'identité anonyme implicite.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from ..auth import AuthConfig, ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER, authenticate as authenticate_proxy
from .errors import ApiError, insufficient_role, invalid_credentials, wrong_environment

_SCOPE_RANK = {"read": 0, "operate": 1, "admin": 2}
_ROLE_TO_SCOPE_RANK = {ROLE_VIEWER: 0, ROLE_OPERATOR: 1, ROLE_ADMIN: 2}
_USER_ROLE_TO_SCOPE_RANK = {"reader": 0, "admin": 2}


@dataclass(frozen=True)
class Identity:
    """Identité résolue pour la requête courante — utilisée par l'audit (tâche 11)."""

    subject: str
    role: str
    actor_kind: str = "human"  # human|agent
    actor_id: str = ""
    actor_display: str = ""
    source_restriction: tuple[str, ...] = ()
    pre_authorized_actions: tuple[str, ...] = ()
    groups: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.actor_id:
            object.__setattr__(self, "actor_id", self.subject)
        if not self.actor_display:
            object.__setattr__(self, "actor_display", self.subject)

    def source_allowed(self, source_id: str | None) -> bool:
        if not self.source_restriction or source_id is None:
            return True
        return source_id in self.source_restriction


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header[len("bearer ") :].strip()
    return token or None


def _identity_from_agent_token(request: Request, token: str) -> Identity | None:
    tokens_service = getattr(request.app.state, "agent_tokens_service", None)
    if tokens_service is None or not token.startswith("qdt_"):
        return None
    from .services.agent_tokens import AgentTokenInvalidError

    try:
        record = tokens_service.authenticate(token)
    except AgentTokenInvalidError as error:
        raise invalid_credentials("jeton d'agent invalide, révoqué ou expiré") from error
    return Identity(
        subject=f"agent:{record.id}",
        role=record.scope,
        actor_kind="agent",
        actor_id=record.id,
        actor_display=record.name,
        source_restriction=record.source_restriction,
        pre_authorized_actions=record.pre_authorized_actions,
    )


def _identity_from_session(request: Request) -> Identity | None:
    users_service = getattr(request.app.state, "users_service", None)
    session_cookie_name = getattr(request.app.state, "session_cookie_name", "quadringent_session")
    if users_service is None:
        return None
    session_value = request.cookies.get(session_cookie_name)
    if not session_value:
        return None
    from .services.users import SessionInvalidError

    try:
        user = users_service.resolve_session(session_value)
    except SessionInvalidError:
        return None
    return Identity(
        subject=f"user:{user.id}",
        role=user.role,
        actor_kind="human",
        actor_id=user.id,
        actor_display=user.email,
    )


def _identity_from_proxy(request: Request) -> Identity | None:
    config: AuthConfig | None = getattr(request.app.state, "auth_config", None)
    if config is None:
        return None
    identity = authenticate_proxy(request.headers, config)
    if identity is None:
        return None
    return Identity(
        subject=identity.subject,
        role=identity.role,
        actor_kind="human",
        actor_id=identity.subject,
        actor_display=identity.subject,
        groups=identity.groups,
    )


def resolve_identity(request: Request) -> Identity:
    """Résout l'identité de la requête (jeton d'agent > session > proxy > anonyme)."""

    token = _bearer_token(request)
    if token is not None:
        identity = _identity_from_agent_token(request, token)
        if identity is not None:
            return identity

    identity = _identity_from_session(request)
    if identity is not None:
        return identity

    identity = _identity_from_proxy(request)
    if identity is not None:
        return identity

    config: AuthConfig | None = getattr(request.app.state, "auth_config", None)
    require_authentication = bool(getattr(request.app.state, "require_authentication", False))
    if config is None and not require_authentication:
        # Mode développement/loopback historique (même posture que
        # ``server.py`` v1) : sans AuthConfig déclarée ni authentification
        # exigée, l'accès réseau est la seule barrière. Un jeton d'agent ou
        # une session présentés mais invalides ont déjà levé une erreur plus
        # haut ; ici, aucune identité n'a été présentée du tout.
        return Identity(subject="anonymous", role=ROLE_ADMIN, actor_kind="human")

    raise ApiError(
        401,
        "invalid_request",
        "authentification requise",
        next_action="fournir une identité via jeton, session ou proxy déclaré",
        retryable=False,
    )


def _scope_rank(identity: Identity) -> int:
    if identity.actor_kind == "agent":
        return _SCOPE_RANK.get(identity.role, -1)
    if identity.role in _USER_ROLE_TO_SCOPE_RANK:
        return _USER_ROLE_TO_SCOPE_RANK[identity.role]
    return _ROLE_TO_SCOPE_RANK.get(identity.role, -1)


def require_scope(scope: str):
    """Fabrique une dépendance FastAPI exigeant au moins ``scope``."""

    if scope not in _SCOPE_RANK:
        raise ValueError(f"scope inconnu : {scope!r}")

    def dependency(request: Request) -> Identity:
        identity = resolve_identity(request)
        if _scope_rank(identity) < _SCOPE_RANK[scope]:
            raise insufficient_role(f"scope {scope!r} requis")
        return identity

    return dependency


def require_source_scope(scope: str):
    """Comme ``require_scope``, en vérifiant en plus la restriction de source du jeton."""

    scope_dependency = require_scope(scope)

    def dependency(source_id: str, request: Request) -> Identity:
        identity = scope_dependency(request)
        if not identity.source_allowed(source_id):
            raise wrong_environment("cette source est hors du périmètre déclaré du jeton")
        return identity

    return dependency
