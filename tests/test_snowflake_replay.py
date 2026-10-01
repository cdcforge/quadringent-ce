from __future__ import annotations
import site_fixture

import unittest

from quadringent.snowflake_replay import (
    SnowflakeReplayConfig,
    summarize_copy_result,
)


SITE = site_fixture.build_test_site()
STAGE = f"{SITE.destination_schema}_EXTERNAL_STAGE"
RAW_TABLE = f"{SITE.destination_schema}_EXT_RAW_20260820"
CANONICAL_TABLE = f"{SITE.destination_schema}_EXT_CANONICAL_20260820"
from quadringent.site_config import SnowflakeScope
FOREIGN_SCOPE = SnowflakeScope(
    database="DEV_RAW", schema="POPSINK_ALPHA",
    forbidden_fragments=SITE.snowflake_scope.forbidden_fragments,
)

class SnowflakeReplayConfigTests(unittest.TestCase):
    def test_stage_reference_and_object_key_are_bounded(self) -> None:
        config = SnowflakeReplayConfig(
            scope=SITE.snowflake_scope, 
            stage=STAGE,
            raw_table=RAW_TABLE,
            canonical_table=CANONICAL_TABLE,
            object_key="batch-abc.jsonl",
        )

        self.assertEqual(
            config.stage_reference,
            f'@"{SITE.snowflake_scope.database}"."{SITE.snowflake_scope.schema}"."{STAGE}"',
        )
        self.assertEqual(config.object_reference, f"{config.stage_reference}/batch-abc.jsonl")
        statements = config.plan.statements_for(config.object_key)
        sql = " ".join(statements)
        self.assertIn(f'"{SITE.snowflake_scope.database}"."{SITE.snowflake_scope.schema}"', sql)
        self.assertNotIn("POPSINK", sql)

    def test_replay_config_refuses_popsink_as400_alpha_destination(self) -> None:
        with self.assertRaises(ValueError):
            SnowflakeReplayConfig(
                scope=FOREIGN_SCOPE,
                stage=STAGE,
                raw_table=RAW_TABLE,
                canonical_table=CANONICAL_TABLE,
                object_key="batch-abc.jsonl",
            )

    def test_identifiers_and_paths_cannot_escape_the_stage(self) -> None:
        with self.assertRaises(ValueError):
            SnowflakeReplayConfig(
                scope=SITE.snowflake_scope, 
                stage=STAGE,
                raw_table="RAW;DROP TABLE X",
                canonical_table="CANONICAL",
                object_key="batch.jsonl",
            )
        with self.assertRaises(ValueError):
            SnowflakeReplayConfig(
                scope=SITE.snowflake_scope, 
                stage=STAGE,
                raw_table="RAW",
                canonical_table="CANONICAL",
                object_key="../outside.jsonl",
            )


class CopyResultTests(unittest.TestCase):
    def test_loaded_copy_result_is_reduced_to_safe_counters(self) -> None:
        result = summarize_copy_result(
            ("file", "status", "rows_parsed", "rows_loaded", "error_limit", "errors_seen"),
            (("redacted.jsonl", "LOADED", 3, 3, 0, 0),),
        )

        self.assertEqual(result["status"], "LOADED")
        self.assertEqual(result["rows_parsed"], 3)
        self.assertEqual(result["rows_loaded"], 3)
        self.assertEqual(result["errors_seen"], 0)
        self.assertNotIn("redacted.jsonl", str(result))

    def test_multi_file_copy_totals_every_safe_counter(self) -> None:
        result = summarize_copy_result(
            ("file", "status", "rows_parsed", "rows_loaded", "error_limit", "errors_seen"),
            (
                ("first.jsonl", "LOADED", 3, 3, 0, 0),
                ("second.jsonl", "LOADED", 5, 5, 0, 0),
            ),
        )

        self.assertEqual(result["result_rows"], 2)
        self.assertEqual(result["status"], "LOADED")
        self.assertEqual(result["rows_parsed"], 8)
        self.assertEqual(result["rows_loaded"], 8)
        self.assertEqual(result["errors_seen"], 0)
        self.assertNotIn("first.jsonl", str(result))
        self.assertNotIn("second.jsonl", str(result))

    def test_replayed_copy_with_only_status_is_counted_as_zero_loaded(self) -> None:
        result = summarize_copy_result(("status",), ((None,),))

        self.assertEqual(result["rows_loaded"], 0)
        self.assertIsNone(result["status"])


if __name__ == "__main__":
    unittest.main()


class CanonicalDeduplicationTests(unittest.TestCase):
    """A load whose files repeat an event (replayed batch) keeps one canonical row."""

    def config(self) -> SnowflakeReplayConfig:
        return SnowflakeReplayConfig(scope=SITE.snowflake_scope, stage=STAGE, raw_table=RAW_TABLE,
                                     canonical_table=CANONICAL_TABLE, object_key="batch-a.jsonl")

    def test_every_canonical_merge_source_keeps_one_row_per_event_id(self) -> None:
        plan = self.config().plan
        merges = [plan.statements_for("batch-a.jsonl")[3], plan.statements_for_all_jsonl()[3],
                  plan.statements_for_object_keys(["batch-a.jsonl", "batch-b.jsonl"])[3]]
        for merge in merges:
            with self.subTest(merge=merge[:40]):
                self.assertIn("QUALIFY ROW_NUMBER() OVER", merge)
                self.assertIn("PARTITION BY PAYLOAD:event_id::VARCHAR", merge)
                self.assertIn(") = 1", merge)

    def test_replay_status_fails_when_canonical_rows_exceed_distinct_events(self) -> None:
        from quadringent.snowflake_replay import execute_external_stage_replay

        class Cursor:
            """Replays the observed defect: 203 raw rows, 154 events, 203 canonical rows."""
            description = [("file",), ("status",), ("rows_parsed",), ("rows_loaded",), ("errors_seen",)]
            sfqid = "query"

            def __init__(self) -> None:
                self.last = ""
                self.merges = 0

            def execute(self, statement: str) -> None:
                self.last = statement
                self.merges += statement.startswith("MERGE")

            def fetchall(self):
                if self.last.startswith("LIST"):
                    return [("gcs://bucket/run/batch-a.jsonl", 10)]
                if self.last.startswith("COPY"):
                    return [("batch-a.jsonl", "LOADED", 203, 203, 0)]
                if self.last.startswith("MERGE"):
                    return [(203 if self.merges == 1 else 0,)]
                return []

            def fetchone(self):
                if "COUNT(DISTINCT" in self.last:
                    return (203, 154)
                return (203,)

        metrics = execute_external_stage_replay(Cursor(), self.config())
        self.assertEqual(metrics["canonical_rows_after_first"], 203)
        self.assertEqual(metrics["merge_second"]["rows_inserted"], 0)
        self.assertEqual(metrics["status"], "FAIL")
