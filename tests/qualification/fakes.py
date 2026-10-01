"""Fakes en mémoire des adaptateurs, pour tester l'orchestrateur hors ligne.

Ces fakes ne contactent jamais un système réel. ``FakeSourceDriver`` interprète
les instructions SQL produites par ``schema.insert_sql``/``update_sql``/
``delete_sql`` avec un mini-analyseur suffisant pour ce format déterministe
précis — ce n'est pas un moteur SQL général.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from quadringent_qualification.adapters import CaptureBoundary, CaptureResult, SourceResult, RawReplayEvidence

_INSERT = re.compile(r"^INSERT INTO (\S+) \((.+)\) VALUES \((.+)\)$")
_UPDATE = re.compile(r"^UPDATE (\S+) SET (.+) WHERE (\S+) = (.+)$")
_DELETE = re.compile(r"^DELETE FROM (\S+) WHERE (\S+) = (.+)$")


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    in_quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'" and not (in_quote and text[i:i + 2] == "''"):
            in_quote = not in_quote
        elif ch == "'" and in_quote and text[i:i + 2] == "''":
            current += "''"
            i += 2
            continue
        if ch == "," and not in_quote:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts


def _parse_literal(text: str) -> Any:
    text = text.strip()
    if text == "NULL":
        return None
    if text.startswith("TIMESTAMP '") and text.endswith("'"):
        return text[len("TIMESTAMP '"):-1]
    if text.startswith("DATE '") and text.endswith("'"):
        return text[len("DATE '"):-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if re.match(r"^-?\d+$", text):
        return int(text)
    return text  # décimal ou autre : conservé en texte, canonicalisé plus tard


@dataclass
class FakeSourceDriver:
    """Table en mémoire + position de journal simulée."""

    rows: dict[Any, dict[str, Any]] = field(default_factory=dict)
    primary_key: str = "ORDER_ID"
    receiver: str = "R1"
    receiver_library: str = "QUALIF_LIB"
    last_sequence: int = 0
    exec_fails: bool = False
    rotate_calls: int = 0
    receiver_chain: list[str] = field(default_factory=list)
    receiver_ends: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.receiver_chain:
            self.receiver_chain.append(self.receiver)

    def execute(self, statements: Sequence[str]) -> SourceResult:
        if self.exec_fails:
            return SourceResult(exit_code=5)
        for sql in statements:
            self._apply(sql)
            self.last_sequence += 1
        return SourceResult(exit_code=0, checks=(f"SRC_EXEC_TOTAL={len(statements)}",))

    def _apply(self, sql: str) -> None:
        if match := _INSERT.match(sql):
            _, cols, vals = match.groups()
            columns = [c.strip() for c in cols.split(",")]
            values = [_parse_literal(v) for v in _split_top_level(vals)]
            row = dict(zip(columns, values))
            self.rows[row[self.primary_key]] = row
        elif match := _UPDATE.match(sql):
            _, assignments, pk_col, pk_val = match.groups()
            key = _parse_literal(pk_val)
            for assignment in _split_top_level(assignments):
                col, _, val = assignment.partition("=")
                self.rows[key][col.strip()] = _parse_literal(val)
        elif match := _DELETE.match(sql):
            _, pk_col, pk_val = match.groups()
            key = _parse_literal(pk_val)
            self.rows.pop(key, None)
        else:
            raise ValueError(f"instruction non reconnue par le fake : {sql!r}")

    def tail(self) -> SourceResult:
        import json

        payload = json.dumps({"JOURNAL_RECEIVER_LIBRARY": self.receiver_library,
                               "JOURNAL_RECEIVER_NAME": self.receiver, "STATUS": "ATTACHED",
                               "LAST_SEQUENCE_NUMBER": str(self.last_sequence)})
        return SourceResult(exit_code=0, checks=(f"SRC_TAIL={payload}",))

    def dump(self) -> SourceResult:
        import json

        checks = tuple(f"SRC_ROW={json.dumps({k: str(v) if v is not None else None for k, v in row.items()})}"
                        for row in self.rows.values())
        return SourceResult(exit_code=0, checks=checks)

    def row_positions(self, starting: tuple[str, int]) -> SourceResult:
        import json

        start_receiver, start_sequence = starting
        chain = list(self.receiver_chain)
        if start_receiver not in chain:
            chain.insert(0, start_receiver)
        if self.receiver not in chain:
            chain.append(self.receiver)
        chain = chain[chain.index(start_receiver):]
        checks: list[str] = []
        for index, receiver in enumerate(chain):
            checks.append(f"SRC_RECEIVER={json.dumps({'JOURNAL_RECEIVER_NAME': receiver})}")
            end = self.last_sequence if receiver == self.receiver else self.receiver_ends.get(receiver, 0)
            first = start_sequence if index == 0 else 1
            checks.extend(
                f"SRC_ROWPOS={json.dumps({'JOURNAL_RECEIVER_NAME': receiver, 'SEQUENCE_NUMBER': str(sequence)})}"
                for sequence in range(first, end + 1)
            )
        return SourceResult(exit_code=0, checks=tuple(checks))

    def rotate(self) -> SourceResult:
        self.rotate_calls += 1
        self.receiver_ends[self.receiver] = self.last_sequence
        self.receiver = f"R{self.rotate_calls + 1}"
        self.receiver_chain.append(self.receiver)
        self.last_sequence = 0
        return SourceResult(exit_code=0, checks=("SRC_ROTATED=true",))


@dataclass
class FakeCaptureRunner:
    """Renvoie une séquence d'évènements préparée à l'avance par le test."""

    scripted_events: list[list[Mapping[str, Any]]] = field(default_factory=list)
    exit_code: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, *, label: str, max_seconds: int, bootstrap: CaptureBoundary | None,
            env: Mapping[str, str]) -> CaptureResult:
        self.calls.append({"label": label, "max_seconds": max_seconds,
                           "bootstrap": bootstrap.capture_start if bootstrap is not None else None,
                           "boundary": bootstrap})
        events = self.scripted_events.pop(0) if self.scripted_events else []
        return CaptureResult(exit_code=self.exit_code, events=tuple(events), log=f"{label}.log")


@dataclass
class FakeStorageBackend:
    objects: dict[str, list[str]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    created_at: dict[str, datetime] = field(default_factory=dict)

    def list_objects(self, prefix: str) -> Sequence[str]:
        return [k for k in (*self.objects, *self.blobs) if k.startswith(prefix + "/")]

    def read_lines(self, key: str) -> Sequence[str]:
        return self.objects[key]

    def read_bytes(self, key: str, max_bytes: int) -> bytes:
        payload = self.blobs[key]
        if len(payload) > max_bytes:
            raise ValueError("fake object exceeds read budget")
        return payload

    def object_created_at(self, key: str) -> datetime:
        return self.created_at[key]


@dataclass
class FakeWarehouseLoader:
    events: list[Mapping[str, Any]] = field(default_factory=list)
    raw_rows: int = 0
    raw_distinct: int = 0
    load_calls: list[str] = field(default_factory=list)
    mirror_rows: list[Mapping[str, Any]] = field(default_factory=list)
    replayed_identical: int = 0
    replayed_divergent: int = 0
    identical_event_ids: tuple[str, ...] = ()
    divergent_event_ids: tuple[str, ...] = ()

    def load(self, *, raw_prefix: str) -> None:
        self.load_calls.append(raw_prefix)

    def fetch_events(self, *, schema: str) -> Sequence[Mapping[str, Any]]:
        return self.events

    def fetch_raw_counts(self, *, schema: str) -> tuple[int, int]:
        return self.raw_rows, self.raw_distinct

    def fetch_raw_evidence(self, *, schema: str) -> RawReplayEvidence:
        return RawReplayEvidence(self.raw_rows, self.raw_distinct, self.replayed_identical,
                                 self.replayed_divergent, self.identical_event_ids, self.divergent_event_ids)

    def fetch_mirror_rows(self, *, schema: str) -> Sequence[Mapping[str, Any]]:
        return self.mirror_rows
