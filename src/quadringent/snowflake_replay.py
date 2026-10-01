from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Sequence

from .site_config import SnowflakeScope
from .snowflake_loader import SnowflakeCanonicalLoadPlan, _stage_path


@dataclass(frozen=True)
class SnowflakeReplayConfig:
    """Bounded configuration for one external-stage replay.

    ``scope`` carries the site-declared destination: a replay configuration
    cannot express a database or schema outside it.
    """

    scope: SnowflakeScope
    stage: str
    raw_table: str
    canonical_table: str
    object_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        # SnowflakeCanonicalLoadPlan owns the identifier validation so this
        # replay cannot diverge from the SQL generator used by the loader.
        SnowflakeCanonicalLoadPlan(
            scope=self.scope,
            raw_table=self.raw_table,
            canonical_table=self.canonical_table,
            stage=self.stage,
        )
        _stage_path(self.object_key)

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    @property
    def plan(self) -> SnowflakeCanonicalLoadPlan:
        return SnowflakeCanonicalLoadPlan(
            scope=self.scope,
            raw_table=self.raw_table,
            canonical_table=self.canonical_table,
            stage=self.stage,
        )

    @property
    def stage_reference(self) -> str:
        return "@" + self.plan.qualified_stage

    @property
    def object_reference(self) -> str:
        return f"{self.stage_reference}/{self.object_key}"


def summarize_copy_result(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> dict[str, object]:
    """Keep only non-sensitive COPY counters and status.

    Snowflake returns a full file name for a normal COPY and only a ``status``
    column when a previously loaded file is skipped. The file name is never
    copied into the returned metrics.
    """

    if not rows:
        return {
            "result_rows": 0,
            "status": None,
            "rows_parsed": 0,
            "rows_loaded": 0,
            "errors_seen": 0,
        }
    values = [
        {str(column).upper(): value for column, value in zip(columns, row)}
        for row in rows
    ]
    statuses = {
        str(row["STATUS"])
        for row in values
        if row.get("STATUS") is not None
    }
    return {
        "result_rows": len(rows),
        "status": next(iter(statuses)) if len(statuses) == 1 else ("MIXED" if statuses else None),
        "rows_parsed": sum(
            _safe_int(row.get("ROWS_PARSED"), default=0) for row in values
        ),
        "rows_loaded": sum(
            _safe_int(row.get("ROWS_LOADED"), default=0) for row in values
        ),
        "errors_seen": sum(
            _safe_int(row.get("ERRORS_SEEN"), default=0) for row in values
        ),
    }


def summarize_merge_result(rows: Sequence[Sequence[Any]]) -> dict[str, object]:
    """Reduce Snowflake's MERGE result to the inserted-row counter."""

    inserted = 0
    if rows and rows[0]:
        inserted = _safe_int(rows[0][0], default=0)
    return {"rows_inserted": inserted}


def execute_external_replay(cursor: Any, config: SnowflakeReplayConfig) -> dict[str, object]:
    """Execute one external-stage load and an identical idempotence replay.

    The caller controls authentication and the target schema. This function
    only uses the exact stage object supplied by ``config`` and returns safe
    counters; it never prints or returns row payloads.
    """

    started = time.perf_counter()
    cursor.execute(f"LIST {config.stage_reference}")
    listed = cursor.fetchall()
    list_ms = _elapsed_ms(started)
    matching = [row for row in listed if _listed_object_matches(row, config.object_key)]
    if len(matching) != 1:
        raise RuntimeError(f"expected exactly one stage object, found {len(matching)}")
    plan = config.plan
    create_raw, copy_into, create_canonical, merge = plan.statements_for(config.object_key)
    object_key_literal = config.object_key.replace("'", "''")
    merge = merge.replace(
        f"WHERE SOURCE_FILE = '{config.object_key}'",
        f"WHERE RIGHT(SOURCE_FILE, LENGTH('{object_key_literal}')) = '{object_key_literal}'",
    )
    return _execute_replay_statements(
        cursor,
        config,
        statements=(create_raw, copy_into, create_canonical, merge),
        metadata={
            "object_key": config.object_key,
            "object_count": 1,
            "file_bytes": _safe_int(matching[0][1], default=0),
            "list_ms": list_ms,
        },
    )


def execute_external_stage_replay(
    cursor: Any, config: SnowflakeReplayConfig
) -> dict[str, object]:
    """Replay every JSONL payload from the bounded DEV stage root."""

    started = time.perf_counter()
    cursor.execute(f"LIST {config.stage_reference}")
    listed = cursor.fetchall()
    list_ms = _elapsed_ms(started)
    payloads = [row for row in listed if row and str(row[0]).lower().endswith(".jsonl")]
    if not payloads:
        raise RuntimeError("expected at least one JSONL stage object, found 0")
    return _execute_replay_statements(
        cursor,
        config,
        statements=config.plan.statements_for_all_jsonl(),
        metadata={
            "object_key": None,
            "object_count": len(payloads),
            "file_bytes": sum(_safe_int(row[1], default=0) for row in payloads),
            "list_ms": list_ms,
        },
    )


def execute_external_files_replay(
    cursor: Any, config: SnowflakeReplayConfig, object_keys: Sequence[str]
) -> dict[str, object]:
    """Replay an explicit JSONL list already bounded to the DEV stage root."""

    started = time.perf_counter()
    cursor.execute(f"LIST {config.stage_reference}")
    listed = cursor.fetchall()
    list_ms = _elapsed_ms(started)
    matching_rows: list[Sequence[Any]] = []
    for key in object_keys:
        matching = [row for row in listed if _listed_object_matches(row, key)]
        if len(matching) != 1:
            raise RuntimeError(
                f"expected exactly one stage object, found {len(matching)}"
            )
        matching_rows.append(matching[0])
    return _execute_replay_statements(
        cursor,
        config,
        statements=config.plan.statements_for_object_keys(object_keys),
        metadata={
            "object_key": None,
            "object_count": len(object_keys),
            "file_bytes": sum(_safe_int(row[1], default=0) for row in matching_rows),
            "list_ms": list_ms,
        },
    )


def _execute_replay_statements(
    cursor: Any,
    config: SnowflakeReplayConfig,
    *,
    statements: tuple[str, str, str, str],
    metadata: dict[str, object],
) -> dict[str, object]:
    plan = config.plan
    create_raw, copy_into, create_canonical, merge = statements
    metrics: dict[str, object] = {
        "database": config.database,
        "schema": config.schema,
        "stage": config.stage,
        "raw_table": config.raw_table,
        "canonical_table": config.canonical_table,
        "errors": [],
        **metadata,
    }

    started = time.perf_counter()
    cursor.execute(create_raw)
    cursor.execute(create_canonical)
    metrics["create_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    cursor.execute(copy_into)
    first_copy_rows = cursor.fetchall()
    metrics["copy_first_ms"] = _elapsed_ms(started)
    metrics["copy_first"] = summarize_copy_result(_cursor_columns(cursor), first_copy_rows)
    metrics["copy_first_query_id"] = getattr(cursor, "sfqid", None)

    started = time.perf_counter()
    cursor.execute(merge)
    first_merge_rows = cursor.fetchall()
    metrics["merge_first_ms"] = _elapsed_ms(started)
    metrics["merge_first"] = summarize_merge_result(first_merge_rows)
    metrics["merge_first_query_id"] = getattr(cursor, "sfqid", None)

    raw_first, event_ids_first = _counts(cursor, plan.qualified_table, "PAYLOAD:event_id::VARCHAR")
    canonical_first = _count(cursor, plan.qualified_canonical_table)
    metrics["raw_rows_after_first"] = raw_first
    metrics["distinct_event_ids_after_first"] = event_ids_first
    metrics["canonical_rows_after_first"] = canonical_first

    started = time.perf_counter()
    cursor.execute(copy_into)
    second_copy_rows = cursor.fetchall()
    metrics["copy_second_ms"] = _elapsed_ms(started)
    metrics["copy_second"] = summarize_copy_result(_cursor_columns(cursor), second_copy_rows)
    metrics["copy_second_query_id"] = getattr(cursor, "sfqid", None)

    started = time.perf_counter()
    cursor.execute(merge)
    second_merge_rows = cursor.fetchall()
    metrics["merge_second_ms"] = _elapsed_ms(started)
    second_merge = summarize_merge_result(second_merge_rows)
    metrics["merge_second"] = second_merge
    metrics["merge_second_query_id"] = getattr(cursor, "sfqid", None)

    raw_second, event_ids_second = _counts(cursor, plan.qualified_table, "PAYLOAD:event_id::VARCHAR")
    canonical_second = _count(cursor, plan.qualified_canonical_table)
    metrics["raw_rows_after_second"] = raw_second
    metrics["distinct_event_ids_after_second"] = event_ids_second
    metrics["canonical_rows_after_second"] = canonical_second
    metrics["status"] = (
        "PASS"
        if raw_first > 0
        and raw_second == raw_first
        and event_ids_first == event_ids_second
        and canonical_first == canonical_second
        and canonical_first == event_ids_first
        and int(second_merge["rows_inserted"]) == 0
        else "FAIL"
    )
    return metrics


def _counts(cursor: Any, table: str, expression: str) -> tuple[int, int]:
    cursor.execute(f"SELECT COUNT(*), COUNT(DISTINCT {expression}) FROM {table}")
    row = cursor.fetchone()
    return _safe_int(row[0], default=0), _safe_int(row[1], default=0)


def _count(cursor: Any, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    row = cursor.fetchone()
    return _safe_int(row[0], default=0)


def _cursor_columns(cursor: Any) -> tuple[str, ...]:
    columns = []
    for description in cursor.description or ():
        name = getattr(description, "name", None)
        if name is None:
            name = description[0]
        columns.append(str(name))
    return tuple(columns)


def _listed_object_matches(row: Sequence[Any], object_key: str) -> bool:
    if not row:
        return False
    value = str(row[0])
    return value == object_key or value.endswith("/" + object_key)


def _safe_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
