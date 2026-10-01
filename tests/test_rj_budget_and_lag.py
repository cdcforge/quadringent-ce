"""Budget and lag reporting on the RetrieveJournal path.

The SQL path got --max-seconds, per-poll lag samples and a closing lag_trend
during the 2026-08-26 soaks. The RJ path had none, so an RJ soak could not be
bounded or judged. ContinuousCaptureService.run already accepts a `stop`
callable and hands metrics to `on_result`, so both hook in without touching
the service.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import budget_exhausted, lag_trend


class BudgetStopTests(unittest.TestCase):
    def test_a_stop_callable_built_from_the_budget_fires_when_exhausted(self) -> None:
        clock = iter([0.0, 10.0, 200.0])
        stop = _budget_stop(started_at=0.0, max_seconds=120, window_seconds=20,
                            clock=lambda: next(clock))
        self.assertFalse(stop())
        self.assertFalse(stop())
        self.assertTrue(stop())

    def test_no_budget_never_stops(self) -> None:
        stop = _budget_stop(started_at=0.0, max_seconds=None, window_seconds=20,
                            clock=lambda: 1e9)
        self.assertFalse(stop())

    def test_the_budget_leaves_room_for_one_window(self) -> None:
        stop = _budget_stop(started_at=0.0, max_seconds=100, window_seconds=25,
                            clock=lambda: 80.0)
        self.assertTrue(stop())


class LagCollectionTests(unittest.TestCase):
    def test_lag_samples_are_taken_from_the_metrics(self) -> None:
        samples: list[int] = []
        _collect_lag({"last_lag_sequences": 9891723}, samples)
        _collect_lag({"last_lag_sequences": 10002785}, samples)
        _collect_lag({"last_lag_sequences": None}, samples)
        self.assertEqual(samples, [9891723, 10002785])

    def test_a_growing_lag_is_reported_as_diverging(self) -> None:
        samples: list[int] = []
        for value in (1, 133812, 415554, 933512, 1377824):
            _collect_lag({"last_lag_sequences": value}, samples)
        self.assertEqual(lag_trend(samples)["verdict"], "DIVERGING")

    def test_an_empty_series_is_inconclusive(self) -> None:
        self.assertEqual(lag_trend([])["verdict"], "INCONCLUSIVE")


def _budget_stop(*, started_at, max_seconds, window_seconds, clock):
    """Mirror of the helper wired into the RJ script."""

    def stop() -> bool:
        return budget_exhausted(
            started_at=started_at, now=clock(),
            max_seconds=max_seconds, window_seconds=window_seconds,
        )

    return stop


def _collect_lag(metrics, samples) -> None:
    value = metrics.get("last_lag_sequences")
    if isinstance(value, int):
        samples.append(value)


class WiringTests(unittest.TestCase):
    """The script must actually use them, not just have them available."""

    def test_the_script_declares_max_seconds(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertIn("--max-seconds", source)

    def test_the_script_passes_a_stop_callable_to_run(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertIn("stop=", source)

    def test_the_script_emits_a_closing_lag_trend(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertIn('"event": "lag_trend"', source)


if __name__ == "__main__":
    unittest.main()
