from __future__ import annotations

import site_fixture

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import re
import unittest

from quadringent.fleet_load_ledger import (
    FleetLoadLedger,
    TableLedger,
    fleet_load_ledger_key,
)
from quadringent.fleet_load_probe import (
    FleetLoadMeasurement,
    FleetTableMeasurement,
    attach_fleet_destination_proof,
    fleet_console_proof_key,
    fleet_console_proof_s3_uri,
    fleet_console_snapshot_key,
    measure_fleet_load,
)
from quadringent_control_plane.model import SourceDescriptor
from quadringent_control_plane.projection import project_console_document


SITE = site_fixture.build_test_site()
NOW = datetime(2026, 9, 18, 8, 45, tzinfo=timezone.utc)
RECEIVER = "DEMOJRN4001"
SOURCE = SourceDescriptor(
    "fleet", "live", SITE.environment, "s3://bucket/fleet/console-proof.json"
)

PROOF_KEYS = {
    "schema_version",
    "observed_at",
    "source_checkpoint",
    "target",
    "activation",
    "load",
    "destination",
    "reconciliation",
}
LOAD_KEYS = {
    "state",
    "observed_at",
    "checkpoint",
    "batch_count",
    "event_count",
    "failed_event_count",
    "incident_code",
}
APPLY_KEYS = {
    "state",
    "observed_at",
    "apply_checkpoint",
    "failed_mutation_count",
    "incident_code",
}
RECONCILIATION_KEYS = {
    "state",
    "observed_at",
    "window",
    "captured_event_count",
    "loaded_event_count",
    "ledger_event_count",
    "distinct_event_count",
    "duplicate_event_count",
    "missing_event_count",
    "unexpected_event_count",
    "failed_mutation_count",
}


def fleet_document(**overrides) -> dict[str, object]:
    document: dict[str, object] = {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(minutes=1)).isoformat(),
        "flux": {"id": "ibmi/ledger/fleet", "label": "FLEET"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": RECEIVER, "sequence": 41},
            "source_tail": {"receiver": RECEIVER, "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {
            "events_published": {"value": 130},
            "windows_published": {"value": 13},
            "errors": {"value": 0},
        },
    }
    for key, value in overrides.items():
        document[key] = value
    return document


def file_key(table: str) -> str:
    """Clé S3 du lot de test pour une voie — même forme qu'en réel."""

    return f"as400/sales/{table.lower()}/journal/batch-00.jsonl"


def table_entry(table: str, **overrides) -> FleetTableMeasurement:
    values = {
        "pipe_state": "RUNNING",
        "pending_files": 0,
        "raw_rows": 10,
        "distinct_events": 10,
        "canonical_rows": 10,
        "load_max_by_receiver": ((RECEIVER, 41),),
        "apply_max_by_receiver": ((RECEIVER, 41),),
        "error": None,
        "snapshot_rows": 0,
        "snapshot_max_sequence": None,
        "journal_rows": 10,
    }
    values.update(overrides)
    if "file_rows" not in overrides:
        values["file_rows"] = (
            {file_key(table): values["raw_rows"]}
            if values["raw_rows"] is not None
            else None
        )
    return FleetTableMeasurement(
        table=table, pipe=SITE.snowflake_pipe_for(table), **values
    )


def complete_ledger(declared: int = 10, **per_table) -> FleetLoadLedger:
    """Registre complet de test : chaque voie déclare son lot à `declared`.

    ``per_table`` remplace le décompte déclaré d'une voie — la fixture
    reste cohérente avec ``table_entry`` : même clé, reçu complet, base
    non-reçue nulle.
    """

    ledger = FleetLoadLedger()
    for table in SITE.fleet_tables:
        entry = TableLedger(
            receipted={file_key(table): per_table.get(table, declared)},
            baselined=True,
            complete=True,
        )
        ledger.tables[table] = entry
    return ledger


def fleet_measurement(**per_table) -> FleetLoadMeasurement:
    return FleetLoadMeasurement(
        measured_at=NOW,
        environment=SITE.environment,
        destination_id=SITE.destination_id,
        database=SITE.destination_database,
        schema=SITE.destination_schema,
        tables=tuple(
            table_entry(table, **per_table.get(table, per_table.get("*", {})))
            for table in SITE.fleet_tables
        ),
    )


def project(document: dict[str, object], now: datetime = NOW):
    return project_console_document(document, SOURCE, now=now)


def assert_closed_proof(test_case: unittest.TestCase, proof: dict[str, object]) -> None:
    """La preuve émise reste dans les ensembles fermés du contrat v1."""

    test_case.assertEqual(set(proof), PROOF_KEYS)
    test_case.assertEqual(proof["schema_version"], "destination-proof-v1")
    test_case.assertLessEqual(set(proof["load"]), LOAD_KEYS)
    test_case.assertLessEqual(set(proof["destination"]), APPLY_KEYS)
    test_case.assertLessEqual(set(proof["reconciliation"]), RECONCILIATION_KEYS)
    test_case.assertEqual(
        set(proof["activation"]),
        {"state", "observed_at", "checks", "blocker_code"},
    )
    test_case.assertEqual(
        set(proof["target"]), {"kind", "destination_id", "environment"}
    )
    test_case.assertEqual(
        set(proof["activation"]["checks"]),
        {
            "configuration",
            "credential",
            "connectivity",
            "authorization",
            "contract",
        },
    )
    if "window" in proof["reconciliation"]:
        test_case.assertEqual(
            set(proof["reconciliation"]["window"]),
            {"from_exclusive", "to_inclusive"},
        )


class FakeCursor:
    """Curseur de test : réponses ordonnées par nature de requête."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.executed: list[str] = []
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str, params=None) -> None:
        self.executed.append(sql)
        result = self.responder(sql)
        if isinstance(result, Exception):
            raise result
        self._rows = list(result)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        pass


def healthy_responder(sql: str):
    if "SYSTEM$PIPE_STATUS" in sql:
        return [(json.dumps({"executionState": "RUNNING", "pendingFileCount": 0}),)]
    if "PAYLOAD:journal_receiver" in sql:
        return [(RECEIVER, 10, 10, 41)]
    if "JOURNAL_RECEIVER" in sql:
        return [(RECEIVER, 10, 41)]
    if "SOURCE_FILE, COUNT(*)" in sql:
        match = re.search(r"%/([a-z0-9]+)/journal/%", sql)
        if match is None:
            raise AssertionError(f"unexpected fleet probe query: {sql}")
        return [(file_key(match.group(1).upper()), 10)]
    raise AssertionError(f"unexpected fleet probe query: {sql}")


class MeasureFleetLoadTests(unittest.TestCase):
    def test_measure_composes_one_row_per_declared_table(self) -> None:
        cursor = FakeCursor(healthy_responder)

        report = measure_fleet_load(cursor, SITE)

        self.assertEqual(len(report.tables), len(SITE.fleet_tables))
        self.assertEqual(len(cursor.executed), 4 * len(SITE.fleet_tables))
        sale = report.tables[SITE.fleet_tables.index("SALE")]
        self.assertEqual(sale.table, "SALE")
        self.assertEqual(sale.pipe, SITE.snowflake_pipe_for("SALE"))
        self.assertEqual(sale.pipe_state, "RUNNING")
        self.assertEqual(sale.pending_files, 0)
        self.assertEqual(sale.raw_rows, 10)
        self.assertEqual(sale.distinct_events, 10)
        self.assertEqual(sale.canonical_rows, 10)
        self.assertEqual(sale.load_max_by_receiver, ((RECEIVER, 41),))
        self.assertEqual(sale.apply_max_by_receiver, ((RECEIVER, 41),))
        self.assertIsNone(sale.error)
        pipe_queries = [
            sql for sql in cursor.executed if "SYSTEM$PIPE_STATUS" in sql
        ]
        self.assertEqual(len(pipe_queries), len(SITE.fleet_tables))
        self.assertIn(
            "SYSTEM$PIPE_STATUS('ACME_RAW.IBMI_TEST.QUADRINGENT_SALE_PIPE')",
            pipe_queries[7],
        )
        raw_queries = [
            sql for sql in cursor.executed if "PAYLOAD:journal_receiver" in sql
        ]
        self.assertIn("%/sale/journal/%", raw_queries[7])
        self.assertIn('"ACME_RAW"."IBMI_TEST"."QUADRINGENT_SALE_RAW"', raw_queries[7])
        canonical_queries = [
            sql for sql in cursor.executed if "JOURNAL_RECEIVER" in sql
        ]
        self.assertIn(
            '"ACME_RAW"."IBMI_TEST"."QUADRINGENT_SALE_CANONICAL"',
            canonical_queries[7],
        )

    def test_measure_records_a_paused_pipe_without_raising(self) -> None:
        def responder(sql: str):
            if "SYSTEM$PIPE_STATUS" in sql and "CNTR" in sql:
                return [(
                    json.dumps(
                        {"executionState": "PAUSED", "pendingFileCount": 3}
                    ),
                )]
            return healthy_responder(sql)

        report = measure_fleet_load(FakeCursor(responder), SITE)
        pays = report.tables[SITE.fleet_tables.index("CNTR")]

        self.assertEqual(pays.pipe_state, "PAUSED")
        self.assertEqual(pays.pending_files, 3)
        self.assertIsNone(pays.error)
        self.assertEqual(pays.raw_rows, 10)

    def test_measure_records_unreadable_pipe_status_fail_closed(self) -> None:
        for payload in ("not-json", json.dumps({"unexpected": True})):
            with self.subTest(payload=payload):
                def responder(sql: str):
                    if "SYSTEM$PIPE_STATUS" in sql:
                        return [(payload,)]
                    return healthy_responder(sql)

                report = measure_fleet_load(FakeCursor(responder), SITE)

                for entry in report.tables:
                    self.assertIsNone(entry.pipe_state)
                    self.assertIsNotNone(entry.error)
                    self.assertEqual(entry.raw_rows, 10)

    def test_measure_records_query_failures_without_content(self) -> None:
        def responder(sql: str):
            if "CNTR" in sql and "PAYLOAD:journal_receiver" in sql:
                return RuntimeError("object RAW_CNTR does not exist or not authorized")
            if "CNTR" in sql and "SYSTEM$PIPE_STATUS" in sql:
                return [(
                    json.dumps(
                        {"executionState": "RUNNING", "pendingFileCount": 0}
                    ),
                )]
            return healthy_responder(sql)

        report = measure_fleet_load(FakeCursor(responder), SITE)
        pays = report.tables[SITE.fleet_tables.index("CNTR")]

        self.assertEqual(pays.error, "raw_query_missing_object")
        self.assertIsNone(pays.raw_rows)
        self.assertEqual(pays.pipe_state, "RUNNING")
        self.assertEqual(pays.canonical_rows, 10)
        self.assertNotIn("RAW_CNTR", pays.error)

    def test_measure_refuses_tables_outside_the_manifest(self) -> None:
        cursor = FakeCursor(healthy_responder)

        with self.assertRaises(ValueError):
            measure_fleet_load(cursor, SITE, tables=["NOPE"])
        with self.assertRaises(ValueError):
            measure_fleet_load(cursor, SITE, tables=[])
        self.assertEqual(cursor.executed, [])

    def test_measure_subset_stays_in_declared_order(self) -> None:
        cursor = FakeCursor(healthy_responder)

        report = measure_fleet_load(cursor, SITE, tables=["CNTR", "SALE"])

        self.assertEqual([entry.table for entry in report.tables], ["CNTR", "SALE"])
        self.assertEqual(len(cursor.executed), 8)

    def test_measure_requires_the_declared_site(self) -> None:
        with self.assertRaises(ValueError):
            measure_fleet_load(FakeCursor(healthy_responder), site=object())

    def test_measure_splits_snapshot_and_journal_receivers(self) -> None:
        def responder(sql: str):
            if "PAYLOAD:journal_receiver" in sql:
                return [
                    ("SNAPSHOT:run-1", 7, 7, 40),
                    (RECEIVER, 3, 3, 41),
                ]
            return healthy_responder(sql)

        report = measure_fleet_load(FakeCursor(responder), SITE)
        sale = report.tables[SITE.fleet_tables.index("SALE")]

        self.assertEqual(sale.raw_rows, 10)
        self.assertEqual(sale.snapshot_rows, 7)
        self.assertEqual(sale.journal_rows, 3)
        self.assertEqual(sale.snapshot_max_sequence, 40)

    def test_measure_without_snapshot_rows_reports_zero_not_none(self) -> None:
        report = measure_fleet_load(FakeCursor(healthy_responder), SITE)
        sale = report.tables[SITE.fleet_tables.index("SALE")]

        self.assertEqual(sale.snapshot_rows, 0)
        self.assertIsNone(sale.snapshot_max_sequence)
        self.assertEqual(sale.journal_rows, 10)


class AttachFleetDestinationProofTests(unittest.TestCase):
    def test_matched_measurement_reuses_the_closed_healthy_proof(self) -> None:
        combined = attach_fleet_destination_proof(
            fleet_document(), fleet_measurement(), site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )
        proof = combined["destination_proof"]

        assert_closed_proof(self, proof)
        self.assertEqual(proof["observed_at"], NOW.isoformat())
        self.assertEqual(
            proof["source_checkpoint"], {"receiver": RECEIVER, "sequence": 41}
        )
        self.assertEqual(
            proof["target"],
            {
                "kind": "snowflake",
                "destination_id": SITE.destination_id,
                "environment": SITE.environment,
            },
        )
        self.assertEqual(proof["activation"]["state"], "active")
        self.assertIsNone(proof["activation"]["blocker_code"])
        self.assertEqual(proof["load"]["state"], "succeeded")
        self.assertEqual(
            proof["load"]["checkpoint"], {"receiver": RECEIVER, "sequence": 41}
        )
        self.assertEqual(proof["load"]["event_count"], 130)
        self.assertEqual(proof["load"]["batch_count"], 13)
        self.assertEqual(proof["destination"]["state"], "applied")
        self.assertEqual(proof["reconciliation"]["state"], "matched")
        self.assertEqual(
            proof["reconciliation"]["window"],
            {
                "from_exclusive": None,
                "to_inclusive": {"receiver": RECEIVER, "sequence": 41},
            },
        )
        for name in (
            "captured_event_count",
            "loaded_event_count",
            "ledger_event_count",
            "distinct_event_count",
        ):
            self.assertEqual(proof["reconciliation"][name], 130)
        for name in (
            "duplicate_event_count",
            "missing_event_count",
            "unexpected_event_count",
            "failed_mutation_count",
        ):
            self.assertEqual(proof["reconciliation"][name], 0)

        projection = project(combined)
        self.assertEqual(projection.status, "healthy")
        self.assertEqual(
            [stage.status for stage in projection.stages],
            ["healthy"] * 5,
        )
        self.assertEqual(projection.quality["coverage"], "complete")

    def test_attach_tables_expose_the_snapshot_journal_split(self) -> None:
        measurement = fleet_measurement(
            SALE={
                "snapshot_rows": 7,
                "snapshot_max_sequence": 40,
                "journal_rows": 3,
            }
        )
        ledger = complete_ledger()
        ledger.tables["SALE"].snapshot_bounds["SNAPSHOT:run-1"] = 50

        combined = attach_fleet_destination_proof(
            fleet_document(),
            measurement,
            site=SITE,
            load_ledger=ledger,
            observed_at=NOW,
        )
        sale = combined["destination"]["tables"]["SALE"]
        pays = combined["destination"]["tables"]["CNTR"]

        self.assertEqual(sale["snapshot_rows"], 7)
        self.assertEqual(sale["journal_rows"], 3)
        # La borne déclarée du registre prime sur la mesure chargée.
        self.assertEqual(sale["snapshot_published"], 50)
        # Sans borne ni mesure, la voie n'a jamais vu d'image initiale.
        self.assertIsNone(pays["snapshot_published"])
        self.assertEqual(pays["snapshot_rows"], 0)

    def test_snapshot_published_falls_back_to_measured_bound(self) -> None:
        measurement = fleet_measurement(
            SALE={"snapshot_rows": 7, "snapshot_max_sequence": 40}
        )

        combined = attach_fleet_destination_proof(
            fleet_document(),
            measurement,
            site=SITE,
            load_ledger=complete_ledger(),
            observed_at=NOW,
        )

        self.assertEqual(
            combined["destination"]["tables"]["SALE"]["snapshot_published"], 40
        )

    def test_attach_never_mutates_and_keeps_the_real_capture_age(self) -> None:
        document = fleet_document()
        before = deepcopy(document)

        combined = attach_fleet_destination_proof(
            document, fleet_measurement(), site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )

        self.assertEqual(document, before)
        self.assertEqual(combined["generated_at"], NOW.isoformat())
        self.assertEqual(
            combined["capture_observed_at"], before["generated_at"]
        )
        self.assertEqual(combined["destination"]["kind"], "snowflake")
        self.assertEqual(combined["destination"]["table_count"], 13)
        self.assertEqual(combined["destination"]["raw_rows"], 130)

    def test_attach_defaults_observed_at_to_the_measurement_time(self) -> None:
        combined = attach_fleet_destination_proof(
            fleet_document(), fleet_measurement(), site=SITE, load_ledger=complete_ledger()
        )

        self.assertEqual(
            combined["destination_proof"]["observed_at"], NOW.isoformat()
        )

    def test_pending_or_partial_load_stays_degraded_never_healthy(self) -> None:
        measurement = fleet_measurement(
            CNTR={
                "raw_rows": 5,
                "distinct_events": 5,
                "canonical_rows": 5,
                "load_max_by_receiver": ((RECEIVER, 30),),
                "apply_max_by_receiver": ((RECEIVER, 30),),
            }
        )

        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )
        proof = combined["destination_proof"]

        assert_closed_proof(self, proof)
        self.assertEqual(proof["activation"]["state"], "active")
        self.assertEqual(proof["load"]["state"], "running")
        self.assertEqual(
            proof["load"]["checkpoint"], {"receiver": RECEIVER, "sequence": 30}
        )
        self.assertEqual(proof["load"]["event_count"], 125)
        self.assertEqual(proof["reconciliation"]["state"], "running")
        self.assertEqual(proof["reconciliation"]["missing_event_count"], 5)

        projection = project(combined)
        self.assertEqual(projection.status, "degraded")
        self.assertEqual(projection.stages[3].status, "degraded")
        self.assertEqual(projection.stages[4].status, "degraded")

    def test_paused_lane_is_a_planned_stop_not_a_failure(self) -> None:
        measurement = fleet_measurement(
            CNTR={
                "pipe_state": "PAUSED",
                "raw_rows": 5,
                "distinct_events": 5,
                "canonical_rows": 5,
                "load_max_by_receiver": ((RECEIVER, 30),),
                "apply_max_by_receiver": ((RECEIVER, 30),),
            }
        )

        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )
        proof = combined["destination_proof"]

        assert_closed_proof(self, proof)
        self.assertEqual(proof["activation"]["state"], "unknown")
        self.assertIsNone(proof["activation"]["blocker_code"])
        self.assertEqual(proof["load"]["state"], "planned_stop")
        self.assertEqual(
            proof["load"]["checkpoint"], {"receiver": RECEIVER, "sequence": 30}
        )

        projection = project(combined)
        self.assertEqual(projection.stages[3].status, "unknown")
        self.assertIsNone(projection.incident)

    def test_pending_files_prevent_matched_even_with_equal_counts(self) -> None:
        measurement = fleet_measurement(CNTR={"pending_files": 2})

        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )

        self.assertEqual(
            combined["destination_proof"]["reconciliation"]["state"], "running"
        )
        self.assertEqual(project(combined).stages[3].status, "degraded")

    def test_duplicate_raw_rows_are_a_contract_incident(self) -> None:
        measurement = fleet_measurement(
            CNTR={"raw_rows": 12, "distinct_events": 10}
        )

        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )
        proof = combined["destination_proof"]

        assert_closed_proof(self, proof)
        self.assertEqual(proof["load"]["state"], "failed")
        self.assertEqual(
            proof["load"]["incident_code"], "destination_load_contract_invalid"
        )
        self.assertEqual(proof["reconciliation"]["state"], "mismatch")
        self.assertEqual(proof["reconciliation"]["duplicate_event_count"], 2)

        projection = project(combined)
        self.assertEqual(projection.status, "incident")
        self.assertEqual(projection.incident["type"], "destination")

    def test_canonical_divergence_is_an_apply_incident(self) -> None:
        measurement = fleet_measurement(CNTR={"canonical_rows": 7})

        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )
        proof = combined["destination_proof"]

        assert_closed_proof(self, proof)
        self.assertEqual(proof["destination"]["state"], "failed")
        self.assertEqual(
            proof["destination"]["incident_code"],
            "destination_apply_contract_invalid",
        )
        self.assertEqual(proof["destination"]["failed_mutation_count"], 3)

        projection = project(combined)
        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident["code"], "destination_apply_contract_invalid"
        )

    def test_measurement_error_blocks_activation_with_allowlisted_codes(self) -> None:
        cases = (
            (
                "raw_query_missing_object",
                "destination_configuration_missing",
                "configuration",
                "missing",
            ),
            (
                "canonical_query_denied",
                "destination_authorization_denied",
                "authorization",
                "denied",
            ),
            (
                "pipe_status_invalid",
                "destination_contract_incompatible",
                "contract",
                "incompatible",
            ),
            (
                "pipe_status_failed",
                "destination_unreachable",
                "connectivity",
                "unreachable",
            ),
        )
        for error, blocker, check, value in cases:
            with self.subTest(error=error):
                measurement = fleet_measurement(
                    CNTR={
                        "error": error,
                        "pipe_state": None,
                        "pending_files": None,
                        "raw_rows": None,
                        "distinct_events": None,
                        "canonical_rows": None,
                        "load_max_by_receiver": (),
                        "apply_max_by_receiver": (),
                    }
                )

                combined = attach_fleet_destination_proof(
                    fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
                )
                proof = combined["destination_proof"]

                assert_closed_proof(self, proof)
                activation = proof["activation"]
                self.assertEqual(activation["state"], "blocked")
                self.assertEqual(activation["blocker_code"], blocker)
                self.assertEqual(activation["checks"][check], value)
                self.assertEqual(proof["load"]["state"], "unknown")
                self.assertEqual(proof["destination"]["state"], "unknown")
                self.assertEqual(proof["reconciliation"]["state"], "unknown")

                projection = project(combined)
                self.assertEqual(projection.status, "incident")
                self.assertEqual(projection.incident["code"], blocker)

    def test_incomplete_ledger_never_infers_healthy(self) -> None:
        """Registre absent ou en construction : la population déclarée est
        inconnue — la réconciliation déclare l'inconnue, jamais le vert."""

        for ledger in (None, FleetLoadLedger(), complete_ledger()):
            with self.subTest(ledger=ledger is None):
                if ledger is not None and "CNTR" in ledger.tables:
                    # Voie encore en amorçage : la ligne de base n'est pas
                    # gelée — la population déclarée reste inconnue.
                    ledger.tables["CNTR"].baselined = False
                combined = attach_fleet_destination_proof(
                    fleet_document(),
                    fleet_measurement(),
                    site=SITE,
                    load_ledger=ledger,
                    observed_at=NOW,
                )
                proof = combined["destination_proof"]

                assert_closed_proof(self, proof)
                self.assertEqual(
                    proof["reconciliation"]["state"], "unknown"
                )
                self.assertNotIn(
                    "captured_event_count", proof["reconciliation"]
                )

                projection = project(combined)
                self.assertNotEqual(projection.status, "healthy")

    def test_absent_ledger_leaves_the_run_counter_as_information(self) -> None:
        """Sans registre, le compteur du run reste exposé en information —
        jamais dans la population réconciliée."""

        document = fleet_document()
        document["counters"]["events_published"] = {
            "value": None,
            "unknown": "compte indisponible",
        }

        combined = attach_fleet_destination_proof(
            document, fleet_measurement(), site=SITE,
            load_ledger=complete_ledger(), observed_at=NOW,
        )

        self.assertIsNone(combined["destination"]["run_published_events"])
        self.assertEqual(
            combined["destination_proof"]["reconciliation"]["state"],
            "matched",
        )

    def test_receiver_without_coverage_floors_at_zero_never_above(self) -> None:
        measurement = fleet_measurement(
            CNTR={"load_max_by_receiver": (), "apply_max_by_receiver": (),
                  "raw_rows": 5, "distinct_events": 5, "canonical_rows": 5}
        )
        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )

        self.assertEqual(
            combined["destination_proof"]["load"]["checkpoint"],
            {"receiver": RECEIVER, "sequence": 0},
        )

    def test_coverage_never_runs_ahead_of_the_source_checkpoint(self) -> None:
        measurement = fleet_measurement(
            **{
                "*": {
                    "load_max_by_receiver": ((RECEIVER, 60),),
                    "apply_max_by_receiver": ((RECEIVER, 60),),
                    "raw_rows": 5,
                    "distinct_events": 5,
                    "canonical_rows": 5,
                }
            }
        )
        combined = attach_fleet_destination_proof(
            fleet_document(), measurement, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
        )

        self.assertEqual(
            combined["destination_proof"]["load"]["checkpoint"],
            {"receiver": RECEIVER, "sequence": 41},
        )
        projection = project(combined)
        self.assertIsNone(projection.incident)

    def test_attach_rejects_incompatible_inputs(self) -> None:
        measurement = fleet_measurement()
        for bad_document, bad_measurement, bad_site, bad_observed in (
            ({"format_version": "other"}, measurement, SITE, NOW),
            (fleet_document(), {"tables": []}, SITE, NOW),
            (fleet_document(), measurement, object(), NOW),
            (fleet_document(), measurement, SITE, "2026-09-18"),
            (fleet_document(), measurement, SITE, datetime(2026, 9, 18)),
        ):
            with self.subTest(bad=type(bad_document).__name__):
                with self.assertRaises(ValueError):
                    attach_fleet_destination_proof(
                        bad_document, bad_measurement,
                        site=bad_site, load_ledger=None, observed_at=bad_observed,
                    )

    def test_attach_rejects_a_measurement_outside_the_declared_scope(self) -> None:
        foreign = FleetLoadMeasurement(
            measured_at=NOW,
            environment="dev",
            destination_id="other",
            database="OTHER",
            schema="OTHER",
            tables=fleet_measurement().tables,
        )
        with self.assertRaises(ValueError):
            attach_fleet_destination_proof(
                fleet_document(), foreign, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
            )

        partial = FleetLoadMeasurement(
            measured_at=NOW,
            environment=SITE.environment,
            destination_id=SITE.destination_id,
            database=SITE.destination_database,
            schema=SITE.destination_schema,
            tables=fleet_measurement().tables[:3],
        )
        with self.assertRaises(ValueError):
            attach_fleet_destination_proof(
                fleet_document(), partial, site=SITE, load_ledger=complete_ledger(), observed_at=NOW
            )

    def test_attach_rejects_a_missing_or_invalid_checkpoint(self) -> None:
        for checkpoint in (None, {"receiver": ""}, {"receiver": RECEIVER}, "X"):
            with self.subTest(checkpoint=checkpoint):
                document = fleet_document()
                document["position"]["checkpoint"] = checkpoint
                with self.assertRaises(ValueError):
                    attach_fleet_destination_proof(
                        document, fleet_measurement(), site=SITE, load_ledger=complete_ledger(), observed_at=NOW
                    )


class FleetObserveCliTests(unittest.TestCase):
    def _storage(self, document, *, existing_etag='"old"'):
        snapshot_key = fleet_console_snapshot_key(SITE)
        proof_key = fleet_console_proof_key(SITE)
        writes = []
        payload = json.dumps(document).encode()
        manifest_payload = json.dumps({"event_count": 10}).encode()
        ledger_key = fleet_load_ledger_key(SITE)

        class Storage:
            def get_object(self, **kwargs):
                if kwargs["Key"] == snapshot_key:
                    return {
                        "Body": BytesIO(payload),
                        "ContentLength": len(payload),
                    }
                if kwargs["Key"] == proof_key and existing_etag is not None:
                    return {
                        "Body": BytesIO(b"{}"),
                        "ContentLength": 2,
                    }
                if kwargs["Key"].endswith(".manifest.json"):
                    return {
                        "Body": BytesIO(manifest_payload),
                        "ContentLength": len(manifest_payload),
                    }
                if kwargs["Key"] == ledger_key:
                    error = RuntimeError("absent")
                    error.response = {"Error": {"Code": "NoSuchKey"}}
                    raise error
                raise AssertionError(kwargs)

            def head_object(self, **kwargs):
                if kwargs["Key"] == proof_key and existing_etag is not None:
                    return {"ETag": existing_etag}
                error = RuntimeError("absent")
                error.response = {"Error": {"Code": "NoSuchKey"}}
                raise error

            def list_objects_v2(self, **kwargs):
                prefix = kwargs.get("Prefix", "")
                if prefix.endswith("/journal/"):
                    table = prefix.strip("/").split("/")[-2]
                    return {
                        "Contents": [{"Key": file_key(table.upper())}],
                        "IsTruncated": False,
                    }
                raise AssertionError(kwargs)

            def put_object(self, **kwargs):
                writes.append(kwargs)
                return {"ETag": '"new"'}

        return Storage(), writes

    def _run_cli(self, argv, storage):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import patch
        import sys

        import quadringent_fleet_observe as cli

        cursor = FakeCursor(healthy_responder)
        connection = SimpleNamespace(
            cursor=lambda: cursor, close=lambda: None
        )
        modules = {
            "boto3": SimpleNamespace(),
            "snowflake": SimpleNamespace(connector=SimpleNamespace()),
            "snowflake.connector": SimpleNamespace(),
        }
        stdout, stderr = StringIO(), StringIO()
        with patch.dict(sys.modules, modules), patch.object(
            cli, "_publication_client", return_value=storage
        ), patch.object(
            cli, "_connect_snowflake", return_value=connection
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), cursor

    def test_dry_run_measures_and_never_writes(self) -> None:
        storage, writes = self._storage(fleet_document())

        code, out, err, cursor = self._run_cli([], storage)

        self.assertEqual(code, 0, err)
        status = json.loads(out)
        self.assertEqual(status["status"], "dry_run")
        self.assertFalse(status["published"])
        self.assertEqual(status["tables"], 13)
        self.assertEqual(status["reconciliation"], "matched")
        self.assertEqual(writes, [])

    def test_execute_publishes_conditionally_on_the_dedicated_key(self) -> None:
        for existing in ('"old"', None):
            with self.subTest(existing=existing):
                storage, writes = self._storage(
                    fleet_document(), existing_etag=existing
                )

                code, out, err, cursor = self._run_cli(["--execute"], storage)

                self.assertEqual(code, 0, err)
                self.assertEqual(len(writes), 2)
                ledger_write = next(
                    item for item in writes
                    if item["Key"] == fleet_load_ledger_key(SITE)
                )
                self.assertEqual(ledger_write["IfNoneMatch"], "*")
                write = next(
                    item for item in writes
                    if item["Key"] == fleet_console_proof_key(SITE)
                )
                self.assertEqual(write["Bucket"], SITE.raw_bucket)
                self.assertEqual(write["CacheControl"], "no-store")
                if existing is None:
                    self.assertEqual(write["IfNoneMatch"], "*")
                else:
                    self.assertEqual(write["IfMatch"], existing)
                published = json.loads(write["Body"])
                self.assertEqual(
                    published["destination_proof"]["schema_version"],
                    "destination-proof-v1",
                )
                status = json.loads(out)
                self.assertEqual(status["status"], "observed")
                self.assertEqual(status["publication_status"], "confirmed")

    def test_error_output_is_bounded_and_never_leaks(self) -> None:
        class FailingStorage:
            def get_object(self, **kwargs):
                raise RuntimeError("sensitive-detail=never")

        code, out, err, cursor = self._run_cli([], FailingStorage())

        self.assertEqual(code, 3)
        self.assertNotIn("sensitive-detail", err + out)
        status = json.loads(err)
        self.assertEqual(status["publication_status"], "not_attempted")


if __name__ == "__main__":
    unittest.main()
