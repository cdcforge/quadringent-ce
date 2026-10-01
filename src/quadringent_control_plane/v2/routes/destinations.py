"""Routes ``/v2/destinations`` — création (clés RSA + script SQL, tâche 3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import ApiError, invalid_request, not_found
from ..http import AuditContext, envelope, idempotent_write
from ..services.destinations import (
    DestinationNotFoundError,
    DestinationValidationError,
    DestinationsService,
)
from ..services.events import EventsService
from ..services.pipelines import PipelineExecutorUnavailableError, PipelinesService

router = APIRouter(prefix="/v2/destinations", tags=["destinations"])


def _service(request: Request) -> DestinationsService:
    return DestinationsService(
        request.app.state.engine, request.app.state.secret_box, org_id=request.app.state.org_id
    )


@router.get("")
async def list_destinations(request: Request, identity=Depends(require_scope("read"))) -> dict[str, object]:
    service = _service(request)
    return {"items": [record.to_dict() for record in service.list()], "next_cursor": None}


@router.get("/{destination_id}")
async def get_destination(
    destination_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(destination_id)
    except DestinationNotFoundError as error:
        raise not_found("destination introuvable") from error
    return record.to_dict()


@router.get("/{destination_id}/setup-script")
async def get_destination_setup_script(
    destination_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    """Script SQL de mise en service, relisible : il ne porte que la clé
    publique (la clé privée n'est jamais relisible)."""
    service = _service(request)
    try:
        return {"destination_id": destination_id, "setup_script": service.setup_script(destination_id)}
    except DestinationNotFoundError as error:
        raise not_found("destination introuvable") from error


@router.post("", status_code=201)
async def create_destination(
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    service = _service(request)

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record, private_key_pem = service.create(
                snowflake_account=payload.get("snowflake_account"),
                destination_mode=payload.get("destination_mode", "copy_merge"),
                mirror_option=payload.get("mirror_option", "loader_merge"),
                destination_database=payload.get("destination_database", "QUADRINGENT"),
                destination_schema=payload.get("destination_schema"),
            )
        except DestinationValidationError as error:
            raise invalid_request(str(error)) from error
        after = record.to_dict()
        # La clé privée et le script sont montrés une seule fois, à la
        # création — jamais relisibles ensuite (même discipline que
        # secret_ref dans connections.py).
        after = {**after, "private_key_pem": private_key_pem, "setup_script": service.setup_script(record.id)}
        return 201, envelope(
            before=None,
            after=after,
            verify_method="GET",
            verify_path=f"/v2/destinations/{record.id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path="/v2/destinations",
        store=request.app.state.idempotency_store,
        build=build,
    )


@router.post("/{destination_id}/verify")
async def verify_destination(
    destination_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    """Vérifie la connexion Snowflake (paire de clés), le rôle/warehouse/base/
    schémas déclarés et les droits requis par le chargeur (chantier
    « backend gaps », item 1) — même style que
    ``routes/sources.py::test_source`` : une sonde injectable
    (``request.app.state.destination_verifier``), jamais improvisée ici.
    """

    service = _service(request)
    path = f"/v2/destinations/{destination_id}/verify"

    def build(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        verifier = getattr(request.app.state, "destination_verifier", None)
        try:
            result = service.verify(destination_id, verifier=verifier)
        except DestinationNotFoundError as error:
            raise not_found("destination introuvable") from error
        return 200, envelope(
            before=None,
            after=result,
            verify_method="GET",
            verify_path=f"/v2/destinations/{destination_id}",
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


@router.post("/{destination_id}/actions/{action}")
async def run_destination_action(
    destination_id: str,
    action: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    """``pause``/``resume`` d'une destination (tâche 18 MCP/CLI, complétée chantier 4).

    Même discipline que ``routes/sources.py::run_source_action`` : pose
    l'intention opérateur **et** pause/reprend réellement chaque pipeline
    `copying`/`live` pointant vers cette destination, sans jamais relancer
    une table pausée individuellement.
    """

    if action not in ("pause", "resume"):
        raise not_found("action de destination inconnue dans ce périmètre")
    service = _service(request)
    pipelines_service = PipelinesService(request.app.state.engine)
    events = EventsService(request.app.state.engine, org_id=request.app.state.org_id)
    path = f"/v2/destinations/{destination_id}/actions/{action}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        try:
            before = service.get(destination_id)
        except DestinationNotFoundError as error:
            raise not_found("destination introuvable") from error
        pipeline_ids = pipelines_service.list_pipeline_ids_for_destination(destination_id)
        if dry_run:
            plan = pipelines_service.plan_scope_action(pipeline_ids, action)
            return 200, envelope(
                before=None,
                after=None,
                verify_method="GET",
                verify_path=f"/v2/destinations/{destination_id}",
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
        after = service.pause(destination_id) if action == "pause" else service.resume(destination_id)
        for item in pipeline_result["applied"]:
            events.publish(
                "pipeline.state_changed",
                {
                    "pipeline_id": item["pipeline_id"],
                    "from": item["before"]["declared_state"],
                    "to": item["after"]["declared_state"],
                    "cause": f"destination.{action}",
                },
            )
        return 200, envelope(
            before=before.to_dict(),
            after={**after.to_dict(), "pipelines": pipeline_result},
            verify_method="GET",
            verify_path=f"/v2/destinations/{destination_id}",
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action=f"destination.{action}", resource_type="destination", resource_id=destination_id),
        identity=identity,
    )
