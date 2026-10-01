from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable


@dataclass(frozen=True)
class BenchmarkSample:
    """One comparable measurement from a source-to-raw or E2E run."""

    solution: str
    phase: str
    table: str
    receiver: str
    start_sequence: int
    end_sequence: int
    contract_version: str
    event_count: int
    payload_bytes: int
    elapsed_ms: float
    position_fingerprint: str | None = None
    event_fingerprint: str | None = None
    error_count: int = 0
    lag_sequences: int | None = None
    cost_usd: float | None = None
    cost_provenance: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.solution, self.phase, self.table, self.receiver, self.contract_version)
        ):
            raise ValueError("benchmark identity fields must not be empty")
        if self.start_sequence < 0 or self.end_sequence < self.start_sequence:
            raise ValueError("benchmark sequence range is invalid")
        if self.event_count < 0 or self.payload_bytes < 0 or self.error_count < 0:
            raise ValueError("benchmark counters must be non-negative")
        if self.elapsed_ms < 0:
            raise ValueError("benchmark elapsed_ms must be non-negative")
        if self.position_fingerprint is not None and not self.position_fingerprint.strip():
            raise ValueError("benchmark position fingerprint must not be empty")
        if self.event_fingerprint is not None and not self.event_fingerprint.strip():
            raise ValueError("benchmark event fingerprint must not be empty")
        if self.lag_sequences is not None and self.lag_sequences < 0:
            raise ValueError("benchmark lag must be non-negative")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("benchmark cost must be non-negative")
        if self.cost_provenance is not None and not self.cost_provenance.strip():
            raise ValueError("benchmark cost provenance must not be empty")

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "BenchmarkSample":
        return cls(
            solution=str(record["solution"]),
            phase=str(record["phase"]),
            table=str(record["table"]),
            receiver=str(record["receiver"]),
            start_sequence=int(record["start_sequence"]),
            end_sequence=int(record["end_sequence"]),
            contract_version=str(record["contract_version"]),
            event_count=int(record["event_count"]),
            payload_bytes=int(record["payload_bytes"]),
            elapsed_ms=float(record["elapsed_ms"]),
            position_fingerprint=(
                None
                if record.get("position_fingerprint") is None
                else str(record["position_fingerprint"])
            ),
            event_fingerprint=(
                None if record.get("event_fingerprint") is None else str(record["event_fingerprint"])
            ),
            error_count=int(record.get("error_count", 0)),
            lag_sequences=(
                None if record.get("lag_sequences") is None else int(record["lag_sequences"])
            ),
            cost_usd=None if record.get("cost_usd") is None else float(record["cost_usd"]),
            cost_provenance=(
                None if record.get("cost_provenance") is None else str(record["cost_provenance"])
            ),
        )


def percentile(values: Iterable[float], quantile: float) -> float:
    """Return a linearly interpolated percentile without external packages."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0 <= quantile <= 100:
        raise ValueError("quantile must be between 0 and 100")
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * quantile / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def compare_samples(
    samples: Iterable[BenchmarkSample],
    *,
    required_solutions: Iterable[str] | None = None,
    require_cost: bool = False,
) -> dict[str, object]:
    """Aggregate samples sharing one exact scope.

    The default mode can produce a single-solution diagnostic baseline. A
    strict comparison supplies ``required_solutions`` and ``require_cost`` so
    an operator cannot accidentally treat an incomplete run as a Popsink/R&D
    performance or cost comparison.
    """

    materialized = list(samples)
    if not materialized:
        raise ValueError("benchmark requires at least one sample")
    required = None
    if required_solutions is not None:
        required = {str(solution).strip() for solution in required_solutions}
        if len(required) < 2 or "" in required:
            raise ValueError("strict benchmark requires at least two non-empty solutions")
        observed = {sample.solution for sample in materialized}
        if observed != required:
            missing = sorted(required - observed)
            unexpected = sorted(observed - required)
            detail = []
            if missing:
                detail.append("missing=" + ",".join(missing))
            if unexpected:
                detail.append("unexpected=" + ",".join(unexpected))
            raise ValueError("benchmark solutions are incomplete: " + "; ".join(detail))
    if require_cost:
        for sample in materialized:
            if sample.cost_usd is None or not sample.cost_provenance:
                raise ValueError(
                    "strict benchmark cost requires cost_usd and cost_provenance for every sample"
                )
    scope_fields = (
        "phase",
        "table",
        "receiver",
        "start_sequence",
        "end_sequence",
        "contract_version",
    )
    first = materialized[0]
    scope = {field: getattr(first, field) for field in scope_fields}
    for sample in materialized[1:]:
        for field in scope_fields:
            if getattr(sample, field) != scope[field]:
                raise ValueError(f"benchmark samples are not isochronous: {field}")

    fingerprints = {sample.position_fingerprint for sample in materialized}
    if fingerprints == {None}:
        raise ValueError("benchmark samples require exact position_fingerprint")
    if len(fingerprints) > 1:
        raise ValueError("benchmark samples are not isochronous: position_fingerprint")
    scope["position_fingerprint"] = first.position_fingerprint

    event_fingerprints = {sample.event_fingerprint for sample in materialized}
    if event_fingerprints == {None}:
        raise ValueError("benchmark samples require exact event_fingerprint")
    if len(event_fingerprints) > 1:
        raise ValueError("benchmark samples are not isochronous: event_fingerprint")
    scope["event_fingerprint"] = first.event_fingerprint

    grouped: dict[str, list[BenchmarkSample]] = {}
    for sample in materialized:
        grouped.setdefault(sample.solution, []).append(sample)

    solutions: dict[str, dict[str, object]] = {}
    for solution in sorted(grouped):
        items = grouped[solution]
        elapsed_ms = sum(item.elapsed_ms for item in items)
        events = sum(item.event_count for item in items)
        payload_bytes = sum(item.payload_bytes for item in items)
        costs = [item.cost_usd for item in items if item.cost_usd is not None]
        lags = [item.lag_sequences for item in items if item.lag_sequences is not None]
        seconds = elapsed_ms / 1000
        if costs and len(costs) != len(items):
            raise ValueError(f"benchmark cost is incomplete for solution: {solution}")
        if costs and any(not item.cost_provenance for item in items):
            raise ValueError(f"benchmark cost provenance is missing for solution: {solution}")
        cost = sum(costs) if costs else None
        cost_provenance = (
            sorted({item.cost_provenance for item in items if item.cost_provenance})
            if costs
            else None
        )
        solutions[solution] = {
            "sample_count": len(items),
            "event_count": events,
            "payload_bytes": payload_bytes,
            "error_count": sum(item.error_count for item in items),
            "latency_ms": {
                "p50": _rounded(percentile((item.elapsed_ms for item in items), 50)),
                "p95": _rounded(percentile((item.elapsed_ms for item in items), 95)),
                "p99": _rounded(percentile((item.elapsed_ms for item in items), 99)),
            },
            "throughput_events_per_second": _rounded(events / seconds) if seconds else 0.0,
            "throughput_bytes_per_second": _rounded(payload_bytes / seconds) if seconds else 0.0,
            "lag_sequences_max": max(lags) if lags else None,
            "cost_usd": _rounded(cost) if cost is not None else None,
            "cost_provenance": cost_provenance,
            "cost_usd_per_million_events": (
                _rounded(cost / events * 1_000_000) if cost is not None and events else None
            ),
        }
    return {"scope": scope, "solutions": solutions}


def position_fingerprint(positions: Iterable[tuple[str, int]]) -> str:
    """Return a stable hash for the exact receiver/sequence position set."""

    normalized: list[tuple[str, int]] = []
    for receiver, sequence in positions:
        if not isinstance(receiver, str) or not receiver.strip():
            raise ValueError("benchmark position receiver must not be empty")
        sequence_number = int(sequence)
        if sequence_number < 0:
            raise ValueError("benchmark position sequence must be non-negative")
        normalized.append((receiver.strip(), sequence_number))
    if not normalized:
        raise ValueError("benchmark position set must not be empty")
    payload = json.dumps(sorted(normalized), separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_fingerprint(events: Iterable[tuple[str, int, str, str]]) -> str:
    """Hash exact event identity and operation metadata without exposing payloads."""

    normalized: list[tuple[str, int, str, str]] = []
    for receiver, sequence, event_id, operation in events:
        if not isinstance(receiver, str) or not receiver.strip():
            raise ValueError("benchmark event receiver must not be empty")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("benchmark event id must not be empty")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("benchmark event operation must not be empty")
        sequence_number = int(sequence)
        if sequence_number < 0:
            raise ValueError("benchmark event sequence must be non-negative")
        normalized.append(
            (
                receiver.strip(),
                sequence_number,
                event_id.strip(),
                operation.strip().upper(),
            )
        )
    if not normalized:
        raise ValueError("benchmark event set must not be empty")
    payload = json.dumps(sorted(normalized), separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def samples_from_continuous_log(
    records: Iterable[dict[str, Any]],
    *,
    solution: str,
    phase: str,
    table: str,
    contract_version: str,
    position_fingerprint: str | None = None,
    event_fingerprint: str | None = None,
    receiver: str | None = None,
    start_sequence: int | None = None,
    end_sequence: int | None = None,
    cost_usd: float | None = None,
    cost_provenance: str | None = None,
) -> list[BenchmarkSample]:
    """Convert safe ``capture_poll`` JSON records into benchmark samples.

    A continuous log can contain idle and empty scans as well as published
    windows. Only published windows are benchmark samples. Optional position
    filters are exact on purpose: comparing different journal windows would
    make the result non-isochronous. ``metrics.errors`` is cumulative in the
    runtime log, so each sample receives the delta observed since the previous
    poll record.
    """

    if not all(value.strip() for value in (solution, phase, table, contract_version)):
        raise ValueError("benchmark identity fields must not be empty")
    if position_fingerprint is not None and not position_fingerprint.strip():
        raise ValueError("benchmark position fingerprint must not be empty")
    if event_fingerprint is not None and not event_fingerprint.strip():
        raise ValueError("benchmark event fingerprint must not be empty")
    if (start_sequence is None) != (end_sequence is None):
        raise ValueError("start_sequence and end_sequence must be supplied together")
    if start_sequence is not None:
        if start_sequence < 0 or end_sequence is None or end_sequence < start_sequence:
            raise ValueError("benchmark sequence range is invalid")
    if receiver is not None and not receiver.strip():
        raise ValueError("receiver filter must not be empty")

    samples: list[BenchmarkSample] = []
    previous_errors = 0
    saw_poll = False
    for record_index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"continuous log record {record_index} must be an object")
        if record.get("event") != "capture_poll":
            continue
        saw_poll = True
        metrics = record.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError(f"capture_poll record {record_index} has no metrics object")
        try:
            cumulative_errors = int(metrics["errors"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"capture_poll record {record_index} has invalid errors") from error
        if cumulative_errors < previous_errors:
            raise ValueError("continuous log error counter moved backwards")
        error_count = cumulative_errors - previous_errors
        previous_errors = cumulative_errors

        if record.get("status") != "published":
            continue
        window = record.get("window")
        if not isinstance(window, dict):
            raise ValueError(f"published capture_poll record {record_index} has no window")
        window_receiver = str(window.get("receiver", ""))
        try:
            window_start = int(window["start_sequence"])
            window_end = int(window["end_sequence"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"published capture_poll record {record_index} has invalid window") from error
        if receiver is not None and window_receiver != receiver:
            continue
        if start_sequence is not None and (
            window_start != start_sequence or window_end != end_sequence
        ):
            continue

        last_poll = metrics.get("last_poll")
        if not isinstance(last_poll, dict):
            raise ValueError(f"published capture_poll record {record_index} has no last_poll metrics")
        try:
            event_count = int(record["event_count"])
            payload_bytes = int(last_poll["payload_bytes"])
            elapsed_ms = float(last_poll["poll_ms"])
            lag_value = last_poll.get("lag_sequences")
            lag_sequences = None if lag_value is None else int(lag_value)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"published capture_poll record {record_index} has invalid benchmark metrics") from error
        samples.append(
            BenchmarkSample(
                solution=solution,
                phase=phase,
                table=table,
                receiver=window_receiver,
                start_sequence=window_start,
                end_sequence=window_end,
                contract_version=contract_version,
                event_count=event_count,
                payload_bytes=payload_bytes,
                elapsed_ms=elapsed_ms,
                position_fingerprint=position_fingerprint,
                event_fingerprint=event_fingerprint,
                error_count=error_count,
                lag_sequences=lag_sequences,
                cost_usd=cost_usd,
                cost_provenance=cost_provenance,
            )
        )

    if not saw_poll:
        raise ValueError("continuous log contains no capture_poll records")
    if not samples:
        raise ValueError("continuous log contains no matching published window")
    return samples


def _rounded(value: float) -> float:
    return round(value, 3)
