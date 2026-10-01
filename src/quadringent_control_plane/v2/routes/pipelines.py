"""Routes ``/v2/pipelines/{id}/actions/*`` (tâches 5, 6, 7).

``pause``/``resume`` s'exécutent directement. ``remove``,
``restart_initial_copy`` et ``replay`` sont des actions sensibles (tâche 7,
contrat §2.4) : sans ``confirmation_token`` valide dans le corps, elles
créent une confirmation ``pending`` et renvoient
``409 pending_confirmation_required`` plutôt que de s'exécuter. L'exécuteur
réel (Kubernetes) n'est jamais câblé : ``request.app.state.pipeline_executor``
est ``None`` par défaut (échec fermé, 503) et les tests injectent un faux
exécuteur.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import (
    ApiError,
    capability_unavailable,
    invalid_request,
    not_found,
    pending_confirmation_required,
    wrong_confirmation,
)
from ..http import AuditContext, envelope, idempotent_write
from ..services.confirmations import ConfirmationNotFoundError, ConfirmationsService
from ..services.logs import KNOWN_LEVELS, LogsService, NullLogSource
from ..services.observation import NullObservationProvider, PipelineObservationProviderProtocol
from ..services.pipelines import (
    ACTIONS,
    CONFIRMATION_REQUIRED_ACTIONS,
    InvalidListFilterError,
    PipelineExecutorUnavailableError,
    PipelineNotFoundError,
    PipelinesService,
)
from ..services.state_machine import ForbiddenTransitionError, UnknownStateError

router = APIRouter(prefix="/v2/pipelines", tags=["pipelines"])


def _service(request: Request) -> PipelinesService:
    return PipelinesService(request.app.state.engine)


def _confirmations_service(request: Request) -> ConfirmationsService:
    return ConfirmationsService(
        request.app.state.engine,
        org_id=request.app.state.org_id,
        pepper=request.app.state.token_pepper,
    )


def _observation_provider(request: Request) -> PipelineObservationProviderProtocol:
    provider = getattr(request.app.state, "pipeline_observation_provider", None)
    return provider if provider is not None else NullObservationProvider()


@router.get("")
async def list_pipelines(
    request: Request,
    state: str | None = None,
    source_id: str | None = None,
    destination_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    service = _service(request)
    try:
        records, next_cursor = service.list(
            state=state,
            source_id=source_id,
            destination_id=destination_id,
            limit=limit,
            cursor=cursor,
            observation_provider=_observation_provider(request),
        )
    except InvalidListFilterError as error:
        raise invalid_request(str(error)) from error
    return {"items": [record.to_dict() for record in records], "next_cursor": next_cursor}


@router.get("/{pipeline_id}")
async def get_pipeline(
    pipeline_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(pipeline_id)
    except PipelineNotFoundError as error:
        raise not_found("pipeline introuvable") from error
    return record.to_dict()


_METRICS_WINDOWS = ("1h", "24h")


@router.get("/{pipeline_id}/metrics")
async def get_pipeline_metrics(
    pipeline_id: str,
    request: Request,
    window: str = "1h",
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    if window not in _METRICS_WINDOWS:
        raise invalid_request("window doit être « 1h » ou « 24h »")
    service = _service(request)
    try:
        service.get(pipeline_id)  # 404 si le pipeline n'existe pas
    except PipelineNotFoundError as error:
        raise not_found("pipeline introuvable") from error
    series = _observation_provider(request).metrics(pipeline_id, window)
    return series.to_dict()


@router.get("/{pipeline_id}/logs")
async def get_pipeline_logs(
    pipeline_id: str,
    request: Request,
    since: str | None = None,
    level: str | None = None,
    correlate_incident: bool = False,
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    if level is not None and level not in KNOWN_LEVELS:
        raise invalid_request("level doit être « info », « warning » ou « error »")
    service = _service(request)
    try:
        service.get(pipeline_id)  # 404 si le pipeline n'existe pas
    except PipelineNotFoundError as error:
        raise not_found("pipeline introuvable") from error
    source = getattr(request.app.state, "log_source", None) or NullLogSource()
    entries = LogsService(source).fetch(
        pipeline_id, since=since, level=level, incident=correlate_incident
    )
    return {"items": [entry.to_dict() for entry in entries], "next_cursor": None}


@router.post("/{pipeline_id}/actions/{action}")
async def run_pipeline_action(
    pipeline_id: str,
    action: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    if action not in ACTIONS:
        raise not_found("action de pipeline inconnue dans ce périmètre")
    service = _service(request)
    path = f"/v2/pipelines/{pipeline_id}/actions/{action}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        action_ref = f"pipeline.{action}"
        confirmations = _confirmations_service(request)
        confirmation_id = payload.get("confirmation_token") if isinstance(payload.get("confirmation_token"), str) else None
        try:
            if dry_run:
                plan = service.plan_action(pipeline_id, action)
                return 200, envelope(
                    before=None,
                    after=None,
                    verify_method="GET",
                    verify_path=f"/v2/pipelines/{pipeline_id}",
                    dry_run=plan,
                )

            if action in CONFIRMATION_REQUIRED_ACTIONS:
                if confirmation_id is None:
                    service.get(pipeline_id)  # 404 si le pipeline n'existe pas
                    conf_record, _approval_token = confirmations.create(
                        action_ref=action_ref,
                        resource_type="pipeline",
                        resource_id=pipeline_id,
                        reason=f"action sensible « {action} » sur le pipeline {pipeline_id}",
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
                if (
                    confirmation.state != "approved"
                    or confirmation.resource_id != pipeline_id
                    or confirmation.action_ref != action_ref
                ):
                    raise wrong_confirmation(
                        f"confirmation {confirmation_id} absente, expirée ou non approuvée pour cette action"
                    )

            executor = getattr(request.app.state, "pipeline_executor", None)
            before, after = service.apply_action(pipeline_id, action, executor=executor)
        except PipelineNotFoundError as error:
            raise not_found("pipeline introuvable") from error
        except (ForbiddenTransitionError, UnknownStateError) as error:
            raise capability_unavailable(str(error)) from error
        except PipelineExecutorUnavailableError as error:
            raise ApiError(
                503,
                "executor_unavailable",
                str(error),
                next_action="vérifier le déploiement du control plane",
                retryable=True,
            ) from error

        if confirmation_id is not None and action in CONFIRMATION_REQUIRED_ACTIONS:
            confirmations.mark_used(confirmation_id)

        return 200, envelope(
            before=before.to_dict(),
            after=after.to_dict(),
            verify_method="GET",
            verify_path=f"/v2/pipelines/{pipeline_id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action=f"pipeline.{action}", resource_type="pipeline", resource_id=pipeline_id),
        identity=identity,
    )
