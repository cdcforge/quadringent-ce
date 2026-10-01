"""The Python relay must not silently drop worker diagnostic lines.

Found 2026-08-27: java_worker.py relays only lines starting with
`window_progress` or `retrieve_start`. Pagination instrumentation added to the
Java scan loop (`page_start` / `page_done`) was emitted by the worker and
dropped by the relay, so the diagnostic looked like it had not run at all.
Any future instrumentation would be invisible the same way.
"""

from __future__ import annotations

import unittest

from quadringent.java_worker import is_relayable_worker_line, worker_line_phase


class RelayedPhasesTests(unittest.TestCase):
    def test_the_existing_phases_are_still_relayed(self) -> None:
        self.assertTrue(is_relayable_worker_line("window_progress seen=1 decoded=0"))
        self.assertTrue(is_relayable_worker_line("retrieve_start receiver=DEMOJRN3780"))

    def test_pagination_lines_are_relayed(self) -> None:
        self.assertTrue(is_relayable_worker_line("page_start index=1 position=260153145"))
        self.assertTrue(is_relayable_worker_line("page_done index=1 elapsed_ms=4300"))

    def test_stalled_stack_lines_are_relayed(self) -> None:
        self.assertTrue(is_relayable_worker_line("stalled_stack thread=as400-retrieve"))
        self.assertTrue(is_relayable_worker_line("stalled_frame 0 java.net.SocketInputStream.read"))

    def test_the_phase_name_is_the_first_token(self) -> None:
        self.assertEqual(worker_line_phase("page_done index=2 elapsed_ms=17"), "page_done")
        self.assertEqual(worker_line_phase("retrieve_start receiver=X"), "retrieve_start")

    def test_protocol_control_lines_are_not_relayed_as_diagnostics(self) -> None:
        """window_done and summary drive the protocol; they keep their own path."""

        self.assertFalse(is_relayable_worker_line("window_done"))
        self.assertFalse(is_relayable_worker_line("summary seen=3 decoded=3"))
        self.assertFalse(is_relayable_worker_line("worker_ready"))

    def test_unknown_noise_is_not_relayed(self) -> None:
        self.assertFalse(is_relayable_worker_line("SLF4J: something"))
        self.assertFalse(is_relayable_worker_line(""))


if __name__ == "__main__":
    unittest.main()
