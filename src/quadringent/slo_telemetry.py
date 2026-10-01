from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import math
from typing import Any, Sequence

from .destination_sync import DestinationSyncError, sale_stage_relative_key
from .site_config import SiteConfig


class SharedWarehouseAttributionRequired(ValueError):
    """Le total d'un warehouse partagé ne mesure pas le coût du produit."""


def collect_slo_telemetry(
    s3_client: Any,
    cloudwatch_client: Any,
    snowflake_cursor: Any,
    *,
    now: datetime,
    window_started_at: datetime,
    window_ended_at: datetime,
    site: SiteConfig,
    object_keys: Sequence[str] | None = None,
    expected_event_count: int | None = None,
) -> dict[str, object]:
    """Collect only attributed site signals and sanitize collection failures."""

    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    if any(value.tzinfo is None for value in (now, window_started_at, window_ended_at)):
        raise ValueError("SLO telemetry timestamps must be timezone-aware")
    if window_started_at > window_ended_at or window_ended_at > now:
        raise ValueError("SLO telemetry proof window is invalid")
    if window_ended_at - window_started_at > timedelta(hours=25):
        raise ValueError("SLO telemetry proof window exceeds 25 hours")
    if expected_event_count is not None and (
        type(expected_event_count) is not int or expected_event_count <= 0
    ):
        raise ValueError("expected event count must be a positive integer")
    latency_filter = ""
    latency_params: tuple[object, ...] = (window_started_at, window_ended_at)
    if object_keys is not None:
        if isinstance(object_keys, (str, bytes)) or not 1 <= len(object_keys) <= 1000:
            raise ValueError("latency scope needs between 1 and 1000 object keys")
        if any(not isinstance(key, str) for key in object_keys):
            raise ValueError("latency scope contains an invalid object key")
        try:
            relative_keys = tuple(
                sale_stage_relative_key(
                    key,
                    required_prefix=site.stream_prefix,
                    forbidden_fragments=site.forbidden_fragments,
                )
                for key in object_keys
            )
        except DestinationSyncError:
            raise ValueError("latency scope escapes the declared proof prefix") from None
        if len(set(relative_keys)) != len(relative_keys):
            raise ValueError("latency scope contains duplicate object keys")
        latency_filter = " AND (" + " OR ".join(
            "RIGHT(SOURCE_FILE, LENGTH(%s)) = %s" for _ in relative_keys
        ) + ")"
        latency_params += tuple(value for key in relative_keys for value in (key, key))
    telemetry: dict[str, object] = {
        "collected_at": now.isoformat(),
        "collection_errors": [],
    }
    errors = telemetry["collection_errors"]
    assert isinstance(errors, list)

    try:
        telemetry["s3_last_object_at"] = _latest_jsonl_at(
            s3_client, object_keys, bucket=site.raw_bucket, prefix=site.stream_prefix + "/"
        ).isoformat()
    except Exception as error:
        errors.append({"source": "s3", "error_type": type(error).__name__})

    try:
        response = cloudwatch_client.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="AllRequests",
            Dimensions=[
                {"Name": "BucketName", "Value": site.raw_bucket},
                {"Name": "FilterId", "Value": "EntireBucket"},
            ],
            StartTime=now - timedelta(hours=24),
            EndTime=now,
            Period=86_400,
            Statistics=["Sum"],
        )
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            raise LookupError("s3_requests_24h is unavailable")
        total = sum(
            _non_negative_number(point.get("Sum"), "s3_requests_24h")
            for point in datapoints
        )
        telemetry["s3_requests_24h"] = int(total) if total.is_integer() else total
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        errors.append({"source": "cloudwatch", "error_type": type(error).__name__})

    try:
        snowflake_cursor.execute(
            f"SELECT SYSTEM$PIPE_STATUS('{site.proof_pipe_fqn}')"
        )
        pipe_row = snowflake_cursor.fetchone()
        if not pipe_row or not isinstance(pipe_row[0], str):
            raise ValueError("snowpipe_pending_files is unavailable")
        pipe_status = json.loads(pipe_row[0])
        pending = _non_negative_number(
            pipe_status.get("pendingFileCount"), "snowpipe_pending_files"
        )
        telemetry["snowpipe_pending_files"] = _integer(
            pending, "snowpipe_pending_files"
        )
    except Exception as error:
        telemetry.pop("snowpipe_pending_files", None)
        errors.append({"source": "snowpipe", "error_type": type(error).__name__})

    try:
        snowflake_cursor.execute(
            f"""WITH recent AS (
    SELECT DATEDIFF(
        'millisecond',
        TRY_TO_TIMESTAMP_TZ(PAYLOAD:commit_timestamp::VARCHAR),
        INGESTED_AT
    ) / 1000.0 AS DELIVERY_SECONDS
    FROM {site.proof_canonical_fqn}
    WHERE INGESTED_AT BETWEEN %s AND %s{latency_filter}
)
SELECT
    APPROX_PERCENTILE(DELIVERY_SECONDS, 0.95),
    APPROX_PERCENTILE(DELIVERY_SECONDS, 0.99),
    MIN(DELIVERY_SECONDS),
    COUNT(*),
    COALESCE(COUNT_IF(DELIVERY_SECONDS IS NULL), 0)
FROM recent""",
            latency_params,
        )
        latency_row = snowflake_cursor.fetchone()
        if not latency_row or len(latency_row) != 5:
            raise ValueError("delivery latency percentiles are unavailable")
        # A future source timestamp is clock/zone uncertainty, not zero latency.
        # Validate the entire window: positive percentiles must not hide a bad
        # minority, and NULL timestamps must not disappear from the population.
        samples = _integer(
            _non_negative_number(latency_row[3], "delivery_latency_samples"),
            "delivery_latency_samples",
        )
        invalid = _integer(
            _non_negative_number(latency_row[4], "delivery_latency_invalid_samples"),
            "delivery_latency_invalid_samples",
        )
        if samples == 0 or invalid != 0:
            raise ValueError("delivery latency window contains unmeasurable timestamps")
        if expected_event_count is not None and samples != expected_event_count:
            raise ValueError("delivery latency does not cover the reconciled event population")
        _non_negative_number(latency_row[2], "delivery_latency_minimum_seconds")
        telemetry["delivery_latency_p95_seconds"] = _non_negative_number(
            latency_row[0], "delivery_latency_p95_seconds"
        )
        telemetry["delivery_latency_p99_seconds"] = _non_negative_number(
            latency_row[1], "delivery_latency_p99_seconds"
        )
    except Exception as error:
        telemetry.pop("delivery_latency_p95_seconds", None)
        telemetry.pop("delivery_latency_p99_seconds", None)
        errors.append(
            {"source": "delivery_latency", "error_type": type(error).__name__}
        )

    try:
        # Cloud-services metering can arrive six hours late. Use only closed
        # hours older than that delay, never a supposed real-time bill.
        if not site.manages_warehouse:
            raise SharedWarehouseAttributionRequired()
        cost_end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=6)
        cost_start = cost_end - timedelta(hours=24)
        snowflake_cursor.execute(
            f"""SELECT SUM(CREDITS_USED), COUNT(*), COUNT(CREDITS_USED)
FROM {site.metering_fqn}
WHERE WAREHOUSE_NAME = '{site.warehouse_name}'
  AND START_TIME >= %s AND END_TIME <= %s""",
            (cost_start, cost_end),
        )
        credit_row = snowflake_cursor.fetchone()
        if not credit_row or len(credit_row) != 3:
            raise ValueError("snowflake_credits_24h is unavailable")
        rows = _integer(_non_negative_number(credit_row[1], "metering_rows"), "metering_rows")
        measured_rows = _integer(_non_negative_number(credit_row[2], "metering_measured_rows"), "metering_measured_rows")
        if rows == 0 or rows != measured_rows:
            raise ValueError("warehouse metering contains missing measurements")
        telemetry["snowflake_credits_24h"] = _non_negative_number(
            credit_row[0], "snowflake_credits_24h"
        )
        telemetry["snowflake_credits_window"] = {
            "from_inclusive": cost_start.isoformat(), "to_exclusive": cost_end.isoformat(),
            "scope": site.warehouse_name, "status": "delayed_metering", "reported_rows": rows,
        }
    except Exception as error:
        telemetry.pop("snowflake_credits_24h", None)
        errors.append(
            {"source": "snowflake_credits", "error_type": type(error).__name__}
        )

    return telemetry


def _latest_jsonl_at(
    s3_client: Any,
    object_keys: Sequence[str] | None = None,
    *,
    bucket: str,
    prefix: str,
) -> datetime:
    latest: datetime | None = None
    if object_keys is not None:
        # The caller validates the bounded key set before any I/O. Never let
        # another run refresh this run's proof or require a broader ListBucket.
        for key in object_keys:
            response = s3_client.head_object(Bucket=bucket, Key=key)
            modified = response.get("LastModified")
            if not isinstance(modified, datetime) or modified.tzinfo is None:
                raise ValueError("s3_last_object_at is invalid")
            if latest is None or modified > latest:
                latest = modified
        if latest is None:
            raise LookupError("s3 raw JSONL object is unavailable")
        return latest
    continuation_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        request: dict[str, object] = {
            "Bucket": bucket,
            "Prefix": prefix,
        }
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token
        response = s3_client.list_objects_v2(**request)
        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise ValueError("s3 object listing is invalid")
        for item in contents:
            if not isinstance(item, dict):
                raise ValueError("s3 object listing is invalid")
            key = item.get("Key")
            if not isinstance(key, str) or not key.endswith(".jsonl"):
                continue
            last_modified = item.get("LastModified")
            if not isinstance(last_modified, datetime) or last_modified.tzinfo is None:
                raise ValueError("s3_last_object_at is invalid")
            if latest is None or last_modified > latest:
                latest = last_modified
        token = response.get("NextContinuationToken")
        if token is None:
            break
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise ValueError("s3 continuation token is invalid")
        seen_tokens.add(token)
        continuation_token = token
    if latest is None:
        raise LookupError("s3 raw JSONL object is unavailable")
    return latest


def _non_negative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _integer(value: float, name: str) -> int:
    if not value.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(value)
