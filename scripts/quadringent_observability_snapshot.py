#!/usr/bin/env python3
"""Attach one validated SLO/alert state to a Quadringent autonomous snapshot."""

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

from quadringent.console_snapshot import FileSnapshotSink
from quadringent.observability_snapshot import attach_observability_snapshot
from quadringent.site_config import current as current_site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--alerts", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    try:
        combined = attach_observability_snapshot(
            _object(Path(args.proof), "proof"),
            _object(Path(args.report), "report"),
            _object(Path(args.alerts), "alerts"),
            site=current_site(),
        )
        payload = (
            json.dumps(combined, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        FileSnapshotSink(Path(args.out)).write(payload)
    except Exception as error:
        print(
            json.dumps(
                {"status": "invalid", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    observability = combined["observability"]
    report = observability["slo_report"]
    state = observability["alert_state"]
    active = sum(
        alert["lifecycle_state"] == "firing" for alert in state["alerts"]
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "phase": "observability_snapshot",
                "check_count": len(report["checks"]),
                "active_alert_count": active,
            },
            sort_keys=True,
        )
    )
    return 0


def _object(path: Path, label: str) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
