from __future__ import annotations

import site_fixture

from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from quadringent.slo_telemetry import collect_slo_telemetry


SITE = site_fixture.build_test_site()

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 9, 1, 11, 59, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 1, 11, 59, 40, tzinfo=timezone.utc)


class S3Client:
    def head_object(self, **kwargs: object) -> dict[str, object]:
        if kwargs["Bucket"] != SITE.raw_bucket:
            raise AssertionError("unexpected bucket")
        return {"LastModified": WINDOW_END}

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        expected = {
            "Bucket": SITE.raw_bucket,
            "Prefix": f"{SITE.stream_prefix}/",
        }
        token = kwargs.pop("ContinuationToken", None)
        if kwargs != expected:
            raise AssertionError("collector escaped the exact DEV raw prefix")
        if token is None:
            return {
                "Contents": [
                    {
                        "Key": f"{SITE.stream_prefix}/batch-old.jsonl",
                        "LastModified": datetime(2026, 9, 1, 11, 59, 30, tzinfo=timezone.utc),
                    },
                    {
                        "Key": f"{SITE.stream_prefix}/console-snapshot.json",
                        "LastModified": NOW,
                    },
                ],
                "NextContinuationToken": "next",
            }
        if token == "next":
            return {
                "Contents": [
                    {
                        "Key": f"{SITE.stream_prefix}/batch-new.jsonl",
                        "LastModified": datetime(2026, 9, 1, 11, 59, 35, tzinfo=timezone.utc),
                    }
                ]
            }
        raise AssertionError("unexpected S3 continuation token")


class CloudWatchClient:
    def get_metric_statistics(self, **kwargs: object) -> dict[str, object]:
        if kwargs["Namespace"] != "AWS/S3" or kwargs["MetricName"] != "AllRequests":
            raise AssertionError("collector requested an unrelated metric")
        dimensions = {item["Name"]: item["Value"] for item in kwargs["Dimensions"]}
        if dimensions != {
            "BucketName": SITE.raw_bucket,
            "FilterId": "EntireBucket",
        }:
            raise AssertionError("collector escaped the exact DEV bucket")
        return {"Datapoints": [{"Sum": 12.0}, {"Sum": 8.0}]}


class SnowflakeCursor:
    def __init__(self) -> None:
        self._row: tuple[object, ...] | None = None

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        upper = " ".join(sql.upper().split())
        if "SYSTEM$PIPE_STATUS('ACME_RAW.IBMI_TEST.QUADRINGENT_SALE_PIPE')" in upper:
            self._row = (json.dumps({"pendingFileCount": 0}),)
        elif "APPROX_PERCENTILE" in upper:
            if "ACME_RAW.IBMI_TEST.QUADRINGENT_SALE_CANONICAL" not in upper:
                raise AssertionError("latency query escaped the exact DEV canonical table")
            if params != (WINDOW_START, WINDOW_END):
                raise AssertionError("latency query escaped the exact proof window")
            self._row = (12.5, 21.0, 1.0, 102, 0)
        elif "QUADRINGENT_TEST_WH_METERING" in upper:
            if "WAREHOUSE_NAME = 'QUADRINGENT_TEST_WH'" not in upper:
                raise AssertionError("credit query escaped the dedicated DEV warehouse")
            self._row = (Decimal("0.05"), 2, 2)
        else:
            raise AssertionError("unexpected Snowflake query")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class SloTelemetryTests(unittest.TestCase):
    def test_missing_metering_rows_are_unknown_but_measured_zero_is_valid(self):
        for row, expected in [((0, 0, 0), None), ((0.05, 2, 1), None), ((None, 1, 0), None), ((0, 1, 1), 0)]:
            class Cursor(SnowflakeCursor):
                def execute(self, sql, params=None):
                    if 'SUM(CREDITS_USED)' in sql:
                        self._row = row
                    else:
                        super().execute(sql, params)
            telemetry = collect_slo_telemetry(S3Client(), CloudWatchClient(), Cursor(),
                now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END, site=SITE)
            with self.subTest(row=row):
                self.assertEqual(telemetry.get('snowflake_credits_24h'), expected)
                self.assertEqual('snowflake_credits_window' in telemetry, expected is not None)

    def test_slo_report_does_not_label_delayed_metering_as_current_cost(self):
        from quadringent.slo import evaluate_slo
        from test_slo import _proof, _policy
        telemetry = collect_slo_telemetry(S3Client(), CloudWatchClient(), SnowflakeCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END, site=SITE)
        report = evaluate_slo(_proof(), telemetry, _policy(), now=NOW, site=SITE)
        cost = next(check for check in report['checks'] if check['id'] == 'snowflake_credits')
        self.assertIn('delayed', cost['unit'])
        self.assertIn('warehouse', cost['unit'])

    def test_metering_reads_only_delayed_dev_view_and_never_invents_zero(self):
        class MeteringCursor(SnowflakeCursor):
            def execute(self, sql, params=None):
                if 'SUM(CREDITS_USED)' in sql:
                    self.query, self.params = sql, params
                    self._row = (None, 0, 0)
                else:
                    super().execute(sql, params)
        cursor = MeteringCursor()
        telemetry = collect_slo_telemetry(S3Client(), CloudWatchClient(), cursor,
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END, site=SITE)
        self.assertNotIn('snowflake_credits_24h', telemetry)
        self.assertIn('FROM ACME_RAW.IBMI_TEST.QUADRINGENT_TEST_WH_METERING', cursor.query)
        self.assertNotIn('COALESCE', cursor.query)
        self.assertEqual(cursor.params, (
            datetime(2026, 8, 31, 6, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 6, tzinfo=timezone.utc)))

    def test_latency_missing_one_reconciled_event_is_unobserved(self) -> None:
        telemetry = collect_slo_telemetry(
            S3Client(), CloudWatchClient(), SnowflakeCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
            expected_event_count=103,
        site=SITE)
        self.assertNotIn("delivery_latency_p95_seconds", telemetry)
        self.assertNotIn("delivery_latency_p99_seconds", telemetry)
        self.assertIn({"source": "delivery_latency", "error_type": "ValueError"}, telemetry["collection_errors"])

    def test_scoped_s3_freshness_cannot_come_from_another_run(self) -> None:
        class ScopedS3:
            def head_object(self, **kwargs: object) -> dict[str, object]:
                expected = {
                    "Bucket": SITE.raw_bucket,
                    "Key": f"{SITE.stream_prefix}/runs/canary/batch-one.jsonl",
                }
                if kwargs != expected:
                    raise AssertionError("escaped reconciled object set")
                return {"LastModified": WINDOW_START}

            def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
                return {"Contents": [{
                    "Key": f"{SITE.stream_prefix}/runs/other/batch-new.jsonl",
                    "LastModified": NOW,
                }]}

        telemetry = collect_slo_telemetry(
            ScopedS3(), CloudWatchClient(), SnowflakeCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
            object_keys=[f"{SITE.stream_prefix}/runs/canary/batch-one.jsonl"],
        site=SITE)
        self.assertEqual(telemetry["s3_last_object_at"], WINDOW_START.isoformat())

    def test_latency_can_be_bound_to_exact_reconciled_object_keys(self) -> None:
        class ScopedCursor(SnowflakeCursor):
            def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
                if "APPROX_PERCENTILE" in sql.upper():
                    self.latency_sql = sql
                    self.latency_params = params
                    self._row = (12.5, 21.0, 1.0, 102, 0)
                else:
                    super().execute(sql, params)
        cursor = ScopedCursor()
        telemetry = collect_slo_telemetry(S3Client(), CloudWatchClient(), cursor,
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
            object_keys=[f"{SITE.stream_prefix}/batch-one.jsonl", f"{SITE.stream_prefix}/nested/batch-two.jsonl"], site=SITE)
        self.assertEqual(telemetry["collection_errors"], [])
        self.assertIn("AND (RIGHT(SOURCE_FILE", cursor.latency_sql)
        self.assertNotIn("batch-one", cursor.latency_sql)
        self.assertEqual(cursor.latency_params, (WINDOW_START, WINDOW_END,
            "batch-one.jsonl", "batch-one.jsonl", "nested/batch-two.jsonl", "nested/batch-two.jsonl"))

    def test_missing_scoped_object_never_falls_back_to_global_freshness(self) -> None:
        class MissingObject(S3Client):
            def head_object(self, **kwargs: object) -> dict[str, object]:
                raise PermissionError("sensitive provider diagnostic")

        telemetry = collect_slo_telemetry(
            MissingObject(), CloudWatchClient(), SnowflakeCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
            object_keys=[f"{SITE.stream_prefix}/runs/canary/batch-one.jsonl"],
        site=SITE)
        self.assertNotIn("s3_last_object_at", telemetry)
        self.assertIn({"source": "s3", "error_type": "PermissionError"}, telemetry["collection_errors"])
        self.assertNotIn("sensitive provider diagnostic", json.dumps(telemetry))

    def test_invalid_object_scope_is_rejected_before_collecting_anything(self) -> None:
        for keys in ([], [f"{SITE.raw_prefix_root}/cntr/file.jsonl"],
                     [f"{SITE.stream_prefix}/file.jsonl"] * 2,
                     [f"{SITE.stream_prefix}/file.jsonl"] * 1001,
                     f"{SITE.stream_prefix}/file.jsonl"):
            with self.subTest(keys_type=type(keys).__name__):
                with self.assertRaises(ValueError):
                    collect_slo_telemetry(None, None, None,
                        now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
                        object_keys=keys, site=SITE)

    def test_latency_query_does_not_hide_clock_skew_or_invalid_timestamps(self) -> None:
        class InspectCursor(SnowflakeCursor):
            def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
                if "APPROX_PERCENTILE" in sql.upper():
                    self.latency_sql = sql.upper()
                super().execute(sql, params)

        cursor = InspectCursor()
        collect_slo_telemetry(
            S3Client(), CloudWatchClient(), cursor,
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
        site=SITE)
        self.assertNotIn("GREATEST", cursor.latency_sql)
        self.assertIn("MIN(DELIVERY_SECONDS)", cursor.latency_sql)
        self.assertIn("COUNT(*)", cursor.latency_sql)
        self.assertIn("COUNT_IF(DELIVERY_SECONDS IS NULL)", cursor.latency_sql)

    def test_even_one_bad_timestamp_invalidates_otherwise_positive_percentiles(self) -> None:
        for bad_row in ((12.5, 21.0, -7190, 102, 0),
                        (12.5, 21.0, 1, 102, 1),
                        (12.5, 21.0, 1, 0, 0),
                        (12.5, 21.0, float("nan"), 102, 0)):
            with self.subTest(row=bad_row):
                class InvalidLatencyCursor(SnowflakeCursor):
                    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
                        if "APPROX_PERCENTILE" in sql.upper():
                            self._row = bad_row
                        else:
                            super().execute(sql, params)

                telemetry = collect_slo_telemetry(
                    S3Client(), CloudWatchClient(), InvalidLatencyCursor(),
                    now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
                site=SITE)
                self.assertNotIn("delivery_latency_p95_seconds", telemetry)
                self.assertNotIn("delivery_latency_p99_seconds", telemetry)
                self.assertEqual(telemetry["snowflake_credits_24h"], 0.05)
                self.assertIn({"source": "delivery_latency", "error_type": "ValueError"},
                              telemetry["collection_errors"])

    def test_collector_returns_only_the_six_attributed_dev_measurements(self) -> None:
        telemetry = collect_slo_telemetry(
            S3Client(), CloudWatchClient(), SnowflakeCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
        site=SITE)

        self.assertEqual(
            telemetry,
            {
                "s3_last_object_at": "2026-09-01T11:59:35+00:00",
                "s3_requests_24h": 20,
                "snowpipe_pending_files": 0,
                "delivery_latency_p95_seconds": 12.5,
                "delivery_latency_p99_seconds": 21.0,
                "snowflake_credits_24h": 0.05,
                "snowflake_credits_window": {
                    "from_inclusive": "2026-08-31T06:00:00+00:00",
                    "to_exclusive": "2026-09-01T06:00:00+00:00",
                    "scope": SITE.warehouse_name, "status": "delayed_metering", "reported_rows": 2,
                },
                "collected_at": NOW.isoformat(),
                "collection_errors": [],
            },
        )

    def test_failed_snowflake_collection_keeps_aws_metrics_and_marks_gap(self) -> None:
        class FailedCursor:
            def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
                raise PermissionError("secret-bearing server detail")

        telemetry = collect_slo_telemetry(
            S3Client(), CloudWatchClient(), FailedCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
        site=SITE)

        self.assertEqual(telemetry["s3_requests_24h"], 20)
        self.assertNotIn("snowpipe_pending_files", telemetry)
        self.assertEqual(
            telemetry["collection_errors"],
            [
                {"source": "snowpipe", "error_type": "PermissionError"},
                {"source": "delivery_latency", "error_type": "PermissionError"},
                {"source": "snowflake_credits", "error_type": "PermissionError"},
            ],
        )
        self.assertNotIn("secret-bearing", json.dumps(telemetry))

    def test_missing_latency_window_preserves_pipe_and_credit_measurements(self) -> None:
        class EmptyLatencyCursor(SnowflakeCursor):
            def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
                if "APPROX_PERCENTILE" in sql.upper():
                    self._row = (None, None)
                    return
                super().execute(sql, params)

        telemetry = collect_slo_telemetry(
            S3Client(), CloudWatchClient(), EmptyLatencyCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
        site=SITE)

        self.assertEqual(telemetry["snowpipe_pending_files"], 0)
        self.assertEqual(telemetry["snowflake_credits_24h"], 0.05)
        self.assertNotIn("delivery_latency_p95_seconds", telemetry)
        self.assertNotIn("delivery_latency_p99_seconds", telemetry)
        self.assertEqual(
            telemetry["collection_errors"],
            [{"source": "delivery_latency", "error_type": "ValueError"}],
        )

    def test_invalid_or_negative_external_values_are_rejected(self) -> None:
        class InvalidCloudWatch(CloudWatchClient):
            def get_metric_statistics(self, **kwargs: object) -> dict[str, object]:
                return {"Datapoints": [{"Sum": -1.0}]}

        with self.assertRaisesRegex(ValueError, "s3_requests_24h"):
            collect_slo_telemetry(
                S3Client(), InvalidCloudWatch(), SnowflakeCursor(),
                now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
            site=SITE)

    def test_no_cloudwatch_datapoint_is_unobserved_not_a_false_zero(self) -> None:
        class EmptyCloudWatch(CloudWatchClient):
            def get_metric_statistics(self, **kwargs: object) -> dict[str, object]:
                return {"Datapoints": []}

        telemetry = collect_slo_telemetry(
            S3Client(), EmptyCloudWatch(), SnowflakeCursor(),
            now=NOW, window_started_at=WINDOW_START, window_ended_at=WINDOW_END,
        site=SITE)

        self.assertNotIn("s3_requests_24h", telemetry)
        self.assertIn(
            {"source": "cloudwatch", "error_type": "LookupError"},
            telemetry["collection_errors"],
        )


if __name__ == "__main__":
    unittest.main()
