"""One-process two-leg DISPLAY_JOURNAL capture for AC2 (single IBM i reader)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence


def load_registry(path: str | Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload.get("windows"), list) or not payload["windows"]:
        raise ValueError("proven window registry must list windows")
    return payload


def window_by_id(registry: dict, window_id: str) -> dict:
    for item in registry["windows"]:
        if item["id"] == window_id:
            return item
    raise ValueError(f"unknown proven window: {window_id}")


REQUIRED_PAIR = ("addrs1_sql_3769", "custom1_sql_3769")


def gate_job_legs(leg_ids: Sequence[str], registry_path: str | Path) -> list[str]:
    if len(leg_ids) != 2 or len(set(leg_ids)) != 2:
        raise ValueError("exactly two distinct proven windows are required")
    registry = load_registry(registry_path)
    required = tuple(registry.get("required_pair") or REQUIRED_PAIR)
    if tuple(leg_ids) != required:
        raise ValueError(f"ac2 pair must be {required[0]} then {required[1]}")
    excluded = set(registry.get("exclude") or [])
    selected = []
    for window_id in leg_ids:
        if window_id in excluded:
            raise ValueError(f"excluded window: {window_id}")
        item = window_by_id(registry, window_id)
        if item.get("mode") not in {"sql", "rj"}:
            raise ValueError(f"unexpected mode for {window_id}: {item.get('mode')}")
        limit = int(item.get("receiver_metadata_limit") or 8)
        if int(item["start_sequence"]) < 200000000 and limit < 20:
            raise ValueError(f"stale receiver needs metadata_limit>=20: {window_id}")
        selected.append(window_id)
    return selected


def parse_leg_stdout(stdout: str) -> dict:
    published = 0
    summary = None
    last_error = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("event")
        if name == "retrieve_summary":
            decoded = event.get("decoded")
            elapsed_ms = event.get("elapsed_ms")
            if isinstance(decoded, int) and isinstance(elapsed_ms, int):
                summary = {
                    "decoded": decoded,
                    "elapsed_ms": elapsed_ms,
                    "events_per_sec": event.get("events_per_sec"),
                }
        if name == "capture_poll" and event.get("status") == "published":
            published = int(event.get("event_count") or 0)
        if event.get("error_type"):
            last_error = str(event.get("error_type"))
        if event.get("last_error_type"):
            last_error = str(event.get("last_error_type"))
    if last_error is None and "bootstrap receiver is not in metadata" in stdout:
        last_error = "ReceiverPlanningError"
    if last_error is None and "bounded IBM i reader timed out" in stdout:
        last_error = "ReaderTimeout"
    return {
        "events_published": published,
        "retrieve_summary": summary,
        "last_error": last_error,
    }


def ac2_claim(legs: Sequence[dict]) -> bool:
    ok = 0
    for leg in legs:
        summary = leg.get("retrieve_summary") or {}
        if (
            int(leg.get("events_published") or 0) > 0
            and isinstance(summary.get("decoded"), int)
            and isinstance(summary.get("elapsed_ms"), int)
        ):
            ok += 1
    return ok >= 2


def run_dual(
    legs: Sequence[dict],
    *,
    run_leg_fn: Callable[[dict], dict],
    settle_s: float = 20.0,
) -> dict:
    results: list[dict] = []
    for index, leg in enumerate(legs):
        try:
            results.append(run_leg_fn(leg))
        except Exception as exc:
            results.append(
                {
                    "id": leg.get("id"),
                    "table": leg.get("table"),
                    "events_published": 0,
                    "retrieve_summary": None,
                    "last_error": type(exc).__name__,
                }
            )
        if index < len(legs) - 1 and settle_s > 0:
            time.sleep(settle_s)
    return {"event": "dual_done", "ac2_claim": ac2_claim(results), "legs": results}


def _leg_env(window: dict, *, prefix: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ISERIES_TABLE": str(window["table"]),
            "AS400_BOOTSTRAP_RECEIVER": str(window["receiver"]),
            "AS400_BOOTSTRAP_SEQUENCE": str(window["start_sequence"]),
            "AS400_BATCH_ENTRIES": str(window["batch_entries"]),
            "ISERIES_MAX_DECODED_ENTRIES": str(window["batch_entries"]),
            "AS400_SQL_TIMEOUT_SECONDS": str(window.get("sql_timeout_seconds", 25)),
            "AS400_RECEIVER_METADATA_LIMIT": str(window.get("receiver_metadata_limit", 8)),
            "AS400_READER_TIMEOUT_SECONDS": str(window.get("reader_timeout_seconds", 35)),
            "AS400_RETRIEVE_TIMEOUT_MS": str(window.get("retrieve_timeout_ms", 25000)),
            "AS400_CATCH_UP_BATCH_ENTRIES": str(
                window.get("catch_up_batch_entries", window["batch_entries"])
            ),
            "AS400_RAW_PREFIX": prefix,
            "AS400_STREAM_KEY": prefix,
        }
    )
    return env


def capture_script(window: dict, *, sql_script: str | Path, rj_script: str | Path) -> Path:
    if window.get("mode") == "rj":
        return Path(rj_script)
    return Path(sql_script)


def subprocess_leg(
    window: dict,
    *,
    script: str | Path,
    prefix: str,
    python: str | None = None,
) -> dict:
    proc = subprocess.run(
        [python or sys.executable, str(script), "--max-polls", "1"],
        env=_leg_env(window, prefix=prefix),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")
    parsed = parse_leg_stdout("\n".join([proc.stdout or "", proc.stderr or ""]))
    parsed.update(
        {
            "id": window["id"],
            "table": window["table"],
            "receiver": window["receiver"],
            "start_sequence": window["start_sequence"],
            "end_sequence": int(window["start_sequence"]) + int(window["batch_entries"]) - 1,
            "rc": proc.returncode,
        }
    )
    return parsed
