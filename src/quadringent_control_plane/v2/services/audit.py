"""Service d'audit v2 (tâche 11, contrat §6.5) — remplace ``audit.py`` (v1).

Table interrogeable ``audit_records`` au lieu du fichier append-only local
(``audit.py::ActionAuditLog``) : mêmes garanties (aucun secret, corps HTTP
ou détail d'exception brut ne doit jamais y être écrit). Les champs de
secret à émission unique sont retirés défensivement à l'écriture et à la
lecture, y compris pour les anciennes lignes restaurées.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..redaction import redact_one_time_secrets

_ACTOR_KINDS = ("human", "agent")
_STATUSES = ("succeeded", "failed", "pending_confirmation")


class AuditValidationError(ValueError):
    """Champ hors contrat — jamais silencieusement ignoré."""


@dataclass(frozen=True)
class AuditRecord:
    id: str
    at: str
    actor_kind: str
    actor_id: str
    actor_display: str
    mcp_client: str | None
    action: str
    resource_type: str
    resource_id: str | None
    request_id: str | None
    idempotency_key: str | None
    dry_run: bool
    confirmation_id: str | None
    status: str
    before: object
    after: object

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "at": self.at,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "actor_display": self.actor_display,
            "mcp_client": self.mcp_client,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "dry_run": self.dry_run,
            "confirmation_id": self.confirmation_id,
            "status": self.status,
            "before": redact_one_time_secrets(self.before),
            "after": redact_one_time_secrets(self.after),
        }


class AuditService:
    def __init__(self, engine: Engine, *, org_id: str) -> None:
        self._engine = engine
        self._org_id = org_id

    def record(
        self,
        *,
        actor_kind: str,
        actor_id: str,
        actor_display: str,
        action: str,
        resource_type: str,
        status: str,
        mcp_client: str | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        confirmation_id: str | None = None,
        before: object = None,
        after: object = None,
        now: datetime | None = None,
    ) -> AuditRecord:
        if actor_kind not in _ACTOR_KINDS:
            raise AuditValidationError(f"actor_kind invalide : {actor_kind!r}")
        if status not in _STATUSES:
            raise AuditValidationError(f"status invalide : {status!r}")
        record_id = uuid.uuid4().hex
        at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                v2_schema.audit_records.insert(),
                {
                    "id": record_id,
                    "org_id": self._org_id,
                    "at": at,
                    "actor_kind": actor_kind,
                    "actor_id": actor_id,
                    "actor_display": actor_display,
                    "mcp_client": mcp_client,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "request_id": request_id,
                    "idempotency_key": idempotency_key,
                    "dry_run": dry_run,
                    "confirmation_id": confirmation_id,
                    "status": status,
                    "before": redact_one_time_secrets(before),
                    "after": redact_one_time_secrets(after),
                },
            )
        return self.get(record_id)

    def get(self, record_id: str) -> AuditRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.audit_records).where(
                        v2_schema.audit_records.c.id == record_id,
                        v2_schema.audit_records.c.org_id == self._org_id,
                    )
                )
                .mappings()
                .one()
            )
        return _to_record(row)

    def query(
        self,
        *,
        actor_kind: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 50,
    ) -> tuple[AuditRecord, ...]:
        statement = (
            select(v2_schema.audit_records)
            .where(v2_schema.audit_records.c.org_id == self._org_id)
            .order_by(v2_schema.audit_records.c.at.desc(), v2_schema.audit_records.c.id.desc())
        )
        if actor_kind is not None:
            statement = statement.where(v2_schema.audit_records.c.actor_kind == actor_kind)
        if action is not None:
            statement = statement.where(v2_schema.audit_records.c.action == action)
        if resource_type is not None:
            statement = statement.where(v2_schema.audit_records.c.resource_type == resource_type)
        if resource_id is not None:
            statement = statement.where(v2_schema.audit_records.c.resource_id == resource_id)
        statement = statement.limit(max(1, min(limit, 500)))
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_to_record(row) for row in rows)


def _to_record(row: object) -> AuditRecord:
    return AuditRecord(
        id=row["id"],
        at=_iso(row["at"]),
        actor_kind=row["actor_kind"],
        actor_id=row["actor_id"],
        actor_display=row["actor_display"],
        mcp_client=row["mcp_client"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        request_id=row["request_id"],
        idempotency_key=row["idempotency_key"],
        dry_run=bool(row["dry_run"]),
        confirmation_id=row["confirmation_id"],
        status=row["status"],
        before=redact_one_time_secrets(row["before"]),
        after=redact_one_time_secrets(row["after"]),
    )


def _iso(value: object) -> str:
    if isinstance(value, str):
        return value
    return value.isoformat()
