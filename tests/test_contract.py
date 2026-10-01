from __future__ import annotations

import unittest

from quadringent.contract import (
    ChangeEvent,
    EventIdentityConflict,
    JournalPosition,
    OffsetLedger,
    deduplicate,
)


def make_event(*, sequence: int = 100, after: dict[str, object] | None = None) -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi",
        journal="QA_JRN",
        library="SALES",
        table="CNTR",
        operation="u",
        position=JournalPosition(receiver="QA0001", sequence=sequence),
        commit_timestamp="2026-08-18T10:00:00Z",
        schema_version="sha256:test-schema",
        before={"PYPA": "FR"},
        after=after if after is not None else {"PYPA": "FR", "PYLIB": "France"},
    )


class ChangeEventContractTests(unittest.TestCase):
    def test_technical_before_and_after_images_are_distinct_events(self) -> None:
        before = ChangeEvent(
            source_system="ibmi",
            journal="QA_JRN",
            library="SALES",
            table="CNTR",
            operation="u_before",
            position=JournalPosition(receiver="QA0001", sequence=100),
            commit_timestamp="2026-08-18T10:00:00Z",
            schema_version="sha256:test-schema",
            before={"PYPA": "FR"},
            after=None,
        )
        after = ChangeEvent(
            source_system="ibmi",
            journal="QA_JRN",
            library="SALES",
            table="CNTR",
            operation="u_after",
            position=JournalPosition(receiver="QA0001", sequence=101),
            commit_timestamp="2026-08-18T10:00:00Z",
            schema_version="sha256:test-schema",
            before=None,
            after={"PYPA": "FR", "PYLIB": "France"},
        )

        self.assertNotEqual(before.event_id, after.event_id)
        self.assertEqual(before.to_record()["operation"], "u_before")
        self.assertEqual(after.to_record()["operation"], "u_after")

    def test_technical_image_event_rejects_the_wrong_image_side(self) -> None:
        with self.assertRaises(ValueError):
            ChangeEvent(
                source_system="ibmi",
                journal="QA_JRN",
                library="SALES",
                table="CNTR",
                operation="u_before",
                position=JournalPosition(receiver="QA0001", sequence=100),
                commit_timestamp="2026-08-18T10:00:00Z",
                schema_version="sha256:test-schema",
                before={"PYPA": "FR"},
                after={"PYPA": "FR"},
            )

    def test_event_rejects_non_object_row_images(self) -> None:
        record = make_event().to_record()
        for field, value in (("before", "not-an-object"), ("after", ["not", "an", "object"])):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "row images must be objects"
            ):
                invalid = dict(record)
                invalid[field] = value
                ChangeEvent.from_record(invalid)

    def test_event_id_is_stable_for_replayed_event(self) -> None:
        first = make_event()
        replay = make_event()

        self.assertEqual(first.event_id, replay.event_id)
        self.assertEqual(first.to_record(), replay.to_record())

    def test_identical_replay_is_kept_once(self) -> None:
        event = make_event()

        result = deduplicate([event, event, make_event(sequence=101)])

        self.assertEqual([item.position.sequence for item in result], [100, 101])

    def test_same_position_with_different_payload_is_rejected(self) -> None:
        event = make_event()
        conflicting = make_event(after={"PYPA": "BE", "PYLIB": "Belgique"})

        with self.assertRaises(EventIdentityConflict):
            deduplicate([event, conflicting])

    def test_offset_advances_only_after_raw_commit(self) -> None:
        ledger = OffsetLedger()
        position = JournalPosition(receiver="QA0001", sequence=100)

        ledger.observe(position)
        self.assertIsNone(ledger.committed)

        ledger.commit_raw(position)

        self.assertEqual(ledger.committed, position)

    def test_offset_cannot_move_backwards(self) -> None:
        ledger = OffsetLedger()
        later = JournalPosition(receiver="QA0001", sequence=101)
        earlier = JournalPosition(receiver="QA0001", sequence=100)

        ledger.observe(later)
        ledger.commit_raw(later)

        with self.assertRaises(ValueError):
            ledger.commit_raw(earlier)

    def test_receiver_rotation_requires_explicit_ordering(self) -> None:
        ledger = OffsetLedger()
        ledger.observe(JournalPosition(receiver="QA0001", sequence=100))

        with self.assertRaises(ValueError):
            ledger.observe(JournalPosition(receiver="QA0002", sequence=1))


if __name__ == "__main__":
    unittest.main()
