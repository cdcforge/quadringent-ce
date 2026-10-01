"""The RJ loop must not refetch the catalogue on every poll.

Measured on example-corp-rj-tail5 (2026-08-27): catalog_ms was 1847 and 1931 on
the two successful polls, i.e. the catalogue was read again each time. The RJ
defaults are ttl_polls=3 / ttl_seconds=2, and an RJ poll takes 6 s when it
succeeds and up to the reader timeout when it hangs, so a 2 s TTL always
expires. The SQL path was already fixed to 30 polls / 60 s; the RJ path kept
the old defaults, and the catalogue shares the Java worker with RetrieveJournal.
"""

from __future__ import annotations

import unittest


class RjCatalogDefaultsTests(unittest.TestCase):
    def test_rj_capture_defaults_match_the_sql_path(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertIn('"AS400_RECEIVER_CATALOG_CACHE_POLLS", 30', source)
        self.assertIn('"AS400_RECEIVER_CATALOG_CACHE_SECONDS", "60"', source)

    def test_a_two_second_ttl_is_no_longer_the_default(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertNotIn('"AS400_RECEIVER_CATALOG_CACHE_SECONDS", "2"', source)
        self.assertNotIn("AS400_RECEIVER_CATALOG_CACHE_POLLS\", 3)", source)


class TailProbeAndAdaptivePollWiringTests(unittest.TestCase):
    """La sonde de tail et le sommeil oisif adaptatif doivent etre cables.

    Sonde activee par defaut (AS400_TAIL_PROBE=false pour revenir au
    comportement historique) ; le plancher d'attente oisive est ajustable
    via AS400_MIN_POLL_SECONDS (defaut 1 s).
    """

    def test_tail_probe_is_wired_and_can_be_disabled(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertIn("tail_probe=receiver_catalog.tail", source)
        self.assertIn('_flag("AS400_TAIL_PROBE", True)', source)

    def test_min_poll_seconds_env_override_is_wired(self) -> None:
        from pathlib import Path

        source = Path("scripts/as400_continuous_capture.py").read_text()
        self.assertIn('min_poll_seconds=float(os.environ.get("AS400_MIN_POLL_SECONDS", "1"))', source)


if __name__ == "__main__":
    unittest.main()
