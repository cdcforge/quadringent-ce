from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from quadringent.contract import JournalPosition


class GcsError(Exception):
    """Mirror of google.api_core exceptions: an integer HTTP ``code``."""

    def __init__(self, code: int) -> None:
        super().__init__(f"gcs {code}")
        self.code = code


class FakeGcsClient:
    """In-memory model of the google-cloud-storage calls used by the backend.

    Every write carries a generation precondition, as the real API does;
    a mismatch raises 412 and a missing object raises 404.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str], int]] = {}
        self._generation = 1000
        self.writes: list[tuple[str, str, int | None]] = []
        self.before_write = None

    def bucket(self, name: str) -> "FakeBucket":
        return FakeBucket(self, name)

    def list_blobs(self, bucket_name: str, *, prefix: str, max_results: int | None = None):
        names = sorted(name for bucket, name in self.objects if bucket == bucket_name and name.startswith(prefix))
        for name in names:
            yield FakeBlob(self, bucket_name, name)


class FakeBucket:
    def __init__(self, client: FakeGcsClient, name: str) -> None:
        self.client = client
        self.name = name

    def blob(self, name: str) -> "FakeBlob":
        return FakeBlob(self.client, self.name, name)

    def get_blob(self, name: str):
        if (self.name, name) not in self.client.objects:
            return None
        blob = FakeBlob(self.client, self.name, name)
        blob.reload()
        return blob


class FakeBlob:
    def __init__(self, client: FakeGcsClient, bucket: str, name: str) -> None:
        self.client = client
        self.bucket_name = bucket
        self.name = name
        self.metadata: dict[str, str] | None = None
        self.generation: int | None = None

    def reload(self) -> None:
        try:
            _, metadata, generation = self.client.objects[(self.bucket_name, self.name)]
        except KeyError as error:
            raise GcsError(404) from error
        self.metadata = dict(metadata)
        self.generation = generation

    def download_as_bytes(self, *, start: int | None = None, end: int | None = None,
                          if_generation_match: int | None = None) -> bytes:
        try:
            content, _, generation = self.client.objects[(self.bucket_name, self.name)]
        except KeyError as error:
            raise GcsError(404) from error
        if if_generation_match is not None and if_generation_match != generation:
            raise GcsError(412)
        if start is None and end is None:
            return content
        return content[(start or 0):(None if end is None else end + 1)]

    def upload_from_string(self, data: bytes, *, content_type: str,
                           if_generation_match: int) -> None:
        if self.client.before_write is not None:
            hook, self.client.before_write = self.client.before_write, None
            hook()
        key = (self.bucket_name, self.name)
        current = self.client.objects.get(key)
        current_generation = 0 if current is None else current[2]
        self.client.writes.append((self.bucket_name, self.name, if_generation_match))
        if if_generation_match != current_generation:
            raise GcsError(412)
        self.client._generation += 1
        self.client.objects[key] = (bytes(data), dict(self.metadata or {}), self.client._generation)
        self.generation = self.client._generation


class GcsObjectStoreTests(unittest.TestCase):
    def store(self, client=None, prefix="qualification/run-1"):
        from quadringent.gcs_backend import GcsObjectStore

        return GcsObjectStore("sandbox-raw", prefix, client=client or FakeGcsClient())

    def test_put_once_creates_with_generation_zero_and_sha256_metadata(self) -> None:
        client = FakeGcsClient()
        store = self.store(client)
        self.assertTrue(store.put_once("batches/a.jsonl", b"payload"))
        content, metadata, _ = client.objects[("sandbox-raw", "qualification/run-1/batches/a.jsonl")]
        self.assertEqual(content, b"payload")
        self.assertEqual(metadata, {"sha256": hashlib.sha256(b"payload").hexdigest()})
        self.assertEqual(client.writes, [("sandbox-raw", "qualification/run-1/batches/a.jsonl", 0)])

    def test_identical_replay_is_idempotent_without_write(self) -> None:
        client = FakeGcsClient()
        store = self.store(client)
        store.put_once("a.json", b"same")
        self.assertFalse(store.put_once("a.json", b"same"))
        self.assertEqual(len(client.writes), 1)

    def test_different_content_is_a_collision(self) -> None:
        store = self.store()
        store.put_once("a.json", b"one")
        with self.assertRaisesRegex(ValueError, "collision"):
            store.put_once("a.json", b"two")

    def test_concurrent_winner_is_checked_not_overwritten(self) -> None:
        client = FakeGcsClient()
        store = self.store(client)
        full = ("sandbox-raw", "qualification/run-1/a.json")

        def rival(content: bytes):
            def write():
                client._generation += 1
                client.objects[full] = (content, {"sha256": hashlib.sha256(content).hexdigest()}, client._generation)
            return write

        client.before_write = rival(b"same")
        self.assertFalse(store.put_once("a.json", b"same"))
        client.objects.clear()
        client.before_write = rival(b"other")
        with self.assertRaisesRegex(ValueError, "collision"):
            store.put_once("a.json", b"mine")
        self.assertEqual(client.objects[full][0], b"other")

    def test_metadata_mismatch_falls_back_to_content_comparison(self) -> None:
        client = FakeGcsClient()
        client.objects[("sandbox-raw", "qualification/run-1/a.json")] = (b"same", {}, 7)
        self.assertFalse(self.store(client).put_once("a.json", b"same"))

    def test_bounded_read_and_missing_object(self) -> None:
        store = self.store()
        store.put_once("receipts/r.json", b"12345")
        self.assertEqual(store.get_bounded("receipts/r.json", 5), b"12345")
        with self.assertRaisesRegex(ValueError, "budget"):
            store.get_bounded("receipts/r.json", 4)
        with self.assertRaises(FileNotFoundError):
            store.get_bounded("receipts/absent.json", 10)
        with self.assertRaises(ValueError):
            store.get_bounded("receipts/r.json", 0)
        self.assertEqual(store.get("receipts/r.json"), b"12345")

    def test_keys_cannot_escape_prefix(self) -> None:
        store = self.store()
        for key in ("../x", "/abs"):
            with self.assertRaises(ValueError):
                store.put_once(key, b"x")

    def test_receipt_listing_is_relative_and_budgeted(self) -> None:
        store = self.store()
        store.put_once("receipts/a.json", b"a")
        store.put_once("receipts/b.json", b"b")
        store.put_once("batches/c.jsonl", b"c")
        self.assertEqual(store.list_receipt_keys(5), ("receipts/a.json", "receipts/b.json"))
        with self.assertRaisesRegex(ValueError, "budget"):
            store.list_receipt_keys(1)

    def test_invalid_bucket_rejected(self) -> None:
        from quadringent.gcs_backend import GcsObjectStore

        for bucket in ("", "-x", "gs://x", "a/b"):
            with self.assertRaises(ValueError):
                GcsObjectStore(bucket, client=FakeGcsClient())

    def test_publish_raw_batch_works_on_gcs(self) -> None:
        from quadringent.object_store import RawFirstCaptureCoordinator
        from quadringent.gcs_backend import GcsCheckpointStore
        from test_raw import event

        client = FakeGcsClient()
        store = self.store(client)
        checkpoint = GcsCheckpointStore("sandbox-state", "qualification/run-1", client=client)
        position = JournalPosition(receiver="TESTRCV001", sequence=10)
        result = RawFirstCaptureCoordinator(store, checkpoint).capture([event(10, "TESTRCV001")], high_watermark=position)
        self.assertTrue(result.checkpoint_committed)
        self.assertEqual(checkpoint.load(), position)
        names = [name for bucket, name in client.objects if bucket == "sandbox-raw"]
        self.assertTrue(names and all(name.startswith("qualification/run-1/") for name in names))


class GcsCheckpointStoreTests(unittest.TestCase):
    def store(self, client=None, key="qualification/run-1"):
        from quadringent.gcs_backend import GcsCheckpointStore

        return GcsCheckpointStore("sandbox-state", key, client=client or FakeGcsClient())

    def test_absent_checkpoint_loads_none_and_first_commit_requires_absence(self) -> None:
        client = FakeGcsClient()
        store = self.store(client)
        self.assertIsNone(store.load())
        store.commit(JournalPosition("R1", 5))
        self.assertEqual(store.load(), JournalPosition("R1", 5))
        self.assertEqual(client.writes[0][2], 0)

    def test_object_name_is_derived_from_stream_key(self) -> None:
        client = FakeGcsClient()
        self.store(client, key="Qualification/Run-1").commit(JournalPosition("R1", 1))
        (name,) = [name for _, name in client.objects]
        self.assertTrue(name.startswith("checkpoints/"))
        self.assertTrue(name.endswith(".json"))

    def test_commit_rejects_backwards_and_implicit_rotation(self) -> None:
        store = self.store()
        store.commit(JournalPosition("R1", 5))
        with self.assertRaisesRegex(ValueError, "backwards"):
            store.commit(JournalPosition("R1", 4))
        with self.assertRaisesRegex(ValueError, "rotation"):
            store.commit(JournalPosition("R2", 1))

    def test_transition_requires_exact_predecessor(self) -> None:
        store = self.store()
        store.commit(JournalPosition("R1", 5))
        with self.assertRaisesRegex(ValueError, "predecessor"):
            store.transition(JournalPosition("R1", 4), JournalPosition("R2", 1))
        store.transition(JournalPosition("R1", 5), JournalPosition("R2", 1))
        self.assertEqual(store.load(), JournalPosition("R2", 1))

    def test_compare_and_set_conflict_when_concurrent_writer_moved(self) -> None:
        client = FakeGcsClient()
        store = self.store(client)
        rival = self.store(client)
        store.commit(JournalPosition("R1", 5))
        client.before_write = lambda: rival.compare_and_set(JournalPosition("R1", 5), JournalPosition("R1", 9))
        with self.assertRaisesRegex(RuntimeError, "compare-and-set conflict"):
            store.compare_and_set(JournalPosition("R1", 5), JournalPosition("R1", 7))
        self.assertEqual(store.load(), JournalPosition("R1", 9))

    def test_compare_and_set_rejects_stale_expected_value(self) -> None:
        store = self.store()
        store.commit(JournalPosition("R1", 5))
        with self.assertRaisesRegex(RuntimeError, "compare-and-set conflict"):
            store.compare_and_set(None, JournalPosition("R1", 6))
        with self.assertRaisesRegex(RuntimeError, "compare-and-set conflict"):
            store.compare_and_set(JournalPosition("R1", 4), JournalPosition("R1", 6))

    def test_invalid_record_is_rejected(self) -> None:
        client = FakeGcsClient()
        store = self.store(client)
        store.commit(JournalPosition("R1", 5))
        (key,) = list(client.objects)
        client.objects[key] = (b'{"receiver":"R1"}', {}, 99)
        with self.assertRaisesRegex(ValueError, "invalid GCS checkpoint"):
            store.load()

    def test_empty_stream_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store(key=" ")


class GcsSourceGateTests(unittest.TestCase):
    def gate(self, client):
        from quadringent.gcs_backend import GcsSourceGate
        from quadringent.source_gate import SourceGatePolicy

        return GcsSourceGate("sandbox-state", "source-gate#test400.com#TESTLIB",
                             policy=SourceGatePolicy(), client=client)

    def test_attempt_budget_is_durable_across_instances(self) -> None:
        from quadringent.source_gate import ConnectFailureClass

        client = FakeGcsClient()
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        first = self.gate(client)
        first.before_connect(now=now)
        first.record_connect_failure(ConnectFailureClass.AUTHENTICATION, now=now, error_head="auth")
        state = self.gate(client).state()
        self.assertNotEqual(state["state"], "closed")
        self.assertNotIn("revision", state)

    def test_success_closes_the_gate(self) -> None:
        client = FakeGcsClient()
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        gate = self.gate(client)
        gate.before_connect(now=now)
        gate.record_connect_success(now=now + timedelta(seconds=1))
        self.assertEqual(self.gate(client).state()["state"], "closed")
        self.assertTrue(all(write[2] is not None for write in client.writes))


class BackendSelectionTests(unittest.TestCase):
    def test_default_backend_is_aws(self) -> None:
        from quadringent.storage_backend import backend_from_environment

        self.assertEqual(backend_from_environment({}), "aws")

    def test_unknown_backend_rejected(self) -> None:
        from quadringent.storage_backend import backend_from_environment

        with self.assertRaises(ValueError):
            backend_from_environment({"QUADRINGENT_STORAGE_BACKEND": "azure"})

    def test_gcs_backend_builds_gcs_stores(self) -> None:
        from quadringent.gcs_backend import GcsCheckpointStore, GcsObjectStore, GcsSourceGate
        from quadringent.storage_backend import StorageBackend

        client = FakeGcsClient()
        backend = StorageBackend.from_environment(
            {"QUADRINGENT_STORAGE_BACKEND": "gcs", "AS400_RAW_BUCKET": "sandbox-raw",
             "AS400_CHECKPOINT_BUCKET": "sandbox-state"},
            gcs_client=client,
        )
        self.assertIsInstance(backend.object_store("qualification/run-1"), GcsObjectStore)
        self.assertIsInstance(backend.checkpoint_store("qualification/run-1"), GcsCheckpointStore)
        self.assertIsInstance(backend.source_gate("source-gate#h#u"), GcsSourceGate)

    def test_gcs_backend_requires_checkpoint_bucket(self) -> None:
        from quadringent.storage_backend import StorageBackend

        with self.assertRaisesRegex(ValueError, "AS400_CHECKPOINT_BUCKET"):
            StorageBackend.from_environment(
                {"QUADRINGENT_STORAGE_BACKEND": "gcs", "AS400_RAW_BUCKET": "sandbox-raw"},
                gcs_client=FakeGcsClient(),
            )

    def test_aws_backend_keeps_existing_classes(self) -> None:
        from quadringent.checkpoint import DynamoDbCheckpointStore
        from quadringent.object_store import S3ObjectStore
        from quadringent.source_gate import DynamoDbSourceGate
        from quadringent.storage_backend import StorageBackend

        backend = StorageBackend.from_environment(
            {"AS400_RAW_BUCKET": "raw", "AS400_CHECKPOINT_TABLE": "checkpoints"},
            aws_client_factory=lambda service: object(),
        )
        self.assertIsInstance(backend.object_store("p"), S3ObjectStore)
        self.assertIsInstance(backend.checkpoint_store("k"), DynamoDbCheckpointStore)
        self.assertIsInstance(backend.source_gate("source-gate#h#u"), DynamoDbSourceGate)


if __name__ == "__main__":
    unittest.main()
