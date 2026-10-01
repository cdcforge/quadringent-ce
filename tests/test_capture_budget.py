"""Wall-clock budget for a continuous capture run.

The escalating soak (2, 10, 30 min) needs a deterministic exit: relying on
activeDeadlineSeconds kills the pod, so the final lag_trend is never emitted.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from quadringent.continuous import budget_exhausted

import as400_continuous_capture as capture
from test_continuous_capture_gcs import ENV


class BudgetTests(unittest.TestCase):
    def test_no_budget_never_exhausts(self) -> None:
        self.assertFalse(budget_exhausted(started_at=0.0, now=99999.0, max_seconds=None))

    def test_budget_not_reached_yet(self) -> None:
        self.assertFalse(budget_exhausted(started_at=100.0, now=200.0, max_seconds=120))

    def test_budget_reached(self) -> None:
        self.assertTrue(budget_exhausted(started_at=100.0, now=220.0, max_seconds=120))

    def test_budget_leaves_room_for_one_window(self) -> None:
        """Stop before a window that cannot finish inside the budget."""

        self.assertTrue(
            budget_exhausted(started_at=0.0, now=100.0, max_seconds=120, window_seconds=25)
        )
        self.assertFalse(
            budget_exhausted(started_at=0.0, now=90.0, max_seconds=120, window_seconds=25)
        )

    def test_negative_budget_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            budget_exhausted(started_at=0.0, now=1.0, max_seconds=-1)


class CaptureBudgetTests(unittest.TestCase):
    def test_budget_not_above_reader_timeout_is_refused_before_io(self) -> None:
        for budget, timeout in (("60", "300"), ("30", "30")):
            with self.subTest(budget=budget), patch.dict(
                os.environ, {**ENV, "AS400_READER_TIMEOUT_SECONDS": timeout}, clear=True
            ), patch("sys.argv", ["capture", "--max-seconds", budget]), patch.object(
                capture, "PersistentJavaWorker"
            ) as worker, patch.object(capture, "_gcs_client") as client:
                with self.assertRaisesRegex(ValueError, "max-seconds"):
                    capture.main()
            worker.assert_not_called()
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
