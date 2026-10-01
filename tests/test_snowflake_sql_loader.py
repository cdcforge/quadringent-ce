"""Contrat du chemin SQL synchrone pour les petits lots CDC."""

from __future__ import annotations

import unittest

import site_fixture

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.snowflake_destination import ColumnDefinition, IbmiColumnType, TableDestinationPlan
from quadringent.snowflake_sql_loader import HistorySqlLoader


def _plan(*, binary: bool = False) -> TableDestinationPlan:
    columns = [
        ColumnDefinition("ID", IbmiColumnType("integer"), nullable=False),
        ColumnDefinition("AMOUNT", IbmiColumnType("decimal", precision=9, scale=2)),
        ColumnDefinition("NOTE", IbmiColumnType("varchar", length=60)),
    ]
    if binary:
        columns.append(ColumnDefinition("PAYLOAD", IbmiColumnType("varbinary", length=32)))
    return TableDestinationPlan(
        scope=site_fixture.build_test_site().snowflake_scope,
        history_table="ORDERS_HISTORY", mirror_table="ORDERS_MIRROR",
        columns=tuple(columns), key_columns=("ID",),
    )


def _event(sequence: int, *, after: dict) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi-test", journal="TRNJRN", library="LEDGER", table="ORDERS",
        operation="c", position=JournalPosition("RCV0001", sequence),
        commit_timestamp="2026-09-29T00:00:00.000000", schema_version="v1",
        before=None, after=after,
    )


class _Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.ids: set[str] = set()
        self.rowcount = -1

    def execute(self, sql: str, params: tuple) -> None:
        self.calls.append((sql, params))
        # EVENT_ID est le premier paramètre de chaque rang de la clause VALUES.
        count = sql.count("(%s")
        stride = len(params) // count
        new_ids = {params[i * stride] for i in range(count)} - self.ids
        self.ids.update(new_ids)
        self.rowcount = len(new_ids)


class HistorySqlLoaderTests(unittest.TestCase):
    def test_batch_merge_is_parameterized_and_replay_is_idempotent(self) -> None:
        cursor = _Cursor()
        writer = HistorySqlLoader(plan=_plan(), cursor=cursor)
        events = (
            _event(1, after={"ID": 1, "AMOUNT": "10.50", "NOTE": "O'Reilly"}),
            _event(2, after={"ID": 2, "AMOUNT": "20.00", "NOTE": None}),
        )

        first = writer.load_batch(events)
        replay = writer.load_batch(events)

        self.assertEqual(first.events_appended, 2)
        self.assertEqual(replay.events_appended, 0)
        self.assertEqual(replay.events_skipped_already_committed, 2)
        self.assertIsNone(writer.resume_position())
        self.assertFalse(writer.needs_history_lookup)
        sql, params = cursor.calls[0]
        self.assertIn("MERGE INTO", sql)
        self.assertIn("ON target.EVENT_ID = source.EVENT_ID", sql)
        self.assertIn("COLUMN7::NUMBER(9, 2) AS AMOUNT", sql)
        self.assertNotIn("O'Reilly", sql)
        self.assertIn("O'Reilly", params)
        self.assertEqual(len(cursor.calls), 2)

    def test_binary_base64_is_decoded_and_bad_envelope_is_rejected(self) -> None:
        cursor = _Cursor()
        writer = HistorySqlLoader(plan=_plan(binary=True), cursor=cursor)
        valid = _event(1, after={
            "ID": 1, "AMOUNT": "1.00", "NOTE": "ok",
            "PAYLOAD": {"type": "bytes", "encoding": "base64", "value": "AAF/gP8="},
        })
        writer.load_batch((valid,))
        sql, params = cursor.calls[0]
        self.assertIn("TO_BINARY(COLUMN9::VARCHAR, 'BASE64') AS PAYLOAD", sql)
        self.assertEqual(params[-1], "AAF/gP8=")

        invalid = _event(2, after={
            "ID": 2, "AMOUNT": "2.00", "NOTE": "bad", "PAYLOAD": {"value": "abc"},
        })
        with self.assertRaisesRegex(ValueError, "PAYLOAD"):
            writer.load_batch((invalid,))
        self.assertEqual(len(cursor.calls), 1)

    def test_large_batch_is_chunked_and_inaccurate_rowcount_fails_closed(self) -> None:
        cursor = _Cursor()
        writer = HistorySqlLoader(plan=_plan(), cursor=cursor)
        events = tuple(_event(i, after={"ID": i, "AMOUNT": "1.00", "NOTE": "x"}) for i in range(1, 202))
        result = writer.load_batch(events)
        self.assertEqual(result.events_appended, 201)
        self.assertEqual(len(cursor.calls), 3)

        class NoCountCursor(_Cursor):
            def execute(self, sql: str, params: tuple) -> None:
                super().execute(sql, params)
                self.rowcount = -1

        with self.assertRaisesRegex(RuntimeError, "rowcount"):
            HistorySqlLoader(plan=_plan(), cursor=NoCountCursor()).load_batch(events[:1])
