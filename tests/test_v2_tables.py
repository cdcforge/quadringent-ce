"""Tâche 4 — routes ``/v2/sources/{id}/tables`` et ``/v2/tables/{id}``.

Le client de découverte est toujours un faux injecté (``FakeDiscoveryClient``)
— aucun test n'ouvre de connexion IBM i. Couvre : liste + filtres,
``refresh`` (dry_run et réel, upsert préservant ``key_strategy``), ``PATCH``
(clé primaire/unique, et RRN qui exige ``acknowledge_rrn``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quadringent.table_discovery import DiscoveredColumn, DiscoveredTable
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.tables import (
    TableNotFoundError,
    TablesService,
    TableValidationError,
)


def _table(**overrides) -> DiscoveredTable:
    defaults = dict(
        library="SALES",
        system_name="ORDHDR",
        sql_name="ORDER_HEADER",
        text="En-tête de commande",
        row_count=1200,
        size_bytes=65536,
        has_key=True,
        key_columns=("ORDER_ID",),
        journaled=True,
        journal_library="SALES",
        journal_name="ORDJRN",
        images="*BOTH",
        omitted=False,
    )
    defaults.update(overrides)
    return DiscoveredTable(**defaults)


class FakeDiscoveryClient:
    def __init__(self, tables: tuple[DiscoveredTable, ...]) -> None:
        self.tables = tables
        self.calls: list[dict[str, object]] = []

    def discover(self, *, libraries, limit, search):
        self.calls.append({"libraries": libraries, "limit": limit, "search": search})
        return self.tables


@pytest.fixture()
def engine(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'tables.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
        connection.execute(
            v2_schema.sources.insert(),
            {
                "id": "src1",
                "org_id": "org1",
                "display_name": "Site principal",
                "ibmi_host": "as400.example.com",
                "ibmi_user": "QSVCUSER",
                "secret_ciphertext": "unused",
            },
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _client(engine, *, discovery_client=None) -> TestClient:
    app = create_v2_app(
        engine=engine,
        secret_box=SecretBox(SecretBox.generate_key()),
        org_id="org1",
        table_discovery_client=discovery_client,
    )
    return TestClient(app)


# --- Service --------------------------------------------------------------


def test_refresh_upserts_and_classifies_tables(engine) -> None:
    discovery = FakeDiscoveryClient((_table(), _table(system_name="LEGACY", journaled=False, journal_library=None, journal_name=None, images=None, has_key=False, key_columns=())))
    service = TablesService(engine)
    records = service.refresh("src1", discovery_client=discovery)
    by_name = {r.table_name: r for r in records}
    assert by_name["ORDHDR"].readiness == "ready"
    assert by_name["LEGACY"].readiness == "not_journaled"
    assert len(by_name["LEGACY"].cl_fix_commands) >= 1


def test_refresh_preserves_key_strategy_chosen_by_a_previous_patch(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    service = TablesService(engine)
    (record,) = service.refresh("src1", discovery_client=discovery)
    service.choose_key(record.id, key_strategy="unique_index", key_columns=["ORDER_ID"])
    (record_again,) = service.refresh("src1", discovery_client=discovery)
    assert record_again.id == record.id
    assert record_again.key_strategy == "unique_index"
    assert record_again.key_columns == ("ORDER_ID",)


def test_choose_key_rrn_requires_explicit_acknowledgement(engine) -> None:
    discovery = FakeDiscoveryClient((_table(has_key=False, key_columns=()),))
    service = TablesService(engine)
    (record,) = service.refresh("src1", discovery_client=discovery)
    from quadringent_control_plane.v2.services.tables import TableValidationError

    with pytest.raises(TableValidationError):
        service.choose_key(record.id, key_strategy="rrn")
    updated = service.choose_key(record.id, key_strategy="rrn", acknowledge_rrn=True)
    assert updated.key_strategy == "rrn"


def test_choose_key_recomputes_readiness_from_no_key_to_ready(engine) -> None:
    """Écart backend corrigé : ``choose_key`` ne laissait jamais une table

    ``no_key`` redevenir ``ready`` après un ``PATCH`` — elle attendait un
    nouveau ``refresh``. La reclassification réutilise
    ``quadringent.table_discovery.classify_table``, la même fonction que
    ``refresh``.
    """

    discovery = FakeDiscoveryClient((_table(has_key=False, key_columns=()),))
    service = TablesService(engine)
    (record,) = service.refresh("src1", discovery_client=discovery)
    assert record.readiness == "no_key"

    updated_primary = service.choose_key(record.id, key_strategy="unique_index", key_columns=["ORDER_ID"])
    assert updated_primary.readiness == "ready"

    # Un second PATCH vers rrn (acquitté) reste "ready" — même chemin de
    # classification, une clé RRN acquittée compte comme une clé valide.
    updated_rrn = service.choose_key(record.id, key_strategy="rrn", acknowledge_rrn=True)
    assert updated_rrn.readiness == "ready"


def test_choose_key_does_not_override_not_journaled_readiness(engine) -> None:
    """Une clé posée sur une table non journalisée ne doit jamais mentir en

    passant "ready" : la classification tient toujours compte de la
    journalisation persistée.
    """

    discovery = FakeDiscoveryClient(
        (_table(journaled=False, journal_library=None, journal_name=None, images=None, has_key=False, key_columns=()),)
    )
    service = TablesService(engine)
    (record,) = service.refresh("src1", discovery_client=discovery)
    assert record.readiness == "not_journaled"

    updated = service.choose_key(record.id, key_strategy="rrn", acknowledge_rrn=True)
    assert updated.readiness == "not_journaled"


# --- Routes HTTP ------------------------------------------------------------


def test_list_tables_filters_by_readiness_and_library(engine) -> None:
    discovery = FakeDiscoveryClient(
        (
            _table(system_name="ORDHDR"),
            _table(system_name="LEGACY", library="QGPL", journaled=False, journal_library=None, journal_name=None, images=None, has_key=False, key_columns=()),
        )
    )
    TablesService(engine).refresh("src1", discovery_client=discovery)
    client = _client(engine)
    response = client.get("/v2/sources/src1/tables", params={"library": "QGPL"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["table_name"] for item in items] == ["LEGACY"]

    response = client.get("/v2/sources/src1/tables", params={"readiness": "ready"})
    assert [item["table_name"] for item in response.json()["items"]] == ["ORDHDR"]


def test_list_tables_unknown_source_is_not_found(engine) -> None:
    client = _client(engine)
    response = client.get("/v2/sources/does-not-exist/tables")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_refresh_dry_run_does_not_persist_and_does_not_call_discovery(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    client = _client(engine, discovery_client=discovery)
    response = client.post(
        "/v2/sources/src1/tables/refresh",
        json={"dry_run": True},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 200
    assert response.json()["dry_run"]["would_refresh"] is True
    assert discovery.calls == []
    assert client.get("/v2/sources/src1/tables").json()["items"] == []


def test_refresh_without_discovery_client_is_service_unavailable(engine) -> None:
    client = _client(engine, discovery_client=None)
    response = client.post(
        "/v2/sources/src1/tables/refresh",
        json={},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "discovery_unavailable"


def test_refresh_persists_and_returns_before_after(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    client = _client(engine, discovery_client=discovery)
    response = client.post(
        "/v2/sources/src1/tables/refresh",
        json={},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["before"] == []
    assert len(body["after"]) == 1
    assert body["after"][0]["readiness"] == "ready"


def test_patch_table_sets_key_strategy(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")
    client = _client(engine)
    response = client.patch(
        f"/v2/tables/{record.id}",
        json={"key_strategy": "unique_index", "key_columns": ["ORDER_ID"]},
        headers={"Idempotency-Key": "k2"},
    )
    assert response.status_code == 200
    assert response.json()["after"]["key_strategy"] == "unique_index"


def test_patch_table_rrn_without_acknowledgement_is_invalid_request(engine) -> None:
    discovery = FakeDiscoveryClient((_table(has_key=False, key_columns=()),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")
    client = _client(engine)
    response = client.patch(
        f"/v2/tables/{record.id}",
        json={"key_strategy": "rrn"},
        headers={"Idempotency-Key": "k3"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_patch_table_unknown_id_is_not_found(engine) -> None:
    client = _client(engine)
    response = client.patch(
        "/v2/tables/does-not-exist",
        json={"key_strategy": "rrn", "acknowledge_rrn": True},
        headers={"Idempotency-Key": "k4"},
    )
    assert response.status_code == 404


# --- Colonnes découvertes (auto, refresh) -----------------------------------
#
# Constat du 24 septembre 2026 : ``discover`` ne remplissait jamais
# ``discovered_columns`` — l'opérateur devait toujours les déclarer à la main
# (``PUT .../discovered-columns``) même pour une table dont chaque type était
# reconnu. ``refresh`` doit maintenant les auto-déclarer quand c'est le cas.


def test_refresh_auto_declares_columns_with_recognized_types(engine) -> None:
    columns = (
        DiscoveredColumn(name="ORDER_ID", type="DECIMAL", length=7, scale=0, nullable=False),
        DiscoveredColumn(name="LABEL", type="VARCHAR", length=60, scale=None, nullable=True),
    )
    discovery = FakeDiscoveryClient((_table(columns=columns),))
    (record,) = TablesService(engine).refresh("src1", discovery_client=discovery)

    assert record.discovered_columns is not None
    by_name = {c["name"]: c for c in record.discovered_columns}
    assert by_name["ORDER_ID"]["kind"] == "decimal"
    assert by_name["ORDER_ID"]["precision"] == 7
    assert by_name["ORDER_ID"]["scale"] == 0
    assert by_name["ORDER_ID"]["nullable"] is False
    assert by_name["LABEL"]["kind"] == "varchar"
    assert by_name["LABEL"]["length"] == 60
    assert by_name["LABEL"]["nullable"] is True


def test_refresh_leaves_discovered_columns_untouched_when_a_type_is_unknown(engine) -> None:
    columns = (DiscoveredColumn(name="WEIRD", type="ROWID", length=None, scale=None, nullable=True),)
    discovery = FakeDiscoveryClient((_table(columns=columns),))
    (record,) = TablesService(engine).refresh("src1", discovery_client=discovery)

    assert record.discovered_columns is None


def test_refresh_never_overwrites_a_manual_declaration_with_an_unrecognized_refresh(engine) -> None:
    ready_columns = (
        DiscoveredColumn(name="ORDER_ID", type="INTEGER", length=None, scale=None, nullable=False),
    )
    discovery = FakeDiscoveryClient((_table(columns=ready_columns),))
    service = TablesService(engine)
    (record,) = service.refresh("src1", discovery_client=discovery)
    assert record.discovered_columns is not None

    # Un refresh suivant qui ne rapporte plus de type reconnu (ex. catalogue
    # incomplet ce coup-ci) ne doit jamais effacer une déclaration déjà
    # acquise — silencieusement perdre discovered_columns casserait le
    # chargeur de destination au prochain démarrage.
    unknown_columns = (DiscoveredColumn(name="ORDER_ID", type="ROWID", length=None, scale=None, nullable=False),)
    discovery_again = FakeDiscoveryClient((_table(columns=unknown_columns),))
    (record_again,) = service.refresh("src1", discovery_client=discovery_again)
    assert record_again.discovered_columns is not None
    assert record_again.discovered_columns == record.discovered_columns


# --- Colonnes découvertes ---------------------------------------------------

_ORDER_COLUMNS = [
    {"name": "ORDER_ID", "kind": "integer", "nullable": False},
    {"name": "LABEL", "kind": "varchar", "length": 60},
    {"name": "AMOUNT", "kind": "decimal", "precision": 9, "scale": 2},
]


def test_set_discovered_columns_persists_and_validates(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")

    updated = TablesService(engine).set_discovered_columns(record.id, columns=_ORDER_COLUMNS)

    assert updated.discovered_columns is not None
    by_name = {c["name"]: c for c in updated.discovered_columns}
    assert by_name["ORDER_ID"]["kind"] == "integer"
    assert by_name["ORDER_ID"]["nullable"] is False
    assert by_name["AMOUNT"]["precision"] == 9
    assert by_name["AMOUNT"]["scale"] == 2


def test_set_discovered_columns_rejects_unsupported_type(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")

    with pytest.raises(TableValidationError):
        TablesService(engine).set_discovered_columns(
            record.id, columns=[{"name": "X", "kind": "rowid"}]
        )


def test_set_discovered_columns_rejects_duplicate_names(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")

    with pytest.raises(TableValidationError):
        TablesService(engine).set_discovered_columns(
            record.id,
            columns=[
                {"name": "ORDER_ID", "kind": "integer"},
                {"name": "ORDER_ID", "kind": "varchar", "length": 10},
            ],
        )


def test_set_discovered_columns_unknown_table_is_not_found(engine) -> None:
    with pytest.raises(TableNotFoundError):
        TablesService(engine).set_discovered_columns("does-not-exist", columns=_ORDER_COLUMNS)


def test_put_discovered_columns_route(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")
    client = _client(engine)

    response = client.put(
        f"/v2/tables/{record.id}/discovered-columns",
        json={"columns": _ORDER_COLUMNS},
        headers={"Idempotency-Key": "k5"},
    )

    assert response.status_code == 200
    after = response.json()["after"]
    assert {c["name"] for c in after["discovered_columns"]} == {"ORDER_ID", "LABEL", "AMOUNT"}


def test_put_discovered_columns_route_invalid_payload_is_invalid_request(engine) -> None:
    discovery = FakeDiscoveryClient((_table(),))
    TablesService(engine).refresh("src1", discovery_client=discovery)
    (record,) = TablesService(engine).list("src1")
    client = _client(engine)

    response = client.put(
        f"/v2/tables/{record.id}/discovered-columns",
        json={"columns": [{"name": "X", "kind": "rowid"}]},
        headers={"Idempotency-Key": "k6"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_put_discovered_columns_route_unknown_table_is_not_found(engine) -> None:
    client = _client(engine)
    response = client.put(
        "/v2/tables/does-not-exist/discovered-columns",
        json={"columns": _ORDER_COLUMNS},
        headers={"Idempotency-Key": "k7"},
    )
    assert response.status_code == 404
