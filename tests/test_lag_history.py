from __future__ import annotations

import unittest

from quadringent.lag_history import LagHistory


class LagHistoryTests(unittest.TestCase):
    def test_memory_stays_bounded_over_a_long_run(self) -> None:
        history = LagHistory(capacity=240, initial_width_s=5.0)
        for second in range(86_400):
            history.observe(elapsed_s=float(second), lag=1)

        self.assertLessEqual(len(history.buckets), 240)
        self.assertEqual(history.sample_count, 86_400)
        # La dernière borne couvre bien la fin du run : rien n'a été perdu.
        self.assertGreaterEqual(history.buckets[-1].end_s, 86_000)

    def test_compaction_never_creates_overlapping_bucket_boundaries(self) -> None:
        history = LagHistory(capacity=240, initial_width_s=5.0)
        for index in range(1_402):
            history.observe(elapsed_s=5.0 + index * 1.27, lag=1)

        self.assertTrue(
            all(
                current.end_s <= following.start_s
                for current, following in zip(history.buckets, history.buckets[1:])
            )
        )

    def test_decimation_never_erases_a_return_to_the_tail(self) -> None:
        """Le point non négociable.

        Un pic isolé au milieu d'un run calme doit survivre à la décimation, et
        le retour au tail aussi. Un seau qui ne garderait que son dernier
        échantillon effacerait l'un ou l'autre selon l'endroit où tombe la
        frontière.
        """

        history = LagHistory(capacity=8, initial_width_s=1.0)
        for second in range(400):
            lag = 468_046 if second == 137 else 1
            history.observe(elapsed_s=float(second), lag=lag)

        peaks = [bucket.maximum for bucket in history.buckets]
        floors = [bucket.minimum for bucket in history.buckets]
        self.assertIn(468_046, peaks)
        self.assertEqual(min(value for value in floors if value is not None), 1)

    def test_a_rising_floor_survives_decimation(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=1.0)
        for second in range(600):
            history.observe(elapsed_s=float(second), lag=1 + second * 2_296)

        first_floor, last_floor = history.floor_thirds()
        assert first_floor is not None and last_floor is not None
        self.assertLess(first_floor, last_floor)

    def test_a_collapsing_floor_survives_decimation(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=1.0)
        for second in range(600):
            history.observe(elapsed_s=float(second), lag=max(1, 30_320_398 - second * 60_000))

        first_floor, last_floor = history.floor_thirds()
        assert first_floor is not None and last_floor is not None
        self.assertGreater(first_floor, last_floor)
        self.assertEqual(last_floor, 1)

    def test_unknown_lag_is_counted_not_swallowed(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=10.0)
        history.observe(elapsed_s=0.0, lag=None)
        history.observe(elapsed_s=1.0, lag=None)
        history.observe(elapsed_s=2.0, lag=7)

        bucket = history.buckets[0]
        self.assertEqual(bucket.samples, 3)
        self.assertEqual(bucket.unknown_samples, 2)
        self.assertEqual(bucket.minimum, 7)
        self.assertEqual(history.unknown_sample_count, 2)

    def test_a_fully_unknown_bucket_reports_no_value(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=1.0)
        history.observe(elapsed_s=0.0, lag=None)

        bucket = history.buckets[0]
        self.assertIsNone(bucket.minimum)
        self.assertIsNone(bucket.maximum)
        self.assertIsNone(bucket.last)
        self.assertEqual(history.known_series(), [])

    def test_an_unknown_sample_does_not_erase_the_last_known_value(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=10.0)
        history.observe(elapsed_s=0.0, lag=42)
        history.observe(elapsed_s=1.0, lag=None)

        self.assertEqual(history.buckets[0].last, 42)

    def test_merging_keeps_the_later_known_value(self) -> None:
        history = LagHistory(capacity=4, initial_width_s=1.0)
        for second, lag in enumerate([9, 8, 7, 6, 5]):
            history.observe(elapsed_s=float(second), lag=lag)

        self.assertLessEqual(len(history.buckets), 4)
        self.assertEqual(history.buckets[-1].last, 5)

    def test_capacity_must_be_even_so_buckets_merge_in_pairs(self) -> None:
        with self.assertRaises(ValueError):
            LagHistory(capacity=7)
        with self.assertRaises(ValueError):
            LagHistory(capacity=2)

    def test_a_negative_elapsed_time_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LagHistory().observe(elapsed_s=-1.0, lag=1)

    def test_floors_need_two_buckets_to_mean_anything(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=100.0)
        history.observe(elapsed_s=0.0, lag=5)

        self.assertEqual(history.floor_thirds(), (None, None))

    def test_payload_is_json_ready(self) -> None:
        import json

        history = LagHistory(capacity=8, initial_width_s=1.0)
        history.observe(elapsed_s=0.0, lag=3)
        history.observe(elapsed_s=1.5, lag=None)

        payload = history.payload()
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(payload["sample_count"], 2)
        self.assertEqual(payload["buckets"][1]["max"], None)



class LagHistoryPeakTests(unittest.TestCase):
    """Un pic absorbé doit survivre à la décimation.

    La série décimée ne retient qu'un échantillon par seau. Lire le pic dessus
    fait disparaître toute pointe tombée en milieu de seau — c'est-à-dire la
    preuve même que l'outil encaisse.
    """

    def test_the_peak_is_exact_even_when_the_last_samples_are_low(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=1.0)
        for second in range(400):
            history.observe(elapsed_s=float(second), lag=468_046 if second == 137 else 1)

        self.assertEqual(history.peak(), 468_046)
        self.assertNotIn(468_046, history.known_series())

    def test_no_known_sample_means_no_peak_not_zero(self) -> None:
        history = LagHistory(capacity=8, initial_width_s=1.0)
        history.observe(elapsed_s=0.0, lag=None)

        self.assertIsNone(history.peak())
if __name__ == "__main__":
    unittest.main()
