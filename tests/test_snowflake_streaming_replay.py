from __future__ import annotations

import site_fixture

import json
import os
from pathlib import Path
import subprocess
import unittest

from quadringent.snowflake_streaming_replay import (
    StreamingReplayConfig,
    build_stream_rows,
    evaluate_streaming_snapshot,
    offset_token,
)


SITE = site_fixture.build_test_site()
SOURCE = {"source_library": SITE.source_schema, "source_table": "CUDFIXTURE"}


class SnowflakeStreamingReplayTests(unittest.TestCase):
    def test_fixture_contains_structured_c_u_d_rows_and_monotone_offsets(self) -> None:
        rows = build_stream_rows(**SOURCE)

        self.assertEqual(len(rows), 5)
        self.assertEqual([row["OPERATION"] for row in rows], ["c", "u", "u", "c", "d"])
        self.assertEqual([row["JOURNAL_SEQUENCE"] for row in rows], [100, 110, 120, 130, 140])
        self.assertTrue(all(isinstance(row["PAYLOAD"], dict) for row in rows))
        self.assertEqual(offset_token(100), f"{100:012d}")
        self.assertEqual(offset_token(140), f"{140:012d}")

    def test_config_isolated_and_snapshot_validation_is_strict(self) -> None:
        config = StreamingReplayConfig(run_tag="STR20260820A", scope=SITE.snowflake_scope)

        self.assertTrue(config.target_table.startswith(f"{SITE.destination_schema}_STREAMING_"))
        self.assertTrue(config.channel_name.startswith(f"{SITE.destination_schema}_STREAMING_"))
        self.assertEqual(
            evaluate_streaming_snapshot(
                row_count=5,
                distinct_event_count=5,
                latest_offset=offset_token(140),
            )["status"],
            "PASS",
        )
        with self.assertRaises(ValueError):
            evaluate_streaming_snapshot(
                row_count=4,
                distinct_event_count=5,
                latest_offset=offset_token(140),
            )

    def test_run_tag_rejects_unsafe_or_unbounded_names(self) -> None:
        with self.assertRaises(ValueError):
            StreamingReplayConfig(run_tag="bad-name", scope=SITE.snowflake_scope)
        with self.assertRaises(ValueError):
            StreamingReplayConfig(run_tag="A", scope=SITE.snowflake_scope)

    def test_cli_is_dry_run_by_default(self) -> None:
        root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = f"{root / 'src'}:{root / 'scripts'}:{root}"
        result = subprocess.run(
            [
                "python3",
                str(root / "scripts/snowflake_streaming_cud_replay.py"),
                "--run-tag",
                "STR20260820A",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertEqual(payload["synthetic_event_count"], 5)
        self.assertNotIn("SNOWFLAKE_PRIVATE_KEY", result.stdout)


if __name__ == "__main__":
    unittest.main()
