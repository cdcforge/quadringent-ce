"""Verrouille le contrat rejoué par l'assistant v2 (`ui/src/screens/wizard/*`) :
crée une source, la teste (sans sonde câblée -> forme "non disponible"),
retrouve la source par `GET /v2/sources` (reprise après rechargement), crée
une destination, découvre des tables via un client de découverte injecté,
choisit une clé pour une table `no_key`, démarre son pipeline. Rejoué contre
la vraie application v2 (SQLite + `create_v2_app`), comme
`tests/test_v2_users_routes.py` — aucune fixture UI (`src_demo`, etc.)
n'intervient ici, uniquement le contrat serveur réel.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from quadringent.table_discovery import DiscoveredTable
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


class _FakeDiscoveryClient:
    def __init__(self, tables: tuple[DiscoveredTable, ...]) -> None:
        self._tables = tables

    def discover(self, *, libraries, limit, search):  # noqa: D401 - protocole du service
        return self._tables


class _FakeExecutor:
    """Exécuteur minimal : accepte toute transition sans effet de bord réel."""

    def execute(self, *, pipeline_id: str, event: str, **kwargs: object) -> None:
        return None


@pytest.fixture()
def wizard_app(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'wizard_sequence.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})

    ready_table = DiscoveredTable(
        library="DEMOLIB", system_name="CLIENTS", sql_name="CLIENTS", text=None,
        row_count=100, size_bytes=2048, has_key=True, key_columns=("ID_CLIENT",),
        journaled=True, journal_library="DEMOLIB", journal_name="DEMOJRN", images="*BOTH", omitted=False,
    )
    no_key_table = DiscoveredTable(
        library="DEMOLIB", system_name="ARCHIVES", sql_name="ARCHIVES", text=None,
        row_count=50, size_bytes=1024, has_key=False, key_columns=(),
        journaled=True, journal_library="DEMOLIB", journal_name="DEMOJRN", images="*BOTH", omitted=False,
    )
    discovery_client = _FakeDiscoveryClient((ready_table, no_key_table))

    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        token_pepper=b"pepper-test",
        table_discovery_client=discovery_client,
        pipeline_executor=_FakeExecutor(),
    )
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def _idempotency_key(label: str) -> dict[str, str]:
    return {"Idempotency-Key": label}


def test_wizard_replays_source_test_destination_tables_and_pipeline_start(wizard_app) -> None:
    client = wizard_app

    # 1. Écran source : POST /v2/sources crée la source réelle (jamais
    #    `src_demo` ni aucun identifiant de fixture).
    created_source = client.post(
        "/v2/sources",
        json={
            "display_name": "IBM i — as400.qualif.local",
            "ibmi_host": "as400.qualif.local",
            "ibmi_user": "QDTOPER",
            "secret": {"kind": "inline", "value": "un-mot-de-passe-ibmi"},
        },
        headers=_idempotency_key("create-source-1"),
    )
    assert created_source.status_code == 201
    source_id = created_source.json()["after"]["id"]
    assert source_id != "src_demo"

    # « Tester » sans sonde câblée : forme honnête "non disponible", jamais
    # un faux résultat réseau/certificat/authentification vert.
    tested = client.post(f"/v2/sources/{source_id}/test", json={}, headers=_idempotency_key("test-source-1"))
    assert tested.status_code == 200
    test_result = tested.json()["after"]
    assert test_result == {"source_id": source_id, "reachable": "unknown", "secret_set": True}

    # Reprise après rechargement : GET /v2/sources retrouve la source réelle,
    # sans jamais exposer le secret.
    listed_sources = client.get("/v2/sources")
    assert listed_sources.status_code == 200
    items = listed_sources.json()["items"]
    assert [item["id"] for item in items] == [source_id]
    assert "secret_ciphertext" not in items[0]
    assert items[0]["secret_set"] is True

    # 2. Écran Snowflake : POST /v2/destinations — la clé privée n'est
    #    renvoyée qu'une seule fois, jamais en relisant la destination.
    created_destination = client.post(
        "/v2/destinations",
        json={"snowflake_account": "qualif-xy12345"},
        headers=_idempotency_key("create-destination-1"),
    )
    assert created_destination.status_code == 201
    destination_body = created_destination.json()["after"]
    destination_id = destination_body["id"]
    assert destination_id != "dst_demo"
    assert destination_body["verification_state"] == "declared_not_verified"
    assert "-----BEGIN PRIVATE KEY-----" in destination_body["private_key_pem"]

    reread_destination = client.get(f"/v2/destinations/{destination_id}")
    assert reread_destination.status_code == 200
    assert "private_key_pem" not in reread_destination.json()

    # 3. Écran tables : rafraîchit le catalogue depuis le client de
    #    découverte injecté (jamais un sondage improvisé), classe chaque
    #    table côté serveur.
    refreshed = client.post(
        f"/v2/sources/{source_id}/tables/refresh", json={}, headers=_idempotency_key("refresh-tables-1")
    )
    assert refreshed.status_code == 200
    tables_after = {item["table_name"]: item for item in refreshed.json()["after"]}
    assert tables_after["CLIENTS"]["readiness"] == "ready"
    assert tables_after["ARCHIVES"]["readiness"] == "no_key"
    archives_id = tables_after["ARCHIVES"]["id"]
    clients_id = tables_after["CLIENTS"]["id"]

    listed_tables = client.get(f"/v2/sources/{source_id}/tables")
    assert listed_tables.status_code == 200
    assert {item["id"] for item in listed_tables.json()["items"]} == {archives_id, clients_id}

    # Choisir la clé d'une table `no_key` : `key_strategy`/`key_columns`,
    # jamais `key_status` (forme fixture historique).
    patched = client.patch(
        f"/v2/tables/{archives_id}",
        json={"key_strategy": "rrn", "acknowledge_rrn": True},
        headers=_idempotency_key("patch-key-1"),
    )
    assert patched.status_code == 200
    assert patched.json()["after"]["key_strategy"] == "rrn"
    # Écart backend corrigé (chantier « backend gaps ») : `TablesService.
    # choose_key` recalcule désormais `readiness` avec la même fonction de
    # classification que `refresh` (`quadringent.table_discovery.
    # classify_table`) — une clé RRN acquittée compte comme une clé valide,
    # la table journalisée en *BOTH redevient donc "ready" sans attendre un
    # nouveau `refresh`.
    assert patched.json()["after"]["readiness"] == "ready"

    # 4. Démarrage automatique du pipeline (résolution implicite de la
    #    destination unique de l'organisation).
    started = client.post(
        f"/v2/tables/{clients_id}/pipeline", json={}, headers=_idempotency_key("start-pipeline-1")
    )
    assert started.status_code == 200
    pipeline_after = started.json()["after"]
    assert pipeline_after["declared_state"] == "copying"

    pipeline_id = pipeline_after["id"]
    fetched_pipeline = client.get(f"/v2/pipelines/{pipeline_id}")
    assert fetched_pipeline.status_code == 200
    assert fetched_pipeline.json()["declared_state"] == "copying"
