from __future__ import annotations

import io
import json
import os
import queue
from pathlib import Path
import re
import select
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from typing import Callable, Mapping

from datetime import datetime, timezone

from .continuous import (
    SqlWindowIncomplete,
    should_probe_emptiness,
    CapturedWindow,
    CaptureWindow,
    IbmiUserDisabledError,
    WindowRunner,
    _is_ibmi_userid_disabled,
)
from .source_gate import (
    ConnectFailureClass,
    SourceAuthenticationBlockedError,
    SourceConfigurationBlockedError,
    SourceGate,
    SourceUnavailablePausedError,
)


DEFAULT_JOURNAL_BUFFER_SIZE = 16_000_000
MIN_JOURNAL_BUFFER_SIZE = 131_072
MAX_JOURNAL_BUFFER_SIZE = 16_000_000
DEFAULT_MAX_SERVER_ENTRIES = 1_000_000
MAX_CAPTURE_TABLES = 32
WORKER_CLASS = "io.quadringent.as400.PersistentJournalWorker"
_TABLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_$#@]{1,128}$")  # même motif que le worker Java
_ONE_SHOT_ENV = (
    "AS400_CHECKPOINT_FILE",
    "AS400_RAW_DIRECTORY",
    "AS400_RAW_HIGH_WATERMARK_SEQUENCE",
    "ISERIES_START_SEQUENCE",
    "ISERIES_END_SEQUENCE",
    "ISERIES_RECEIVER",
    "ISERIES_RECEIVER_LIBRARY",
)


# Diagnostic lines the worker may emit. Relaying by explicit prefix rather than
# a hardcoded pair, because pagination instrumentation added to the Java scan
# loop was silently dropped and looked like it had never run (2026-08-27).
RELAYED_WORKER_PREFIXES = (
    "window_progress",
    "retrieve_start",
    "page_start",
    "page_done",
    "page_buffer_ok",
    "decode_start",
    "decode_done",
    "more_data_unknown",
    "final_position_start",
    "final_position_done",
    "summary_written",
    "window_flushed",
    "process_window_returned",
    "stalled_stack",
    "stalled_frame",
)


def parse_capture_tables(
    raw: str | Sequence[str] | None,
    *,
    fallback: str,
) -> tuple[str, ...]:
    """Ordered fail-closed table list for one journal session.

    ``None`` or a blank CSV falls back to ``ISERIES_TABLE``. A present list is
    validated before any JVM I/O: safe identifiers, unique, non-empty, at most
    32, and the fallback table must belong to it.
    """

    if fallback is None or not str(fallback).strip():
        raise ValueError("ISERIES_TABLE is required")
    fallback_table = str(fallback).strip()
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return (fallback_table,)
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    tables: list[str] = []
    seen: set[str] = set()
    for item in items:
        table = item.strip() if isinstance(item, str) else str(item).strip()
        if not table:
            raise ValueError("ISERIES_TABLES entries must be non-empty")
        if _TABLE_IDENTIFIER.fullmatch(table) is None:
            raise ValueError("unsafe IBM i identifier")
        key = table.upper()
        if key in seen:
            raise ValueError("ISERIES_TABLES entries must be unique")
        seen.add(key)
        tables.append(table)
        if len(tables) > MAX_CAPTURE_TABLES:
            raise ValueError("ISERIES_TABLES must have at most 32 tables")
    if not tables:
        raise ValueError("ISERIES_TABLES entries must be non-empty")
    if fallback_table.upper() not in seen:
        raise ValueError("ISERIES_TABLE must be included in ISERIES_TABLES")
    return tuple(tables)


def worker_line_phase(line: str) -> str:
    """First token of a worker diagnostic line."""

    return line.strip().split(" ", 1)[0]


def is_relayable_worker_line(line: str) -> bool:
    """True when the line is a diagnostic the caller should see.

    Protocol control lines (``window_done``, ``summary``, ``worker_ready``)
    keep their own handling and are not diagnostics.
    """

    stripped = line.strip()
    if not stripped:
        return False
    return worker_line_phase(stripped) in RELAYED_WORKER_PREFIXES


class SourceConnectFailedError(RuntimeError):
    """Une tentative d'établissement de session IBM i a échoué avant worker_ready."""


_CONNECT_ERROR_CLASSES = {
    "USER_DISABLED": ConnectFailureClass.AUTHENTICATION,
    "AUTHENTICATION_FAILED": ConnectFailureClass.AUTHENTICATION,
    "SOURCE_CLOCK_MISMATCH": ConnectFailureClass.CONFIGURATION,
    "CONNECTION_FAILED": ConnectFailureClass.UNAVAILABLE,
    "TLS_FAILED": ConnectFailureClass.UNAVAILABLE,
    "QUERY_TIMEOUT": ConnectFailureClass.UNAVAILABLE,
}


def classify_connect_error(code: str) -> ConnectFailureClass:
    """Code ``connect_error`` du worker → famille de politique de reconnexion."""

    return _CONNECT_ERROR_CLASSES.get(code.strip().upper(), ConnectFailureClass.UNKNOWN)


def _parse_gate_instant(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class LinePump:
    """Drain a subprocess stdout pipe in its own thread.

    The JVM writes several control and diagnostic lines per window. When the
    consumer read the pipe inline, any slowness on its side backed up into the
    pipe and stalled the JVM mid-write: measured 2026-08-27, windows were
    abandoned at a different write every time (19 / 11 / 6 / 3 across four
    successive markers). Draining ahead of the consumer removes that coupling,
    the same way stderr is already drained.
    """

    def __init__(self, stream, *, maxsize: int = 4096, eof_marker: str | None = "") -> None:
        self._stream = stream
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=maxsize)
        self._eof_marker = eof_marker
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._drained = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return

        def run() -> None:
            try:
                while not self._stopped.is_set():
                    line = self._stream.readline()
                    if not line:
                        break
                    self._queue.put(line)
            except Exception:
                pass
            finally:
                self._drained.set()
                if self._eof_marker is not None:
                    try:
                        self._queue.put(self._eof_marker, timeout=1.0)
                    except Exception:
                        pass

        self._thread = threading.Thread(target=run, name="as400-java-stdout", daemon=True)
        self._thread.start()

    def next_line(self, *, timeout: float) -> str:
        """Next queued line. Raises queue.Empty when nothing arrives in time."""

        return self._queue.get(timeout=timeout)

    def wait_until_drained(self, *, timeout: float) -> bool:
        return self._drained.wait(timeout)

    def stop(self) -> None:
        self._stopped.set()



class PersistentJavaWorker:
    """One JVM for catalog and journal windows; AS400/JDBC stay open."""

    def __init__(
        self,
        *,
        java: str,
        classpath: str,
        host: str,
        user: str,
        schema: str | None = None,
        table: str | None = None,
        timeout_seconds: float,
        journal_buffer_size: int = DEFAULT_JOURNAL_BUFFER_SIZE,
        max_server_entries: int = DEFAULT_MAX_SERVER_ENTRIES,
        class_name: str = WORKER_CLASS,
        retrieve_timeout_ms: int = 60_000,
        tables: str | Sequence[str] | None = None,
        connect_gate: SourceGate | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retrieve_timeout_ms < 1_000:
            raise ValueError("retrieve_timeout_ms must be >= 1000")
        if not MIN_JOURNAL_BUFFER_SIZE <= journal_buffer_size <= MAX_JOURNAL_BUFFER_SIZE:
            raise ValueError(
                "journal_buffer_size must be between "
                f"{MIN_JOURNAL_BUFFER_SIZE} and {MAX_JOURNAL_BUFFER_SIZE}"
            )
        if not 1 <= max_server_entries <= DEFAULT_MAX_SERVER_ENTRIES:
            raise ValueError(
                "max_server_entries must be between 1 and "
                f"{DEFAULT_MAX_SERVER_ENTRIES}"
            )
        self.java = java
        self.classpath = classpath
        self.host = host
        self.user = user
        self.schema = schema
        self.table = table
        # ``table`` absent (worker de diagnostic, ex. DiagnosticWorker : ni
        # bibliothèque ni table capturée) : aucune liste de tables à valider,
        # aucune variable ISERIES_TABLE(S) à poser sur le sous-processus.
        self.tables: tuple[str, ...] = () if table is None else parse_capture_tables(tables, fallback=table)
        self._pass_tables_env = table is not None and tables is not None and not (
            isinstance(tables, str) and not tables.strip()
        )
        self.timeout_seconds = timeout_seconds
        self.retrieve_timeout_ms = retrieve_timeout_ms
        self.journal_buffer_size = journal_buffer_size
        self.max_server_entries = max_server_entries
        self.class_name = class_name
        self.connect_gate = connect_gate
        self.utc_now = utc_now
        self._process: subprocess.Popen[str] | None = None
        self._pump: LinePump | None = None
        self.spawn_count = 0

    def catalog(self, *, limit: int = 20, required_receiver: str | None = None) -> str:
        if not 1 <= limit <= 100:
            raise ValueError("receiver metadata limit must be between 1 and 100")
        process = self._ensure_worker()
        try:
            print(json.dumps({"event": "java_worker", "phase": "catalog_write", "limit": limit}), flush=True)
            command: dict[str, object] = {"cmd": "catalog", "limit": limit}
            if required_receiver is not None and required_receiver.strip():
                # Le receiver porteur du checkpoint doit rester visible après
                # des jours de rotation : la requête est ancrée dessus.
                command["requires_receiver"] = required_receiver.strip()
            self._write(process, command)
            return self._read_until(
                process,
                done="catalog_done",
                error_prefix="catalog_error=",
                failure="IBM i receiver catalog failed",
            )
        except Exception:
            self._kill_worker()
            raise

    def discover(
        self, *, libraries: Sequence[str] | None = None, limit: int = 500, search: str | None = None
    ) -> str:
        """Lance ``discover`` sur le worker persistant — catalogue seulement.

        ``libraries`` est joint en CSV (le protocole ligne du worker n'a
        qu'un parseur JSON à plat, sans tableau — voir
        ``JournalSession.parseFlatJson``) ; ``None``/vide laisse le worker
        retomber sur sa bibliothèque courante.
        """

        if not 1 <= limit <= 5000:
            raise ValueError("discover limit must be between 1 and 5000")
        process = self._ensure_worker()
        try:
            print(json.dumps({"event": "java_worker", "phase": "discover_write", "limit": limit}), flush=True)
            command: dict[str, object] = {"cmd": "discover", "limit": limit}
            if libraries:
                command["libraries"] = ",".join(libraries)
            if search is not None and search.strip():
                command["search"] = search.strip()
            self._write(process, command)
            return self._read_until(
                process,
                done="discover_done",
                error_prefix="discover_error=",
                failure="IBM i table discovery failed",
            )
        except Exception:
            self._kill_worker()
            raise

    def probe(self) -> str:
        """Version IBM i et ``QTIMZON`` — sonde de source (chantier « prod-wiring »).

        L'authentification est déjà prouvée par ``_ensure_worker`` (la
        connexion JDBC JTOpen doit être ouverte avant tout appel : un échec
        de sign-on lève une exception ici, jamais un résultat partiel).
        """

        process = self._ensure_worker()
        try:
            print(json.dumps({"event": "java_worker", "phase": "probe_write"}), flush=True)
            self._write(process, {"cmd": "probe"})
            return self._read_until(
                process,
                done="probe_done",
                error_prefix="probe_error=",
                failure="IBM i source probe failed",
            )
        except Exception:
            self._kill_worker()
            raise

    def tail(self) -> str:
        process = self._ensure_worker()
        try:
            print(json.dumps({"event": "java_worker", "phase": "tail_write"}), flush=True)
            self._write(process, {"cmd": "tail"})
            return self._read_until(
                process,
                done="tail_done",
                error_prefix="tail_error=",
                failure="IBM i tail probe failed",
            )
        except Exception:
            self._kill_worker()
            raise

    def process_window(self, request: Mapping[str, object]) -> str:
        process = self._ensure_worker()
        try:
            print(
                json.dumps(
                    {
                        "event": "java_worker",
                        "phase": "process_window_write",
                        "receiver": str(request.get("receiver", "")),
                        "start_sequence": str(request.get("start_sequence", "")),
                        "end_sequence": str(request.get("end_sequence", "")),
                    }
                ),
                flush=True,
            )
            self._write(process, request)
            return self._read_until(
                process,
                done="window_done",
                error_prefix="window_error=",
                failure="bounded IBM i reader failed",
            )
        except Exception:
            self._kill_worker()
            raise

    def sql_window(self, request: Mapping[str, object]) -> str:
        if len(self.tables) > 1:
            raise RuntimeError("bounded DISPLAY_JOURNAL refuses multi-table capture")
        payload = dict(request)
        payload["cmd"] = "sql_window"
        process = self._ensure_worker()
        try:
            self._write(process, payload)
            return self._read_until(
                process,
                done="window_done",
                error_prefix="window_error=",
                failure="bounded DISPLAY_JOURNAL failed",
            )
        except Exception:
            self._kill_worker()
            raise

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write('{"cmd":"shutdown"}\n')
                process.stdin.flush()
                process.stdin.close()
                process.wait(timeout=5)
        except Exception:
            self._kill_worker()
        finally:
            self._process = None

    def _ensure_worker(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self._process = None
        environment = os.environ.copy()
        environment.update(
            {
                "ISERIES_HOST": self.host,
                "ISERIES_USER": self.user,
                "ISERIES_JOURNAL_BUFFER_SIZE": str(self.journal_buffer_size),
                "ISERIES_MAX_SERVER_ENTRIES": str(self.max_server_entries),
                "AS400_RETRIEVE_TIMEOUT_MS": str(self.retrieve_timeout_ms),
                "AS400_VERBOSE": "false",
            }
        )
        # ``schema``/``table`` absents : worker de diagnostic — ni
        # ISERIES_SCHEMA ni ISERIES_TABLE ne sont posées, le worker Java
        # (DiagnosticWorker) ne les lit jamais.
        if self.schema is not None:
            environment["ISERIES_SCHEMA"] = self.schema
        else:
            environment.pop("ISERIES_SCHEMA", None)
        if self.table is not None:
            environment["ISERIES_TABLE"] = self.table
        else:
            environment.pop("ISERIES_TABLE", None)
        for name in _ONE_SHOT_ENV:
            environment.pop(name, None)
        if self._pass_tables_env:
            environment["ISERIES_TABLES"] = ",".join(self.tables)
        else:
            environment.pop("ISERIES_TABLES", None)
        command = [
            self.java,
            "-Dorg.slf4j.simpleLogger.defaultLogLevel=error",
            "-cp",
            self.classpath,
            self.class_name,
        ]
        gate = self.connect_gate
        if gate is not None:
            # La garde tranche avant le moindre spawn : un refus ne coûte
            # aucun sign-on, une concession est comptée durablement.
            gate.before_connect(now=self.utc_now())
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            if gate is not None:
                gate.record_connect_failure(ConnectFailureClass.UNKNOWN, now=self.utc_now())
            raise RuntimeError("bounded IBM i reader failed to start") from error
        self._process = process
        self.spawn_count += 1
        self._pump = LinePump(process.stdout)
        self._pump.start()
        _drain_stderr(process)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            ready = self._readline(process, deadline).strip()
        except Exception:
            self._kill_worker()
            if gate is not None:
                gate.record_connect_failure(ConnectFailureClass.UNKNOWN, now=self.utc_now())
            raise
        if ready.startswith("connect_error="):
            self._kill_worker()
            raise self._connect_failure(ready)
        if ready != "worker_ready":
            self._kill_worker()
            if gate is not None:
                gate.record_connect_failure(ConnectFailureClass.UNKNOWN, now=self.utc_now())
            raise RuntimeError("bounded IBM i reader did not become ready")
        if gate is not None:
            gate.record_connect_success(now=self.utc_now())
        print('{"event":"java_worker","phase":"worker_ready"}', flush=True)
        return process

    def _connect_failure(self, marker: str) -> BaseException:
        """Traduit le ``connect_error`` du worker en décision de garde.

        La famille vient du classifieur Java (chaîne d'exceptions JTOpen/JDBC) —
        jamais du seul nom de classe SQL : un ``SQLNonTransientConnectionException``
        peut être une coupure réseau comme un profil désactivé.
        """

        code = marker.split("=", 1)[1].strip()[:40]
        failure_class = classify_connect_error(code)
        record = None
        if self.connect_gate is not None:
            record = self.connect_gate.record_connect_failure(
                failure_class, now=self.utc_now(), error_head=f"connect_error={code}"
            )
        if failure_class is ConnectFailureClass.AUTHENTICATION:
            if code == "USER_DISABLED":
                return IbmiUserDisabledError(
                    "IBM i profile disabled — sign-on forbidden until operator reset"
                )
            return SourceAuthenticationBlockedError(
                f"IBM i authentication refused (connect_error={code}) — gate blocked"
            )
        if failure_class is ConnectFailureClass.CONFIGURATION:
            # Erreur de configuration (ex. AS400_SOURCE_TIME_ZONE incohérent
            # avec la source) — jamais présentée comme une coupure : rejouer
            # le sign-on reproduirait le même échec indéfiniment.
            return SourceConfigurationBlockedError(
                f"IBM i source configuration invalid (connect_error={code}) — "
                "gate blocked until the declared configuration is fixed"
            )
        if isinstance(record, Mapping) and record.get("state") == "paused":
            retry_after = _parse_gate_instant(record.get("retry_after"))
            if retry_after is not None:
                return SourceUnavailablePausedError(
                    f"source IBM i indisponible — pause jusqu'à {retry_after.isoformat()}",
                    retry_after=retry_after,
                    reason_code=str(record.get("reason_code") or "SOURCE_UNAVAILABLE"),
                )
        return SourceConnectFailedError(f"IBM i connect failed: connect_error={code}")

    def _write(self, process: subprocess.Popen[str], payload: Mapping[str, object]) -> None:
        if process.stdin is None:
            raise RuntimeError("bounded IBM i reader stdin is closed")
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _read_until(
        self,
        process: subprocess.Popen[str],
        *,
        done: str,
        error_prefix: str,
        failure: str,
    ) -> str:
        lines: list[str] = []
        while True:
            # Idle timeout: a long window may emit window_progress without
            # finishing. The previous total deadline aborted soak at ~25 min.
            deadline = time.monotonic() + self.timeout_seconds
            line = self._readline(process, deadline)
            stripped = line.strip()
            if stripped.startswith(error_prefix):
                detail = stripped[len(error_prefix) :].split()[0][:60]
                message = f"{failure}:{detail}"
                if _is_ibmi_userid_disabled(RuntimeError(message)):
                    raise IbmiUserDisabledError(message)
                raise RuntimeError(message)
            if is_relayable_worker_line(stripped):
                print(
                    json.dumps({
                        "event": "java_worker",
                        "phase": worker_line_phase(stripped),
                        "line": stripped[:300],
                    }),
                    flush=True,
                )
                continue
            if stripped == done:
                print(json.dumps({"event": "java_worker", "phase": done}), flush=True)
                return "".join(lines)
            lines.append(line)
            if len(lines) > 10_000:
                raise RuntimeError("bounded IBM i reader output overflow")

    def _readline(self, process: subprocess.Popen[str], deadline: float) -> str:
        """Next worker line, with a deadline that is actually enforced.

        The previous implementation selected on the pipe and then called
        ``readline()``, which blocks until a newline arrives with no bound of its
        own: select only promises that *some* bytes are ready. A JVM stalled
        mid-write therefore parked the consumer indefinitely, and the deadline
        was only noticed on the following iteration. Reading from the pump keeps
        the deadline real, and the pump keeps draining so the JVM is never
        blocked by us.
        """

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("bounded IBM i reader timed out")
        pump = self._pump
        if pump is None:
            raise RuntimeError("bounded IBM i reader stdout is closed")
        try:
            line = pump.next_line(timeout=remaining)
        except queue.Empty:
            if process.poll() is not None:
                raise RuntimeError(
                    f"bounded IBM i reader exited={process.returncode}"
                ) from None
            raise RuntimeError("bounded IBM i reader timed out") from None
        if line == "":
            raise RuntimeError("bounded IBM i reader exited")
        return line

    def _kill_worker(self) -> None:
        process = self._process
        self._process = None
        pump = self._pump
        self._pump = None
        if pump is not None:
            pump.stop()
        if process is None:
            return
        try:
            process.kill()
        except Exception:
            return
        try:
            process.wait(timeout=2)
        except Exception:
            return


class JavaWindowRunner(WindowRunner):
    """Talk to one persistent Java journal worker for every bounded window."""

    def __init__(
        self,
        worker: PersistentJavaWorker,
        *,
        max_decoded_entries: int,
        empty_probe: bool = True,
    ) -> None:
        if max_decoded_entries < 1:
            raise ValueError("max_decoded_entries must be positive")
        self.worker = worker
        self.max_decoded_entries = max_decoded_entries
        self.empty_probe = empty_probe
        self._lag_sequences: int | None = None

    def observe_lag(self, lag_sequences: int | None) -> None:
        """Latest known distance to the journal tail, fed by the capture loop."""

        self._lag_sequences = lag_sequences

    def _probe_wanted(self, *, window_sequences: int) -> bool:
        if not self.empty_probe:
            return False
        tables = getattr(self.worker, "tables", None)
        if tables is not None and len(tuple(tables)) > 1:
            return False
        return should_probe_emptiness(
            lag_sequences=self._lag_sequences,
            window_sequences=window_sequences,
        )

    def capture(self, window: CaptureWindow) -> CapturedWindow:
        span = window.end.sequence - window.start.sequence + 1
        if span < 1:
            raise RuntimeError("bounded IBM i window is empty")
        request = {
            "receiver": window.end.receiver,
            "receiver_library": window.receiver_library,
            "start_sequence": window.start.sequence,
            "end_sequence": window.end.sequence,
            "max_server_entries": span,
            "max_decoded_entries": max(self.max_decoded_entries, span),
            "high_watermark_sequence": str(window.end.sequence),
        }
        if self._probe_wanted(window_sequences=span):
            with tempfile.TemporaryDirectory(prefix="as400-empty-probe-") as probe_dir:
                probe_output = self.worker.sql_window({**request, "raw_directory": probe_dir})
                probe_summaries = [
                    line.strip()
                    for line in probe_output.splitlines()
                    if line.startswith("summary ")
                ]
                if (
                    len(probe_summaries) != 1
                    or "scan_complete=true" not in probe_summaries[0]
                ):
                    raise RuntimeError(
                        "bounded DISPLAY_JOURNAL probe did not certify scan completion"
                    )
                decoded = _summary_int(probe_summaries[0], "decoded")
                manifests = list(Path(probe_dir).glob("*.manifest.json"))
                if decoded == 0:
                    if manifests:
                        raise RuntimeError(
                            "bounded DISPLAY_JOURNAL probe produced raw artifacts without decoded rows"
                        )
                    print(
                        json.dumps(
                            {
                                "event": "java_worker",
                                "phase": "skip_retrieve_empty",
                                "receiver": window.end.receiver,
                                "start_sequence": window.start.sequence,
                                "end_sequence": window.end.sequence,
                            }
                        ),
                        flush=True,
                    )
                    return CapturedWindow(scanned_to=window.end)
                # The probe has already scanned and decoded this tail window
                # and written its raw batch. Repeating the same window through
                # RetrieveJournal adds a second IBM i round trip to every
                # isolated update. Backlog windows still use the faster RJ path.
                if len(manifests) != 1:
                    raise RuntimeError(
                        "bounded DISPLAY_JOURNAL probe decoded rows without raw artifacts"
                    )
                manifest_path = manifests[0]
                payload_path = manifest_path.with_name(
                    manifest_path.name.removesuffix(".manifest.json") + ".jsonl"
                )
                if not payload_path.exists():
                    raise RuntimeError(
                        "bounded DISPLAY_JOURNAL probe produced an incomplete raw batch"
                    )
                elapsed_ms = _summary_int(probe_summaries[0], "elapsed_ms")
                print(
                    json.dumps(
                        {
                            "event": "retrieve_summary",
                            "reader_path": "sql_tail",
                            "decoded": decoded,
                            "elapsed_ms": elapsed_ms,
                            "events_per_sec": (
                                round(decoded * 1000.0 / elapsed_ms, 3) if elapsed_ms > 0 else None
                            ),
                        }
                    ),
                    flush=True,
                )
                return CapturedWindow(
                    scanned_to=window.end,
                    manifest=manifest_path.read_bytes(),
                    payload=payload_path.read_bytes(),
                )
        with tempfile.TemporaryDirectory(prefix="as400-capture-") as directory:
            request = {**request, "raw_directory": directory}
            output = self.worker.process_window(request)
            summary_lines = [
                line.strip()
                for line in output.splitlines()
                if line.startswith("summary ")
            ]
            if len(summary_lines) != 1 or "scan_complete=true" not in summary_lines[0]:
                raise RuntimeError("bounded IBM i reader did not certify scan completion")
            decoded = _summary_int(summary_lines[0], "decoded")
            elapsed_ms = _summary_int(summary_lines[0], "elapsed_ms")
            events_per_sec = (
                round(decoded * 1000.0 / elapsed_ms, 3) if elapsed_ms > 0 else None
            )
            print(
                json.dumps(
                    {
                        "event": "retrieve_summary",
                        "decoded": decoded,
                        "elapsed_ms": elapsed_ms,
                        "events_per_sec": events_per_sec,
                    }
                ),
                flush=True,
            )

            manifests = list(Path(directory).glob("*.manifest.json"))
            if not manifests:
                if decoded > 0:
                    raise RuntimeError(
                        "bounded IBM i reader decoded events without raw artifacts"
                    )
                return CapturedWindow(scanned_to=window.end)
            if len(manifests) != 1:
                raise RuntimeError("bounded IBM i reader produced multiple manifests")
            manifest_path = manifests[0]
            payload_path = manifest_path.with_name(
                manifest_path.name.removesuffix(".manifest.json") + ".jsonl"
            )
            if not payload_path.exists():
                raise RuntimeError("bounded IBM i reader produced an incomplete raw batch")
            return CapturedWindow(
                scanned_to=window.end,
                manifest=manifest_path.read_bytes(),
                payload=payload_path.read_bytes(),
            )


class SqlJavaWindowRunner(WindowRunner):
    """One DISPLAY_JOURNAL window through the persistent JDBC worker."""

    def __init__(
        self,
        worker: PersistentJavaWorker,
        *,
        max_decoded_entries: int,
    ) -> None:
        if max_decoded_entries < 1:
            raise ValueError("max_decoded_entries must be positive")
        self.worker = worker
        self.max_decoded_entries = max_decoded_entries

    def capture(self, window: CaptureWindow) -> CapturedWindow:
        span = window.end.sequence - window.start.sequence + 1
        if span < 1:
            raise RuntimeError("bounded DISPLAY_JOURNAL window is empty")
        with tempfile.TemporaryDirectory(prefix="as400-sql-capture-") as directory:
            request = {
                "receiver": window.end.receiver,
                "receiver_library": window.receiver_library,
                "start_sequence": window.start.sequence,
                "end_sequence": window.end.sequence,
                "max_server_entries": span,
                "max_decoded_entries": max(self.max_decoded_entries, span),
                "raw_directory": directory,
            }
            output = self.worker.sql_window(request)
            summary_lines = [
                line.strip()
                for line in output.splitlines()
                if line.startswith("summary ")
            ]
            if len(summary_lines) != 1 or "scan_complete=true" not in summary_lines[0]:
                raise SqlWindowIncomplete("bounded DISPLAY_JOURNAL did not certify scan completion")
            manifests = list(Path(directory).glob("*.manifest.json"))
            if manifests:
                if len(manifests) != 1:
                    raise RuntimeError("DISPLAY_JOURNAL produced multiple manifests")
                manifest_path = manifests[0]
                payload_path = manifest_path.with_name(
                    manifest_path.name.removesuffix(".manifest.json") + ".jsonl"
                )
                if not payload_path.exists():
                    raise RuntimeError("DISPLAY_JOURNAL produced an incomplete raw batch")
                return CapturedWindow(
                    scanned_to=window.end,
                    manifest=manifest_path.read_bytes(),
                    payload=payload_path.read_bytes(),
                )
            from .sql_window import captured_window_from_sql_output

            # The journal name is the one the worker actually queries: it comes
            # from the declared environment, never from a code default.
            journal = os.environ.get("AS400_JOURNAL_NAME", "").strip()
            if not journal:
                raise RuntimeError("AS400_JOURNAL_NAME is required")
            return captured_window_from_sql_output(
                output,
                window,
                journal=journal,
                library=self.worker.schema,
                table=self.worker.table,
            )


def _summary_int(summary: str, name: str) -> int:
    prefix = name + "="
    for token in summary.split():
        if token.startswith(prefix):
            try:
                value = int(token[len(prefix) :])
            except ValueError as error:
                raise RuntimeError("bounded IBM i reader summary is invalid") from error
            if value < 0:
                raise RuntimeError("bounded IBM i reader summary is invalid")
            return value
    raise RuntimeError("bounded IBM i reader summary is invalid")


def _drain_stderr(process: subprocess.Popen[str]) -> None:
    stream = process.stderr
    if stream is None:
        return

    def discard() -> None:
        try:
            while True:
                line = stream.readline()
                if line == "":
                    return
                text = line.strip()
                if text:
                    print(json.dumps({"event": "java_stderr", "line": text[:300]}), flush=True)
        except Exception:
            return

    threading.Thread(target=discard, name="as400-java-stderr", daemon=True).start()
