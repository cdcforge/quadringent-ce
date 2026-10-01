from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from quadringent.contract import ChangeEvent, JournalPosition, OffsetLedger
from quadringent.raw import RawBatchReader, RawBatchWriter, _batch_id, read_raw_batch


def event(sequence: int, receiver: str = "DEMOJRN3677") -> ChangeEvent:
    return ChangeEvent(
        source_system="ibmi",
        journal="DEMOJRN",
        library="SALES",
        table="CNTR",
        operation="u",
        position=JournalPosition(receiver, sequence),
        commit_timestamp="2026-08-18T10:00:00Z",
        schema_version="sha256:test-schema",
        before={"PYPA": "FR"},
        after={"PYPA": "FR", "PYLIB": "France"},
    )


def test_exact_decimal_literal_can_be_preserved_for_typed_sql_loading() -> None:
    sample = event(100)
    record = sample.to_record()
    record["after"]["AMOUNT"] = "12345678901234567890.123456"
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    line = line.replace('"AMOUNT":"12345678901234567890.123456"', '"AMOUNT":12345678901234567890.123456')
    payload = (line + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "batch_id": _batch_id(RawBatchWriter.FORMAT_VERSION, [sample.event_id], sample.position, digest),
        "format_version": RawBatchWriter.FORMAT_VERSION,
        "event_count": 1,
        "event_ids": [sample.event_id],
        "high_watermark": {"receiver": sample.position.receiver, "sequence": sample.position.sequence},
        "payload_sha256": digest,
    }
    raw = json.dumps(manifest).encode()

    exact = read_raw_batch(raw, payload, preserve_decimals=True)

    assert exact.events[0].after["AMOUNT"] == "12345678901234567890.123456"


class RawBatchWriterTests(unittest.TestCase):
    def test_writer_is_idempotent_and_emits_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            watermark = JournalPosition("DEMOJRN3677", 101)

            first = writer.write_batch([event(100), event(101), event(101)], high_watermark=watermark)
            second = writer.write_batch([event(100), event(101)], high_watermark=watermark)

            self.assertEqual(first, second)
            files = sorted(Path(temporary_directory).iterdir())
            self.assertEqual(len(files), 2)
            manifest = json.loads(next(path for path in files if path.name.endswith("manifest.json")).read_text())
            self.assertEqual(manifest["event_count"], 2)
            self.assertEqual(manifest["high_watermark"]["sequence"], 101)

    def test_checkpoint_is_after_raw_write_and_replay_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            ledger = OffsetLedger()
            watermark = JournalPosition("DEMOJRN3677", 100)
            ledger.observe(watermark)

            writer.write_batch([event(100)], high_watermark=watermark)
            self.assertIsNone(ledger.committed)

            ledger.commit_raw(watermark)
            self.assertEqual(ledger.committed, watermark)

            payload = next(Path(temporary_directory).glob("*.jsonl")).read_text()
            record = json.loads(payload)
            self.assertEqual(record["journal_sequence"], 100)

    def test_writer_rejects_events_beyond_high_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ValueError):
                RawBatchWriter(temporary_directory).write_batch(
                    [event(101)],
                    high_watermark=JournalPosition("DEMOJRN3677", 100),
                )

    def test_writer_rejects_events_out_of_journal_sequence_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "ordered"):
                RawBatchWriter(temporary_directory).write_batch(
                    [event(101), event(100)],
                    high_watermark=JournalPosition("DEMOJRN3677", 101),
                )

    def test_reader_verifies_manifest_and_replays_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            watermark = JournalPosition("DEMOJRN3677", 101)
            writer.write_batch([event(100), event(101)], high_watermark=watermark)

            reader = RawBatchReader(temporary_directory)
            batches = reader.read_batches()
            self.assertEqual(len(batches), 1)
            self.assertEqual([item.position.sequence for item in batches[0].events], [100, 101])
            replay = reader.replay()
            self.assertEqual([item.event_id for item in replay], [event(100).event_id, event(101).event_id])

    def test_reader_replays_multiple_batches_by_journal_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            for sequence in range(100, 110):
                writer.write_batch(
                    [event(sequence)],
                    high_watermark=JournalPosition("DEMOJRN3677", sequence),
                )

            batches = RawBatchReader(temporary_directory).read_batches()

            self.assertEqual(
                [batch.manifest.high_watermark.sequence for batch in batches],
                list(range(100, 110)),
            )

    def test_reader_rejects_receiver_rotation_without_an_explicit_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            writer.write_batch(
                [event(100, receiver="DEMOJRN3677")],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )
            writer.write_batch(
                [event(101, receiver="DEMOJRN3678")],
                high_watermark=JournalPosition("DEMOJRN3678", 101),
            )

            with self.assertRaisesRegex(ValueError, "explicit receiver order"):
                RawBatchReader(temporary_directory).read_batches()

            batches = RawBatchReader(
                temporary_directory,
                receiver_order=("DEMOJRN3677", "DEMOJRN3678"),
            ).read_batches()
            self.assertEqual(
                [batch.manifest.high_watermark.receiver for batch in batches],
                ["DEMOJRN3677", "DEMOJRN3678"],
            )

    def test_reader_rejects_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            writer.write_batch(
                [event(100)],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )
            payload_path = next(Path(temporary_directory).glob("*.jsonl"))
            payload_path.write_text(payload_path.read_text() + "\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                RawBatchReader(temporary_directory).read_batches()

    def test_reader_rejects_manifest_identity_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            writer.write_batch(
                [event(100)],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )
            manifest_path = next(Path(temporary_directory).glob("*.manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["batch_id"] = "0" * 32
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(ValueError):
                RawBatchReader(temporary_directory).read_batches()

    def test_reader_rejects_a_coherent_but_out_of_order_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            writer = RawBatchWriter(temporary_directory)
            writer.write_batch(
                [event(100), event(101)],
                high_watermark=JournalPosition("DEMOJRN3677", 101),
            )
            payload_path = next(Path(temporary_directory).glob("*.jsonl"))
            manifest_path = next(Path(temporary_directory).glob("*.manifest.json"))
            payload_lines = payload_path.read_bytes().splitlines(keepends=True)
            reversed_payload = b"".join(reversed(payload_lines))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["event_ids"] = list(reversed(manifest["event_ids"]))
            manifest["payload_sha256"] = hashlib.sha256(reversed_payload).hexdigest()
            manifest["batch_id"] = _batch_id(
                manifest["format_version"],
                manifest["event_ids"],
                JournalPosition(
                    manifest["high_watermark"]["receiver"],
                    manifest["high_watermark"]["sequence"],
                ),
                manifest["payload_sha256"],
            )
            payload_path.write_bytes(reversed_payload)
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "ordered"):
                RawBatchReader(temporary_directory).read_batches()


if __name__ == "__main__":
    unittest.main()
