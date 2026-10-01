#!/usr/bin/env python3
"""DEV one-poll DISPLAY_JOURNAL capture. Does not call JTOpen RetrieveJournal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import time

from quadringent.checkpoint import DynamoDbCheckpointStore
from quadringent.continuous import (
    PollResult,
    ReceiverSnapshot,
    finite_tail_bootstrap,
    journal_lag,
    adaptive_window,
    budget_exhausted,
    lag_trend,
    plan_next_window,
)
from quadringent.console_snapshot import (
    ConsoleSnapshotBuilder,
    FileSnapshotSink,
    FluxIdentity,
    S3SnapshotSink,
)
from quadringent.contract import JournalPosition
from quadringent.java_catalog import CachedReceiverCatalog, WorkerReceiverCatalog
from quadringent.java_worker import (
    DEFAULT_JOURNAL_BUFFER_SIZE,
    PersistentJavaWorker,
    SqlJavaWindowRunner,
    WORKER_CLASS,
)
from quadringent.object_store import RawFirstCaptureCoordinator, S3ObjectStore
from quadringent.site_config import current as current_site
from quadringent.sql_window import SqlWindowIncomplete, SqlWindowTimeout, capture_sql_window


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-polls", type=int, default=1)
    parser.add_argument("--max-seconds", type=int, default=None,
                        help="wall-clock budget; exits the loop cleanly so the "
                             "closing lag trend is still emitted")
    parser.add_argument(
        "--console-snapshot-path",
        default=None,
        help="écrit le snapshot opérateur final dans un fichier atomique",
    )
    args = parser.parse_args()
    if args.max_polls < 1:
        raise ValueError("--max-polls must be positive")

    checkpoint = DynamoDbCheckpointStore(
        _required("AS400_CHECKPOINT_TABLE"),
        _required("AS400_STREAM_KEY"),
    )
    object_store = S3ObjectStore(
        _required("AS400_RAW_BUCKET"),
        os.environ.get("AS400_RAW_PREFIX", ""),
    )
    coordinator = RawFirstCaptureCoordinator(object_store, checkpoint)
    worker = PersistentJavaWorker(
        java=os.environ.get("AS400_JAVA", "java"),
        classpath=_required("AS400_JAVA_CLASSPATH"),
        host=_required("ISERIES_HOST"),
        user=_required("ISERIES_USER"),
        schema=_required("ISERIES_SCHEMA"),
        table=_required("ISERIES_TABLE"),
        timeout_seconds=_reader_timeout_seconds(),
        retrieve_timeout_ms=_retrieve_timeout_ms(),
        journal_buffer_size=_positive(
            "ISERIES_JOURNAL_BUFFER_SIZE",
            DEFAULT_JOURNAL_BUFFER_SIZE,
        ),
        class_name=os.environ.get("AS400_JAVA_WORKER_CLASS", WORKER_CLASS),
    )
    catalog = CachedReceiverCatalog(
        WorkerReceiverCatalog(
            worker,
            limit=_positive("AS400_RECEIVER_METADATA_LIMIT", 20),
        ),
        ttl_polls=_positive("AS400_CATALOG_TTL_POLLS", 30),
        ttl_seconds=float(os.environ.get("AS400_CATALOG_TTL_SECONDS", "60")),
        max_stale_reuse=_positive("AS400_CATALOG_MAX_STALE", 5),
    )
    runner = SqlJavaWindowRunner(
        worker,
        max_decoded_entries=_positive("ISERIES_MAX_DECODED_ENTRIES", 1000),
    )
    timeout_seconds = min(29.0, float(os.environ.get("AS400_SQL_TIMEOUT_SECONDS", "25")))
    console = ConsoleSnapshotBuilder(identity=_sql_flux_identity())
    console_sink = _console_sink(args.console_snapshot_path)
    console_metrics = _initial_console_metrics()

    def emit(event: dict[str, object]) -> None:
        print(json.dumps(event, sort_keys=True), flush=True)

    polls = 0
    started_at = time.monotonic()
    consecutive_timeouts = 0
    last_lag: int | None = None
    last_processed: tuple[str, int] | None = None
    lag_samples: list[int] = []
    failed = False
    try:
        receivers = list(catalog.snapshot())
        bootstrap_receiver = os.environ.get("AS400_BOOTSTRAP_RECEIVER", "").strip()
        bootstrap_sequence = os.environ.get("AS400_BOOTSTRAP_SEQUENCE", "").strip()
        if bootstrap_receiver and bootstrap_sequence:
            bootstrap = JournalPosition(bootstrap_receiver, int(bootstrap_sequence))
        else:
            bootstrap = finite_tail_bootstrap(
                receivers,
                _positive("AS400_BATCH_ENTRIES", 1000),
            )
        tail = receivers[-1]
        emit(
            {
                "event": "bootstrap_tail",
                "finite": True,
                "receiver": bootstrap.receiver,
                "sequence": bootstrap.sequence,
                "source_tail_sequence": tail.last_sequence,
                "status": tail.status,
                "bootstrap_mode": "explicit" if bootstrap_receiver else "finite_tail",
            }
        )
        while polls < args.max_polls:
            if budget_exhausted(
                started_at=started_at,
                now=time.monotonic(),
                max_seconds=args.max_seconds,
                window_seconds=timeout_seconds,
            ):
                emit({"event": "budget_reached", "polls": polls,
                      "max_seconds": args.max_seconds})
                break
            polls += 1
            # Reuse the bootstrap snapshot on poll 1. A second catalog()
            # call can time out on a loaded IBM i before DISPLAY_JOURNAL.
            snap = receivers if polls == 1 else list(catalog.snapshot())
            if last_processed is not None and snap:
                try:
                    measured = journal_lag(
                        tail_receiver=snap[-1].receiver,
                        tail_sequence=snap[-1].last_sequence,
                        processed_receiver=last_processed[0],
                        processed_sequence=last_processed[1],
                    )
                except ValueError as error:
                    measured = {"comparable": False, "lag_sequences": None,
                                "error": type(error).__name__}
                emit({"event": "lag_sample", "poll": polls, **measured})
                if measured.get("lag_sequences") is not None:
                    last_lag = int(measured["lag_sequences"])
                    lag_samples.append(last_lag)
            backoff = adaptive_window(
                base_max_entries=_positive("AS400_BATCH_ENTRIES", 1000),
                lag_sequences=last_lag,
                consecutive_timeouts=consecutive_timeouts,
                catch_up_divisor=_positive("AS400_CATCH_UP_DIVISOR", 10),
                ceiling=_positive("AS400_BATCH_CEILING", 10000),
            )
            if backoff["mode"] != "tail":
                emit({"event": "window_width", "poll": polls,
                      "mode": backoff["mode"],
                      "max_entries": backoff["max_entries"],
                      "widened": backoff.get("widened", False),
                      "lag": last_lag})
            if not backoff["retry"]:
                emit({"event": "capture_poll", "status": "error",
                      "event_count": 0, "error_type": "WindowBackoffExhausted",
                      "reason": backoff["reason"]})
                break
            plan = plan_next_window(
                checkpoint.load(),
                snap,
                max_entries=int(backoff["max_entries"]),
                bootstrap=bootstrap,
            )
            if plan is None:
                emit({"event": "capture_poll", "status": "idle", "event_count": 0})
                console_metrics["polls"] = int(console_metrics["polls"]) + 1
                console_metrics["idle_polls"] = int(console_metrics["idle_polls"]) + 1
                console.observe(None, console_metrics)
                if polls < args.max_polls:
                    time.sleep(float(os.environ.get("AS400_POLL_SECONDS", "5")))
                continue
            try:
                result = capture_sql_window(
                    window=plan,
                    runner=runner,
                    coordinator=coordinator,
                    checkpoint_store=checkpoint,
                    timeout_seconds=timeout_seconds,
                    emit=emit,
                    object_name=_required("ISERIES_TABLE"),
                )
            except (SqlWindowTimeout, SqlWindowIncomplete) as error:
                consecutive_timeouts += 1
                console_metrics["polls"] = int(console_metrics["polls"]) + 1
                console_metrics["errors"] = int(console_metrics["errors"]) + 1
                console_metrics["last_error_type"] = type(error).__name__
                console.observe(None, console_metrics)
                emit(
                    {
                        "event": "capture_poll",
                        "status": "error",
                        "event_count": 0,
                        "error_type": type(error).__name__,
                        "consecutive_timeouts": consecutive_timeouts,
                        "next_max_entries": adaptive_window(
                            base_max_entries=_positive("AS400_BATCH_ENTRIES", 1000),
                            lag_sequences=last_lag,
                            consecutive_timeouts=consecutive_timeouts,
                        )["max_entries"],
                    }
                )
                time.sleep(float(os.environ.get("AS400_POLL_SECONDS", "5")))
                continue
            consecutive_timeouts = 0
            last_processed = (plan.end.receiver, plan.end.sequence)
            console_metrics = _observe_console_result(
                console_metrics, result, snap[-1]
            )
            console.observe(result, console_metrics)
            emit(
                {
                    "event": "capture_poll",
                    "status": result.status,
                    "event_count": result.event_count,
                    "window": {
                        "receiver": plan.end.receiver,
                        "start_sequence": plan.start.sequence,
                        "end_sequence": plan.end.sequence,
                    },
                }
            )
    except BaseException as error:
        failed = True
        console_metrics["errors"] = int(console_metrics["errors"]) + 1
        console_metrics["last_error_type"] = type(error).__name__
        console.observe(None, console_metrics)
        raise
    finally:
        emit({"event": "lag_trend", **lag_trend(lag_samples)})
        worker.close()
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        self_usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_seconds = (
            usage.ru_utime
            + usage.ru_stime
            + self_usage.ru_utime
            + self_usage.ru_stime
        )
        console.mark_stopped(
            "STOPPED_FAIL_CLOSED" if failed else "STOPPED_BUDGET",
            "échec du canari" if failed else "canari borné terminé",
        )
        console.observe_cpu_seconds(cpu_seconds)
        if console_sink is not None:
            try:
                console_sink.write(console.encode())
            except Exception as error:
                emit(
                    {
                        "event": "console_snapshot_write_failed",
                        "error_type": type(error).__name__,
                    }
                )
        emit(
            {
                "event": "capture_finished",
                "polls": polls,
                "cpu_user_s": round(usage.ru_utime + self_usage.ru_utime, 6),
                "cpu_sys_s": round(usage.ru_stime + self_usage.ru_stime, 6),
                "cpu_children_user_s": round(usage.ru_utime, 6),
                "cpu_children_sys_s": round(usage.ru_stime, 6),
            }
        )
    return 0


def _initial_console_metrics() -> dict[str, object]:
    return {
        "polls": 0,
        "idle_polls": 0,
        "empty_scans": 0,
        "batches_published": 0,
        "events_published": 0,
        "payload_bytes_published": None,
        "receiver_rotations": 0,
        "errors": 0,
        "last_watermark": None,
        "last_source_tail": None,
        "last_lag_sequences": None,
        "last_receiver_first_sequence": None,
        "last_receiver_last_sequence": None,
    }


def _observe_console_result(
    metrics: dict[str, object],
    result: PollResult,
    tail: ReceiverSnapshot,
) -> dict[str, object]:
    observed = dict(metrics)
    observed["polls"] = int(observed["polls"]) + 1
    if result.status == "empty_scan":
        observed["empty_scans"] = int(observed["empty_scans"]) + 1
    elif result.status == "published":
        observed["batches_published"] = int(observed["batches_published"]) + 1
        observed["events_published"] = (
            int(observed["events_published"]) + result.event_count
        )
    if result.window is not None:
        checkpoint = result.window.end
        observed["last_watermark"] = {
            "receiver": checkpoint.receiver,
            "sequence": checkpoint.sequence,
        }
        observed["last_source_tail"] = {
            "receiver": tail.receiver,
            "sequence": tail.last_sequence,
        }
        observed["last_receiver_first_sequence"] = tail.first_sequence
        observed["last_receiver_last_sequence"] = tail.last_sequence
        try:
            lag = journal_lag(
                tail_receiver=tail.receiver,
                tail_sequence=tail.last_sequence,
                processed_receiver=checkpoint.receiver,
                processed_sequence=checkpoint.sequence,
            )
        except ValueError:
            observed["last_lag_sequences"] = None
        else:
            observed["last_lag_sequences"] = lag.get("lag_sequences")
    return observed


def _default_target_label() -> str:
    """Cible déclarée du site — jamais un littéral d'installation."""

    site = current_site()
    return f"Snowflake {site.fleet_environment} · {site.destination_namespace}"


def _sql_flux_identity() -> FluxIdentity:
    table = _required("ISERIES_TABLE")
    return FluxIdentity(
        id=os.environ.get("AS400_STREAM_KEY", table).lower(),
        label=f"{table} — canari DISPLAY_JOURNAL",
        journal=os.environ.get("AS400_JOURNAL_NAME", ""),
        journal_library=os.environ.get("AS400_JOURNAL_LIBRARY", ""),
        objects=(table,),
        reader_path="DISPLAY_JOURNAL",
        target=(
            os.environ["AS400_TARGET_LABEL"]
            if "AS400_TARGET_LABEL" in os.environ
            else _default_target_label()
        ),
        job=os.environ.get("AS400_JOB_NAME", "as400-sql-canary"),
    )


def _console_sink(path: str | None):
    if path:
        return FileSnapshotSink(Path(path))
    key = os.environ.get("AS400_CONSOLE_SNAPSHOT_S3_KEY")
    if not key:
        return None
    import boto3

    return S3SnapshotSink(
        bucket=_required("AS400_RAW_BUCKET"),
        key=key,
        client=boto3.client("s3"),
    )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise ValueError(f"{name} is required")
    return value


def _reader_timeout_seconds() -> float:
    timeout_seconds = float(os.environ.get("AS400_READER_TIMEOUT_SECONDS", "35"))
    if timeout_seconds < 15:
        raise ValueError("AS400_READER_TIMEOUT_SECONDS must be >= 15")
    return timeout_seconds


def _retrieve_timeout_ms() -> int:
    timeout_ms = int(os.environ.get("AS400_RETRIEVE_TIMEOUT_MS", "25000"))
    if timeout_ms < 1000:
        raise ValueError("AS400_RETRIEVE_TIMEOUT_MS must be >= 1000")
    return timeout_ms


def _positive(name: str, default: int) -> int:
    value = os.environ.get(name)
    result = default if value is None or value.strip() == "" else int(value)
    if result < 1:
        raise ValueError(f"{name} must be positive")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
