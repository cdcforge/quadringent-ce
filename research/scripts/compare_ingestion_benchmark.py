#!/usr/bin/env python3
"""Compare isochronous IBM i ingestion samples without touching credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent_research.benchmark import BenchmarkSample, compare_samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, help="JSON array of safe benchmark samples")
    parser.add_argument(
        "--require-solutions",
        nargs="+",
        metavar="SOLUTION",
        help="require exactly these solution labels (use for the strict comparison)",
    )
    parser.add_argument(
        "--require-cost",
        action="store_true",
        help="require attributed cost and provenance for every sample",
    )
    args = parser.parse_args()
    records = json.loads(args.samples.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("samples file must contain a JSON array")
    samples = [BenchmarkSample.from_record(record) for record in records]
    print(
        json.dumps(
            compare_samples(
                samples,
                required_solutions=args.require_solutions,
                require_cost=args.require_cost,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
