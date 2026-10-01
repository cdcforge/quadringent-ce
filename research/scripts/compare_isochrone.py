#!/usr/bin/env python3
"""Diff R&D raw JSONL keys against Popsink Debezium-like records.

Reads only technical keys. No business payloads in stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent_research.isochrone import (  # noqa: E402
    compare_key_sets,
    filter_keys_to_window,
    key_from_popsink_record,
    key_from_rd_record,
)


def _load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        item = json.loads(stripped)
        if isinstance(item, dict):
            records.append(item)
    return records


def _load_json_or_jsonl(path: Path) -> list[dict]:
    text = path.read_text().strip()
    if not text:
        return []
    if text.startswith("["):
        payload = json.loads(text)
        return [item for item in payload if isinstance(item, dict)]
    return _load_jsonl(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rd-jsonl", required=True)
    parser.add_argument("--popsink-json")
    parser.add_argument("--receiver")
    parser.add_argument("--start-sequence", type=int)
    parser.add_argument("--end-sequence", type=int)
    args = parser.parse_args()

    rd_keys = {key_from_rd_record(item) for item in _load_jsonl(Path(args.rd_jsonl))}
    popsink_keys: set[str] = set()
    if args.popsink_json:
        popsink_keys = {
            key_from_popsink_record(item) for item in _load_json_or_jsonl(Path(args.popsink_json))
        }
    if args.receiver is not None:
        if args.start_sequence is None or args.end_sequence is None:
            parser.error("window filter requires --start-sequence and --end-sequence")
        rd_keys = filter_keys_to_window(
            rd_keys,
            receiver=args.receiver,
            start_sequence=args.start_sequence,
            end_sequence=args.end_sequence,
        )
        popsink_keys = filter_keys_to_window(
            popsink_keys,
            receiver=args.receiver,
            start_sequence=args.start_sequence,
            end_sequence=args.end_sequence,
        )
    matrix = compare_key_sets(rd_keys, popsink_keys)
    json.dump(matrix.to_record(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
