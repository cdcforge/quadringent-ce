from __future__ import annotations

import json
import os
import re
from pathlib import Path
import signal
import sys
import tempfile
from typing import Any, Literal

from .checkpoint import JsonCheckpointStore
from .contract import ChangeEvent, JournalPosition
from .object_store import (
    FileObjectStore,
    ObjectStore,
    RawFirstCaptureCoordinator,
    S3ObjectStore,
    read_published_batch,
)
from .raw import RawBatchWriter, read_raw_batch
from .site_config import SiteConfig, current as _current_site


CrashFrontier = Literal["after_read", "after_payload", "after_manifest"]
FRONTIERS: tuple[CrashFrontier, ...] = ("after_read", "after_payload", "after_manifest")


class SimulatedCrash(RuntimeError):
    """In-process stand-in for a worker kill at a raw/checkpoint frontier."""


def _position_payload(position: JournalPosition | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {"receiver": position.receiver, "sequence": position.sequence}


def _crash(mode: str) -> None:
    if mode == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    raise SimulatedCrash("fault_runtime crash frontier")


def raw_object_present(store: ObjectStore, key: str) -> bool:
    try:
        store.get(key)
        return True
    except FileNotFoundError:
        return False
    except Exception as error:
        code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def count_raw_objects(store: ObjectStore) -> int:
    if isinstance(store, FileObjectStore):
        if not store.root.exists():
            return 0
        return sum(1 for path in store.root.iterdir() if path.is_file())
    client = getattr(store, "client", None)
    bucket = getattr(store, "bucket", None)
    prefix = str(getattr(store, "prefix", "") or "").strip("/")
    if client is None or not bucket:
        raise TypeError("cannot count objects for this store")
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix + "/"
    response = client.list_objects_v2(**kwargs)
    return len(response.get("Contents") or [])


def inspect_frontier(
    store: ObjectStore,
    checkpoint_store: Any,
    *,
    payload_key: str,
    manifest_key: str,
) -> dict[str, Any]:
    payload_present = raw_object_present(store, payload_key)
    manifest_present = raw_object_present(store, manifest_key)
    checkpoint = checkpoint_store.load()
    ahead = bool(checkpoint is not None and not (payload_present and manifest_present))
    raw_watermark = None
    if payload_present and manifest_present:
        batch = read_published_batch(store, payload_key, manifest_key)
        raw_watermark = batch.manifest.high_watermark
        if checkpoint is not None and (
            checkpoint.receiver != raw_watermark.receiver
            or checkpoint.sequence > raw_watermark.sequence
        ):
            ahead = True
    return {
        "payload_present": payload_present,
        "manifest_present": manifest_present,
        "checkpoint": _position_payload(checkpoint),
        "raw_watermark": _position_payload(raw_watermark),
        "checkpoint_ahead_of_raw": ahead,
        "raw_objects": count_raw_objects(store),
    }


def expected_presence(frontier: CrashFrontier) -> tuple[bool, bool]:
    if frontier == "after_read":
        return False, False
    if frontier == "after_payload":
        return True, False
    return True, True


def capture_with_crash(
    store: ObjectStore,
    checkpoint_store: Any,
    *,
    payload: bytes,
    manifest: bytes,
    crash_after: CrashFrontier | None,
    crash_mode: str = "exception",
) -> dict[str, Any]:
    """Publish payload then manifest then checkpoint, optionally dying between steps."""

    batch = read_raw_batch(manifest, payload)
    payload_key = f"batch-{batch.manifest.batch_id}.jsonl"
    manifest_key = f"batch-{batch.manifest.batch_id}.manifest.json"
    if crash_after == "after_read":
        _crash(crash_mode)
    payload_created = store.put_once(payload_key, payload)
    if crash_after == "after_payload":
        _crash(crash_mode)
    manifest_created = store.put_once(manifest_key, manifest)
    if crash_after == "after_manifest":
        _crash(crash_mode)
    checkpoint_store.commit(batch.manifest.high_watermark)
    return {
        "payload_created": payload_created,
        "manifest_created": manifest_created,
        "checkpoint": _position_payload(batch.manifest.high_watermark),
        "event_count": batch.manifest.event_count,
        "batch_id": batch.manifest.batch_id,
    }


def recover(
    store: ObjectStore,
    checkpoint_store: Any,
    *,
    payload: bytes,
    manifest: bytes,
) -> tuple[dict[str, Any], int]:
    batch = read_raw_batch(manifest, payload)
    payload_key = f"batch-{batch.manifest.batch_id}.jsonl"
    manifest_key = f"batch-{batch.manifest.batch_id}.manifest.json"
    try:
        result = RawFirstCaptureCoordinator(store, checkpoint_store).capture_raw(
            manifest,
            payload,
            payload_key=payload_key,
            manifest_key=manifest_key,
        )
    except ValueError as error:
        if "collision" in str(error).lower():
            return {"error_type": type(error).__name__}, 1
        raise
    replay = read_published_batch(store, payload_key, manifest_key)
    original_ids = set(batch.manifest.event_ids)
    replay_ids = {event.event_id for event in replay.events}
    return {
        "payload_created": result.publish.payload_created,
        "manifest_created": result.publish.manifest_created,
        "checkpoint_committed": result.checkpoint_committed,
        "replayed_event_count": len(replay.events),
        "loss": len(original_ids - replay_ids),
        "extra": len(replay_ids - original_ids),
        "batch_id": batch.manifest.batch_id,
    }, 0


def evaluate_case(
    frontier: CrashFrontier,
    *,
    failure_observed: bool,
    after_fault: dict[str, Any],
    recovery: dict[str, Any],
    collisions: int,
    after_recovery: dict[str, Any],
    expected_events: int,
) -> dict[str, Any]:
    payload_expected, manifest_expected = expected_presence(frontier)
    recovered = (
        failure_observed
        and after_fault["payload_present"] is payload_expected
        and after_fault["manifest_present"] is manifest_expected
        and after_fault["checkpoint"] is None
        and not after_fault["checkpoint_ahead_of_raw"]
        and collisions == 0
        and recovery.get("loss") == 0
        and recovery.get("extra") == 0
        and recovery.get("replayed_event_count") == expected_events
        and after_recovery["payload_present"]
        and after_recovery["manifest_present"]
        and after_recovery["checkpoint"] is not None
        and after_recovery["checkpoint"] == after_recovery["raw_watermark"]
        and not after_recovery["checkpoint_ahead_of_raw"]
    )
    return {
        "frontier": frontier,
        "failure_observed": failure_observed,
        "payload_after_fault": after_fault["payload_present"],
        "manifest_after_fault": after_fault["manifest_present"],
        "checkpoint_after_fault": after_fault["checkpoint"],
        "checkpoint_ahead_of_raw_after_fault": after_fault["checkpoint_ahead_of_raw"],
        "raw_objects_after_fault": after_fault["raw_objects"],
        "recovered": recovered,
        "loss": recovery.get("loss"),
        "extra": recovery.get("extra"),
        "collisions": collisions,
        "replayed_event_count": recovery.get("replayed_event_count"),
        "retry_payload_created": recovery.get("payload_created"),
        "retry_manifest_created": recovery.get("manifest_created"),
        "checkpoint_after_recovery": after_recovery["checkpoint"],
        "checkpoint_ahead_of_raw_after_recovery": after_recovery["checkpoint_ahead_of_raw"],
        "raw_objects_after_recovery": after_recovery["raw_objects"],
    }


def _fixture_event(position: JournalPosition, *, site: SiteConfig) -> ChangeEvent:
    source_system, journal, library, table = site.event_scope()
    return ChangeEvent(
        source_system=source_system,
        journal=journal,
        library=library,
        table=table,
        operation="c",
        position=position,
        commit_timestamp="2026-08-23T00:00:00Z",
        schema_version="as400-raw-v1",
        before=None,
        after={"ID": "A"},
    )


def run_one_case_inprocess(
    frontier: CrashFrontier, *, site: SiteConfig
) -> dict[str, Any]:
    watermark = JournalPosition("SIMFAULT", 200)
    events = [_fixture_event(watermark, site=site)]
    with tempfile.TemporaryDirectory(prefix="as400-fault-runtime-") as directory:
        root = Path(directory)
        store = FileObjectStore(root / "objects")
        checkpoint = JsonCheckpointStore(root / "checkpoint.json")
        staged = RawBatchWriter(root / "stage").write_batch(events, high_watermark=watermark)
        payload_key = f"batch-{staged.batch_id}.jsonl"
        manifest_key = f"batch-{staged.batch_id}.manifest.json"
        payload = (root / "stage" / payload_key).read_bytes()
        manifest = (root / "stage" / manifest_key).read_bytes()
        failure_observed = False
        try:
            capture_with_crash(
                store,
                checkpoint,
                payload=payload,
                manifest=manifest,
                crash_after=frontier,
                crash_mode="exception",
            )
        except SimulatedCrash:
            failure_observed = True
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
        return evaluate_case(
            frontier,
            failure_observed=failure_observed,
            after_fault=after_fault,
            recovery=recovery,
            collisions=collisions,
            after_recovery=after_recovery,
            expected_events=len(events),
        )


def run_fault_runtime(*, site: SiteConfig) -> dict[str, Any]:
    """Offline crash-frontier matrix. Counters and positions only."""

    cases = [run_one_case_inprocess(frontier, site=site) for frontier in FRONTIERS]
    passed = all(
        item["recovered"]
        and item["loss"] == 0
        and item["extra"] == 0
        and item["collisions"] == 0
        and not item["checkpoint_ahead_of_raw_after_fault"]
        and not item["checkpoint_ahead_of_raw_after_recovery"]
        for item in cases
    )
    return {"status": "PASS" if passed else "FAIL", "crash_mode": "exception", "cases": cases}


def validate_fault_scope(*, site: SiteConfig, child: bool = False) -> None:
    """Reject unapproved fault targets before source/storage I/O.

    Every expected value is derived from the declared site configuration —
    nothing is pinned to an installation. This validates configuration, not
    live account identity or run ownership. Those remain mandatory deployment
    preflight checks.
    """
    if not isinstance(site, SiteConfig):
        raise ValueError("fault injection requires a declared site configuration")
    expected = {
        "AS400_FAULT_CONFIRM": site.fault_confirmation_token,
        "AS400_RAW_BUCKET": site.raw_bucket,
        "AS400_CHECKPOINT_TABLE": site.checkpoint_table,
        "ISERIES_SCHEMA": site.source_schema,
        "ISERIES_TABLE": site.proof_table,
        "AWS_DEFAULT_REGION": site.aws_region,
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "fault injection requires the declared site configuration"
        )
    if os.environ.get("AWS_REGION", site.aws_region) != site.aws_region:
        raise ValueError("fault injection region mismatch")
    run_id = os.environ.get("AS400_FAULT_RUN_ID", "")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", run_id):
        raise ValueError("fault injection requires an isolated run id")
    base = f"{site.stream_prefix}/faults/{run_id}"
    pairs = {(base, base)} if not child else {
        (f"{base}/{frontier.replace('_', '-')}", f"{base}/{frontier}")
        for frontier in FRONTIERS
    }
    if (os.environ.get("AS400_RAW_PREFIX"), os.environ.get("AS400_STREAM_KEY")) not in pairs:
        raise ValueError("fault injection requires matching isolated raw/checkpoint paths")


def store_from_env(*, site: SiteConfig) -> tuple[ObjectStore, Any]:
    bucket = os.environ.get("AS400_RAW_BUCKET", "").strip()
    root = os.environ.get("AS400_FAULT_ROOT", "").strip()
    if bucket:
        validate_fault_scope(site=site, child=True)
        from .checkpoint import DynamoDbCheckpointStore

        store: ObjectStore = S3ObjectStore(bucket, os.environ.get("AS400_RAW_PREFIX", ""))
        checkpoint: Any = DynamoDbCheckpointStore(
            os.environ["AS400_CHECKPOINT_TABLE"],
            os.environ["AS400_STREAM_KEY"],
        )
        return store, checkpoint
    if not root:
        raise ValueError("AS400_RAW_BUCKET or AS400_FAULT_ROOT is required")
    base = Path(root)
    return FileObjectStore(base / "objects"), JsonCheckpointStore(base / "checkpoint.json")


def run_child(*, site: SiteConfig) -> int:
    frontier = os.environ["AS400_CRASH_AFTER"]
    if frontier not in FRONTIERS:
        raise ValueError("AS400_CRASH_AFTER is not a known frontier")
    payload = Path(os.environ["AS400_FAULT_PAYLOAD"]).read_bytes()
    manifest = Path(os.environ["AS400_FAULT_MANIFEST"]).read_bytes()
    store, checkpoint = store_from_env(site=site)
    print(
        json.dumps({"event": "fault_crash", "frontier": frontier}, sort_keys=True),
        flush=True,
    )
    capture_with_crash(
        store,
        checkpoint,
        payload=payload,
        manifest=manifest,
        crash_after=frontier,  # type: ignore[arg-type]
        crash_mode=os.environ.get("AS400_CRASH_MODE", "kill"),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    site = _current_site()
    if "--child" in args or os.environ.get("AS400_FAULT_CHILD") == "1":
        return run_child(site=site)
    report = run_fault_runtime(site=site)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
