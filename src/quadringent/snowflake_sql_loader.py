"""Historique SQL synchrone et idempotent pour les petits flux CDC.

Le MERGE par EVENT_ID valide l'historique avant le MERGE du miroir. Si le
processus tombe entre les deux, le lot est relu depuis son reçu durable :
l'historique ignore les EVENT_ID présents et le miroir est rejoué.
"""

from __future__ import annotations

import base64
from collections.abc import Collection, Sequence
from decimal import Decimal
from typing import Any

from .contract import ChangeEvent, JournalPosition
from .raw import RawBatch
from .snowflake_destination import TableDestinationPlan, snowflake_type_for
from .snowflake_streaming_loader import HistoryStreamingLoadResult, _history_row

_MAX_MERGE_ROWS = 100
_TECHNICAL_TYPES = (
    ("EVENT_ID", "VARCHAR"),
    ("OPERATION", "VARCHAR"),
    ("JOURNAL_RECEIVER", "VARCHAR"),
    ("JOURNAL_SEQUENCE", "NUMBER(38, 0)"),
    ("COMMIT_TIMESTAMP", "TIMESTAMP_NTZ(6)"),
)


class HistorySqlLoader:
    """Écrit l'historique par MERGE SQL, sans canal Snowpipe ni jeton d'offset.

    Le checkpoint local reste la seule position de reprise ; l'unicité des
    EVENT_ID est assurée par le MERGE, y compris si le checkpoint n'a pas été
    validé après une panne. Une instance ne doit avoir qu'un écrivain par
    table, comme le chargeur Snowpipe existant.
    """

    def __init__(self, *, plan: TableDestinationPlan, cursor: Any) -> None:
        self._plan = plan
        self._cursor = cursor

    @property
    def needs_history_lookup(self) -> bool:
        return False

    def resume_position(self) -> JournalPosition | None:
        return None

    def load_batch(
        self, batch: RawBatch | Sequence[ChangeEvent], *, already_present_ids: Collection[str] = ()
    ) -> HistoryStreamingLoadResult:
        if already_present_ids:
            raise ValueError("SQL history MERGE owns EVENT_ID deduplication")
        events = batch.events if isinstance(batch, RawBatch) else batch
        unique: dict[str, ChangeEvent] = {}
        for event in events:
            previous = unique.get(event.event_id)
            if previous is not None and previous != event:
                raise ValueError("conflicting events for one EVENT_ID")
            unique[event.event_id] = event
        # Valider et normaliser tout le lot avant la première mutation SQL.
        rows = [_normalized_row(self._plan, event) for event in unique.values()]
        inserted = 0
        for start in range(0, len(rows), _MAX_MERGE_ROWS):
            chunk = rows[start : start + _MAX_MERGE_ROWS]
            statement, params = _merge_statement(self._plan, chunk)
            self._cursor.execute(statement, params)
            count = self._cursor.rowcount
            if type(count) is not int or not 0 <= count <= len(chunk):
                raise RuntimeError("Snowflake history MERGE returned an invalid rowcount")
            inserted += count
        return HistoryStreamingLoadResult(
            events_appended=inserted,
            events_skipped_already_committed=len(events) - inserted,
            resumed_from=None,
        )

    def close(self) -> None:
        pass


def _normalized_row(plan: TableDestinationPlan, event: ChangeEvent) -> tuple[Any, ...]:
    row = _history_row(plan, event)
    values: list[Any] = [row[name] for name, _ in _TECHNICAL_TYPES]
    for column in plan.columns:
        value = row[column.name]
        if value is not None and column.type.kind in {"binary", "varbinary", "blob"}:
            if (
                type(value) is not dict
                or set(value) != {"type", "encoding", "value"}
                or value["type"] != "bytes"
                or value["encoding"] != "base64"
                or type(value["value"]) is not str
            ):
                raise ValueError(f"invalid binary envelope for {column.name}")
            try:
                base64.b64decode(value["value"], validate=True)
            except ValueError as exc:
                raise ValueError(f"invalid base64 for {column.name}") from exc
            value = value["value"]
        elif value is not None and not isinstance(value, (str, int, float, bool, Decimal)):
            raise ValueError(f"unsupported value for {column.name}")
        # Une seule représentation VARCHAR par colonne dans VALUES : évite
        # qu'un NULL ou un mélange JSON Number/String change l'inférence de
        # type du lot. Le SELECT applique ensuite le type IBM i déclaré.
        values.append(None if value is None else str(value))
    return tuple(values)


def _merge_statement(plan: TableDestinationPlan, rows: Sequence[tuple[Any, ...]]) -> tuple[str, tuple[Any, ...]]:
    if not rows:
        raise ValueError("history MERGE requires at least one row")
    names = [name for name, _ in _TECHNICAL_TYPES]
    expressions = [
        f"COLUMN{index}::{sql_type} AS {name}"
        for index, (name, sql_type) in enumerate(_TECHNICAL_TYPES, 1)
    ]
    for index, column in enumerate(plan.columns, len(_TECHNICAL_TYPES) + 1):
        names.append(column.name)
        if column.type.kind in {"binary", "varbinary", "blob"}:
            expressions.append(f"TO_BINARY(COLUMN{index}::VARCHAR, 'BASE64') AS {column.name}")
        else:
            expressions.append(f"COLUMN{index}::{snowflake_type_for(column.type)} AS {column.name}")
    width = len(names)
    placeholders = ", ".join("(" + ", ".join(["%s"] * width) + ")" for _ in rows)
    columns = ", ".join(names)
    values = ", ".join(f"source.{name}" for name in names)
    statement = (
        f"MERGE INTO {plan.qualified_history_table} AS target "
        f"USING (SELECT {', '.join(expressions)} FROM VALUES {placeholders}) AS source "
        "ON target.EVENT_ID = source.EVENT_ID "
        f"WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({values})"
    )
    return statement, tuple(value for row in rows for value in row)
