from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from test_ibmi_reader import FakeConnection, response

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import CapturedWindow, CaptureWindow
from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.ibmi_reader import IbmiJournalReader
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.raw import RawBatchWriter, read_raw_batch
from quadringent.sql_window import (
    DisplayJournalWindowReader,
    SqlWindowIncomplete,
    SqlWindowTimeout,
    capture_sql_window,
    captured_window_from_sql_output,
)


class SlowReader:
    def __init__(self, delay_seconds: float, captured: CapturedWindow) -> None:
        self.delay_seconds = delay_seconds
        self.captured = captured
        self.calls = 0

    def capture(self, window: CaptureWindow) -> CapturedWindow:
        self.calls += 1
        time.sleep(self.delay_seconds)
        return self.captured


class RecordingReader:
    def __init__(self, captured: CapturedWindow) -> None:
        self.captured = captured
        self.windows: list[CaptureWindow] = []

    def capture(self, window: CaptureWindow) -> CapturedWindow:
        self.windows.append(window)
        return self.captured


def _window() -> CaptureWindow:
    return CaptureWindow(
        receiver_library="DEMOLIB",
        start=JournalPosition("DEMOJRN3761", 100),
        end=JournalPosition("DEMOJRN3761", 109),
    )


class SqlBoundedWindowTests(unittest.TestCase):
    def test_delete_images_are_published_before_checkpoint_advances(self) -> None:
        for entry_type in ("DL",):
            with self.subTest(entry_type=entry_type), tempfile.TemporaryDirectory() as directory:
                output = (
                    f"sql_event sequence=105 type={entry_type} timestamp=2026-08-25T10:00:00 fields=2 rrn=41\n"
                    "sql_fields sequence=105 SDOM=A SCOD=1\n"
                    "summary seen=1 decoded=1 elapsed_ms=9 scan_complete=true\n"
                )
                captured = captured_window_from_sql_output(
                    output, _window(), journal="DEMOJRN", library="SALES", table="SALE",
                )
                self.assertIsNotNone(captured.manifest)
                self.assertIsNotNone(captured.payload)
                batch = read_raw_batch(captured.manifest, captured.payload)
                self.assertEqual(len(batch.events), 1)
                event = batch.events[0]
                self.assertEqual(event.operation, "d")
                self.assertEqual(event.before, {"SDOM": "A", "SCOD": "1", "_rrn": 41})
                self.assertIsNone(event.after)
                self.assertEqual(event.position, JournalPosition("DEMOJRN3761", 105))
                checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
                raw = Path(directory) / "raw"
                coordinator = RawFirstCaptureCoordinator(FileObjectStore(raw), checkpoint)
                result = capture_sql_window(
                    window=_window(), runner=RecordingReader(captured),
                    coordinator=coordinator, checkpoint_store=checkpoint, timeout_seconds=5,
                )
                self.assertEqual(result.status, "published")
                self.assertEqual(result.event_count, 1)
                self.assertEqual(checkpoint.load(), _window().end)
                self.assertEqual(len(list(raw.glob("*.jsonl"))), 1)

    def test_delete_without_image_cannot_advance_checkpoint(self) -> None:
        # Ni image ni RRN : impossible de distinguer un delete *AFTER legitime
        # d'une entree tronquee — le checkpoint ne doit pas avancer.
        for entry_type in ("DL", "DR"):
            with self.subTest(entry_type=entry_type), tempfile.TemporaryDirectory() as directory:
                output = (
                    f"sql_event sequence=105 type={entry_type} timestamp=2026-08-25T10:00:00 fields=0 rrn=-1\n"
                    "summary seen=1 decoded=1 elapsed_ms=9 scan_complete=true\n"
                )

                class OutputReader:
                    def __init__(self, text: str) -> None:
                        self.text = text

                    def capture(self, window: CaptureWindow) -> CapturedWindow:
                        return captured_window_from_sql_output(
                            self.text, window, journal="DEMOJRN", library="SALES", table="SALE",
                        )

                checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
                raw = Path(directory) / "raw"
                coordinator = RawFirstCaptureCoordinator(FileObjectStore(raw), checkpoint)
                with self.assertRaises(SqlWindowIncomplete):
                    capture_sql_window(
                        window=_window(), runner=OutputReader(output), coordinator=coordinator,
                        checkpoint_store=checkpoint, timeout_seconds=5,
                    )
                self.assertIsNone(checkpoint.load())
                self.assertEqual(list(raw.iterdir()), [])

    def test_delete_without_image_but_with_rrn_is_published(self) -> None:
        # IMAGES(*AFTER) : le delete n'a pas d'image, le RRN de l'entete est la
        # seule identite durable de la ligne supprimee — il est publie.
        output = (
            "sql_event sequence=105 type=DL timestamp=2026-08-25T10:00:00 fields=0 rrn=77\n"
            "summary seen=1 decoded=1 elapsed_ms=9 scan_complete=true\n"
        )
        captured = captured_window_from_sql_output(
            output, _window(), journal="DEMOJRN", library="SALES", table="PLACE01",
        )
        self.assertIsNotNone(captured.payload)
        batch = read_raw_batch(captured.manifest, captured.payload)
        self.assertEqual(len(batch.events), 1)
        event = batch.events[0]
        self.assertEqual(event.operation, "d")
        self.assertEqual(event.before, {"_rrn": 77})
        self.assertIsNone(event.after)

    def test_rollback_entries_cannot_advance_checkpoint(self) -> None:
        # BR/UR/DR sont des entrees de rollback : un delete annule (DR) n'est
        # pas un delete — le rejouer supprimerait une ligne qui existe encore.
        for entry_type in ("BR", "UR", "DR"):
            with self.subTest(entry_type=entry_type):
                output = (
                    f"sql_event sequence=105 type={entry_type} timestamp=2026-08-25T10:00:00 fields=0 rrn=77\n"
                    "summary seen=1 decoded=1 elapsed_ms=9 scan_complete=true\n"
                )
                with self.assertRaises(SqlWindowIncomplete):
                    captured_window_from_sql_output(
                        output, _window(), journal="DEMOJRN", library="SALES", table="SALE",
                    )

    def test_timeout_must_be_under_30_seconds(self) -> None:
        with self.assertRaises(ValueError):
            capture_sql_window(
                window=_window(),
                runner=RecordingReader(CapturedWindow(scanned_to=JournalPosition("DEMOJRN3761", 109))),
                coordinator=None,  # type: ignore[arg-type]
                checkpoint_store=None,  # type: ignore[arg-type]
                timeout_seconds=30,
            )

    def test_timeout_does_not_advance_checkpoint_or_emit_window_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            events: list[dict[str, object]] = []
            runner = SlowReader(
                0.2,
                CapturedWindow(scanned_to=JournalPosition("DEMOJRN3761", 109)),
            )
            with self.assertRaises(SqlWindowTimeout):
                capture_sql_window(
                    window=_window(),
                    runner=runner,
                    coordinator=coordinator,
                    checkpoint_store=checkpoint,
                    timeout_seconds=0.05,
                    emit=events.append,
                )
            self.assertIsNone(checkpoint.load())
            self.assertEqual(list((Path(directory) / "raw").iterdir()), [])
            names = [item.get("event") for item in events]
            self.assertIn("retrieve_start", names)
            self.assertIn("window_timeout", names)
            self.assertNotIn("window_done", names)
            self.assertNotIn("retrieve_summary", names)

    def test_empty_complete_scan_emits_retrieve_start_then_window_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            events: list[dict[str, object]] = []
            plan = _window()
            result = capture_sql_window(
                window=plan,
                runner=RecordingReader(CapturedWindow(scanned_to=plan.end)),
                coordinator=coordinator,
                checkpoint_store=checkpoint,
                timeout_seconds=5,
                emit=events.append,
            )
            self.assertEqual(result.status, "empty_scan")
            self.assertEqual(checkpoint.load(), plan.end)
            self.assertEqual(
                [item["event"] for item in events],
                ["retrieve_start", "retrieve_summary", "window_done"],
            )
            self.assertEqual(events[0]["object_name"], "CNTR")
            summary = events[1]
            self.assertEqual(summary["decoded"], 0)
            self.assertIsInstance(summary["elapsed_ms"], int)
            self.assertGreaterEqual(summary["elapsed_ms"], 0)
            self.assertEqual(summary["events_per_sec"], 0.0)
            self.assertEqual(events[0]["receiver"], "DEMOJRN3761")
            self.assertEqual(events[0]["start_sequence"], 100)
            self.assertEqual(events[0]["end_sequence"], 109)

    def test_published_window_emits_retrieve_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            writer = RawBatchWriter(Path(directory) / "src")
            watermark = JournalPosition("DEMOJRN3761", 109)
            event = ChangeEvent(
                source_system="ibmi",
                journal="DEMOJRN",
                library="SALES",
                table="ADDRS1",
                operation="c",
                position=watermark,
                commit_timestamp="2026-08-26T17:00:00Z",
                schema_version="sha256:test-schema",
                before=None,
                after={"ID": "1"},
            )
            writer.write_batch([event], high_watermark=watermark)
            payload = next(Path(directory, "src").glob("*.jsonl")).read_bytes()
            manifest = next(Path(directory, "src").glob("*.manifest.json")).read_bytes()
            events: list[dict[str, object]] = []
            plan = _window()
            result = capture_sql_window(
                window=plan,
                runner=RecordingReader(
                    CapturedWindow(
                        scanned_to=plan.end,
                        manifest=manifest,
                        payload=payload,
                    )
                ),
                coordinator=coordinator,
                checkpoint_store=checkpoint,
                timeout_seconds=5,
                emit=events.append,
                object_name="ADDRS1",
            )
            self.assertEqual(result.status, "published")
            self.assertEqual(result.event_count, 1)
            names = [item["event"] for item in events]
            self.assertEqual(names, ["retrieve_start", "retrieve_summary", "window_done"])
            summary = events[1]
            self.assertEqual(summary["decoded"], 1)
            self.assertIsInstance(summary["elapsed_ms"], int)
            self.assertGreaterEqual(summary["elapsed_ms"], 0)
            self.assertEqual(summary["events_per_sec"], round(1000.0 / summary["elapsed_ms"], 3) if summary["elapsed_ms"] else 0.0)

    def test_incomplete_scan_does_not_advance_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = JsonCheckpointStore(Path(directory) / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(
                FileObjectStore(Path(directory) / "raw"), checkpoint
            )
            events: list[dict[str, object]] = []
            with self.assertRaises(SqlWindowIncomplete):
                capture_sql_window(
                    window=_window(),
                    runner=RecordingReader(
                        CapturedWindow(scanned_to=JournalPosition("DEMOJRN3761", 105))
                    ),
                    coordinator=coordinator,
                    checkpoint_store=checkpoint,
                    timeout_seconds=5,
                    emit=events.append,
                )
            self.assertIsNone(checkpoint.load())
            self.assertNotIn("window_done", [item.get("event") for item in events])

    def test_display_journal_reader_filters_cntr_and_keeps_same_receiver_bounds(self) -> None:
        connection = FakeConnection(
            [
                response(
                    ["SEQUENCE_NUMBER", "JOURNAL_CODE", "JOURNAL_ENTRY_TYPE", "RECEIVER_NAME"],
                )
            ]
        )
        reader = DisplayJournalWindowReader(
            IbmiJournalReader(connection),
            journal_library="DEMOLIB",
            journal_name="DEMOJRN",
            object_library="SALES",
            object_name="CNTR",
        )
        captured = reader.capture(_window())
        query = connection.cursor_instance.executed[0]
        self.assertIn("QSYS2.DISPLAY_JOURNAL", query)
        self.assertIn("OBJECT_NAME => 'CNTR'", query)
        self.assertIn("OBJECT_LIBRARY => 'SALES'", query)
        self.assertIn("STARTING_RECEIVER_NAME => 'DEMOJRN3761'", query)
        self.assertIn("ENDING_RECEIVER_NAME => 'DEMOJRN3761'", query)
        self.assertIn("STARTING_SEQUENCE => 100", query)
        self.assertIn("ENDING_SEQUENCE => 109", query)
        self.assertEqual(captured.scanned_to, JournalPosition("DEMOJRN3761", 109))
        self.assertIsNone(captured.payload)

    def test_px_without_row_image_is_fail_closed(self) -> None:
        # Un insert n'a jamais de fallback identite : sans image il est perdu.
        output = (
            "sql_event sequence=105 type=PX timestamp=2026-08-25T10:00:00 fields=0 rrn=5\n"
            "summary seen=1 decoded=1 elapsed_ms=9 scan_complete=true\n"
        )
        with self.assertRaises(SqlWindowIncomplete):
            captured_window_from_sql_output(
                output,
                _window(),
                journal="DEMOJRN",
                library="SALES",
                table="SALE",
            )

    def test_px_ub_up_images_are_published_with_named_fields(self) -> None:
        output = (
            "sql_event sequence=105 type=PX timestamp=2026-08-25T10:00:00 fields=2 rrn=11\n"
            "sql_fields sequence=105 SDOM=A SCOD=1\n"
            "sql_event sequence=106 type=UB timestamp=2026-08-25T10:00:01 fields=2 rrn=11\n"
            "sql_fields sequence=106 SDOM=A SCOD=1\n"
            "sql_event sequence=107 type=UP timestamp=2026-08-25T10:00:02 fields=2 rrn=11\n"
            "sql_fields sequence=107 SDOM=B SCOD=2\n"
            "summary seen=3 decoded=3 elapsed_ms=9 scan_complete=true\n"
        )
        captured = captured_window_from_sql_output(
            output,
            _window(),
            journal="DEMOJRN",
            library="SALES",
            table="SALE",
        )
        self.assertIsNotNone(captured.payload)
        assert captured.payload is not None
        compact = captured.payload.replace(b" ", b"")
        self.assertIn(b'"operation":"c"', compact)
        self.assertIn(b'"operation":"u_before"', compact)
        self.assertIn(b'"operation":"u_after"', compact)
        self.assertIn(b'"SDOM"', captured.payload)
        self.assertNotIn(b'"JOURNAL_ENTRY_TYPE"', captured.payload)


if __name__ == "__main__":
    unittest.main()
