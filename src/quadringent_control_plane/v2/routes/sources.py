"""Routes ``/v2/sources`` — CRUD read/create/test (tâche 2 + enveloppe tâche 6)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import ApiError, invalid_request, not_found
from ..executor.diagnostic_jobs import SourceProbeUnavailableError
from ..http import AuditContext, envelope, idempotent_write
from ..services.events import EventsService
from ..services.pipelines import PipelineExecutorUnavailableError, PipelinesService
from ..services.sources import SourceNotFoundError, SourceValidationError, SourcesService

router = APIRouter(prefix="/v2/sources", tags=["sources"])


def _service(request: Request) -> SourcesService:
    return SourcesService(request.app.state.engine, request.app.state.secret_box, org_id=request.app.state.org_id)


@router.get("")
async def list_sources(request: Request, identity=Depends(require_scope("read"))) -> dict[str, object]:
    service = _service(request)
    return {"items": [record.to_dict() for record in service.list()], "next_cursor": None}


@router.get("/{source_id}")
async def get_source(
    source_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(source_id)
    except SourceNotFoundError as error:
        raise not_found("source introuvable") from error
    return record.to_dict()


@router.post("", status_code=201)
async def create_source(
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    service = _service(request)

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        secret = payload.get("secret")
        secret_value = secret.get("value") if isinstance(secret, dict) else None
        if not isinstance(secret, dict) or secret.get("kind") != "inline" or not isinstance(secret_value, str):
            raise invalid_request("secret.kind doit être 'inline' et secret.value une chaîne")
        try:
            if dry_run:
                plan = service.plan_create(
                    display_name=payload.get("display_name"),
                    ibmi_host=payload.get("ibmi_host"),
                    ibmi_user=payload.get("ibmi_user"),
                )
                return 200, envelope(
                    before=None, after=None, verify_method="GET", verify_path="/v2/sources", dry_run=plan
                )
            record = service.create(
                display_name=payload.get("display_name"),
                ibmi_host=payload.get("ibmi_host"),
                ibmi_user=payload.get("ibmi_user"),
                secret_value=secret_value,
            )
        except SourceValidationError as error:
            raise invalid_request(str(error)) from error
        return 201, envelope(
            before=None,
            after=record.to_dict(),
            verify_method="GET",
            verify_path=f"/v2/sources/{record.id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path="/v2/sources",
        store=request.app.state.idempotency_store,
        build=build,
    )


@router.post("/{source_id}/test")
async def test_source(
    source_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/sources/{source_id}/test"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        probe = getattr(request.app.state, "source_probe", None)
        # Décision d'épinglage explicite pour cette tentative — voir
        # ``WizardSource.tsx`` (« faire confiance à ce certificat ») et
        # ``SourcesService.test`` pour la logique de persistance (jamais
        # posée avant confirmation par la sonde elle-même).
        tls = payload.get("tls") if isinstance(payload, dict) else None
        tls_trust = tls.get("trust") if isinstance(tls, dict) else None
        pinned_fingerprint = tls.get("fingerprint") if isinstance(tls, dict) else None
        pinned_pem = tls.get("certificate_pem") if isinstance(tls, dict) else None
        try:
            result = service.test(
                source_id,
                probe=probe,
                tls_trust=tls_trust if isinstance(tls_trust, str) else None,
                pinned_fingerprint=pinned_fingerprint if isinstance(pinned_fingerprint, str) else None,
                pinned_pem=pinned_pem if isinstance(pinned_pem, str) else None,
            )
        except SourceNotFoundError as error:
            raise not_found("source introuvable") from error
        except SourceProbeUnavailableError as error:
            # Le Job Kubernetes de sonde n'a produit aucun résultat
            # exploitable (API indisponible, délai dépassé, journal
            # illisible — voir DiagnosticJobError) : jamais une 500 brute,
            # toujours une erreur structurée avec next_action (objectif D).
            raise ApiError(
                503,
                "executor_unavailable",
                str(error),
                next_action="vérifier le déploiement du control plane et réessayer",
                retryable=True,
            ) from error
        return 200, envelope(
            before=None, after=result, verify_method="GET", verify_path=f"/v2/sources/{source_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
    )


@router.post("/{source_id}/actions/{action}")
async def run_source_action(
    source_id: str,
    action: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    """``pause``/``resume`` d'une source (tâche 18 MCP/CLI, complétée chantier 4).

    Pose l'intention opérateur (``sources.paused_at``) **et** pause/reprend
    réellement chaque pipeline `copying`/`live` de la source via l'exécuteur
    injecté — une pause qui ne posait qu'un marqueur sans arrêter les flux
    ne correspond pas au design (§3). Une table pausée individuellement par
    l'utilisateur (``POST /v2/pipelines/{id}/actions/pause``) n'est jamais
    relancée par la reprise de la source (``PipelinesService._would_skip_scope_action``)
    — au mieux, jamais bloquant pour les autres tables de la source.
    """

    if action not in ("pause", "resume"):
        raise not_found("action de source inconnue dans ce périmètre")
    service = _service(request)
    pipelines_service = PipelinesService(request.app.state.engine)
    events = EventsService(request.app.state.engine, org_id=request.app.state.org_id)
    path = f"/v2/sources/{source_id}/actions/{action}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        try:
            before = service.get(source_id)
        except SourceNotFoundError as error:
            raise not_found("source introuvable") from error
        pipeline_ids = pipelines_service.list_pipeline_ids_for_source(source_id)
        if dry_run:
            plan = pipelines_service.plan_scope_action(pipeline_ids, action)
            return 200, envelope(
                before=None,
                after=None,
                verify_method="GET",
                verify_path=f"/v2/sources/{source_id}",
                dry_run={"would_transition": {"paused": action == "pause"}, "pipelines": plan},
            )
        executor = getattr(request.app.state, "pipeline_executor", None)
        try:
            pipeline_result = pipelines_service.apply_scope_action(pipeline_ids, action, executor=executor)
        except PipelineExecutorUnavailableError as error:
            raise ApiError(
                503,
                "executor_unavailable",
                str(error),
                next_action="vérifier le déploiement du control plane",
                retryable=True,
            ) from error
        after = service.pause(source_id) if action == "pause" else service.resume(source_id)
        for item in pipeline_result["applied"]:
            events.publish(
                "pipeline.state_changed",
                {
                    "pipeline_id": item["pipeline_id"],
                    "from": item["before"]["declared_state"],
                    "to": item["after"]["declared_state"],
                    "cause": f"source.{action}",
                },
            )
        return 200, envelope(
            before=before.to_dict(),
            after={**after.to_dict(), "pipelines": pipeline_result},
            verify_method="GET",
            verify_path=f"/v2/sources/{source_id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action=f"source.{action}", resource_type="source", resource_id=source_id),
        identity=identity,
    )
