"""Lag measured across a chain of receivers, not just inside one.

continuous.py:230 returns None as soon as the cursor and the tail sit on
different receivers, because sequence numbers are only comparable inside one
receiver. That is correct but it blanks the lag exactly when the reader is
most behind. Measured 2026-08-27 (rj-tail4): cursor DEMOJRN3780, tail
DEMOJRN3781, last_lag_sequences=null, so both the probe policy and the
catch-up window sizing fell back to their defaults.

The catalogue already carries first_sequence and last_sequence per receiver
and is ordered oldest-to-newest, so the cumulative distance is computable
without any new source data.
"""

from __future__ import annotations

import unittest

from quadringent.continuous import ReceiverSnapshot, chain_lag
from quadringent.contract import JournalPosition


def _r(name: str, first: int | None, last: int | None, status: str = "DETACHED"):
    return ReceiverSnapshot(
        receiver_library="DEMOLIB", receiver=name,
        first_sequence=first, last_sequence=last, status=status,
    )


CHAIN = [
    _r("DEMOJRN3779", 250_000_000, 255_000_000),
    _r("DEMOJRN3780", 255_000_001, 260_500_000),
    _r("DEMOJRN3781", 260_500_001, 265_378_971, status="ATTACHED"),
]


class SameReceiverTests(unittest.TestCase):
    def test_same_receiver_is_a_plain_difference(self) -> None:
        lag = chain_lag(JournalPosition("DEMOJRN3781", 265_000_000), CHAIN)
        self.assertEqual(lag, 265_378_971 - 265_000_000)

    def test_caught_up_is_zero(self) -> None:
        self.assertEqual(chain_lag(JournalPosition("DEMOJRN3781", 265_378_971), CHAIN), 0)


class AcrossReceiversTests(unittest.TestCase):
    def test_one_rotation_behind_sums_both_parts(self) -> None:
        """Cursor on 3780, tail on 3781: remainder of 3780 plus start of 3781."""

        lag = chain_lag(JournalPosition("DEMOJRN3780", 260_173_144), CHAIN)
        expected = (260_500_000 - 260_173_144) + (265_378_971 - 260_500_001 + 1)
        self.assertEqual(lag, expected)

    def test_two_rotations_behind_includes_the_whole_middle_receiver(self) -> None:
        lag = chain_lag(JournalPosition("DEMOJRN3779", 254_000_000), CHAIN)
        expected = (
            (255_000_000 - 254_000_000)
            + (260_500_000 - 255_000_001 + 1)
            + (265_378_971 - 260_500_001 + 1)
        )
        self.assertEqual(lag, expected)

    def test_the_measured_case_is_no_longer_none(self) -> None:
        """rj-tail4: this returned None and blanked the probe policy."""

        self.assertIsNotNone(chain_lag(JournalPosition("DEMOJRN3780", 260_153_145), CHAIN))


class FailClosedTests(unittest.TestCase):
    def test_a_cursor_on_an_unknown_receiver_is_refused(self) -> None:
        self.assertIsNone(chain_lag(JournalPosition("DEMOJRN9999", 1), CHAIN))

    def test_an_empty_catalogue_is_refused(self) -> None:
        self.assertIsNone(chain_lag(JournalPosition("DEMOJRN3780", 1), []))

    def test_no_cursor_is_refused(self) -> None:
        self.assertIsNone(chain_lag(None, CHAIN))

    def test_a_receiver_without_bounds_is_refused(self) -> None:
        broken = [_r("DEMOJRN3780", None, None), _r("DEMOJRN3781", 1, 100)]
        self.assertIsNone(chain_lag(JournalPosition("DEMOJRN3780", 1), broken))

    def test_a_cursor_ahead_of_its_receiver_end_is_refused(self) -> None:
        """A cursor past the receiver's own last sequence is a broken position."""

        self.assertIsNone(chain_lag(JournalPosition("DEMOJRN3780", 260_500_001), CHAIN))

    def test_a_cursor_after_the_tail_receiver_is_refused(self) -> None:
        cursor = JournalPosition("DEMOJRN3781", 265_378_972)
        self.assertIsNone(chain_lag(cursor, CHAIN))


class CompatibilityTests(unittest.TestCase):
    def test_single_receiver_catalogue_still_works(self) -> None:
        one = [_r("DEMOJRN3781", 260_500_001, 265_378_971, status="ATTACHED")]
        lag = chain_lag(JournalPosition("DEMOJRN3781", 265_000_000), one)
        self.assertEqual(lag, 378_971)


if __name__ == "__main__":
    unittest.main()


class EffectiveLagTests(unittest.TestCase):
    """The loop helper: same-receiver difference first, chain fallback after."""

    def test_same_receiver_keeps_the_existing_behaviour(self) -> None:
        from quadringent.continuous import _effective_lag

        cursor = JournalPosition("DEMOJRN3781", 265_000_000)
        tail = JournalPosition("DEMOJRN3781", 265_378_971)
        self.assertEqual(_effective_lag(cursor, tail, CHAIN), 378_971)

    def test_different_receivers_now_fall_back_to_the_chain(self) -> None:
        from quadringent.continuous import _effective_lag

        cursor = JournalPosition("DEMOJRN3780", 260_173_144)
        tail = JournalPosition("DEMOJRN3781", 265_378_971)
        expected = (260_500_000 - 260_173_144) + (265_378_971 - 260_500_001 + 1)
        self.assertEqual(_effective_lag(cursor, tail, CHAIN), expected)

    def test_an_untrustworthy_chain_still_yields_none(self) -> None:
        from quadringent.continuous import _effective_lag

        cursor = JournalPosition("DEMOJRN9999", 1)
        tail = JournalPosition("DEMOJRN3781", 265_378_971)
        self.assertIsNone(_effective_lag(cursor, tail, CHAIN))

    def test_no_tail_yields_none(self) -> None:
        from quadringent.continuous import _effective_lag

        self.assertIsNone(
            _effective_lag(JournalPosition("DEMOJRN3780", 260_173_144), None, CHAIN)
        )
