"""When to pay for an emptiness probe before a RetrieveJournal window.

The probe exists because RJ hangs on an empty ATTACHED window (behind1, 41 s).
But it runs a full DISPLAY_JOURNAL scan, which at 531 seq/s costs ~38 s on a
20000-sequence window - more than the RJ window itself (3 s). Measured
2026-08-27: with the probe on, three windows did not finish in 300 s; with it
off, one window took 3097 ms.

The insight: a window can only be empty when the reader is near the tail, and
near the tail the window is small, so the probe is cheap exactly when it is
needed. Far behind, the window is necessarily full and the probe is pure cost.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from quadringent.continuous import should_probe_emptiness


class ProbePolicyTests(unittest.TestCase):
    def test_at_the_tail_the_probe_is_paid(self) -> None:
        """Lag of a few sequences: the window may well be empty."""

        self.assertTrue(should_probe_emptiness(lag_sequences=1, window_sequences=5))

    def test_far_behind_the_probe_is_skipped(self) -> None:
        """A 5 M backlog cannot produce an empty 20000-sequence window."""

        self.assertFalse(
            should_probe_emptiness(lag_sequences=5_205_827, window_sequences=20_000)
        )

    def test_a_window_covering_the_whole_lag_is_probed(self) -> None:
        """If the window reaches the tail, its tail end may be empty."""

        self.assertTrue(should_probe_emptiness(lag_sequences=4_000, window_sequences=20_000))

    def test_a_window_well_inside_the_backlog_is_not_probed(self) -> None:
        self.assertFalse(should_probe_emptiness(lag_sequences=100_000, window_sequences=20_000))

    def test_unknown_lag_falls_back_to_probing(self) -> None:
        """Fail safe: without a lag reading, keep the hang protection."""

        self.assertTrue(should_probe_emptiness(lag_sequences=None, window_sequences=20_000))

    def test_the_boundary_is_the_window_itself(self) -> None:
        self.assertTrue(should_probe_emptiness(lag_sequences=20_000, window_sequences=20_000))
        self.assertFalse(should_probe_emptiness(lag_sequences=20_001, window_sequences=20_000))

    def test_a_negative_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            should_probe_emptiness(lag_sequences=1, window_sequences=0)

    def test_cost_saved_is_the_point(self) -> None:
        """On the measured backlog the probe is skipped on every window."""

        lag = 5_205_827
        skipped = sum(
            0 if should_probe_emptiness(lag_sequences=lag - i * 20_000,
                                        window_sequences=20_000) else 1
            for i in range(260)
        )
        self.assertGreater(skipped, 250)


if __name__ == "__main__":
    unittest.main()


class RunnerIntegrationTests(unittest.TestCase):
    """The RJ runner must apply the policy, and the loop must feed it the lag."""

    def test_runner_skips_the_probe_when_far_behind(self) -> None:
        from quadringent.contract import JournalPosition
        from quadringent.continuous import CaptureWindow
        from quadringent.java_worker import JavaWindowRunner

        class _Worker:
            schema = "SALES"
            table = "SALE"

            def __init__(self):
                self.sql_calls = 0
                self.window_calls = 0

            def sql_window(self, request):
                self.sql_calls += 1
                return "summary decoded=5 elapsed_ms=10 scan_complete=true\n"

            def process_window(self, request):
                self.window_calls += 1
                # decoded=0 keeps the fake honest: no raw artifacts are written,
                # and the runner rightly refuses decoded>0 without artifacts.
                return "summary decoded=0 elapsed_ms=10 scan_complete=true\n"

        worker = _Worker()
        runner = JavaWindowRunner(worker, max_decoded_entries=1000, empty_probe=True)
        window = CaptureWindow(
            receiver_library="DEMOLIB",
            start=JournalPosition("DEMOJRN3780", 1),
            end=JournalPosition("DEMOJRN3780", 20_000),
        )
        runner.observe_lag(5_205_827)
        runner.capture(window)
        self.assertEqual(worker.sql_calls, 0, "probe must be skipped far behind")
        self.assertEqual(worker.window_calls, 1)

    def test_runner_cntr_the_probe_at_the_tail(self) -> None:
        from quadringent.contract import JournalPosition
        from quadringent.continuous import CaptureWindow
        from quadringent.java_worker import JavaWindowRunner

        class _Worker:
            schema = "SALES"
            table = "SALE"

            def __init__(self):
                self.sql_calls = 0

            def sql_window(self, request):
                self.sql_calls += 1
                return "summary decoded=0 elapsed_ms=10 scan_complete=true\n"

        worker = _Worker()
        runner = JavaWindowRunner(worker, max_decoded_entries=1000, empty_probe=True)
        window = CaptureWindow(
            receiver_library="DEMOLIB",
            start=JournalPosition("DEMOJRN3780", 1),
            end=JournalPosition("DEMOJRN3780", 5),
        )
        runner.observe_lag(2)
        captured = runner.capture(window)
        self.assertEqual(worker.sql_calls, 1, "probe must run near the tail")
        self.assertIsNone(captured.manifest)

    def test_tail_capture_reuses_complete_sql_batch_without_second_journal_read(self) -> None:
        from quadringent.contract import ChangeEvent, JournalPosition
        from quadringent.continuous import CaptureWindow
        from quadringent.java_worker import JavaWindowRunner
        from quadringent.raw import RawBatchWriter, read_raw_batch

        class _Worker:
            schema = "SALES"
            table = "SALE"

            def __init__(self) -> None:
                self.sql_calls = 0
                self.window_calls = 0

            def sql_window(self, request: dict[str, object]) -> str:
                self.sql_calls += 1
                directory = Path(str(request["raw_directory"]))
                position = JournalPosition("DEMOJRN3780", 5)
                RawBatchWriter(directory).write_batch(
                    [ChangeEvent(
                        source_system="ibmi", journal="DEMOJRN", library="SALES",
                        table="SALE", operation="c", position=position,
                        commit_timestamp="2026-09-30T00:00:00Z",
                        schema_version="sha256:test-schema", before=None, after={"ID": "1"},
                    )],
                    high_watermark=position,
                )
                return "summary decoded=1 elapsed_ms=12 scan_complete=true\n"

            def process_window(self, request: dict[str, object]) -> str:
                self.window_calls += 1
                raise AssertionError("the completed SQL scan must not be repeated")

        worker = _Worker()
        runner = JavaWindowRunner(worker, max_decoded_entries=1000, empty_probe=True)
        window = CaptureWindow(
            receiver_library="DEMOLIB",
            start=JournalPosition("DEMOJRN3780", 4),
            end=JournalPosition("DEMOJRN3780", 5),
        )
        runner.observe_lag(2)
        captured = runner.capture(window)
        self.assertEqual(worker.sql_calls, 1)
        self.assertEqual(worker.window_calls, 0)
        assert captured.manifest is not None and captured.payload is not None
        batch = read_raw_batch(captured.manifest, captured.payload)
        self.assertEqual(batch.manifest.high_watermark, window.end)
        self.assertEqual(len(batch.events), 1)

    def test_tail_capture_refuses_decoded_rows_without_sql_batch(self) -> None:
        from quadringent.contract import JournalPosition
        from quadringent.continuous import CaptureWindow
        from quadringent.java_worker import JavaWindowRunner

        class _Worker:
            schema = "SALES"
            table = "SALE"

            def sql_window(self, request: dict[str, object]) -> str:
                return "summary decoded=1 elapsed_ms=12 scan_complete=true\n"

            def process_window(self, request: dict[str, object]) -> str:
                raise AssertionError("a missing SQL batch must fail closed")

        runner = JavaWindowRunner(_Worker(), max_decoded_entries=1000, empty_probe=True)
        window = CaptureWindow(
            receiver_library="DEMOLIB",
            start=JournalPosition("DEMOJRN3780", 4),
            end=JournalPosition("DEMOJRN3780", 5),
        )
        runner.observe_lag(2)
        with self.assertRaisesRegex(RuntimeError, "without raw artifacts"):
            runner.capture(window)

    def test_probe_disabled_by_config_stays_disabled(self) -> None:
        from quadringent.java_worker import JavaWindowRunner

        runner = JavaWindowRunner(object(), max_decoded_entries=1000, empty_probe=False)
        runner.observe_lag(1)
        self.assertFalse(runner._probe_wanted(window_sequences=5))

    def test_unknown_lag_keeps_the_probe(self) -> None:
        from quadringent.java_worker import JavaWindowRunner

        runner = JavaWindowRunner(object(), max_decoded_entries=1000, empty_probe=True)
        self.assertTrue(runner._probe_wanted(window_sequences=20_000))
