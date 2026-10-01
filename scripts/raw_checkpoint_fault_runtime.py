#!/usr/bin/env python3
"""Crash-frontier runtime for raw-before-checkpoint capture.

Offline (no AS400_RAW_BUCKET): in-process exception matrix.
On the capture Job: decode a tiny proof-table window, SIGKILL a child at
each frontier, recover, and emit counters only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from quadringent.fault_runtime import (
    FRONTIERS,
    inspect_frontier,
    recover,
    run_fault_runtime,
    store_from_env,
    validate_fault_scope,
)
from quadringent.raw import read_raw_batch
from quadringent.site_config import SiteConfig, current as current_site


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"ERROR: {name} is required")
    return value


def main() -> int:
    site = current_site()
    if "--child" in sys.argv[1:] or os.environ.get("AS400_FAULT_CHILD") == "1":
        from quadringent.fault_runtime import run_child

        return run_child(site=site)
    if not os.environ.get("AS400_RAW_BUCKET", "").strip():
        report = run_fault_runtime(site=site)
    else:
        report = run_live(site=site)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "PASS" else 1


def run_live(*, site: SiteConfig) -> dict[str, object]:
    validate_fault_scope(site=site)
    payload, manifest, decode = _decode_window()
    batch = read_raw_batch(manifest, payload)
    payload_key = f"batch-{batch.manifest.batch_id}.jsonl"
    manifest_key = f"batch-{batch.manifest.batch_id}.manifest.json"
    payload_path = Path(os.environ.get("AS400_FAULT_PAYLOAD", "/tmp/fault-raw/payload.jsonl"))
    manifest_path = Path(os.environ.get("AS400_FAULT_MANIFEST", "/tmp/fault-raw/manifest.json"))
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(payload)
    manifest_path.write_bytes(manifest)

    base_prefix = os.environ["AS400_RAW_PREFIX"]
    base_stream = os.environ["AS400_STREAM_KEY"]
    cases: list[dict[str, object]] = []
    for frontier in FRONTIERS:
        prefix = f"{base_prefix}/{frontier.replace('_', '-')}"
        stream_key = f"{base_stream}/{frontier}"
        child_env = os.environ.copy()
        child_env.update(
            {
                "AS400_FAULT_CHILD": "1",
                "AS400_CRASH_AFTER": frontier,
                "AS400_CRASH_MODE": "kill",
                "AS400_RAW_PREFIX": prefix,
                "AS400_STREAM_KEY": stream_key,
                "AS400_FAULT_PAYLOAD": str(payload_path),
                "AS400_FAULT_MANIFEST": str(manifest_path),
            }
        )
        started = time.perf_counter()
        child = subprocess.run(
            [sys.executable, "-m", "quadringent.fault_runtime", "--child"],
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        crash_ms = round((time.perf_counter() - started) * 1000, 3)
        failure_observed = child.returncode == -9 or child.returncode == 137
        os.environ["AS400_RAW_PREFIX"] = prefix
        os.environ["AS400_STREAM_KEY"] = stream_key
        store, checkpoint = store_from_env(site=site)
        after_fault = inspect_frontier(
            store,
            checkpoint,
            payload_key=payload_key,
            manifest_key=manifest_key,
        )
        recovery, collisions = recover(
            store,
            checkpoint,
            payload=payload,
            manifest=manifest,
        )
        after_recovery = inspect_frontier(
            store,
            checkpoint,
            payload_key=payload_key,
            manifest_key=manifest_key,
        )
        from quadringent.fault_runtime import evaluate_case

        case = evaluate_case(
            frontier,
            failure_observed=failure_observed,
            after_fault=after_fault,
            recovery=recovery,
            collisions=collisions,
            after_recovery=after_recovery,
            expected_events=batch.manifest.event_count,
        )
        case["child_returncode"] = child.returncode
        case["crash_ms"] = crash_ms
        case["s3_prefix"] = prefix
        case["stream_key"] = stream_key
        case["payload_key"] = payload_key
        case["object_key"] = f"{prefix}/{payload_key}"
        cases.append(case)

    passed = all(
        item["recovered"]
        and item["loss"] == 0
        and item["extra"] == 0
        and item["collisions"] == 0
        and not item["checkpoint_ahead_of_raw_after_fault"]
        and not item["checkpoint_ahead_of_raw_after_recovery"]
        for item in cases
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "crash_mode": "kill",
        "decode": decode,
        "event_count": batch.manifest.event_count,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "batch_id": batch.manifest.batch_id,
        "high_watermark": {
            "receiver": batch.manifest.high_watermark.receiver,
            "sequence": batch.manifest.high_watermark.sequence,
        },
        "cases": cases,
    }


def _decode_window() -> tuple[bytes, bytes, dict[str, object]]:
    raw_dir = Path(os.environ.get("AS400_RAW_DIRECTORY", "/tmp/fault-raw/decode"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    receiver = _required("AS400_FAULT_RECEIVER")
    start = _required("AS400_FAULT_START_SEQUENCE")
    end = _required("AS400_FAULT_END_SEQUENCE")
    env = os.environ.copy()
    env.update(
        {
            "ISERIES_RECEIVER": receiver,
            "ISERIES_RECEIVER_LIBRARY": os.environ.get("AS400_FAULT_RECEIVER_LIBRARY", "DEMOLIB"),
            "ISERIES_START_SEQUENCE": start,
            "ISERIES_END_SEQUENCE": end,
            "ISERIES_MAX_SERVER_ENTRIES": str(int(end) - int(start) + 1),
            "ISERIES_MAX_DECODED_ENTRIES": os.environ.get("ISERIES_MAX_DECODED_ENTRIES", "50"),
            "AS400_RAW_HIGH_WATERMARK_SEQUENCE": end,
            "AS400_RAW_DIRECTORY": str(raw_dir),
            "AS400_VERBOSE": "false",
        }
    )
    env.pop("AS400_CHECKPOINT_FILE", None)
    classpath = os.environ.get("AS400_JAVA_CLASSPATH", "/app/probe.jar:/app/lib/*")
    java = os.environ.get("AS400_JAVA", "java")
    started = time.perf_counter()
    completed = subprocess.run(
        [java, "-cp", classpath, "io.quadringent.as400.ReadOnlyJournalDecode"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    summary_line = ""
    for line in completed.stdout.splitlines():
        if line.startswith("summary "):
            summary_line = line
            break
    if completed.returncode != 0:
        raise RuntimeError(f"decode_failed:exit={completed.returncode}")
    payloads = list(raw_dir.glob("*.jsonl"))
    manifests = list(raw_dir.glob("*.manifest.json"))
    if len(payloads) != 1 or len(manifests) != 1:
        raise RuntimeError("decode produced an incomplete raw batch")
    tokens = dict(
        token.split("=", 1)
        for token in summary_line.split()[1:]
        if "=" in token
    )
    decode = {
        "receiver": receiver,
        "start_sequence": int(start),
        "end_sequence": int(end),
        "exit_code": completed.returncode,
        "elapsed_ms": elapsed_ms,
        "seen": _maybe_int(tokens.get("seen")),
        "decoded": _maybe_int(tokens.get("decoded")),
        "scan_complete": tokens.get("scan_complete") == "true",
    }
    return payloads[0].read_bytes(), manifests[0].read_bytes(), decode


def _maybe_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "event": "fault_runtime_failed",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
