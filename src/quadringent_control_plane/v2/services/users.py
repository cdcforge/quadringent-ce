"""Service Users/roles/activation (tâche 9, contrat §6.1).

Rôles ``admin``/``reader`` (le rôle ``reader`` ne peut jamais écrire — voir
``v2/auth.py::_USER_ROLE_TO_SCOPE_RANK``). Le premier admin s'active via un
lien à usage unique (``activation_tokens``, hachage sha256+pepper — même
primitive que les jetons d'agent, TTL 24 h) ; les admins suivants créent
d'autres utilisateurs avec le même mécanisme d'activation. Le mot de passe
est haché avec scrypt (``crypto.hash_password``) ; la session UI est un
cookie signé (HMAC, sans état serveur — pas de table ``sessions`` dans ce
chantier, TTL court).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import time
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import (
    hash_agent_token as _hash_opaque_token,
    hash_password,
    sign_payload,
    verify_password,
    verify_signature,
)

ROLES = ("admin", "reader")
ACTIVATION_TTL = timedelta(hours=24)
SESSION_TTL = timedelta(hours=12)


class UserValidationError(ValueError):
    """Corps hors contrat (email/role invalide)."""


class UserNotFoundError(LookupError):
    """Aucun utilisateur pour cet identifiant/email — 404 ``not_found``."""


class AdminAlreadyExistsError(RuntimeError):
    """Un admin existe déjà — ``POST /v2/setup/first-admin`` n'est plus permis."""


class ActivationTokenInvalidError(ValueError):
    """Lien d'activation inconnu, déjà utilisé ou expiré."""


class ActivationTargetMismatchError(ActivationTokenInvalidError):
    """Le lien valide ne correspond pas à l'utilisateur demandé."""


class ActivationReissueForbiddenError(ValueError):
    """Réémission réservée au premier admin pending, avant tout admin actif."""


class InvalidCredentialsError(ValueError):
    """Email/mot de passe incorrect, ou compte non activé."""


class SessionInvalidError(ValueError):
    """Cookie de session absent, mal formé, expiré ou signature invalide."""


@dataclass(frozen=True)
class UserRecord:
    id: str
    email: str
    role: str
    activated_at: str | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "activated_at": self.activated_at,
            "created_at": self.created_at,
        }


class UsersService:
    def __init__(self, engine: Engine, *, org_id: str, pepper: bytes) -> None:
        self._engine = engine
        self._org_id = org_id
        self._pepper = pepper

    def has_admin(self) -> bool:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.users.c.id)
                    .where(v2_schema.users.c.org_id == self._org_id)
                    .where(v2_schema.users.c.role == "admin")
                    .where(v2_schema.users.c.activated_at.is_not(None))
                )
                .mappings()
                .first()
            )
        return row is not None

    def create_first_admin(self, *, email: str, now: datetime | None = None) -> tuple[UserRecord, str]:
        if self.has_admin():
            raise AdminAlreadyExistsError("un admin actif existe déjà")
        return self._create_pending_user(email=email, role="admin", now=now)

    def invite(self, *, email: str, role: str, now: datetime | None = None) -> tuple[UserRecord, str]:
        if role not in ROLES:
            raise UserValidationError(f"role invalide : {role!r}")
        return self._create_pending_user(email=email, role=role, now=now)

    def _create_pending_user(self, *, email: str, role: str, now: datetime | None = None) -> tuple[UserRecord, str]:
        email = (email or "").strip().lower()
        if not email or "@" not in email or len(email) > 320:
            raise UserValidationError("email invalide")
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        user_id = uuid.uuid4().hex
        with self._engine.begin() as connection:
            connection.execute(
                v2_schema.users.insert(),
                {
                    "id": user_id,
                    "org_id": self._org_id,
                    "email": email,
                    "role": role,
                    "password_hash": None,
                    "oidc_subject": None,
                    "created_at": reference,
                    "activated_at": None,
                },
            )
            activation_token = secrets.token_urlsafe(32)
            connection.execute(
                v2_schema.activation_tokens.insert(),
                {
                    "id": uuid.uuid4().hex,
                    "user_id": user_id,
                    "hash": _hash_opaque_token(activation_token, self._pepper),
                    "expires_at": reference + ACTIVATION_TTL,
                    "used_at": None,
                    "created_at": reference,
                },
            )
        return self.get(user_id), activation_token

    def reissue_first_admin_activation(self, user_id: str, *, now: datetime | None = None) -> tuple[UserRecord, str]:
        """Action explicite privilégiée : même utilisateur, nouveaux liens uniquement.

        L'appelant doit exiger le scope admin. Le verrou d'organisation
        sérialise les réémissions ; la transaction invalide les anciens liens
        et insère seulement le hash du nouveau jeton (jamais sa valeur).
        """
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                select(v2_schema.organizations.c.id)
                .where(v2_schema.organizations.c.id == self._org_id)
                .with_for_update()
            ).scalar_one()
            row = (
                connection.execute(
                    select(v2_schema.users)
                    .where(
                        v2_schema.users.c.id == user_id,
                        v2_schema.users.c.org_id == self._org_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise UserNotFoundError(user_id)
            active = connection.execute(
                select(v2_schema.users.c.id).where(
                    v2_schema.users.c.org_id == self._org_id,
                    v2_schema.users.c.role == "admin",
                    v2_schema.users.c.activated_at.is_not(None),
                )
            ).first()
            if row["role"] != "admin" or row["activated_at"] is not None or active is not None:
                raise ActivationReissueForbiddenError("réémission permise uniquement pour le premier admin non activé")
            connection.execute(
                update(v2_schema.activation_tokens)
                .where(
                    v2_schema.activation_tokens.c.user_id == user_id,
                    v2_schema.activation_tokens.c.used_at.is_(None),
                )
                .values(used_at=reference)
            )
            token = secrets.token_urlsafe(32)
            connection.execute(
                v2_schema.activation_tokens.insert(),
                {
                    "id": uuid.uuid4().hex,
                    "user_id": user_id,
                    "hash": _hash_opaque_token(token, self._pepper),
                    "expires_at": reference + ACTIVATION_TTL,
                    "used_at": None,
                    "created_at": reference,
                },
            )
        return self.get(user_id), token

    def activate(
        self, *, activation_token: str, password: str, expected_user_id: str | None = None, now: datetime | None = None
    ) -> UserRecord:
        token_hash = _hash_opaque_token(activation_token, self._pepper)
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.activation_tokens)
                    .join(v2_schema.users)
                    .where(
                        v2_schema.activation_tokens.c.hash == token_hash,
                        v2_schema.users.c.org_id == self._org_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["used_at"] is not None:
            raise ActivationTokenInvalidError("lien d'activation inconnu ou déjà utilisé")
        if expected_user_id is not None and row["user_id"] != expected_user_id:
            raise ActivationTargetMismatchError("lien d'activation ne correspond pas à cet utilisateur")
        expires_at = row["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < reference:
            raise ActivationTokenInvalidError("lien d'activation expiré")
        password_hash = hash_password(password)
        with self._engine.begin() as connection:
            connection.execute(
                select(v2_schema.organizations.c.id)
                .where(v2_schema.organizations.c.id == self._org_id)
                .with_for_update()
            ).scalar_one()
            consumed = connection.execute(
                update(v2_schema.activation_tokens)
                .where(v2_schema.activation_tokens.c.id == row["id"])
                .where(v2_schema.activation_tokens.c.used_at.is_(None))
                .values(used_at=reference)
            )
            if consumed.rowcount != 1:
                raise ActivationTokenInvalidError("lien d'activation déjà utilisé ou réémis")
            connection.execute(
                update(v2_schema.users)
                .where(v2_schema.users.c.id == row["user_id"])
                .values(password_hash=password_hash, activated_at=reference)
            )
        return self.get(row["user_id"])

    def link_or_create_oidc_user(
        self, *, oidc_subject: str, email: str | None, now: datetime | None = None
    ) -> UserRecord:
        """Résout l'utilisateur d'un callback OIDC réussi (tâche 10, contrat §6.2).

        Trois cas, dans cet ordre : (1) un utilisateur porte déjà ce
        ``oidc_subject`` — c'est lui ; (2) un utilisateur existe avec cet
        email (créé par invitation classique) — on le lie
        (``oidc_subject`` posé, jamais réécrit une fois lié à un autre
        ``sub``) ; (3) sinon, un nouvel utilisateur ``reader`` est créé et
        activé immédiatement (l'identité a déjà été vérifiée par le
        fournisseur OIDC — pas de lien d'activation à renvoyer). Un premier
        admin doit toujours être activé par mot de passe
        (``create_first_admin``) : OIDC ne crée jamais d'admin.
        """

        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.connect() as connection:
            by_subject = (
                connection.execute(
                    select(v2_schema.users).where(
                        v2_schema.users.c.oidc_subject == oidc_subject, v2_schema.users.c.org_id == self._org_id
                    )
                )
                .mappings()
                .first()
            )
        if by_subject is not None:
            return _to_record(by_subject)

        normalized_email = (email or "").strip().lower() or None
        if normalized_email:
            with self._engine.connect() as connection:
                by_email = (
                    connection.execute(
                        select(v2_schema.users).where(
                            v2_schema.users.c.email == normalized_email, v2_schema.users.c.org_id == self._org_id
                        )
                    )
                    .mappings()
                    .first()
                )
            if by_email is not None:
                if by_email["oidc_subject"] is not None and by_email["oidc_subject"] != oidc_subject:
                    raise UserValidationError(
                        "cet email est déjà lié à un autre compte OIDC — contacter un administrateur"
                    )
                with self._engine.begin() as connection:
                    connection.execute(
                        update(v2_schema.users)
                        .where(v2_schema.users.c.id == by_email["id"])
                        .values(oidc_subject=oidc_subject, activated_at=by_email["activated_at"] or reference)
                    )
                return self.get(by_email["id"])

        if not normalized_email:
            raise UserValidationError("le fournisseur OIDC n'a renvoyé aucun email exploitable pour créer le compte")
        user_id = uuid.uuid4().hex
        with self._engine.begin() as connection:
            connection.execute(
                v2_schema.users.insert(),
                {
                    "id": user_id,
                    "org_id": self._org_id,
                    "email": normalized_email,
                    "role": "reader",
                    "password_hash": None,
                    "oidc_subject": oidc_subject,
                    "created_at": reference,
                    "activated_at": reference,
                },
            )
        return self.get(user_id)

    def authenticate_password(self, *, email: str, password: str) -> UserRecord:
        email = (email or "").strip().lower()
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.users).where(
                        v2_schema.users.c.email == email, v2_schema.users.c.org_id == self._org_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None or row["activated_at"] is None or row["password_hash"] is None:
            raise InvalidCredentialsError("email/mot de passe incorrect")
        if not verify_password(password, row["password_hash"]):
            raise InvalidCredentialsError("email/mot de passe incorrect")
        return _to_record(row)

    def create_session_token(self, user: UserRecord, *, now: datetime | None = None) -> str:
        """Cookie de session : ``<user_id>.<expiry_epoch>.<signature>`` — sans état serveur."""

        reference = now or datetime.now(timezone.utc)
        expiry = int((reference + SESSION_TTL).timestamp())
        payload = f"{user.id}.{expiry}"
        signature = sign_payload(payload, self._pepper)
        return f"{payload}.{signature}"

    def resolve_session(self, session_token: str) -> UserRecord:
        parts = (session_token or "").split(".")
        if len(parts) != 3:
            raise SessionInvalidError("cookie de session mal formé")
        user_id, expiry_raw, signature = parts
        payload = f"{user_id}.{expiry_raw}"
        if not verify_signature(payload, signature, self._pepper):
            raise SessionInvalidError("signature de session invalide")
        try:
            expiry = int(expiry_raw)
        except ValueError as error:
            raise SessionInvalidError("expiration de session invalide") from error
        if expiry < int(time.time()):
            raise SessionInvalidError("session expirée")
        try:
            return self.get(user_id)
        except UserNotFoundError as error:
            raise SessionInvalidError("utilisateur de la session introuvable") from error

    def get(self, user_id: str) -> UserRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.users).where(
                        v2_schema.users.c.id == user_id, v2_schema.users.c.org_id == self._org_id
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise UserNotFoundError(user_id)
        return _to_record(row)

    def list(self) -> tuple[UserRecord, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(v2_schema.users)
                    .where(v2_schema.users.c.org_id == self._org_id)
                    .order_by(v2_schema.users.c.created_at)
                )
                .mappings()
                .all()
            )
        return tuple(_to_record(row) for row in rows)


def _to_record(row: object) -> UserRecord:
    return UserRecord(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        activated_at=_iso(row["activated_at"]),
        created_at=_iso(row["created_at"]),
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()
