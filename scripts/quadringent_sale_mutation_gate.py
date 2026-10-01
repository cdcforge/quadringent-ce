#!/usr/bin/env python3
"""Evaluate an SALE raw mutation window without exposing business values."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC in sys.path:
    sys.path.remove(SRC)
sys.path.insert(0, SRC)

from quadringent.sale_mutation import (
    MAX_JSONL_BYTES,
    build_sale_contract_events,
    evaluate_sale_mutation_window,
    load_sale_jsonl,
)
from quadringent.site_config import current as current_site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        help="bounded raw JSONL window; omission runs only the synthetic contract fixture",
    )
    args = parser.parse_args(argv)

    try:
        site = current_site()
        if args.input:
            if args.input == "-":
                payload = sys.stdin.buffer.read(MAX_JSONL_BYTES + 1)
            else:
                with Path(args.input).open("rb") as source:
                    payload = source.read(MAX_JSONL_BYTES + 1)
            events = load_sale_jsonl(payload)
            evidence_tier = "provided_raw_window"
        else:
            events = build_sale_contract_events(site=site)
            evidence_tier = "synthetic_contract"
        report = evaluate_sale_mutation_window(events, site=site)
    except Exception as error:
        print(
            json.dumps(
                {"status": "invalid", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    report["evidence_tier"] = evidence_tier
    if evidence_tier == "synthetic_contract":
        report["verdict"] = "CONTRACT_PASS_LIVE_PENDING"
        return_code = 2
    else:
        report["verdict"] = {
            "pass": "MUTATION_WINDOW_PASS",
            "breach": "MUTATION_WINDOW_FAIL",
            "unobserved": "MUTATION_WINDOW_INCOMPLETE",
        }[str(report["status"])]
        return_code = {"pass": 0, "breach": 1, "unobserved": 2}[str(report["status"])]
    print(json.dumps(report, sort_keys=True))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
