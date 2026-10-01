from __future__ import annotations

import unittest
from unittest.mock import patch

from quadringent.continuous import CaptureWindow
from quadringent.contract import JournalPosition
from quadringent.java_worker import JavaWindowRunner


_HAS_SALE = "summary seen=1 decoded=1 elapsed_ms=1 scan_complete=true\n"


class FakeWorker:
    def __init__(self, output: str, sql_output: str = _HAS_SALE) -> None:
        self.output = output
        self.sql_output = sql_output
        self.requests: list[dict[str, object]] = []
        self.sql_requests: list[dict[str, object]] = []

    def process_window(self, request: dict[str, object]) -> str:
        self.requests.append(request)
        return self.output

    def sql_window(self, request: dict[str, object]) -> str:
        self.sql_requests.append(request)
        return self.sql_output


class JavaWindowRunnerTests(unittest.TestCase):
    def test_runner_rejects_success_without_a_scan_completion_marker(self) -> None:
        worker = FakeWorker(
            "summary seen=0 decoded=0 elapsed_ms=1 final_position=R2:100\n"
        )
        runner = JavaWindowRunner(
            worker, max_decoded_entries=1000, empty_probe=False
        )  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        with self.assertRaisesRegex(RuntimeError, "scan completion"):
            runner.capture(window)

        self.assertEqual(len(worker.requests), 1)
        self.assertEqual(worker.requests[0]["max_server_entries"], 11)
        self.assertEqual(worker.requests[0]["receiver"], "R2")

    def test_runner_reuses_one_worker_for_two_windows(self) -> None:
        worker = FakeWorker(
            "summary seen=0 decoded=0 elapsed_ms=1 final_position=R2:110 scan_complete=true\n"
        )
        runner = JavaWindowRunner(
            worker, max_decoded_entries=1000, empty_probe=False
        )  # type: ignore[arg-type]
        first = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )
        second = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 111),
            end=JournalPosition("R2", 120),
        )

        runner.capture(first)
        runner.capture(second)

        self.assertEqual(len(worker.requests), 2)
        self.assertEqual(worker.requests[1]["start_sequence"], 111)

    def test_runner_does_not_spawn_java_itself(self) -> None:
        worker = FakeWorker(
            "summary seen=0 decoded=0 elapsed_ms=1 final_position=R2:110 scan_complete=true\n"
        )
        runner = JavaWindowRunner(
            worker, max_decoded_entries=11, empty_probe=False
        )  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            captured = runner.capture(window)

        popen.assert_not_called()
        run.assert_not_called()
        self.assertIsNone(captured.manifest)
        self.assertEqual(worker.sql_requests, [])
        self.assertNotIn("checkpoint_file", worker.requests[0])

    def test_catch_up_window_uses_span_as_entry_limits(self) -> None:
        worker = FakeWorker(
            "summary seen=0 decoded=0 elapsed_ms=1 final_position=R2:150 scan_complete=true\n"
        )
        runner = JavaWindowRunner(worker, max_decoded_entries=10)  # type: ignore[arg-type]
        runner.observe_lag(1000)
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 101),
            end=JournalPosition("R2", 150),
        )

        runner.capture(window)

        request = worker.requests[0]
        self.assertEqual(request["max_server_entries"], 50)
        self.assertEqual(request["max_decoded_entries"], 50)
        self.assertNotIn("checkpoint_file", request)

    def test_runner_refuses_decoded_events_without_raw_artifacts(self) -> None:
        worker = FakeWorker(
            "summary seen=5 decoded=5 elapsed_ms=1 final_position=R2:110 scan_complete=true\n"
        )
        runner = JavaWindowRunner(worker, max_decoded_entries=1000)  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        with self.assertRaisesRegex(RuntimeError, "without raw"):
            runner.capture(window)

    def test_empty_sql_probe_skips_retrievejournal(self) -> None:
        worker = FakeWorker(
            "summary seen=9 decoded=9 elapsed_ms=1 final_position=R2:110 scan_complete=true\n",
            sql_output="summary seen=0 decoded=0 elapsed_ms=4 scan_complete=true\n",
        )
        runner = JavaWindowRunner(worker, max_decoded_entries=1000)  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        captured = runner.capture(window)

        self.assertEqual(len(worker.sql_requests), 1)
        self.assertEqual(worker.requests, [])
        self.assertEqual(captured.scanned_to, window.end)
        self.assertIsNone(captured.manifest)
        self.assertIsNone(captured.payload)

    def test_nonempty_sql_tail_refuses_missing_raw_without_retrieve_fallback(self) -> None:
        worker = FakeWorker(
            "summary seen=2 decoded=2 elapsed_ms=1 final_position=R2:110 scan_complete=true\n",
            sql_output="summary seen=2 decoded=2 elapsed_ms=4 scan_complete=true\n",
        )
        runner = JavaWindowRunner(worker, max_decoded_entries=1000)  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        with self.assertRaisesRegex(RuntimeError, "without raw"):
            runner.capture(window)

        self.assertEqual(len(worker.sql_requests), 1)
        self.assertEqual(worker.requests, [])

    def test_direct_retrieve_skips_sql_probe(self) -> None:
        worker = FakeWorker(
            "summary seen=2 decoded=2 elapsed_ms=4 final_position=R2:110 scan_complete=true\n",
        )
        runner = JavaWindowRunner(
            worker, max_decoded_entries=1000, empty_probe=False
        )  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        with self.assertRaisesRegex(RuntimeError, "without raw"):
            runner.capture(window)

        self.assertEqual(worker.sql_requests, [])
        self.assertEqual(len(worker.requests), 1)

    def test_multi_table_runner_skips_sql_probe(self) -> None:
        worker = FakeWorker(
            "summary seen=0 decoded=0 elapsed_ms=1 final_position=R2:110 scan_complete=true\n",
            sql_output="summary seen=0 decoded=0 elapsed_ms=4 scan_complete=true\n",
        )
        worker.tables = ("SALE", "CNTR")
        runner = JavaWindowRunner(worker, max_decoded_entries=1000)  # type: ignore[arg-type]
        window = CaptureWindow(
            receiver_library="QGPL",
            start=JournalPosition("R2", 100),
            end=JournalPosition("R2", 110),
        )

        captured = runner.capture(window)

        self.assertEqual(worker.sql_requests, [])
        self.assertEqual(len(worker.requests), 1)
        self.assertEqual(captured.scanned_to, window.end)


if __name__ == "__main__":
    unittest.main()
