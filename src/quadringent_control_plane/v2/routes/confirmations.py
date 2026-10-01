"""Routes ``/v2/confirmations`` — cockpit + lien direct (tâche 7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import Identity, require_scope
from ..errors import ApiError, invalid_request, not_found
from ..http import AuditContext, envelope, idempotent_write, parse_json_body
from ..services.confirmations import (
    ConfirmationForbiddenError,
    ConfirmationNotFoundError,
    ConfirmationRecord,
    ConfirmationsService,
    ConfirmationStateError,
    ConfirmationTokenInvalidError,
)

router = APIRouter(prefix="/v2/confirmations", tags=["confirmations"])


def _service(request: Request) -> ConfirmationsService:
    return ConfirmationsService(
        request.app.state.engine,
        org_id=request.app.state.org_id,
        pepper=request.app.state.token_pepper,
    )


def _present(record: ConfirmationRecord, identity: Identity) -> dict[str, object]:
    """Expose les décisions réellement autorisées pour cette identité."""
    pending = record.state == "pending"
    can_operate = identity.role in {"operator", "operate", "admin"}
    can_approve = (
        pending and can_operate and
        (identity.actor_kind != "agent" or record.action_ref in identity.pre_authorized_actions)
    )
    can_execute = (
        record.state == "approved" and can_operate and record.resource_type == "pipeline"
        and record.action_ref in {"pipeline.remove", "pipeline.restart_initial_copy"}
        and (identity.actor_kind != "agent" or record.action_ref in identity.pre_authorized_actions)
    )
    return {
        **record.to_dict(),
        "available": {"approve": can_approve, "reject": pending and can_operate, "execute": can_execute},
    }


@router.get("")
async def list_confirmations(
    request: Request, identity=Depends(require_scope("read")), state: str | None = "pending"
) -> dict[str, object]:
    service = _service(request)
    return {"items": [_present(record, identity) for record in service.list(state=None if state == "all" else state)], "next_cursor": None}


@router.get("/{confirmation_id}")
async def get_confirmation(
    confirmation_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(confirmation_id)
    except ConfirmationNotFoundError as error:
        raise not_found("confirmation introuvable") from error
    return _present(record, identity)


@router.post("/{confirmation_id}/approve")
async def approve_confirmation(
    confirmation_id: str,
    request: Request,
    response: Response,
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/confirmations/{confirmation_id}/approve"
    payload = parse_json_body(await request.body())
    approval_token = payload.get("token")
    signed_link = isinstance(approval_token, str) and bool(approval_token)
    identity = None
    if signed_link:
        # Valider la capacité avant le cache, même pour un rejeu. Son identité
        # stable est distincte d'une session/agent et ignore les en-têtes libres.
        try:
            actor_id = service.approval_link_actor(confirmation_id, approval_token)
        except ConfirmationNotFoundError as error:
            raise not_found("confirmation introuvable") from error
        except ConfirmationTokenInvalidError as error:
            raise invalid_request(str(error)) from error
    else:
        identity = require_scope("operate")(request)
        actor_id = identity.subject
        if identity.actor_kind == "agent":
            try:
                record = service.get(confirmation_id)
            except ConfirmationNotFoundError as error:
                raise not_found("confirmation introuvable") from error
            if record.action_ref not in identity.pre_authorized_actions:
                from ..errors import insufficient_role

                raise insufficient_role("cette identité n'est pas pré-autorisée pour cette action")

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        if signed_link:
            # Lien signé non authentifié — aucune identité requise, l'usage
            # unique du jeton fait foi (§6.4 du contrat).
            try:
                record = service.approve(confirmation_id, approval_token=approval_token)
            except ConfirmationNotFoundError as error:
                raise not_found("confirmation introuvable") from error
            except ConfirmationTokenInvalidError as error:
                raise invalid_request(str(error)) from error
            except ConfirmationStateError as error:
                raise ApiError(
                    409,
                    "capability_unavailable",
                    str(error),
                    next_action="relire l'état de la confirmation",
                    retryable=False,
                ) from error
            return 200, envelope(
                before=None,
                after=record.to_dict(),
                verify_method="GET",
                verify_path=f"/v2/confirmations/{confirmation_id}",
            )

        allowed = identity.pre_authorized_actions if identity.actor_kind == "agent" else None
        try:
            record = service.approve(
                confirmation_id,
                approver_kind=identity.actor_kind,
                approver_id=identity.actor_id,
                allowed_action_refs=allowed,
            )
        except ConfirmationNotFoundError as error:
            raise not_found("confirmation introuvable") from error
        except ConfirmationForbiddenError as error:
            from ..errors import insufficient_role

            raise insufficient_role(str(error)) from error
        except ConfirmationStateError as error:
            raise ApiError(
                409,
                "capability_unavailable",
                str(error),
                next_action="relire l'état de la confirmation",
                retryable=False,
            ) from error
        # Trace explicitement cette branche (approbation par identité,
        # humaine ou jeton d'agent pré-autorisé) : ``idempotent_write``
        # n'a ici ni ``audit=`` ni ``identity=`` déclarés (la branche
        # « lien signé » juste au-dessus est volontairement anonyme, cf.
        # docstring du module — aucune identité à tracer pour elle). Sans
        # cet enregistrement direct, ``audit_records`` ne distinguerait
        # jamais une approbation humaine d'une approbation par jeton
        # d'agent (contrat MCP/CLI §9.2 tâche 18 : « audit rows distinguish
        # agent vs human »).
        audit_service = getattr(request.app.state, "audit_service", None)
        if audit_service is not None:
            audit_service.record(
                actor_kind=identity.actor_kind,
                actor_id=identity.actor_id,
                actor_display=identity.actor_display,
                mcp_client=request.headers.get("x-mcp-client"),
                action="confirmation.approve",
                resource_type="confirmation",
                resource_id=confirmation_id,
                request_id=request.headers.get("x-request-id"),
                idempotency_key=request.headers.get("idempotency-key"),
                dry_run=False,
                status="succeeded",
                before=None,
                after=record.to_dict(),
            )
        return 200, envelope(
            before=None, after=record.to_dict(), verify_method="GET", verify_path=f"/v2/confirmations/{confirmation_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=actor_id,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="confirmation.approve", resource_type="confirmation", resource_id=confirmation_id),
    )


@router.post("/{confirmation_id}/reject")
async def reject_confirmation(
    confirmation_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/confirmations/{confirmation_id}/reject"

    def build(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record = service.reject(confirmation_id, approver_kind=identity.actor_kind, approver_id=identity.actor_id)
        except ConfirmationNotFoundError as error:
            raise not_found("confirmation introuvable") from error
        except ConfirmationStateError as error:
            raise ApiError(
                409,
                "capability_unavailable",
                str(error),
                next_action="relire l'état de la confirmation",
                retryable=False,
            ) from error
        return 200, envelope(
            before=None, after=record.to_dict(), verify_method="GET", verify_path=f"/v2/confirmations/{confirmation_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="confirmation.reject", resource_type="confirmation", resource_id=confirmation_id),
        identity=identity,
    )
