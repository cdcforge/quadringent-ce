from __future__ import annotations

import io
import time
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from quadringent.java_catalog import WorkerReceiverCatalog, parse_catalog_output
from quadringent.java_worker import (
    PersistentJavaWorker,
    SourceConnectFailedError,
    parse_capture_tables,
)
from quadringent.source_gate import (
    FileSourceGate,
    SourceAuthenticationBlockedError,
    SourceUnavailablePausedError,
    IbmiUserDisabledError,
)
import tempfile


CATALOG_OUTPUT = """as400-receiver-catalog-v1
receiver\tQGPL\tR2\tONLINE\t100\t110
receiver\tQGPL\tR10\tONLINE\t111\t130
"""


class ScriptedProcess:
    def __init__(self, replies: list[list[str]]) -> None:
        self.stdin = self
        self.stdout = self
        self.stderr = io.StringIO()
        self.writes: list[str] = []
        self._replies = [list(item) for item in replies]
        self._queue = self._replies.pop(0)
        self.returncode = None

    def write(self, data: str) -> int:
        self.writes.append(data)
        if self._replies:
            self._queue.extend(self._replies.pop(0))
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def readline(self) -> str:
        # A real pipe blocks when there is nothing to read yet; it only reports
        # EOF once the writer is gone. Returning "" on an empty queue made the
        # fake claim EOF while more replies were still to come, which a
        # continuously draining reader notices immediately.
        # EOF only once the writer is gone, like a real pipe. The generous bound
        # simply keeps a stuck test from hanging forever; it must stay well above
        # any reader deadline a test sets.
        deadline = time.monotonic() + 30.0
        while not self._queue:
            if self.returncode is not None:
                return ""
            if time.monotonic() > deadline:
                return ""
            time.sleep(0.005)
        return self._queue.pop(0)

    def fileno(self) -> int:
        raise io.UnsupportedOperation("no fd")

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.returncode = 1


def _worker(test_case: unittest.TestCase) -> PersistentJavaWorker:
    """Un worker dont le nettoyage est garanti à la fin du test.

    ``LinePump`` (java_worker.py) démarre un thread démon qui lit en boucle
    ``ScriptedProcess.readline()`` ; ce double de test ne quitte sa propre
    boucle d'attente (jusqu'à 30 s) que lorsque ``returncode`` est posé —
    ce que ``PersistentJavaWorker.close()`` fait. Sans cet appel, le thread
    reste vivant bien après la fin du test et continue d'appeler
    ``time.sleep`` réel, ce qui peut fausser un test sans rapport qui
    substitue temporairement ``time.sleep`` global (ex. ``test_verifier_wait.py``).
    ``close()`` est sans effet si le worker est déjà fermé.
    """

    worker = PersistentJavaWorker(
        java="java",
        classpath="classpath",
        host="ibmi-dev",
        user="catalog-user",
        schema="SALES",
        table="SALE",
        timeout_seconds=5,
        journal_buffer_size=16_000_000,
    )
    test_case.addCleanup(worker.close)
    return worker


class ConnectGateWorkerTests(unittest.TestCase):
    """La garde borne les sign-ons : un spawn JVM est une tentative comptée."""

    def _gated_worker(self, gate) -> PersistentJavaWorker:
        worker = PersistentJavaWorker(
            java="java",
            classpath="classpath",
            host="ibmi-dev",
            user="catalog-user",
            schema="SALES",
            table="SALE",
            timeout_seconds=5,
            journal_buffer_size=16_000_000,
            connect_gate=gate,
        )
        # Cf. docstring de ``_worker()`` : ferme le worker à la fin du test
        # pour ne pas laisser le thread lecteur de ``LinePump`` vivant.
        self.addCleanup(worker.close)
        return worker

    def test_connect_error_connection_failed_is_counted_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = FileSourceGate(f"{tmp}/gate.json")
            process = ScriptedProcess([["connect_error=CONNECTION_FAILED\n"]])
            with patch("quadringent.java_worker.subprocess.Popen", return_value=process) as popen:
                worker = self._gated_worker(gate)
                with self.assertRaises(SourceConnectFailedError):
                    worker.catalog(limit=20)
            self.assertEqual(popen.call_count, 1)
            record = gate.state()
            self.assertEqual(record["attempts_used"], 1)
            self.assertEqual(record["state"], "closed")

    def test_connect_error_user_disabled_blocks_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = FileSourceGate(f"{tmp}/gate.json")
            process = ScriptedProcess([["connect_error=USER_DISABLED\n"]])
            with patch("quadringent.java_worker.subprocess.Popen", return_value=process) as popen:
                worker = self._gated_worker(gate)
                with self.assertRaises(IbmiUserDisabledError):
                    worker.catalog(limit=20)
                # La garde est bloquée : le moindre nouvel essai lève avant Popen.
                with self.assertRaises(SourceAuthenticationBlockedError):
                    worker.catalog(limit=20)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(gate.state()["state"], "blocked")

    def test_connect_error_authentication_failed_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = FileSourceGate(f"{tmp}/gate.json")
            process = ScriptedProcess([["connect_error=AUTHENTICATION_FAILED\n"]])
            with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
                worker = self._gated_worker(gate)
                with self.assertRaises(SourceAuthenticationBlockedError):
                    worker.catalog(limit=20)
            self.assertEqual(gate.state()["state"], "blocked")

    def test_third_connect_failure_opens_the_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = FileSourceGate(f"{tmp}/gate.json")
            with patch("quadringent.java_worker.subprocess.Popen") as popen:
                popen.side_effect = lambda *a, **k: ScriptedProcess([["connect_error=CONNECTION_FAILED\n"]])
                worker = self._gated_worker(gate)
                for _ in range(2):
                    with self.assertRaises(SourceConnectFailedError):
                        worker.catalog(limit=20)
                # Le 3e échec bascule la garde en pause : l'erreur levée est
                # explicite et porte l'échéance de reprise.
                with self.assertRaises(SourceUnavailablePausedError):
                    worker.catalog(limit=20)
                self.assertEqual(popen.call_count, 3)
                # Toute tentative suivante est refusée avant Popen : zéro sign-on.
                with self.assertRaises(SourceUnavailablePausedError):
                    worker.catalog(limit=20)
                self.assertEqual(popen.call_count, 3)
            self.assertEqual(gate.state()["state"], "paused")

    def test_worker_ready_releases_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = FileSourceGate(f"{tmp}/gate.json")
            process = ScriptedProcess([["worker_ready\n"], CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"]])
            with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
                worker = self._gated_worker(gate)
                receivers = WorkerReceiverCatalog(worker, limit=20).snapshot()
            self.assertEqual([r.receiver for r in receivers], ["R2", "R10"])
            record = gate.state()
            self.assertEqual(record["state"], "closed")
            self.assertEqual(record["attempts_used"], 0)


class PersistentJavaWorkerProbeTests(unittest.TestCase):
    """``probe`` (chantier « prod-wiring ») — sonde de source via JTOpen,
    jamais ODBC/pyodbc : l'authentification est déjà prouvée par la
    connexion JDBC ouverte au démarrage du worker."""

    def test_probe_returns_the_version_and_qtimzon_protocol_lines(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                ["version\tV7.R5M0\n", "qtimzon\tQP0100CET\n", "probe_done\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            raw_output = worker.probe()
        self.assertIn("version\tV7.R5M0", raw_output)
        self.assertIn("qtimzon\tQP0100CET", raw_output)
        self.assertEqual(json.loads(process.writes[0])["cmd"], "probe")

    def test_probe_error_raises_without_leaking_the_password(self) -> None:
        process = ScriptedProcess([["worker_ready\n"], ["probe_error=SQLException\n"]])
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            with patch.dict(os.environ, {"ISERIES_PASSWORD": "unit-only-password"}, clear=False):
                worker = _worker(self)
                with self.assertRaises(RuntimeError) as captured:
                    worker.probe()
        self.assertNotIn("unit-only-password", str(captured.exception))


class PersistentJavaWorkerTests(unittest.TestCase):
    def test_one_jvm_serves_catalog_then_two_windows(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"],
                [
                    "summary seen=0 decoded=0 elapsed_ms=1 final_position=R2:110 scan_complete=true\n",
                    "window_done\n",
                ],
                [
                    "summary seen=2 decoded=2 elapsed_ms=3 final_position=R2:120 scan_complete=true\n",
                    "window_done\n",
                ],
            ]
        )

        with patch("quadringent.java_worker.subprocess.Popen", return_value=process) as popen:
            with patch.dict(
                os.environ,
                {
                    "ISERIES_PASSWORD": "unit-only-password",
                    "ISERIES_START_SEQUENCE": "1",
                    "AS400_CHECKPOINT_FILE": "/tmp/should-not-be-used",
                },
                clear=False,
            ):
                worker = _worker(self)
                catalog = WorkerReceiverCatalog(worker, limit=20)
                receivers = catalog.snapshot()
                first = worker.process_window({"receiver": "R2", "start_sequence": 100})
                second = worker.process_window({"receiver": "R2", "start_sequence": 111})
                worker.close()

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(worker.spawn_count, 1)
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], "io.quadringent.as400.PersistentJournalWorker")
        self.assertNotIn("unit-only-password", command)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["ISERIES_PASSWORD"], "unit-only-password")
        self.assertEqual(environment["ISERIES_JOURNAL_BUFFER_SIZE"], "16000000")
        self.assertEqual(environment["ISERIES_MAX_SERVER_ENTRIES"], "1000000")
        self.assertNotIn("ISERIES_START_SEQUENCE", environment)
        self.assertNotIn("AS400_CHECKPOINT_FILE", environment)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual([receiver.receiver for receiver in receivers], ["R2", "R10"])
        self.assertIn("scan_complete=true", first)
        self.assertIn("scan_complete=true", second)
        self.assertEqual(json.loads(process.writes[0])["cmd"], "catalog")
        self.assertEqual(json.loads(process.writes[-1])["cmd"], "shutdown")

    def test_catalog_request_anchors_on_the_checkpoint_receiver(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            catalog = WorkerReceiverCatalog(worker, limit=20)
            catalog.snapshot(required_receiver="DEMOJRN4115")
            worker.close()

        command = json.loads(process.writes[0])
        self.assertEqual(command["cmd"], "catalog")
        self.assertEqual(command["requires_receiver"], "DEMOJRN4115")

    def test_catalog_request_omits_the_anchor_without_a_checkpoint(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            WorkerReceiverCatalog(worker, limit=20).snapshot()
            worker.close()

        command = json.loads(process.writes[0])
        self.assertNotIn("requires_receiver", command)

    def test_window_error_is_fail_closed_without_scan_complete(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                ["window_error=IllegalStateException\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            with self.assertRaisesRegex(RuntimeError, "bounded IBM i reader failed"):
                worker.process_window({"receiver": "R2"})

    def test_catalog_error_does_not_leak_stderr(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                ["catalog_error=SQLException\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            with self.assertRaises(RuntimeError) as context:
                worker.catalog(limit=20)
        self.assertEqual(str(context.exception), "IBM i receiver catalog failed:SQLException")

    def test_tail_probe_writes_command_and_returns_the_single_row(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                [
                    "tail receiver=R10 library=QGPL first_sequence=111 last_sequence=140 status=ATTACHED\n",
                    "tail_done\n",
                ],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            catalog = WorkerReceiverCatalog(worker, limit=20)
            snapshot = catalog.tail()
            worker.close()

        self.assertEqual(json.loads(process.writes[0])["cmd"], "tail")
        assert snapshot is not None
        self.assertEqual(snapshot.receiver, "R10")
        self.assertEqual(snapshot.last_sequence, 140)

    def test_tail_probe_error_does_not_leak_stderr(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                ["tail_error=SQLException\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            with self.assertRaises(RuntimeError) as context:
                worker.tail()
        self.assertEqual(str(context.exception), "IBM i tail probe failed:SQLException")

    def test_parser_still_accepts_worker_catalog_payload(self) -> None:
        receivers = parse_catalog_output(CATALOG_OUTPUT)
        self.assertEqual(receivers[0].last_sequence, 110)

    def test_blocked_window_times_out_without_window_done(self) -> None:
        """A worker that stops answering must raise inside the reader deadline.

        Rewritten 2026-08-27. The previous version patched select.select and
        asserted that readline was never called after a select timeout, which
        pinned an implementation detail. stdout is now drained by LinePump in its
        own thread precisely so the JVM is never blocked by the consumer, so
        readline is called continuously by design. The behaviour under test is
        unchanged: no window_done within the deadline must fail closed.
        """

        process = ScriptedProcess([["worker_ready\n"]])
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = PersistentJavaWorker(
                java="java",
                classpath="classpath",
                host="ibmi-dev",
                user="catalog-user",
                schema="SALES",
                table="CNTR",
                timeout_seconds=0.05,
                journal_buffer_size=16_000_000,
            )
            self.addCleanup(worker.close)
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "bounded IBM i reader timed out"):
                worker.process_window({
                    "receiver": "DEMOJRN3761",
                    "receiver_library": "DEMOLIB",
                    "start_sequence": "100",
                    "end_sequence": "109",
                })
            # the deadline is enforced by the queue read, not by a later loop turn
            self.assertLess(time.monotonic() - started, 5.0)

    def test_worker_passes_retrieve_timeout_to_the_jvm(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"],
            ]
        )
        captured: dict[str, object] = {}

        def fake_popen(*_args, **kwargs):
            captured["env"] = kwargs["env"]
            return process

        with patch("quadringent.java_worker.subprocess.Popen", side_effect=fake_popen):
            worker = PersistentJavaWorker(
                java="java",
                classpath="classpath",
                host="ibmi-dev",
                user="catalog-user",
                schema="SALES",
                table="CNTR",
                timeout_seconds=5,
                retrieve_timeout_ms=20_000,
            )
            self.addCleanup(worker.close)
            worker.catalog(limit=20)

        env = captured["env"]
        assert isinstance(env, dict)
        self.assertEqual(env["AS400_RETRIEVE_TIMEOUT_MS"], "20000")

    def test_sql_window_sends_cmd_and_skips_retrieve_start(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                [
                    "retrieve_start receiver=R2 start_sequence=100 end_sequence=109 timeout_ms=25000\n",
                    "summary seen=0 decoded=0 elapsed_ms=12 scan_complete=true\n",
                    "window_done\n",
                ],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
            worker = _worker(self)
            output = worker.sql_window(
                {
                    "receiver": "R2",
                    "receiver_library": "QGPL",
                    "start_sequence": 100,
                    "end_sequence": 109,
                }
            )
        self.assertIn("cmd", json.loads(process.writes[0]))
        self.assertEqual(json.loads(process.writes[0])["cmd"], "sql_window")
        self.assertIn("scan_complete=true", output)
        self.assertNotIn("retrieve_start", output)
        self.assertEqual(worker.tables, ("SALE",))

    def test_parse_capture_tables_is_ordered_unique_and_fail_closed(self) -> None:
        self.assertEqual(parse_capture_tables(None, fallback="SALE"), ("SALE",))
        self.assertEqual(parse_capture_tables("  ", fallback="SALE"), ("SALE",))
        self.assertEqual(parse_capture_tables("SALE, CNTR", fallback="SALE"), ("SALE", "CNTR"))
        self.assertEqual(parse_capture_tables(["CNTR", "SALE"], fallback="SALE"), ("CNTR", "SALE"))
        with self.assertRaisesRegex(ValueError, "non-empty"):
            parse_capture_tables("SALE,,CNTR", fallback="SALE")
        with self.assertRaisesRegex(ValueError, "unique"):
            parse_capture_tables("SALE,sale", fallback="SALE")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            parse_capture_tables("SALE,CNTR;DROP", fallback="SALE")
        with self.assertRaisesRegex(ValueError, "included"):
            parse_capture_tables("SALE,CNTR", fallback="ORDER")
        too_many = ",".join(["SALE"] + [f"T{index}" for index in range(2, 34)])
        with self.assertRaisesRegex(ValueError, "at most 32"):
            parse_capture_tables(too_many, fallback="SALE")

    def test_worker_passes_table_list_to_the_jvm_without_secrets_in_argv(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process) as popen:
            with patch.dict(
                os.environ,
                {
                    "ISERIES_PASSWORD": "unit-only-password",
                    "ISERIES_TABLES": "SHOULD_NOT_INHERIT",
                },
                clear=False,
            ):
                worker = PersistentJavaWorker(
                    java="java",
                    classpath="classpath",
                    host="ibmi-dev",
                    user="catalog-user",
                    schema="SALES",
                    table="SALE",
                    tables="SALE,CNTR",
                    timeout_seconds=5,
                )
                self.addCleanup(worker.close)
                worker.catalog(limit=20)
        self.assertEqual(worker.tables, ("SALE", "CNTR"))
        command = popen.call_args.args[0]
        self.assertNotIn("unit-only-password", command)
        self.assertNotIn("SALE,CNTR", command)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["ISERIES_TABLES"], "SALE,CNTR")
        self.assertEqual(environment["ISERIES_TABLE"], "SALE")
        self.assertEqual(environment["ISERIES_PASSWORD"], "unit-only-password")

    def test_mono_table_worker_does_not_inherit_a_foreign_table_list(self) -> None:
        process = ScriptedProcess(
            [
                ["worker_ready\n"],
                CATALOG_OUTPUT.splitlines(keepends=True) + ["catalog_done\n"],
            ]
        )
        with patch("quadringent.java_worker.subprocess.Popen", return_value=process) as popen:
            with patch.dict(os.environ, {"ISERIES_TABLES": "SALE,CNTR"}, clear=False):
                worker = _worker(self)
                worker.catalog(limit=20)
        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("ISERIES_TABLES", environment)
        self.assertEqual(worker.tables, ("SALE",))
        self.assertEqual(environment["ISERIES_TABLE"], "SALE")

    def test_sql_window_refuses_multi_table_before_worker_io(self) -> None:
        with patch("quadringent.java_worker.subprocess.Popen") as popen:
            worker = PersistentJavaWorker(
                java="java",
                classpath="classpath",
                host="ibmi-dev",
                user="catalog-user",
                schema="SALES",
                table="SALE",
                tables=("SALE", "CNTR"),
                timeout_seconds=5,
            )
            self.addCleanup(worker.close)
            with self.assertRaisesRegex(RuntimeError, "refuses multi-table"):
                worker.sql_window({"receiver": "R2"})
        popen.assert_not_called()

    def test_window_progress_resets_idle_timeout_and_is_not_payload(self) -> None:
        clock = {"t": 0.0}

        class DelayedProcess(ScriptedProcess):
            def readline(self) -> str:
                clock["t"] += 4.0
                return super().readline()

        process = DelayedProcess(
            [
                ["worker_ready\n"],
                [
                    "window_progress seen=1 decoded=0 last_sequence=101\n",
                    "summary seen=1 decoded=0 elapsed_ms=1 final_position=R2:110 scan_complete=true\n",
                    "window_done\n",
                ],
            ]
        )
        with patch("quadringent.java_worker.time.monotonic", side_effect=lambda: clock["t"]):
            with patch("quadringent.java_worker.subprocess.Popen", return_value=process):
                worker = _worker(self)
                output = worker.process_window({"receiver": "R2"})
        self.assertIn("scan_complete=true", output)
        self.assertNotIn("window_progress", output)


if __name__ == "__main__":
    unittest.main()


def test_capture_tables_accept_the_same_identifiers_as_the_java_worker() -> None:
    """Constaté sur GKE : deux tables sur un journal (mode flotte) → « unsafe
    IBM i identifier » pour QDC_ORDERS ; le motif Python refusait ``_`` alors
    que le worker Java accepte ``[A-Za-z0-9_$#@]{1,128}``."""
    from quadringent.java_worker import parse_capture_tables

    assert parse_capture_tables("QDC_ORDERS,QDC_TAIL", fallback="QDC_ORDERS") == ("QDC_ORDERS", "QDC_TAIL")
    assert parse_capture_tables("A@B,C#D,E$F", fallback="A@B") == ("A@B", "C#D", "E$F")
    for unsafe in ("QDC ORDERS", "QDC;DROP", "X" * 129, "QDC'O"):
        try:
            parse_capture_tables(unsafe, fallback="QDC_ORDERS")
        except ValueError:
            continue
        raise AssertionError(unsafe)
