"""Service ``pending_confirmation`` (tâche 7, contrat §6.4).

Une action sensible sans confirmation crée une ligne ``confirmations`` et
renvoie ``409 pending_confirmation_required``. Trois façons de la lever :

1. Un humain authentifié (scope suffisant) appelle
   ``POST /v2/confirmations/{id}/approve``.
2. Un lien signé à usage unique (``approval_token``, HMAC-sha256 avec le
   pepper serveur — même primitive que les jetons d'agent, TTL court)
   envoyé par email/webhook, sans authentification préalable.
3. Un jeton d'agent pré-autorisé (``pre_authorized_actions`` déclarée à la
   création du jeton, tâche 8) approuve directement s'il porte le scope
   requis et que l'action figure dans sa liste.

Une confirmation approuvée n'est valide qu'une fois : après exécution de
l'action sensible, l'appelant (route) la fait passer à l'état ``used`` —
voir ``mark_used``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hmac
import secrets
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import hash_agent_token as _hash_token  # primitive HMAC générique, réutilisée telle quelle

DEFAULT_TTL = timedelta(hours=24)
APPROVAL_LINK_TTL = timedelta(minutes=30)

_STATES = ("pending", "approved", "rejected", "expired", "used")


class ConfirmationNotFoundError(LookupError):
    """Aucune confirmation pour cet identifiant — 404 ``not_found``."""


class ConfirmationStateError(ValueError):
    """La confirmation n'est pas dans l'état attendu pour cette opération."""


class ConfirmationTokenInvalidError(ValueError):
    """Le jeton d'approbation présenté (lien signé) est invalide."""


class ConfirmationForbiddenError(PermissionError):
    """L'identité n'a pas le droit d'approuver/rejeter cette confirmation."""


@dataclass(frozen=True)
class ConfirmationRecord:
    id: str
    action_ref: str
    resource_type: str
    resource_id: str
    reason: str
    risk_estimate: object
    requested_by_kind: str
    requested_by_id: str
    expires_at: str
    state: str
    approved_by_kind: str | None
    approved_by_id: str | None
    approved_at: str | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "action_ref": self.action_ref,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "reason": self.reason,
            "risk_estimate": self.risk_estimate,
            "requested_by_kind": self.requested_by_kind,
            "requested_by_id": self.requested_by_id,
            "expires_at": self.expires_at,
            "state": self.state,
            "approved_by_kind": self.approved_by_kind,
            "approved_by_id": self.approved_by_id,
            "approved_at": self.approved_at,
            "created_at": self.created_at,
        }


class ConfirmationsService:
    def __init__(self, engine: Engine, *, org_id: str, pepper: bytes) -> None:
        self._engine = engine
        self._org_id = org_id
        self._pepper = pepper

    def create(
        self,
        *,
        action_ref: str,
        resource_type: str,
        resource_id: str,
        reason: str,
        requested_by_kind: str,
        requested_by_id: str,
        risk_estimate: object = None,
        ttl: timedelta = DEFAULT_TTL,
        now: datetime | None = None,
    ) -> tuple[ConfirmationRecord, str]:
        """Crée une confirmation ``pending`` ; retourne ``(record, approval_token)``.

        ``approval_token`` est la valeur en clair du lien signé à usage
        unique — jamais rejouable après consommation, jamais relisible
        ensuite (même discipline que la rotation des jetons d'agent).
        """

        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        confirmation_id = uuid.uuid4().hex
        approval_token = secrets.token_urlsafe(32)
        approval_token_hash = _hash_token(approval_token, self._pepper)
        with self._engine.begin() as connection:
            connection.execute(
                v2_schema.confirmations.insert(),
                {
                    "id": confirmation_id,
                    "org_id": self._org_id,
                    "action_ref": action_ref,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "reason": reason,
                    "risk_estimate": risk_estimate,
                    "requested_by_kind": requested_by_kind,
                    "requested_by_id": requested_by_id,
                    "approval_token_hash": approval_token_hash,
                    "expires_at": reference + ttl,
                    "state": "pending",
                    "approved_by_kind": None,
                    "approved_by_id": None,
                    "approved_at": None,
                    "created_at": reference,
                },
            )
        return self.get(confirmation_id), approval_token

    def get(self, confirmation_id: str, *, now: datetime | None = None) -> ConfirmationRecord:
        record = self._fetch(confirmation_id)
        return self._expire_if_needed(record, now=now)

    def list(self, *, state: str | None = None, now: datetime | None = None) -> tuple[ConfirmationRecord, ...]:
        statement = (
            select(v2_schema.confirmations)
            .where(v2_schema.confirmations.c.org_id == self._org_id)
            .order_by(v2_schema.confirmations.c.created_at.desc())
        )
        if state is not None:
            statement = statement.where(v2_schema.confirmations.c.state == state)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        records = tuple(self._expire_if_needed(_to_record(row), now=now) for row in rows)
        if state is not None:
            records = tuple(record for record in records if record.state == state)
        return records

    def approve(
        self,
        confirmation_id: str,
        *,
        approver_kind: str | None = None,
        approver_id: str | None = None,
        approval_token: str | None = None,
        allowed_action_refs: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> ConfirmationRecord:
        """Approuve une confirmation ``pending`` — lien signé ou identité pré-autorisée.

        ``allowed_action_refs`` restreint l'approbation par identité (jeton
        pré-autorisé, tâche 8) : ``None`` = pas de restriction (humain via
        cockpit), un tuple = seules ces ``action_ref`` sont approuvables par
        cette identité.
        """

        record = self.get(confirmation_id, now=now)
        if record.state != "pending":
            raise ConfirmationStateError(f"confirmation dans l'état {record.state!r}, ni approuvable ni rejetable")

        if approval_token is not None:
            if not self._token_matches(confirmation_id, approval_token):
                raise ConfirmationTokenInvalidError("jeton d'approbation invalide ou déjà consommé")
            kind, actor_id = "human", "lien-signé"
        else:
            if allowed_action_refs is not None and record.action_ref not in allowed_action_refs:
                raise ConfirmationForbiddenError("cette identité n'est pas pré-autorisée pour cette action")
            kind, actor_id = approver_kind or "human", approver_id or "inconnu"

        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.confirmations)
                .where(v2_schema.confirmations.c.id == confirmation_id)
                .where(v2_schema.confirmations.c.org_id == self._org_id)
                .values(state="approved", approved_by_kind=kind, approved_by_id=actor_id, approved_at=reference)
            )
        return self.get(confirmation_id)

    def reject(
        self,
        confirmation_id: str,
        *,
        approver_kind: str,
        approver_id: str,
        now: datetime | None = None,
    ) -> ConfirmationRecord:
        record = self.get(confirmation_id, now=now)
        if record.state != "pending":
            raise ConfirmationStateError(f"confirmation dans l'état {record.state!r}, ni approuvable ni rejetable")
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.confirmations)
                .where(v2_schema.confirmations.c.id == confirmation_id)
                .where(v2_schema.confirmations.c.org_id == self._org_id)
                .values(
                    state="rejected",
                    approved_by_kind=approver_kind,
                    approved_by_id=approver_id,
                    approved_at=reference,
                )
            )
        return self.get(confirmation_id)

    def mark_used(self, confirmation_id: str) -> ConfirmationRecord:
        """Consomme une confirmation ``approved`` après exécution de l'action liée."""

        record = self.get(confirmation_id)
        if record.state != "approved":
            raise ConfirmationStateError(f"confirmation dans l'état {record.state!r}, pas 'approved'")
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.confirmations)
                .where(v2_schema.confirmations.c.id == confirmation_id)
                .where(v2_schema.confirmations.c.org_id == self._org_id)
                .values(state="used")
            )
        return self.get(confirmation_id)

    def approval_link_actor(self, confirmation_id: str, approval_token: str, *, now: datetime | None = None) -> str:
        """Valide le lien avant rejeu sans consommer de nouveau l'approbation."""
        record = self._fetch(confirmation_id)
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expiry = datetime.fromisoformat(record.expires_at)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry < reference or not self._token_matches(confirmation_id, approval_token):
            raise ConfirmationTokenInvalidError("jeton d'approbation invalide ou expiré")
        return f"signed-link:{confirmation_id}:{_hash_token(approval_token, self._pepper)}"

    def _token_matches(self, confirmation_id: str, approval_token: str) -> bool:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.confirmations.c.approval_token_hash).where(
                        v2_schema.confirmations.c.id == confirmation_id,
                        v2_schema.confirmations.c.org_id == self._org_id,
                    )
                )
                .mappings()
                .one()
            )
        stored = row["approval_token_hash"]
        if stored is None:
            return False
        return hmac.compare_digest(_hash_token(approval_token, self._pepper), stored)

    def _fetch(self, confirmation_id: str) -> ConfirmationRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.confirmations).where(
                        v2_schema.confirmations.c.id == confirmation_id,
                        v2_schema.confirmations.c.org_id == self._org_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ConfirmationNotFoundError(confirmation_id)
        return _to_record(row)

    def _expire_if_needed(self, record: ConfirmationRecord, *, now: datetime | None) -> ConfirmationRecord:
        if record.state != "pending":
            return record
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires_at = datetime.fromisoformat(record.expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at >= reference:
            return record
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.confirmations)
                .where(v2_schema.confirmations.c.id == record.id)
                .where(v2_schema.confirmations.c.org_id == self._org_id)
                .where(v2_schema.confirmations.c.state == "pending")
                .values(state="expired")
            )
        return self._fetch(record.id)


def _to_record(row: object) -> ConfirmationRecord:
    return ConfirmationRecord(
        id=row["id"],
        action_ref=row["action_ref"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        reason=row["reason"],
        risk_estimate=row["risk_estimate"],
        requested_by_kind=row["requested_by_kind"],
        requested_by_id=row["requested_by_id"],
        expires_at=_iso(row["expires_at"]),
        state=row["state"],
        approved_by_kind=row["approved_by_kind"],
        approved_by_id=row["approved_by_id"],
        approved_at=_iso(row["approved_at"]),
        created_at=_iso(row["created_at"]),
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()
