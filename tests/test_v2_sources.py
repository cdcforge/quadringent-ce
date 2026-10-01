"""Tâche 2 — modèle Source v2 (CRUD read/create/test).

Le secret n'est jamais renvoyé en clair (seulement ``secret_set: true``), et
la validation des champs régresse exactement sur les motifs de
``connections.py`` (v1) : un ``ibmi_host``/``ibmi_user`` refusé en v1 doit
l'être aussi en v2.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.connections import _IBMI_HOST, _IBMI_USER
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.executor.diagnostic_jobs import SourceProbeUnavailableError
from quadringent_control_plane.v2.services.sources import (
    SourceNotFoundError,
    SourceValidationError,
    SourcesService,
)
from quadringent_control_plane.v2.services.validation import (
    ValidationError,
    validated_ibmi_host,
    validated_ibmi_user,
)


@pytest.fixture()
def sources_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'sources.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    service = SourcesService(engine, secret_box, org_id="org1")
    try:
        yield service
    finally:
        engine.dispose()


def test_create_then_read_never_returns_the_secret_in_clear(sources_service) -> None:
    created = sources_service.create(
        display_name="Site principal",
        ibmi_host="as400.example.com",
        ibmi_user="QSVCUSER",
        secret_value="un-mot-de-passe-tres-secret",
    )
    payload = created.to_dict()
    assert payload["secret_set"] is True
    assert "secret" not in payload
    assert "secret_value" not in payload
    assert "un-mot-de-passe-tres-secret" not in str(payload)

    fetched = sources_service.get(created.id)
    assert fetched.to_dict()["secret_set"] is True
    assert "un-mot-de-passe-tres-secret" not in str(fetched.to_dict())


def test_list_orders_by_creation_then_id(sources_service) -> None:
    first = sources_service.create(
        display_name="A",
        ibmi_host="host-a.example.com",
        ibmi_user="USERA",
        secret_value="secretA",
    )
    second = sources_service.create(
        display_name="B",
        ibmi_host="host-b.example.com",
        ibmi_user="USERB",
        secret_value="secretB",
    )
    listed = sources_service.list()
    assert [record.id for record in listed] == [first.id, second.id]


def test_get_unknown_source_raises_not_found(sources_service) -> None:
    with pytest.raises(SourceNotFoundError):
        sources_service.get("does-not-exist")


def test_test_source_never_exposes_the_secret(sources_service) -> None:
    created = sources_service.create(
        display_name="Site test",
        ibmi_host="as400.example.com",
        ibmi_user="QSVCUSER",
        secret_value="secret-de-test",
    )
    result = sources_service.test(created.id)
    assert result["secret_set"] is True
    assert "secret-de-test" not in str(result)


def test_test_unknown_source_raises_not_found(sources_service) -> None:
    with pytest.raises(SourceNotFoundError):
        sources_service.test("does-not-exist")


@pytest.mark.parametrize(
    "display_name,ibmi_host,ibmi_user",
    [
        ("Site principal", "as400 invalide", "QSVCUSER"),
        ("Site principal", "as400.example.com", "user-minuscule"),
        ("Site principal", "as400.example.com", ""),
        ("", "as400.example.com", "QSVCUSER"),
    ],
)
def test_create_rejects_invalid_fields(sources_service, display_name, ibmi_host, ibmi_user) -> None:
    with pytest.raises(SourceValidationError):
        sources_service.create(
            display_name=display_name,
            ibmi_host=ibmi_host,
            ibmi_user=ibmi_user,
            secret_value="peu-importe",
        )


def test_create_rejects_empty_secret(sources_service) -> None:
    with pytest.raises(SourceValidationError):
        sources_service.create(
            display_name="Site principal",
            ibmi_host="as400.example.com",
            ibmi_user="QSVCUSER",
            secret_value="",
        )


# --- Régression directe sur les motifs de connections.py (v1) ---------------


@pytest.mark.parametrize(
    "value,expected_ok",
    [
        ("as400.example.com", True),
        ("AS400-01.prod.example.com", True),
        ("host avec espace", False),
        ("", False),
        ("é-non-ascii.example.com", False),
    ],
)
def test_ibmi_host_regex_matches_connections_py(value, expected_ok) -> None:
    v1_ok = _IBMI_HOST.fullmatch(value) is not None
    assert v1_ok is expected_ok
    if expected_ok:
        assert validated_ibmi_host(value) == value
    else:
        with pytest.raises(ValidationError):
            validated_ibmi_host(value)


@pytest.mark.parametrize(
    "value,expected_ok",
    [
        ("QSVCUSER", True),
        ("Q1", True),
        ("qsvcuser", False),
        ("1QSVC", False),
        ("", False),
    ],
)
def test_ibmi_user_regex_matches_connections_py(value, expected_ok) -> None:
    v1_ok = _IBMI_USER.fullmatch(value) is not None
    assert v1_ok is expected_ok
    if expected_ok:
        assert validated_ibmi_user(value) == value
    else:
        with pytest.raises(ValidationError):
            validated_ibmi_user(value)


# --- POST /v2/sources/{id}/test — le Job de sonde indisponible n'est jamais une 500 --------


class _UnavailableProbe:
    """Sonde factice : le Job Kubernetes n'a produit aucun résultat exploitable."""

    def probe(self, request):  # noqa: ANN001 — protocole factice, signature ignorée
        raise SourceProbeUnavailableError("executor_unavailable", "le Job de diagnostic n'a produit aucun résultat exploitable")


def test_test_route_maps_probe_unavailable_to_a_structured_503_not_a_bare_500() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        dsn = f"sqlite:///{Path(tmp) / 'sources-probe-unavailable.sqlite3'}"
        v2_db.run_migrations(dsn)
        engine = v2_db.create_engine_for(dsn)
        with engine.begin() as connection:
            connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
        secret_box = SecretBox(SecretBox.generate_key())
        service = SourcesService(engine, secret_box, org_id="default")
        created = service.create(
            display_name="Site principal",
            ibmi_host="as400.example.com",
            ibmi_user="QSVCUSER",
            secret_value="s3cret",
        )
        app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default", source_probe=_UnavailableProbe())
        http = TestClient(app)
        response = http.post(
            f"/v2/sources/{created.id}/test",
            json={},
            headers={"Idempotency-Key": 'fixture-request'},
        )
        engine.dispose()

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "executor_unavailable"
    assert body["error"]["next_action"]
    assert body["error"]["retryable"] is True
