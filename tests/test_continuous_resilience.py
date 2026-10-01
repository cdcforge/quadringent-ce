"""Surviving a slow journal window, and judging lag divergence honestly.

Measured on the 2026-08-26 lag step 1: a single SqlWindowTimeout ended the
worker after 46 s of a 120 s budget, which makes any long soak impossible.
The same run showed lag oscillating 1..157 with repeated returns to 1, which
a slope-only trend wrongly called DIVERGING.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import lag_trend, window_backoff


class WindowBackoffTests(unittest.TestCase):
    def test_a_timeout_halves_the_window(self) -> None:
        state = window_backoff(max_entries=5000, consecutive_timeouts=1)
        self.assertEqual(state["max_entries"], 2500)
        self.assertTrue(state["retry"])

    def test_backoff_compounds_with_consecutive_timeouts(self) -> None:
        self.assertEqual(window_backoff(max_entries=5000, consecutive_timeouts=2)["max_entries"], 1250)
        self.assertEqual(window_backoff(max_entries=5000, consecutive_timeouts=3)["max_entries"], 625)

    def test_window_never_goes_below_the_floor(self) -> None:
        state = window_backoff(max_entries=5000, consecutive_timeouts=20, floor=100)
        self.assertEqual(state["max_entries"], 100)

    def test_too_many_consecutive_timeouts_stops_fail_closed(self) -> None:
        state = window_backoff(max_entries=5000, consecutive_timeouts=6, max_consecutive=5)
        self.assertFalse(state["retry"])
        self.assertEqual(state["reason"], "too many consecutive window timeouts")

    def test_no_timeout_keeps_the_configured_window(self) -> None:
        state = window_backoff(max_entries=5000, consecutive_timeouts=0)
        self.assertEqual(state["max_entries"], 5000)
        self.assertTrue(state["retry"])


class LagDivergenceTests(unittest.TestCase):
    def test_measured_step1_series_is_bounded_not_diverging(self) -> None:
        """Real series from example-corp-lag-step1: returns to 1 repeatedly."""

        trend = lag_trend([4, 8, 1, 1, 29, 1, 3, 1, 157, 63])
        self.assertEqual(trend["verdict"], "BOUNDED")
        self.assertFalse(trend["diverging"])

    def test_a_reader_that_never_catches_up_is_diverging(self) -> None:
        trend = lag_trend([100, 400, 900, 1600, 2500, 3600])
        self.assertEqual(trend["verdict"], "DIVERGING")

    def test_a_shrinking_lag_still_reads_as_catching_up(self) -> None:
        trend = lag_trend([5000, 4000, 3000, 2000, 1000])
        self.assertEqual(trend["verdict"], "CATCHING_UP")

    def test_a_flat_lag_is_still_bounded(self) -> None:
        trend = lag_trend([500, 480, 510, 495, 505])
        self.assertEqual(trend["verdict"], "BOUNDED")

    def test_trend_reports_the_floor_the_reader_returns_to(self) -> None:
        trend = lag_trend([4, 8, 1, 1, 29, 1, 3, 1, 157, 63])
        self.assertEqual(trend["min"], 1)
        self.assertEqual(trend["max"], 157)


if __name__ == "__main__":
    unittest.main()


class FloorRiseTests(unittest.TestCase):
    """A rising floor is divergence, whatever the slope test says.

    Measured on example-corp-lag-step6 (2026-08-27): lag went from 1 to 1377824
    monotonically, floor of the last third was 751741 against 1 for the first,
    and lag_trend still returned BOUNDED. The slope threshold was relative to
    the mean lag (0.25 * 557583 = 139396 per sample), so a real +11004 per
    sample climb fell under it. The two criteria were combined with AND.
    """

    def test_measured_step6_series_is_diverging(self) -> None:
        series = [1, 133812, 228321, 328011, 415554, 400303, 497330, 762366,
                  933512, 1085321, 1214120, 1377824]
        trend = lag_trend(series)
        self.assertEqual(trend["verdict"], "DIVERGING")
        self.assertTrue(trend["diverging"])

    def test_a_floor_returning_to_one_is_still_bounded(self) -> None:
        trend = lag_trend([4, 8, 1, 1, 29, 1, 3, 1, 157, 63])
        self.assertEqual(trend["verdict"], "BOUNDED")

    def test_a_tiny_floor_rise_is_not_divergence(self) -> None:
        """A healthy reader touching 1 then settling at 5 has not diverged."""

        trend = lag_trend([1, 2, 1, 3, 4, 5, 3, 5])
        self.assertEqual(trend["verdict"], "BOUNDED")

    def test_the_absolute_floor_guard_is_reported(self) -> None:
        trend = lag_trend([1, 133812, 1377824])
        self.assertGreater(trend["floor_last_third"], trend["floor_first_third"])
        self.assertTrue(trend["diverging"])
