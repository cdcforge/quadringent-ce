from __future__ import annotations

import site_fixture

import json
from pathlib import Path
import tempfile
import unittest

SITE = site_fixture.build_test_site()

from quadringent_research.solo_e2e_report import (
    REPORT_STEM,
    build_report,
    emit_report,
    gate_report_pair,
)


def _write_scratch(root: Path, **overrides: dict) -> Path:
    defaults = {
        "capture-metrics.json": {
            "ac2": "MET",
            "pause_t0": "2026-08-26T19:00:20Z",
            "landed_tables": ["ADDRS1", "CUSTOM1"],
            "mode": "one_retrieve_multi_object_multi_volume",
            "crush": {
                "best_events_per_sec": 12.5,
                "best_cpu_ms_per_event": 4.2,
                "bar_events_per_sec": 515,
                "bar_cpu_ms_per_event": 1.96,
                "usd": "INCOMPLETE",
                "verdict_events": "non",
                "verdict_cpu": "non",
            },
            "volumes": [
                {"batch": 500, "job": "s"},
                {"batch": 5000, "job": "m"},
                {"batch": 10000, "job": "l"},
            ],
        },
        "snowflake-e2e.json": {
            "count": 2,
            "ac2_same_window_table_count": 2,
            "pause_t0": "2026-08-26T19:00:20Z",
            "status": "PASS",
            "tables": ["ADDRS1", "CUSTOM1"],
            "passed_loads": 6,
            "pause_window": {
                "volumes": [
                    {"batch_entries": 500},
                    {"batch_entries": 5000},
                    {"batch_entries": 10000},
                ]
            },
            "volumes": {
                "p16s": {
                    "addrs1": {
                        "metrics": {"schema": SITE.snowflake_scope.schema, "database": SITE.snowflake_scope.database, "status": "PASS"}
                    }
                }
            },
        },
        "verdict.json": {
            "tool_vs_popsink_cost_efficiency": "non",
            "usd": "INCOMPLETE",
            "pause_t0": "2026-08-26T19:00:20Z",
            "ac2": "MET",
        },
        "popsink-paused.json": {
            "source_name": "AS400-ALPHA",
            "status": "paused",
            "paused": True,
            "stop_http": 202,
            "t0": "2026-08-26T19:00:20Z",
            "paused_wait_s": 137.3,
            "target_name": "snowflake-dev",
            "target_status": "live",
        },
        "popsink-restored.json": {
            "source_name": "AS400-ALPHA",
            "source_status": "live",
            "live": True,
            "ibmi_ready": True,
            "target_touched": False,
            "useriddisabled_hits": 0,
            "ibmi": {"ready": True, "restarts": 0, "name_suffix": "56-wth7q"},
        },
    }
    defaults.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    for name, payload in defaults.items():
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    return root


class SoloE2eReportTests(unittest.TestCase):
    def test_build_report_maps_scratch_verdict_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = _write_scratch(Path(tmp))
            report = build_report(scratch, database=SITE.snowflake_scope.database, schema=SITE.snowflake_scope.schema)
        self.assertEqual(report["verdict"], "non")
        self.assertEqual(report["ac2"], "MET")
        self.assertEqual(report["pause_t0"], "2026-08-26T19:00:20Z")
        self.assertEqual(report["usd"], "INCOMPLETE")
        self.assertEqual(report["schema"], SITE.snowflake_scope.schema)
        self.assertEqual(report["verdict_bars"]["best_events_per_sec"], 12.5)
        self.assertEqual(report["verdict_bars"]["best_cpu_ms_per_event"], 4.2)
        self.assertEqual(report["volumes"], [500, 5000, 10000])
        self.assertNotEqual(report["ac2"], "UNMET")

    def test_build_report_rejects_missing_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = _write_scratch(
                Path(tmp),
                **{"verdict.json": {"usd": "INCOMPLETE", "ac2": "MET"}},
            )
            with self.assertRaisesRegex(ValueError, "malformed verdict"):
                build_report(scratch, database=SITE.snowflake_scope.database, schema=SITE.snowflake_scope.schema)

    def test_emit_and_gate_agree_on_six_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = _write_scratch(Path(tmp) / "scratch")
            reports = Path(tmp) / "reports"
            reports.mkdir()
            json_path, md_path = emit_report(scratch, reports, database=SITE.snowflake_scope.database, schema=SITE.snowflake_scope.schema)
            self.assertEqual(json_path.name, f"{REPORT_STEM}.json")
            self.assertEqual(md_path.name, f"{REPORT_STEM}.md")
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "non")
            self.assertEqual(gate_report_pair(json_path, md_path)["status"], "GATE_PASS")

    def test_gate_fails_when_json_ac2_unmet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / f"{REPORT_STEM}.json"
            md_path = Path(tmp) / f"{REPORT_STEM}.md"
            json_path.write_text(
                json.dumps(
                    {
                        "ac2": "UNMET",
                        "pause_t0": "2026-08-26T19:00:20Z",
                        "verdict": "non",
                        "usd": "INCOMPLETE",
                        "schema": SITE.snowflake_scope.schema,
                        "volumes": [500, 5000, 10000],
                        "verdict_bars": {
                            "best_events_per_sec": 12.5,
                            "best_cpu_ms_per_event": 4.2,
                        },
                    }
                ),
                encoding="utf-8",
            )
            md_path.write_text("verdict **non** ac2 UNMET", encoding="utf-8")
            result = gate_report_pair(json_path, md_path)
            self.assertEqual(result["status"], "GATE_FAIL")
            self.assertIn("ac2", result["reason"])

    def test_gate_fails_when_markdown_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / f"{REPORT_STEM}.json"
            md_path = Path(tmp) / f"{REPORT_STEM}.md"
            json_path.write_text(
                json.dumps(
                    {
                        "ac2": "MET",
                        "pause_t0": "2026-08-26T19:00:20Z",
                        "verdict": "non",
                        "usd": "INCOMPLETE",
                        "schema": SITE.snowflake_scope.schema,
                        "volumes": [500, 5000, 10000],
                        "verdict_bars": {
                            "best_events_per_sec": 12.5,
                            "best_cpu_ms_per_event": 4.2,
                        },
                    }
                ),
                encoding="utf-8",
            )
            md_path.write_text("unrelated", encoding="utf-8")
            result = gate_report_pair(json_path, md_path)
            self.assertEqual(result["status"], "GATE_FAIL")
