from __future__ import annotations

import unittest

from quadringent.continuous import CaptureWindow, PollResult, ReceiverSnapshot
from quadringent.contract import JournalPosition
from as400_sql_journal_capture import (
    _initial_console_metrics,
    _observe_console_result,
)


class SqlCanarySnapshotTests(unittest.TestCase):
    def test_published_window_updates_one_coherent_console_observation(self) -> None:
        metrics = _initial_console_metrics()
        window = CaptureWindow(
            receiver_library="JRNLIB1",
            start=JournalPosition("DEMOJRN3848", 100),
            end=JournalPosition("DEMOJRN3848", 109),
        )
        result = PollResult("published", window, 3)
        receiver = ReceiverSnapshot(
            "JRNLIB1", "DEMOJRN3848", 1, 110, "ATTACHED"
        )

        observed = _observe_console_result(metrics, result, receiver)

        self.assertIsNot(observed, metrics)
        self.assertEqual(observed["polls"], 1)
        self.assertEqual(observed["batches_published"], 1)
        self.assertEqual(observed["events_published"], 3)
        self.assertEqual(
            observed["last_watermark"],
            {"receiver": "DEMOJRN3848", "sequence": 109},
        )
        self.assertEqual(
            observed["last_source_tail"],
            {"receiver": "DEMOJRN3848", "sequence": 110},
        )
        self.assertEqual(observed["last_lag_sequences"], 1)
        self.assertEqual(observed["last_receiver_first_sequence"], 1)
        self.assertEqual(observed["last_receiver_last_sequence"], 110)

    def test_empty_scan_is_observed_without_inventing_a_published_batch(self) -> None:
        metrics = _initial_console_metrics()
        window = CaptureWindow(
            receiver_library="JRNLIB1",
            start=JournalPosition("DEMOJRN3848", 100),
            end=JournalPosition("DEMOJRN3848", 109),
        )
        receiver = ReceiverSnapshot(
            "JRNLIB1", "DEMOJRN3848", 1, 110, "ATTACHED"
        )

        observed = _observe_console_result(
            metrics, PollResult("empty_scan", window, 0), receiver
        )

        self.assertEqual(observed["empty_scans"], 1)
        self.assertEqual(observed["batches_published"], 0)
        self.assertEqual(observed["events_published"], 0)


if __name__ == "__main__":
    unittest.main()
