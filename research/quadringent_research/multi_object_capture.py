"""One DISPLAY_JOURNAL retrieve covering >=2 whitelist tables."""

from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
from typing import Any, Sequence

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import RawFirstCaptureCoordinator, S3ObjectStore
from quadringent.checkpoint import DynamoDbCheckpointStore
from quadringent.raw import RawBatchWriter


REQUIRED_TABLES = ("ADDRS1", "CUSTOM1")
ROW_OPERATIONS = {
    "PT": ("c", "after"),
    "PX": ("c", "after"),
    "UB": ("u_before", "before"),
    "UP": ("u_after", "after"),
    "DL": ("d", "before"),
    "DR": ("d", "before"),
}


def parse_tables(raw: str) -> list[str]:
    tables = []
    for item in raw.split(","):
        table = item.strip().upper()
        if table and table not in tables:
            tables.append(table)
    if len(tables) < 2:
        raise ValueError("AS400_MULTI_TABLES needs >=2 distinct tables")
    return tables


def gate_multi_tables(tables: Sequence[str]) -> list[str]:
    selected = [str(item).strip().upper() for item in tables if str(item).strip()]
    if tuple(selected) != REQUIRED_TABLES:
        raise ValueError("ac2 multi pair must be ADDRS1 then CUSTOM1")
    return selected


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def parse_multi_stdout(stdout: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    total = None
    timeout = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("event")
        if name == "window_timeout" or event.get("error_type") == "SqlWindowTimeout":
            timeout = True
        if name == "window_error" and event.get("error_type"):
            timeout = timeout or str(event.get("error_type")) == "SqlWindowTimeout"
        if name == "sql_row":
            rows.append(event)
        if name == "retrieve_summary":
            decoded = _as_int(event.get("decoded"))
            elapsed_ms = _as_int(event.get("elapsed_ms"))
            if decoded is None or elapsed_ms is None:
                continue
            payload = {
                "decoded": decoded,
                "elapsed_ms": elapsed_ms,
                "events_per_sec": _as_float(event.get("events_per_sec")),
            }
            table = str(event.get("table") or "").upper()
            if table:
                summaries[table] = payload
            else:
                total = payload
    return {
        "rows": rows,
        "summaries": summaries,
        "total": total,
        "timeout": timeout,
    }


def row_to_event(
    row: dict[str, Any],
    *,
    journal: str,
    library: str,
    receiver: str,
) -> ChangeEvent | None:
    table = str(row.get("table") or "").upper()
    type_code = str(row.get("type") or "").upper()
    sequence = _as_int(row.get("sequence"))
    if not table or sequence is None or type_code not in ROW_OPERATIONS:
        return None
    operation, image_side = ROW_OPERATIONS[type_code]
    image = {
        "journal_entry_type": type_code,
        "hex_sha256": str(row.get("hex_sha256") or ""),
    }
    timestamp = str(row.get("timestamp") or "1970-01-01T00:00:00Z")
    return ChangeEvent(
        source_system="ibmi",
        journal=journal,
        library=library,
        table=table,
        operation=operation,
        position=JournalPosition(receiver, sequence),
        commit_timestamp=timestamp,
        schema_version="as400-raw-v1",
        before=image if image_side == "before" else None,
        after=image if image_side == "after" else None,
    )


def group_events(events: Sequence[ChangeEvent], tables: Sequence[str]) -> dict[str, list[ChangeEvent]]:
    grouped = {table: [] for table in tables}
    for event in events:
        if event.table in grouped:
            grouped[event.table].append(event)
    return grouped


def ac2_claim_from_groups(
    grouped: dict[str, list[ChangeEvent]],
    summaries: dict[str, dict[str, Any]],
) -> bool:
    ok = 0
    for table, events in grouped.items():
        summary = summaries.get(table) or {}
        if (
            len(events) > 0
            and isinstance(summary.get("decoded"), int)
            and isinstance(summary.get("elapsed_ms"), int)
        ):
            ok += 1
    return ok >= 2


def publish_table(
    events: Sequence[ChangeEvent],
    *,
    bucket: str,
    prefix: str,
    checkpoint_table: str,
    stream_key: str,
    high_watermark: JournalPosition,
) -> dict[str, Any]:
    if not events:
        return {"table": events, "published": 0}
    store = S3ObjectStore(bucket, prefix)
    checkpoint = DynamoDbCheckpointStore(checkpoint_table, stream_key)
    coordinator = RawFirstCaptureCoordinator(store, checkpoint)
    result = coordinator.capture(list(events), high_watermark=high_watermark)
    return {
        "published": len(events),
        "payload_key": result.publish.payload_key,
        "payload_bytes": result.payload_bytes,
    }


def run_java(*, java: str, classpath: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [java, "-Dorg.slf4j.simpleLogger.defaultLogLevel=error", "-cp", classpath, "io.quadringent.as400.MultiObjectDisplayJournal"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
