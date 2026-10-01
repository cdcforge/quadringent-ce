"""Sous-commandes ``/v2`` de la CLI ``quadringent`` (contrat §5, tâche 19).

Un seul principe : chaque sous-commande construit un chemin/corps de
requête et délègue à ``ApiClient`` — aucune logique métier ici, tout est
côté serveur (même discipline que ``mcp_server.py``, côté MCP). La sortie
est toujours du JSON sur stdout ; le code de sortie est celui d'
``ApiResult.exit_code``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence, TextIO

from .api_client import ApiClient, ApiResult, ClientConfig, ConfigError, EXIT_CONFIG_ERROR, load_config


def add_v2_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Ajoute les sous-commandes ``/v2`` au parseur ``quadringent`` existant."""

    _add_sources(subparsers)
    _add_destinations(subparsers)
    _add_tables(subparsers)
    _add_pipelines(subparsers)
    _add_actions(subparsers)
    _add_confirmations(subparsers)
    _add_tokens(subparsers)
    _add_users(subparsers)
    _add_audit(subparsers)
    _add_events(subparsers)
    _add_webhooks(subparsers)


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Prévisualise l'effet sans l'appliquer")


def _add_idempotency_key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--idempotency-key", default=None, help="Réutilise cette clé (défaut : générée et affichée)"
    )


def _add_confirmation_token(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirmation-token", default=None, help="Confirmation approuvée à rejouer")


# --- sources -------------------------------------------------------------


def _add_sources(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("sources", help="Sources IBM i")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="Liste les sources").set_defaults(handler=_cmd_sources_list)

    get = sub.add_parser("get", help="Détail d'une source")
    get.add_argument("source_id")
    get.set_defaults(handler=_cmd_sources_get)

    test = sub.add_parser("test", help="Vérifie qu'une source déchiffre son secret")
    test.add_argument("source_id")
    _add_idempotency_key(test)
    test.set_defaults(handler=_cmd_sources_test)

    for verb in ("pause", "resume"):
        action = sub.add_parser(verb, help=f"{verb.capitalize()} une source")
        action.add_argument("source_id")
        _add_dry_run(action)
        _add_idempotency_key(action)
        action.set_defaults(handler=_cmd_sources_action, action=verb)


def _cmd_sources_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get("/v2/sources")


def _cmd_sources_get(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get(f"/v2/sources/{args.source_id}")


def _cmd_sources_test(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write("POST", f"/v2/sources/{args.source_id}/test", json_body={}, idempotency_key=args.idempotency_key)


def _cmd_sources_action(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST",
        f"/v2/sources/{args.source_id}/actions/{args.action}",
        json_body={"dry_run": args.dry_run},
        idempotency_key=args.idempotency_key,
    )


# --- destinations ----------------------------------------------------------


def _add_destinations(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("destinations", help="Destinations Snowflake")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="Liste les destinations").set_defaults(handler=_cmd_destinations_list)

    get = sub.add_parser("get", help="Détail d'une destination")
    get.add_argument("destination_id")
    get.set_defaults(handler=_cmd_destinations_get)

    for verb in ("pause", "resume"):
        action = sub.add_parser(verb, help=f"{verb.capitalize()} une destination")
        action.add_argument("destination_id")
        _add_dry_run(action)
        _add_idempotency_key(action)
        action.set_defaults(handler=_cmd_destinations_action, action=verb)


def _cmd_destinations_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get("/v2/destinations")


def _cmd_destinations_get(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get(f"/v2/destinations/{args.destination_id}")


def _cmd_destinations_action(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST",
        f"/v2/destinations/{args.destination_id}/actions/{args.action}",
        json_body={"dry_run": args.dry_run},
        idempotency_key=args.idempotency_key,
    )


# --- tables ----------------------------------------------------------------


def _add_tables(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tables", help="Tables découvertes")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    listp = sub.add_parser("list", help="Liste les tables d'une source")
    listp.add_argument("source_id")
    listp.add_argument("--search", default=None)
    listp.add_argument("--library", default=None)
    listp.add_argument("--readiness", default=None)
    listp.set_defaults(handler=_cmd_tables_list)

    refresh = sub.add_parser("refresh", help="Relance la découverte de tables")
    refresh.add_argument("source_id")
    _add_dry_run(refresh)
    _add_idempotency_key(refresh)
    refresh.set_defaults(handler=_cmd_tables_refresh)

    choose = sub.add_parser("choose-key", help="Déclare la stratégie de clé d'une table")
    choose.add_argument("table_id")
    choose.add_argument("--key-strategy", required=True, choices=("primary", "unique_index", "rrn"))
    choose.add_argument("--key-column", action="append", dest="key_columns", default=None)
    choose.add_argument("--acknowledge-rrn", action="store_true")
    _add_dry_run(choose)
    _add_idempotency_key(choose)
    choose.set_defaults(handler=_cmd_tables_choose_key)


def _cmd_tables_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    params = {k: v for k, v in {"search": args.search, "library": args.library, "readiness": args.readiness}.items() if v}
    return client.get(f"/v2/sources/{args.source_id}/tables", params=params)


def _cmd_tables_refresh(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST",
        f"/v2/sources/{args.source_id}/tables/refresh",
        json_body={"dry_run": args.dry_run},
        idempotency_key=args.idempotency_key,
    )


def _cmd_tables_choose_key(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    body: dict[str, object] = {"dry_run": args.dry_run, "key_strategy": args.key_strategy}
    if args.key_columns:
        body["key_columns"] = args.key_columns
    if args.acknowledge_rrn:
        body["acknowledge_rrn"] = True
    return client.write("PATCH", f"/v2/tables/{args.table_id}", json_body=body, idempotency_key=args.idempotency_key)


# --- pipelines ---------------------------------------------------------


_PIPELINE_ACTIONS = ("pause", "resume", "restart-initial-copy", "replay", "remove")


def _add_pipelines(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("pipelines", help="Pipelines source x table x destination")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="Liste les pipelines").set_defaults(handler=_cmd_pipelines_list)

    get = sub.add_parser("get", help="Détail d'un pipeline")
    get.add_argument("pipeline_id")
    get.set_defaults(handler=_cmd_pipelines_get)

    for verb in _PIPELINE_ACTIONS:
        action = sub.add_parser(verb, help=f"Action « {verb} » sur un pipeline")
        action.add_argument("pipeline_id")
        _add_dry_run(action)
        _add_idempotency_key(action)
        _add_confirmation_token(action)
        if verb == "replay":
            action.add_argument("--journal-from", default=None)
            action.add_argument("--journal-to", default=None)
        action.set_defaults(handler=_cmd_pipelines_action, action=verb.replace("-", "_"))


def _cmd_pipelines_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get("/v2/pipelines")


def _cmd_pipelines_get(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get(f"/v2/pipelines/{args.pipeline_id}")


def _cmd_pipelines_action(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    body: dict[str, object] = {"dry_run": args.dry_run}
    if args.confirmation_token:
        body["confirmation_token"] = args.confirmation_token
    if args.action == "replay":
        if getattr(args, "journal_from", None):
            body["journal_from"] = args.journal_from
        if getattr(args, "journal_to", None):
            body["journal_to"] = args.journal_to
    return client.write(
        "POST",
        f"/v2/pipelines/{args.pipeline_id}/actions/{args.action}",
        json_body=body,
        idempotency_key=args.idempotency_key,
    )


# --- actions globales (pause_all/resume_all) --------------------------


def _add_actions(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("actions", help="Actions globales (toutes sources/destinations)")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    for verb in ("pause-all", "resume-all"):
        action = sub.add_parser(verb, help=f"{verb.replace('-', ' ').capitalize()}")
        _add_dry_run(action)
        _add_idempotency_key(action)
        _add_confirmation_token(action)
        action.set_defaults(handler=_cmd_actions_bulk, action=verb.replace("-", "_"))


def _cmd_actions_bulk(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    body: dict[str, object] = {"dry_run": args.dry_run}
    if args.confirmation_token:
        body["confirmation_token"] = args.confirmation_token
    return client.write("POST", f"/v2/actions/{args.action}", json_body=body, idempotency_key=args.idempotency_key)


# --- confirmations -------------------------------------------------------


def _add_confirmations(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("confirmations", help="Confirmations d'actions sensibles")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    listp = sub.add_parser("list", help="Liste les confirmations")
    listp.add_argument("--state", default=None, choices=("pending", "approved", "rejected", "expired", "used"))
    listp.set_defaults(handler=_cmd_confirmations_list)

    for verb in ("approve", "reject"):
        action = sub.add_parser(verb, help=f"{verb.capitalize()} une confirmation")
        action.add_argument("confirmation_id")
        _add_idempotency_key(action)
        action.set_defaults(handler=_cmd_confirmations_action, action=verb)


def _cmd_confirmations_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    params = {"state": args.state} if args.state else None
    return client.get("/v2/confirmations", params=params)


def _cmd_confirmations_action(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST",
        f"/v2/confirmations/{args.confirmation_id}/{args.action}",
        json_body={},
        idempotency_key=args.idempotency_key,
    )


# --- tokens (agent tokens) ------------------------------------------------


def _add_tokens(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tokens", help="Jetons d'agent")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="Liste les jetons d'agent").set_defaults(handler=_cmd_tokens_list)

    create = sub.add_parser("create", help="Crée un jeton d'agent (affiché une seule fois)")
    create.add_argument("--name", required=True)
    create.add_argument("--scope", required=True, choices=("read", "operate", "admin"))
    create.add_argument("--source-restriction", action="append", default=None)
    create.add_argument("--pre-authorized-action", action="append", dest="pre_authorized_actions", default=None)
    create.add_argument("--never-expires", action="store_true")
    create.add_argument("--expires-at", default=None, help="ISO 8601 — requis sauf --never-expires")
    _add_idempotency_key(create)
    create.set_defaults(handler=_cmd_tokens_create)

    rotate = sub.add_parser("rotate", help="Fait pivoter un jeton (nouvelle valeur, même id)")
    rotate.add_argument("token_id")
    _add_idempotency_key(rotate)
    rotate.set_defaults(handler=_cmd_tokens_rotate)

    revoke = sub.add_parser("revoke", help="Révoque un jeton")
    revoke.add_argument("token_id")
    _add_idempotency_key(revoke)
    revoke.set_defaults(handler=_cmd_tokens_revoke)


def _cmd_tokens_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get("/v2/agent-tokens")


def _cmd_tokens_create(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    body: dict[str, object] = {
        "name": args.name,
        "scope": args.scope,
        "source_restriction": args.source_restriction or [],
        "pre_authorized_actions": args.pre_authorized_actions or [],
        "never_expires": args.never_expires,
    }
    if args.expires_at:
        body["expires_at"] = args.expires_at
    return client.write("POST", "/v2/agent-tokens", json_body=body, idempotency_key=args.idempotency_key)


def _cmd_tokens_rotate(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST", f"/v2/agent-tokens/{args.token_id}/rotate", json_body={}, idempotency_key=args.idempotency_key
    )


def _cmd_tokens_revoke(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.delete(f"/v2/agent-tokens/{args.token_id}")


# --- users -----------------------------------------------------------------


def _add_users(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("users", help="Utilisateurs humains")
    parser.add_argument(
        "--config",
        dest="users_config",
        type=Path,
        default=None,
        help="Configuration privée 0600 de ce site (prioritaire sur l'environnement)",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("list", help="Liste les utilisateurs").set_defaults(handler=_cmd_users_list)

    invite = sub.add_parser("invite", help="Invite un utilisateur (lien d'activation)")
    invite.add_argument("--email", required=True)
    invite.add_argument("--role", required=True, choices=("admin", "reader"))
    _add_idempotency_key(invite)
    invite.set_defaults(handler=_cmd_users_invite)

    reissue = sub.add_parser("reissue-activation", help="Réémet explicitement le lien du premier admin non activé")
    reissue.add_argument("user_id")
    _add_idempotency_key(reissue)
    reissue.set_defaults(handler=_cmd_users_reissue_activation)


def _cmd_users_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get("/v2/users")


def _cmd_users_invite(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST", "/v2/users", json_body={"email": args.email, "role": args.role}, idempotency_key=args.idempotency_key
    )


def _cmd_users_reissue_activation(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST",
        f"/v2/users/{args.user_id}/activation/reissue",
        json_body={},
        idempotency_key=args.idempotency_key,
    )


# --- audit -------------------------------------------------------------


def _add_audit(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("audit", help="Journal d'audit")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    tail = sub.add_parser("tail", help="Interroge les dernières lignes d'audit")
    tail.add_argument("--actor-kind", default=None, choices=("human", "agent"))
    tail.add_argument("--resource-type", default=None)
    tail.add_argument("--limit", type=int, default=None)
    tail.set_defaults(handler=_cmd_audit_tail)


def _cmd_audit_tail(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    params = {
        k: v
        for k, v in {
            "actor_kind": args.actor_kind,
            "resource_type": args.resource_type,
            "limit": args.limit,
        }.items()
        if v is not None
    }
    return client.get("/v2/audit", params=params)


# --- events (SSE -> JSON lines) -----------------------------------------


def _add_events(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("events", help="Flux d'évènements")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    stream = sub.add_parser(
        "stream", help="Suit le flux d'évènements (Server-Sent Events -> une ligne JSON par évènement)"
    )
    stream.add_argument(
        "--last-event-id",
        default=None,
        help="Identifiant d'évènement (curseur ``events.id``) à partir duquel reprendre",
    )
    stream.set_defaults(handler=_cmd_events_stream, is_stream=True)


def _cmd_events_stream(client: ApiClient, args: argparse.Namespace, stdout: TextIO) -> int:
    """Diffère de la forme ``ApiResult`` : écrit une ligne JSON par évènement au fil de l'eau."""

    params = {"last_event_id": args.last_event_id} if args.last_event_id else None
    exit_code = 0
    try:
        with client.stream("/v2/events", params=params) as response:
            if not response.is_success:
                print(json.dumps({"error": {"code": "http_error", "status": response.status_code}}), file=stdout)
                return 1
            event_type = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data = line[len("data:") :].strip()
                    try:
                        payload = json.loads(data) if data else {}
                    except ValueError:
                        payload = {"raw": data}
                    print(json.dumps({"event": event_type, "data": payload}, ensure_ascii=False), file=stdout)
                    stdout.flush()
                elif line == "":
                    event_type = None
    except Exception as error:  # réseau coupé, flux fermé par le serveur — jamais fatal pour l'appelant
        print(json.dumps({"error": {"code": "stream_interrupted", "message": str(error)}}), file=stdout)
        exit_code = 9
    return exit_code


# --- webhooks --------------------------------------------------------------


def _add_webhooks(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("webhooks", help="Points de livraison webhook")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("list", help="Liste les webhooks").set_defaults(handler=_cmd_webhooks_list)

    create = sub.add_parser("create", help="Crée un webhook (secret affiché une seule fois)")
    create.add_argument("--url", required=True)
    create.add_argument("--event", action="append", dest="events", required=True)
    _add_idempotency_key(create)
    create.set_defaults(handler=_cmd_webhooks_create)

    delete = sub.add_parser("delete", help="Supprime un webhook")
    delete.add_argument("webhook_id")
    delete.set_defaults(handler=_cmd_webhooks_delete)


def _cmd_webhooks_list(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.get("/v2/webhooks")


def _cmd_webhooks_create(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.write(
        "POST", "/v2/webhooks", json_body={"url": args.url, "events": args.events}, idempotency_key=args.idempotency_key
    )


def _cmd_webhooks_delete(client: ApiClient, args: argparse.Namespace) -> ApiResult:
    return client.delete(f"/v2/webhooks/{args.webhook_id}")


# --- dispatch ----------------------------------------------------------


def run_v2_command(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    transport=None,
    config: ClientConfig | None = None,
) -> int:
    """Résout la config, construit le client, exécute le handler, imprime le JSON."""

    try:
        users_config = getattr(args, "users_config", None)
        resolved_config = (
            config
            if config is not None
            else (load_config(env={}, config_path=users_config) if users_config is not None else load_config())
        )
    except ConfigError as error:
        print(json.dumps({"error": {"code": "config_error", "message": str(error)}}), file=stdout)
        return EXIT_CONFIG_ERROR

    with ApiClient(resolved_config, transport=transport) as client:
        if getattr(args, "is_stream", False):
            return _cmd_events_stream(client, args, stdout)
        result: ApiResult = args.handler(client, args)
        output: dict[str, object] = {"result": result.body}
        if result.idempotency_key:
            output["idempotency_key"] = result.idempotency_key
        print(json.dumps(output, ensure_ascii=False, indent=2), file=stdout)
        return result.exit_code
