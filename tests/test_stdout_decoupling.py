"""The Java stdout pipe must be drained independently of the consumer.

Measured 2026-08-27 with markers placed on every write between the retrieve and
window_done: 19 windows reached final_position_done, 11 reached summary_written,
6 reached window_flushed, 3 returned. The abandonment point is not fixed, it
moves with every write, which is the signature of a producer blocked on a full
pipe rather than a hang at one instruction.

stderr already has a draining thread. stdout did not: it was read inline by the
same loop that parses and logs, so any slowness downstream backed up into the
pipe and stalled the JVM mid-write.
"""

from __future__ import annotations

import queue
import unittest

from quadringent.java_worker import LinePump


class _FakeStream:
    def __init__(self, lines, block_after=None):
        self._lines = list(lines)
        self._block_after = block_after
        self.reads = 0

    def readline(self):
        self.reads += 1
        if not self._lines:
            return ""
        return self._lines.pop(0)


class LinePumpTests(unittest.TestCase):
    def test_lines_are_available_in_order(self) -> None:
        pump = LinePump(_FakeStream(["a\n", "b\n", "c\n"]))
        pump.start()
        self.assertEqual(pump.next_line(timeout=1.0), "a\n")
        self.assertEqual(pump.next_line(timeout=1.0), "b\n")
        self.assertEqual(pump.next_line(timeout=1.0), "c\n")

    def test_the_pump_drains_ahead_of_the_consumer(self) -> None:
        """The producer must not wait for the consumer to ask."""

        stream = _FakeStream(["a\n", "b\n", "c\n"])
        pump = LinePump(stream)
        pump.start()
        pump.wait_until_drained(timeout=1.0)
        self.assertGreaterEqual(stream.reads, 3)

    def test_end_of_stream_is_reported(self) -> None:
        pump = LinePump(_FakeStream(["only\n"]))
        pump.start()
        self.assertEqual(pump.next_line(timeout=1.0), "only\n")
        self.assertEqual(pump.next_line(timeout=1.0), "")

    def test_a_timeout_raises_so_the_caller_can_fail_closed(self) -> None:
        pump = LinePump(_FakeStream([]), eof_marker=None)
        pump.start()
        with self.assertRaises(queue.Empty):
            pump.next_line(timeout=0.05)

    def test_stopping_is_idempotent(self) -> None:
        pump = LinePump(_FakeStream(["a\n"]))
        pump.start()
        pump.stop()
        pump.stop()


class WiringTests(unittest.TestCase):
    def test_the_worker_uses_the_pump(self) -> None:
        from pathlib import Path

        source = Path("src/quadringent/java_worker.py").read_text()
        self.assertIn("LinePump", source)
        self.assertIn("self._pump", source)


if __name__ == "__main__":
    unittest.main()
