from __future__ import annotations

import site_fixture

SITE = site_fixture.build_test_site()

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from quadringent_control_plane.fleet import (
    DESTINATION_NAMESPACE,
    ENVIRONMENT,
    MANIFEST,
    FleetError,
)
from quadringent_control_plane.fleet_plan import build_fleet_plan, parse_fleet_catalog
from quadringent_control_plane.fleet_sidecar import (
    OBSERVABILITY_UNOBSERVED_REASON,
    PIPELINE_ID,
    SIDECAR_FORMAT_VERSION,
    build_fleet_ui_sidecar,
    generate_fleet_ui_sidecar,
    generate_fleet_ui_sidecar_file,
    parse_fleet_ui_sidecar,
    write_fleet_ui_sidecar,
)
from quadringent_control_plane.projection import PUBLIC_COUNTERS


try:
    from tests.test_fleet_plan import (
        KEYED_TABLES,
        RRN_TABLES,
        OBSERVED_CATALOG_PATH,
        OBSERVED_DATA_SIZE,
        OBSERVED_ROW_COUNT,
        catalog_payload,
    )
except ImportError:
    from test_fleet_plan import (
        KEYED_TABLES,
        RRN_TABLES,
        OBSERVED_CATALOG_PATH,
        OBSERVED_DATA_SIZE,
        OBSERVED_ROW_COUNT,
        catalog_payload,
    )


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "quadringent_fleet_sidecar.py"
GENERATED_AT = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
SENSITIVE_TOKENS = ("host", "user", "password", "secret", "token", "credit", "credential")


def build_sidecar(payload: dict[str, object] | None = None, **kwargs) -> dict[str, object]:
    arguments = {"generated_at": GENERATED_AT, **kwargs}
    return generate_fleet_ui_sidecar(payload if payload is not None else catalog_payload(), **arguments)


def dumped(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True)


def assert_json_safe(test: unittest.TestCase, value: object) -> None:
    if value is None or type(value) in (str, int, float, bool):
        return
    if type(value) is list:
        for item in value:
            assert_json_safe(test, item)
        return
    if type(value) is dict:
        for key, item in value.items():
            test.assertIs(type(key), str)
            lowered = key.lower()
            test.assertFalse(any(token in lowered for token in SENSITIVE_TOKENS))
            assert_json_safe(test, item)
        return
    test.fail(f"type JSON non autorisé: {type(value)!r}")


def assert_safe_error(test: unittest.TestCase, error: FleetError) -> None:
    message = error.safe_message.lower()
    test.assertNotIn("host", message)
    test.assertNotIn("user", message)
    test.assertNotIn("password", message)
    test.assertNotIn("@", message)
    test.assertNotIn("/tmp/", message)


class SidecarContractTests(unittest.TestCase):
    def test_envelope_is_versioned_and_not_the_legacy_fleet_wire(self) -> None:
        sidecar = build_sidecar()
        self.assertEqual(sidecar["format_version"], SIDECAR_FORMAT_VERSION)
        self.assertNotEqual(sidecar["format_version"], "quadringent-fleet-v1")
        self.assertEqual(set(sidecar), {"format_version", "generated_at", "overview", "fleet"})
        self.assertNotIn("pipelines", sidecar)
        self.assertNotIn("credit_budget", sidecar)
        parse_fleet_ui_sidecar(sidecar)
        assert_json_safe(self, sidecar)

    def test_overview_is_nested_and_compatible_with_current_contract(self) -> None:
        sidecar = build_sidecar()
        overview = sidecar["overview"]
        self.assertEqual(overview["revision"], 0)
        self.assertEqual(overview["generated_at"], sidecar["generated_at"])
        self.assertEqual(overview["scope"], {"kind": "single", "environments": [SITE.environment]})
        self.assertEqual(len(overview["pipelines"]), 1)
        self.assertEqual(len(overview["sources"]), 1)
        pipeline = overview["pipelines"][0]
        source = overview["sources"][0]
        self.assertEqual(pipeline["id"], PIPELINE_ID)
        self.assertEqual(pipeline["environment"], SITE.environment)
        self.assertEqual(pipeline["status"], "unknown")
        self.assertNotEqual(pipeline["status"], "healthy")
        self.assertEqual(pipeline["quality"]["coverage"], "partial")
        self.assertEqual(pipeline["quality"]["freshness"], "fresh")
        self.assertEqual(pipeline["quality"]["evidence_kind"], "historical")
        self.assertEqual([stage["id"] for stage in pipeline["stages"]], ["source", "capture", "raw", "load", "destination"])
        self.assertEqual(pipeline["stages"][0]["status"], "healthy")
        self.assertEqual(pipeline["stages"][1]["status"], "unknown")
        self.assertEqual(pipeline["stages"][2]["status"], "unknown")
        self.assertEqual(pipeline["stages"][3]["status"], "unknown")
        self.assertEqual(pipeline["stages"][4]["status"], "unknown")
        self.assertIsNone(pipeline["lag_sequences"])
        self.assertIsNone(pipeline["lag_seconds"])
        self.assertIsNone(pipeline["lag_series"])
        self.assertIsNone(pipeline["incident"])
        self.assertEqual(set(pipeline["counters"]), set(PUBLIC_COUNTERS))
        self.assertTrue(all(value is None for value in pipeline["counters"].values()))
        self.assertEqual(source["id"], PIPELINE_ID)
        self.assertEqual(source["environment"], SITE.environment)
        self.assertEqual(source["evidence_kind"], "historical")
        self.assertEqual(source["status"], "available")
        self.assertIsNone(source["error"])
        self.assertEqual(
            pipeline["observability"],
            {
                "status": "unobserved",
                "quality": {
                    "coverage": "partial",
                    "freshness": "fresh",
                    "evidence_kind": "historical",
                },
                "observed_at": None,
                "reason": OBSERVABILITY_UNOBSERVED_REASON,
                "checks": [],
                "alerts": [],
            },
        )
        self.assertNotIn("window_delivery", pipeline)
        self.assertNotIn("fleet", pipeline)

    def test_unobserved_runtime_values_are_null_never_invented_zero(self) -> None:
        sidecar = build_sidecar()
        encoded = dumped(sidecar)
        self.assertNotRegex(encoded, r'"lag_sequences": 0')
        self.assertNotRegex(encoded, r'"copied_rows": 0')
        self.assertNotRegex(encoded, r'"copied_bytes": 0')
        self.assertNotRegex(encoded, r'"observed": 0')
        fleet = sidecar["fleet"]
        self.assertEqual(fleet["cost"]["status"], "unknown")
        self.assertIsNone(fleet["cost"]["observed"])
        self.assertEqual(fleet["cost"]["unknown_because"], "coût réel non observé")
        for table in fleet["tables"]:
            self.assertIsNone(table["copied_rows"])
            self.assertIsNone(table["copied_bytes"])

    def test_uncertain_continuity_blocks_live_not_history_with_rrn_identity(self) -> None:
        sidecar = build_sidecar()
        fleet = sidecar["fleet"]
        self.assertEqual(fleet["environment"], ENVIRONMENT)
        self.assertEqual(fleet["destination_namespace"], DESTINATION_NAMESPACE)
        self.assertEqual(fleet["continuity"], "uncertain")
        self.assertTrue(fleet["live_blocked"])
        self.assertTrue(fleet["certification_blocked"])
        self.assertEqual(fleet["live_promise"], "blocked")
        self.assertEqual(fleet["certification_promise"], "blocked")
        self.assertEqual(fleet["promise_blockers"], ["uncertain_continuity"])
        self.assertTrue(fleet["history_admitted"])
        self.assertIs(fleet["cutover_required_before_history"], True)
        self.assertEqual(fleet["identity"]["keyed"], list(KEYED_TABLES))
        self.assertEqual(fleet["identity"]["rrn"], list(RRN_TABLES))
        self.assertEqual(fleet["identity"]["blocked"], [])
        self.assertEqual(fleet["identity"]["keyed_count"], 1)
        self.assertEqual(fleet["identity"]["rrn_count"], 12)
        self.assertEqual(fleet["identity"]["blocked_count"], 0)
        self.assertEqual(tuple(table["name"] for table in fleet["tables"]), MANIFEST)
        self.assertEqual(fleet["journal"]["reader_kind"], "multi_object")
        self.assertEqual(fleet["journal"]["reader_count"], 1)
        self.assertEqual(fleet["journal"]["table_names"], list(MANIFEST))
        for table in fleet["tables"]:
            self.assertFalse(table["live_possible"])
            self.assertFalse(table["certification_possible"])
            self.assertTrue(table["historical_admitted"])
            self.assertIsNotNone(table["historical_lane"])
            if table["name"] in KEYED_TABLES:
                self.assertEqual(table["identity_status"], "keyed")
            else:
                self.assertEqual(table["identity_status"], "rrn")
                self.assertEqual(table["candidate_key"], ["_rrn"])
            self.assertIn("uncertain_continuity", table["blocked_reasons"])

    def test_byte_budget_can_exclude_history_without_inventing_live(self) -> None:
        sidecar = build_sidecar(max_concurrency=2, historical_byte_budget=0)
        fleet = sidecar["fleet"]
        self.assertFalse(fleet["history_admitted"])
        self.assertEqual(fleet["historical"]["byte_budget"], 0)
        self.assertEqual(fleet["historical"]["admitted_count"], 0)
        self.assertEqual(fleet["historical"]["excluded_count"], 13)
        self.assertEqual(fleet["historical"]["lanes"], [])
        self.assertEqual(fleet["live_promise"], "blocked")
        self.assertEqual(fleet["certification_promise"], "blocked")
        self.assertIsNone(fleet["cost"]["observed"])
        self.assertIn("non admise", sidecar["overview"]["pipelines"][0]["summary"])
        for table in fleet["tables"]:
            self.assertFalse(table["historical_admitted"])
            self.assertIsNone(table["historical_lane"])
            self.assertIsNone(table["copied_rows"])

    def test_generation_is_deterministic_for_a_fixed_clock(self) -> None:
        first = build_sidecar(max_concurrency=3)
        second = build_sidecar(max_concurrency=3)
        self.assertEqual(dumped(first), dumped(second))
        later = build_sidecar(generated_at=GENERATED_AT + timedelta(minutes=10))
        self.assertEqual(later["overview"]["pipelines"][0]["quality"]["freshness"], "stale")
        self.assertEqual(later["fleet"]["freshness"], "stale")

    def test_unknown_fields_are_rejected_fail_closed(self) -> None:
        sidecar = build_sidecar()
        mutated = copy.deepcopy(sidecar)
        mutated["extra"] = 1
        with self.assertRaises(FleetError) as raised:
            parse_fleet_ui_sidecar(mutated)
        self.assertEqual(raised.exception.code, "invalid_sidecar")
        assert_safe_error(self, raised.exception)
        live = copy.deepcopy(sidecar)
        live["overview"]["pipelines"][0]["status"] = "healthy"
        with self.assertRaises(FleetError) as healthy:
            parse_fleet_ui_sidecar(live)
        self.assertEqual(healthy.exception.code, "invalid_sidecar")
        cost = copy.deepcopy(sidecar)
        cost["fleet"]["cost"]["observed"] = 0
        with self.assertRaises(FleetError):
            parse_fleet_ui_sidecar(cost)

    def test_observability_is_unobserved_and_rejects_invented_pass_or_checks(self) -> None:
        sidecar = build_sidecar()
        observability = sidecar["overview"]["pipelines"][0]["observability"]
        self.assertEqual(observability["status"], "unobserved")
        self.assertEqual(observability["quality"]["coverage"], "partial")
        self.assertEqual(observability["quality"]["evidence_kind"], "historical")
        self.assertNotEqual(observability["quality"]["evidence_kind"], "live")
        self.assertIsNone(observability["observed_at"])
        self.assertEqual(observability["reason"], OBSERVABILITY_UNOBSERVED_REASON)
        self.assertEqual(observability["checks"], [])
        self.assertEqual(observability["alerts"], [])
        parse_fleet_ui_sidecar(sidecar)

        for forged_status in ("pass", "breach"):
            mutated = copy.deepcopy(sidecar)
            mutated["overview"]["pipelines"][0]["observability"]["status"] = forged_status
            with self.assertRaises(FleetError) as raised:
                parse_fleet_ui_sidecar(mutated)
            self.assertEqual(raised.exception.code, "invalid_sidecar")
            assert_safe_error(self, raised.exception)

        invented_check = copy.deepcopy(sidecar)
        invented_check["overview"]["pipelines"][0]["observability"]["checks"] = [
            {
                "id": "lag",
                "stage": "capture",
                "status": "pass",
                "observed": 0,
                "threshold": 0,
                "unit": "s",
                "reason": "invented",
            }
        ]
        with self.assertRaises(FleetError) as checks:
            parse_fleet_ui_sidecar(invented_check)
        self.assertEqual(checks.exception.code, "invalid_sidecar")
        assert_safe_error(self, checks.exception)

        invented_alert = copy.deepcopy(sidecar)
        invented_alert["overview"]["pipelines"][0]["observability"]["alerts"] = [
            {"fingerprint": "x", "check_id": "lag", "signal_status": "breach"}
        ]
        with self.assertRaises(FleetError) as alerts:
            parse_fleet_ui_sidecar(invented_alert)
        self.assertEqual(alerts.exception.code, "invalid_sidecar")
        assert_safe_error(self, alerts.exception)


class AtomicWriteAndCliTests(unittest.TestCase):
    def test_atomic_write_in_tempdir_replaces_and_sets_mode(self) -> None:
        sidecar = build_sidecar()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sidecar.json"
            target.write_text("stale", encoding="utf-8")
            write_fleet_ui_sidecar(target, sidecar)
            leftovers = list(Path(directory).glob(".*.tmp"))
            self.assertEqual(leftovers, [])
            loaded = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(loaded["format_version"], SIDECAR_FORMAT_VERSION)
            self.assertEqual(loaded, json.loads(dumped(sidecar)))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertNotIn(str(target), dumped(loaded))

    def test_cli_reads_catalog_and_writes_output_in_tempdir(self) -> None:
        payload = catalog_payload()
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.json"
            output = Path(directory) / "out" / "sidecar.json"
            catalog.write_text(json.dumps(payload), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--catalog",
                    str(catalog),
                    "--output",
                    str(output),
                    "--concurrency",
                    "2",
                    "--historical-byte-budget",
                    "100000",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            loaded = json.loads(output.read_text(encoding="utf-8"))
            parsed = parse_fleet_ui_sidecar(loaded)
            self.assertEqual(parsed["fleet"]["historical"]["max_concurrency"], 2)
            self.assertEqual(parsed["fleet"]["historical"]["byte_budget"], 100000)
            self.assertTrue(parsed["fleet"]["history_admitted"])
            self.assertEqual(parsed["fleet"]["live_promise"], "blocked")
            encoded = dumped(parsed)
            self.assertNotIn(str(catalog), encoded)
            self.assertNotIn(str(output), encoded)
            assert_json_safe(self, parsed)

    def test_cli_fail_closed_without_leaking_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-catalog.json"
            output = Path(directory) / "sidecar.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--catalog",
                    str(missing),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env={"PYTHONPATH": str(ROOT / "src"), "PATH": os.environ.get("PATH", "")},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("Catalogue illisible", completed.stderr)
            self.assertNotIn(str(missing), completed.stderr)
            self.assertFalse(output.exists())

    def test_api_file_helper_roundtrip_from_tempdir(self) -> None:
        payload = catalog_payload(attached_name="DEMOJRN4242", attached_tail=77)
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "catalog.json"
            output = Path(directory) / "sidecar.json"
            catalog.write_text(json.dumps(payload), encoding="utf-8")
            sidecar = generate_fleet_ui_sidecar_file(
                catalog,
                output,
                max_concurrency=4,
                generated_at=GENERATED_AT,
            )
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(loaded, json.loads(dumped(sidecar)))
            self.assertEqual(loaded["fleet"]["cutover_checkpoint"], {"receiver": "DEMOJRN4242", "sequence": 77})
            catalog_model = parse_fleet_catalog(payload)
            plan = build_fleet_plan(catalog_model, max_concurrency=4)
            self.assertEqual(plan.cutover_checkpoint.receiver, "DEMOJRN4242")
            rebuilt = build_fleet_ui_sidecar(catalog_model, generated_at=GENERATED_AT)
            self.assertEqual(dumped(rebuilt), dumped(sidecar))


@unittest.skipUnless(
    OBSERVED_CATALOG_PATH.is_file(),
    "catalogue observé /tmp/quadringent-fleet-catalog-20260913.json absent",
)
class ObservedCatalogSidecarProofTests(unittest.TestCase):
    def test_observed_catalog_sidecar_keeps_derived_proofs_and_unknown_cost(self) -> None:
        payload = json.loads(OBSERVED_CATALOG_PATH.read_text(encoding="utf-8"))
        sidecar = generate_fleet_ui_sidecar(payload, generated_at=GENERATED_AT)
        parse_fleet_ui_sidecar(sidecar)
        fleet = sidecar["fleet"]
        self.assertEqual(fleet["observed_totals"]["row_count"], OBSERVED_ROW_COUNT)
        self.assertEqual(fleet["observed_totals"]["data_size"], OBSERVED_DATA_SIZE)
        self.assertEqual(fleet["observed_totals"]["table_count"], 13)
        self.assertEqual(fleet["continuity"], "uncertain")
        self.assertEqual(fleet["live_promise"], "blocked")
        self.assertEqual(fleet["certification_promise"], "blocked")
        self.assertTrue(fleet["history_admitted"])
        self.assertEqual(fleet["identity"]["keyed_count"], 1)
        self.assertEqual(fleet["identity"]["rrn_count"], 12)
        self.assertEqual(fleet["identity"]["blocked_count"], 0)
        self.assertEqual(fleet["cutover_checkpoint"]["receiver"], "DEMOJRN4043")
        self.assertEqual(fleet["cutover_checkpoint"]["sequence"], 787461)
        self.assertIsNone(fleet["cost"]["observed"])
        self.assertEqual(sidecar["overview"]["pipelines"][0]["environment"], SITE.environment)
        self.assertNotEqual(sidecar["overview"]["pipelines"][0]["status"], "healthy")
        encoded = dumped(sidecar)
        self.assertNotIn("credit", encoded)
        self.assertNotIn(str(OBSERVED_CATALOG_PATH), encoded)
        assert_json_safe(self, sidecar)
