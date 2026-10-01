"""Factory FastAPI de la surface ``/v2`` — monté à côté du serveur v1.

Ce module ne modifie rien de ``server.py`` (routeur ``http.server`` du
serveur v1) : ``create_v2_app`` construit une application ASGI indépendante,
qu'un processus peut servir seule (``uvicorn``) ou monter sous ``/v2`` à
côté d'un futur pont v1. Le contrat (§9.2, « Décisions du 23 septembre
2026 ») impose FastAPI pour porter OpenAPI 3.1, SSE et le futur montage MCP
nativement.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import Callable
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text as sql_text
from sqlalchemy.engine import Engine

from ..auth import AuthConfig
from .crypto import SecretBox
from .errors import ApiError, from_executor_error, invalid_configuration
from .executor.boundary_reader import BoundaryUnavailableError
from .executor.kubernetes import ExecutorError
from .executor.manifests import ManifestError
from .mcp_server import LLMS_TXT, mount_mcp
from .oidc import OidcConfig, OidcHttpClient, OidcService
from .routes import actions as actions_routes
from .routes import agent_tokens as agent_tokens_routes
from .routes import audit as audit_routes
from .routes import oidc as oidc_routes
from .routes import confirmations as confirmations_routes
from .routes import costs as costs_routes
from .routes import destinations as destinations_routes
from .routes import events as events_routes
from .routes import pipelines as pipelines_routes
from .routes import sources as sources_routes
from .routes import tables as tables_routes
from .routes import users as users_routes
from .routes import webhooks as webhooks_routes
from .services.agent_tokens import AgentTokensService
from .services.audit import AuditService
from .services.costs import CostsProviderProtocol
from .services.costs_composite import FallbackCostsProvider
from .services.costs_projection import CostsV1ProjectionAdapter
from .services.costs_snowflake import SnowflakeCreditsQueryProtocol, SnowflakeWarehouseCostsAdapter
from .services.idempotency import IdempotencyStore
from .services.logs import LogSourceProtocol
from .services.logs_kubernetes import KubernetesLogSource
from .services.observation import PipelineObservationProviderProtocol
from .services.observation_composite import CompositeObservationProvider
from .services.observation_projection import PipelineSourceSpecResolver, ProjectionRepositoryObservationAdapter
from .services.observation_refresh import ObservationRefreshService
from .services.observation_storage import PipelineStreamKeyResolver, StorageBackendObservationAdapter
from .services.pipelines import PipelineExecutorProtocol
from .services.reconciler import ReconciliationLoop, ReconcilerExecutorProtocol, run_forever
from .services.scheduler import ObservationRefreshScheduler
from .services.scheduler_lock import SchedulerLock
from .services.destination_verifier import DestinationVerifierProtocol
from .services.source_probe import SourceProbeProtocol
from .services.tables import TableDiscoveryClientProtocol
from .services.users import UsersService

API_DESCRIPTION = (
    "API de pilotage d'une réplication IBM i → Snowflake — fondation du "
    "control plane v2 (chantier 3 : schéma Postgres, sources, destinations, "
    "machine à états déclarée du pipeline, enveloppe d'action générique)."
)


def create_v2_app(
    *,
    engine: Engine,
    secret_box: SecretBox,
    org_id: str = "default",
    auth_config: AuthConfig | None = None,
    pipeline_executor: PipelineExecutorProtocol | None = None,
    table_discovery_client: TableDiscoveryClientProtocol | None = None,
    source_probe: SourceProbeProtocol | None = None,
    destination_verifier: DestinationVerifierProtocol | None = None,
    pipeline_observation_provider: PipelineObservationProviderProtocol | None = None,
    log_source: LogSourceProtocol | None = None,
    costs_provider: CostsProviderProtocol | None = None,
    # -- Câblage par défaut des adaptateurs réels (chantier « observabilité
    # v2 » suite), seulement quand la configuration le permet — sinon les
    # routes retombent sur les fournisseurs Null* déjà en place (voir
    # ``routes/pipelines.py::_observation_provider``,
    # ``routes/pipelines.py::get_pipeline_logs``, ``routes/costs.py``).
    # ``*_provider``/``log_source``/``costs_provider`` explicites priment
    # toujours sur ce câblage automatique.
    pipeline_source_spec_resolver: PipelineSourceSpecResolver | None = None,
    pipeline_stream_key_resolver: PipelineStreamKeyResolver | None = None,
    capture_storage_backend: object | None = None,
    connection_source_spec_resolver: Callable[[str], str | None] | None = None,
    kubernetes_pods_client: object | None = None,
    snowflake_credits_query: SnowflakeCreditsQueryProtocol | None = None,
    snowflake_warehouse: str | None = None,
    enable_observation_scheduler: bool = False,
    observation_scheduler_interval_seconds: float = 30.0,
    observation_scheduler_holder: str | None = None,
    token_pepper: bytes | None = None,
    session_cookie_secure: bool = False,
    require_authentication: bool = False,
    sse_max_iterations: int | None = None,
    sse_poll_interval: float = 0.05,
    reconciliation_executor: ReconcilerExecutorProtocol | None = None,
    reconciliation_interval_seconds: float | None = None,
    enable_mcp: bool = True,
    oidc_config: OidcConfig | None = None,
    oidc_http_client: OidcHttpClient | None = None,
) -> FastAPI:
    # Boucle de réconciliation (chantier 4, tâche 2) : désactivée par défaut
    # (``reconciliation_interval_seconds is None``) — sûr en test (aucune
    # tâche de fond créée) et explicite en production (l'opérateur doit
    # déclarer un intervalle). ``reconciliation_executor`` sans intervalle
    # ne démarre rien non plus : les deux sont requis ensemble.
    @asynccontextmanager
    async def _lifespan(running_app: FastAPI):
        stop_event = asyncio.Event()
        task: asyncio.Task | None = None
        if reconciliation_executor is not None and reconciliation_interval_seconds is not None:
            loop = ReconciliationLoop(engine, executor=reconciliation_executor, org_id=org_id)
            running_app.state.reconciliation_loop = loop
            task = asyncio.create_task(
                run_forever(loop, interval_seconds=reconciliation_interval_seconds, stop_event=stop_event)
            )
        # Planificateur d'observation : démarré et arrêté ici, dans le même
        # lifespan que la réconciliation — ``@app.on_event`` est ignoré dès
        # qu'un lifespan est déclaré.
        scheduler = getattr(running_app.state, "observation_scheduler", None)
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()
            if task is not None:
                stop_event.set()
                await task

    app = FastAPI(
        title="Quadringent control plane v2",
        version="0.1.0",
        description=API_DESCRIPTION,
        openapi_url="/v2/openapi.json",
        docs_url="/v2/docs",
        redoc_url="/v2/redoc",
        lifespan=_lifespan,
    )
    app.state.reconciliation_loop = None
    app.state.engine = engine
    app.state.secret_box = secret_box
    app.state.org_id = org_id
    app.state.auth_config = auth_config
    app.state.pipeline_executor = pipeline_executor
    app.state.table_discovery_client = table_discovery_client
    app.state.source_probe = source_probe
    app.state.destination_verifier = destination_verifier

    resolved_observation_provider = pipeline_observation_provider
    if resolved_observation_provider is None and pipeline_source_spec_resolver is not None:
        projection_adapter = ProjectionRepositoryObservationAdapter(pipeline_source_spec_resolver)
        if capture_storage_backend is not None and pipeline_stream_key_resolver is not None:
            storage_adapter = StorageBackendObservationAdapter(
                capture_storage_backend, pipeline_stream_key=pipeline_stream_key_resolver
            )
            resolved_observation_provider = CompositeObservationProvider(projection_adapter, storage_adapter)
        else:
            # Fournisseur partiel, mais réel : état/retard/lignes viennent du
            # document de projection v1, débit/dernière arrivée restent
            # absents (raison explicite) tant qu'un stockage de capture
            # n'est pas aussi déclaré.
            resolved_observation_provider = projection_adapter
    app.state.pipeline_observation_provider = resolved_observation_provider

    resolved_log_source = log_source
    if resolved_log_source is None and kubernetes_pods_client is not None:
        resolved_log_source = KubernetesLogSource(kubernetes_pods_client)
    app.state.log_source = resolved_log_source

    resolved_costs_provider = costs_provider
    if resolved_costs_provider is None:
        candidates: list[CostsProviderProtocol] = []
        if connection_source_spec_resolver is not None:
            candidates.append(CostsV1ProjectionAdapter(connection_source_spec_resolver))
        if snowflake_credits_query is not None and snowflake_warehouse is not None:
            try:
                from quadringent.site_config import current as _current_site

                site = _current_site()
                price = float(site.snowflake_credit_price) if site.snowflake_credit_price else None
                candidates.append(
                    SnowflakeWarehouseCostsAdapter(
                        snowflake_credits_query,
                        warehouse=snowflake_warehouse,
                        price_per_credit=price,
                        currency=site.cost_currency or "USD",
                    )
                )
            except Exception:
                # Site non configuré (`SiteConfigurationError` ou import
                # absent) : ce candidat reste indisponible, jamais un échec
                # de démarrage de l'application pour une brique optionnelle.
                pass
        if candidates:
            resolved_costs_provider = candidates[0] if len(candidates) == 1 else FallbackCostsProvider(*candidates)
    app.state.costs_provider = resolved_costs_provider

    app.state.observation_scheduler = None
    if enable_observation_scheduler and resolved_observation_provider is not None:
        refresh_service = ObservationRefreshService(
            engine, org_id=org_id, observation_provider=resolved_observation_provider
        )
        holder = observation_scheduler_holder or f"pod-{secrets.token_hex(4)}"
        lock = SchedulerLock(engine, name="observation-refresh", holder=holder)
        scheduler = ObservationRefreshScheduler(
            refresh_service, lock, interval_seconds=observation_scheduler_interval_seconds
        )
        app.state.observation_scheduler = scheduler

    app.state.idempotency_store = IdempotencyStore(engine, org_id=org_id)
    app.state.audit_service = AuditService(engine, org_id=org_id)
    # Le pepper des jetons d'agent est aussi utilisé pour les jetons
    # d'approbation de confirmation (tâche 7) — sans valeur déclarée
    # explicitement, un pepper éphémère est généré par processus (mode
    # développement) plutôt que de bloquer les confirmations ; une
    # production réelle doit déclarer QUADRINGENT_V2_TOKEN_PEPPER.
    app.state.token_pepper = token_pepper if token_pepper is not None else secrets.token_bytes(32)
    app.state.agent_tokens_service = (
        AgentTokensService(engine, org_id=org_id, pepper=token_pepper) if token_pepper is not None else None
    )
    app.state.users_service = UsersService(engine, org_id=org_id, pepper=app.state.token_pepper)
    app.state.session_cookie_name = "quadringent_session"
    app.state.session_cookie_secure = session_cookie_secure
    # Mode « authentification exigée » (tâche « auth-login ») : désactive le
    # secours anonyme-admin de ``auth.py::resolve_identity`` — voir sa
    # docstring et ``docs/api-v2.md``. ``False`` par défaut (mode
    # développement/loopback historique, inchangé).
    app.state.require_authentication = require_authentication
    # Tâche 12 (SSE) : ``sse_max_iterations`` borne le flux — ``None`` (par
    # défaut) le rend infini (production, jusqu'à déconnexion du client) ;
    # les tests passent une petite valeur pour ne jamais bloquer.
    app.state.sse_max_iterations = sse_max_iterations
    app.state.sse_poll_interval = sse_poll_interval

    @app.exception_handler(ApiError)
    async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    # Constat du 24 septembre 2026 : une ExecutorError (colonnes non
    # déclarées, Job/Deployment refusé, position de journal illisible…)
    # traversait ces routes jusqu'à FastAPI sans jamais être enveloppée —
    # 500 brute avec trace Python. Ces trois classes portent déjà un code
    # sûr (``.code``/message, jamais de détail distant) ; ici on les
    # convertit systématiquement, quelle que soit la route qui les a
    # laissé remonter, jamais au cas par cas dans chaque route.
    @app.exception_handler(ExecutorError)
    async def _executor_error_handler(_request: Request, exc: ExecutorError) -> JSONResponse:
        api_error = from_executor_error(exc)
        return JSONResponse(status_code=api_error.status_code, content=api_error.to_body())

    @app.exception_handler(BoundaryUnavailableError)
    async def _boundary_unavailable_handler(_request: Request, exc: BoundaryUnavailableError) -> JSONResponse:
        api_error = from_executor_error(exc)
        return JSONResponse(status_code=api_error.status_code, content=api_error.to_body())

    @app.exception_handler(ManifestError)
    async def _manifest_error_handler(_request: Request, exc: ManifestError) -> JSONResponse:
        api_error = invalid_configuration(str(exc))
        return JSONResponse(status_code=api_error.status_code, content=api_error.to_body())

    @app.get("/v2/healthz", include_in_schema=False)
    async def _healthz() -> JSONResponse:
        """Sonde readiness/liveness de la chart : exige une connexion Postgres
        vivante — un processus qui répond mais ne peut plus parler à la base
        ne doit pas rester marqué prêt (ni continuer à recevoir du trafic)."""

        try:
            with app.state.engine.connect() as connection:
                connection.execute(sql_text("SELECT 1"))
        except Exception:  # noqa - toute panne de connexion échoue la sonde
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    app.include_router(sources_routes.router)
    app.include_router(destinations_routes.router)
    app.include_router(costs_routes.router)
    app.include_router(pipelines_routes.router)
    app.include_router(actions_routes.router)
    app.include_router(tables_routes.sources_router)
    app.include_router(tables_routes.tables_router)
    app.include_router(audit_routes.router)
    app.include_router(confirmations_routes.router)
    app.include_router(users_routes.router)
    app.include_router(events_routes.router)
    app.include_router(webhooks_routes.router)
    if app.state.agent_tokens_service is not None:
        app.include_router(agent_tokens_routes.router)

    # OIDC (tâche 10, contrat §6.2) : optionnel, off par défaut — monté
    # seulement si l'appelant déclare explicitement ``oidc_config`` (et un
    # client réseau injecté ; jamais de réseau réel non contrôlé).
    app.state.oidc_service = None
    if oidc_config is not None:
        if oidc_http_client is None:
            raise ValueError("oidc_config déclarée sans oidc_http_client — voir OidcHttpClient (v2/oidc.py)")
        app.state.oidc_service = OidcService(oidc_config, http_client=oidc_http_client, pepper=app.state.token_pepper)
        app.include_router(oidc_routes.router)

    @app.get("/llms.txt", include_in_schema=False)
    async def _llms_txt() -> PlainTextResponse:
        return PlainTextResponse(LLMS_TXT)

    # Serveur MCP in-process (contrat §4, §9.2 tâche 18) — streamable HTTP,
    # monté à côté des routes REST, mêmes services (voir mcp_server.py).
    # Désactivable (``enable_mcp=False``) pour les tests qui ne veulent pas
    # de la dépendance ``mcp`` ou du coût du montage.
    if enable_mcp:
        mount_mcp(app)

    return app
