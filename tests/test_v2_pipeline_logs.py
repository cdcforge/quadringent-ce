"""Service et route ``GET /v2/pipelines/{id}/logs`` (contrat §2.4).

Couvre la règle de rédaction (secrets + blocs ressemblant à de la donnée de
ligne, voir l'en-tête de ``services/logs.py``), les filtres (niveau,
depuis, corrélation d'incident) et l'échec fermé sans source injectée.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.logs import (
    KNOWN_LEVELS,
    LogsService,
    NullLogSource,
    RawLogEntry,
    redact,
)


# --- redact() ------------------------------------------------------------


def test_redact_masks_password_key_value_pair() -> None:
    assert redact("connexion avec password=hunter2 réussie") == (
        "connexion avec password=[SECRET_MASQUE] réussie"
    )


@pytest.mark.parametrize("key", ["password", "secret", "token", "api_key", "api-key", "authorization", "credential"])
def test_redact_masks_all_known_secret_keys(key) -> None:
    message = f'{key}="abc123"'
    assert "abc123" not in redact(message)
    assert "[SECRET_MASQUE]" in redact(message)


def test_redact_masks_json_like_row_payload_block() -> None:
    message = 'ligne rejetée: {"id": 42, "customer_name": "Jean Dupont"}'
    redacted = redact(message)
    assert "Jean Dupont" not in redacted
    assert "42" not in redacted
    assert '{"redacted": "donnee_de_ligne_masquee"}' in redacted


def test_redact_leaves_single_field_braces_untouched() -> None:
    # Un seul « : » : pas assez pour ressembler à une ligne de donnée
    # source — ne doit pas être masqué (faux positif évité).
    message = "statut du lecteur : {état: en cours}"
    assert redact(message) == message


def test_redact_leaves_ordinary_messages_untouched() -> None:
    message = "reprise du curseur à la séquence 42 après redémarrage"
    assert redact(message) == message


# --- LogsService -----------------------------------------------------------


class _FakeLogSource:
    def __init__(self, entries: tuple[RawLogEntry, ...]) -> None:
        self._entries = entries

    def fetch(self, pipeline_id: str, *, since: str | None) -> tuple[RawLogEntry, ...]:
        return self._entries


def test_null_log_source_returns_empty_tuple() -> None:
    service = LogsService(NullLogSource())
    assert service.fetch("ppl1") == ()


def test_service_filters_by_level() -> None:
    entries = (
        RawLogEntry(at="2026-09-23T10:00:00Z", level="info", message="ok"),
        RawLogEntry(at="2026-09-23T10:00:01Z", level="error", message="échec"),
    )
    service = LogsService(_FakeLogSource(entries))
    filtered = service.fetch("ppl1", level="error")
    assert [entry.level for entry in filtered] == ["error"]


def test_service_filters_by_incident_correlation() -> None:
    entries = (
        RawLogEntry(at="2026-09-23T10:00:00Z", level="info", message="ok", incident_id=None),
        RawLogEntry(at="2026-09-23T10:00:01Z", level="error", message="échec", incident_id="inc1"),
    )
    service = LogsService(_FakeLogSource(entries))
    filtered = service.fetch("ppl1", incident=True)
    assert [entry.incident_id for entry in filtered] == ["inc1"]


def test_service_redacts_every_entry_regardless_of_source() -> None:
    entries = (RawLogEntry(at="2026-09-23T10:00:00Z", level="info", message="password=abc123"),)
    service = LogsService(_FakeLogSource(entries))
    filtered = service.fetch("ppl1")
    assert "abc123" not in filtered[0].message


def test_known_levels_are_exactly_info_warning_error() -> None:
    assert KNOWN_LEVELS == frozenset({"info", "warning", "error"})


# --- Route HTTP --------------------------------------------------------


def _seed_pipeline(engine, *, org_id: str = "default") -> None:
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": org_id, "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": org_id,
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "gAAAAA==",
            },
        )
        connection.execute(
            v2_schema.destinations.insert(),
            {
                "id": "dst1",
                "org_id": org_id,
                "snowflake_account": "acme-sf",
                "key_pair_ciphertext": "gAAAAA==",
                "setup_script": "-- setup.sql",
            },
        )
        connection.execute(
            v2_schema.tables.insert(),
            {"id": "tbl1", "source_id": "src1", "schema_name": "PAYSLIB", "table_name": "ORDERS"},
        )
        connection.execute(
            v2_schema.pipelines.insert(),
            {"id": "ppl1", "table_id": "tbl1", "destination_id": "dst1", "declared_state": "live"},
        )


@pytest.fixture()
def client_with_logs(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'pipeline_logs.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed_pipeline(engine)
    entries = (
        RawLogEntry(at="2026-09-23T10:00:00Z", level="info", message="démarrage ok"),
        RawLogEntry(
            at="2026-09-23T10:00:05Z",
            level="error",
            message='échec avec secret=hunter2 sur {"id": 1, "name": "x"}',
            incident_id="inc1",
        ),
    )
    app = create_v2_app(
        engine=engine, secret_box=SecretBox(SecretBox.generate_key()), log_source=_FakeLogSource(entries)
    )
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_route_returns_redacted_logs(client_with_logs) -> None:
    response = client_with_logs.get("/v2/pipelines/ppl1/logs")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    error_entry = next(item for item in items if item["level"] == "error")
    assert "hunter2" not in error_entry["message"]
    assert '"name": "x"' not in error_entry["message"]
    assert error_entry["incident_id"] == "inc1"


def test_route_filters_by_level_query_param(client_with_logs) -> None:
    response = client_with_logs.get("/v2/pipelines/ppl1/logs", params={"level": "error"})
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["level"] == "error"


def test_route_filters_by_incident_correlation(client_with_logs) -> None:
    response = client_with_logs.get("/v2/pipelines/ppl1/logs", params={"correlate_incident": "true"})
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["incident_id"] == "inc1"


def test_route_rejects_unknown_level(client_with_logs) -> None:
    response = client_with_logs.get("/v2/pipelines/ppl1/logs", params={"level": "bogus"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_route_returns_404_for_unknown_pipeline(client_with_logs) -> None:
    response = client_with_logs.get("/v2/pipelines/does-not-exist/logs")
    assert response.status_code == 404


def test_route_without_log_source_returns_empty_list(tmp_path) -> None:
    dsn = f"sqlite:///{tmp_path / 'pipeline_logs_null.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    _seed_pipeline(engine)
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    try:
        client = TestClient(app)
        response = client.get("/v2/pipelines/ppl1/logs")
        assert response.status_code == 200
        assert response.json()["items"] == []
    finally:
        engine.dispose()
