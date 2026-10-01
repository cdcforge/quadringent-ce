"""Service jetons d'agent (tâche 8, contrat §6.3).

Format ``qdt_<rd|op|ad>_<b62>``, seul le hash (sha256 avec pepper serveur —
voir la justification dans ``v2/crypto.py``) est persisté. Un jeton non
révoqué et non expiré porte un scope (read/operate/admin), une restriction
de sources optionnelle et une liste d'actions pré-autorisées (utilisée par
le flux de confirmation, tâche 7 : un jeton peut approuver ses propres
confirmations si l'action y figure).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import generate_agent_token, hash_agent_token

_SCOPES = ("read", "operate", "admin")


class AgentTokenValidationError(ValueError):
    """Corps de création hors contrat (scope inconnu, expiration absente...)."""


class AgentTokenNotFoundError(LookupError):
    """Aucun jeton pour cet identifiant — 404 ``not_found``."""


class AgentTokenInvalidError(LookupError):
    """Le jeton présenté est inconnu, révoqué ou expiré — 401."""


@dataclass(frozen=True)
class AgentTokenRecord:
    id: str
    name: str
    scope: str
    prefix: str
    source_restriction: tuple[str, ...]
    pre_authorized_actions: tuple[str, ...]
    created_by: str
    never_expires: bool
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "scope": self.scope,
            "source_restriction": list(self.source_restriction),
            "pre_authorized_actions": list(self.pre_authorized_actions),
            "created_by": self.created_by,
            "never_expires": self.never_expires,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "last_used_at": self.last_used_at,
            "created_at": self.created_at,
        }


class AgentTokensService:
    def __init__(self, engine: Engine, *, org_id: str, pepper: bytes) -> None:
        self._engine = engine
        self._org_id = org_id
        self._pepper = pepper

    def create(
        self,
        *,
        name: object,
        scope: object,
        source_restriction: object = (),
        pre_authorized_actions: object = (),
        created_by: str,
        expires_at: datetime | None = None,
        never_expires: bool = False,
        now: datetime | None = None,
    ) -> tuple[AgentTokenRecord, str]:
        if not isinstance(name, str) or not name.strip():
            raise AgentTokenValidationError("name invalide ou vide")
        if scope not in _SCOPES:
            raise AgentTokenValidationError(f"scope invalide : {scope!r}")
        if not never_expires and expires_at is None:
            raise AgentTokenValidationError(
                "expires_at requis, sauf si never_expires: true est déclaré explicitement"
            )
        if never_expires and expires_at is not None:
            raise AgentTokenValidationError("expires_at et never_expires sont mutuellement exclusifs")
        restriction = _validated_string_list(source_restriction, "source_restriction")
        pre_authorized = _validated_string_list(pre_authorized_actions, "pre_authorized_actions")

        token_value, prefix = generate_agent_token(scope)
        token_hash = hash_agent_token(token_value, self._pepper)
        token_id = uuid.uuid4().hex
        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires_value = expires_at.astimezone(timezone.utc) if expires_at is not None else None
        with self._engine.begin() as connection:
            connection.execute(
                insert(v2_schema.agent_tokens),
                {
                    "id": token_id,
                    "org_id": self._org_id,
                    "name": name,
                    "scope": scope,
                    "prefix": prefix,
                    "hash": token_hash,
                    "source_restriction": list(restriction),
                    "pre_authorized_actions": list(pre_authorized),
                    "created_by": created_by,
                    "never_expires": never_expires,
                    "expires_at": expires_value,
                    "revoked_at": None,
                    "last_used_at": None,
                    "created_at": created_at,
                },
            )
        return self.get(token_id), token_value

    def rotate(self, token_id: str, *, now: datetime | None = None) -> tuple[AgentTokenRecord, str]:
        """Invalide l'ancien hash, retourne une nouvelle valeur en clair (une seule fois)."""

        record = self.get(token_id)
        token_value, prefix = generate_agent_token(record.scope)
        token_hash = hash_agent_token(token_value, self._pepper)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.agent_tokens)
                .where(v2_schema.agent_tokens.c.id == token_id)
                .values(hash=token_hash, prefix=prefix, last_used_at=None)
            )
        return self.get(token_id), token_value

    def revoke(self, token_id: str, *, now: datetime | None = None) -> AgentTokenRecord:
        self.get(token_id)  # 404 si absent
        revoked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.agent_tokens)
                .where(v2_schema.agent_tokens.c.id == token_id)
                .values(revoked_at=revoked_at)
            )
        return self.get(token_id)

    def get(self, token_id: str) -> AgentTokenRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.agent_tokens).where(v2_schema.agent_tokens.c.id == token_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentTokenNotFoundError(token_id)
        return _to_record(row)

    def list(self) -> tuple[AgentTokenRecord, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(v2_schema.agent_tokens).order_by(
                        v2_schema.agent_tokens.c.created_at, v2_schema.agent_tokens.c.id
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_to_record(row) for row in rows)

    def authenticate(self, token_value: str, *, now: datetime | None = None) -> AgentTokenRecord:
        """Résout un jeton en clair vers son enregistrement — échoue fermé.

        Met à jour ``last_used_at`` sur succès. Ne distingue volontairement
        pas, dans le message d'erreur, un jeton inconnu d'un jeton
        révoqué/expiré (pas d'oracle d'énumération).
        """

        token_hash = hash_agent_token(token_value, self._pepper)
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.agent_tokens).where(v2_schema.agent_tokens.c.hash == token_hash, v2_schema.agent_tokens.c.org_id == self._org_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentTokenInvalidError("jeton d'agent inconnu")
        record = _to_record(row)
        if record.revoked_at is not None:
            raise AgentTokenInvalidError("jeton d'agent révoqué")
        if not record.never_expires:
            expires_at = row["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at is None or expires_at.replace(tzinfo=expires_at.tzinfo or timezone.utc) < reference:
                raise AgentTokenInvalidError("jeton d'agent expiré")
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.agent_tokens)
                .where(v2_schema.agent_tokens.c.id == record.id)
                .values(last_used_at=reference)
            )
        return self.get(record.id)


def _validated_string_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise AgentTokenValidationError(f"{field} doit être une liste de chaînes")
    return tuple(value)


def _to_record(row: object) -> AgentTokenRecord:
    return AgentTokenRecord(
        id=row["id"],
        name=row["name"],
        scope=row["scope"],
        prefix=row["prefix"],
        source_restriction=tuple(row["source_restriction"] or ()),
        pre_authorized_actions=tuple(row["pre_authorized_actions"] or ()),
        created_by=row["created_by"],
        never_expires=bool(row["never_expires"]),
        expires_at=_iso(row["expires_at"]),
        revoked_at=_iso(row["revoked_at"]),
        last_used_at=_iso(row["last_used_at"]),
        created_at=_iso(row["created_at"]),
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()
