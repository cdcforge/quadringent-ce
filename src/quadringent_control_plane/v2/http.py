"""Glue FastAPI de l'enveloppe d'action générique (tâche 6).

Factorise, pour toutes les routes d'écriture ``/v2``, la lecture du corps
JSON, la validation de l'en-tête ``Idempotency-Key``, le rejeu/conflit
d'idempotence et la forme de réponse ``{"before","after","verify",
"dry_run"}``. Une route n'a qu'à fournir sa fonction ``build`` qui, à partir
du corps déjà parsé, retourne ``(status_code, body_de_l_enveloppe)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json

from fastapi import Request, Response

from .errors import ApiError, invalid_request
from .services.idempotency import (
    IdempotencyKeyConflictError,
    IdempotencyKeyMissingError,
    IdempotencyStore,
    request_hash,
    validate_key,
)


class AuditContext:
    """Décrit comment journaliser une écriture (tâche 11) — optionnel.

    Passé à ``idempotent_write`` par une route qui veut que son écriture
    soit tracée dans ``audit_records`` sans dupliquer la logique de journal
    dans chaque module de route.
    """

    def __init__(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
    ) -> None:
        self.action = action
        self.resource_type = resource_type
        self.resource_id = resource_id


def envelope(
    *,
    before: object,
    after: object,
    verify_method: str,
    verify_path: str,
    dry_run: object = None,
) -> dict[str, object]:
    return {
        "before": before,
        "after": after,
        "verify": {"method": verify_method, "path": verify_path},
        "dry_run": dry_run,
    }


def parse_json_body(raw_body: bytes) -> dict[str, object]:
    if not raw_body:
        return {}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise invalid_request("corps JSON invalide") from error
    if not isinstance(payload, dict):
        raise invalid_request("le corps doit être un objet JSON")
    return payload


def require_idempotency_key(raw: str | None) -> str:
    try:
        return validate_key(raw)
    except IdempotencyKeyMissingError as error:
        raise invalid_request(str(error)) from error


async def idempotent_write(
    *,
    request: Request,
    response: Response,
    actor_id: str,
    method: str,
    path: str,
    store: IdempotencyStore,
    build: Callable[[dict[str, object]], tuple[int, dict[str, object]]] | Callable[
        [dict[str, object]], Awaitable[tuple[int, dict[str, object]]]
    ],
    audit: AuditContext | None = None,
    identity: object = None,
) -> dict[str, object]:
    """Exécute une écriture sous l'enveloppe générique (dry_run/idempotence).

    ``build`` reçoit le corps JSON déjà parsé et renvoie
    ``(status_code, corps_de_reponse)`` — c'est elle qui décide, selon
    ``dry_run``, de persister ou non. Si ``audit`` et ``identity`` sont
    fournis (et qu'un ``AuditService`` est déclaré sur l'application), une
    ligne d'audit est écrite pour toute tentative d'écriture — succès comme
    échec (tâche 11 : « recorded for every write ») — jamais pour un rejeu
    d'idempotence déjà tracé la première fois.
    """

    raw_body = await request.body()
    key = require_idempotency_key(request.headers.get("idempotency-key"))
    body_hash = request_hash(method, path, raw_body)
    try:
        replay = store.resolve(key=key, actor_id=actor_id, method=method, path=path, body_hash=body_hash)
    except IdempotencyKeyConflictError as error:
        raise ApiError(
            409,
            "idempotency_key_conflict",
            str(error),
            next_action="changer de clé ou renvoyer le corps identique",
            retryable=False,
        ) from error
    if replay is not None:
        response.status_code = replay.status_code
        return replay.body

    payload = parse_json_body(raw_body)
    audit_service = getattr(request.app.state, "audit_service", None) if audit is not None else None
    try:
        result = build(payload)
        if hasattr(result, "__await__"):
            status_code, body = await result  # type: ignore[misc]
        else:
            status_code, body = result  # type: ignore[assignment]
    except ApiError as error:
        if audit_service is not None and identity is not None:
            _write_audit(
                audit_service,
                audit,
                identity,
                request,
                status="failed",
                dry_run=bool(payload.get("dry_run", False)),
                before=None,
                after=None,
            )
        raise

    if audit_service is not None and identity is not None:
        dry_run = bool(payload.get("dry_run", False)) or body.get("dry_run") is not None
        envelope_body = body if isinstance(body, dict) else {}
        _write_audit(
            audit_service,
            audit,
            identity,
            request,
            status="succeeded",
            dry_run=dry_run,
            before=envelope_body.get("before"),
            after=envelope_body.get("after"),
        )

    store.store(
        key=key,
        actor_id=actor_id,
        method=method,
        path=path,
        body_hash=body_hash,
        status_code=status_code,
        response=body,
    )
    response.status_code = status_code
    return body


def _write_audit(
    audit_service: object,
    audit: AuditContext | None,
    identity: object,
    request: Request,
    *,
    status: str,
    dry_run: bool,
    before: object,
    after: object,
) -> None:
    if audit is None:
        return
    resource_id = audit.resource_id
    audit_service.record(  # type: ignore[attr-defined]
        actor_kind=getattr(identity, "actor_kind", "human"),
        actor_id=getattr(identity, "actor_id", getattr(identity, "subject", "unknown")),
        actor_display=getattr(identity, "actor_display", getattr(identity, "subject", "unknown")),
        mcp_client=request.headers.get("x-mcp-client"),
        action=audit.action,
        resource_type=audit.resource_type,
        resource_id=resource_id,
        request_id=request.headers.get("x-request-id"),
        idempotency_key=request.headers.get("idempotency-key"),
        dry_run=dry_run,
        status=status,
        before=before,
        after=after,
    )
