"""Retryable window exceptions must live where every deployment mounts them.

Importing SqlWindowIncomplete from sql_window into java_worker broke the
RetrieveJournal jobs: their YAML mounts continuous.py, java_worker.py and
java_catalog.py but not sql_window.py, so the pod died on
ModuleNotFoundError (example-corp-rj-tail1, 2026-08-27).
"""

from __future__ import annotations

import unittest


class ExceptionPlacementTests(unittest.TestCase):
    def test_the_exception_is_defined_in_continuous(self) -> None:
        from quadringent.continuous import SqlWindowIncomplete

        self.assertTrue(issubclass(SqlWindowIncomplete, RuntimeError))

    def test_sql_window_still_exports_it_for_existing_callers(self) -> None:
        from quadringent.continuous import SqlWindowIncomplete as base
        from quadringent.sql_window import SqlWindowIncomplete as reexported

        self.assertIs(base, reexported)

    def test_timeout_is_reachable_from_both_places_too(self) -> None:
        from quadringent.continuous import SqlWindowTimeout as base
        from quadringent.sql_window import SqlWindowTimeout as reexported

        self.assertIs(base, reexported)

    def test_java_worker_has_no_module_level_sql_window_import(self) -> None:
        """A deferred import inside a SQL-only method is fine: it is never
        reached by the RetrieveJournal path, which does not mount sql_window.
        A module-level import would break that path at load time."""

        from quadringent import java_worker

        with open(java_worker.__file__, encoding="utf-8") as handle:
            module_level = [
                line for line in handle.read().splitlines()
                if line.startswith("from .sql_window") or line.startswith("import .sql_window")
            ]
        self.assertEqual(module_level, [])

    def test_java_catalog_has_no_module_level_sql_window_import(self) -> None:
        from quadringent import java_catalog

        with open(java_catalog.__file__, encoding="utf-8") as handle:
            module_level = [
                line for line in handle.read().splitlines()
                if line.startswith("from .sql_window")
            ]
        self.assertEqual(module_level, [])


if __name__ == "__main__":
    unittest.main()
