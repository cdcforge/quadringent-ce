"""``quadringent mcp --stdio`` — pont stdio -> ``/v2`` distant (contrat §5, tâche 19).

Construit le même ``MCPServer`` que le montage in-process
(``quadringent_control_plane.v2.mcp_server.build_mcp_server``) mais avec un
``make_client`` différent : au lieu d'un ``httpx.ASGITransport`` local, un
``httpx.AsyncClient`` réseau pointé sur ``QUADRINGENT_URL`` (jeton
``QUADRINGENT_TOKEN``, ou ``~/.quadringent/cli.json``) — c'est la seule
différence, aucune logique d'outil n'est dupliquée (voir la docstring de
``mcp_server.py``). Un client MCP stdio (Claude Code, Claude Desktop,
Codex...) parle protocole MCP en local ; les appels d'outils partent en
HTTP vers le control plane réel.
"""

from __future__ import annotations

import argparse
from typing import TextIO

from .api_client import ConfigError, load_config


def add_mcp_subcommand(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "mcp", help="Pont MCP stdio -> control plane distant (pour un client MCP local : Claude Code, etc.)"
    )
    parser.add_argument(
        "--stdio", action="store_true", required=True, help="Seul mode supporté dans ce chantier — requis explicitement"
    )


def run_mcp_command(args: argparse.Namespace, *, stdout: TextIO) -> int:
    import json

    try:
        config = load_config()
    except ConfigError as error:
        print(json.dumps({"error": {"code": "config_error", "message": str(error)}}), file=stdout)
        return 10

    # Imports différés : ``mcp``/``httpx`` sont dans l'extra ``api``, pas
    # une dépendance de base de la CLI d'installation (``quadringent
    # install``/``uninstall``/``status`` n'en ont pas besoin).
    import httpx
    from mcp.server.mcpserver import Context

    from quadringent_control_plane.v2.mcp_server import build_mcp_server

    def make_client(ctx: Context | None) -> httpx.AsyncClient:
        headers = {}
        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
        return httpx.AsyncClient(base_url=config.base_url, headers=headers, timeout=30.0)

    server = build_mcp_server(make_client=make_client, name="quadringent-control-plane-bridge")
    server.run(transport="stdio")
    return 0
