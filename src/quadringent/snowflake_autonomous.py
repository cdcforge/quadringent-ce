"""Provision the Snowflake-managed destination without runtime credentials."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .destination_proof import attach_snowflake_proof
from .destination_sync import sale_stage_relative_key
from .site_config import SiteConfig
from .snowflake_loader import SnowflakeAutonomousLoadPlan
from .site_config import current as _current_site


class DestinationLoadPending(ValueError):
    """Only a still-partial, non-duplicated file load can be retried."""


def autonomous_plan(site: SiteConfig) -> SnowflakeAutonomousLoadPlan:
    """The proof-lane load plan, derived only from the declared site."""

    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    return SnowflakeAutonomousLoadPlan(
        scope=site.snowflake_scope,
        stage=site.proof_stage,
        raw_table=site.proof_raw_table,
        canonical_table=site.proof_canonical_table,
        pipe=site.proof_pipe,
        warehouse=site.warehouse_name,
        manage_warehouse=site.manages_warehouse,
    )


def autonomous_proof_s3_target(uri: str, *, expected: str) -> tuple[str, str]:
    """Resolve the one versioned proof key the cockpit is allowed to consume.

    ``expected`` is the site-declared proof URI — the comparison refuses any
    target outside it.
    """

    if not isinstance(expected, str) or not expected:
        raise ValueError("the declared proof URI is required")
    if uri != expected:
        raise ValueError("autonomous proof S3 target is outside the declared key")
    parsed = urlparse(uri)
    return parsed.netloc, parsed.path.removeprefix("/")


def publish_autonomous_proof(
    client: Any, uri: str, payload: bytes, *,
    expected: str,
    expected_etag: str | None = None, create_only: bool = False,
) -> None:
    """Publish one reconciled document to the versioned no-cache cockpit key."""

    if not isinstance(payload, bytes) or not payload:
        raise ValueError("autonomous proof payload must be non-empty bytes")
    bucket, key = autonomous_proof_s3_target(uri, expected=expected)
    if type(create_only) is not bool or (expected_etag is None and not create_only):
        raise ValueError("proof publication requires an explicit concurrency condition")
    if expected_etag is not None and (not isinstance(expected_etag, str) or not expected_etag.strip() or create_only):
        raise ValueError("invalid conditional proof publication")
    conditions = {}
    if expected_etag is not None:
        conditions["IfMatch"] = expected_etag
    elif create_only:
        conditions["IfNoneMatch"] = "*"
    client.put_object(Bucket=bucket, Key=key, Body=payload,
                      ContentType="application/json", CacheControl="no-store", **conditions)


def provision_autonomous_destination(
    cursor: Any, plan: SnowflakeAutonomousLoadPlan, *, site: SiteConfig
) -> dict[str, object]:
    """Create the declared load objects and return the SQS channel to wire in S3."""

    if (
        not isinstance(site, SiteConfig)
        or plan.scope != site.snowflake_scope
        or plan.warehouse != site.warehouse_name
        or plan.manage_warehouse != site.manages_warehouse
    ):
        raise ValueError("plan must target the declared site destination")

    legacy_canonical = _legacy_dynamic_canonical_exists(cursor, plan)
    dropped_legacy = False
    statements = plan.statements()
    prerequisite_count = 3 if plan.manage_warehouse else 2
    try:
        for statement in statements[:prerequisite_count]:
            _execute_destination_statement(cursor, statement)
        if legacy_canonical:
            cursor.execute(plan.drop_legacy_canonical_statement())
            dropped_legacy = True
        for statement in statements[prerequisite_count:]:
            _execute_destination_statement(cursor, statement)
        cursor.execute(
            f"SHOW PIPES LIKE '{plan.pipe}' IN SCHEMA "
            f'"{plan.database}"."{plan.schema}"'
        )
        rows = cursor.fetchall()
        columns = _cursor_columns(cursor)
        if len(rows) != 1:
            raise RuntimeError("expected exactly one autonomous Snowpipe")
        pipe = dict(zip(columns, rows[0]))
        channel = pipe.get("notification_channel")
        if not isinstance(channel, str) or not channel.startswith("arn:aws:sqs:"):
            raise RuntimeError("Snowpipe notification channel is unavailable")
    except Exception:
        rollback_errors: list[Exception] = []
        if dropped_legacy:
            try:
                cursor.execute(plan.drop_canonical_view_statement())
                for statement in plan.restore_legacy_canonical_statements():
                    cursor.execute(statement)
            except Exception as error:
                rollback_errors.append(error)
        for statement in plan.pause_statements():
            try:
                _execute_destination_statement(cursor, statement)
            except Exception as error:
                rollback_errors.append(error)
        if rollback_errors:
            raise RuntimeError(
                "autonomous destination failed and safe rollback was incomplete"
            ) from rollback_errors[0]
        raise
    return {
        "status": "READY_FOR_S3_NOTIFICATION",
        "environment": site.environment,
        "pipe": f"{plan.database}.{plan.schema}.{plan.pipe}",
        "raw_table": f"{plan.database}.{plan.schema}.{plan.raw_table}",
        "canonical_table": f"{plan.database}.{plan.schema}.{plan.canonical_table}",
        "notification_channel": channel,
    }


def _legacy_dynamic_canonical_exists(
    cursor: Any, plan: SnowflakeAutonomousLoadPlan
) -> bool:
    """Identify only the exact legacy canonical dynamic table in DEV."""

    cursor.execute(
        "SHOW DYNAMIC TABLES IN SCHEMA "
        f'"{plan.database}"."{plan.schema}"'
    )
    rows = cursor.fetchall()
    columns = _cursor_columns(cursor)
    if "name" not in columns:
        raise RuntimeError("dynamic table inventory does not expose object names")
    name_index = columns.index("name")
    matches = [row for row in rows if str(row[name_index]) == plan.canonical_table]
    if len(matches) > 1:
        raise RuntimeError("multiple exact legacy canonical objects found")
    return len(matches) == 1


def pause_autonomous_destination(
    cursor: Any, plan: SnowflakeAutonomousLoadPlan, *, site: SiteConfig
) -> dict[str, object]:
    """Pause ingestion and compute without deleting captured or loaded data."""

    if (
        not isinstance(site, SiteConfig)
        or plan.scope != site.snowflake_scope
        or plan.warehouse != site.warehouse_name
        or plan.manage_warehouse != site.manages_warehouse
    ):
        raise ValueError("plan must target the declared site destination")
    for statement in plan.pause_statements():
        _execute_destination_statement(cursor, statement)
    return {
        "status": "PAUSED",
        "environment": site.environment,
        "pipe": f"{plan.database}.{plan.schema}.{plan.pipe}",
        "canonical_table": f"{plan.database}.{plan.schema}.{plan.canonical_table}",
        "warehouse": plan.warehouse,
    }


def _execute_destination_statement(cursor: Any, statement: str) -> None:
    """Execute DDL while accepting Snowflake's already-suspended state only."""

    try:
        cursor.execute(statement)
    except Exception as error:
        warehouse_suspend = statement.startswith("ALTER WAREHOUSE IF EXISTS ") and (
            statement.endswith(" SUSPEND")
        )
        if warehouse_suspend and getattr(error, "errno", None) == 90064:
            return
        raise


def verify_autonomous_destination(
    cursor: Any,
    capture_document: Mapping[str, object],
    plan: SnowflakeAutonomousLoadPlan,
    *,
    object_keys: Sequence[str],
    run_tag: str,
    observed_at: datetime,
    site: SiteConfig,
    expected_event_ids_sha256: str | None = None,
) -> dict[str, object]:
    """Reconcile one capture against managed tables using read-only queries."""

    counters = capture_document.get('counters')
    wrapper = counters.get('events_published') if isinstance(counters, Mapping) else None
    expected_rows = wrapper.get('value') if isinstance(wrapper, Mapping) else None
    metrics = reconcile_autonomous_files(cursor, plan, object_keys=object_keys,
        expected_rows=expected_rows, expected_event_ids_sha256=expected_event_ids_sha256,
        site=site)
    combined = attach_snowflake_proof(capture_document, metrics, run_tag=run_tag, observed_at=observed_at, site=site)
    if expected_event_ids_sha256 is not None:
        combined['stored_event_identity_proof'] = {
            'state': 'matched', 'basis': 'verified_s3_batches',
            'event_count': expected_rows, 'event_ids_sha256': expected_event_ids_sha256,
            'observed_at': observed_at.isoformat(),
        }
    return combined


def reconcile_autonomous_files(
    cursor: Any, plan: SnowflakeAutonomousLoadPlan, *, object_keys: Sequence[str],
    expected_rows: int, site: SiteConfig, expected_event_ids_sha256: str | None = None,
) -> dict[str, object]:
    """Read-only reconciliation of an exact population, independent of capture state."""
    if (
        not isinstance(site, SiteConfig)
        or plan.scope != site.snowflake_scope
        or plan.warehouse != site.warehouse_name
        or plan.manage_warehouse != site.manages_warehouse
    ):
        raise ValueError("plan must target the declared site destination")
    if type(expected_rows) is not int or expected_rows <= 0:
        raise ValueError('expected event count must be a positive integer')

    if expected_event_ids_sha256 is not None and not re.fullmatch(r'[a-f0-9]{64}', expected_event_ids_sha256):
        raise ValueError('Invalid expected event identity digest')

    relative_keys = tuple(
        sale_stage_relative_key(
            key,
            required_prefix=site.stream_prefix,
            forbidden_fragments=site.forbidden_fragments,
        )
        for key in object_keys
    )
    if not relative_keys or len(relative_keys) > 1000:
        raise ValueError("object_keys must contain between 1 and 1000 proof files")
    if len(set(relative_keys)) != len(relative_keys):
        raise ValueError("object_keys must not contain duplicates")

    pipe_name = f'"{plan.database}"."{plan.schema}"."{plan.pipe}"'
    cursor.execute(f"SELECT SYSTEM$PIPE_STATUS('{pipe_name}')")
    status_row = cursor.fetchone()
    if not status_row or not isinstance(status_row[0], str):
        raise RuntimeError("Snowpipe status is unavailable")
    try:
        pipe_status = json.loads(status_row[0])
    except json.JSONDecodeError as error:
        raise RuntimeError("Snowpipe status is invalid") from error
    if pipe_status.get("executionState") != "RUNNING":
        raise RuntimeError("Snowpipe is not running")

    raw_table = f'"{plan.database}"."{plan.schema}"."{plan.raw_table}"'
    canonical_table = (
        f'"{plan.database}"."{plan.schema}"."{plan.canonical_table}"'
    )
    predicate = " OR ".join(
        f"RIGHT(SOURCE_FILE, LENGTH({_sql_string(key)})) = {_sql_string(key)}"
        for key in relative_keys
    )
    cursor.execute(
        "SELECT COUNT(*), COUNT(DISTINCT PAYLOAD:event_id::VARCHAR), "
        f"COUNT(DISTINCT SOURCE_FILE) FROM {raw_table} WHERE ({predicate})"
    )
    raw_row = cursor.fetchone()
    if not raw_row or len(raw_row) < 3:
        raise RuntimeError("Snowpipe raw reconciliation is unavailable")
    raw_rows, distinct_events, source_files = (int(value) for value in raw_row[:3])
    cursor.execute(
        f"SELECT COUNT(*) FROM {canonical_table} WHERE EVENT_ID IN ("
        "SELECT DISTINCT PAYLOAD:event_id::VARCHAR "
        f"FROM {raw_table} WHERE ({predicate}))"
    )
    canonical_row = cursor.fetchone()
    if not canonical_row:
        raise RuntimeError("canonical reconciliation is unavailable")
    canonical_rows = int(canonical_row[0])
    if (type(expected_rows) is int and expected_rows > 0
            and 0 <= source_files < len(relative_keys)
            and 0 <= canonical_rows <= raw_rows == distinct_events < expected_rows):
        raise DestinationLoadPending('Snowflake replay is not reconciled: file load pending')
    metrics = {
        "status": "PASS"
        if source_files == len(relative_keys)
        and raw_rows > 0
        and raw_rows == distinct_events == canonical_rows
        else "FAIL",
        "database": plan.database,
        "schema": plan.schema,
        "stage": plan.stage,
        "raw_table": plan.raw_table,
        "canonical_table": plan.canonical_table,
        "raw_rows_after_second": raw_rows,
        "distinct_event_ids_after_second": distinct_events,
        "canonical_rows_after_second": canonical_rows,
    }
    if metrics['status'] != 'PASS' or raw_rows != expected_rows:
        raise ValueError('Snowflake replay is not reconciled')
    if expected_event_ids_sha256 is not None:
        raw_digest = _identity_digest(cursor,
            'SELECT DISTINCT PAYLOAD:event_id::VARCHAR '
            f'FROM {raw_table} WHERE ({predicate}) ORDER BY 1', raw_rows)
        canonical_digest = _identity_digest(cursor,
            f'SELECT EVENT_ID FROM {canonical_table} WHERE EVENT_ID IN ('
            'SELECT DISTINCT PAYLOAD:event_id::VARCHAR '
            f'FROM {raw_table} WHERE ({predicate})) ORDER BY EVENT_ID', canonical_rows)
        if raw_digest != expected_event_ids_sha256 or canonical_digest != expected_event_ids_sha256:
            raise ValueError('Stored event identities do not reconcile with Snowflake')
    return metrics


def _identity_digest(cursor: Any, query: str, expected_rows: int) -> str:
    """Hash ordered identifiers incrementally; never log identifiers or rows."""
    cursor.execute(query)
    digest = hashlib.sha256()
    previous: str | None = None
    count = 0
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        for row in rows:
            count += 1
            value = row[0]
            if count > expected_rows or not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError('Invalid or excessive event identities')
            if previous is not None:
                if value <= previous:
                    raise ValueError('Duplicate or unordered event identities')
                digest.update(b'\n')
            digest.update(value.encode())
            previous = value
    if count != expected_rows:
        raise ValueError('Event identities changed during verification')
    return digest.hexdigest()


def _cursor_columns(cursor: Any) -> tuple[str, ...]:
    columns: list[str] = []
    for description in cursor.description or ():
        name = getattr(description, "name", None)
        if name is None:
            name = description[0]
        columns.append(str(name).lower())
    return tuple(columns)


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def __getattr__(name: str):
    """Compatibilité paresseuse : les valeurs du site se résolvent à l'appel."""

    if name == "AUTONOMOUS_PROOF_S3_URI":
        return _current_site().autonomous_proof_s3_uri
    if name == "default_autonomous_plan":
        return lambda: autonomous_plan(_current_site())
    raise AttributeError(name)
