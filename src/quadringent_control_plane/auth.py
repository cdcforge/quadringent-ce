"""Authentification déléguée au fournisseur d'identité du site.

Quadringent ne gère ni comptes ni mots de passe : l'identité est produite
par l'IdP du client (Okta, Entra ID, Keycloak, Google) et véhiculée par un
proxy d'authentification placé devant le control plane (oauth2-proxy,
forward-auth d'Ingress, IAP). Ce module lit les en-têtes de confiance que
seul ce proxy peut poser et en déduit un rôle :

- ``viewer``   : tout utilisateur authentifié, lecture seule ;
- ``operator`` : membres des groupes opérateurs — peut lancer les actions ;
- ``admin``    : membres des groupes admin — mêmes actions aujourd'hui,
  réservé aux futurs réglages sensibles.

Les membres se gèrent dans l'IdP du site : ajouter un opérateur revient à
l'ajouter au groupe déclaré, sans toucher Quadringent. Sans en-tête posé
par le proxy, la requête est refusée — fail-closed.

Ce mode reste optionnel : sans ``AuthConfig``, le control plane garde son
modèle loopback historique (accès = port-forward Kubernetes).
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
from http import HTTPStatus
from typing import Mapping


ROLE_VIEWER = "viewer"
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"

_SAFE_HEADER_NAME = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

# En-tête de transit : quand le site déclare un secret partagé avec son proxy,
# seul le proxy peut le poser — un accès direct au pod ne forge plus aucun
# rôle, même en connaissant les en-têtes d'identité.
PROXY_SECRET_HEADER = "x-quadringent-proxy-secret"


@dataclass(frozen=True)
class Identity:
    subject: str
    groups: frozenset[str]
    role: str


@dataclass(frozen=True)
class AuthConfig:
    user_header: str
    groups_header: str
    operator_groups: frozenset[str]
    admin_groups: frozenset[str]
    proxy_secret: str | None = None

    @classmethod
    def build(
        cls,
        *,
        user_header: str = "",
        groups_header: str = "",
        operator_groups: str = "",
        admin_groups: str = "",
        proxy_secret: str = "",
    ) -> "AuthConfig | None":
        """Construit la configuration ou ``None`` quand l'auth est inactive."""

        user_header = (user_header or "").strip().lower()
        if not user_header:
            return None
        if not _header_name_ok(user_header):
            raise ValueError("--auth-user-header doit être un nom d'en-tête valide")
        groups_header = (groups_header or "").strip().lower()
        if not _header_name_ok(groups_header):
            raise ValueError("--auth-groups-header doit être un nom d'en-tête valide")
        operators = frozenset(_split_groups(operator_groups))
        admins = frozenset(_split_groups(admin_groups))
        if not operators and not admins:
            raise ValueError("l'auth exige --auth-operator-groups ou --auth-admin-groups")
        secret = (proxy_secret or "").strip()
        return cls(
            user_header=user_header,
            groups_header=groups_header,
            operator_groups=operators,
            admin_groups=admins,
            proxy_secret=secret or None,
        )


def _header_name_ok(name: str) -> bool:
    return bool(name) and all(character in _SAFE_HEADER_NAME for character in name)


def _split_groups(raw: str) -> list[str]:
    return [group.strip() for group in (raw or "").split(",") if group.strip()]


def _header_values(headers: object, name: str) -> list[str]:
    """Toutes les occurrences d'un en-tête, quel que soit le conteneur."""

    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        return [str(value) for value in get_all(name, [])]
    if isinstance(headers, Mapping):
        return [str(value) for key, value in headers.items() if key.lower() == name]
    return []


def _header(headers: Mapping[str, str], name: str) -> str:
    values = _header_values(headers, name)
    return values[0] if values else ""


def authenticate(headers: object, config: AuthConfig) -> Identity | None:
    """Identité depuis les en-têtes de confiance ; ``None`` = non authentifié.

    Un en-tête d'identité dupliqué rend la requête ambiguë : elle est refusée
    plutôt que d'arbitrer entre ses valeurs.
    """

    if len(_header_values(headers, config.user_header)) > 1:
        return None
    if len(_header_values(headers, config.groups_header)) > 1:
        return None
    if config.proxy_secret is not None:
        presented = _header_values(headers, PROXY_SECRET_HEADER)
        if len(presented) != 1 or not hmac.compare_digest(
            presented[0].encode("utf-8"), config.proxy_secret.encode("utf-8")
        ):
            return None
    subject = _header(headers, config.user_header).strip()
    if not subject or len(subject) > 256 or any(ord(c) < 0x21 for c in subject):
        return None
    groups = frozenset(_split_groups(_header(headers, config.groups_header)))
    if groups & config.admin_groups:
        role = ROLE_ADMIN
    elif groups & config.operator_groups:
        role = ROLE_OPERATOR
    else:
        role = ROLE_VIEWER
    return Identity(subject=subject, groups=groups, role=role)


_ROLE_RANK = {ROLE_VIEWER: 0, ROLE_OPERATOR: 1, ROLE_ADMIN: 2}


def required_role(method: str) -> str:
    """Toute écriture exige un opérateur ; la lecture suffit à un viewer."""

    return ROLE_VIEWER if method in {"GET", "HEAD", "OPTIONS"} else ROLE_OPERATOR


def authorize(identity: Identity, method: str) -> tuple[HTTPStatus, str] | None:
    """Refus structuré quand le rôle est insuffisant, ``None`` sinon."""

    needed = required_role(method)
    if _ROLE_RANK[identity.role] < _ROLE_RANK[needed]:
        return HTTPStatus.FORBIDDEN, "insufficient_role"
    return None
