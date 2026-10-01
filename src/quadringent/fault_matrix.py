from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any, Literal

from .checkpoint import JsonCheckpointStore
from .contract import ChangeEvent, JournalPosition
from .object_store import (
    FileObjectStore,
    ObjectStore,
    RawFirstCaptureCoordinator,
    read_published_batch,
)
from .site_config import SiteConfig


FaultStage = Literal["payload", "manifest", "checkpoint"]


class _FailingObjectStore:
    def __init__(self, delegate: ObjectStore, *, fail_on_call: int) -> None:
        self.delegate = delegate
        self.fail_on_call = fail_on_call
        self.calls = 0

    def put_once(self, key: str, content: bytes) -> bool:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("simulated raw publication failure")
        return self.delegate.put_once(key, content)

    def get(self, key: str) -> bytes:
        return self.delegate.get(key)


class _FailingCheckpointStore:
    def __init__(self, delegate: JsonCheckpointStore) -> None:
        self.delegate = delegate
        self.failed = False

    def load(self) -> JournalPosition | None:
        return self.delegate.load()

    def commit(self, position: JournalPosition) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated checkpoint failure")
        self.delegate.commit(position)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated checkpoint failure")
        self.delegate.transition(previous, position)


def run_fault_matrix(*, site: SiteConfig) -> dict[str, Any]:
    """Exercise raw/checkpoint crash boundaries without external services.

    Each case intentionally fails once, recreates the coordinator, retries the
    same deterministic event, and verifies that the raw batch can be replayed
    and the checkpoint can then advance. The returned structure contains only
    counters and technical positions; it never includes payload bytes or row
    values.
    """

    stages: list[dict[str, Any]] = []
    for stage in ("payload", "manifest", "checkpoint"):
        stages.append(_run_stage(stage, site=site))
    return {
        "status": "PASS" if all(item["recovered"] for item in stages) else "FAIL",
        "stages": stages,
    }


def _run_stage(stage: FaultStage, *, site: SiteConfig) -> dict[str, Any]:
    watermark = JournalPosition("SIM0001", 100)
    events = [_fixture_event(watermark, site=site)]
    with tempfile.TemporaryDirectory(prefix="as400-fault-matrix-") as directory:
        root = Path(directory)
        base_store = FileObjectStore(root / "objects")
        base_checkpoint = JsonCheckpointStore(root / "checkpoint.json")

        if stage == "checkpoint":
            fault_store: ObjectStore = base_store
            fault_checkpoint: Any = _FailingCheckpointStore(base_checkpoint)
        else:
            fault_store = _FailingObjectStore(
                base_store,
                fail_on_call=1 if stage == "payload" else 2,
            )
            fault_checkpoint = base_checkpoint

        failure_observed = False
        try:
            RawFirstCaptureCoordinator(fault_store, fault_checkpoint).capture(
                events,
                high_watermark=watermark,
            )
        except RuntimeError:
            failure_observed = True

        checkpoint_after_fault = _position_payload(base_checkpoint.load())
        raw_objects_after_fault = (
            len([item for item in (root / "objects").iterdir() if item.is_file()])
            if (root / "objects").exists()
            else 0
        )

        retry = RawFirstCaptureCoordinator(base_store, base_checkpoint).capture(
            events,
            high_watermark=watermark,
        )
        batch = read_published_batch(
            base_store,
            retry.publish.payload_key,
            retry.publish.manifest_key,
        )
        checkpoint_after_recovery = _position_payload(base_checkpoint.load())
        recovered = (
            failure_observed
            and checkpoint_after_fault is None
            and len(batch.events) == len(events)
            and checkpoint_after_recovery == _position_payload(watermark)
        )
        return {
            "stage": stage,
            "failure_observed": failure_observed,
            "raw_objects_after_fault": raw_objects_after_fault,
            "checkpoint_after_fault": checkpoint_after_fault,
            "recovered": recovered,
            "replayed_event_count": len(batch.events),
            "retry_payload_created": retry.publish.payload_created,
            "retry_manifest_created": retry.publish.manifest_created,
            "checkpoint_after_recovery": checkpoint_after_recovery,
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
        commit_timestamp="2026-08-20T00:00:00Z",
        schema_version="as400-raw-v1",
        before=None,
        after={"ID": "A", "VALUE": "one"},
    )


def _position_payload(position: JournalPosition | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {"receiver": position.receiver, "sequence": position.sequence}
