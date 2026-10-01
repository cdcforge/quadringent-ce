"""Window width that widens to catch up and narrows to survive.

Measured on example-corp-lag-step4 (2026-08-26, 1800 s): lag peaked at 468046
sequences while the window stayed capped at 5000 per poll, and 54 of 537
windows exceeded the SQL deadline. The backoff then narrowed the window at
the exact moment throughput was most needed.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import adaptive_window


class CatchUpTests(unittest.TestCase):
    def test_at_the_tail_the_configured_width_is_used(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=1,
                                consecutive_timeouts=0)
        self.assertEqual(state["max_entries"], 5000)
        self.assertEqual(state["mode"], "tail")

    def test_a_large_lag_widens_the_window(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=400_000,
                                consecutive_timeouts=0)
        self.assertGreater(state["max_entries"], 5000)
        self.assertEqual(state["mode"], "catch_up")

    def test_widening_is_capped(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=10_000_000,
                                consecutive_timeouts=0, ceiling=40_000)
        self.assertEqual(state["max_entries"], 40_000)

    def test_a_small_lag_does_not_widen(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=900,
                                consecutive_timeouts=0)
        self.assertEqual(state["max_entries"], 5000)
        self.assertEqual(state["mode"], "tail")


class TimeoutPrecedenceTests(unittest.TestCase):
    def test_a_timeout_narrows_even_while_catching_up(self) -> None:
        """Surviving the window matters more than catching up fast."""

        state = adaptive_window(base_max_entries=5000, lag_sequences=400_000,
                                consecutive_timeouts=2)
        self.assertLess(state["max_entries"], 5000)
        self.assertEqual(state["mode"], "backoff")

    def test_narrowing_respects_the_floor(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=400_000,
                                consecutive_timeouts=20, floor=100)
        self.assertEqual(state["max_entries"], 100)
        self.assertFalse(state["retry"])

    def test_a_cleared_streak_returns_to_catch_up(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=400_000,
                                consecutive_timeouts=0)
        self.assertEqual(state["mode"], "catch_up")

    def test_unknown_lag_falls_back_to_the_base_width(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=None,
                                consecutive_timeouts=0)
        self.assertEqual(state["max_entries"], 5000)
        self.assertEqual(state["mode"], "tail")


class BoundsTests(unittest.TestCase):
    def test_the_result_never_exceeds_the_java_hard_cap(self) -> None:
        """AS400_BATCH_ENTRIES is refused above 10000 by the Java side."""

        state = adaptive_window(base_max_entries=5000, lag_sequences=5_000_000,
                                consecutive_timeouts=0, ceiling=10_000)
        self.assertLessEqual(state["max_entries"], 10_000)

    def test_a_negative_lag_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            adaptive_window(base_max_entries=5000, lag_sequences=-1,
                            consecutive_timeouts=0)


if __name__ == "__main__":
    unittest.main()


class DivisorTests(unittest.TestCase):
    """The widening threshold must be reachable for the lags actually observed.

    With the default divisor of 10 against a base of 5000, widening needs a lag
    above 50000. The 1800 s run of 2026-08-26 peaked at 17901, so the mechanism
    never fired and its effect was never measured.
    """

    def test_default_divisor_needs_a_very_large_lag(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=17_901,
                                consecutive_timeouts=0)
        self.assertEqual(state["max_entries"], 5000)

    def test_a_smaller_divisor_widens_on_the_observed_lag(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=17_901,
                                consecutive_timeouts=0, catch_up_divisor=2)
        self.assertEqual(state["max_entries"], 8950)
        self.assertEqual(state["mode"], "catch_up")

    def test_divisor_one_widens_to_the_whole_lag_within_the_ceiling(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=8_000,
                                consecutive_timeouts=0, catch_up_divisor=1)
        self.assertEqual(state["max_entries"], 8000)

    def test_the_ceiling_still_wins(self) -> None:
        state = adaptive_window(base_max_entries=5000, lag_sequences=90_000,
                                consecutive_timeouts=0, catch_up_divisor=1,
                                ceiling=10_000)
        self.assertEqual(state["max_entries"], 10_000)

    def test_a_zero_divisor_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            adaptive_window(base_max_entries=5000, lag_sequences=1000,
                            consecutive_timeouts=0, catch_up_divisor=0)

    def test_widening_reports_that_it_fired(self) -> None:
        narrow = adaptive_window(base_max_entries=5000, lag_sequences=17_901,
                                 consecutive_timeouts=0)
        wide = adaptive_window(base_max_entries=5000, lag_sequences=17_901,
                               consecutive_timeouts=0, catch_up_divisor=2)
        self.assertFalse(narrow["widened"])
        self.assertTrue(wide["widened"])
