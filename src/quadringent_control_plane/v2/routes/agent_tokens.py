"""Routes ``/v2/agent-tokens`` — CRUD + rotation/révocation (tâche 8).

Scope ``admin`` partout : la gestion des jetons d'agent est une opération
sensible (§2.5 du contrat). La valeur en clair n'est jamais renvoyée après
la création/rotation initiale — ``to_dict()`` du service ne l'expose jamais.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import invalid_request, not_found
from ..http import AuditContext, envelope, idempotent_write
from ..services.agent_tokens import (
    AgentTokenNotFoundError,
    AgentTokenValidationError,
    AgentTokensService,
)

router = APIRouter(prefix="/v2/agent-tokens", tags=["agent-tokens"])


def _service(request: Request) -> AgentTokensService:
    return AgentTokensService(
        request.app.state.engine,
        org_id=request.app.state.org_id,
        pepper=request.app.state.token_pepper,
    )


@router.get("")
async def list_agent_tokens(request: Request, identity=Depends(require_scope("admin"))) -> dict[str, object]:
    service = _service(request)
    return {"items": [record.to_dict() for record in service.list()], "next_cursor": None}


@router.get("/{token_id}")
async def get_agent_token(
    token_id: str, request: Request, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(token_id)
    except AgentTokenNotFoundError as error:
        raise not_found("jeton d'agent introuvable") from error
    return record.to_dict()


@router.post("", status_code=201)
async def create_agent_token(
    request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        from datetime import datetime

        expires_raw = payload.get("expires_at")
        expires_at = None
        if isinstance(expires_raw, str) and expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw)
            except ValueError as error:
                raise invalid_request("expires_at doit être une date ISO 8601") from error
        try:
            record, token_value = service.create(
                name=payload.get("name"),
                scope=payload.get("scope"),
                source_restriction=payload.get("source_restriction", []),
                pre_authorized_actions=payload.get("pre_authorized_actions", []),
                created_by=identity.actor_display,
                expires_at=expires_at,
                never_expires=bool(payload.get("never_expires", False)),
            )
        except AgentTokenValidationError as error:
            raise invalid_request(str(error)) from error
        body = record.to_dict()
        body["token"] = token_value
        return 201, envelope(
            before=None, after=body, verify_method="GET", verify_path=f"/v2/agent-tokens/{record.id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path="/v2/agent-tokens",
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="agent_token.create", resource_type="agent_token"),
        identity=identity,
    )


@router.post("/{token_id}/rotate")
async def rotate_agent_token(
    token_id: str, request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/agent-tokens/{token_id}/rotate"

    def build(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record, token_value = service.rotate(token_id)
        except AgentTokenNotFoundError as error:
            raise not_found("jeton d'agent introuvable") from error
        body = record.to_dict()
        body["token"] = token_value
        return 200, envelope(
            before=None, after=body, verify_method="GET", verify_path=f"/v2/agent-tokens/{token_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="agent_token.rotate", resource_type="agent_token", resource_id=token_id),
        identity=identity,
    )


@router.delete("/{token_id}")
async def revoke_agent_token(
    token_id: str, request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/agent-tokens/{token_id}"

    def build(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            before = service.get(token_id)
            after = service.revoke(token_id)
        except AgentTokenNotFoundError as error:
            raise not_found("jeton d'agent introuvable") from error
        return 200, envelope(
            before=before.to_dict(),
            after=after.to_dict(),
            verify_method="GET",
            verify_path=f"/v2/agent-tokens/{token_id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="DELETE",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="agent_token.revoke", resource_type="agent_token", resource_id=token_id),
        identity=identity,
    )
