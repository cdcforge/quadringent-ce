#!/usr/bin/env python3
"""Convert safe continuous-capture JSON logs to isochronous samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent_research.benchmark import samples_from_continuous_log


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="JSONL capture log or JSON array")
    parser.add_argument("--solution", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument(
        "--position-fingerprint",
        required=True,
        help="sha256 fingerprint of the exact receiver/sequence set",
    )
    parser.add_argument(
        "--event-fingerprint",
        required=True,
        help="sha256 fingerprint of exact event identities and operations",
    )
    parser.add_argument("--receiver")
    parser.add_argument("--start-sequence", type=int)
    parser.add_argument("--end-sequence", type=int)
    parser.add_argument("--cost-usd", type=float)
    parser.add_argument("--cost-provenance")
    args = parser.parse_args()

    records = _read_records(args.log)
    samples = samples_from_continuous_log(
        records,
        solution=args.solution,
        phase=args.phase,
        table=args.table,
        contract_version=args.contract_version,
        position_fingerprint=args.position_fingerprint,
        event_fingerprint=args.event_fingerprint,
        receiver=args.receiver,
        start_sequence=args.start_sequence,
        end_sequence=args.end_sequence,
        cost_usd=args.cost_usd,
        cost_provenance=args.cost_provenance,
    )
    print(
        json.dumps(
            [sample.__dict__ for sample in samples],
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _read_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("continuous log is empty")
    if text.lstrip().startswith("["):
        records = json.loads(text)
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("continuous log must contain JSON objects")
    return records


if __name__ == "__main__":
    raise SystemExit(main())
