"""Routes ``/v2/sources/{id}/tables`` et ``/v2/tables/{id}`` (tâche 4).

``refresh`` délègue toujours à ``request.app.state.table_discovery_client``
(``None`` par défaut → 503 ``store_unavailable``-like ``discovery_unavailable``,
échec fermé — jamais de sondage IBM i improvisé ici). Les tests injectent un
faux client (voir ``TableDiscoveryClientProtocol``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import ApiError, capability_unavailable, invalid_request, not_found
from ..http import envelope, idempotent_write
from ..services.pipelines import (
    AmbiguousDestinationError,
    DestinationNotFoundError,
    PipelineExecutorUnavailableError,
    PipelinesService,
)
from ..services.pipelines import TableNotFoundError as PipelineTableNotFoundError
from ..services.state_machine import ForbiddenTransitionError, UnknownStateError
from ..services.tables import (
    SourceNotFoundError,
    TableNotFoundError,
    TableValidationError,
    TablesService,
)

sources_router = APIRouter(prefix="/v2/sources", tags=["tables"])
tables_router = APIRouter(prefix="/v2/tables", tags=["tables"])


def _service(request: Request) -> TablesService:
    return TablesService(request.app.state.engine)


def _pipelines_service(request: Request) -> PipelinesService:
    return PipelinesService(request.app.state.engine)


@sources_router.get("/{source_id}/tables")
async def list_tables(
    source_id: str,
    request: Request,
    search: str | None = None,
    library: str | None = None,
    readiness: str | None = None,
    identity=Depends(require_scope("read")),
) -> dict[str, object]:
    service = _service(request)
    try:
        records = service.list(source_id, search=search, library=library, readiness=readiness)
    except SourceNotFoundError as error:
        raise not_found("source introuvable") from error
    return {"items": [record.to_dict() for record in records], "next_cursor": None}


@sources_router.post("/{source_id}/tables/refresh")
async def refresh_tables(
    source_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/sources/{source_id}/tables/refresh"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        libraries = payload.get("libraries")
        if libraries is not None and (
            not isinstance(libraries, list) or not all(isinstance(item, str) for item in libraries)
        ):
            raise invalid_request("libraries doit être une liste de chaînes")
        limit = payload.get("limit", 500)
        if not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise invalid_request("limit doit être un entier entre 1 et 5000")
        search = payload.get("search")
        if search is not None and not isinstance(search, str):
            raise invalid_request("search doit être une chaîne")
        client = getattr(request.app.state, "table_discovery_client", None)
        if dry_run:
            return 200, envelope(
                before=None,
                after=None,
                verify_method="GET",
                verify_path=f"/v2/sources/{source_id}/tables",
                dry_run={"would_refresh": True, "libraries": libraries, "limit": limit, "search": search},
            )
        if client is None:
            raise ApiError(
                503,
                "discovery_unavailable",
                "aucun client de découverte configuré pour cette source",
                next_action="vérifier le déploiement du control plane",
                retryable=True,
            )
        try:
            before = service.list(source_id)
            after = service.refresh(
                source_id,
                discovery_client=client,
                libraries=tuple(libraries) if libraries else None,
                limit=limit,
                search=search,
            )
        except SourceNotFoundError as error:
            raise not_found("source introuvable") from error
        return 200, envelope(
            before=[record.to_dict() for record in before],
            after=[record.to_dict() for record in after],
            verify_method="GET",
            verify_path=f"/v2/sources/{source_id}/tables",
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


@tables_router.post("/{table_id}/pipeline")
async def start_table_pipeline(
    table_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    """``POST /v2/tables/{id}/pipeline`` — démarrage automatique (contrat §2.3).

    Crée le pipeline s'il n'existe pas encore, puis déclenche l'évènement
    ``start`` de la machine à états via l'exécuteur injecté
    (``request.app.state.pipeline_executor``) : lecture de la position du
    journal, lancement de la copie initiale, apparition dans le lecteur de
    capture continue (voir `docs/orchestration.md`). ``destination_id`` est
    optionnel dans le corps : résolu automatiquement s'il n'existe qu'une
    seule destination pour l'organisation, sinon ``400 invalid_request``
    (jamais de choix implicite silencieux).
    """

    service = _pipelines_service(request)
    path = f"/v2/tables/{table_id}/pipeline"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        destination_id = payload.get("destination_id")
        if destination_id is not None and not isinstance(destination_id, str):
            raise invalid_request("destination_id doit être une chaîne")
        try:
            if dry_run:
                plan = service.plan_start(table_id, destination_id=destination_id)
                return 200, envelope(
                    before=None, after=None, verify_method="GET", verify_path=f"/v2/tables/{table_id}", dry_run=plan
                )
            executor = getattr(request.app.state, "pipeline_executor", None)
            before, after = service.start_table_pipeline(
                table_id, destination_id=destination_id, executor=executor
            )
        except PipelineTableNotFoundError as error:
            raise not_found("table introuvable") from error
        except (DestinationNotFoundError, AmbiguousDestinationError) as error:
            raise invalid_request(str(error)) from error
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
        return 200, envelope(
            before=before.to_dict() if before is not None else None,
            after=after.to_dict(),
            verify_method="GET",
            verify_path=f"/v2/pipelines/{after.id}",
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


@tables_router.patch("/{table_id}")
async def patch_table(
    table_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/tables/{table_id}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        dry_run = bool(payload.get("dry_run", False))
        key_strategy = payload.get("key_strategy")
        key_columns = payload.get("key_columns")
        acknowledge_rrn = payload.get("acknowledge_rrn", False)
        try:
            if dry_run:
                before = service.get(table_id)
                return 200, envelope(
                    before=before.to_dict(),
                    after=None,
                    verify_method="GET",
                    verify_path=path,
                    dry_run={
                        "would_set": {
                            "key_strategy": key_strategy,
                            "key_columns": key_columns,
                            "acknowledge_rrn": acknowledge_rrn,
                        }
                    },
                )
            before = service.get(table_id)
            after = service.choose_key(
                table_id,
                key_strategy=key_strategy,
                key_columns=key_columns,
                acknowledge_rrn=acknowledge_rrn,
            )
        except TableNotFoundError as error:
            raise not_found("table introuvable") from error
        except TableValidationError as error:
            raise invalid_request(str(error)) from error
        return 200, envelope(
            before=before.to_dict(), after=after.to_dict(), verify_method="GET", verify_path=path
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="PATCH",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
    )


@tables_router.put("/{table_id}/discovered-columns")
async def put_discovered_columns(
    table_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    """Déclare les colonnes métier (nom + type IBM i) d'une table.

    Aucune découverte automatique de type de colonne n'existe encore côté
    worker (``table_discovery.py`` ne rapporte que des métadonnées de
    table) : cette route porte la déclaration explicite requise par le
    chargeur de destination pour générer le DDL historique/miroir — jamais
    inférée depuis une autre source.
    """

    service = _service(request)
    path = f"/v2/tables/{table_id}/discovered-columns"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            before = service.get(table_id)
            after = service.set_discovered_columns(table_id, columns=payload.get("columns"))
        except TableNotFoundError as error:
            raise not_found("table introuvable") from error
        except TableValidationError as error:
            raise invalid_request(str(error)) from error
        return 200, envelope(
            before=before.to_dict(), after=after.to_dict(), verify_method="GET", verify_path=f"/v2/tables/{table_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="PUT",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
    )
