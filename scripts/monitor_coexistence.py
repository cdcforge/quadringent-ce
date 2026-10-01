#!/usr/bin/env python3
"""Stop one Quadringent pilot Job if the read-only Popsink baseline changes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from quadringent.coexistence import monitor_pilot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--job-namespace", default=os.environ.get("QUADRINGENT_JOB_NAMESPACE", ""),
                        help="namespace du Job pilote (défaut : QUADRINGENT_JOB_NAMESPACE)")
    parser.add_argument("--popsink-namespace", default="popsink")
    parser.add_argument(
        "--baseline-file",
        type=Path,
        help="snapshot JSON Popsink capturé avant le déploiement du pilote",
    )
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    args = parser.parse_args()
    if not args.job_namespace.strip():
        print("ERROR: --job-namespace or QUADRINGENT_JOB_NAMESPACE is required", file=sys.stderr)
        return 2

    def read_popsink() -> dict:
        return _kubectl_json(
            args.context,
            "-n",
            args.popsink_namespace,
            "get",
            "pods",
            "-o",
            "json",
        )

    def read_job_state() -> str:
        proc = _kubectl(
            args.context,
            "-n",
            args.job_namespace,
            "get",
            "job",
            args.job_name,
            "-o",
            "json",
            check=False,
        )
        if proc.returncode != 0:
            if "NotFound" in proc.stderr:
                return "not_found"
            raise RuntimeError("pilot Job state is unavailable")
        payload = json.loads(proc.stdout)
        conditions = payload.get("status", {}).get("conditions", [])
        for condition in conditions:
            if condition.get("status") != "True":
                continue
            if condition.get("type") == "Complete":
                return "complete"
            if condition.get("type") == "Failed":
                return "failed"
        return "running"

    def stop_pilot(issues: list[str]) -> None:
        print(
            json.dumps(
                {"event": "coexistence_guard_open", "issues": issues},
                sort_keys=True,
            ),
            flush=True,
        )
        _kubectl(
            args.context,
            "-n",
            args.job_namespace,
            "delete",
            "job",
            args.job_name,
            "--wait=true",
            "--timeout=60s",
        )

    try:
        baseline_payload = (
            json.loads(args.baseline_file.read_text(encoding="utf-8"))
            if args.baseline_file is not None
            else None
        )
        result = monitor_pilot(
            read_popsink=read_popsink,
            read_job_state=read_job_state,
            stop_pilot=stop_pilot,
            baseline_payload=baseline_payload,
            interval_seconds=args.interval_seconds,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            json.dumps(
                {
                    "event": "coexistence_monitor_failed",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"event": "coexistence_monitor_finished", **result}, sort_keys=True))
    return 0 if result["verdict"] == "COMPLETE" else 1


def _kubectl(context: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", context, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _kubectl_json(context: str, *args: str) -> dict:
    return json.loads(_kubectl(context, *args).stdout)


if __name__ == "__main__":
    raise SystemExit(main())
