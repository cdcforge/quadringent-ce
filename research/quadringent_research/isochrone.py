"""Isochrone comparison of technical journal keys. No business payloads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping


def technical_key(receiver: str, sequence: int, operation: str) -> str:
    """Stable identity: receiver/sequence/operation, never a payload field."""

    if not isinstance(receiver, str) or not receiver.strip():
        raise ValueError("isochrone receiver must not be empty")
    sequence_number = int(sequence)
    if sequence_number < 0:
        raise ValueError("isochrone sequence must be non-negative")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("isochrone operation must not be empty")
    return f"{receiver.strip()}/{sequence_number}/{operation.strip()}"


def _sha256_of_keys(keys: Iterable[str]) -> str:
    payload = json.dumps(sorted(keys), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IsochroneMatrix:
    overlap: int
    extra: int
    missing: int
    overlap_sha256: str
    extra_sha256: str
    missing_sha256: str
    rd_count: int
    popsink_count: int

    def to_record(self) -> dict[str, int | str]:
        return {
            "overlap": self.overlap,
            "extra": self.extra,
            "missing": self.missing,
            "overlap_sha256": self.overlap_sha256,
            "extra_sha256": self.extra_sha256,
            "missing_sha256": self.missing_sha256,
            "rd_count": self.rd_count,
            "popsink_count": self.popsink_count,
        }


def compare_key_sets(rd: Iterable[str], popsink: Iterable[str]) -> IsochroneMatrix:
    """Diff two technical key sets. overlap is always an int, including 0."""

    rd_keys = set(rd)
    popsink_keys = set(popsink)
    overlap_keys = rd_keys & popsink_keys
    extra_keys = rd_keys - popsink_keys
    missing_keys = popsink_keys - rd_keys
    return IsochroneMatrix(
        overlap=len(overlap_keys),
        extra=len(extra_keys),
        missing=len(missing_keys),
        overlap_sha256=_sha256_of_keys(overlap_keys),
        extra_sha256=_sha256_of_keys(extra_keys),
        missing_sha256=_sha256_of_keys(missing_keys),
        rd_count=len(rd_keys),
        popsink_count=len(popsink_keys),
    )


def key_from_rd_record(record: Mapping[str, Any]) -> str:
    """Map a raw R&D JSONL event to a technical key."""

    receiver = record.get("journal_receiver")
    sequence = record.get("journal_sequence")
    operation = record.get("operation")
    if receiver is None or sequence is None or operation is None:
        raise ValueError("rd record is missing journal_receiver, journal_sequence or operation")
    return technical_key(str(receiver), int(sequence), str(operation))


def key_from_popsink_record(record: Mapping[str, Any]) -> str:
    """Map a Debezium-like Popsink envelope to a technical key."""

    source = record.get("source")
    if not isinstance(source, Mapping):
        source = {}
    receiver = source.get("receiver", record.get("receiver"))
    sequence = source.get("sequence", record.get("sequence"))
    operation = record.get("op", record.get("operation"))
    if receiver is None or sequence is None or operation is None:
        raise ValueError("popsink record is missing receiver, sequence or operation")
    return technical_key(str(receiver), int(sequence), str(operation))


def parse_technical_key(key: str) -> tuple[str, int, str]:
    receiver, sequence_text, operation = key.split("/", 2)
    return receiver, int(sequence_text), operation


def filter_keys_to_window(
    keys: Iterable[str],
    *,
    receiver: str,
    start_sequence: int,
    end_sequence: int,
) -> set[str]:
    """Keep keys inside one receiver and inclusive sequence bounds."""

    if end_sequence < start_sequence:
        raise ValueError("isochrone window end must be >= start")
    selected: set[str] = set()
    for key in keys:
        key_receiver, sequence, _operation = parse_technical_key(key)
        if key_receiver != receiver:
            continue
        if start_sequence <= sequence <= end_sequence:
            selected.add(key)
    return selected
