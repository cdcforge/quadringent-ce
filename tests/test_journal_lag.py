"""Lag of the continuous reader behind the journal tail.

This is the product metric the crush lot never measured: a continuous CDC is
good enough when it follows the change stream with a bounded lag, not when it
rereads history fast.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import journal_lag, lag_trend


class JournalLagTests(unittest.TestCase):
    def test_lag_is_the_distance_to_the_tail(self) -> None:
        lag = journal_lag(
            tail_receiver="DEMOJRN3769", tail_sequence=210_010_000,
            processed_receiver="DEMOJRN3769", processed_sequence=210_009_000,
        )
        self.assertEqual(lag["lag_sequences"], 1000)
        self.assertTrue(lag["comparable"])

    def test_caught_up_reader_has_zero_lag(self) -> None:
        lag = journal_lag(
            tail_receiver="R1", tail_sequence=100,
            processed_receiver="R1", processed_sequence=100,
        )
        self.assertEqual(lag["lag_sequences"], 0)

    def test_different_receivers_are_not_on_one_scale(self) -> None:
        lag = journal_lag(
            tail_receiver="DEMOJRN3770", tail_sequence=10,
            processed_receiver="DEMOJRN3769", processed_sequence=210_009_000,
        )
        self.assertFalse(lag["comparable"])
        self.assertIsNone(lag["lag_sequences"])

    def test_processed_ahead_of_tail_is_refused(self) -> None:
        """A checkpoint ahead of the tail means the position is wrong."""

        with self.assertRaises(ValueError):
            journal_lag(
                tail_receiver="R1", tail_sequence=100,
                processed_receiver="R1", processed_sequence=101,
            )


class LagTrendTests(unittest.TestCase):
    def test_a_flat_lag_is_bounded(self) -> None:
        trend = lag_trend([500, 480, 510, 495, 505])
        self.assertEqual(trend["verdict"], "BOUNDED")
        self.assertFalse(trend["diverging"])

    def test_a_growing_lag_is_diverging(self) -> None:
        trend = lag_trend([100, 400, 900, 1600, 2500])
        self.assertEqual(trend["verdict"], "DIVERGING")
        self.assertTrue(trend["diverging"])

    def test_a_shrinking_lag_is_catching_up(self) -> None:
        trend = lag_trend([5000, 4000, 3000, 2000, 1000])
        self.assertEqual(trend["verdict"], "CATCHING_UP")
        self.assertFalse(trend["diverging"])

    def test_too_few_samples_is_inconclusive(self) -> None:
        trend = lag_trend([100])
        self.assertEqual(trend["verdict"], "INCONCLUSIVE")

    def test_trend_reports_first_last_and_slope(self) -> None:
        trend = lag_trend([100, 200, 300, 400])
        self.assertEqual(trend["first"], 100)
        self.assertEqual(trend["last"], 400)
        self.assertEqual(trend["samples"], 4)
        self.assertGreater(trend["slope_per_sample"], 0)

    def test_noise_around_a_plateau_does_not_read_as_divergence(self) -> None:
        """A reader that keeps up wobbles; it must not be called diverging."""

        trend = lag_trend([1000, 1200, 900, 1100, 950, 1050])
        self.assertEqual(trend["verdict"], "BOUNDED")


if __name__ == "__main__":
    unittest.main()


class MissingPositionTests(unittest.TestCase):
    def test_unknown_tail_sequence_is_not_comparable(self) -> None:
        """An ATTACHED receiver can expose no last_sequence yet."""

        lag = journal_lag(
            tail_receiver="R1", tail_sequence=None,
            processed_receiver="R1", processed_sequence=100,
        )
        self.assertFalse(lag["comparable"])
        self.assertIsNone(lag["lag_sequences"])

    def test_unknown_processed_sequence_is_not_comparable(self) -> None:
        lag = journal_lag(
            tail_receiver="R1", tail_sequence=100,
            processed_receiver="R1", processed_sequence=None,
        )
        self.assertFalse(lag["comparable"])
