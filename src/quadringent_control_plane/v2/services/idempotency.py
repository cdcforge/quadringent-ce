"""Magasin des clés d'idempotence — enveloppe d'action générique (tâche 6).

Toute écriture (POST/PATCH/DELETE) sous ``/v2`` exige un en-tête
``Idempotency-Key`` (UUID ou chaîne ≤128 caractères). La même clé rejouée
par le même acteur de la même organisation renvoie les métadonnées déjà
produites, sans les secrets à émission unique. Un autre acteur ou un corps
différent est un conflit (409 ``idempotency_key_conflict``). Une
clé expire après 24 h (§2 du contrat) : au-delà, elle peut être réutilisée
comme si elle était inédite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..redaction import redact_one_time_secrets

_MAX_KEY_LENGTH = 128
_TTL = timedelta(hours=24)


class IdempotencyKeyConflictError(ValueError):
    """Même clé, acteur ou requête différents — 409 ``idempotency_key_conflict``."""

    def __init__(self, key: str) -> None:
        super().__init__(f"clé d'idempotence liée à un autre acteur ou une autre requête : {key}")
        self.key = key


class IdempotencyKeyMissingError(ValueError):
    """En-tête ``Idempotency-Key`` absent ou hors gabarit — 400 ``invalid_request``."""


def validate_key(raw_key: str | None) -> str:
    if raw_key is None or not raw_key.strip():
        raise IdempotencyKeyMissingError("en-tête Idempotency-Key manquant")
    key = raw_key.strip()
    if len(key) > _MAX_KEY_LENGTH:
        raise IdempotencyKeyMissingError("Idempotency-Key dépasse 128 caractères")
    return key


def request_hash(method: str, path: str, body: bytes) -> str:
    """Empreinte stable du corps d'une requête, jamais du corps en clair."""

    digest = hashlib.sha256()
    digest.update(method.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, object]


class IdempotencyStore:
    """Une ligne par organisation et clé (table ``idempotency_keys``, §7.1)."""

    def __init__(self, engine: Engine, *, org_id: str = "default") -> None:
        if not org_id:
            raise ValueError("organisation requise pour le magasin d'idempotence")
        self._engine = engine
        self._org_id = org_id

    def resolve(
        self,
        *,
        key: str,
        actor_id: str,
        method: str,
        path: str,
        body_hash: str,
        now: datetime | None = None,
    ) -> StoredResponse | None:
        """``None`` si la clé est inédite ou expirée ; sinon la réponse rejouable.

        Lève ``IdempotencyKeyConflictError`` si la clé existe (et n'a pas
        expiré) pour un autre acteur ou une requête différente. Une ancienne
        clé sans organisation certaine est refusée, jamais traitée comme inédite.
        """

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.idempotency_keys).where(
                        v2_schema.idempotency_keys.c.org_id == self._org_id,
                        v2_schema.idempotency_keys.c.key == key,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                ambiguous = connection.execute(
                    select(v2_schema.idempotency_keys.c.key).where(
                        v2_schema.idempotency_keys.c.org_id == "",
                        v2_schema.idempotency_keys.c.key == key,
                    )
                ).first()
                if ambiguous is not None:
                    raise IdempotencyKeyConflictError(key)
        if row is None:
            return None
        if row["actor_id"] != actor_id:
            raise IdempotencyKeyConflictError(key)
        if _is_expired(row["created_at"], now):
            return None
        if row["request_hash"] != body_hash or row["method"] != method or row["path"] != path:
            raise IdempotencyKeyConflictError(key)
        return StoredResponse(status_code=row["status_code"], body=redact_one_time_secrets(dict(row["response"])))

    def store(
        self,
        *,
        key: str,
        actor_id: str,
        method: str,
        path: str,
        body_hash: str,
        status_code: int,
        response: dict[str, object],
        now: datetime | None = None,
    ) -> None:
        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            # Réutilisation après TTL : ne supprimer que la ligne expirée de
            # cette organisation, jamais une clé active ou d'un autre site.
            connection.execute(
                delete(v2_schema.idempotency_keys).where(
                    v2_schema.idempotency_keys.c.org_id == self._org_id,
                    v2_schema.idempotency_keys.c.key == key,
                    v2_schema.idempotency_keys.c.created_at < created_at - _TTL,
                )
            )
            connection.execute(
                insert(v2_schema.idempotency_keys),
                {
                    "key": key,
                    "org_id": self._org_id,
                    "actor_id": actor_id,
                    "method": method,
                    "path": path,
                    "request_hash": body_hash,
                    "status_code": status_code,
                    "response": redact_one_time_secrets(response),
                    "created_at": created_at,
                },
            )


def _is_expired(created_at: object, now: datetime | None) -> bool:
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return reference - created_at > _TTL
