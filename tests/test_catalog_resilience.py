"""The receiver catalogue must not be the single point of failure of a soak.

Measured on example-corp-lag-step2 (2026-08-26): the run died at 74 s on
`bounded IBM i reader timed out` raised by catalog.snapshot(), which the
capture loop did not guard. The catalogue was also refetched on every poll
because ttl_polls was 1, multiplying the exposure.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import ReceiverSnapshot
from quadringent.java_catalog import CachedReceiverCatalog


class _Flaky:
    def __init__(self, snapshots, failures_at=()):
        self.snapshots = snapshots
        self.failures_at = set(failures_at)
        self.calls = 0

    def snapshot(self, required_receiver=None):
        self.calls += 1
        if self.calls in self.failures_at:
            raise RuntimeError("bounded IBM i reader timed out")
        return self.snapshots


def _snap(seq):
    return [ReceiverSnapshot(
        receiver_library="DEMOLIB", receiver="DEMOJRN3775",
        first_sequence=1, last_sequence=seq, status="ATTACHED",
    )]


class StaleFallbackTests(unittest.TestCase):
    def test_a_failed_refetch_reuses_the_last_snapshot(self) -> None:
        clock = iter([0.0, 100.0, 200.0])
        inner = _Flaky(_snap(10), failures_at=(2,))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=1, ttl_seconds=0.0, clock=lambda: next(clock)
        )
        first = catalog.snapshot()
        self.assertEqual(first[0].last_sequence, 10)
        second = catalog.snapshot()
        self.assertEqual(second[0].last_sequence, 10)
        self.assertEqual(catalog.stale_count, 1)

    def test_a_failure_with_no_cache_still_raises(self) -> None:
        inner = _Flaky(_snap(10), failures_at=(1,))
        catalog = CachedReceiverCatalog(inner, ttl_polls=1, ttl_seconds=0.0)
        with self.assertRaises(RuntimeError):
            catalog.snapshot()

    def test_stale_reuse_is_bounded(self) -> None:
        """Serving a stale catalogue forever would hide a dead reader."""

        inner = _Flaky(_snap(10), failures_at=(2, 3, 4, 5))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=1, ttl_seconds=0.0, max_stale_reuse=2
        )
        catalog.snapshot()
        catalog.snapshot()
        catalog.snapshot()
        with self.assertRaises(RuntimeError):
            catalog.snapshot()

    def test_a_successful_refetch_clears_the_stale_streak(self) -> None:
        inner = _Flaky(_snap(10), failures_at=(2,))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=1, ttl_seconds=0.0, max_stale_reuse=1
        )
        catalog.snapshot()
        catalog.snapshot()
        catalog.snapshot()
        self.assertEqual(catalog.consecutive_stale, 0)


class RequiredReceiverTests(unittest.TestCase):
    """A cached tail window must not hide a still-online checkpoint receiver.

    Measured 2026-09-20 on example-corp: the checkpoint sat on DEMOJRN4115 while
    the bounded catalog returned only the newest eight receivers
    (DEMOJRN4143..4150). The planner refused a live position as 'missing from
    metadata'. A cache hit is only valid when it covers the required receiver.
    """

    def test_a_cache_without_the_required_receiver_refetches(self) -> None:
        clock = iter([0.0, 1.0])
        inner = _Flaky(_snap(10))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=30, ttl_seconds=60.0, clock=lambda: next(clock)
        )
        catalog.snapshot()
        catalog.snapshot(required_receiver="DEMOJRN4115")
        self.assertEqual(inner.calls, 2)

    def test_a_covering_cache_is_still_served(self) -> None:
        clock = iter([0.0, 1.0])
        inner = _Flaky(_snap(10))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=30, ttl_seconds=60.0, clock=lambda: next(clock)
        )
        catalog.snapshot()
        catalog.snapshot(required_receiver="DEMOJRN3775")
        self.assertEqual(inner.calls, 1)

    def test_stale_fallback_never_hides_the_required_receiver(self) -> None:
        clock = iter([0.0, 1.0])
        inner = _Flaky(_snap(10), failures_at=(2,))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=30, ttl_seconds=60.0, clock=lambda: next(clock)
        )
        catalog.snapshot()
        with self.assertRaises(RuntimeError):
            catalog.snapshot(required_receiver="DEMOJRN4115")


class TtlTests(unittest.TestCase):
    def test_a_long_ttl_avoids_refetching_every_poll(self) -> None:
        inner = _Flaky(_snap(10))
        catalog = CachedReceiverCatalog(
            inner, ttl_polls=30, ttl_seconds=60.0, clock=lambda: 0.0
        )
        for _ in range(10):
            catalog.snapshot()
        self.assertEqual(inner.calls, 1)


if __name__ == "__main__":
    unittest.main()
