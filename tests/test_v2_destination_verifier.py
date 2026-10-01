"""Chantier « backend gaps », item 1 — ``POST /v2/destinations/{id}/verify``.

Couvre la structure du résultat (``DestinationVerificationResult``), le
service (``DestinationsService.verify``, sans vérificateur -> "unknown",
avec un faux vérificateur -> transitions ``verified``/``failed`` de
``verification_state``) et la route HTTP, avec un faux vérificateur injecté
(jamais de connexion Snowflake réelle dans ces tests).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services import destination_verifier as dv_module
from quadringent_control_plane.v2.services.destination_verifier import (
    DestinationVerificationResult,
    DestinationVerifierError,
    DestinationVerifierRequest,
    SnowflakeKeyPairVerifier,
    VerificationOutcome,
    warehouse_name_for,
)
from quadringent_control_plane.v2.services.destinations import (
    DestinationNotFoundError,
    DestinationsService,
)


def _ok(detail: str = "ok") -> VerificationOutcome:
    return VerificationOutcome(True, detail)


def _fail(detail: str = "échec") -> VerificationOutcome:
    return VerificationOutcome(False, detail)


def _all_ok_result() -> DestinationVerificationResult:
    return DestinationVerificationResult(
        connection=_ok(), role=_ok(), warehouse=_ok(), database=_ok(), schema=_ok(), load_privileges=_ok()
    )


class FakeVerifier:
    def __init__(self, result: DestinationVerificationResult, *, captured: list | None = None) -> None:
        self._result = result
        self._captured = captured

    def verify(self, request: DestinationVerifierRequest) -> DestinationVerificationResult:
        if self._captured is not None:
            self._captured.append(request)
        return self._result


# --- warehouse_name_for -----------------------------------------------------


def test_warehouse_name_for_matches_setup_script_convention() -> None:
    assert warehouse_name_for("QDT_ROLE_ABCD1234") == "QDT_WH_ABCD1234"


def test_warehouse_name_for_rejects_unknown_convention() -> None:
    with pytest.raises(DestinationVerifierError):
        warehouse_name_for("SOME_OTHER_ROLE")


# --- DestinationVerificationResult ------------------------------------------


def test_result_verified_is_true_only_when_all_outcomes_ok() -> None:
    assert _all_ok_result().verified() is True

    partial = DestinationVerificationResult(
        connection=_ok(), role=_ok(), warehouse=_ok(), database=_ok(), schema=_ok(), load_privileges=_fail()
    )
    assert partial.verified() is False


def test_result_to_dict_never_leaks_a_secret_and_reports_each_step() -> None:
    result = _all_ok_result()
    payload = result.to_dict()
    assert payload["verified"] is True
    for key in ("connection", "role", "warehouse", "database", "schema", "load_privileges"):
        assert payload[key] == {"ok": True, "detail": "ok"}


# --- SnowflakeKeyPairVerifier (pas de connecteur Snowflake requis) ---------


class FakeCursor:
    def __init__(self, script: dict[str, object]) -> None:
        self._script = script
        self._last: object = None

    def execute(self, sql: str) -> None:
        for pattern, outcome in self._script.items():
            if pattern in sql:
                if isinstance(outcome, Exception):
                    raise outcome
                self._last = outcome
                return
        self._last = None

    def fetchall(self):
        if self._last is None:
            return []
        return [(self._last,)]

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_snowflake_key_pair_verifier_reports_connection_refused_when_connector_absent() -> None:
    # L'environnement de test n'installe pas snowflake-connector-python
    # (dépendance optionnelle) : ``_connect`` échoue à l'import, exactement
    # comme un rejet réseau réel — jamais de faux résultat vert.
    verifier = SnowflakeKeyPairVerifier()
    request = DestinationVerifierRequest(
        snowflake_account="acme-sf", service_user="QDT_SVC_X", service_role="QDT_ROLE_X",
        warehouse="QDT_WH_X", private_key_pem="not-a-real-key",
    )
    result = verifier.verify(request)
    assert result.verified() is False
    assert result.connection.ok is False


def test_snowflake_key_pair_verifier_reports_each_step_from_the_connection(monkeypatch) -> None:
    cursor = FakeCursor(
        {
            "CURRENT_ROLE": "QDT_ROLE_X",
            "CURRENT_WAREHOUSE": "QDT_WH_X",
            "USE DATABASE QUADRINGENT": None,
            "USE SCHEMA QUADRINGENT.RAW": None,
            "USE SCHEMA QUADRINGENT.CURATED": None,
            "CREATE TABLE QUADRINGENT.RAW": None,
            "CREATE TABLE QUADRINGENT.CURATED": None,
            "DROP TABLE IF EXISTS QUADRINGENT.RAW": None,
            "DROP TABLE IF EXISTS QUADRINGENT.CURATED": None,
        }
    )
    connection = FakeConnection(cursor)
    verifier = SnowflakeKeyPairVerifier()
    monkeypatch.setattr(verifier, "_connect", lambda request: connection)

    request = DestinationVerifierRequest(
        snowflake_account="acme-sf", service_user="QDT_SVC_X", service_role="QDT_ROLE_X",
        warehouse="QDT_WH_X", private_key_pem="not-a-real-key",
    )
    result = verifier.verify(request)

    assert result.verified() is True
    assert connection.closed is True


def test_snowflake_key_pair_verifier_flags_role_mismatch(monkeypatch) -> None:
    cursor = FakeCursor(
        {
            "CURRENT_ROLE": "SOME_OTHER_ROLE",
            "CURRENT_WAREHOUSE": "QDT_WH_X",
            "USE DATABASE QUADRINGENT": None,
            "USE SCHEMA QUADRINGENT.RAW": None,
            "USE SCHEMA QUADRINGENT.CURATED": None,
            "CREATE TABLE QUADRINGENT.RAW": None,
            "CREATE TABLE QUADRINGENT.CURATED": None,
        }
    )
    connection = FakeConnection(cursor)
    verifier = SnowflakeKeyPairVerifier()
    monkeypatch.setattr(verifier, "_connect", lambda request: connection)

    request = DestinationVerifierRequest(
        snowflake_account="acme-sf", service_user="QDT_SVC_X", service_role="QDT_ROLE_X",
        warehouse="QDT_WH_X", private_key_pem="not-a-real-key",
    )
    result = verifier.verify(request)

    assert result.verified() is False
    assert result.role.ok is False
    assert "SOME_OTHER_ROLE" in result.role.detail


def test_snowflake_key_pair_verifier_flags_missing_create_table_privilege(monkeypatch) -> None:
    cursor = FakeCursor(
        {
            "CURRENT_ROLE": "QDT_ROLE_X",
            "CURRENT_WAREHOUSE": "QDT_WH_X",
            "USE DATABASE QUADRINGENT": None,
            "USE SCHEMA QUADRINGENT.RAW": None,
            "USE SCHEMA QUADRINGENT.CURATED": None,
            "CREATE TABLE QUADRINGENT.RAW": None,
            "CREATE TABLE QUADRINGENT.CURATED": RuntimeError("insufficient privileges"),
        }
    )
    connection = FakeConnection(cursor)
    verifier = SnowflakeKeyPairVerifier()
    monkeypatch.setattr(verifier, "_connect", lambda request: connection)

    request = DestinationVerifierRequest(
        snowflake_account="acme-sf", service_user="QDT_SVC_X", service_role="QDT_ROLE_X",
        warehouse="QDT_WH_X", private_key_pem="not-a-real-key",
    )
    result = verifier.verify(request)

    assert result.verified() is False
    assert result.load_privileges.ok is False
    assert "CURATED" in result.load_privileges.detail


def test_declared_database_and_schema_constants_match_setup_script() -> None:
    # Aucune valeur redécouverte séparément — cf.
    # services/destinations.py::_build_setup_script.
    assert dv_module.DECLARED_DATABASE == "QUADRINGENT"
    assert dv_module.DECLARED_HISTORY_SCHEMA == "RAW"
    assert dv_module.DECLARED_MIRROR_SCHEMA == "CURATED"


# --- DestinationsService.verify ---------------------------------------------


@pytest.fixture()
def destinations_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'destination_verify.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    service = DestinationsService(engine, secret_box, org_id="org1")
    try:
        yield service
    finally:
        engine.dispose()


def test_verify_without_a_verifier_is_honestly_unknown(destinations_service) -> None:
    created, _private_key_pem = destinations_service.create(snowflake_account="acme-sf")
    result = destinations_service.verify(created.id, verifier=None)
    assert result == {"destination_id": created.id, "verified": "unknown"}
    # verification_state ne transitionne jamais sans vérificateur réel.
    assert destinations_service.get(created.id).to_dict()["verification_state"] == "declared_not_verified"


def test_verify_unknown_destination_raises_not_found(destinations_service) -> None:
    with pytest.raises(DestinationNotFoundError):
        destinations_service.verify("does-not-exist", verifier=FakeVerifier(_all_ok_result()))


def test_verify_success_transitions_verification_state_to_verified(destinations_service) -> None:
    created, private_key_pem = destinations_service.create(snowflake_account="acme-sf")
    captured: list[DestinationVerifierRequest] = []
    result = destinations_service.verify(created.id, verifier=FakeVerifier(_all_ok_result(), captured=captured))

    assert result["destination_id"] == created.id
    assert result["verified"] is True
    assert destinations_service.get(created.id).to_dict()["verification_state"] == "verified"

    # Le vérificateur reçoit la clé privée déchiffrée et l'identité de
    # service réelles — jamais une valeur devinée.
    assert len(captured) == 1
    sent = captured[0]
    assert sent.snowflake_account == "acme-sf"
    assert sent.service_user == created.service_user
    assert sent.service_role == created.service_role
    assert sent.private_key_pem == private_key_pem
    assert sent.warehouse == warehouse_name_for(created.service_role)


def test_verify_failure_transitions_verification_state_to_failed(destinations_service) -> None:
    created, _private_key_pem = destinations_service.create(snowflake_account="acme-sf")
    failing = DestinationVerificationResult(
        connection=_ok(), role=_ok(), warehouse=_ok(), database=_ok(), schema=_ok(),
        load_privileges=_fail("CREATE TABLE refusé sur QUADRINGENT.CURATED"),
    )
    result = destinations_service.verify(created.id, verifier=FakeVerifier(failing))
    assert result["verified"] is False
    assert result["load_privileges"]["ok"] is False
    assert destinations_service.get(created.id).to_dict()["verification_state"] == "failed"


def test_verify_never_exposes_the_private_key_in_the_result(destinations_service) -> None:
    created, private_key_pem = destinations_service.create(snowflake_account="acme-sf")
    result = destinations_service.verify(created.id, verifier=FakeVerifier(_all_ok_result()))
    assert private_key_pem not in str(result)


# --- Route HTTP --------------------------------------------------------------


@pytest.fixture()
def wired_app_and_ids(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'destination_verify_route.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    secret_box = SecretBox(SecretBox.generate_key())
    service = DestinationsService(engine, secret_box, org_id="default")
    created, _private_key_pem = service.create(snowflake_account="acme-sf")
    return engine, secret_box, created.id


def test_route_without_verifier_returns_unknown(wired_app_and_ids) -> None:
    engine, secret_box, destination_id = wired_app_and_ids
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default")
    client = TestClient(app)
    response = client.post(
        f"/v2/destinations/{destination_id}/verify",
        json={},
        headers={"Idempotency-Key": "verify-1"},
    )
    assert response.status_code == 200
    assert response.json()["after"] == {"destination_id": destination_id, "verified": "unknown"}
    engine.dispose()


def test_route_with_verifier_persists_verification_state(wired_app_and_ids) -> None:
    engine, secret_box, destination_id = wired_app_and_ids
    app = create_v2_app(
        engine=engine, secret_box=secret_box, org_id="default", destination_verifier=FakeVerifier(_all_ok_result())
    )
    client = TestClient(app)
    response = client.post(
        f"/v2/destinations/{destination_id}/verify",
        json={},
        headers={"Idempotency-Key": "verify-2"},
    )
    assert response.status_code == 200
    body = response.json()["after"]
    assert body["verified"] is True
    assert body["destination_id"] == destination_id

    reread = client.get(f"/v2/destinations/{destination_id}")
    assert reread.json()["verification_state"] == "verified"
    engine.dispose()


def test_route_unknown_destination_is_not_found(wired_app_and_ids) -> None:
    engine, secret_box, _destination_id = wired_app_and_ids
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id="default")
    client = TestClient(app)
    response = client.post(
        "/v2/destinations/does-not-exist/verify", json={}, headers={"Idempotency-Key": "verify-3"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    engine.dispose()


def test_declared_scope_survives_service_reload_and_reaches_verifier(destinations_service):
    created, _ = destinations_service.create(snowflake_account='acme-sf', destination_database='DATA', destination_schema='SITE')
    captured = []
    destinations_service.verify(created.id, verifier=FakeVerifier(_all_ok_result(), captured=captured))
    assert captured[0].destination_database == 'DATA'
    assert captured[0].destination_schema == 'SITE'


def test_real_verifier_queries_only_declared_output_scope(monkeypatch):
    statements = []
    class ScopedCursor(FakeCursor):
        def execute(self, sql):
            statements.append(sql)
            assert 'QUADRINGENT' not in sql and '.RAW' not in sql and '.CURATED' not in sql
            super().execute(sql)
    cursor = ScopedCursor({'CURRENT_ROLE': 'QDT_ROLE_X', 'CURRENT_WAREHOUSE': 'QDT_WH_X'})
    connection = FakeConnection(cursor)
    verifier = SnowflakeKeyPairVerifier()
    monkeypatch.setattr(verifier, '_connect', lambda request: connection)
    request = DestinationVerifierRequest(snowflake_account='acme-sf', service_user='QDT_SVC_X', service_role='QDT_ROLE_X', warehouse='QDT_WH_X', private_key_pem='not-a-real-key', destination_database='DATA', destination_schema='SITE')
    assert verifier.verify(request).verified()
    assert 'USE DATABASE DATA' in statements and 'USE SCHEMA DATA.SITE' in statements
    assert len([s for s in statements if s.startswith('CREATE TABLE')]) == 1
    assert all('GRANT' not in s for s in statements)


def test_verifier_rejects_corrupt_persisted_scope_before_connection(monkeypatch):
    verifier = SnowflakeKeyPairVerifier()
    called = []
    monkeypatch.setattr(verifier, '_connect', lambda request: called.append(request))
    request = DestinationVerifierRequest(snowflake_account='acme-sf', service_user='QDT_SVC_X', service_role='QDT_ROLE_X', warehouse='QDT_WH_X', private_key_pem='not-a-real-key', destination_database='DATA;DROP X', destination_schema='SITE')
    assert not verifier.verify(request).verified()
    assert called == []


def test_route_creation_persists_scope_and_verify_body_cannot_override_it(wired_app_and_ids):
    engine, secret_box, _ = wired_app_and_ids
    captured = []
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id='default', destination_verifier=FakeVerifier(_all_ok_result(), captured=captured))
    with TestClient(app) as client:
        response = client.post('/v2/destinations', json={'snowflake_account':'acme-sf', 'destination_database':'DATA', 'destination_schema':'SITE', 'destination_mode':'streaming'}, headers={'Idempotency-Key':'scoped-create'})
        assert response.status_code == 201
        record = response.json()['after']
        assert record['destination_database'] == 'DATA' and record['destination_schema'] == 'SITE'
        assert record['verification_state'] == 'declared_not_verified'
        result = client.post(f"/v2/destinations/{record['id']}/verify", json={'destination_database':'OTHER', 'destination_schema':'PUBLIC', 'verified':True}, headers={'Idempotency-Key':'scoped-verify'})
        assert result.status_code == 200
        assert captured[0].destination_database == 'DATA' and captured[0].destination_schema == 'SITE'
        bad = client.post('/v2/destinations', json={'snowflake_account':'acme-sf', 'destination_schema':'SITE;DROP DATABASE X'}, headers={'Idempotency-Key':'unsafe-scope'})
        assert bad.status_code == 400
    engine.dispose()


def test_custom_database_legacy_schemas_are_both_verified(monkeypatch):
    statements = []
    class Cursor(FakeCursor):
        def execute(self, sql):
            statements.append(sql)
            super().execute(sql)
    connection = FakeConnection(Cursor({'CURRENT_ROLE':'QDT_ROLE_X', 'CURRENT_WAREHOUSE':'QDT_WH_X'}))
    verifier = SnowflakeKeyPairVerifier()
    monkeypatch.setattr(verifier, '_connect', lambda request: connection)
    request = DestinationVerifierRequest(snowflake_account='acme-sf', service_user='QDT_SVC_X', service_role='QDT_ROLE_X', warehouse='QDT_WH_X', private_key_pem='not-a-real-key', destination_database='DATA')
    assert verifier.verify(request).verified()
    assert 'USE SCHEMA DATA.RAW' in statements and 'USE SCHEMA DATA.CURATED' in statements
    assert all('QUADRINGENT' not in sql for sql in statements)


def test_probe_collision_never_adopts_or_drops_existing_table():
    from quadringent_control_plane.v2.services import destination_verifier as module

    statements = []

    class CollisionCursor:
        def execute(self, sql):
            statements.append(sql)
            if sql.startswith('CREATE TABLE ') and 'IF NOT EXISTS' not in sql:
                raise RuntimeError('table already exists')

    result = module._check_load_privileges(CollisionCursor(), 'DATA', ('SITE',))
    assert result.ok is False
    assert not any(sql.startswith('DROP TABLE') for sql in statements)
    assert all('IF NOT EXISTS' not in sql for sql in statements)


def test_probe_drop_failure_fails_verification():
    from quadringent_control_plane.v2.services import destination_verifier as module

    statements = []

    class DropFailureCursor:
        def execute(self, sql):
            statements.append(sql)
            if sql.startswith('DROP TABLE'):
                raise RuntimeError('cleanup refused')

    result = module._check_load_privileges(DropFailureCursor(), 'DATA', ('SITE',))
    assert result.ok is False
    assert 'retrait' in result.detail.lower()
    assert len(statements) == 2


def test_probe_names_are_unique_and_cleanup_only_created_tables():
    from quadringent_control_plane.v2.services import destination_verifier as module

    statements = []

    class RecordingCursor:
        def execute(self, sql):
            statements.append(sql)

    cursor = RecordingCursor()
    assert module._check_load_privileges(cursor, 'DATA', ('SITE',)).ok is True
    assert module._check_load_privileges(cursor, 'DATA', ('SITE',)).ok is True
    created = [sql.split()[2] for sql in statements if sql.startswith('CREATE TABLE')]
    dropped = [sql.split()[2] for sql in statements if sql.startswith('DROP TABLE')]
    assert len(set(created)) == 2
    assert created == dropped
    assert all(name.startswith('DATA.SITE.QDT_VERIFY_') for name in created)


def test_route_probe_cleanup_failure_persists_failed(wired_app_and_ids):
    from dataclasses import replace
    from quadringent_control_plane.v2.services import destination_verifier as module

    class DropFailureCursor:
        def execute(self, sql):
            if sql.startswith('DROP TABLE'):
                raise RuntimeError('cleanup refused')

    outcome = module._check_load_privileges(DropFailureCursor(), 'DATA', ('SITE',))
    result = replace(_all_ok_result(), load_privileges=outcome)
    engine, secret_box, destination_id = wired_app_and_ids
    app = create_v2_app(engine=engine, secret_box=secret_box, org_id='default', destination_verifier=FakeVerifier(result))
    try:
        with TestClient(app) as client:
            response = client.post(f'/v2/destinations/{destination_id}/verify', json={}, headers={'Idempotency-Key': 'cleanup-failed'})
            assert response.status_code == 200
            assert response.json()['after']['verified'] is False
            assert response.json()['after']['load_privileges']['ok'] is False
            assert client.get(f'/v2/destinations/{destination_id}').json()['verification_state'] == 'failed'
    finally:
        engine.dispose()
