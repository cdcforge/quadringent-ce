"""Isochrone comparison on a shared identity instead of the journal sequence.

Reproduces the measured G5 false negative: Popsink/Debezium exposes
``source.sequence`` on a different scale than IBM i ``SEQUENCE_NUMBER``
(img2 2026-08-26: delta 5..54, 16 distinct values, never constant), so
joining on receiver/sequence/op reports missing+extra for events that both
systems actually captured.
"""

from __future__ import annotations

import unittest

from quadringent_research.isochrone import compare_key_sets, technical_key
from quadringent_research.isochrone_temporal import (
    commit_timestamp_to_epoch_ms,
    compare_event_multisets,
    key_from_popsink_record_temporal,
    key_from_rd_record_temporal,
    sequence_offset_profile,
    temporal_key,
)

# img2 band: 23 events, CEST 22:45:18.021600 .. 22:45:23.947264
IMG2_BASE_MS = 1787690718021
IMG2_N = 23
# non-constant offsets actually observed between the two scales
IMG2_OFFSETS = [5, 7, 9, 12, 13, 15, 18, 20, 22, 25, 27, 29, 31, 33, 36, 38, 40, 42, 45, 47, 50, 52, 54]


def _rd_records() -> list[dict]:
    return [
        {
            "journal_receiver": "DEMOJRN3763",
            "journal_sequence": 178624211 + index * 100,
            "operation": "c",
            "commit_timestamp": f"2026-08-25T22:45:18.{21600 + index * 1000:06d}",
        }
        for index in range(IMG2_N)
    ]


def _popsink_records() -> list[dict]:
    """Same events, same commit instants, shifted sequence scale."""

    return [
        {
            "op": "c",
            "source": {
                "receiver": "DEMOJRN3763",
                "sequence": 178624211 + index * 100 - IMG2_OFFSETS[index],
                "ts_ms": IMG2_BASE_MS + index,
            },
        }
        for index in range(IMG2_N)
    ]


class SequenceKeyIsAFalseNegativeTests(unittest.TestCase):
    def test_sequence_key_reports_missing_and_extra_for_captured_events(self) -> None:
        rd = {technical_key(r["journal_receiver"], r["journal_sequence"], r["operation"]) for r in _rd_records()}
        popsink = {
            technical_key(r["source"]["receiver"], r["source"]["sequence"], r["op"])
            for r in _popsink_records()
        }
        matrix = compare_key_sets(rd, popsink)
        self.assertEqual(matrix.rd_count, IMG2_N)
        self.assertEqual(matrix.popsink_count, IMG2_N)
        self.assertEqual(matrix.overlap, 0)
        self.assertEqual(matrix.extra, IMG2_N)
        self.assertEqual(matrix.missing, IMG2_N)


class TemporalKeyTests(unittest.TestCase):
    def test_commit_timestamp_truncates_microseconds_to_milliseconds(self) -> None:
        self.assertEqual(
            commit_timestamp_to_epoch_ms("2026-08-25T22:45:18.021600", offset_hours=2),
            IMG2_BASE_MS,
        )

    def test_temporal_key_matches_all_events_across_both_scales(self) -> None:
        rd = [key_from_rd_record_temporal(r, offset_hours=2) for r in _rd_records()]
        popsink = [key_from_popsink_record_temporal(r) for r in _popsink_records()]
        matrix = compare_event_multisets(rd, popsink)
        self.assertEqual(matrix.rd_count, IMG2_N)
        self.assertEqual(matrix.popsink_count, IMG2_N)
        self.assertEqual(matrix.overlap, IMG2_N)
        self.assertEqual(matrix.extra, 0)
        self.assertEqual(matrix.missing, 0)

    def test_operation_still_separates_events_at_the_same_instant(self) -> None:
        rd = [temporal_key(IMG2_BASE_MS, "c"), temporal_key(IMG2_BASE_MS, "u_after")]
        popsink = [temporal_key(IMG2_BASE_MS, "c")]
        matrix = compare_event_multisets(rd, popsink)
        self.assertEqual(matrix.overlap, 1)
        self.assertEqual(matrix.extra, 1)
        self.assertEqual(matrix.missing, 0)


class MultisetTests(unittest.TestCase):
    def test_simultaneous_events_are_not_collapsed(self) -> None:
        """Two events in the same millisecond are two events, not one."""

        rd = [temporal_key(IMG2_BASE_MS, "c")] * 3
        popsink = [temporal_key(IMG2_BASE_MS, "c")] * 3
        matrix = compare_event_multisets(rd, popsink)
        self.assertEqual(matrix.rd_count, 3)
        self.assertEqual(matrix.popsink_count, 3)
        self.assertEqual(matrix.overlap, 3)

    def test_a_dropped_duplicate_is_reported_as_missing(self) -> None:
        rd = [temporal_key(IMG2_BASE_MS, "c")] * 2
        popsink = [temporal_key(IMG2_BASE_MS, "c")] * 3
        matrix = compare_event_multisets(rd, popsink)
        self.assertEqual(matrix.overlap, 2)
        self.assertEqual(matrix.missing, 1)
        self.assertEqual(matrix.extra, 0)


class SequenceDiagnosticTests(unittest.TestCase):
    def test_offset_profile_reports_the_measured_non_constant_shift(self) -> None:
        profile = sequence_offset_profile(
            _rd_records(), _popsink_records(), offset_hours=2
        )
        self.assertEqual(profile["n"], IMG2_N)
        self.assertEqual(profile["delta_min"], 5)
        self.assertEqual(profile["delta_max"], 54)
        self.assertEqual(profile["distinct_deltas"], len(set(IMG2_OFFSETS)))
        self.assertFalse(profile["constant"])
        self.assertEqual(profile["native_sequence_matches"], 0)

    def test_constant_offset_is_flagged_as_constant(self) -> None:
        rd = _rd_records()[:3]
        popsink = [
            {
                "op": "c",
                "source": {
                    "receiver": "DEMOJRN3763",
                    "sequence": r["journal_sequence"] - 7,
                    "ts_ms": commit_timestamp_to_epoch_ms(r["commit_timestamp"], offset_hours=2),
                },
            }
            for r in rd
        ]
        profile = sequence_offset_profile(rd, popsink, offset_hours=2)
        self.assertTrue(profile["constant"])
        self.assertEqual(profile["delta_min"], 7)
        self.assertEqual(profile["delta_max"], 7)


if __name__ == "__main__":
    unittest.main()


class ComparabilityTests(unittest.TestCase):
    """A band outside the reference's retention is not a completeness failure."""

    def test_band_inside_coverage_is_comparable(self) -> None:
        from quadringent_research.isochrone_temporal import band_comparability

        verdict = band_comparability(
            band_low_ms=1787690718021,
            band_high_ms=1787690723947,
            coverage_low_ms=1787589770912,
            coverage_high_ms=1787777612384,
        )
        self.assertTrue(verdict["comparable"])
        self.assertEqual(verdict["reason"], "band inside reference coverage")

    def test_band_before_coverage_is_not_comparable(self) -> None:
        from quadringent_research.isochrone_temporal import band_comparability

        verdict = band_comparability(
            band_low_ms=1787569610243,
            band_high_ms=1787569623310,
            coverage_low_ms=1787589770912,
            coverage_high_ms=1787777612384,
        )
        self.assertFalse(verdict["comparable"])
        self.assertEqual(verdict["reason"], "band starts before reference coverage")

    def test_empty_reference_is_not_comparable(self) -> None:
        from quadringent_research.isochrone_temporal import band_comparability

        verdict = band_comparability(
            band_low_ms=1, band_high_ms=2,
            coverage_low_ms=None, coverage_high_ms=None,
        )
        self.assertFalse(verdict["comparable"])
        self.assertEqual(verdict["reason"], "reference table is empty")

    def test_non_comparable_band_must_not_be_read_as_missing_events(self) -> None:
        """extra>0 with missing==0 outside coverage means the reference never
        delivered, not that the R&D tool lost anything."""

        from quadringent_research.isochrone_temporal import classify_matrix

        verdict = classify_matrix(
            overlap=0, extra=62, missing=0, comparable=False,
        )
        self.assertEqual(verdict, "NOT_COMPARABLE")

    def test_complete_match_is_pass(self) -> None:
        from quadringent_research.isochrone_temporal import classify_matrix

        self.assertEqual(
            classify_matrix(overlap=23, extra=0, missing=0, comparable=True), "PASS"
        )

    def test_rd_losing_an_event_is_a_real_fail(self) -> None:
        from quadringent_research.isochrone_temporal import classify_matrix

        self.assertEqual(
            classify_matrix(overlap=22, extra=0, missing=1, comparable=True), "FAIL"
        )


class ReferenceRoutingTests(unittest.TestCase):
    """Each R&D window must be compared against its own source table."""

    def test_reference_table_follows_the_payload_source_table(self) -> None:
        from quadringent_research.isochrone_temporal import reference_table_for

        self.assertEqual(reference_table_for("ADDRS1", {"ADDRS1", "SALE"}), "ADDRS1")
        self.assertEqual(reference_table_for("SALE", {"ADDRS1", "SALE"}), "SALE")

    def test_missing_reference_table_is_not_comparable(self) -> None:
        from quadringent_research.isochrone_temporal import reference_table_for

        self.assertIsNone(reference_table_for("PLACES", {"ADDRS1", "SALE"}))

    def test_source_table_is_validated_as_an_identifier(self) -> None:
        from quadringent_research.isochrone_temporal import reference_table_for

        with self.assertRaises(ValueError):
            reference_table_for("SALE; DROP TABLE X", {"SALE"})

    def test_a_window_mixing_source_tables_is_rejected(self) -> None:
        from quadringent_research.isochrone_temporal import single_source_table

        self.assertEqual(single_source_table(["SALE", "SALE"]), "SALE")
        with self.assertRaises(ValueError):
            single_source_table(["SALE", "ADDRS1"])


class OffsetSelectionTests(unittest.TestCase):
    """The offset probe must maximise alignment, not reference volume."""

    def test_offset_is_chosen_by_overlap_not_by_row_count(self) -> None:
        from quadringent_research.isochrone_temporal import best_offset_by_overlap

        rd_by_offset = {
            0: [temporal_key(1000, "c")],
            2: [temporal_key(7_201_000, "c")],
        }
        popsink_by_offset = {
            # offset 0 pulls a big but unaligned band
            0: [temporal_key(1500, "c"), temporal_key(1600, "c"), temporal_key(1700, "c")],
            # offset 2 pulls one row that actually matches
            2: [temporal_key(7_201_000, "c")],
        }
        chosen = best_offset_by_overlap(rd_by_offset, popsink_by_offset)
        self.assertEqual(chosen, 2)

    def test_ties_prefer_the_smaller_offset(self) -> None:
        from quadringent_research.isochrone_temporal import best_offset_by_overlap

        rd_by_offset = {0: [temporal_key(10, "c")], 2: [temporal_key(20, "c")]}
        popsink_by_offset = {0: [temporal_key(10, "c")], 2: [temporal_key(20, "c")]}
        self.assertEqual(best_offset_by_overlap(rd_by_offset, popsink_by_offset), 0)

    def test_no_alignment_anywhere_returns_none(self) -> None:
        from quadringent_research.isochrone_temporal import best_offset_by_overlap

        rd_by_offset = {0: [temporal_key(10, "c")]}
        popsink_by_offset = {0: [temporal_key(99, "c")]}
        self.assertIsNone(best_offset_by_overlap(rd_by_offset, popsink_by_offset))
