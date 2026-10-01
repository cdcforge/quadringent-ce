from __future__ import annotations

import site_fixture

import unittest
from unittest.mock import Mock

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.raw import RawBatch, RawBatchWriter
from quadringent.snowflake_destination import ColumnDefinition, IbmiColumnType, TableDestinationPlan
from quadringent.snowflake_streaming_loader import (
    FakeStreamingClient,
    HistoryStreamingLoader,
    LagQueryPlan,
    MirrorMergePlan,
    StreamingCommitTimeoutError,
    SnowpipeStreamingChannelAdapter,
    channel_name_for,
    decode_offset_token,
    encode_offset_token,
)


SITE = site_fixture.build_test_site()
SCOPE = SITE.snowflake_scope


def _plan() -> TableDestinationPlan:
    return TableDestinationPlan(
        scope=SCOPE,
        history_table="SALE_HISTORY",
        mirror_table="SALE_MIRROR",
        columns=(
            ColumnDefinition(name="ORDER_ID", type=IbmiColumnType("integer"), nullable=False),
            ColumnDefinition(name="LABEL", type=IbmiColumnType("varchar", length=60)),
            ColumnDefinition(name="AMOUNT", type=IbmiColumnType("decimal", precision=9, scale=2)),
        ),
        key_columns=("ORDER_ID",),
    )


def _event(operation: str, *, seq: int, before=None, after=None, receiver="RCV0001") -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi-test",
        journal="TRNJRN",
        library="LEDGER",
        table="SALE",
        operation=operation,
        position=JournalPosition(receiver=receiver, sequence=seq),
        commit_timestamp="2026-09-23T08:00:00.000000",
        schema_version="v1",
        before=before,
        after=after,
    )


def _batch(events: list[ChangeEvent]) -> RawBatch:
    from quadringent.raw import RawBatchManifest
    import hashlib

    manifest = RawBatchManifest(
        batch_id="test-batch",
        format_version=RawBatchWriter.FORMAT_VERSION,
        event_count=len(events),
        event_ids=tuple(e.event_id for e in events),
        high_watermark=events[-1].position,
        payload_sha256=hashlib.sha256(b"x").hexdigest(),
    )
    return RawBatch(manifest=manifest, events=tuple(events))


class ChannelNamingTests(unittest.TestCase):
    def test_channel_name_is_deterministic_and_stable(self) -> None:
        name1 = channel_name_for(SCOPE, "SALE_HISTORY", "LEDGER/SALE")
        name2 = channel_name_for(SCOPE, "SALE_HISTORY", "LEDGER/SALE")
        self.assertEqual(name1, name2)
        self.assertNotIn("/", name1)

    def test_different_stream_ids_produce_different_channels(self) -> None:
        name1 = channel_name_for(SCOPE, "SALE_HISTORY", "LEDGER/SALE")
        name2 = channel_name_for(SCOPE, "SALE_HISTORY", "LEDGER/ORDER")
        self.assertNotEqual(name1, name2)

    def test_empty_stream_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            channel_name_for(SCOPE, "SALE_HISTORY", "")


class OffsetTokenTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        position = JournalPosition(receiver="RCV0001", sequence=42)
        token = encode_offset_token(position)
        self.assertEqual(decode_offset_token(token), position)

    def test_none_token_decodes_to_none(self) -> None:
        self.assertIsNone(decode_offset_token(None))

    def test_tokens_sort_lexicographically_within_a_receiver(self) -> None:
        low = encode_offset_token(JournalPosition(receiver="RCV0001", sequence=3))
        high = encode_offset_token(JournalPosition(receiver="RCV0001", sequence=42))
        self.assertLess(low, high)


class HistoryStreamingLoaderTests(unittest.TestCase):
    def test_channel_without_offset_keeps_history_lookup_for_later_batches(self) -> None:
        client = FakeStreamingClient()
        loader = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        old = _event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"})
        new = _event("c", seq=2, after={"ORDER_ID": 2, "LABEL": "B", "AMOUNT": "20.00"})

        self.assertTrue(loader.needs_history_lookup)
        result = loader.load_batch(_batch([old, new]), already_present_ids={old.event_id})

        self.assertEqual(result.events_appended, 1)
        self.assertEqual(result.events_skipped_already_committed, 1)
        channel = client.channels[loader.channel_name]
        self.assertEqual([row["EVENT_ID"] for row in channel.rows], [new.event_id])
        self.assertTrue(loader.needs_history_lookup)

    def test_low_latency_flushes_only_new_rows_before_waiting_for_commit(self) -> None:
        client = FakeStreamingClient()
        loader = HistoryStreamingLoader(
            plan=_plan(), client=client, stream_id="LEDGER/SALE", flush_each_batch=True
        )
        channel = client.channels[loader.channel_name]
        calls: list[str] = []
        append = channel.append_rows
        wait = channel.wait_for_commit

        def record_append(*args, **kwargs):
            calls.append("append")
            return append(*args, **kwargs)

        def record_wait(*args, **kwargs):
            calls.append("commit")
            return wait(*args, **kwargs)

        channel.append_rows = record_append
        channel.initiate_flush = lambda: calls.append("flush")
        channel.wait_for_commit = record_wait
        batch = _batch([_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"})])

        loader.load_batch(batch)
        self.assertEqual(calls, ["append", "flush", "commit"])
        loader.load_batch(batch)
        self.assertEqual(calls, ["append", "flush", "commit"])

    def test_loads_events_in_order_and_commits_channel(self) -> None:
        client = FakeStreamingClient()
        loader = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        events = [
            _event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"}),
            _event("c", seq=2, after={"ORDER_ID": 2, "LABEL": "B", "AMOUNT": "20.00"}),
        ]
        result = loader.load_batch(_batch(events))

        self.assertEqual(result.events_appended, 2)
        self.assertEqual(result.events_skipped_already_committed, 0)
        self.assertIsNone(result.resumed_from)
        channel = client.channels[loader.channel_name]
        self.assertEqual(len(channel.rows), 2)
        self.assertEqual(channel.rows[0]["EVENT_ID"], events[0].event_id)
        self.assertEqual(channel.rows[0]["LABEL"], "A")
        self.assertEqual(
            channel.latest_committed_offset_token, encode_offset_token(events[-1].position)
        )

    def test_resumes_from_last_committed_offset_token(self) -> None:
        client = FakeStreamingClient()
        loader1 = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        events = [
            _event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"}),
            _event("c", seq=2, after={"ORDER_ID": 2, "LABEL": "B", "AMOUNT": "20.00"}),
        ]
        loader1.load_batch(_batch(events))

        # Nouveau chargeur (redémarrage), même client / même canal.
        loader2 = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        self.assertEqual(loader2.resume_position(), events[-1].position)

        replay_batch = _batch(
            [
                events[1],
                _event("c", seq=3, after={"ORDER_ID": 3, "LABEL": "C", "AMOUNT": "30.00"}),
            ]
        )
        result = loader2.load_batch(replay_batch)

        self.assertEqual(result.events_appended, 1)
        self.assertEqual(result.events_skipped_already_committed, 1)
        channel = client.channels[loader1.channel_name]
        self.assertEqual(len(channel.rows), 3)

    def test_empty_batch_after_resume_filtering_appends_nothing(self) -> None:
        client = FakeStreamingClient()
        loader1 = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"})]
        loader1.load_batch(_batch(events))

        loader2 = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        result = loader2.load_batch(_batch(events))

        self.assertEqual(result.events_appended, 0)
        self.assertEqual(result.events_skipped_already_committed, 1)

    def test_different_channel_name_after_recreation_starts_clean_but_dedup_relies_on_event_id(
        self,
    ) -> None:
        client = FakeStreamingClient()
        loader_v1 = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"})]
        loader_v1.load_batch(_batch(events))

        # Un canal recréé sous un autre nom (incident opérateur) reprend à zéro
        # côté canal : l'historique reçoit une seconde ligne pour le même
        # EVENT_ID. C'est le MERGE miroir (dédup EVENT_ID) qui absorbe ce cas,
        # pas le chargeur historique lui-même.
        loader_v2 = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE-v2")
        result = loader_v2.load_batch(_batch(events))
        self.assertEqual(result.events_appended, 1)
        self.assertNotEqual(loader_v1.channel_name, loader_v2.channel_name)

    def test_commit_timeout_propagates(self) -> None:
        client = FakeStreamingClient()
        loader = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        channel = client.open_channel(loader.channel_name)
        channel.fail_next_commit = True
        events = [_event("c", seq=1, after={"ORDER_ID": 1, "LABEL": "A", "AMOUNT": "10.00"})]

        with self.assertRaises(StreamingCommitTimeoutError):
            loader.load_batch(_batch(events))

    def test_close_closes_the_channel(self) -> None:
        client = FakeStreamingClient()
        loader = HistoryStreamingLoader(plan=_plan(), client=client, stream_id="LEDGER/SALE")
        loader.close()
        self.assertTrue(client.channels[loader.channel_name].closed)


class SnowpipeStreamingChannelAdapterTests(unittest.TestCase):
    def test_initiate_flush_uses_the_sdk_channel(self) -> None:
        raw_channel = Mock()
        adapter = SnowpipeStreamingChannelAdapter(raw_channel, latest_committed_offset_token=None)

        adapter.initiate_flush()

        raw_channel.initiate_flush.assert_called_once_with()


class MirrorMergePlanTests(unittest.TestCase):
    def test_merge_can_apply_only_the_current_journal_window_after_receiver_rotation(self) -> None:
        """Un numéro de séquence remis à 1 doit gagner sur l'ancien receveur
        lorsque son reçu vient après celui-ci dans la chaîne prouvée."""
        new_event_ids = ("a" * 64, "b" * 64)
        sql = MirrorMergePlan(plan=_plan()).merge_sql(event_ids=new_event_ids)
        self.assertIn("WHERE EVENT_ID IN ('" + new_event_ids[0] + "', '" + new_event_ids[1] + "')", sql)
        self.assertIn("ORDER BY JOURNAL_SEQUENCE DESC", sql)

    def test_merge_sql_dedups_by_event_id_then_latest_position(self) -> None:
        merge = MirrorMergePlan(plan=_plan())
        sql = merge.merge_sql()

        self.assertIn('MERGE INTO "ACME_RAW"."IBMI_TEST"."SALE_MIRROR" AS target', sql)
        self.assertIn('FROM "ACME_RAW"."IBMI_TEST"."SALE_HISTORY"', sql)
        self.assertIn("PARTITION BY EVENT_ID ORDER BY INGESTED_AT DESC", sql)
        self.assertIn("PARTITION BY ORDER_ID", sql)
        self.assertIn("ORDER BY JOURNAL_SEQUENCE DESC", sql)
        self.assertIn("WHERE OPERATION != 'u_before'", sql)
        self.assertIn("WHEN MATCHED AND source.OPERATION = 'd' THEN DELETE", sql)
        self.assertIn("WHEN NOT MATCHED AND source.OPERATION != 'd' THEN INSERT", sql)
        self.assertIn("MIRROR_UPDATED_AT = CURRENT_TIMESTAMP()", sql)
        # La clé n'est jamais réécrite dans le SET (elle a servi au JOIN).
        self.assertNotIn("SET ORDER_ID = source.ORDER_ID", sql)

    def test_execute_sends_the_merge_statement(self) -> None:
        class RecordingCursor:
            def __init__(self) -> None:
                self.executed: list[str] = []

            def execute(self, sql: str) -> None:
                self.executed.append(sql)

        cursor = RecordingCursor()
        MirrorMergePlan(plan=_plan()).execute(cursor)
        self.assertEqual(len(cursor.executed), 1)
        self.assertIn("MERGE INTO", cursor.executed[0])


class LagQueryPlanTests(unittest.TestCase):
    def test_history_and_mirror_lag_sql_target_the_right_tables(self) -> None:
        plan = LagQueryPlan(plan=_plan())
        self.assertIn('FROM "ACME_RAW"."IBMI_TEST"."SALE_HISTORY"', plan.history_lag_sql())
        self.assertIn('FROM "ACME_RAW"."IBMI_TEST"."SALE_MIRROR"', plan.mirror_lag_sql())
        # COMMIT_TIMESTAMP est un TIMESTAMP_NTZ en UTC : le comparer à
        # CURRENT_TIMESTAMP() (converti dans le fuseau de session) faussait le
        # retard du décalage du compte (constaté : -24 535 s sur un compte en
        # America/Los_Angeles). SYSDATE() est l'instant présent en UTC, NTZ.
        for sql in (plan.history_lag_sql(), plan.mirror_lag_sql()):
            self.assertIn("DATEDIFF('millisecond', MAX(COMMIT_TIMESTAMP), SYSDATE()) / 1000.0", sql)
            self.assertNotIn("CURRENT_TIMESTAMP()", sql)

    def test_read_returns_measured_lag_in_seconds(self) -> None:
        class RowCursor:
            def __init__(self, rows: list[tuple]) -> None:
                self._rows = iter(rows)

            def execute(self, sql: str) -> None:
                self._current = next(self._rows)

            def fetchone(self):
                return self._current

        cursor = RowCursor([(5.2,), (6.3,)])
        metrics = LagQueryPlan(plan=_plan()).read(cursor)
        self.assertEqual(metrics.history_lag_seconds, 5.2)
        self.assertEqual(metrics.mirror_lag_seconds, 6.3)

    def test_read_never_invents_a_zero_for_an_empty_table(self) -> None:
        class RowCursor:
            def __init__(self, rows: list[tuple]) -> None:
                self._rows = iter(rows)

            def execute(self, sql: str) -> None:
                self._current = next(self._rows)

            def fetchone(self):
                return self._current

        cursor = RowCursor([(None,), (None,)])
        metrics = LagQueryPlan(plan=_plan()).read(cursor)
        self.assertIsNone(metrics.history_lag_seconds)
        self.assertIsNone(metrics.mirror_lag_seconds)


if __name__ == "__main__":
    unittest.main()
