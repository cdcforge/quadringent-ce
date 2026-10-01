from __future__ import annotations

import json
import unittest

from quadringent_research.isochrone import (
    IsochroneMatrix,
    compare_key_sets,
    filter_keys_to_window,
    key_from_popsink_record,
    key_from_rd_record,
    technical_key,
)


class IsochroneComparatorTests(unittest.TestCase):
    def test_partial_overlap_counts_intersection_only(self) -> None:
        rd = {
            technical_key("DEMOJRN3762", 100, "c"),
            technical_key("DEMOJRN3762", 101, "c"),
            technical_key("DEMOJRN3762", 102, "c"),
        }
        popsink = {
            technical_key("DEMOJRN3762", 101, "c"),
            technical_key("DEMOJRN3762", 102, "c"),
            technical_key("DEMOJRN3762", 103, "c"),
        }
        matrix = compare_key_sets(rd, popsink)
        self.assertEqual(matrix.overlap, 2)
        self.assertEqual(matrix.extra, 1)
        self.assertEqual(matrix.missing, 1)
        self.assertEqual(matrix.rd_count, 3)
        self.assertEqual(matrix.popsink_count, 3)

    def test_extra_rd_is_counted_when_popsink_lacks_keys(self) -> None:
        rd = {
            technical_key("DEMOJRN3762", 200, "c"),
            technical_key("DEMOJRN3762", 201, "u_after"),
        }
        popsink = {technical_key("DEMOJRN3762", 200, "c")}
        matrix = compare_key_sets(rd, popsink)
        self.assertEqual(matrix.overlap, 1)
        self.assertEqual(matrix.extra, 1)
        self.assertEqual(matrix.missing, 0)

    def test_missing_rd_is_counted_when_popsink_has_extra_keys(self) -> None:
        rd = {technical_key("DEMOJRN3762", 300, "c")}
        popsink = {
            technical_key("DEMOJRN3762", 300, "c"),
            technical_key("DEMOJRN3762", 301, "c"),
        }
        matrix = compare_key_sets(rd, popsink)
        self.assertEqual(matrix.overlap, 1)
        self.assertEqual(matrix.extra, 0)
        self.assertEqual(matrix.missing, 1)

    def test_disjoint_sets_measure_overlap_zero(self) -> None:
        rd = {technical_key("DEMOJRN3762", 400, "c")}
        popsink = {technical_key("DEMOJRN3762", 401, "c")}
        matrix = compare_key_sets(rd, popsink)
        self.assertEqual(matrix.overlap, 0)
        self.assertEqual(matrix.extra, 1)
        self.assertEqual(matrix.missing, 1)
        self.assertIsInstance(matrix.overlap, int)

    def test_empty_sets_still_measure_overlap_zero(self) -> None:
        matrix = compare_key_sets(set(), set())
        self.assertEqual(matrix.overlap, 0)
        self.assertEqual(matrix.extra, 0)
        self.assertEqual(matrix.missing, 0)
        self.assertTrue(matrix.overlap_sha256)
        self.assertTrue(matrix.extra_sha256)
        self.assertTrue(matrix.missing_sha256)

    def test_sha256_is_order_independent_and_has_no_business_fields(self) -> None:
        first = compare_key_sets(
            {technical_key("DEMOJRN3762", 12, "c"), technical_key("DEMOJRN3762", 11, "c")},
            {technical_key("DEMOJRN3762", 11, "c"), technical_key("DEMOJRN3762", 13, "c")},
        )
        second = compare_key_sets(
            {technical_key("DEMOJRN3762", 11, "c"), technical_key("DEMOJRN3762", 12, "c")},
            {technical_key("DEMOJRN3762", 13, "c"), technical_key("DEMOJRN3762", 11, "c")},
        )
        self.assertEqual(first.overlap_sha256, second.overlap_sha256)
        self.assertEqual(first.extra_sha256, second.extra_sha256)
        self.assertEqual(first.missing_sha256, second.missing_sha256)
        payload = json.dumps(first.to_record(), sort_keys=True)
        for forbidden in ("SDOM", "SCOD", "SSEQ", "SORD", "STYP", "SDISC", "SDATE", "after", "before"):
            self.assertNotIn(forbidden, payload)

    def test_rd_and_popsink_records_normalize_to_the_same_technical_key(self) -> None:
        rd_key = key_from_rd_record(
            {
                "event_id": "abc123",
                "journal_receiver": "DEMOJRN3762",
                "journal_sequence": 55,
                "operation": "c",
            }
        )
        popsink_key = key_from_popsink_record(
            {
                "op": "c",
                "source": {"receiver": "DEMOJRN3762", "sequence": 55},
            }
        )
        self.assertEqual(rd_key, popsink_key)
        self.assertEqual(rd_key, technical_key("DEMOJRN3762", 55, "c"))

    def test_window_filter_keeps_only_receiver_and_sequence_bounds(self) -> None:
        keys = {
            technical_key("DEMOJRN3762", 10, "c"),
            technical_key("DEMOJRN3762", 20, "c"),
            technical_key("DEMOJRN3762", 30, "c"),
            technical_key("DEMOJRN3761", 20, "c"),
        }
        filtered = filter_keys_to_window(
            keys,
            receiver="DEMOJRN3762",
            start_sequence=10,
            end_sequence=20,
        )
        self.assertEqual(
            filtered,
            {
                technical_key("DEMOJRN3762", 10, "c"),
                technical_key("DEMOJRN3762", 20, "c"),
            },
        )

    def test_matrix_type_exposes_only_counters_and_fingerprints(self) -> None:
        matrix = IsochroneMatrix(
            overlap=0,
            extra=0,
            missing=0,
            overlap_sha256="a" * 64,
            extra_sha256="b" * 64,
            missing_sha256="c" * 64,
            rd_count=0,
            popsink_count=0,
        )
        self.assertEqual(
            set(matrix.to_record()),
            {
                "overlap",
                "extra",
                "missing",
                "overlap_sha256",
                "extra_sha256",
                "missing_sha256",
                "rd_count",
                "popsink_count",
            },
        )


if __name__ == "__main__":
    unittest.main()
