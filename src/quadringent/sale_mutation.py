from __future__ import annotations

from collections import Counter
import json
import math
from typing import Any, Iterable, Sequence

from .contract import ChangeEvent, JournalPosition, deduplicate
from .site_config import SiteConfig


MAX_JSONL_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 10_000


def _key_columns(site: SiteConfig) -> tuple[str, ...]:
    if not isinstance(site, SiteConfig):
        raise ValueError("a declared site configuration is required")
    if not site.proof_key_columns:
        raise ValueError("the declared proof key columns are required")
    return site.proof_key_columns


def build_sale_contract_events(*, site: SiteConfig) -> tuple[ChangeEvent, ...]:
    """Synthetic proof-table contract fixture, never a live-data claim."""

    key_columns = _key_columns(site)
    first_key = _key(key_columns, "A")
    transient_key = _key(key_columns, "B")
    first_v1 = {
        **first_key,
        "NOTE": None,
        "PRIVATE_FIELD": "value-one",
    }
    first_v2 = {
        **first_key,
        "NOTE": "updated",
        "PRIVATE_FIELD": "value-two",
        "ADDITIVE_FIELD": None,
    }
    transient_v2 = {
        **transient_key,
        "NOTE": None,
        "PRIVATE_FIELD": "transient",
        "ADDITIVE_FIELD": "present",
    }
    return (
        _event(100, "c", after=first_v1, schema_version="sha256:v1", site=site),
        _event(110, "u_before", before=first_v1, schema_version="sha256:v1", site=site),
        _event(111, "u_after", after=first_v2, schema_version="sha256:v2", site=site),
        _event(120, "c", after=transient_v2, schema_version="sha256:v2", site=site),
        _event(130, "d", before=transient_v2, schema_version="sha256:v2", site=site),
    )


def load_sale_jsonl(payload: bytes) -> tuple[ChangeEvent, ...]:
    """Parse a bounded raw window and verify every declared event identity."""

    if not isinstance(payload, bytes) or not payload:
        raise ValueError("proof JSONL payload must be non-empty bytes")
    if len(payload) > MAX_JSONL_BYTES:
        raise ValueError("proof JSONL payload exceeds the size limit")
    events: list[ChangeEvent] = []
    for line_number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            continue
        if len(events) >= MAX_EVENTS:
            raise ValueError("proof JSONL payload exceeds the event limit")
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("proof JSONL contains invalid JSON") from error
        if not isinstance(record, dict):
            raise ValueError("proof JSONL rows must be objects")
        try:
            event = ChangeEvent.from_record(record)
        except (KeyError, TypeError) as error:
            raise ValueError(f"proof JSONL row {line_number} is invalid") from error
        except ValueError as error:
            if str(error) == "row images must be objects or null":
                raise
            raise ValueError(f"proof JSONL row {line_number} is invalid") from error
        declared_event_id = record.get("event_id")
        if not isinstance(declared_event_id, str) or declared_event_id != event.event_id:
            raise ValueError("proof event identity mismatch")
        events.append(event)
    if not events:
        raise ValueError("proof JSONL payload contains no events")
    return tuple(events)


def evaluate_sale_mutation_window(
    events: Sequence[ChangeEvent],
    *,
    site: SiteConfig,
) -> dict[str, object]:
    """Evaluate a supplied proof-table window without returning business values."""

    key_columns = _key_columns(site)
    if not events:
        raise ValueError("proof mutation window must not be empty")
    if len(events) > MAX_EVENTS:
        raise ValueError("proof mutation window exceeds the event limit")
    unique = tuple(deduplicate(list(events)))
    checks: list[dict[str, object]] = []

    scope_ok = all(
        event.library == site.source_schema and event.table == site.proof_table
        for event in unique
    )
    checks.append(_check("table_scope", "pass" if scope_ok else "breach", "exact_scope" if scope_ok else "scope_mismatch"))

    receivers = {event.position.receiver for event in unique}
    receiver_ok = len(receivers) == 1
    checks.append(_check("receiver_order", "pass" if receiver_ok else "unobserved", "single_receiver" if receiver_ok else "receiver_chain_required"))

    positions = [event.position.sequence for event in unique]
    positions_ok = receiver_ok and all(
        current > previous for previous, current in zip(positions, positions[1:])
    )
    if not receiver_ok:
        checks.append(_check("position_order", "unobserved", "receiver_chain_required"))
    else:
        checks.append(_check("position_order", "pass" if positions_ok else "breach", "strictly_increasing" if positions_ok else "position_not_monotone"))

    invalid_keys = sum(
        _invalid_key_image_count(event, key_columns) for event in unique
    )
    checks.append(_check("business_keys", "pass" if invalid_keys == 0 else "breach", "key_columns_present" if invalid_keys == 0 else "key_missing_or_non_scalar"))

    operation_counts = Counter(event.operation for event in unique)
    required_operations = {"c", "u_before", "u_after", "d"}
    operations_ok = required_operations.issubset(operation_counts)
    checks.append(_check("operation_coverage", "pass" if operations_ok else "unobserved", "technical_cud_observed" if operations_ok else "technical_cud_incomplete"))

    update_images_balanced = (
        operation_counts["u_before"] > 0
        and operation_counts["u_before"] == operation_counts["u_after"]
    )
    checks.append(
        _check(
            "update_image_balance",
            "pass" if update_images_balanced else "unobserved",
            "before_after_counts_match"
            if update_images_balanced
            else "before_after_pair_incomplete",
        )
    )

    null_observed = any(
        _has_business_null(event, key_columns) for event in unique
    )
    # Necessary window-order condition only: counts cannot identify transaction
    # pairs, and a boundary may omit an image. Never label that as source corruption.
    pending_before = 0
    ordered_images = update_images_balanced and positions_ok
    for event in unique:
        if event.operation == "u_before":
            pending_before += 1
        elif event.operation == "u_after":
            if pending_before == 0:
                ordered_images = False
            else:
                pending_before -= 1
    ordered_images = ordered_images and pending_before == 0
    checks.append(_check(
        "update_image_order",
        "pass" if ordered_images else "unobserved",
        "balanced_ordered_image_window" if ordered_images else "ordered_image_window_not_proved",
    ))

    checks.append(_check("business_null", "pass" if null_observed else "unobserved", "null_preserved" if null_observed else "null_not_observed"))

    additive_schema = _has_additive_schema(unique)
    checks.append(_check("additive_schema", "pass" if additive_schema else "unobserved", "additive_field_observed" if additive_schema else "schema_evolution_not_observed"))

    replay_ok = False
    final_row_count: int | None = None
    replay_row_count: int | None = None
    if scope_ok and receiver_ok and positions_ok and invalid_keys == 0:
        state: dict[tuple[object, ...], dict[str, Any]] = {}
        seen: set[str] = set()
        _apply_events(state, seen, unique, key_columns)
        final_row_count = len(state)
        first_fingerprint = _state_fingerprint(state)
        _apply_events(state, seen, unique, key_columns)
        replay_row_count = len(state)
        replay_ok = first_fingerprint == _state_fingerprint(state)
    if not receiver_ok:
        checks.append(_check("idempotent_replay", "unobserved", "receiver_chain_required"))
    else:
        checks.append(_check("idempotent_replay", "pass" if replay_ok else "breach", "same_snapshot" if replay_ok else "replay_not_proved"))

    non_passing = [check for check in checks if check["status"] != "pass"]
    if any(check["status"] == "breach" for check in non_passing):
        status = "breach"
    elif non_passing:
        status = "unobserved"
    else:
        status = "pass"
    return {
        "schema_version": "quadringent-proof-mutation-v1",
        "status": status,
        "input_event_count": len(events),
        "unique_event_count": len(unique),
        "duplicate_replay_count": len(events) - len(unique),
        "operation_counts": {
            operation: operation_counts.get(operation, 0)
            for operation in ("c", "u", "u_before", "u_after", "d")
        },
        "final_row_count": final_row_count,
        "replay_row_count": replay_row_count,
        "checks": checks,
    }


def _event(
    sequence: int,
    operation: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    schema_version: str,
    site: SiteConfig,
) -> ChangeEvent:
    return ChangeEvent(
        source_system="synthetic",
        journal=site.journal_name,
        library=site.source_schema,
        table=site.proof_table,
        operation=operation,
        position=JournalPosition("SIM0001", sequence),
        commit_timestamp=f"2026-09-01T10:00:{sequence % 60:02d}Z",
        schema_version=schema_version,
        before=before,
        after=after,
    )


def _key(key_columns: Sequence[str], suffix: str) -> dict[str, str]:
    return {column: f"{column}-{suffix}" for column in key_columns}


def _images(event: ChangeEvent) -> Iterable[dict[str, Any]]:
    if event.before is not None:
        yield event.before
    if event.after is not None:
        yield event.after


def _key_tuple(
    image: dict[str, Any], key_columns: Sequence[str]
) -> tuple[object, ...]:
    values = tuple(image.get(column) for column in key_columns)
    if any(not _valid_key_value(value) for value in values):
        raise ValueError("business key is missing or non-scalar")
    return values


def _valid_key_value(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def _invalid_key_image_count(
    event: ChangeEvent, key_columns: Sequence[str]
) -> int:
    invalid = 0
    for image in _images(event):
        try:
            _key_tuple(image, key_columns)
        except ValueError:
            invalid += 1
    return invalid


def _has_business_null(event: ChangeEvent, key_columns: Sequence[str]) -> bool:
    keys = frozenset(key_columns)
    return any(
        value is None
        for image in _images(event)
        for name, value in image.items()
        if name not in keys
    )


def _has_additive_schema(events: Sequence[ChangeEvent]) -> bool:
    first_schema = events[0].schema_version
    baseline_fields = set().union(*(set(image) for image in _images(events[0])))
    if not baseline_fields:
        return False
    for event in events[1:]:
        if event.schema_version == first_schema:
            continue
        for image in _images(event):
            if set(image) > baseline_fields:
                return True
    return False


def _apply_events(
    state: dict[tuple[object, ...], dict[str, Any]],
    seen: set[str],
    events: Sequence[ChangeEvent],
    key_columns: Sequence[str],
) -> None:
    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        if event.operation in {"u_before", "d"}:
            assert event.before is not None
            state.pop(_key_tuple(event.before, key_columns), None)
        elif event.operation in {"c", "u_after"}:
            assert event.after is not None
            state[_key_tuple(event.after, key_columns)] = dict(event.after)
        elif event.operation == "u":
            assert event.before is not None and event.after is not None
            before_key = _key_tuple(event.before, key_columns)
            after_key = _key_tuple(event.after, key_columns)
            if before_key != after_key:
                state.pop(before_key, None)
            state[after_key] = dict(event.after)


def _state_fingerprint(state: dict[tuple[object, ...], dict[str, Any]]) -> str:
    normalized = [
        json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for _, row in sorted(state.items(), key=lambda item: repr(item[0]))
    ]
    return json.dumps(normalized, separators=(",", ":"))


def _check(check_id: str, status: str, reason: str) -> dict[str, object]:
    return {"id": check_id, "status": status, "reason": reason}


def __getattr__(name: str):
    """Compatibilité paresseuse : les colonnes de preuve du site à l'appel."""

    from .site_config import current as _current  # noqa: PLC0415

    if name == "SALE_KEY_COLUMNS":
        return _current().proof_key_columns
    raise AttributeError(name)
