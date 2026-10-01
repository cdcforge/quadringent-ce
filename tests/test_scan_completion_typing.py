"""An uncertified scan must be a typed, retryable failure.

Measured on example-corp-lag-step2 (2026-08-26, second attempt): the run died on
`RuntimeError: bounded DISPLAY_JOURNAL did not certify scan completion`. The
capture loop only retries SqlWindowTimeout and SqlWindowIncomplete, so a bare
RuntimeError ends the worker even though the fail-closed contract is intact —
the checkpoint did not advance, so the window is safe to retry smaller.
"""

from __future__ import annotations

import unittest

from quadringent.sql_window import SqlWindowIncomplete


class ScanCompletionTypingTests(unittest.TestCase):
    def test_uncertified_scan_raises_the_retryable_type(self) -> None:
        from quadringent import java_worker

        source = java_worker.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn(
            'raise RuntimeError("bounded DISPLAY_JOURNAL did not certify scan completion")',
            text,
            "an uncertified scan must raise SqlWindowIncomplete, not a bare RuntimeError",
        )
        self.assertIn(
            'raise SqlWindowIncomplete("bounded DISPLAY_JOURNAL did not certify scan completion")',
            text,
        )

    def test_the_retryable_type_is_still_a_runtime_error(self) -> None:
        """Callers that catch RuntimeError keep working."""

        self.assertTrue(issubclass(SqlWindowIncomplete, RuntimeError))


if __name__ == "__main__":
    unittest.main()
