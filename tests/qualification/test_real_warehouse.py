"""Le contrôle Snowflake charge le brut produit et relit l'historique réel."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quadringent_qualification.config import (
    CaptureConfig, RunConfig, SourceConfig, StorageConfig, WarehouseConfig,
)
from quadringent_qualification.real_warehouse import (
    SnowflakeQualificationWarehouse, qualification_schema_suffix,
)
from quadringent_qualification.schema import Column, TableSchema
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, publish_raw_batch
from quadringent.storage_layout import journal_prefix, snapshot_prefix
from quadringent_control_plane.v2.executor.boundary import JournalBoundary
from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef


RUN_ID = "00000000-0000-4000-8000-000000000001"
SCHEMA = "QUALSCHEMA" + qualification_schema_suffix(RUN_ID)


def _config(secret_file: Path) -> RunConfig:
    return RunConfig(
        run_id=RUN_ID,
        table=TableSchema(
            "QUALTEST.QUALIF_ORDERS",
            (Column("ID", "integer"), Column("LABEL", "varchar", length=20)),
            "ID",
        ),
        source=SourceConfig("ibmi_java", ("QUALTEST",), "/private/source.json", "QUALTEST", "QUALJRN"),
        capture=CaptureConfig("sha256:" + "a" * 64),
        storage=StorageConfig("gcs", "qual-bucket", f"qualification/{RUN_ID}", "qual-state"),
        warehouse=WarehouseConfig("snowflake", str(secret_file), "QUALDB", SCHEMA),
        steps=("snapshot", "capture", "reconcile"),
    )


class Cursor:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.sql.append(sql)

    def fetchone(self) -> tuple[int, int]:
        return 3, 3

    def fetchall(self) -> list[tuple[object, ...]]:
        return [
            ("event-1", "c", f"SNAPSHOT:{RUN_ID}", 1, 1, "first"),
            ("event-2", "u_before", "QUALRCV1", 20, 1, "first"),
            ("event-3", "u_after", "QUALRCV1", 21, 1, "second"),
        ]

    def close(self) -> None:
        self.closed = True


class Connection:
    def __init__(self) -> None:
        self.latest_cursor = Cursor()
        self.closed = False

    def cursor(self) -> Cursor:
        return self.latest_cursor

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "snowflake.json"
    path.write_text(json.dumps({
        "account": "example-account", "user": "QUALUSER", "role": "QDT_ROLE_QUAL",
        "private_key_pem": "fixture-private-key",
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_charge_par_le_moteur_sql_produit_dans_un_run_isole(secret_file: Path) -> None:
    connections: list[Connection] = []
    calls: list[dict[str, object]] = []
    storage = object()

    def connect(**kwargs: object) -> Connection:
        assert "timeout_seconds" not in kwargs
        assert kwargs["private_key_pem"] == "fixture-private-key"
        assert kwargs["warehouse"] == "QDT_WH_QUAL"
        connection = Connection()
        connections.append(connection)
        return connection

    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=storage, connection_factory=connect,
        loader=lambda **kwargs: calls.append(kwargs),
    )
    warehouse.load(raw_prefix=f"qualification/{RUN_ID}")

    assert len(calls) == 1
    assert calls[0]["storage"] is storage
    assert calls[0]["history_mode"] == "sql"
    assert calls[0]["raw_prefix_root"] == f"qualification/{RUN_ID}"
    assert calls[0]["database"] == "QUALDB"
    assert calls[0]["schema"] == SCHEMA
    table = calls[0]["tables"][0]
    assert table.schema_name == "QUALTEST"
    assert table.table_name == "QUALIF_ORDERS"
    assert table.evidence_key == f"qualification/{RUN_ID}/qual-{RUN_ID}/evidence/{RUN_ID}.json"
    assert connections[0].closed and connections[0].latest_cursor.closed


def test_relit_les_lignes_snowflake_sans_inventer_le_pendant_avant(secret_file: Path) -> None:
    connections: list[Connection] = []

    def connect(**_kwargs: object) -> Connection:
        connection = Connection()
        connections.append(connection)
        return connection

    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(), connection_factory=connect,
    )
    events = warehouse.fetch_events(schema=SCHEMA)
    assert events == [
        {"event_id": "event-1", "receiver": f"SNAPSHOT:{RUN_ID}", "sequence": 1, "operation": "c",
         "payload": {"after": {"ID": 1, "LABEL": "first"}}, "is_snapshot": True},
        {"event_id": "event-2", "receiver": "QUALRCV1", "sequence": 20, "operation": "u_before",
         "payload": {"before": {"ID": 1, "LABEL": "first"}}, "is_snapshot": False},
        {"event_id": "event-3", "receiver": "QUALRCV1", "sequence": 21, "operation": "u_after",
         "payload": {"after": {"ID": 1, "LABEL": "second"}}, "is_snapshot": False},
    ]
    assert all(connection.closed and connection.latest_cursor.closed for connection in connections)
    assert all(warehouse.plan.qualified_history_table in sql
               for connection in connections for sql in connection.latest_cursor.sql)


def test_history_keeps_loaded_event_ids_and_physical_snapshot_duplicates(secret_file):
    connection = Connection()
    physical_row = ("observed-id", "c", f"SNAPSHOT:{RUN_ID}", 1, 1, "first")
    connection.latest_cursor.fetchall = lambda: [physical_row, physical_row]
    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(), connection_factory=lambda **_: connection,
    )

    events = warehouse.fetch_events(schema=SCHEMA)

    assert len(events) == 2
    assert [event.get("event_id") for event in events] == ["observed-id", "observed-id"]
    assert all(event["is_snapshot"] for event in events)
    assert connection.closed and connection.latest_cursor.closed


def _event(receiver: str, sequence: int, label: str) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi", journal="QUALJRN", library="QUALTEST", table="QUALIF_ORDERS",
        operation="c", position=JournalPosition(receiver, sequence),
        commit_timestamp="2026-09-29T10:00:00Z", schema_version="v1",
        before=None, after={"ID": sequence, "LABEL": label},
    )


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def object_store(self, prefix: str) -> FileObjectStore:
        return FileObjectStore(self.root / prefix)


def test_compte_les_lots_bruts_valides_et_les_tentatives_de_rejeu(secret_file: Path, tmp_path: Path) -> None:
    config = _config(secret_file)
    storage = FileStorage(tmp_path / "objects")
    warehouse = SnowflakeQualificationWarehouse(config, storage_backend=storage)
    snapshot_event = _event(f"SNAPSHOT:{RUN_ID}", 1, "initial")
    snapshot_store = storage.object_store(snapshot_prefix(config.storage.raw_prefix, warehouse.table.table_name))
    snapshot = publish_raw_batch(snapshot_store, [snapshot_event], high_watermark=snapshot_event.position)
    boundary = JournalBoundary(
        receiver_library="QUALTEST", receiver_name="QUALRCV1", last_sequence=10,
        observed_at=datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc),
    )
    evidence = InitialCopyEvidence(
        pipeline_id="qual-pipeline", table_id=warehouse.table.table_id, run_id=RUN_ID,
        boundary=boundary, rows_copied=1, completed_at=datetime(2026, 9, 29, 10, 1, tzinfo=timezone.utc),
        snapshot_batches=(SnapshotBatchRef(snapshot.payload_key, snapshot.manifest_key),),
    )
    storage.object_store("").put_once(
        warehouse.table.evidence_key, json.dumps(evidence.to_dict()).encode("utf-8"),
    )
    journal_store = storage.object_store(journal_prefix(config.storage.raw_prefix, warehouse.table.table_name))
    journal_event = _event("QUALRCV1", 11, "after")
    journal = publish_raw_batch(journal_store, [journal_event], high_watermark=journal_event.position)
    for number in (1, 2):
        journal_store.put_once(
            f"receipts/{number}.json",
            json.dumps({"raw": {"payload_key": journal.payload_key, "manifest_key": journal.manifest_key}}).encode(),
        )

    assert warehouse.fetch_raw_counts(schema=SCHEMA) == (3, 2)
    fleet_store = storage.object_store(config.storage.raw_prefix)
    other_table_event = replace(journal_event, table="UNRELATED", position=JournalPosition("QUALRCV1", 12))
    fleet = publish_raw_batch(
        fleet_store, [journal_event, other_table_event], high_watermark=other_table_event.position,
    )
    fleet_store.put_once(
        "receipts/fleet.json",
        json.dumps({"raw": {"payload_key": fleet.payload_key, "manifest_key": fleet.manifest_key}}).encode(),
    )
    assert warehouse.fetch_raw_counts(schema=SCHEMA) == (4, 2)
    replay = warehouse.fetch_raw_evidence(schema=SCHEMA)
    assert replay.replayed_identical == 2
    assert replay.replayed_divergent == 0
    assert replay.identical_event_ids == (journal_event.event_id,)

    conflicting = replace(journal_event, after={"ID": 11, "LABEL": "divergent"})
    conflict_batch = publish_raw_batch(journal_store, [conflicting], high_watermark=conflicting.position)
    journal_store.put_once("receipts/conflict.json", json.dumps({"raw": {
        "payload_key": conflict_batch.payload_key, "manifest_key": conflict_batch.manifest_key,
    }}).encode())
    replay = warehouse.fetch_raw_evidence(schema=SCHEMA)
    assert (replay.raw_rows, replay.raw_distinct_events) == (5, 2)
    assert replay.replayed_divergent > 0
    assert replay.divergent_event_ids == (journal_event.event_id,)


def test_relit_le_miroir_reel_sans_deduire_son_contenu_de_l_historique(secret_file):
    connection = Connection()
    connection.latest_cursor.fetchall = lambda: [(1, "different"), (1, "duplicate")]
    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(), connection_factory=lambda **_: connection,
    )
    assert warehouse.fetch_mirror_rows(schema=SCHEMA) == [
        {"ID": 1, "LABEL": "different"}, {"ID": 1, "LABEL": "duplicate"},
    ]
    assert warehouse.plan.qualified_mirror_table in connection.latest_cursor.sql[0]
    assert "LIMIT" in connection.latest_cursor.sql[0]
    assert connection.closed and connection.latest_cursor.closed


@pytest.mark.parametrize("rows", [[(1,)], [(1, "a"), (2, "b")]])
def test_lecture_miroir_refuse_forme_ou_budget_invalide(secret_file, monkeypatch, rows):
    monkeypatch.setattr("quadringent_qualification.real_warehouse._MAX_EVENTS", 1)
    connection = Connection()
    connection.latest_cursor.fetchall = lambda: rows
    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(), connection_factory=lambda **_: connection,
    )
    with pytest.raises(ValueError, match="mirror"):
        warehouse.fetch_mirror_rows(schema=SCHEMA)
    assert connection.closed and connection.latest_cursor.closed


def test_refuse_schemas_partages_et_run_id_non_compatible_snapshot(secret_file: Path) -> None:
    config = _config(secret_file)
    with pytest.raises(ValueError, match="isolated"):
        SnowflakeQualificationWarehouse(
            replace(config, warehouse=replace(config.warehouse, schema_name="SHARED")),
            storage_backend=object(),
        )
    with pytest.raises(ValueError, match="canonical UUID"):
        SnowflakeQualificationWarehouse(
            replace(config, run_id="qual-test-1", storage=replace(config.storage, raw_prefix="qualification/qual-test-1")),
            storage_backend=object(),
        )


def test_refuse_un_autre_prefixe_et_un_autre_schema_avant_connexion(secret_file: Path) -> None:
    calls: list[object] = []
    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(),
        connection_factory=lambda **kwargs: calls.append(kwargs),
    )
    with pytest.raises(ValueError, match="run prefix"):
        warehouse.load(raw_prefix="qualification/another-run")
    with pytest.raises(ValueError, match="schema"):
        warehouse.fetch_events(schema="OTHER")
    with pytest.raises(ValueError, match="schema"):
        warehouse.fetch_mirror_rows(schema="OTHER")
    with pytest.raises(ValueError, match="schema"):
        warehouse.fetch_raw_evidence(schema="OTHER")
    assert calls == []


def test_secret_snowflake_doit_rester_hors_des_arguments_et_prive(secret_file: Path) -> None:
    secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="Snowflake credential"):
        SnowflakeQualificationWarehouse(_config(secret_file), storage_backend=object())
    secret_file.chmod(0o600)
    link = secret_file.with_name("linked.json")
    link.symlink_to(secret_file)
    with pytest.raises(ValueError, match="Snowflake credential"):
        SnowflakeQualificationWarehouse(_config(link), storage_backend=object())


@pytest.mark.parametrize('rows,expected', [([], None), ([("marker-1",)], "marker-1")])
def test_probe_relit_le_miroir_par_select_parametre_et_ferme_la_connexion(secret_file, rows, expected):
    connections = []

    class ProbeCursor(Cursor):
        def execute(self, sql, parameters=None):
            self.sql.append((sql, parameters))

        def fetchall(self):
            return rows

    def connect(**kwargs):
        assert kwargs["timeout_seconds"] == 2
        connection = Connection()
        connection.latest_cursor = ProbeCursor()
        connections.append(connection)
        return connection

    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(), connection_factory=connect,
    )
    assert warehouse.fetch_mirror_value(schema=SCHEMA, row_key=1, column='LABEL', timeout_seconds=2) == expected
    connection = connections[0]
    assert connection.closed and connection.latest_cursor.closed
    statements = connection.latest_cursor.sql
    assert statements[0] == ('ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 2', None)
    assert 'WHERE ID = %s LIMIT 2' in statements[1][0]
    assert warehouse.plan.qualified_mirror_table in statements[1][0]
    assert statements[1][1] == (1,)


@pytest.mark.parametrize('cleanup_error', [False, True])
def test_probe_refuse_doublons_ou_erreur_cleanup_sans_masquer_la_fermeture(secret_file, cleanup_error):
    connection = Connection()

    class ProbeCursor(Cursor):
        def execute(self, sql, parameters=None):
            pass

        def fetchall(self):
            return [('marker',), ('marker',)]

        def close(self):
            super().close()
            if cleanup_error:
                raise RuntimeError('cleanup failed')

    connection.latest_cursor = ProbeCursor()
    warehouse = SnowflakeQualificationWarehouse(
        _config(secret_file), storage_backend=object(), connection_factory=lambda **kwargs: connection,
    )
    with pytest.raises((ValueError, RuntimeError)):
        warehouse.fetch_mirror_value(schema=SCHEMA, row_key=1, column='LABEL', timeout_seconds=1)
    assert connection.closed and connection.latest_cursor.closed


@pytest.mark.parametrize('timeout', [None, 3])
def test_connecteur_natif_transmet_les_trois_timeouts_seulement_sur_demande(monkeypatch, timeout):
    import sys
    from types import SimpleNamespace
    from scripts import quadringent_destination_loader as loader

    calls = []
    connector = SimpleNamespace(connect=lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setitem(sys.modules, 'snowflake', SimpleNamespace(connector=connector))
    monkeypatch.setitem(sys.modules, 'snowflake.connector', connector)
    serialization = SimpleNamespace(
        load_pem_private_key=lambda *args, **kwargs: SimpleNamespace(private_bytes=lambda *args: b'key'),
        Encoding=SimpleNamespace(DER='der'), PrivateFormat=SimpleNamespace(PKCS8='pkcs8'),
        NoEncryption=lambda: None,
    )
    monkeypatch.setitem(sys.modules, 'cryptography', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'cryptography.hazmat', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'cryptography.hazmat.primitives', SimpleNamespace(serialization=serialization))
    loader._connect_snowflake(account='example', user='USER', role='ROLE', private_key_pem='fixture',
                              warehouse='WAREHOUSE', timeout_seconds=timeout)
    actual = {name: calls[0][name] for name in ('login_timeout', 'network_timeout', 'socket_timeout') if name in calls[0]}
    assert actual == ({} if timeout is None else {'login_timeout': 3, 'network_timeout': 3, 'socket_timeout': 3})
