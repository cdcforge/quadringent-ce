"""Crossing a receiver rotation on the SQL capture path.

Measured on example-corp-lag-step3 (2026-08-26): the journal rotated from
DEMOJRN3775 to DEMOJRN3776 after 232 s and the run died with
`ValueError: receiver rotation requires explicit ordering`.

plan_next_window already sets ``rotated_from`` on a rotating window, and the
RetrieveJournal path honours it (continuous.py) as does the SQL empty-scan
path (sql_window.py). Only the SQL *published* path calls capture_raw, which
uses commit() and refuses a receiver change.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import CapturedWindow, CaptureWindow
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.raw import RawBatchWriter
from quadringent.sql_window import capture_sql_window

OLD = "DEMOJRN3775"
NEW = "DEMOJRN3776"


class _Runner:
    def __init__(self, captured: CapturedWindow) -> None:
        self.captured = captured

    def capture(self, window: CaptureWindow) -> CapturedWindow:
        return self.captured


def _batch(directory: str, watermark: JournalPosition) -> tuple[bytes, bytes]:
    writer = RawBatchWriter(Path(directory) / "src")
    event = ChangeEvent(
        source_system="ibmi",
        journal="DEMOJRN",
        library="SALES",
        table="SALE",
        operation="c",
        position=watermark,
        commit_timestamp="2026-08-26T21:43:00Z",
        schema_version="sha256:test-schema",
        before=None,
        after={"ID": "1"},
    )
    writer.write_batch([event], high_watermark=watermark)
    payload = next(Path(directory, "src").glob("*.jsonl")).read_bytes()
    manifest = next(Path(directory, "src").glob("*.manifest.json")).read_bytes()
    return manifest, payload


class SqlRotationTests(unittest.TestCase):
    def test_a_published_rotating_window_crosses_the_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            previous = JournalPosition(OLD, 240_181_522)
            checkpoint.commit(previous)
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            watermark = JournalPosition(NEW, 42)
            manifest, payload = _batch(directory, watermark)
            plan = CaptureWindow(
                receiver_library="DEMOLIB",
                start=JournalPosition(NEW, 1),
                end=watermark,
                rotated_from=previous,
            )
            self.assertTrue(plan.rotated)

            result = capture_sql_window(
                window=plan,
                runner=_Runner(CapturedWindow(
                    scanned_to=plan.end, manifest=manifest, payload=payload)),
                coordinator=coordinator,
                checkpoint_store=checkpoint,
                timeout_seconds=5,
                emit=lambda item: None,
                object_name="SALE",
            )
            self.assertEqual(result.status, "published")
            landed = checkpoint.load()
            self.assertEqual(landed.receiver, NEW)
            self.assertEqual(landed.sequence, 42)

    def test_a_non_rotating_published_window_still_commits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint.commit(JournalPosition(OLD, 100))
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            watermark = JournalPosition(OLD, 109)
            manifest, payload = _batch(directory, watermark)
            plan = CaptureWindow(
                receiver_library="DEMOLIB",
                start=JournalPosition(OLD, 101),
                end=watermark,
            )
            result = capture_sql_window(
                window=plan,
                runner=_Runner(CapturedWindow(
                    scanned_to=plan.end, manifest=manifest, payload=payload)),
                coordinator=coordinator,
                checkpoint_store=checkpoint,
                timeout_seconds=5,
                emit=lambda item: None,
                object_name="SALE",
            )
            self.assertEqual(result.status, "published")
            self.assertEqual(checkpoint.load().receiver, OLD)
            self.assertEqual(checkpoint.load().sequence, 109)

    def test_a_rotation_against_a_wrong_predecessor_is_refused(self) -> None:
        """Fail-closed: the CAS must match the exact stored position."""

        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            checkpoint.commit(JournalPosition(OLD, 240_181_522))
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            watermark = JournalPosition(NEW, 42)
            manifest, payload = _batch(directory, watermark)
            plan = CaptureWindow(
                receiver_library="DEMOLIB",
                start=JournalPosition(NEW, 1),
                end=watermark,
                rotated_from=JournalPosition(OLD, 999),  # not what is stored
            )
            with self.assertRaises(ValueError):
                capture_sql_window(
                    window=plan,
                    runner=_Runner(CapturedWindow(
                        scanned_to=plan.end, manifest=manifest, payload=payload)),
                    coordinator=coordinator,
                    checkpoint_store=checkpoint,
                    timeout_seconds=5,
                    emit=lambda item: None,
                    object_name="SALE",
                )


if __name__ == "__main__":
    unittest.main()
