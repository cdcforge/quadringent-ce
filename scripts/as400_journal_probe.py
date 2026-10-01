#!/usr/bin/env python3
"""Run a bounded, read-only IBM i journal probe from an approved runtime.

The password is accepted only on stdin. The output intentionally contains
metadata and hashes/lengths for ENTRY_DATA, never row payloads or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

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
    parser.add_argument("--start-sequence", type=int, required=True)
    parser.add_argument("--end-sequence", type=int, required=True)
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="omit ENTRY_DATA when scanning a bounded window",
    )
    args = parser.parse_args()
    library = args.library or current_site().source_schema

    password = sys.stdin.read().rstrip("\n")
    if not password:
        raise SystemExit("password must be supplied on stdin")
    connection_string = (
        "DRIVER={IBM i Access ODBC Driver 64-bit};"
        f"SYSTEM={args.host};UID={args.user};PWD={password};NAM=1;"
    )
    connection = pyodbc.connect(connection_string, autocommit=True)
    try:
        reader = IbmiJournalReader(connection)
        journaled_object = reader.discover_object(library, args.table)
        info = reader.journal_info(
            journaled_object.journal_library,
            journaled_object.journal_name,
        )
        receivers = reader.latest_receivers(
            journaled_object.journal_library,
            journaled_object.journal_name,
            limit=10,
        )
        entries = reader.read_entries(
            journaled_object.journal_library,
            journaled_object.journal_name,
            receiver_library=args.receiver_library,
            starting=JournalPosition(args.receiver, args.start_sequence),
            ending=JournalPosition(args.receiver, args.end_sequence),
            object_library=library,
            object_name=args.table,
            max_rows=args.max_rows,
            include_entry_data=not args.metadata_only,
        )
        output = {
            "journaled_object": {
                "journal_library": journaled_object.journal_library,
                "journal_name": journaled_object.journal_name,
                "object_library": journaled_object.object_library,
                "object_name": journaled_object.object_name,
                "object_type": journaled_object.object_type,
                "journal_images": journaled_object.journal_images,
            },
            "journal_info": {
                "journal_library": info.journal_library,
                "journal_name": info.journal_name,
                "state": info.state,
                "receiver_count": info.receiver_count,
                "receiver_total_size": info.receiver_total_size,
                "remote_journal_count": info.remote_journal_count,
            },
            "receivers": [
                {
                    "library": receiver.journal_receiver_library,
                    "name": receiver.journal_receiver_name,
                    "status": receiver.status,
                    "attach_timestamp": str(receiver.attach_timestamp)
                    if receiver.attach_timestamp is not None
                    else None,
                    "first_sequence": receiver.first_sequence_number,
                    "last_sequence": receiver.last_sequence_number,
                    "entry_count": receiver.entry_count,
                }
                for receiver in receivers
            ],
            "entries": [_safe_entry(entry) for entry in entries],
        }
        print(json.dumps(output, sort_keys=True, ensure_ascii=False))
    finally:
        connection.close()
    return 0


def _safe_entry(entry: dict[str, object]) -> dict[str, object]:
    payload_hex = entry.get("ENTRY_DATA_HEX")
    payload = entry.get("ENTRY_DATA")
    if payload_hex is not None:
        if isinstance(payload_hex, bytes):
            payload_bytes = bytes.fromhex(payload_hex.decode("ascii"))
        else:
            payload_bytes = bytes.fromhex(str(payload_hex).strip())
        entry_data = {
            "length": len(payload_bytes),
            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }
    elif isinstance(payload, bytes):
        entry_data = {
            "length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    elif payload is None:
        entry_data = None
    else:
        encoded = str(payload).encode("utf-8")
        entry_data = {
            "length": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return {
        "sequence_number": str(entry.get("SEQUENCE_NUMBER")),
        "journal_code": _text(entry.get("JOURNAL_CODE")),
        "journal_entry_type": _text(entry.get("JOURNAL_ENTRY_TYPE")),
        "object": _text(entry.get("OBJECT")),
        "object_type": _text(entry.get("OBJECT_TYPE")),
        "receiver_name": _text(entry.get("RECEIVER_NAME")),
        "receiver_library": _text(entry.get("RECEIVER_LIBRARY")),
        "entry_timestamp": _text(entry.get("ENTRY_TIMESTAMP")),
        "entry_data": entry_data,
    }


def _text(value: object) -> str | None:
    return None if value is None else str(value).strip()


if __name__ == "__main__":
    raise SystemExit(main())
