from __future__ import annotations

import site_fixture

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from quadringent.snowflake_business_replay import (
    BusinessReplayConfig,
    build_synthetic_events,
    build_technical_image_events,
    evaluate_business_snapshot,
)


SITE = site_fixture.build_test_site()
SOURCE = {"source_library": SITE.source_schema, "source_table": "CUDFIXTURE"}


def _env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{root / 'src'}:{root / 'scripts'}:{root}"
    return env


class SnowflakeBusinessReplayTests(unittest.TestCase):
    def test_cli_is_dry_run_by_default(self) -> None:
        script = Path(__file__).parents[1] / "scripts/snowflake_business_cud_replay.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--run-tag", "CUD20260820A"],
            capture_output=True,
            text=True,
            env=_env(Path(__file__).parents[1]),
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["canonical_table"], f"{SITE.destination_schema}_CUD_CANONICAL_CUD20260820A")
        self.assertNotIn("PRIVATE", completed.stdout.upper())

    def test_cli_exposes_a_bounded_technical_image_fixture(self) -> None:
        script = Path(__file__).parents[1] / "scripts/snowflake_business_cud_replay.py"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--run-tag",
                "CUD20260821T",
                "--fixture",
                "technical-images",
            ],
            capture_output=True,
            text=True,
            env=_env(Path(__file__).parents[1]),
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["fixture"], "technical-images")
        self.assertEqual(result["synthetic_event_count"], 7)

    def test_synthetic_fixture_contains_c_u_d_and_key_change(self) -> None:
        events = build_synthetic_events(**SOURCE)

        self.assertEqual([event["operation"] for event in events], ["c", "u", "u", "c", "d"])
        self.assertEqual(events[1]["before"]["ID"], "A")
        self.assertEqual(events[2]["before"]["ID"], "A")
        self.assertEqual(events[2]["after"]["ID"], "B")
        self.assertEqual(events[4]["before"]["ID"], "C")
        self.assertIsNone(events[4]["after"])

    def test_technical_fixture_preserves_before_and_after_roles(self) -> None:
        events = build_technical_image_events(**SOURCE)

        self.assertEqual(
            [event["operation"] for event in events],
            ["c", "u_before", "u_after", "u_before", "u_after", "c", "d"],
        )
        self.assertEqual(events[1]["before"]["ID"], "A")
        self.assertIsNone(events[1]["after"])
        self.assertIsNone(events[2]["before"])
        self.assertEqual(events[2]["after"]["ID"], "A")
        self.assertEqual(events[4]["after"]["ID"], "B")

    def test_technical_fixture_has_expected_final_sequence(self) -> None:
        self.assertEqual(
            evaluate_business_snapshot(
                [("B", "moved", 121, "upsert")],
                expected_sequence=121,
            ),
            {"status": "PASS", "row_count": 1, "keys": ["B"]},
        )

    def test_fixture_has_one_expected_final_business_row(self) -> None:
        self.assertEqual(
            evaluate_business_snapshot([("B", "moved", 120, "upsert")]),
            {"status": "PASS", "row_count": 1, "keys": ["B"]},
        )

    def test_fixture_rejects_a_missing_or_stale_business_row(self) -> None:
        with self.assertRaisesRegex(ValueError, "business snapshot mismatch"):
            evaluate_business_snapshot([("A", "two", 110, "upsert")])

    def test_run_tag_is_bounded_and_table_names_are_isolated(self) -> None:
        config = BusinessReplayConfig(run_tag="CUD20260820A", scope=SITE.snowflake_scope, **SOURCE)

        self.assertEqual(config.canonical_table, f"{SITE.destination_schema}_CUD_CANONICAL_CUD20260820A")
        self.assertEqual(config.target_table, f"{SITE.destination_schema}_CUD_CURRENT_CUD20260820A")
        self.assertEqual(config.key_columns, ("ID",))

        with self.assertRaises(ValueError):
            BusinessReplayConfig(run_tag="bad-tag", scope=SITE.snowflake_scope, **SOURCE)


if __name__ == "__main__":
    unittest.main()
