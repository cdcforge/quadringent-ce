"""Isochrone comparison on the commit instant, not the journal sequence.

IBM i ``SEQUENCE_NUMBER`` and Debezium ``source.sequence`` are not the same
scale: measured offset 5..54 with 16 distinct values on a 23-event band
(img2 2026-08-26), and +7 on the n=1 soak reference. Joining on the sequence
therefore reports missing+extra for events both systems captured, which is
what made G5 fail.

The commit instant is a shared identity: both sides derive it from the same
journal entry. R&D keeps microseconds, Popsink emits milliseconds, so the
comparison normalises to the millisecond. Because several events can share a
millisecond, sets would silently collapse them; comparison is done on
multisets so a dropped duplicate is still reported.

Technical fields only. No business payload, no secret.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

OPERATIONS = {"c", "u", "u_before", "u_after", "d"}


def commit_timestamp_to_epoch_ms(value: str, *, offset_hours: int = 0) -> int:
    """Parse an IBM i commit timestamp into epoch milliseconds.

    ``offset_hours`` states the wall-clock offset the IBM i timestamp is
    expressed in (CEST is +2). Microseconds are truncated, never rounded, so
    the value matches Debezium's millisecond field for the same entry.
    """

    text = str(value).strip()
    if not text:
        raise ValueError("commit timestamp must not be empty")
    normalised = text.replace(" ", "T")
    if normalised.endswith("Z"):
        parsed = datetime.fromisoformat(normalised[:-1]).replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.fromisoformat(normalised)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=offset_hours)))
    epoch_us = int(parsed.timestamp() * 1_000_000) + parsed.microsecond % 1
    return epoch_us // 1000


def temporal_key(epoch_ms: int, operation: str) -> str:
    """Stable identity: commit millisecond and operation."""

    instant = int(epoch_ms)
    if instant < 0:
        raise ValueError("isochrone epoch_ms must be non-negative")
    op = str(operation).strip()
    if op not in OPERATIONS:
        raise ValueError(f"unsupported operation: {op}")
    return f"{instant}/{op}"


def key_from_rd_record_temporal(record: Mapping[str, Any], *, offset_hours: int = 0) -> str:
    timestamp = record.get("commit_timestamp")
    operation = record.get("operation")
    if timestamp is None or operation is None:
        raise ValueError("rd record is missing commit_timestamp or operation")
    return temporal_key(
        commit_timestamp_to_epoch_ms(str(timestamp), offset_hours=offset_hours),
        str(operation),
    )


def key_from_popsink_record_temporal(record: Mapping[str, Any]) -> str:
    source = record.get("source")
    if not isinstance(source, Mapping):
        source = {}
    epoch_ms = source.get("ts_ms", record.get("ts_ms", record.get("__SOURCE_TS_MS")))
    operation = record.get("op", record.get("operation", record.get("__OP")))
    if epoch_ms is None or operation is None:
        raise ValueError("popsink record is missing ts_ms or op")
    return temporal_key(int(epoch_ms), _normalise_popsink_operation(str(operation)))


def _normalise_popsink_operation(value: str) -> str:
    op = value.strip()
    lowered = op.lower()
    if lowered in OPERATIONS:
        return lowered
    mapped = {"c": "c", "r": "c", "u": "u", "d": "d"}.get(lowered)
    if mapped is None:
        raise ValueError(f"unsupported popsink operation: {value}")
    return mapped


def _sha256_of_multiset(counter: Counter[str]) -> str:
    payload = json.dumps(sorted(counter.items()), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class TemporalMatrix:
    overlap: int
    extra: int
    missing: int
    rd_count: int
    popsink_count: int
    overlap_sha256: str
    extra_sha256: str
    missing_sha256: str

    @property
    def complete(self) -> bool:
        """True when neither side lost an event."""

        return self.extra == 0 and self.missing == 0 and self.overlap == self.rd_count

    def to_record(self) -> dict[str, Any]:
        return {
            "overlap": self.overlap,
            "extra": self.extra,
            "missing": self.missing,
            "rd_count": self.rd_count,
            "popsink_count": self.popsink_count,
            "complete": self.complete,
            "overlap_sha256": self.overlap_sha256,
            "extra_sha256": self.extra_sha256,
            "missing_sha256": self.missing_sha256,
            "join_key": "commit_epoch_ms/operation",
        }


def compare_event_multisets(rd: Iterable[str], popsink: Iterable[str]) -> TemporalMatrix:
    """Diff two key multisets. Simultaneous events are kept distinct."""

    rd_counter = Counter(rd)
    popsink_counter = Counter(popsink)
    overlap_counter = rd_counter & popsink_counter
    extra_counter = rd_counter - popsink_counter
    missing_counter = popsink_counter - rd_counter
    return TemporalMatrix(
        overlap=sum(overlap_counter.values()),
        extra=sum(extra_counter.values()),
        missing=sum(missing_counter.values()),
        rd_count=sum(rd_counter.values()),
        popsink_count=sum(popsink_counter.values()),
        overlap_sha256=_sha256_of_multiset(overlap_counter),
        extra_sha256=_sha256_of_multiset(extra_counter),
        missing_sha256=_sha256_of_multiset(missing_counter),
    )


def sequence_offset_profile(
    rd_records: Sequence[Mapping[str, Any]],
    popsink_records: Sequence[Mapping[str, Any]],
    *,
    offset_hours: int = 0,
) -> dict[str, Any]:
    """Characterise the sequence-scale shift, joining on the commit instant.

    Diagnostic only: it explains why the sequence cannot be a join key. It is
    never a completeness gate.
    """

    popsink_by_key: dict[str, list[int]] = {}
    for record in popsink_records:
        key = key_from_popsink_record_temporal(record)
        source = record.get("source")
        source_map = source if isinstance(source, Mapping) else {}
        sequence = source_map.get("sequence", record.get("sequence"))
        if sequence is None:
            continue
        popsink_by_key.setdefault(key, []).append(int(sequence))

    deltas: list[int] = []
    native_matches = 0
    for record in rd_records:
        key = key_from_rd_record_temporal(record, offset_hours=offset_hours)
        candidates = popsink_by_key.get(key)
        if not candidates:
            continue
        rd_sequence = int(record["journal_sequence"])
        best = min(candidates, key=lambda value: abs(rd_sequence - value))
        if best == rd_sequence:
            native_matches += 1
        deltas.append(rd_sequence - best)

    if not deltas:
        return {
            "n": 0,
            "delta_min": None,
            "delta_max": None,
            "distinct_deltas": 0,
            "constant": False,
            "native_sequence_matches": 0,
            "join_key": "commit_epoch_ms/operation",
        }
    distinct = sorted(set(deltas))
    return {
        "n": len(deltas),
        "delta_min": min(deltas),
        "delta_max": max(deltas),
        "distinct_deltas": len(distinct),
        "constant": len(distinct) == 1,
        "native_sequence_matches": native_matches,
        "join_key": "commit_epoch_ms/operation",
    }


def band_comparability(
    *,
    band_low_ms: int,
    band_high_ms: int,
    coverage_low_ms: int | None,
    coverage_high_ms: int | None,
) -> dict[str, Any]:
    """Say whether the reference actually covers the compared band.

    Popsink's Snowflake table has a start of retention and sandbox lots never
    delivered into it. Comparing a band it never covered produces extra>0 with
    missing==0, which must not be read as the R&D tool losing events.
    """

    if coverage_low_ms is None or coverage_high_ms is None:
        return {"comparable": False, "reason": "reference table is empty"}
    if band_low_ms < coverage_low_ms:
        return {"comparable": False, "reason": "band starts before reference coverage"}
    if band_high_ms > coverage_high_ms:
        return {"comparable": False, "reason": "band ends after reference coverage"}
    return {"comparable": True, "reason": "band inside reference coverage"}


def classify_matrix(*, overlap: int, extra: int, missing: int, comparable: bool) -> str:
    """PASS only when the band is comparable and nothing was lost either way."""

    if not comparable:
        return "NOT_COMPARABLE"
    if missing == 0 and extra == 0 and overlap > 0:
        return "PASS"
    return "FAIL"


_IDENTIFIER = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$#"


def reference_table_for(source_table: str, available: Iterable[str]) -> str | None:
    """Route one R&D window to the reference table of the same source.

    Comparing a window captured from ADDRS1 against the SALE reference counts
    every unrelated SALE event as missing, which is a comparator bug, not a
    capture defect. Returns None when the reference has no such table.
    """

    name = str(source_table).strip().upper()
    if not name or any(character not in _IDENTIFIER for character in name):
        raise ValueError("unsafe source table identifier")
    return name if name in {str(item).strip().upper() for item in available} else None


def single_source_table(source_tables: Sequence[str]) -> str:
    """A window must come from exactly one source table to be comparable."""

    distinct = {str(item).strip().upper() for item in source_tables if str(item).strip()}
    if len(distinct) != 1:
        raise ValueError(f"window spans {len(distinct)} source tables, expected 1")
    return distinct.pop()


def best_offset_by_overlap(
    rd_by_offset: Mapping[int, Sequence[str]],
    popsink_by_offset: Mapping[int, Sequence[str]],
) -> int | None:
    """Pick the wall-clock offset that actually aligns the two sides.

    Selecting the offset that returns the most reference rows is wrong: a
    misaligned band can be denser than the aligned one and still overlap
    nothing. Ties go to the smaller offset. Returns None when no offset
    produces any overlap, which means the window is not alignable rather
    than incomplete.
    """

    best_offset: int | None = None
    best_overlap = 0
    for offset in sorted(rd_by_offset):
        popsink = popsink_by_offset.get(offset)
        if not popsink:
            continue
        matrix = compare_event_multisets(rd_by_offset[offset], popsink)
        if matrix.overlap > best_overlap:
            best_overlap = matrix.overlap
            best_offset = offset
    return best_offset
