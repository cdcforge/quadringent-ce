"""Routes ``/v2/actions/pause_all`` et ``/v2/actions/resume_all`` (tâche 18 MCP/CLI).

Portée organisation entière — voir ``services/bulk.py``. Toujours
confirmées (``pause_all``/``resume_all`` touchent potentiellement toutes
les sources/destinations en une seule action) sauf si un
``confirmation_token`` valide est fourni, même discipline que
``routes/pipelines.py`` pour les actions sensibles.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import ApiError, pending_confirmation_required, wrong_confirmation
from ..http import AuditContext, envelope, idempotent_write
from ..services.bulk import BulkActionsService
from ..services.confirmations import ConfirmationNotFoundError, ConfirmationsService
from ..services.events import EventsService
from ..services.pipelines import PipelineExecutorUnavailableError

router = APIRouter(prefix="/v2/actions", tags=["actions"])


def _service(request: Request) -> BulkActionsService:
    return BulkActionsService(
        request.app.state.engine,
        request.app.state.secret_box,
        org_id=request.app.state.org_id,
        executor=getattr(request.app.state, "pipeline_executor", None),
    )


def _confirmations_service(request: Request) -> ConfirmationsService:
    return ConfirmationsService(
        request.app.state.engine,
        org_id=request.app.state.org_id,
        pepper=request.app.state.token_pepper,
    )


@router.post("/{action}")
async def run_bulk_action(
    action: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    from ..errors import not_found

    if action not in ("pause_all", "resume_all"):
        raise not_found("action globale inconnue dans ce périmètre")
    service = _service(request)
    path = f"/v2/actions/{action}"
    action_ref = f"organization.{action}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        confirmations = _confirmations_service(request)
        confirmation_id = payload.get("confirmation_token") if isinstance(payload.get("confirmation_token"), str) else None

        if dry_run:
            return 200, envelope(
                before=None,
                after=None,
                verify_method="GET",
                verify_path="/v2/sources",
                dry_run={"would_apply": action},
            )

        if confirmation_id is None:
            conf_record, _approval_token = confirmations.create(
                action_ref=action_ref,
                resource_type="organization",
                resource_id=request.app.state.org_id,
                reason=f"action globale « {action} » sur toutes les sources et destinations",
                requested_by_kind=identity.actor_kind,
                requested_by_id=identity.actor_id,
            )
            raise pending_confirmation_required(
                f"confirmation requise (id={conf_record.id}) — voir /v2/confirmations/{conf_record.id}"
            )
        try:
            confirmation = confirmations.get(confirmation_id)
        except ConfirmationNotFoundError as error:
            raise wrong_confirmation("confirmation_token inconnu") from error
        if confirmation.state != "approved" or confirmation.action_ref != action_ref:
            raise wrong_confirmation(f"confirmation {confirmation_id} absente, expirée ou non approuvée pour cette action")

        try:
            result = service.pause_all() if action == "pause_all" else service.resume_all()
        except PipelineExecutorUnavailableError as error:
            raise ApiError(
                503,
                "executor_unavailable",
                str(error),
                next_action="vérifier le déploiement du control plane",
                retryable=True,
            ) from error
        confirmations.mark_used(confirmation_id)
        events = EventsService(request.app.state.engine, org_id=request.app.state.org_id)
        for item in result.pipelines["applied"]:
            events.publish(
                "pipeline.state_changed",
                {
                    "pipeline_id": item["pipeline_id"],
                    "from": item["before"]["declared_state"],
                    "to": item["after"]["declared_state"],
                    "cause": f"organization.{action}",
                },
            )
        return 200, envelope(
            before=None,
            after=result.to_dict(),
            verify_method="GET",
            verify_path="/v2/sources",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action=action_ref, resource_type="organization", resource_id=request.app.state.org_id),
        identity=identity,
    )
