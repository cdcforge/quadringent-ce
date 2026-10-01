"""Bout-en-bout : un agent pilote l'onboarding via MCP, un humain approuve.

Scénario du chantier MCP/CLI (§9.2 tâches 18-20) : un agent scripté se
connecte au serveur MCP in-process (streamable HTTP réel — ``ClientSession``
+ ``streamable_http_client`` du SDK MCP officiel, pas un raccourci
``call_tool`` sans transport) avec un jeton d'agent, découvre les tables
d'une source déjà créée (fixture REST), choisit une clé, met en pause la
source, tente de retirer un pipeline (action sensible -> confirmation en
attente), qu'un humain (identité anonyme admin implicite, cf. ``auth.py``)
approuve via REST, puis l'agent rejoue l'action avec le ``confirmation_id``
et elle s'applique. Vérifie enfin que ``audit_records`` distingue
``actor_kind='agent'`` (avec ``mcp_client`` peuplé depuis les métadonnées du
client MCP) de ``actor_kind='human'``.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.agent_tokens import AgentTokensService

_PEPPER = b"0" * 32


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, *, pipeline_id: str, event: str) -> None:
        self.calls.append((pipeline_id, event))


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'mcp_e2e.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "default",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "x",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dest1",
                "org_id": "default",
                "snowflake_account": "acct123",
                "key_pair_ciphertext": "x",
                "setup_script": "-- x",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "SALES", "table_name": "ORDERS"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "pipe1", "table_id": "tbl1", "destination_id": "dest1", "declared_state": "live"},
        )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def executor() -> _FakeExecutor:
    return _FakeExecutor()


@pytest.fixture()
def app(engine, executor):
    return create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        token_pepper=_PEPPER,
        pipeline_executor=executor,
    )


@pytest.fixture()
def agent_token(engine) -> str:
    tokens_service = AgentTokensService(engine, org_id="default", pepper=_PEPPER)
    _record, token_value = tokens_service.create(
        name="agent-onboarding",
        scope="operate",
        created_by="test",
        source_restriction=[],
        pre_authorized_actions=[],
        never_expires=True,
    )
    return token_value


def test_agent_onboarding_then_human_approval(app, engine, agent_token, executor) -> None:
    asyncio.run(_run_onboarding_scenario(app, engine, agent_token, executor))


async def _run_onboarding_scenario(app, engine, agent_token, executor) -> None:
    # Un seul passage dans le lifespan applicatif pour tout le scénario :
    # ``mcp.session_manager.run()`` ne peut être entré qu'une fois par
    # instance de ``MCPServer`` (cf. SDK) — pas question de le déclencher à
    # nouveau par appel d'outil, ni via un second ``TestClient(app)`` pour
    # l'approbation humaine (même app, même serveur MCP). L'approbation
    # humaine passe donc par un client ``httpx`` nu sur le même transport
    # ASGI, sans en-tête ``Authorization`` (identité anonyme admin
    # implicite — pas d'``AuthConfig`` déclarée, cf. ``auth.py``).
    def _new_agent_client() -> httpx.AsyncClient:
        # Un client ``httpx`` par appel d'outil (jamais réutilisé après
        # fermeture — ``streamable_http_client`` ferme son flux à chaque
        # ``async with``) ; c'est le même choix que ``mcp_server.py``
        # (``_call`` construit un client neuf par appel, voir sa docstring).
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Authorization": f"Bearer {agent_token}"},
        )

    async def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        async with _new_agent_client() as agent_client:
            async with streamable_http_client("http://localhost/mcp/", http_client=agent_client) as (
                read,
                write,
                *_extra,
            ):
                async with ClientSession(
                    read, write, client_info=types.Implementation(name="onboarding-agent", version="1.0")
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    return result.structured_content or {}

    async with app.router.lifespan_context(app):
        # 1. L'agent liste les tables découvertes de la source déjà créée.
        tables = await call_tool("list_tables", {"source_id": "src1"})
        assert "error" not in tables
        assert any(item["id"] == "tbl1" for item in tables["items"])

        # 2. L'agent choisit la clé de la table.
        choose_key = await call_tool(
            "choose_table_key",
            {"table_id": "tbl1", "key_strategy": "unique_index", "key_columns": ["ORDER_ID"]},
        )
        assert "error" not in choose_key
        assert choose_key["after"]["key_strategy"] == "unique_index"

        # 3. L'agent inspecte le pipeline (déjà démarré par fixture — pas de
        #    route de création de pipeline dans ce chantier, cf. pipelines.py).
        pipeline = await call_tool("get_pipeline", {"pipeline_id": "pipe1"})
        assert pipeline["declared_state"] == "live"

        # 4. L'agent met en pause la source — action non sensible, s'applique directement.
        paused = await call_tool("pause_source", {"source_id": "src1"})
        assert paused["after"]["paused"] is True

        # 5. L'agent tente de retirer le pipeline — action sensible : confirmation en attente.
        remove_attempt = await call_tool("remove_table", {"pipeline_id": "pipe1"})
        assert remove_attempt["status"] == "pending_confirmation"
        confirmation_id = remove_attempt["confirmation_id"]
        assert confirmation_id

        # 6. Un humain approuve — identité anonyme admin implicite (pas
        #    d'``AuthConfig`` déclarée, cf. ``auth.py``), donc
        #    actor_kind="human" côté audit ; aucun en-tête ``Authorization``.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as human_client:
            approved = await human_client.post(
                f"/v2/confirmations/{confirmation_id}/approve",
                json={},
                headers={"Idempotency-Key": "approve-e2e-1"},
            )
        assert approved.status_code == 200
        assert approved.json()["after"]["state"] == "approved"

        # 7. L'agent rejoue l'action avec le confirmation_id — elle s'applique.
        removed = await call_tool("remove_table", {"pipeline_id": "pipe1", "confirmation_token": confirmation_id})
    assert "status" not in removed or removed.get("status") != "pending_confirmation"
    assert removed["after"]["declared_state"] == "stopped"
    assert ("pipe1", "remove") in executor.calls

    # 8. L'audit distingue bien agent (avec mcp_client peuplé) et humain.
    with engine.connect() as connection:
        rows = connection.execute(v2_schema.audit_records.select()).mappings().all()
    by_action = {row["action"]: row for row in rows}

    source_pause_row = by_action["source.pause"]
    assert source_pause_row["actor_kind"] == "agent"
    assert source_pause_row["mcp_client"] == "onboarding-agent/1.0"

    remove_rows = [row for row in rows if row["action"] == "pipeline.remove"]
    assert any(row["actor_kind"] == "agent" and row["mcp_client"] == "onboarding-agent/1.0" for row in remove_rows)
    assert any(row["status"] == "succeeded" for row in remove_rows)

    approve_row = by_action["confirmation.approve"]
    assert approve_row["actor_kind"] == "human"
    assert approve_row["mcp_client"] is None
