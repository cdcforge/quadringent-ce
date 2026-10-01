#!/usr/bin/env python3
"""Run two proven DISPLAY_JOURNAL windows in one process (one IBM i reader)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent.site_config import current as current_site
from quadringent_research.dual_sql_capture import (
    capture_script,
    gate_job_legs,
    load_registry,
    run_dual,
    subprocess_leg,
    window_by_id,
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"ERROR: {name} is required")
    return value


def main() -> int:
    registry_path = Path(_required("AS400_PROVEN_WINDOWS"))
    sql_script = Path(os.environ.get("AS400_SQL_CAPTURE_SCRIPT", "/app/as400_sql_journal_capture.py"))
    rj_script = Path(os.environ.get("AS400_RJ_CAPTURE_SCRIPT", "/app/as400_continuous_capture.py"))
    raw_ids = os.environ.get("AS400_DUAL_LEG_IDS", "addrs1_sql_3769,custom1_sql_3769")
    leg_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
    prefix_root = os.environ.get("AS400_DUAL_PREFIX_ROOT") or f"{current_site().raw_prefix_root}/cntr"
    run_tag = os.environ.get("AS400_DUAL_RUN_TAG", "p13-20260826")
    settle_s = float(os.environ.get("AS400_DUAL_SETTLE_S", "20"))
    selected = gate_job_legs(leg_ids, registry_path)
    registry = load_registry(registry_path)
    windows = [window_by_id(registry, window_id) for window_id in selected]

    def run_leg(window: dict) -> dict:
        table = str(window["table"]).lower()
        mode = str(window.get("mode") or "sql")
        prefix = f"{prefix_root}/{table}-{mode}-{run_tag}"
        script = capture_script(window, sql_script=sql_script, rj_script=rj_script)
        print(
            json.dumps(
                {
                    "event": "dual_leg_start",
                    "id": window["id"],
                    "table": window["table"],
                    "mode": mode,
                }
            ),
            flush=True,
        )
        return subprocess_leg(window, script=script, prefix=prefix)

    result = run_dual(windows, run_leg_fn=run_leg, settle_s=settle_s)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
