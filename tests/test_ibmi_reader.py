from __future__ import annotations

from decimal import Decimal
import unittest

from quadringent.contract import JournalPosition
from quadringent.ibmi_reader import IbmiJournalReader, JournalReceiver, JournalReceiverChain


class FakeCursor:
    def __init__(self, responses: list[tuple[list[tuple[str]], list[tuple[object, ...]]]]) -> None:
        self._responses = responses
        self.description: list[tuple[str]] = []
        self.executed: list[str] = []

    def execute(self, query: str) -> None:
        self.executed.append(query)
        description, rows = self._responses.pop(0)
        self.description = description
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, responses: list[tuple[list[tuple[str]], list[tuple[object, ...]]]]) -> None:
        self.cursor_instance = FakeCursor(responses)

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


def response(columns: list[str], *rows: tuple[object, ...]) -> tuple[list[tuple[str]], list[tuple[object, ...]]]:
    return ([(column,) for column in columns], list(rows))


def receiver(name: str, first: int, last: int) -> JournalReceiver:
    return JournalReceiver(
        journal_receiver_library="DEMOLIB",
        journal_receiver_name=name,
        status="ONLINE",
        attach_timestamp="2026-08-18",
        first_sequence_number=first,
        last_sequence_number=last,
        entry_count=None,
    )


class IbmiJournalReaderTests(unittest.TestCase):
    def test_discover_object_maps_journal_metadata(self) -> None:
        connection = FakeConnection(
            [
                response(
                    [
                        "JOURNAL_LIBRARY",
                        "JOURNAL_NAME",
                        "OBJECT_LIBRARY",
                        "OBJECT_NAME",
                        "OBJECT_TYPE",
                        "JOURNAL_IMAGES",
                    ],
                    ("DEMOLIB", "DEMOJRN", "SALES", "CNTR", "*FILE", "*AFTER"),
                )
            ]
        )

        result = IbmiJournalReader(connection).discover_object("SALES", "CNTR")

        self.assertEqual(result.journal_name, "DEMOJRN")
        self.assertEqual(result.journal_images, "*AFTER")
        self.assertIn("OBJECT_NAME = 'CNTR'", connection.cursor_instance.executed[0])

    def test_journal_info_and_receiver_normalize_decimal_columns(self) -> None:
        connection = FakeConnection(
            [
                response(
                    [
                        "JOURNAL_LIBRARY",
                        "JOURNAL_NAME",
                        "JOURNAL_STATE",
                        "NUMBER_JOURNAL_RECEIVERS",
                        "TOTAL_SIZE_JOURNAL_RECEIVERS",
                        "NUMBER_REMOTE_JOURNALS",
                    ],
                    ("DEMOLIB", "DEMOJRN", "*ACTIVE", Decimal("42"), Decimal("123"), 1),
                ),
                response(
                    [
                        "JOURNAL_RECEIVER_LIBRARY",
                        "JOURNAL_RECEIVER_NAME",
                        "STATUS",
                        "ATTACH_TIMESTAMP",
                        "NUMBER_OF_JOURNAL_ENTRIES",
                        "FIRST_SEQUENCE_NUMBER",
                        "LAST_SEQUENCE_NUMBER",
                    ],
                    ("DEMOLIB", "DEMOJRN3677", "ATTACHED", "2026-08-18", Decimal("11"), Decimal("10"), Decimal("20")),
                ),
            ]
        )
        reader = IbmiJournalReader(connection)

        journal = reader.journal_info("DEMOLIB", "DEMOJRN")
        receivers = reader.latest_receivers("DEMOLIB", "DEMOJRN", limit=1)

        self.assertEqual(journal.receiver_count, 42)
        self.assertEqual(journal.remote_journal_count, 1)
        self.assertEqual(receivers[0].last_sequence_number, 20)
        self.assertEqual(receivers[0].entry_count, 11)

    def test_read_entries_is_object_filtered_and_bounded(self) -> None:
        connection = FakeConnection(
            [
                response(
                    ["SEQUENCE_NUMBER", "JOURNAL_CODE", "OBJECT", "ENTRY_DATA"],
                    (Decimal("100"), "R", "CNTR", b"payload"),
                )
            ]
        )
        reader = IbmiJournalReader(connection)

        rows = reader.read_entries(
            "DEMOLIB",
            "DEMOJRN",
            receiver_library="DEMOLIB",
            starting=JournalPosition("DEMOJRN3677", 100),
            ending=JournalPosition("DEMOJRN3677", 120),
            object_library="SALES",
            object_name="CNTR",
            max_rows=25,
        )

        self.assertEqual(rows[0]["SEQUENCE_NUMBER"], Decimal("100"))
        query = connection.cursor_instance.executed[0]
        self.assertIn("OBJECT_LIBRARY => 'SALES'", query)
        self.assertIn("OBJECT_NAME => 'CNTR'", query)
        self.assertIn("OBJECT_OBJTYPE => '*FILE'", query)
        self.assertIn("HEX(ENTRY_DATA) AS ENTRY_DATA_HEX", query)
        self.assertIn("FETCH FIRST 25 ROWS ONLY", query)

    def test_read_entries_rejects_implicit_receiver_rotation(self) -> None:
        reader = IbmiJournalReader(FakeConnection([]))

        with self.assertRaises(ValueError):
            reader.read_entries(
                "DEMOLIB",
                "DEMOJRN",
                receiver_library="DEMOLIB",
                starting=JournalPosition("DEMOJRN3677", 100),
                ending=JournalPosition("DEMOJRN3676", 120),
                object_library="SALES",
                object_name="CNTR",
            )

    def test_metadata_only_read_does_not_select_entry_blob(self) -> None:
        connection = FakeConnection(
            [
                response(
                    ["SEQUENCE_NUMBER", "JOURNAL_CODE", "OBJECT"],
                    (Decimal("100"), "R", "SALE"),
                )
            ]
        )
        reader = IbmiJournalReader(connection)

        reader.read_entries(
            "DEMOLIB",
            "DEMOJRN",
            receiver_library="DEMOLIB",
            starting=JournalPosition("DEMOJRN3677", 100),
            object_library="SALES",
            object_name="SALE",
            max_rows=1,
            include_entry_data=False,
        )

        query = connection.cursor_instance.executed[0]
        self.assertIn("SEQUENCE_NUMBER", query)
        self.assertNotIn("SELECT *", query)
        self.assertNotIn("ENTRY_DATA", query)

    def test_explicit_receiver_chain_transitions_only_at_contiguous_boundary(self) -> None:
        first = receiver("DEMOJRN3676", 145233722, 150170615)
        second = receiver("DEMOJRN3677", 150170616, 155104408)
        chain = JournalReceiverChain((first, second))

        next_position = chain.next_position(JournalPosition("DEMOJRN3676", 150170615))

        self.assertEqual(next_position, JournalPosition("DEMOJRN3677", 150170616))

        with self.assertRaises(ValueError):
            chain.next_position(JournalPosition("DEMOJRN3676", 150170614))

    def test_explicit_receiver_chain_rejects_a_sequence_gap(self) -> None:
        with self.assertRaises(ValueError):
            JournalReceiverChain(
                (
                    receiver("DEMOJRN3676", 145233722, 150170615),
                    receiver("DEMOJRN3678", 155104409, 157028659),
                )
            )


if __name__ == "__main__":
    unittest.main()
