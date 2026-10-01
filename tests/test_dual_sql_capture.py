from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from quadringent_research.dual_sql_capture import (
    ac2_claim,
    capture_script,
    gate_job_legs,
    load_registry,
    parse_leg_stdout,
    run_dual,
)
from as400_ac2_gate import extract_leg_ids


REGISTRY = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "proven-windows.json"


class DualSqlCaptureTests(unittest.TestCase):
    def test_registry_is_addrs1_sql_then_custom1_sql(self) -> None:
        registry = load_registry(REGISTRY)
        ids = [item["id"] for item in registry["windows"]]
        self.assertEqual(ids, ["addrs1_sql_3769", "custom1_sql_3769"])
        self.assertEqual([item["mode"] for item in registry["windows"]], ["sql", "sql"])
        self.assertEqual(
            {item["receiver"] for item in registry["windows"]},
            {"DEMOJRN3769"},
        )
        self.assertEqual(
            registry["required_pair"],
            ["addrs1_sql_3769", "custom1_sql_3769"],
        )
        self.assertIn("calend_sql_3763", registry["exclude"])
        self.assertIn("calend_rj_3763", registry["exclude"])
        addrs1 = next(item for item in registry["windows"] if item["id"] == "addrs1_sql_3769")
        custom1 = next(item for item in registry["windows"] if item["id"] == "custom1_sql_3769")
        self.assertEqual(int(addrs1["published_events"]), 22)
        self.assertEqual(int(custom1["published_events"]), 40)
        self.assertEqual(int(addrs1["start_sequence"]), int(custom1["start_sequence"]))

    def test_parse_leg_stdout_reads_retrieve_summary_and_published(self) -> None:
        stdout = "\n".join(
            [
                '{"event": "retrieve_start", "object_name": "ADDRS1"}',
                '{"decoded": 22, "elapsed_ms": 1861, "event": "retrieve_summary", "events_per_sec": 11.822}',
                '{"event": "capture_poll", "event_count": 22, "status": "published"}',
            ]
        )
        parsed = parse_leg_stdout(stdout)
        self.assertEqual(parsed["events_published"], 22)
        self.assertEqual(parsed["retrieve_summary"]["decoded"], 22)
        self.assertEqual(parsed["retrieve_summary"]["elapsed_ms"], 1861)
        self.assertEqual(parsed["retrieve_summary"]["events_per_sec"], 11.822)

    def test_parse_custom1_leg_stdout_from_cdc3_shape(self) -> None:
        stdout = "\n".join(
            [
                '{"event": "retrieve_summary", "decoded": 40, "elapsed_ms": 2100, "events_per_sec": 19.048}',
                '{"event": "capture_poll", "event_count": 40, "status": "published"}',
            ]
        )
        parsed = parse_leg_stdout(stdout)
        self.assertEqual(parsed["events_published"], 40)
        self.assertEqual(parsed["retrieve_summary"]["decoded"], 40)
        self.assertEqual(parsed["retrieve_summary"]["elapsed_ms"], 2100)

    def test_run_dual_continues_after_first_leg_error(self) -> None:
        calls: list[str] = []

        def run_leg(leg: dict) -> dict:
            calls.append(leg["id"])
            if leg["id"] == "first":
                raise RuntimeError("catalog timeout")
            return {
                "id": leg["id"],
                "table": leg["table"],
                "events_published": 40,
                "retrieve_summary": {"decoded": 40, "elapsed_ms": 10, "events_per_sec": 4000.0},
            }

        result = run_dual(
            [{"id": "first", "table": "ADDRS1"}, {"id": "second", "table": "CUSTOM1"}],
            run_leg_fn=run_leg,
            settle_s=0,
        )
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result["legs"][0]["events_published"], 0)
        self.assertEqual(result["legs"][1]["events_published"], 40)
        self.assertFalse(result["ac2_claim"])

    def test_ac2_claim_requires_two_published_summaries(self) -> None:
        one = [
            {
                "table": "ADDRS1",
                "events_published": 22,
                "retrieve_summary": {"decoded": 22, "elapsed_ms": 1861},
            }
        ]
        two = one + [
            {
                "table": "CUSTOM1",
                "events_published": 40,
                "retrieve_summary": {"decoded": 40, "elapsed_ms": 2100},
            }
        ]
        self.assertFalse(ac2_claim(one))
        self.assertTrue(ac2_claim(two))
        self.assertFalse(
            ac2_claim(
                two[:1]
                + [
                    {
                        "table": "CUSTOM1",
                        "events_published": 40,
                        "retrieve_summary": None,
                    }
                ]
            )
        )

    def test_gate_accepts_sql_then_sql_and_rejects_cal001_pairs(self) -> None:
        self.assertEqual(
            gate_job_legs(["addrs1_sql_3769", "custom1_sql_3769"], REGISTRY),
            ["addrs1_sql_3769", "custom1_sql_3769"],
        )
        with self.assertRaisesRegex(ValueError, "ac2 pair"):
            gate_job_legs(["custom1_sql_3769", "addrs1_sql_3769"], REGISTRY)
        with self.assertRaisesRegex(ValueError, "ac2 pair"):
            gate_job_legs(["addrs1_sql_3769", "calend_sql_3763"], REGISTRY)
        with self.assertRaisesRegex(ValueError, "ac2 pair"):
            gate_job_legs(["addrs1_sql_3769", "calend_rj_3763"], REGISTRY)
        with self.assertRaisesRegex(ValueError, "ac2 pair"):
            gate_job_legs(["addrs1_sql_3769", "sale_rj_20k"], REGISTRY)
        with self.assertRaisesRegex(ValueError, "ac2 pair"):
            gate_job_legs(["addrs1_sql_3769", "invented_window"], REGISTRY)

    def test_gate_requires_exactly_two_distinct_sql_legs(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly two"):
            gate_job_legs(["addrs1_sql_3769"], REGISTRY)
        with self.assertRaisesRegex(ValueError, "exactly two"):
            gate_job_legs(
                ["addrs1_sql_3769", "custom1_sql_3769", "addrs1_sql_3769"],
                REGISTRY,
            )

    def test_extrait_les_deux_lectures_d_un_manifeste_synthetique(self) -> None:
        text = 'AS400_DUAL_LEG_IDS\n  value: "addrs1_sql_3769,custom1_sql_3769"'
        self.assertEqual(extract_leg_ids(text), ["addrs1_sql_3769", "custom1_sql_3769"])
        with self.assertRaises(ValueError):
            extract_leg_ids("aucune déclaration")

    def test_gate_rejects_stale_receiver_with_tiny_catalog(self) -> None:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for item in registry["windows"]:
            if item["id"] == "custom1_sql_3769":
                item["start_sequence"] = 181723578
                item["receiver_metadata_limit"] = 8
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "windows.json"
            path.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metadata_limit"):
                gate_job_legs(["addrs1_sql_3769", "custom1_sql_3769"], path)

    def test_parse_leg_stdout_detects_reader_timeout(self) -> None:
        parsed = parse_leg_stdout("RuntimeError: bounded IBM i reader timed out\n")
        self.assertEqual(parsed["last_error"], "ReaderTimeout")
        self.assertEqual(parsed["events_published"], 0)

    def test_capture_script_selects_rj_vs_sql(self) -> None:
        self.assertEqual(
            capture_script({"mode": "rj"}, sql_script="/sql.py", rj_script="/rj.py").as_posix(),
            "/rj.py",
        )
        self.assertEqual(
            capture_script({"mode": "sql"}, sql_script="/sql.py", rj_script="/rj.py").as_posix(),
            "/sql.py",
        )
