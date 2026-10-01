from __future__ import annotations

import unittest

from quadringent_research.popsink_readonly_lag import evaluate_tail, parse_statistics_text

# Le texte analysé est la sortie de l'outil historique : « SALES » y est
# une donnée d'entrée, pas une valeur du site.
LIBRARY = "SALES"
WATCHED = ("ORDER", "SALE", "ADDRS1", "CUSTOM1")


_SAMPLE = """
INFO SALES.ORDER -> ORDER Statistics [periodRecords=0, periodSec=60.0, recordsPerSec=0.00, totalRecords=3572034, totalBatches=267, totalErrors=0, topicLag=0, bytesPerSec=0.0k, ETA=0h 0m 0s]
INFO SALES.SALE -> SALE Statistics [periodRecords=100, periodSec=30.0, recordsPerSec=3.33, totalRecords=12, totalBatches=1, totalErrors=0, topicLag=42448567, bytesPerSec=1.0k, ETA=1h 0m 0s]
INFO SALES.ADDRS1 -> ADDRS1 Statistics [periodRecords=50, periodSec=30.0, recordsPerSec=1.66, totalRecords=12, totalBatches=1, totalErrors=0, topicLag=47030657, bytesPerSec=1.0k, ETA=1h 0m 0s]
INFO SALES.CUSTOM1 -> CUSTOM1 Statistics [periodRecords=0, periodSec=60.0, recordsPerSec=0.00, totalRecords=1, totalBatches=1, totalErrors=0, topicLag=0, bytesPerSec=0.0k, ETA=0h 0m 0s]
"""


class PopsinkReadonlyLagTests(unittest.TestCase):
    def test_catchup_is_not_calm(self) -> None:
        stats = parse_statistics_text(_SAMPLE, library=LIBRARY)
        result = evaluate_tail(stats, watched_tables=WATCHED)
        self.assertEqual(stats["ORDER"]["topicLag"], "0")
        self.assertFalse(result["calm"])
        self.assertEqual(result["lags"]["SALE"], 42448567)
        self.assertEqual(result["lags"]["ADDRS1"], 47030657)

    def test_tail_is_calm_when_watched_lags_are_at_or_below_threshold(self) -> None:
        text = _SAMPLE.replace("topicLag=42448567", "topicLag=0").replace(
            "topicLag=47030657", "topicLag=12"
        )
        result = evaluate_tail(parse_statistics_text(text, library=LIBRARY), watched_tables=WATCHED, max_lag=1000)
        self.assertTrue(result["calm"])
        self.assertEqual(result["lags"]["ADDRS1"], 12)

    def test_missing_watched_table_is_not_calm(self) -> None:
        result = evaluate_tail(parse_statistics_text("INFO SALES.CNTR -> CNTR Statistics [topicLag=0]", library=LIBRARY), watched_tables=WATCHED + ("CNTR",))
        self.assertFalse(result["calm"])


if __name__ == "__main__":
    unittest.main()
