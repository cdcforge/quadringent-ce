from __future__ import annotations

from datetime import datetime, timezone
import unittest

import site_fixture
from quadringent.destination_sync import (
    DestinationSyncError,
    assert_dedicated_sale_object,
    sync_captured_destination,
)
from quadringent.site_config import SnowflakeScope
from quadringent.snowflake_replay import SnowflakeReplayConfig


SITE = site_fixture.build_test_site()
OBSERVED = datetime(2026, 8, 31, 16, 12, 27, tzinfo=timezone.utc)
OBJECT_KEY = f"{SITE.stream_prefix}/batch-proof.jsonl"


def capture_document(events: int = 7) -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "position": {"checkpoint": {"receiver": "TRNJRN3848", "sequence": 76071470}},
        "counters": {"events_published": {"value": events}},
    }


def config(**overrides: str) -> SnowflakeReplayConfig:
    values = {
        "scope": SITE.snowflake_scope,
        "stage": SITE.proof_stage,
        "raw_table": "IBMI_TEST_EXT_RAW_E2E20260831",
        "canonical_table": "IBMI_TEST_EXT_CANONICAL_E2E20260831",
        "object_key": OBJECT_KEY,
    }
    values.update(overrides)
    return SnowflakeReplayConfig(**values)


class ScriptedCursor:
    """Cursor that drives the shipped replay functions, not a copy of them."""

    def __init__(
        self,
        *,
        listed: list[tuple[object, ...]] | None = None,
        row_count: int = 7,
        copy_status: str = "LOADED",
        fail_on: str | None = None,
    ) -> None:
        self.listed = listed if listed is not None else [(OBJECT_KEY, 128)]
        self.row_count = row_count
        self.copy_status = copy_status
        self.fail_on = fail_on
        self.description: tuple[tuple[str, ...], ...] | None = None
        self.sfqid = "qid-test"
        self.executed: list[str] = []
        self._merge_count = 0
        self._last: tuple[str, object] = ("none", None)

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self.fail_on and self.fail_on.lower() in sql.lower():
            if sql.startswith("LIST"):
                raise OSError("unavailable")
            raise RuntimeError("unavailable")
        if sql.startswith("LIST"):
            self.description = (("name",), ("size",))
            self._last = ("rows", list(self.listed))
            return
        if "COPY INTO" in sql:
            if self.copy_status == "LOADED" and self._merge_count == 0:
                self.description = (
                    ("file",),
                    ("status",),
                    ("rows_parsed",),
                    ("rows_loaded",),
                    ("error_limit",),
                    ("errors_seen",),
                )
                self._last = (
                    "rows",
                    [("batch-proof.jsonl", "LOADED", self.row_count, self.row_count, 0, 0)],
                )
            else:
                self.description = (("status",),)
                self._last = ("rows", [(None,)])
            return
        if "MERGE INTO" in sql:
            inserted = 0 if self._merge_count or self.copy_status != "LOADED" else self.row_count
            self._merge_count += 1
            self.description = (("number of rows inserted",),)
            self._last = ("rows", [(inserted,)])
            return
        if "COUNT(DISTINCT" in sql:
            self.description = (("COUNT(*)",), ("COUNT(DISTINCT)",))
            self._last = ("one", (self.row_count, self.row_count))
            return
        if "COUNT(*)" in sql:
            self.description = (("COUNT(*)",),)
            self._last = ("one", (self.row_count,))
            return
        self.description = None
        self._last = ("none", None)

    def fetchall(self):
        kind, payload = self._last
        return payload if kind == "rows" else []

    def fetchone(self):
        kind, payload = self._last
        return payload if kind == "one" else None


class DestinationSyncTests(unittest.TestCase):
    def test_nominal_replay_attaches_a_reconciled_proof(self) -> None:
        combined = sync_captured_destination(
            ScriptedCursor(),
            capture_document(),
            config(),
            run_tag="E2E20260831",
            observed_at=OBSERVED,
            site=SITE,
        )

        destination = combined["destination"]
        self.assertEqual(destination["schema"], SITE.destination_schema)
        self.assertEqual(destination["stage"], SITE.proof_stage)
        self.assertEqual(destination["source_events"], 7)
        self.assertEqual(destination["raw_rows"], 7)
        self.assertEqual(destination["canonical_rows"], 7)
        self.assertEqual(destination["duplicates"], 0)
        self.assertEqual(destination["load_checkpoint"]["sequence"], 76071470)

    def test_already_loaded_file_is_idempotent_when_counts_match(self) -> None:
        combined = sync_captured_destination(
            ScriptedCursor(copy_status="SKIPPED"),
            capture_document(),
            config(),
            run_tag="E2E20260831",
            observed_at=OBSERVED,
            site=SITE,
        )

        self.assertEqual(combined["destination"]["raw_rows"], 7)
        self.assertEqual(combined["destination"]["duplicates"], 0)

    def test_partial_counts_fail_closed(self) -> None:
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                ScriptedCursor(row_count=3),
                capture_document(events=7),
                config(),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(raised.exception.code, "unreconciled")

    def test_stage_list_failure_is_store_unavailable(self) -> None:
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                ScriptedCursor(fail_on="LIST"),
                capture_document(),
                config(),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(raised.exception.code, "store_unavailable")

    def test_copy_failure_is_snowflake_unavailable(self) -> None:
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                ScriptedCursor(fail_on="COPY INTO"),
                capture_document(),
                config(),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(raised.exception.code, "snowflake_unavailable")

    def test_missing_stage_object_fails_closed(self) -> None:
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                ScriptedCursor(listed=[]),
                capture_document(),
                config(),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(raised.exception.code, "snowflake_unavailable")

    def test_forbidden_fragment_key_is_rejected_before_sql(self) -> None:
        cursor = ScriptedCursor()
        with self.assertRaises(DestinationSyncError) as raised:
            assert_dedicated_sale_object(
                "popsink/raw/batch.jsonl",
                required_prefix=SITE.stream_prefix,
                forbidden_fragments=SITE.forbidden_fragments,
            )
        self.assertEqual(raised.exception.code, "forbidden_prefix")
        with self.assertRaises(DestinationSyncError) as sync_raised:
            sync_captured_destination(
                cursor,
                capture_document(),
                config(object_key="popsink/raw/batch.jsonl"),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(sync_raised.exception.code, "forbidden_prefix")
        self.assertEqual(cursor.executed, [])

    def test_outside_prefix_key_on_sync_never_executes_sql(self) -> None:
        cursor = ScriptedCursor()
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                cursor,
                capture_document(),
                config(object_key=f"{SITE.raw_prefix_root}/other/batch.jsonl"),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(raised.exception.code, "prefix_forbidden")
        self.assertEqual(cursor.executed, [])

    def test_foreign_scope_is_rejected_by_sync(self) -> None:
        foreign = SnowflakeReplayConfig(
            scope=SnowflakeScope(database="ACME_RAW", schema="OTHER_SCHEMA"),
            stage="OTHER_SCHEMA_STAGE",
            raw_table="OTHER_EXT_RAW_E2E20260831",
            canonical_table="OTHER_EXT_CANONICAL_E2E20260831",
            object_key=OBJECT_KEY,
        )
        with self.assertRaises(ValueError):
            sync_captured_destination(
                ScriptedCursor(),
                capture_document(),
                foreign,
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )

    def test_explicit_prefixed_object_keys_are_copied_relative_to_the_sale_stage(self) -> None:
        keys = [
            f"{SITE.stream_prefix}/batch-aaa.jsonl",
            f"{SITE.stream_prefix}/batch-bbb.jsonl",
        ]
        cursor = ScriptedCursor(
            listed=[
                (f"s3://{SITE.raw_bucket}/" + keys[0], 128),
                (f"s3://{SITE.raw_bucket}/" + keys[1], 256),
            ],
            row_count=7,
        )

        combined = sync_captured_destination(
            cursor,
            capture_document(),
            config(),
            run_tag="EVNTTLS20260831",
            observed_at=OBSERVED,
            site=SITE,
            object_keys=keys,
        )

        copy_sql = next(sql for sql in cursor.executed if "COPY INTO" in sql)
        self.assertIn("FILES = ('batch-aaa.jsonl', 'batch-bbb.jsonl')", copy_sql)
        self.assertNotIn("PATTERN = '.*[.]jsonl$'", copy_sql)
        self.assertNotIn(f"{SITE.stream_prefix}/batch-", copy_sql)
        self.assertEqual(combined["destination"]["source_events"], 7)
        self.assertEqual(combined["destination"]["raw_rows"], 7)
        self.assertEqual(combined["destination"]["duplicates"], 0)

    def test_forbidden_key_in_object_list_never_executes_sql(self) -> None:
        cursor = ScriptedCursor()
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                cursor,
                capture_document(),
                config(),
                run_tag="EVNTTLS20260831",
                observed_at=OBSERVED,
                site=SITE,
                object_keys=["popsink/raw/batch.jsonl"],
            )
        self.assertEqual(raised.exception.code, "forbidden_prefix")
        self.assertEqual(cursor.executed, [])

    def test_non_dedicated_stage_is_rejected(self) -> None:
        cursor = ScriptedCursor()
        with self.assertRaises(DestinationSyncError) as raised:
            sync_captured_destination(
                cursor,
                capture_document(),
                config(stage="IBMI_TEST_EXTERNAL_STAGE"),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
                site=SITE,
            )
        self.assertEqual(raised.exception.code, "stage_forbidden")
        self.assertEqual(cursor.executed, [])


if __name__ == "__main__":
    unittest.main()
