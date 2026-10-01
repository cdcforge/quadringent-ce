from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Sequence

from .site_config import SnowflakeScope
from .snowflake_business import SnowflakeBusinessMergePlan


_RUN_TAG = re.compile(r"^[A-Z0-9_]{8,30}$")


@dataclass(frozen=True)
class BusinessReplayConfig:
    """Bounded names for one synthetic C/U/D replay in the declared scope."""

    run_tag: str
    scope: SnowflakeScope
    source_library: str
    source_table: str
    key_columns: tuple[str, ...] = ("ID",)

    def __post_init__(self) -> None:
        if not _RUN_TAG.fullmatch(self.run_tag):
            raise ValueError("run_tag must contain 8 to 30 uppercase letters, digits or underscores")
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        self.plan  # validate all generated identifiers and keys

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    @property
    def canonical_table(self) -> str:
        return f"{self.scope.schema}_CUD_CANONICAL_{self.run_tag}"

    @property
    def target_table(self) -> str:
        return f"{self.scope.schema}_CUD_CURRENT_{self.run_tag}"

    @property
    def plan(self) -> SnowflakeBusinessMergePlan:
        return SnowflakeBusinessMergePlan(
            scope=self.scope,
            canonical_table=self.canonical_table,
            target_table=self.target_table,
            source_library=self.source_library,
            source_table=self.source_table,
            key_columns=self.key_columns,
        )


def build_synthetic_events(
    *, source_library: str, source_table: str
) -> tuple[dict[str, Any], ...]:
    """Return a deterministic fixture covering C/U/key-change/U/C/D semantics."""

    return (
        _event("evt-100", 100, "c", None, {"ID": "A", "VALUE": "one"}, source_library, source_table),
        _event("evt-110", 110, "u", {"ID": "A", "VALUE": "one"}, {"ID": "A", "VALUE": "two"}, source_library, source_table),
        _event("evt-120", 120, "u", {"ID": "A", "VALUE": "two"}, {"ID": "B", "VALUE": "moved"}, source_library, source_table),
        _event("evt-130", 130, "c", None, {"ID": "C", "VALUE": "transient"}, source_library, source_table),
        _event("evt-140", 140, "d", {"ID": "C", "VALUE": "transient"}, None, source_library, source_table),
    )


def build_technical_image_events(
    *, source_library: str, source_table: str
) -> tuple[dict[str, Any], ...]:
    """Return a fixture preserving the two journal entries emitted by ``*BOTH``."""

    return (
        _event("evt-100", 100, "c", None, {"ID": "A", "VALUE": "one"}, source_library, source_table),
        _event("evt-110", 110, "u_before", {"ID": "A", "VALUE": "one"}, None, source_library, source_table),
        _event("evt-111", 111, "u_after", None, {"ID": "A", "VALUE": "two"}, source_library, source_table),
        _event("evt-120", 120, "u_before", {"ID": "A", "VALUE": "two"}, None, source_library, source_table),
        _event("evt-121", 121, "u_after", None, {"ID": "B", "VALUE": "moved"}, source_library, source_table),
        _event("evt-130", 130, "c", None, {"ID": "C", "VALUE": "transient"}, source_library, source_table),
        _event("evt-140", 140, "d", {"ID": "C", "VALUE": "transient"}, None, source_library, source_table),
    )


def evaluate_business_snapshot(
    rows: Iterable[Sequence[Any]],
    *,
    expected_sequence: int = 120,
) -> dict[str, object]:
    """Accept only the expected final row from the synthetic C/U/D fixture."""

    normalized: list[tuple[str, str, int, str]] = []
    for row in rows:
        if len(row) != 4:
            raise ValueError("business snapshot mismatch: unexpected column count")
        key, value, sequence, operation = row
        normalized.append((str(key), str(value), int(sequence), str(operation)))
    normalized.sort(key=lambda item: item[0])
    expected = [("B", "moved", expected_sequence, "upsert")]
    if normalized != expected:
        raise ValueError("business snapshot mismatch")
    return {"status": "PASS", "row_count": 1, "keys": ["B"]}


def _event(
    event_id: str,
    sequence: int,
    operation: str,
    before: dict[str, str] | None,
    after: dict[str, str] | None,
    source_library: str,
    source_table: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source_system": "synthetic",
        "journal": "SYNTHJRN",
        "library": source_library,
        "table": source_table,
        "operation": operation,
        "journal_receiver": "SIM0001",
        "journal_sequence": sequence,
        "commit_timestamp": f"2026-08-20T10:{sequence // 10:02d}:00Z",
        "schema_version": "sha256:synthetic",
        "before": before,
        "after": after,
    }
