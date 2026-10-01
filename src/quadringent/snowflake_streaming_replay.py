from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .site_config import SnowflakeScope
from .snowflake_business_replay import build_synthetic_events
from .snowflake_loader import assert_declared_destination


_RUN_TAG = re.compile(r"^[A-Z0-9_]{8,30}$")


@dataclass(frozen=True)
class StreamingReplayConfig:
    """Isolated names for one synthetic Snowpipe Streaming run."""

    run_tag: str
    scope: SnowflakeScope

    def __post_init__(self) -> None:
        if not _RUN_TAG.fullmatch(self.run_tag):
            raise ValueError("run_tag must contain 8 to 30 uppercase letters, digits or underscores")
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        assert_declared_destination(
            self.scope, self.scope.database, self.scope.schema
        )

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    @property
    def target_table(self) -> str:
        return f"{self.scope.schema}_STREAMING_{self.run_tag}"

    @property
    def channel_name(self) -> str:
        return f"{self.scope.schema}_STREAMING_{self.run_tag}"

    @property
    def qualified_target_table(self) -> str:
        return ".".join(
            _quoted_identifier(value)
            for value in (self.scope.database, self.scope.schema, self.target_table)
        )


def build_stream_rows(
    *, source_library: str, source_table: str
) -> tuple[dict[str, Any], ...]:
    """Convert the existing C/U/D fixture to native Snowpipe row objects."""

    rows: list[dict[str, Any]] = []
    for event in build_synthetic_events(
        source_library=source_library, source_table=source_table
    ):
        rows.append(
            {
                "EVENT_ID": event["event_id"],
                "JOURNAL_RECEIVER": event["journal_receiver"],
                "JOURNAL_SEQUENCE": event["journal_sequence"],
                "OPERATION": event["operation"],
                # Keep VARIANT input as a native object, not a JSON string.
                "PAYLOAD": event,
                "SOURCE_FILE": f"synthetic/{event['event_id']}.jsonl",
            }
        )
    return tuple(rows)


def offset_token(sequence: int) -> str:
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    return f"{sequence:012d}"


def evaluate_streaming_snapshot(
    *,
    row_count: int,
    distinct_event_count: int,
    latest_offset: str | None,
) -> dict[str, Any]:
    expected_offset = offset_token(140)
    if row_count != 5:
        raise ValueError("streaming snapshot row count mismatch")
    if distinct_event_count != 5:
        raise ValueError("streaming snapshot event identity mismatch")
    if latest_offset != expected_offset:
        raise ValueError("streaming offset mismatch")
    return {
        "status": "PASS",
        "row_count": row_count,
        "distinct_event_count": distinct_event_count,
        "latest_offset": latest_offset,
    }


def _safe_identifier(value: str) -> bool:
    return bool(value) and value.replace("_", "").replace("$", "").isalnum()


def _quoted_identifier(value: str) -> str:
    if not _safe_identifier(value):
        raise ValueError("unsafe Snowflake identifier")
    return f'"{value}"'
