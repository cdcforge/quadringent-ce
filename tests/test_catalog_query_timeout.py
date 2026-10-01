"""The receiver-catalogue query must be time-bounded.

Root cause found 2026-08-27 after ten RJ runs. ReadOnlyReceiverCatalog
.writeReceivers called executeQuery() with no setQueryTimeout, unlike
displayJournalSql which always sets one. QSYS2.JOURNAL_RECEIVER_INFO is
ordered by ATTACH_TIMESTAMP DESC over every receiver of the journal, so the
call can block with no client-side bound; only the Python reader deadline
eventually cut it.

Evidence: failures always ended on `catalog_write` then silence with
catalog_ms 0.0, no Java TimeoutException was ever reached (zero stalled
stacks captured), and cutting the row count from 32 to 8 moved the success
rate from 33% to 50%.
"""

from __future__ import annotations

from pathlib import Path
import unittest

SOURCE = Path("java/src/main/java/io/quadringent/as400/ReadOnlyReceiverCatalog.java")


class CatalogQueryTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SOURCE.read_text(encoding="utf-8")

    def test_the_catalogue_statement_sets_a_query_timeout(self) -> None:
        self.assertIn("setQueryTimeout", self.text)

    def test_the_timeout_is_applied_before_execute(self) -> None:
        timeout_at = self.text.index("setQueryTimeout")
        execute_at = self.text.index("executeQuery")
        self.assertLess(timeout_at, execute_at)

    def test_the_timeout_is_configurable_and_bounded(self) -> None:
        self.assertIn("AS400_CATALOG_QUERY_TIMEOUT_SECONDS", self.text)

    def test_a_timed_out_catalogue_is_a_typed_failure(self) -> None:
        """SQLTimeoutException must not surface as an opaque blocking read."""

        self.assertIn("SQLTimeoutException", self.text)


if __name__ == "__main__":
    unittest.main()
