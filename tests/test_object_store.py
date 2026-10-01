from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import (
    FileObjectStore,
    RawFirstCaptureCoordinator,
    S3ObjectStore,
    read_published_batch,
    publish_raw_artifacts,
    publish_raw_batch,
)
from quadringent.raw import RawBatchReader, RawBatchWriter
from test_raw import event


class ObjectStoreTests(unittest.TestCase):
    def test_new_directories_are_durable_before_success(self) -> None:
        from unittest.mock import patch
        import quadringent.object_store as module
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)
            with patch.object(module,'_sync_directory',wraps=module._sync_directory) as sync:
                FileObjectStore(parent/'nested'/'root').put_once('receipts/a.json',b'proof')
            observed={call.args[0] for call in sync.call_args_list}
            self.assertTrue({parent,parent/'nested',parent/'nested'/'root',parent/'nested'/'root'/'receipts'} <= observed)

    def test_concurrent_identical_content_has_one_creator_and_one_replay(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        import quadringent.object_store as module

        with tempfile.TemporaryDirectory() as directory:
            barrier=Barrier(2)
            original=module.tempfile.mkstemp
            def synchronized_tempfile(*args,**kwargs):
                result=original(*args,**kwargs)
                barrier.wait(timeout=5)
                return result
            def write(_): return FileObjectStore(directory).put_once('receipt.json',b'same')
            with patch.object(module.tempfile,'mkstemp',side_effect=synchronized_tempfile):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(write,[1,2]))
            self.assertEqual(sorted(results),[False,True])
            self.assertEqual(FileObjectStore(directory).get('receipt.json'),b'same')
            self.assertEqual([p.name for p in Path(directory).iterdir()],['receipt.json'])

    def test_concurrent_different_content_never_replaces_the_winner(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        import quadringent.object_store as module

        with tempfile.TemporaryDirectory() as directory:
            barrier = Barrier(2)
            original = module.tempfile.mkstemp
            def synchronized_tempfile(*args, **kwargs):
                result = original(*args, **kwargs)
                barrier.wait(timeout=5)
                return result
            def write(content):
                try:
                    FileObjectStore(directory).put_once('receipt.json', content)
                    return content
                except ValueError:
                    return None
            with patch.object(module.tempfile, 'mkstemp', side_effect=synchronized_tempfile):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(write, [b'first', b'second']))
            winners = [r for r in results if r is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(FileObjectStore(directory).get('receipt.json'), winners[0])
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['receipt.json'])

    def test_publish_is_ordered_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = FileObjectStore(Path(temporary_directory) / "objects")
            result = publish_raw_batch(
                store,
                [event(100)],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )
            replay = publish_raw_batch(
                store,
                [event(100)],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )

            self.assertTrue(result.payload_created)
            self.assertTrue(result.manifest_created)
            self.assertFalse(replay.payload_created)
            self.assertFalse(replay.manifest_created)
            self.assertEqual(len(RawBatchReader(Path(temporary_directory) / "objects").replay()), 1)

    def test_store_rejects_same_key_with_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = FileObjectStore(Path(temporary_directory) / "objects")
            store.put_once("batch.jsonl", b"first")

            with self.assertRaises(ValueError):
                store.put_once("batch.jsonl", b"second")

    def test_capture_commits_after_raw_and_replays_once_after_worker_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkpoint_path = root / "checkpoint.json"
            watermark = JournalPosition("DEMOJRN3677", 101)

            first_worker = RawFirstCaptureCoordinator(
                FileObjectStore(root / "objects"),
                JsonCheckpointStore(checkpoint_path),
            )
            first_result = first_worker.capture(
                [event(100), event(101)],
                high_watermark=watermark,
            )

            restarted_worker = RawFirstCaptureCoordinator(
                FileObjectStore(root / "objects"),
                JsonCheckpointStore(checkpoint_path),
            )
            retry_result = restarted_worker.capture(
                [event(100), event(101)],
                high_watermark=watermark,
            )

            self.assertTrue(first_result.checkpoint_committed)
            self.assertFalse(retry_result.publish.payload_created)
            self.assertFalse(retry_result.publish.manifest_created)
            self.assertEqual(JsonCheckpointStore(checkpoint_path).load(), watermark)
            replay = RawBatchReader(root / "objects").replay()
            self.assertEqual([item.event_id for item in replay], [event(100).event_id, event(101).event_id])

    def test_capture_does_not_commit_checkpoint_when_raw_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkpoint_store = JsonCheckpointStore(root / "checkpoint.json")
            coordinator = RawFirstCaptureCoordinator(FailingObjectStore(), checkpoint_store)

            with self.assertRaises(RuntimeError):
                coordinator.capture(
                    [event(100)],
                    high_watermark=JournalPosition("DEMOJRN3677", 100),
                )

            self.assertIsNone(checkpoint_store.load())

    def test_published_batch_can_be_validated_and_replayed_from_object_store(self) -> None:
        store = S3ObjectStore("dev-bucket", "as400/raw", client=FakeS3Client())
        result = publish_raw_batch(
            store,
            [event(100), event(101)],
            high_watermark=JournalPosition("DEMOJRN3677", 101),
        )
        batch = read_published_batch(store, result.payload_key, result.manifest_key)

        self.assertEqual(
            [item.event_id for item in batch.events],
            [event(100).event_id, event(101).event_id],
        )

    def test_capture_raw_preserves_encoded_bytes_before_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            manifest = RawBatchWriter(source).write_batch(
                [event(100)],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )
            payload_key = f"batch-{manifest.batch_id}.jsonl"
            manifest_key = f"batch-{manifest.batch_id}.manifest.json"
            payload = (source / payload_key).read_bytes()
            manifest_content = (source / manifest_key).read_bytes()
            objects = FileObjectStore(root / "objects")
            checkpoint = JsonCheckpointStore(root / "checkpoint.json")

            result = RawFirstCaptureCoordinator(objects, checkpoint).capture_raw(
                manifest_content,
                payload,
                payload_key=payload_key,
                manifest_key=manifest_key,
            )

            self.assertTrue(result.checkpoint_committed)
            self.assertEqual(objects.get(payload_key), payload)
            self.assertEqual(objects.get(manifest_key), manifest_content)
            self.assertEqual(checkpoint.load(), JournalPosition("DEMOJRN3677", 100))

    def test_publish_raw_artifacts_rejects_manifest_key_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source"
            manifest = RawBatchWriter(source).write_batch(
                [event(100)],
                high_watermark=JournalPosition("DEMOJRN3677", 100),
            )
            payload_key = f"batch-{manifest.batch_id}.jsonl"
            payload = (source / payload_key).read_bytes()
            manifest_content = (source / f"batch-{manifest.batch_id}.manifest.json").read_bytes()

            with self.assertRaises(ValueError):
                publish_raw_artifacts(
                    FileObjectStore(Path(temporary_directory) / "objects"),
                    manifest_content,
                    payload,
                    payload_key=payload_key,
                    manifest_key="batch-wrong.manifest.json",
                )

    def test_s3_adapter_uses_hash_metadata_for_idempotent_put(self) -> None:
        store = S3ObjectStore("dev-bucket", "as400/raw", client=FakeS3Client())

        self.assertTrue(store.put_once("batch.jsonl", b"first"))
        self.assertFalse(store.put_once("batch.jsonl", b"first"))
        with self.assertRaises(ValueError):
            store.put_once("batch.jsonl", b"second")


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            content, metadata = self.objects[(Bucket, Key)]
        except KeyError as error:
            missing = RuntimeError("not found")
            missing.response = {"Error": {"Code": "404"}}
            raise missing from error
        return {"ContentLength": len(content), "Metadata": metadata}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        content, _ = self.objects[(Bucket, Key)]
        return {"Body": MemoryBody(content)}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, Metadata: dict[str, str], IfNoneMatch: str) -> None:
        if (Bucket, Key) in self.objects:
            conflict = RuntimeError("precondition failed")
            conflict.response = {"Error": {"Code": "412"}}
            raise conflict
        self.objects[(Bucket, Key)] = (Body, Metadata)


class MemoryBody:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self) -> bytes:
        return self.content


class FailingObjectStore:
    def put_once(self, key: str, content: bytes) -> bool:
        raise RuntimeError("simulated raw-store outage")


if __name__ == "__main__":
    unittest.main()
