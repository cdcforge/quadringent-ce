"""``GET /v2/costs`` (contrat §2.5) — mesuré/estimé/absent, jamais 0 par défaut.

Reprend l'invariant de ``costs.py``/``infrastructure_costs.py`` v1
(sans réutiliser leurs types, voir ``services/costs.py`` pour la
justification) : sans fournisseur injecté, le statut est toujours
``absent`` et le montant reste ``None``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.costs import CostSnapshot, NullCostsProvider, absent_cost


def test_null_provider_returns_absent_never_zero() -> None:
    provider = NullCostsProvider()
    snapshot = provider.get("connection", "conn1", window="30d")
    assert snapshot.status == "absent"
    assert snapshot.amount is None
    assert snapshot.currency is None
    assert snapshot.basis is None


def test_absent_cost_helper_shape() -> None:
    snapshot = absent_cost("table", "tbl1", window=None)
    assert snapshot.to_dict() == {
        "scope": "table",
        "id": "tbl1",
        "window": None,
        "status": "absent",
        "amount": None,
        "currency": None,
        "basis": None,
        "collected_at": None,
        "reason": None,
    }


class _FakeCostsProvider:
    def get(self, scope, id_, *, window):
        return CostSnapshot(
            scope=scope,
            id=id_,
            window=window,
            status="measured",
            amount=12.5,
            currency="USD",
            basis="warehouse_credits",
            collected_at="2026-09-23T10:00:00Z",
        )


@pytest.fixture()
def client(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'costs.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(engine=engine, secret_box=SecretBox(SecretBox.generate_key()))
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


@pytest.fixture()
def client_with_provider(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'costs_measured.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"})
    app = create_v2_app(
        engine=engine, secret_box=SecretBox(SecretBox.generate_key()), costs_provider=_FakeCostsProvider()
    )
    try:
        yield TestClient(app)
    finally:
        engine.dispose()


def test_route_without_provider_returns_absent(client) -> None:
    response = client.get("/v2/costs", params={"scope": "connection", "id": "conn1"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "absent"
    assert body["amount"] is None


def test_route_with_provider_returns_measured_snapshot(client_with_provider) -> None:
    response = client_with_provider.get(
        "/v2/costs", params={"scope": "table", "id": "tbl1", "window": "30d"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "measured"
    assert body["amount"] == 12.5
    assert body["currency"] == "USD"
    assert body["basis"] == "warehouse_credits"
    assert body["window"] == "30d"


def test_route_rejects_unknown_scope(client) -> None:
    response = client.get("/v2/costs", params={"scope": "warehouse", "id": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_route_requires_id(client) -> None:
    response = client.get("/v2/costs", params={"scope": "connection"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
