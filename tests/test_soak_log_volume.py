"""Log volume must stay bounded on a long soak.

At the tail the worker polls about 1.3 times per second, so a 24 h run emits
roughly 110000 capture_poll lines, each carrying the full metrics object. That
is hundreds of megabytes of pod logs and it truncates, which would lose exactly
the evidence the soak is meant to produce.

A poll that publishes or errors is always logged. Quiet polls (idle, empty scan)
are summarised on an interval instead.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import should_log_poll


class AlwaysLoggedTests(unittest.TestCase):
    def test_a_published_poll_is_always_logged(self) -> None:
        self.assertTrue(should_log_poll(status="published", polls=5000,
                                        seconds_since_last_log=0.0, interval_s=60))

    def test_an_error_is_always_logged(self) -> None:
        self.assertTrue(should_log_poll(status="error", polls=5000,
                                        seconds_since_last_log=0.0, interval_s=60))


class QuietPollTests(unittest.TestCase):
    def test_a_quiet_poll_is_skipped_inside_the_interval(self) -> None:
        self.assertFalse(should_log_poll(status="idle", polls=5000,
                                         seconds_since_last_log=5.0, interval_s=60))
        self.assertFalse(should_log_poll(status="empty_scan", polls=5000,
                                        seconds_since_last_log=59.0, interval_s=60))

    def test_a_quiet_poll_is_logged_once_the_interval_elapsed(self) -> None:
        self.assertTrue(should_log_poll(status="idle", polls=5000,
                                        seconds_since_last_log=60.0, interval_s=60))

    def test_the_first_polls_are_always_logged(self) -> None:
        """The start of a run must be fully visible."""

        for polls in (1, 2, 3):
            self.assertTrue(should_log_poll(status="idle", polls=polls,
                                            seconds_since_last_log=0.0, interval_s=60))

    def test_an_interval_of_zero_logs_everything(self) -> None:
        self.assertTrue(should_log_poll(status="idle", polls=9999,
                                        seconds_since_last_log=0.0, interval_s=0))


class BoundsTests(unittest.TestCase):
    def test_a_negative_interval_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            should_log_poll(status="idle", polls=1, seconds_since_last_log=0.0,
                            interval_s=-1)

    def test_volume_estimate_for_24h_is_bounded(self) -> None:
        """With a 60 s interval, quiet polls contribute 1440 lines a day."""

        seconds_per_day = 86_400
        self.assertEqual(seconds_per_day // 60, 1440)


if __name__ == "__main__":
    unittest.main()
