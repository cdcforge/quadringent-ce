#!/usr/bin/env python3
"""Measure bounded DISPLAY_JOURNAL calls on one approved IBM i receiver."""

from __future__ import annotations

import argparse
import json
import sys
import time

import pyodbc

from quadringent.contract import JournalPosition
from quadringent.ibmi_reader import IbmiJournalReader
from quadringent.site_config import current as current_site


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--library", default=None,
                        help="bibliothèque source ; défaut : QUADRINGENT_SOURCE_SCHEMA du site")
    parser.add_argument("--table", required=True)
    parser.add_argument("--receiver-library", required=True)
    parser.add_argument("--receiver", required=True)
    parser.add_argument("--end-sequence", type=int, required=True)
    parser.add_argument("--window-sizes", nargs="+", type=int, default=[73, 1000, 10000])
    parser.add_argument("--max-rows", type=int, default=100)
    args = parser.parse_args()
    library = args.library or current_site().source_schema

    password = sys.stdin.read().rstrip("\n")
    if not password:
        raise SystemExit("password must be supplied on stdin")
    connection = pyodbc.connect(
        "DRIVER={IBM i Access ODBC Driver 64-bit};"
        f"SYSTEM={args.host};UID={args.user};PWD={password};NAM=1;",
        autocommit=True,
    )
    try:
        reader = IbmiJournalReader(connection)
        journaled_object = reader.discover_object(library, args.table)
        results: list[dict[str, object]] = []
        for window_size in args.window_sizes:
            if window_size < 1:
                raise ValueError("window sizes must be positive")
            start_sequence = args.end_sequence - window_size + 1
            started = time.perf_counter()
            rows = reader.read_entries(
                journaled_object.journal_library,
                journaled_object.journal_name,
                receiver_library=args.receiver_library,
                starting=JournalPosition(args.receiver, start_sequence),
                ending=JournalPosition(args.receiver, args.end_sequence),
                object_library=library,
                object_name=args.table,
                max_rows=args.max_rows,
                include_entry_data=False,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(
                {
                    "table": f"{library}.{args.table}",
                    "receiver": args.receiver,
                    "window_size": window_size,
                    "start_sequence": start_sequence,
                    "end_sequence": args.end_sequence,
                    "rows": len(rows),
                    "elapsed_ms": round(elapsed_ms, 2),
                }
            )
        print(json.dumps(results, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
