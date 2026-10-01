#!/usr/bin/env python3
"""Evaluate one Quadringent proof against explicit DEV SLO thresholds."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC in sys.path:
    sys.path.remove(SRC)
sys.path.insert(0, SRC)

from quadringent.site_config import current as current_site
from quadringent.slo import SloPolicy, evaluate_slo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--telemetry", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--now", default="")
    args = parser.parse_args(argv)

    try:
        proof = _object(args.proof, "proof")
        telemetry = _object(args.telemetry, "telemetry")
        policy = SloPolicy.from_mapping(_object(args.policy, "policy"))
        now = _timestamp(args.now) if args.now else datetime.now(UTC)
        report = evaluate_slo(proof, telemetry, policy, now=now, site=current_site())
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "invalid",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    print(json.dumps(report, sort_keys=True))
    return {"pass": 0, "breach": 1, "unobserved": 2}[str(report["status"])]


def _object(path: str, label: str) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must be timezone-aware")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
