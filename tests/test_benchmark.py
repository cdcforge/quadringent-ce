from __future__ import annotations

import unittest

from quadringent_research.benchmark import (
    BenchmarkSample,
    compare_samples,
    event_fingerprint,
    percentile,
    position_fingerprint,
    samples_from_continuous_log,
)


def sample(solution: str, elapsed_ms: float, events: int = 100) -> BenchmarkSample:
    records = [
        ("R2", sequence, f"event-{sequence}", "u")
        for sequence in range(100, 200)
    ]
    return BenchmarkSample(
        solution=solution,
        phase="source_to_raw",
        table="SALES.CNTR",
        receiver="R2",
        start_sequence=100,
        end_sequence=199,
        contract_version="as400-raw-v1",
        event_count=events,
        payload_bytes=events * 100,
        elapsed_ms=elapsed_ms,
        position_fingerprint=position_fingerprint(
            [("R2", sequence) for sequence in range(100, 200)]
        ),
        event_fingerprint=event_fingerprint(records),
        error_count=0,
        lag_sequences=2,
        cost_usd=0.01,
        cost_provenance="snowflake:query-tag=AS400_RD_BENCHMARK_DEV",
    )


class BenchmarkTests(unittest.TestCase):
    def test_percentile_is_deterministic_for_small_samples(self) -> None:
        self.assertEqual(percentile([10, 20, 30, 40], 50), 25)
        self.assertEqual(percentile([10, 20, 30, 40], 95), 38.5)

    def test_comparison_reports_rates_latency_errors_lag_and_cost(self) -> None:
        report = compare_samples(
            [
                sample("popsink", 1000),
                sample("popsink", 2000),
                sample("rd", 500),
            ]
        )

        self.assertEqual(report["scope"]["receiver"], "R2")
        self.assertEqual(report["solutions"]["popsink"]["sample_count"], 2)
        self.assertEqual(report["solutions"]["rd"]["event_count"], 100)
        self.assertEqual(report["solutions"]["rd"]["latency_ms"]["p50"], 500)
        self.assertEqual(report["solutions"]["popsink"]["throughput_events_per_second"], 66.667)
        self.assertEqual(report["solutions"]["rd"]["cost_usd"], 0.01)

    def test_comparison_rejects_different_windows(self) -> None:
        mismatched = sample("rd", 500)
        mismatched = BenchmarkSample(**{**mismatched.__dict__, "end_sequence": 200})

        with self.assertRaises(ValueError):
            compare_samples([sample("popsink", 1000), mismatched])

    def test_position_fingerprint_is_deterministic_for_the_same_position_set(self) -> None:
        first = position_fingerprint([("R2", 102), ("R2", 100), ("R2", 101)])
        second = position_fingerprint([("R2", 100), ("R2", 101), ("R2", 102)])

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_comparison_rejects_different_position_fingerprints(self) -> None:
        popsink = BenchmarkSample(
            **{**sample("popsink", 1000).__dict__, "position_fingerprint": "sha256:a"}
        )
        rd = BenchmarkSample(
            **{**sample("rd", 500).__dict__, "position_fingerprint": "sha256:b"}
        )

        with self.assertRaises(ValueError):
            compare_samples([popsink, rd])

    def test_event_fingerprint_is_deterministic_and_operation_sensitive(self) -> None:
        records = [("R2", 100, "event-100", "u"), ("R2", 101, "event-101", "u")]
        reordered = list(reversed(records))
        changed_operation = [("R2", 100, "event-100", "d"), ("R2", 101, "event-101", "u")]

        self.assertEqual(event_fingerprint(records), event_fingerprint(reordered))
        self.assertNotEqual(event_fingerprint(records), event_fingerprint(changed_operation))

    def test_comparison_rejects_different_event_fingerprints(self) -> None:
        popsink = BenchmarkSample(
            **{**sample("popsink", 1000).__dict__, "event_fingerprint": "sha256:a"}
        )
        rd = sample("rd", 500)

        with self.assertRaises(ValueError):
            compare_samples([popsink, rd])

    def test_comparison_requires_exact_event_fingerprint(self) -> None:
        popsink = BenchmarkSample(
            **{**sample("popsink", 1000).__dict__, "event_fingerprint": None}
        )
        rd = BenchmarkSample(
            **{**sample("rd", 500).__dict__, "event_fingerprint": None}
        )

        with self.assertRaises(ValueError):
            compare_samples([popsink, rd])

    def test_cost_requires_a_safe_provenance(self) -> None:
        sample_without_provenance = BenchmarkSample(
            **{**sample("rd", 500).__dict__, "cost_provenance": None}
        )

        with self.assertRaises(ValueError):
            compare_samples([sample_without_provenance])

    def test_strict_comparison_requires_both_solutions_and_attributed_cost(self) -> None:
        report = compare_samples(
            [sample("popsink", 1000), sample("rd", 500)],
            required_solutions=("popsink", "rd"),
            require_cost=True,
        )

        self.assertEqual(set(report["solutions"]), {"popsink", "rd"})

    def test_strict_comparison_rejects_a_single_solution(self) -> None:
        with self.assertRaisesRegex(ValueError, "solutions"):
            compare_samples(
                [sample("rd", 500)],
                required_solutions=("popsink", "rd"),
                require_cost=True,
            )

    def test_strict_comparison_rejects_missing_cost(self) -> None:
        popsink = BenchmarkSample(**{**sample("popsink", 1000).__dict__, "cost_usd": None})
        rd = BenchmarkSample(**{**sample("rd", 500).__dict__, "cost_usd": None})

        with self.assertRaisesRegex(ValueError, "cost"):
            compare_samples(
                [popsink, rd],
                required_solutions=("popsink", "rd"),
                require_cost=True,
            )

    def test_comparison_rejects_missing_position_fingerprint_when_peer_has_one(self) -> None:
        popsink = BenchmarkSample(
            **{**sample("popsink", 1000).__dict__, "position_fingerprint": "sha256:a"}
        )
        rd = BenchmarkSample(**{**sample("rd", 500).__dict__, "position_fingerprint": None})

        with self.assertRaises(ValueError):
            compare_samples([popsink, rd])

    def test_comparison_requires_exact_position_fingerprint(self) -> None:
        popsink = BenchmarkSample(**{**sample("popsink", 1000).__dict__, "position_fingerprint": None})
        rd = BenchmarkSample(**{**sample("rd", 500).__dict__, "position_fingerprint": None})

        with self.assertRaises(ValueError):
            compare_samples([popsink, rd])

    def test_sample_rejects_negative_measurements(self) -> None:
        with self.assertRaises(ValueError):
            BenchmarkSample(**{**sample("rd", 500).__dict__, "elapsed_ms": -1})

    def test_continuous_log_becomes_safe_benchmark_samples(self) -> None:
        records = [
            {
                "event": "capture_poll",
                "status": "published",
                "event_count": 2,
                "window": {
                    "receiver": "R2",
                    "start_sequence": 100,
                    "end_sequence": 109,
                },
                "metrics": {
                    "errors": 0,
                    "last_poll": {
                        "poll_ms": 12.5,
                        "payload_bytes": 320,
                        "lag_sequences": 4,
                    },
                },
            },
            {
                "event": "capture_poll",
                "status": "idle",
                "event_count": 0,
                "window": None,
                "metrics": {"errors": 0, "last_poll": None},
            },
        ]

        samples = samples_from_continuous_log(
            records,
            solution="rd",
            phase="source_to_raw",
            table="SALES.CNTR",
            contract_version="as400-raw-v1",
            position_fingerprint="sha256:window",
        )

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].event_count, 2)
        self.assertEqual(samples[0].payload_bytes, 320)
        self.assertEqual(samples[0].elapsed_ms, 12.5)
        self.assertEqual(samples[0].lag_sequences, 4)
        self.assertEqual(samples[0].position_fingerprint, "sha256:window")

    def test_continuous_log_requires_a_matching_window(self) -> None:
        with self.assertRaises(ValueError):
            samples_from_continuous_log(
                [
                    {
                        "event": "capture_poll",
                        "status": "published",
                        "event_count": 1,
                        "window": {
                            "receiver": "R2",
                            "start_sequence": 100,
                            "end_sequence": 109,
                        },
                        "metrics": {
                            "errors": 0,
                            "last_poll": {
                                "poll_ms": 10,
                                "payload_bytes": 10,
                                "lag_sequences": 0,
                            },
                        },
                    }
                ],
                solution="rd",
                phase="source_to_raw",
                table="SALES.CNTR",
                contract_version="as400-raw-v1",
                start_sequence=200,
                end_sequence=209,
            )


if __name__ == "__main__":
    unittest.main()
