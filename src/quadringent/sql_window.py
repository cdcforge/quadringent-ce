from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .continuous import (
    CapturedWindow,
    CaptureWindow,
    PollResult,
    SqlWindowIncomplete,
    SqlWindowTimeout,
)
from .contract import ChangeEvent, JournalPosition
from .ibmi_reader import IbmiJournalReader
from .object_store import RawFirstCaptureCoordinator
from .raw import RawBatchWriter


def _retrieve_summary(*, decoded: int, elapsed_ms: int) -> dict[str, object]:
    events_per_sec = round(decoded * 1000.0 / elapsed_ms, 3) if elapsed_ms > 0 else 0.0
    return {
        "event": "retrieve_summary",
        "decoded": decoded,
        "elapsed_ms": elapsed_ms,
        "events_per_sec": events_per_sec,
    }


class SqlWindowRunner(Protocol):
    def capture(self, window: CaptureWindow) -> CapturedWindow:
        """Read one DISPLAY_JOURNAL window without advancing the checkpoint."""


def capture_sql_window(
    *,
    window: CaptureWindow,
    runner: SqlWindowRunner,
    coordinator: RawFirstCaptureCoordinator,
    checkpoint_store: Any,
    timeout_seconds: float,
    emit: Callable[[dict[str, object]], None] | None = None,
    object_name: str = "CNTR",
) -> PollResult:
    """Fail-closed DISPLAY_JOURNAL capture: timeout and incomplete scans stay put."""

    if timeout_seconds <= 0 or timeout_seconds >= 30:
        raise ValueError("timeout_seconds must be positive and below 30")
    if emit is None:
        emit = lambda _event: None
    emit(
        {
            "event": "retrieve_start",
            "object_name": object_name,
            "receiver": window.start.receiver,
            "start_sequence": window.start.sequence,
            "end_sequence": window.end.sequence,
            "timeout_seconds": timeout_seconds,
        }
    )
    started = time.monotonic()
    captured_box: list[CapturedWindow] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            captured_box.append(runner.capture(window))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, name="as400-sql-window", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    if worker.is_alive():
        emit(
            {
                "event": "window_timeout",
                "timeout_seconds": timeout_seconds,
                "receiver": window.start.receiver,
            }
        )
        raise SqlWindowTimeout("bounded DISPLAY_JOURNAL timed out")
    if errors:
        raise errors[0]
    if not captured_box:
        raise SqlWindowTimeout("bounded DISPLAY_JOURNAL returned no window")
    captured = captured_box[0]
    if captured.scanned_to != window.end:
        emit(
            {
                "event": "window_incomplete",
                "scanned_sequence": captured.scanned_to.sequence,
                "end_sequence": window.end.sequence,
            }
        )
        raise SqlWindowIncomplete(
            "bounded DISPLAY_JOURNAL scan did not reach the planned end"
        )
    if captured.manifest is None:
        coordinator.advance_without_raw(captured.scanned_to, previous=window.rotated_from)
        emit(_retrieve_summary(decoded=0, elapsed_ms=elapsed_ms))
        emit(
            {
                "event": "window_done",
                "status": "empty_scan",
                "decoded": 0,
                "receiver": window.end.receiver,
                "end_sequence": window.end.sequence,
            }
        )
        return PollResult("empty_scan", window, 0)

    from .raw import read_raw_batch

    batch = read_raw_batch(captured.manifest, captured.payload or b"")
    if window.rotated:
        # A window that crosses receivers cannot be committed: commit() refuses
        # a receiver change on purpose. The empty-scan path above already routes
        # through the transition CAS; the published path must do the same, or a
        # rotation carrying events ends the run (measured 2026-08-26, step 3).
        coordinator.capture_raw_transition(
            captured.manifest,
            captured.payload or b"",
            previous=window.rotated_from,
        )
    else:
        coordinator.capture_raw(
            captured.manifest,
            captured.payload or b"",
            payload_key=f"batch-{batch.manifest.batch_id}.jsonl",
            manifest_key=f"batch-{batch.manifest.batch_id}.manifest.json",
        )
    emit(_retrieve_summary(decoded=len(batch.events), elapsed_ms=elapsed_ms))
    emit(
        {
            "event": "window_done",
            "status": "published",
            "decoded": len(batch.events),
            "receiver": window.end.receiver,
            "end_sequence": window.end.sequence,
        }
    )
    return PollResult("published", window, len(batch.events))


def captured_window_from_sql_output(
    output: str,
    window: CaptureWindow,
    *,
    journal: str,
    library: str,
    table: str,
) -> CapturedWindow:
    """Turn JDBC sql_event stdout into a fail-closed captured window."""

    events: list[ChangeEvent] = []
    scan_complete = False
    images: dict[int, dict[str, str]] = {}
    pending: list[dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("sql_fields "):
            fields = _sql_tokens(stripped)
            sequence = int(fields.pop("sequence"))
            images[sequence] = fields
            continue
        if stripped.startswith("sql_event "):
            pending.append(_sql_event_fields(stripped))
            continue
        if stripped.startswith("summary ") and "scan_complete=true" in stripped:
            scan_complete = True
    for fields in pending:
        sequence = int(fields["sequence"])
        row = {
            "SEQUENCE_NUMBER": sequence,
            "JOURNAL_ENTRY_TYPE": fields["type"],
            "ENTRY_TIMESTAMP": fields.get("timestamp", "1970-01-01T00:00:00Z"),
            "COUNT_OR_RRN": fields.get("rrn"),
            "IMAGE": images.get(sequence, {}),
        }
        event = _event_from_sql_row(row, window, journal, library, table)
        if event is not None:
            events.append(event)
    if not scan_complete:
        raise SqlWindowIncomplete("bounded DISPLAY_JOURNAL did not certify scan completion")
    if not events:
        return CapturedWindow(scanned_to=window.end)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="as400-sql-raw-") as directory:
        writer = RawBatchWriter(directory)
        manifest = writer.write_batch(events, high_watermark=window.end)
        payload = Path(directory) / f"batch-{manifest.batch_id}.jsonl"
        manifest_path = Path(directory) / f"batch-{manifest.batch_id}.manifest.json"
        return CapturedWindow(
            scanned_to=window.end,
            manifest=manifest_path.read_bytes(),
            payload=payload.read_bytes(),
        )


def _sql_tokens(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.split()[1:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def _sql_event_fields(line: str) -> dict[str, str]:
    fields = _sql_tokens(line)
    if "sequence" not in fields or "type" not in fields:
        raise SqlWindowIncomplete("sql_event is missing sequence or type")
    return fields


class DisplayJournalWindowReader:
    """Run one object-filtered DISPLAY_JOURNAL window through a DB-API connection."""

    def __init__(
        self,
        reader: IbmiJournalReader,
        *,
        journal_library: str,
        journal_name: str,
        object_library: str,
        object_name: str,
    ) -> None:
        self.reader = reader
        self.journal_library = journal_library
        self.journal_name = journal_name
        self.object_library = object_library
        self.object_name = object_name

    def capture(self, window: CaptureWindow) -> CapturedWindow:
        span = window.end.sequence - window.start.sequence + 1
        if span < 1:
            raise SqlWindowIncomplete("bounded DISPLAY_JOURNAL window is empty")
        rows = self.reader.read_entries(
            self.journal_library,
            self.journal_name,
            receiver_library=window.receiver_library,
            starting=window.start,
            ending=window.end,
            object_library=self.object_library,
            object_name=self.object_name,
            max_rows=min(span, 10000),
            include_entry_data=False,
        )
        if not rows:
            return CapturedWindow(scanned_to=window.end)
        last_sequence = _row_sequence(rows[-1])
        if len(rows) >= span and last_sequence < window.end.sequence:
            return CapturedWindow(
                scanned_to=JournalPosition(window.end.receiver, last_sequence)
            )
        events = [
            event
            for row in rows
            if (event := _event_from_sql_row(row, window, self.journal_name, self.object_library, self.object_name))
            is not None
        ]
        if not events:
            return CapturedWindow(scanned_to=window.end)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="as400-sql-raw-") as directory:
            writer = RawBatchWriter(directory)
            manifest = writer.write_batch(events, high_watermark=window.end)
            payload = Path(directory) / f"batch-{manifest.batch_id}.jsonl"
            manifest_path = Path(directory) / f"batch-{manifest.batch_id}.manifest.json"
            return CapturedWindow(
                scanned_to=window.end,
                manifest=manifest_path.read_bytes(),
                payload=payload.read_bytes(),
            )


def _row_sequence(row: dict[str, Any]) -> int:
    value = row.get("SEQUENCE_NUMBER")
    if value is None:
        raise SqlWindowIncomplete("DISPLAY_JOURNAL row is missing SEQUENCE_NUMBER")
    return int(value)


def _row_rrn(row: dict[str, Any]) -> int | None:
    value = row.get("COUNT_OR_RRN")
    if value is None:
        return None
    if isinstance(value, str) and (not value.strip() or value.strip() == "-"):
        return None
    try:
        rrn = int(value)
    except (TypeError, ValueError) as error:
        raise SqlWindowIncomplete(
            "DISPLAY_JOURNAL row carries an unusable RRN; checkpoint must not advance"
        ) from error
    return rrn if rrn > 0 else None


def _event_from_sql_row(
    row: dict[str, Any],
    window: CaptureWindow,
    journal: str,
    library: str,
    table: str,
) -> ChangeEvent | None:
    entry_type = str(row.get("JOURNAL_ENTRY_TYPE") or "").strip().upper()
    if entry_type in {"BR", "UR", "DR"}:
        # Entree de rollback (aligne sur le chemin Java) : un delete annule
        # (DR) n'est pas un delete — le rejouer supprimerait une ligne qui
        # existe encore. Le checkpoint ne doit pas avancer.
        raise SqlWindowIncomplete(
            "unsupported rollback journal row entry; checkpoint must not advance"
        )
    sequence = _row_sequence(row)
    rrn = _row_rrn(row)
    position = JournalPosition(window.end.receiver, sequence)
    timestamp = str(row.get("ENTRY_TIMESTAMP") or "1970-01-01T00:00:00Z")
    image = {
        key: value
        for key, value in dict(row.get("IMAGE") or {}).items()
        if key not in {"JOURNAL_ENTRY_TYPE", "SEQUENCE_NUMBER", "sequence", "type", "timestamp", "fields"}
    }
    if "_rrn" in image:
        raise SqlWindowIncomplete(
            "journal row image collides with reserved _rrn field; checkpoint must not advance"
        )
    if entry_type in {"PT", "PX", "UB", "UP", "DL"} and not image:
        # IMAGES(*AFTER) : un delete n'a pas d'image — le RRN de l'entete est
        # la seule identite durable de la ligne supprimee. Toute autre entree
        # sans image reste un echec : insert/update sans etat, c'est de la
        # perte de donnees.
        if entry_type == "DL":
            if rrn is None:
                raise SqlWindowIncomplete(
                    "DISPLAY_JOURNAL delete entry carries no row image and no RRN; checkpoint must not advance"
                )
            image = {"_rrn": rrn}
        else:
            raise SqlWindowIncomplete(
                "DISPLAY_JOURNAL row image is incomplete; checkpoint must not advance"
            )
    elif rrn is not None:
        image["_rrn"] = rrn
    if entry_type in {"PT", "PX"}:
        return ChangeEvent(
            source_system="ibmi",
            journal=journal,
            library=library,
            table=table,
            operation="c",
            position=position,
            commit_timestamp=timestamp,
            schema_version="sql-display-journal-v1",
            before=None,
            after=image,
        )
    if entry_type in {"UB", "DL"}:
        return ChangeEvent(
            source_system="ibmi",
            journal=journal,
            library=library,
            table=table,
            operation="u_before" if entry_type == "UB" else "d",
            position=position,
            commit_timestamp=timestamp,
            schema_version="sql-display-journal-v1",
            before=image,
            after=None,
        )
    if entry_type == "UP":
        return ChangeEvent(
            source_system="ibmi",
            journal=journal,
            library=library,
            table=table,
            operation="u_after",
            position=position,
            commit_timestamp=timestamp,
            schema_version="sql-display-journal-v1",
            before=None,
            after=image,
        )
    return None
