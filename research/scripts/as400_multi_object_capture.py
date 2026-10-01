#!/usr/bin/env python3
"""One IBM i DISPLAY_JOURNAL retrieve for ADDRS1+CUSTOM1, then two S3 prefixes."""

from __future__ import annotations

import json
import os
import resource
import sys

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent.contract import JournalPosition
from quadringent.site_config import current as current_site
from quadringent_research.multi_object_capture import (
    ac2_claim_from_groups,
    gate_multi_tables,
    group_events,
    parse_multi_stdout,
    parse_tables,
    publish_table,
    row_to_event,
    run_java,
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def main() -> int:
    tables = gate_multi_tables(parse_tables(_required("AS400_MULTI_TABLES")))
    receiver = _required("AS400_BOOTSTRAP_RECEIVER")
    start_sequence = int(_required("AS400_BOOTSTRAP_SEQUENCE"))
    batch = int(os.environ.get("AS400_BATCH_ENTRIES", "5000"))
    high_watermark = JournalPosition(receiver, start_sequence + batch - 1)
    prefix_root = os.environ.get("AS400_DUAL_PREFIX_ROOT") or f"{current_site().raw_prefix_root}/cntr"
    run_tag = os.environ.get("AS400_DUAL_RUN_TAG", "p14-20260826")
    java = os.environ.get("AS400_JAVA", "java")
    classpath = _required("AS400_JAVA_CLASSPATH")
    env = os.environ.copy()
    env["AS400_MULTI_TABLES"] = ",".join(tables)
    print(
        json.dumps(
            {
                "event": "multi_start",
                "tables": tables,
                "receiver": receiver,
                "start_sequence": start_sequence,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    proc = run_java(java=java, classpath=classpath, env=env)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr[:2000])
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")
    parsed = parse_multi_stdout("\n".join([proc.stdout or "", proc.stderr or ""]))
    events = []
    for row in parsed["rows"]:
        event = row_to_event(
            row,
            journal=_required("AS400_JOURNAL_NAME"),
            library=_required("ISERIES_SCHEMA"),
            receiver=receiver,
        )
        if event is not None:
            events.append(event)
    grouped = group_events(events, tables)
    landed: list[dict[str, object]] = []
    bucket = _required("AS400_RAW_BUCKET")
    checkpoint_table = _required("AS400_CHECKPOINT_TABLE")
    for table in tables:
        table_events = grouped[table]
        summary = parsed["summaries"].get(table)
        published = 0
        payload_key = None
        if table_events:
            prefix = f"{prefix_root}/{table.lower()}-sql-{run_tag}"
            result = publish_table(
                table_events,
                bucket=bucket,
                prefix=prefix,
                checkpoint_table=checkpoint_table,
                stream_key=prefix,
                high_watermark=high_watermark,
            )
            published = int(result.get("published") or 0)
            payload_key = result.get("payload_key")
        landed.append(
            {
                "table": table,
                "events_published": published,
                "retrieve_summary": summary,
                "payload_key": payload_key,
                "last_error": "SqlWindowTimeout" if parsed["timeout"] else None,
            }
        )
        print(
            json.dumps(
                {
                    "event": "capture_poll",
                    "status": "published" if published else "error",
                    "event_count": published,
                    "table": table,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    ac2 = ac2_claim_from_groups(grouped, parsed["summaries"])
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    self_usage = resource.getrusage(resource.RUSAGE_SELF)
    print(
        json.dumps(
            {
                "event": "dual_done",
                "ac2_claim": ac2,
                "legs": landed,
                "mode": "one_retrieve_multi_object",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                "event": "capture_finished",
                "polls": 1,
                "cpu_user_s": round(usage.ru_utime + self_usage.ru_utime, 6),
                "cpu_sys_s": round(usage.ru_stime + self_usage.ru_stime, 6),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if proc.returncode == 0 else proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
