"""Serveur MCP ``/mcp`` (contrat §4 et §9.2 tâche 18) — streamable HTTP en tâche de fond.

Un seul principe organise ce module : **aucune logique métier dupliquée**.
Chaque outil MCP n'est qu'un appel HTTP à la route ``/v2`` équivalente, via
un client ``httpx`` fourni par l'appelant (``make_client``) — jamais un
appel direct à un service. C'est le même code qui traite une requête REST
d'un humain et un appel d'outil d'un agent : dry_run, Idempotency-Key,
confirmations, audit (``AuditService.record``) passent tous par
``idempotent_write`` (voir ``http.py``), pas par ce module.

Deux façons de brancher ``make_client`` :

- **Montage in-process** (``mount_mcp`` ci-dessous) : ``httpx.AsyncClient``
  sur ``httpx.ASGITransport(app=app)`` — zéro réseau, l'en-tête
  ``Authorization`` du client MCP est retransmis tel quel (``ctx.headers``),
  donc l'authentification par jeton d'agent passe par ``auth.py`` sans
  rien réécrire ici.
- **Pont CLI stdio->HTTP distant** (``quadringent mcp --stdio``,
  ``quadringent/cli/mcp_bridge.py``) : ``httpx.AsyncClient`` pointé sur
  l'URL du control plane distant, avec le jeton lu par la CLI
  (``QUADRINGENT_TOKEN``) — ``ctx.headers`` est alors ``None`` (stdio ne
  porte pas d'en-têtes HTTP), donc ce client transporte l'identité au lieu
  du contexte MCP.

``X-MCP-Client`` (lu par ``AuditService.record`` comme ``mcp_client``,
cf. ``http.py::_write_audit``) est dérivé de
``ctx.session.client_params.client_info`` — présent quel que soit le
transport (c'est un champ du protocole MCP, pas de la couche HTTP).
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from typing import Any
import uuid

from fastapi import FastAPI
import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

ClientFactory = Callable[[Any], httpx.AsyncClient]

# Contrat §5/§9.2 tâche 18 : verbes sensibles — jamais approuvés par le même
# appel qui les déclenche (sauf jeton d'agent pré-autorisé, géré côté
# ConfirmationsService/route, pas ici).
SENSITIVE_PIPELINE_ACTIONS = frozenset({"remove", "restart_initial_copy", "replay"})


def _client_label(ctx: Context | None) -> str:
    """Dérive l'étiquette ``X-MCP-Client`` du contexte MCP — jamais None ni vide.

    ``ctx`` est ``None`` pour les ressources statiques (contrat MCP : pas
    d'injection de ``Context`` sur un URI sans variable de template — voir
    ``docs_api``/``state_overview`` ci-dessous) ; l'étiquette retombe alors
    sur ``"resource-read"``.
    """

    if ctx is None:
        return "resource-read"
    try:
        session = ctx.session
        params = session.client_params
        if params is not None and params.client_info is not None:
            info = params.client_info
            version = getattr(info, "version", "") or ""
            return f"{info.name}/{version}".rstrip("/")
    except Exception:  # défensif — un client mal formé ne doit jamais casser l'appel outil
        pass
    return "unknown-mcp-client"


def _idempotency_key() -> str:
    return f"mcp-{uuid.uuid4().hex}"


async def _call(
    ctx: Context | None,
    make_client: ClientFactory,
    method: str,
    path: str,
    *,
    json_body: dict[str, object] | None = None,
    params: dict[str, object] | None = None,
    idempotent: bool = False,
) -> dict[str, object]:
    """Appelle la route ``/v2`` équivalente et renvoie son corps JSON tel quel.

    Une erreur HTTP (4xx/5xx) n'est jamais convertie en exception protocole
    MCP : le corps ``{"error": {...}}`` de l'enveloppe standard (§2.6) est
    renvoyé à l'agent tel quel, pour qu'il puisse décider (retryable,
    next_action) sans avoir à parser un message d'erreur protocole opaque.
    """

    headers = {"X-MCP-Client": _client_label(ctx)}
    if idempotent:
        headers["Idempotency-Key"] = _idempotency_key()
    async with make_client(ctx) as client:
        response = await client.request(method, path, json=json_body, params=params, headers=headers)
    try:
        return response.json()
    except ValueError:
        return {"error": {"code": "invalid_response", "message": response.text[:2000]}}


def build_mcp_server(*, make_client: ClientFactory, name: str = "quadringent-control-plane") -> MCPServer:
    """Construit le serveur MCP — outils, ressources, sans état applicatif propre.

    ``make_client(ctx)`` est appelé à chaque outil/ressource : voir la
    docstring du module pour les deux implémentations (montage in-process,
    pont CLI stdio).
    """

    mcp = MCPServer(
        name=name,
        title="Quadringent — control plane de réplication IBM i -> Snowflake",
        instructions=(
            "Pilote une réplication IBM i (Db2 for i) -> Snowflake : sources, "
            "tables découvertes, pipelines (source x table x destination), "
            "confirmations pour les actions sensibles. Toujours essayer "
            "dry_run=true avant une action d'écriture pour prévisualiser "
            "l'effet ; les actions sensibles (remove, restart_initial_copy, "
            "replay, pause_all, resume_all) renvoient "
            "{'status': 'pending_confirmation', 'confirmation_id', "
            "'approve_url'} plutôt que de s'exécuter — un humain (ou un "
            "jeton d'agent pré-autorisé) doit approuver via "
            "list_pending_confirmations puis POST /v2/confirmations/{id}/approve."
        ),
    )

    # --- Sources ---------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def list_sources(ctx: Context) -> dict[str, object]:
        """Liste les sources IBM i déclarées (jamais le secret en clair)."""

        return await _call(ctx, make_client, "GET", "/v2/sources")

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    async def test_source(source_id: str, ctx: Context) -> dict[str, object]:
        """Vérifie qu'une source existe et que son secret déchiffre correctement."""

        return await _call(ctx, make_client, "POST", f"/v2/sources/{source_id}/test", json_body={}, idempotent=True)

    @mcp.tool()
    async def pause_source(source_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Met en pause une source (n'agit pas sur les pipelines — dry_run supporté)."""

        return await _call(
            ctx, make_client, "POST", f"/v2/sources/{source_id}/actions/pause",
            json_body={"dry_run": dry_run}, idempotent=True,
        )

    @mcp.tool()
    async def resume_source(source_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Reprend une source mise en pause — dry_run supporté."""

        return await _call(
            ctx, make_client, "POST", f"/v2/sources/{source_id}/actions/resume",
            json_body={"dry_run": dry_run}, idempotent=True,
        )

    # --- Destinations ------------------------------------------------------

    @mcp.tool()
    async def pause_destination(destination_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Met en pause une destination Snowflake — dry_run supporté."""

        return await _call(
            ctx, make_client, "POST", f"/v2/destinations/{destination_id}/actions/pause",
            json_body={"dry_run": dry_run}, idempotent=True,
        )

    @mcp.tool()
    async def resume_destination(destination_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Reprend une destination Snowflake mise en pause — dry_run supporté."""

        return await _call(
            ctx, make_client, "POST", f"/v2/destinations/{destination_id}/actions/resume",
            json_body={"dry_run": dry_run}, idempotent=True,
        )

    @mcp.tool(annotations=ToolAnnotations(idempotent_hint=True))
    async def verify_destination(destination_id: str, ctx: Context) -> dict[str, object]:
        """Vérifie la connexion Snowflake (paire de clés), le rôle/warehouse/base/
        schémas déclarés et les droits requis par le chargeur pour une destination."""

        return await _call(
            ctx, make_client, "POST", f"/v2/destinations/{destination_id}/verify",
            json_body={}, idempotent=True,
        )

    # --- Tables --------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_tables(
        source_id: str,
        ctx: Context,
        search: str | None = None,
        library: str | None = None,
        readiness: str | None = None,
    ) -> dict[str, object]:
        """Liste les tables découvertes d'une source, avec leur état de préparation (readiness)."""

        params = {k: v for k, v in {"search": search, "library": library, "readiness": readiness}.items() if v}
        return await _call(ctx, make_client, "GET", f"/v2/sources/{source_id}/tables", params=params)

    @mcp.tool()
    async def refresh_tables(source_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Relance la découverte de tables sur la source (catalogue IBM i) — dry_run supporté."""

        return await _call(
            ctx, make_client, "POST", f"/v2/sources/{source_id}/tables/refresh",
            json_body={"dry_run": dry_run}, idempotent=True,
        )

    @mcp.tool()
    async def choose_table_key(
        table_id: str,
        ctx: Context,
        key_strategy: str | None = None,
        key_columns: list[str] | None = None,
    ) -> dict[str, object]:
        """Déclare la stratégie de clé d'une table découverte (``PATCH /v2/tables/{id}``)."""

        body: dict[str, object] = {}
        if key_strategy is not None:
            body["key_strategy"] = key_strategy
        if key_columns is not None:
            body["key_columns"] = key_columns
        return await _call(ctx, make_client, "PATCH", f"/v2/tables/{table_id}", json_body=body, idempotent=True)

    # --- Pipelines -------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def list_pipelines(ctx: Context) -> dict[str, object]:
        """Liste tous les pipelines (source x table x destination) et leur état déclaré."""

        return await _call(ctx, make_client, "GET", "/v2/pipelines")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def get_pipeline(pipeline_id: str, ctx: Context) -> dict[str, object]:
        """Détail d'un pipeline (état déclaré)."""

        return await _call(ctx, make_client, "GET", f"/v2/pipelines/{pipeline_id}")

    async def _pipeline_action(
        pipeline_id: str,
        action: str,
        ctx: Context,
        *,
        dry_run: bool,
        confirmation_token: str | None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {"dry_run": dry_run, **(extra or {})}
        if confirmation_token:
            body["confirmation_token"] = confirmation_token
        result = await _call(
            ctx, make_client, "POST", f"/v2/pipelines/{pipeline_id}/actions/{action}",
            json_body=body, idempotent=True,
        )
        error = result.get("error") if isinstance(result, dict) else None
        if isinstance(error, dict) and error.get("code") == "pending_confirmation_required":
            confirmation_id = _extract_confirmation_id(str(error.get("message", "")))
            return {
                "status": "pending_confirmation",
                "confirmation_id": confirmation_id,
                "approve_url": f"/v2/confirmations/{confirmation_id}/approve" if confirmation_id else None,
                "reason": error.get("message"),
            }
        return result

    @mcp.tool()
    async def pause_pipeline(pipeline_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Met en pause un pipeline — dry_run supporté."""

        return await _pipeline_action(pipeline_id, "pause", ctx, dry_run=dry_run, confirmation_token=None)

    @mcp.tool()
    async def resume_pipeline(pipeline_id: str, ctx: Context, dry_run: bool = False) -> dict[str, object]:
        """Reprend un pipeline en pause — dry_run supporté."""

        return await _pipeline_action(pipeline_id, "resume", ctx, dry_run=dry_run, confirmation_token=None)

    @mcp.tool()
    async def restart_initial_copy(
        pipeline_id: str, ctx: Context, dry_run: bool = False, confirmation_token: str | None = None
    ) -> dict[str, object]:
        """Relance la copie initiale complète d'un pipeline — action sensible, confirmation requise."""

        return await _pipeline_action(
            pipeline_id, "restart_initial_copy", ctx, dry_run=dry_run, confirmation_token=confirmation_token
        )

    @mcp.tool()
    async def replay_journal_range(
        pipeline_id: str,
        ctx: Context,
        dry_run: bool = False,
        confirmation_token: str | None = None,
        journal_from: str | None = None,
        journal_to: str | None = None,
    ) -> dict[str, object]:
        """Rejoue une plage du journal IBM i sur un pipeline — action sensible, confirmation requise."""

        extra = {k: v for k, v in {"journal_from": journal_from, "journal_to": journal_to}.items() if v}
        return await _pipeline_action(
            pipeline_id, "replay", ctx, dry_run=dry_run, confirmation_token=confirmation_token, extra=extra
        )

    @mcp.tool()
    async def remove_table(
        pipeline_id: str, ctx: Context, dry_run: bool = False, confirmation_token: str | None = None
    ) -> dict[str, object]:
        """Retire une table de la réplication (arrête son pipeline) — action sensible, confirmation requise."""

        return await _pipeline_action(
            pipeline_id, "remove", ctx, dry_run=dry_run, confirmation_token=confirmation_token
        )

    # --- Actions globales --------------------------------------------------

    async def _bulk_action(action: str, ctx: Context, *, dry_run: bool, confirmation_token: str | None) -> dict[str, object]:
        body: dict[str, object] = {"dry_run": dry_run}
        if confirmation_token:
            body["confirmation_token"] = confirmation_token
        result = await _call(ctx, make_client, "POST", f"/v2/actions/{action}", json_body=body, idempotent=True)
        error = result.get("error") if isinstance(result, dict) else None
        if isinstance(error, dict) and error.get("code") == "pending_confirmation_required":
            confirmation_id = _extract_confirmation_id(str(error.get("message", "")))
            return {
                "status": "pending_confirmation",
                "confirmation_id": confirmation_id,
                "approve_url": f"/v2/confirmations/{confirmation_id}/approve" if confirmation_id else None,
                "reason": error.get("message"),
            }
        return result

    @mcp.tool()
    async def pause_all(ctx: Context, dry_run: bool = False, confirmation_token: str | None = None) -> dict[str, object]:
        """Met en pause toutes les sources et destinations — action sensible, confirmation requise."""

        return await _bulk_action("pause_all", ctx, dry_run=dry_run, confirmation_token=confirmation_token)

    @mcp.tool()
    async def resume_all(ctx: Context, dry_run: bool = False, confirmation_token: str | None = None) -> dict[str, object]:
        """Reprend toutes les sources et destinations — action sensible, confirmation requise."""

        return await _bulk_action("resume_all", ctx, dry_run=dry_run, confirmation_token=confirmation_token)

    # --- Confirmations, coûts, audit ---------------------------------------

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def list_pending_confirmations(ctx: Context) -> dict[str, object]:
        """Liste les confirmations en attente d'approbation humaine (ou jeton pré-autorisé)."""

        return await _call(ctx, make_client, "GET", "/v2/confirmations", params={"state": "pending"})

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def get_costs(ctx: Context) -> dict[str, object]:  # noqa: ARG001 — signature uniforme des outils
        """Coûts mesurés/estimés — ``absent`` : aucun service de coûts n'est câblé sur /v2 dans ce chantier."""

        return {"status": "absent", "reason": "aucun service de coûts /v2 câblé dans ce chantier (voir costs.py v1)"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
    async def get_audit(
        ctx: Context,
        actor_kind: str | None = None,
        resource_type: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Interroge le journal d'audit (``actor_kind``: human|agent ; filtre optionnel par ressource)."""

        params = {
            k: v
            for k, v in {"actor_kind": actor_kind, "resource_type": resource_type, "limit": limit}.items()
            if v is not None
        }
        return await _call(ctx, make_client, "GET", "/v2/audit", params=params)

    # --- Ressources ----------------------------------------------------

    @mcp.resource("quadringent://docs/api")
    async def docs_api() -> str:
        """Le document OpenAPI 3.1 complet de ``/v2`` (JSON, sérialisé en texte).

        Pas de paramètre ``Context`` : un URI sans variable de template ne
        peut pas en recevoir un dans ce SDK (contrat MCP) ; ``make_client``
        est donc appelé avec ``ctx=None`` (voir ``_client_label``).
        """

        import json

        body = await _call(None, make_client, "GET", "/v2/openapi.json")
        return json.dumps(body, ensure_ascii=False)

    @mcp.resource("quadringent://docs/errors")
    def docs_errors() -> str:
        """Catalogue des codes d'erreur ``/v2`` (§2.6 du contrat)."""

        return _ERRORS_CATALOG

    @mcp.resource("quadringent://docs/llms.txt")
    def docs_llms_txt() -> str:
        """Point d'entrée court pour un agent découvrant ce control plane (format llms.txt)."""

        return LLMS_TXT

    @mcp.resource("quadringent://state/overview")
    async def state_overview() -> str:
        """Vue d'ensemble courante : sources, destinations, pipelines, confirmations en attente."""

        import json

        sources = await _call(None, make_client, "GET", "/v2/sources")
        destinations = await _call(None, make_client, "GET", "/v2/destinations")
        pipelines = await _call(None, make_client, "GET", "/v2/pipelines")
        pending = await _call(None, make_client, "GET", "/v2/confirmations", params={"state": "pending"})
        return json.dumps(
            {
                "sources": sources.get("items", sources),
                "destinations": destinations.get("items", destinations),
                "pipelines": pipelines.get("items", pipelines),
                "pending_confirmations": pending.get("items", pending),
            },
            ensure_ascii=False,
        )

    return mcp


def in_process_client_factory(app: FastAPI) -> ClientFactory:
    """``make_client`` pour un montage ``/mcp`` in-process (§4 du contrat).

    Zéro réseau (``httpx.ASGITransport``) ; retransmet l'en-tête
    ``Authorization`` du client MCP tel quel — l'authentification par
    jeton d'agent (``auth.py::resolve_identity``) s'applique donc sans
    rien réécrire ici. Un nouveau client est construit à chaque appel
    (``_call`` le ferme via ``async with``) : c'est volontaire, voir la
    docstring du module (contrat ``make_client``).
    """

    def make_client(ctx: Context | None) -> httpx.AsyncClient:
        forwarded: dict[str, str] = {}
        headers: dict[str, str] = {}
        if ctx is not None:
            try:
                headers = dict(ctx.headers or {})
            except ValueError:
                # ``Context`` construite hors d'une requête réelle (ex. appel
                # direct ``MCPServer.call_tool`` en test) — aucun en-tête à
                # transmettre, comportement identique à ``ctx=None``.
                headers = {}
        authorization = headers.get("authorization")
        if authorization:
            forwarded["Authorization"] = authorization
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://mcp.internal",
            headers=forwarded,
        )

    return make_client


def mount_mcp(
    app: FastAPI, *, path: str = "/mcp", transport_security: TransportSecuritySettings | None = None
) -> MCPServer:
    """Monte le serveur MCP dans ``app`` sous ``path`` (streamable HTTP, défaut ``/mcp``).

    Le cycle de vie du gestionnaire de session MCP (``mcp.session_manager``)
    doit tourner dans le lifespan de l'application qui sert la requête
    ASGI — jamais celui d'une sous-application montée (elle ne démarre
    jamais, cf. doc SDK ``docs/run/asgi.md`` « The host app owns the
    lifespan »). ``create_v2_app`` n'avait jusqu'ici aucun lifespan propre ;
    celui-ci se contente d'entrer ``mcp.session_manager.run()``.

    La protection anti-DNS-rebinding du SDK (vérification de l'en-tête
    ``Host``) est désactivée par défaut : la frontière de sécurité réelle
    de ce montage est l'authentification par jeton d'agent (``auth.py``),
    pas le nom d'hôte présenté — un ingress/Service Kubernetes peut server
    ``/mcp`` sous des noms d'hôte variés selon l'environnement. Un
    déploiement qui expose ``/mcp`` directement sur une interface loopback
    partagée par plusieurs origines non authentifiées doit passer son
    propre ``transport_security``.
    """

    mcp = build_mcp_server(make_client=in_process_client_factory(app))
    security = transport_security or TransportSecuritySettings(enable_dns_rebinding_protection=False)
    sub_app = mcp.streamable_http_app(streamable_http_path="/", transport_security=security)
    app.mount(path, sub_app)

    previous_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan(started_app: FastAPI):
        async with mcp.session_manager.run():
            async with previous_lifespan(started_app) as state:
                yield state

    app.router.lifespan_context = _lifespan
    app.state.mcp_server = mcp
    return mcp


def _extract_confirmation_id(message: str) -> str | None:
    """Extrait ``id=...`` du message d'erreur ``pending_confirmation_required`` (voir ``errors.py``)."""

    marker = "id="
    if marker not in message:
        return None
    tail = message.split(marker, 1)[1]
    return tail.split(")", 1)[0].strip() or None


_ERRORS_CATALOG = """\
# Catalogue des codes d'erreur /v2 (contrat §2.6)

Toute erreur /v2 a la forme :

    {"error": {"code", "message", "next_action", "retryable"}}

Codes stables (voir errors.py) :

- invalid_request (400, non rejouable) — corps ou paramètres hors contrat.
- idempotency_key_conflict (409, non rejouable) — même clé, corps différent.
- not_found (404, non rejouable) — identifiant inconnu.
- store_unavailable (503, rejouable) — magasin Postgres indisponible.
- insufficient_role (403, non rejouable) — scope insuffisant pour l'identité.
- capability_unavailable (409, non rejouable) — transition d'état refusée
  par la machine à états, ou capacité absente (ex. exécuteur non câblé).
- action_in_progress (409, rejouable) — une action conflictuelle est déjà en cours.
- pending_confirmation_required (409, non rejouable) — action sensible sans
  confirmation valide ; voir /v2/confirmations/{id}.
- wrong_confirmation (403, non rejouable) — confirmation_token inconnu,
  expiré, ou déjà utilisé.
- wrong_environment (403, non rejouable) — ressource hors du périmètre
  déclaré du jeton d'agent (source_restriction).
"""

LLMS_TXT = """\
# Quadringent control plane

> Pilotage d'une réplication IBM i (Db2 for i) -> Snowflake : sources,
> tables découvertes, pipelines, confirmations pour les actions sensibles.

## Démarrage

1. list_sources, puis test_source(source_id) pour vérifier une source.
2. list_tables(source_id) pour voir les tables découvertes et leur readiness ;
   refresh_tables(source_id) si le catalogue est périmé.
3. choose_table_key(table_id, ...) pour déclarer la clé d'une table.
4. list_pipelines / get_pipeline(pipeline_id) pour l'état déclaré.
5. Actions d'écriture : toujours essayer dry_run=true d'abord.
6. Actions sensibles (remove_table, restart_initial_copy,
   replay_journal_range, pause_all, resume_all) renvoient
   {"status": "pending_confirmation", "confirmation_id", "approve_url"} —
   un humain (ou un jeton d'agent pré-autorisé) doit approuver via
   POST /v2/confirmations/{id}/approve avant de rejouer l'action avec
   confirmation_token.

## Documents

- quadringent://docs/api — OpenAPI 3.1 complet.
- quadringent://docs/errors — catalogue des codes d'erreur.
- quadringent://state/overview — vue d'ensemble courante.
"""
