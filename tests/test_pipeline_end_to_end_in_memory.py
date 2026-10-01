"""Bout en bout en mémoire : copie initiale -> lecteur -> chargeur.

Pièce maîtresse de la correction de disposition de stockage (constat du 24
septembre 2026, premier pipeline réel sur GKE — voir
``src/quadringent/storage_layout.py``) : avant cette correction, la copie
initiale, le lecteur (mode une seule table) et le chargeur écrivaient/
lisaient sous trois préfixes différents, si bien que le chargeur ne trouvait
jamais rien et n'aurait de toute façon jamais chargé l'instantané. Ce test
échoue sur le code d'avant la correction (préfixes divergents,
``receipted_scans`` jamais activé en mode une seule table, aucun chargement
d'instantané) et passe une fois les trois composants alignés sur
``quadringent.storage_layout``.

Magasin objet : un faux client GCS en mémoire (``test_gcs_backend.
FakeGcsClient``, déjà utilisé par ``tests/test_gcs_backend.py`` et
``tests/test_v2_executor_manifest_script_contracts.py``) derrière les
adaptateurs réels (``GcsObjectStore``/``GcsCheckpointStore``) — jamais de
bucket réel. Snowflake : ``FakeStreamingClient`` (déjà utilisé par
``tests/test_snowflake_streaming_loader.py``) pour l'historique, et un
curseur factice qui matérialise le MERGE miroir dans un dict en mémoire (une
vraie base ne peut pas exécuter du SQL Snowflake dans un test unitaire, mais
la règle de dédoublonnage/précédence appliquée ici est celle de
``MirrorMergePlan.merge_sql`` : dernier ``EVENT_ID`` gagne, puis plus haute
``JOURNAL_SEQUENCE`` par clé, ``u_before`` ignoré, ``d`` supprime).

Chaque étage est piloté par l'environnement que le manifest v2 rendrait
réellement (``build_initial_copy_job``/``build_reader_deployment``/
``build_loader_deployment``), pas par des chemins codés en dur dans ce test.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from test_gcs_backend import FakeGcsClient  # noqa: E402

from quadringent.contract import ChangeEvent, JournalPosition  # noqa: E402
from quadringent.fleet_capture import FleetTableRouter, FleetWindowCoordinator  # noqa: E402
from quadringent.gcs_backend import GcsCheckpointStore, GcsObjectStore  # noqa: E402
from quadringent.object_store import RawFirstCaptureCoordinator  # noqa: E402
from quadringent.raw import RawBatchWriter  # noqa: E402
from quadringent.snowflake_streaming_loader import FakeStreamingClient, channel_name_for  # noqa: E402
from quadringent.storage_backend import StorageBackend  # noqa: E402
from quadringent.storage_layout import journal_prefix, snapshot_prefix  # noqa: E402

from quadringent_control_plane.v2.executor.boundary import JournalBoundary  # noqa: E402
from quadringent_control_plane.v2.executor.evidence import evidence_key  # noqa: E402
from quadringent_control_plane.v2.executor.manifests import (  # noqa: E402
    InitialCopyDesiredSpec,
    LoaderDesiredSpec,
    LoaderTableSpec,
    ReaderDesiredSpec,
    TableBootstrap,
    build_initial_copy_job,
    build_loader_deployment,
    build_reader_deployment,
)

import as400_snapshot_publish as publish_module  # noqa: E402
import quadringent_destination_loader as loader_module  # noqa: E402

BUCKET = "e2e-raw"
CHECKPOINT_BUCKET = "e2e-checkpoint"
PIPELINE_ID = "pipe-e2e"
TABLE_ID = "tbl-orders"
RUN_ID = "0d6a9c2e-1a2b-4c3d-9e0f-1234567890ab"
SOURCE_ID = "src-e2e"
JOURNAL_LIBRARY = "TESTLIB"
JOURNAL_NAME = "TESTJRN"
SCHEMA = "TESTLIB"
TABLE = "QDC_ORDERS"
DATABASE = "ACME_RAW"
DEST_SCHEMA = "CURATED"

BOUNDARY = JournalBoundary(
    receiver_library=JOURNAL_LIBRARY,
    receiver_name="RCV0001",
    last_sequence=100,
    observed_at=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
)

_COLUMNS = (
    {"name": "ORDER_ID", "kind": "integer", "nullable": False},
    {"name": "LABEL", "kind": "varchar", "length": 60},
)


def _container_env(manifest: dict) -> dict[str, str]:
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry["value"] for entry in container.get("env", [])}


def _job_env(manifest: dict) -> dict[str, str]:
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    return {entry["name"]: entry["value"] for entry in container.get("env", [])}


def _change_event(operation: str, *, receiver: str, seq: int, before=None, after=None) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi-e2e",
        journal=JOURNAL_NAME,
        library=SCHEMA,
        table=TABLE,
        operation=operation,
        position=JournalPosition(receiver=receiver, sequence=seq),
        commit_timestamp="2026-09-24T10:05:00.000000",
        schema_version="v1",
        before=before,
        after=after,
    )


def _compute_mirror(channel, *, key_columns: tuple[str, ...]) -> dict[tuple, dict]:
    """Même règle de dédoublonnage/précédence que ``MirrorMergePlan.merge_sql``
    (dernier EVENT_ID gagne, puis plus haute JOURNAL_SEQUENCE par clé,
    ``u_before`` ignoré, ``d`` supprime) — appliquée ici sur les lignes déjà
    vues par un ``FakeStreamingChannel``, faute de vrai moteur SQL Snowflake
    disponible dans un test unitaire."""

    by_event_id: dict[str, dict] = {}
    for row in channel.rows:
        by_event_id[row["EVENT_ID"]] = row
    latest_by_key: dict[tuple, dict] = {}
    for row in by_event_id.values():
        if row["OPERATION"] == "u_before":
            continue
        key = tuple(row[column] for column in key_columns)
        current = latest_by_key.get(key)
        if current is None or row["JOURNAL_SEQUENCE"] > current["JOURNAL_SEQUENCE"]:
            latest_by_key[key] = row
    mirror: dict[tuple, dict] = {}
    for key, row in latest_by_key.items():
        if row["OPERATION"] == "d":
            continue
        mirror[key] = {
            k: v for k, v in row.items()
            if k not in {"OPERATION", "JOURNAL_RECEIVER", "JOURNAL_SEQUENCE", "COMMIT_TIMESTAMP"}
        }
    return mirror


class _MirrorCursor:
    """Curseur factice : exécute la DDL en no-op, matérialise le MERGE miroir
    depuis les lignes déjà vues par ``channel`` (voir :func:`_compute_mirror`)."""

    def __init__(self, channel, *, key_columns: tuple[str, ...]) -> None:
        self._channel = channel
        self._key_columns = key_columns
        self.mirror: dict[tuple, dict] = {}
        self.merge_calls = 0
        self._last_row: tuple | None = None
        self._last_rows: list[tuple[str]] = []

    def execute(self, sql: str) -> None:
        if "CREATE TABLE" in sql:
            return
        if sql.startswith("SELECT EVENT_ID FROM"):
            requested = set(re.findall(r"'([0-9a-f]{64})'", sql))
            self._last_rows = [
                (row["EVENT_ID"],) for row in self._channel.rows if row["EVENT_ID"] in requested
            ]
            return
        if "MERGE INTO" in sql:
            self.merge_calls += 1
            self.mirror = _compute_mirror(self._channel, key_columns=self._key_columns)
            return
        # LagQueryPlan : aucune ligne réelle nécessaire pour ce test.
        self._last_row = (0.0,)

    def fetchone(self):
        return self._last_row

    def fetchall(self) -> list[tuple[str]]:
        return self._last_rows


class _MultiTableMirrorCursor:
    """Variante à plusieurs tables : identifie la table cible d'un MERGE par
    le nom de sa table miroir (présent dans le texte SQL — voir
    ``MirrorMergePlan.merge_sql``), pour ne jamais mélanger deux tables
    partageant le même curseur ``run_once``."""

    def __init__(self, targets: dict[str, tuple[object, tuple[str, ...]]]) -> None:
        # targets : nom de table miroir -> (channel, key_columns)
        self._targets = targets
        self.mirrors: dict[str, dict[tuple, dict]] = {name: {} for name in targets}
        self.merge_calls: dict[str, int] = {name: 0 for name in targets}
        self._last_row: tuple | None = None
        self._last_rows: list[tuple[str]] = []

    def execute(self, sql: str) -> None:
        if "CREATE TABLE" in sql:
            return
        if sql.startswith("SELECT EVENT_ID FROM"):
            requested = set(re.findall(r"'([0-9a-f]{64})'", sql))
            for name, (channel, _) in self._targets.items():
                history_name = name.removesuffix("_MIRROR") + "_HISTORY"
                if f'"{history_name}"' in sql:
                    self._last_rows = [
                        (row["EVENT_ID"],) for row in channel.rows if row["EVENT_ID"] in requested
                    ]
                    return
            raise AssertionError(f"cible de l'historique non reconnue : {sql[:200]!r}")
        if "MERGE INTO" in sql:
            for name, (channel, key_columns) in self._targets.items():
                if f'"{name}"' in sql:
                    self.merge_calls[name] += 1
                    self.mirrors[name] = _compute_mirror(channel, key_columns=key_columns)
                    return
            raise AssertionError(f"cible du MERGE non reconnue : {sql[:200]!r}")
        self._last_row = (0.0,)

    def fetchone(self):
        return self._last_row

    def fetchall(self) -> list[tuple[str]]:
        return self._last_rows


class PipelineEndToEndTests(unittest.TestCase):
    def test_snapshot_then_journal_load_without_duplication_and_idempotent_restart(self) -> None:
        client = FakeGcsClient()
        table_bootstrap = TableBootstrap(
            table_id=TABLE_ID, schema_name=SCHEMA, table_name=TABLE, boundary=BOUNDARY
        )

        # --- 1. Manifestes réels, un par composant --------------------------
        reader_manifest = build_reader_deployment(
            ReaderDesiredSpec(
                source_id=SOURCE_ID,
                journal_library=JOURNAL_LIBRARY,
                journal_name=JOURNAL_NAME,
                image="registry.example.test/quadringent/capture:1.0.0",
                namespace="quadringent",
                storage_backend="gcs",
                source_time_zone="Europe/Paris",
                raw_prefix="qqual/pending",
                reader_timeout_seconds=300,
                tables=(table_bootstrap,),
                destination_secret_ref="qdt-destination-dst-1",
                ibmi_secret_ref="qdt-source-src-1",
                service_account_name="quadringent-capture",
                ibmi_host="as400.example.test",
                ibmi_user="TESTUSER",
                raw_bucket=BUCKET,
                checkpoint_location=CHECKPOINT_BUCKET,
            )
        )
        reader_env = _container_env(reader_manifest)

        run_evidence_key = evidence_key("qqual/pending", TABLE_ID, RUN_ID)
        copy_manifest = build_initial_copy_job(
            InitialCopyDesiredSpec(
                pipeline_id=PIPELINE_ID,
                table_id=TABLE_ID,
                schema_name=SCHEMA,
                table_name=TABLE,
                source_id=SOURCE_ID,
                image="registry.example.test/quadringent/capture:1.0.0",
                namespace="quadringent",
                storage_backend="gcs",
                source_time_zone="Europe/Paris",
                raw_prefix="qqual/pending",
                boundary=BOUNDARY,
                run_id=RUN_ID,
                evidence_key=run_evidence_key,
                destination_secret_ref="qdt-destination-dst-1",
                ibmi_secret_ref="qdt-source-src-1",
                service_account_name="quadringent-capture",
                ibmi_host="as400.example.test",
                ibmi_user="TESTUSER",
                raw_bucket=BUCKET,
            )
        )
        copy_env = _job_env(copy_manifest)

        loader_table = LoaderTableSpec(
            table_id=TABLE_ID,
            schema_name=SCHEMA,
            table_name=TABLE,
            key_columns=("ORDER_ID",),
            columns=_COLUMNS,
            evidence_key=run_evidence_key,
        )
        loader_manifest = build_loader_deployment(
            LoaderDesiredSpec(
                destination_id="dst-1",
                image="registry.example.test/quadringent/capture:1.0.0",
                namespace="quadringent",
                storage_backend="gcs",
                raw_bucket=BUCKET,
                raw_prefix="qqual/pending",
                checkpoint_location=CHECKPOINT_BUCKET,
                destination_database=DATABASE,
                destination_schema=DEST_SCHEMA,
                tables=(loader_table,),
                destination_secret_ref="qdt-destination-dst-1",
                service_account_name="quadringent-capture",
            )
        )
        loader_env = _container_env(loader_manifest)

        # Les trois composants s'accordent sur la même disposition.
        self.assertEqual(reader_env["AS400_RAW_PREFIX"], journal_prefix("qqual/pending", TABLE))
        self.assertEqual(reader_env["AS400_RECEIPTED_SCANS"], "true")
        self.assertNotIn("AS400_FLEET_TABLE_ROOT", reader_env)  # mono-table : jamais le mode flotte
        self.assertEqual(
            loader_module.raw_prefix_for_table(loader_env["AS400_RAW_PREFIX"], SCHEMA, TABLE),
            reader_env["AS400_RAW_PREFIX"],
        )

        # --- 2. Copie initiale : faux instantané Java, publication réelle ---
        with tempfile.TemporaryDirectory() as snapshot_dir:
            snapshot_root = Path(snapshot_dir)
            snapshot_event = _change_event(
                "c", receiver="SNAPSHOT-" + RUN_ID, seq=1, after={"ORDER_ID": 3, "LABEL": "depuis l'instantané"}
            )
            manifest = RawBatchWriter(str(snapshot_root)).write_batch(
                [snapshot_event], high_watermark=snapshot_event.position
            )
            store_for_publish = GcsObjectStore(BUCKET, client=client)
            snapshot_batches = publish_module.publish_snapshot_batches(
                root=snapshot_root, bucket=BUCKET, prefix=copy_env["AS400_RAW_PREFIX"], table=TABLE,
                backend="gcs", store=store_for_publish,
            )
        self.assertEqual(
            {entry["payload_key"] for entry in snapshot_batches},
            {f"batch-{manifest.batch_id}.jsonl"},
        )

        from quadringent_control_plane.v2.executor.evidence import InitialCopyEvidence, SnapshotBatchRef

        evidence = InitialCopyEvidence(
            pipeline_id=PIPELINE_ID,
            table_id=TABLE_ID,
            run_id=RUN_ID,
            boundary=BOUNDARY,
            rows_copied=1,
            completed_at=datetime(2026, 9, 24, 10, 4, tzinfo=timezone.utc),
            snapshot_batches=tuple(SnapshotBatchRef.from_dict(entry) for entry in snapshot_batches),
        )
        GcsObjectStore(BUCKET, "", client=client).put_once(
            copy_env["AS400_EVIDENCE_KEY"],
            __import__("json").dumps(evidence.to_dict()).encode("utf-8"),
        )

        # --- 3. Cœur du lecteur : faux journal, disposition réelle du manifest --
        reader_store = GcsObjectStore(BUCKET, reader_env["AS400_RAW_PREFIX"], client=client)
        reader_checkpoint = GcsCheckpointStore(CHECKPOINT_BUCKET, reader_env["AS400_STREAM_KEY"], client=client)
        coordinator = RawFirstCaptureCoordinator(reader_store, reader_checkpoint)

        receiver = BOUNDARY.receiver_name
        events = [
            _change_event("c", receiver=receiver, seq=101, after={"ORDER_ID": 1, "LABEL": "initiale"}),
            _change_event(
                "u", receiver=receiver, seq=102,
                before={"ORDER_ID": 1, "LABEL": "initiale"}, after={"ORDER_ID": 1, "LABEL": "mise à jour"},
            ),
            _change_event("c", receiver=receiver, seq=103, after={"ORDER_ID": 2, "LABEL": "à supprimer"}),
            _change_event("d", receiver=receiver, seq=104, before={"ORDER_ID": 2, "LABEL": "à supprimer"}),
        ]
        end = JournalPosition(receiver=receiver, sequence=104)
        start = JournalPosition(receiver=receiver, sequence=101)
        with tempfile.TemporaryDirectory() as staging:
            journal_manifest = RawBatchWriter(staging).write_batch(events, high_watermark=end)
            payload = (Path(staging) / f"batch-{journal_manifest.batch_id}.jsonl").read_bytes()
            manifest_content = (Path(staging) / f"batch-{journal_manifest.batch_id}.manifest.json").read_bytes()
        coordinator.capture_receipted_window_result(
            start=start, end=end, previous=None,
            manifest_content=manifest_content, payload=payload,
            scan_completed_at=datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc),
        )

        # --- 4. Cœur du chargeur : run_once vers un faux puits Snowflake ------
        storage = StorageBackend(kind="gcs", raw_bucket=BUCKET, state_location=CHECKPOINT_BUCKET, gcs_client=client)
        tables = loader_module.parse_table_set(loader_env["QUADRINGENT_LOADER_TABLE_SET_JSON"])
        (table,) = tables
        plan = loader_module.build_plan(table, database=DATABASE, schema=DEST_SCHEMA)
        streaming_client = FakeStreamingClient()
        channel_name = channel_name_for(plan.scope, plan.history_table, loader_module.loader_stream_id(table))

        def streaming_client_factory(_history_table: str) -> FakeStreamingClient:
            return streaming_client

        channel = streaming_client.open_channel(channel_name)
        cursor = _MirrorCursor(channel, key_columns=("ORDER_ID",))

        loader_module.run_once(
            storage=storage, tables=tables, raw_prefix_root=loader_env["AS400_RAW_PREFIX"],
            database=DATABASE, schema=DEST_SCHEMA,
            streaming_client_factory=streaming_client_factory, cursor=cursor,
        )

        # L'historique porte l'instantané et les quatre événements, sans doublon.
        history_event_ids = [row["EVENT_ID"] for row in channel.rows]
        self.assertEqual(len(history_event_ids), len(set(history_event_ids)))
        self.assertEqual(len(history_event_ids), 5)  # 1 instantané + 4 événements
        self.assertIn(snapshot_event.event_id, history_event_ids)
        for event in events:
            self.assertIn(event.event_id, history_event_ids)

        # Le miroir reflète l'état attendu : instantané visible, mise à jour
        # appliquée, suppression effective.
        mirror_by_key = {key[0]: row["LABEL"] for key, row in cursor.mirror.items()}
        self.assertEqual(mirror_by_key, {3: "depuis l'instantané", 1: "mise à jour"})
        self.assertNotIn(2, mirror_by_key)
        self.assertGreaterEqual(cursor.merge_calls, 1)

        # --- 5. Idempotence : une relance ne change rien -----------------------
        rows_before_restart = len(channel.rows)
        merges_before_restart = cursor.merge_calls

        def no_streaming_client_without_new_receipts(_history_table: str) -> FakeStreamingClient:
            raise AssertionError("un cycle inactif ne doit pas ouvrir de canal Snowpipe")

        loader_module.run_once(
            storage=storage, tables=tables, raw_prefix_root=loader_env["AS400_RAW_PREFIX"],
            database=DATABASE, schema=DEST_SCHEMA,
            streaming_client_factory=no_streaming_client_without_new_receipts, cursor=cursor,
        )
        self.assertEqual(len(channel.rows), rows_before_restart)
        self.assertEqual(cursor.merge_calls, merges_before_restart)
        mirror_after_restart = {key[0]: row["LABEL"] for key, row in cursor.mirror.items()}
        self.assertEqual(mirror_after_restart, {3: "depuis l'instantané", 1: "mise à jour"})


TAIL_TABLE_ID = "tbl-tail"
TAIL_TABLE = "QDC_TAIL"

_TAIL_COLUMNS = (
    {"name": "ORDER_ID", "kind": "integer", "nullable": False},
    {"name": "LABEL", "kind": "varchar", "length": 60},
)


class FleetPipelineEndToEndTests(unittest.TestCase):
    """Deux tables sur un même journal (mode flotte) — constat en réel sur
    GKE après la correction de disposition mono-table : le lecteur écrit le
    lot combiné de la fenêtre et le reçu à la RACINE du préfixe (une seule
    lecture de journal pour toute la flotte), et le lot routé par table sous
    ``<racine>/<table>/journal/`` (voir ``quadringent.fleet_capture.
    FleetWindowCoordinator``). Avant la correction, le chargeur ne cherchait
    les reçus que sous le préfixe de la table et ne trouvait donc jamais
    rien — ce test échoue sans ``discover_new_fleet_batches``."""

    def test_two_tables_sharing_a_journal_are_loaded_without_mixing_events(self) -> None:
        client = FakeGcsClient()
        orders_bootstrap = TableBootstrap(table_id=TABLE_ID, schema_name=SCHEMA, table_name=TABLE, boundary=BOUNDARY)
        tail_bootstrap = TableBootstrap(
            table_id=TAIL_TABLE_ID, schema_name=SCHEMA, table_name=TAIL_TABLE, boundary=BOUNDARY
        )

        reader_manifest = build_reader_deployment(
            ReaderDesiredSpec(
                source_id=SOURCE_ID,
                journal_library=JOURNAL_LIBRARY,
                journal_name=JOURNAL_NAME,
                image="registry.example.test/quadringent/capture:1.0.0",
                namespace="quadringent",
                storage_backend="gcs",
                source_time_zone="Europe/Paris",
                raw_prefix="qqual/pending",
                reader_timeout_seconds=300,
                tables=(orders_bootstrap, tail_bootstrap),
                destination_secret_ref="qdt-destination-dst-1",
                ibmi_secret_ref="qdt-source-src-1",
                service_account_name="quadringent-capture",
                ibmi_host="as400.example.test",
                ibmi_user="TESTUSER",
                raw_bucket=BUCKET,
                checkpoint_location=CHECKPOINT_BUCKET,
            )
        )
        reader_env = _container_env(reader_manifest)
        self.assertEqual(reader_env["AS400_FLEET_TABLE_ROOT"], "qqual/pending")
        self.assertEqual(set(reader_env["AS400_FLEET_TABLES"].split(",")), {"QDC_ORDERS", "QDC_TAIL"})
        self.assertEqual(reader_env["AS400_RAW_PREFIX"], "qqual/pending")  # racine, jamais routé en mode flotte
        self.assertEqual(reader_env["AS400_RECEIPTED_SCANS"], "true")

        loader_tables = (
            LoaderTableSpec(
                table_id=TABLE_ID, schema_name=SCHEMA, table_name=TABLE,
                key_columns=("ORDER_ID",), columns=_COLUMNS,
            ),
            LoaderTableSpec(
                table_id=TAIL_TABLE_ID, schema_name=SCHEMA, table_name=TAIL_TABLE,
                key_columns=("ORDER_ID",), columns=_TAIL_COLUMNS,
            ),
        )
        loader_manifest = build_loader_deployment(
            LoaderDesiredSpec(
                destination_id="dst-1",
                image="registry.example.test/quadringent/capture:1.0.0",
                namespace="quadringent",
                storage_backend="gcs",
                raw_bucket=BUCKET,
                raw_prefix="qqual/pending",
                checkpoint_location=CHECKPOINT_BUCKET,
                destination_database=DATABASE,
                destination_schema=DEST_SCHEMA,
                tables=loader_tables,
                destination_secret_ref="qdt-destination-dst-1",
                service_account_name="quadringent-capture",
            )
        )
        loader_env = _container_env(loader_manifest)

        # --- Cœur du lecteur : un journal partagé, la disposition réelle du
        # lecteur en mode flotte (FleetWindowCoordinator/FleetTableRouter,
        # jamais reconstruite à la main dans ce test).
        root_object_store = GcsObjectStore(BUCKET, reader_env["AS400_FLEET_TABLE_ROOT"], client=client)
        root_checkpoint = GcsCheckpointStore(CHECKPOINT_BUCKET, reader_env["AS400_STREAM_KEY"], client=client)
        router = FleetTableRouter(
            root=reader_env["AS400_FLEET_TABLE_ROOT"],
            tables=tuple(reader_env["AS400_FLEET_TABLES"].split(",")),
            store_factory=lambda prefix: GcsObjectStore(BUCKET, prefix, client=client),
            checkpoint_factory=lambda key: GcsCheckpointStore(CHECKPOINT_BUCKET, key, client=client),
        )
        coordinator = FleetWindowCoordinator(
            RawFirstCaptureCoordinator(root_object_store, root_checkpoint), router
        )

        def _event(table: str, operation: str, *, seq: int, before=None, after=None) -> ChangeEvent:
            return ChangeEvent(
                source_system="ibmi-e2e", journal=JOURNAL_NAME, library=SCHEMA, table=table, operation=operation,
                position=JournalPosition(receiver=receiver, sequence=seq),
                commit_timestamp=f"2026-09-24T10:05:0{seq - 100}.000000",
                schema_version="v1", before=before, after=after,
            )

        receiver = BOUNDARY.receiver_name
        events = [
            _event(TAIL_TABLE, "c", seq=101, after={"ORDER_ID": 1, "LABEL": "tail initiale"}),
            _event(
                TAIL_TABLE, "u", seq=102,
                before={"ORDER_ID": 1, "LABEL": "tail initiale"}, after={"ORDER_ID": 1, "LABEL": "tail mise à jour"},
            ),
            _event(TAIL_TABLE, "d", seq=103, before={"ORDER_ID": 1, "LABEL": "tail mise à jour"}),
            _event(TABLE, "c", seq=104, after={"ORDER_ID": 9, "LABEL": "orders seule ligne"}),
        ]
        end = JournalPosition(receiver=receiver, sequence=104)
        start = JournalPosition(receiver=receiver, sequence=101)
        with tempfile.TemporaryDirectory() as staging:
            fleet_manifest = RawBatchWriter(staging).write_batch(events, high_watermark=end)
            payload = (Path(staging) / f"batch-{fleet_manifest.batch_id}.jsonl").read_bytes()
            manifest_content = (Path(staging) / f"batch-{fleet_manifest.batch_id}.manifest.json").read_bytes()
        coordinator.capture_receipted_window_result(
            start=start, end=end, previous=None,
            manifest_content=manifest_content, payload=payload,
            scan_completed_at=datetime(2026, 9, 24, 10, 6, tzinfo=timezone.utc),
        )

        # Le lot combiné à la racine n'est pas un doublon fautif : c'est la
        # preuve de fenêtre, commune à toute la flotte (voir
        # discover_new_fleet_batches). ``put_once`` renvoie False : l'objet
        # existe déjà, avec exactement ce contenu.
        self.assertFalse(root_object_store.put_once(f"batch-{fleet_manifest.batch_id}.jsonl", payload))
        # Le lot routé par table existe (FleetTableRouter), mais jamais de
        # reçu à cet endroit : discover_new_batches (préfixe de table) n'y
        # trouve rien, c'est discover_new_fleet_batches (racine) qui charge.
        tail_routed_store = GcsObjectStore(BUCKET, journal_prefix("qqual/pending", TAIL_TABLE), client=client)
        self.assertEqual(tail_routed_store.list_receipt_keys(10), ())

        # --- Cœur du chargeur : les deux tables, sans mélanger leurs événements --
        storage = StorageBackend(kind="gcs", raw_bucket=BUCKET, state_location=CHECKPOINT_BUCKET, gcs_client=client)
        tables = loader_module.parse_table_set(loader_env["QUADRINGENT_LOADER_TABLE_SET_JSON"])
        plans = {t.table_name: loader_module.build_plan(t, database=DATABASE, schema=DEST_SCHEMA) for t in tables}
        streaming_client = FakeStreamingClient()

        def streaming_client_factory(_history_table: str) -> FakeStreamingClient:
            return streaming_client

        channels = {
            t.table_name: streaming_client.open_channel(
                channel_name_for(plans[t.table_name].scope, plans[t.table_name].history_table, f"{SCHEMA}/{t.table_name}")
            )
            for t in tables
        }
        cursor = _MultiTableMirrorCursor(
            {
                plans[t.table_name].mirror_table: (channels[t.table_name], ("ORDER_ID",))
                for t in tables
            }
        )

        loader_module.run_once(
            storage=storage, tables=tables, raw_prefix_root=loader_env["AS400_RAW_PREFIX"],
            database=DATABASE, schema=DEST_SCHEMA,
            streaming_client_factory=streaming_client_factory, cursor=cursor,
        )

        tail_history_ids = [row["EVENT_ID"] for row in channels[TAIL_TABLE].rows]
        orders_history_ids = [row["EVENT_ID"] for row in channels[TABLE].rows]
        self.assertEqual(len(tail_history_ids), 3)  # insertion, mise à jour, suppression
        self.assertEqual(len(orders_history_ids), 1)
        self.assertEqual(set(tail_history_ids) & set(orders_history_ids), set())  # jamais mélangés

        tail_mirror = cursor.mirrors[plans[TAIL_TABLE].mirror_table]
        orders_mirror = cursor.mirrors[plans[TABLE].mirror_table]
        self.assertEqual(tail_mirror, {})  # supprimée
        self.assertEqual(
            {key[0]: row["LABEL"] for key, row in orders_mirror.items()},
            {9: "orders seule ligne"},
        )

        # --- Idempotence : une relance ne change rien ----------------------
        tail_rows_before = len(channels[TAIL_TABLE].rows)
        orders_rows_before = len(channels[TABLE].rows)
        tail_merges_before = cursor.merge_calls[plans[TAIL_TABLE].mirror_table]
        orders_merges_before = cursor.merge_calls[plans[TABLE].mirror_table]
        loader_module.run_once(
            storage=storage, tables=tables, raw_prefix_root=loader_env["AS400_RAW_PREFIX"],
            database=DATABASE, schema=DEST_SCHEMA,
            streaming_client_factory=streaming_client_factory, cursor=cursor,
        )
        self.assertEqual(len(channels[TAIL_TABLE].rows), tail_rows_before)
        self.assertEqual(len(channels[TABLE].rows), orders_rows_before)
        self.assertEqual(cursor.merge_calls[plans[TAIL_TABLE].mirror_table], tail_merges_before)
        self.assertEqual(cursor.merge_calls[plans[TABLE].mirror_table], orders_merges_before)


if __name__ == "__main__":
    unittest.main()
