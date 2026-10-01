from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from quadringent.checkpoint import DynamoDbCheckpointStore, JsonCheckpointStore
from quadringent.contract import JournalPosition


class JsonCheckpointStoreTests(unittest.TestCase):
    def test_explicit_dynamo_cas_keeps_original_predecessor_after_another_writer(self):
        client = FakeDynamoClient()
        store = DynamoDbCheckpointStore('checkpoints', 'stream', client=client)
        store.commit(JournalPosition('R1',20))
        client.conditional_failure = True
        with self.assertRaises(RuntimeError):
            store.compare_and_set(JournalPosition('R1',9), JournalPosition('R1',30))
        self.assertEqual(client.put_calls[-1]['ExpressionAttributeValues'],
                         {':receiver':{'S':'R1'}, ':sequence':{'N':'9'}})
        self.assertEqual(store.load(), JournalPosition('R1',20))

    def test_local_cas_allows_only_one_writer_for_the_same_predecessor(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'checkpoint.json'
            previous=JournalPosition('R1',9)
            JsonCheckpointStore(path).commit(previous)
            barrier=Barrier(2)
            def attempt(sequence):
                barrier.wait(timeout=5)
                try:
                    JsonCheckpointStore(path).compare_and_set(previous,JournalPosition('R1',sequence))
                    return True
                except RuntimeError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(attempt,[20,30]))
            self.assertEqual(results.count(True),1)
            self.assertEqual(JsonCheckpointStore(path).load(),JournalPosition('R1',20 if results[0] else 30))

    def test_checkpoint_survives_a_new_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "checkpoint.json"
            position = JournalPosition("DEMOJRN3677", 101)
            JsonCheckpointStore(path).commit(position)

            self.assertEqual(JsonCheckpointStore(path).load(), position)

    def test_checkpoint_rejects_backwards_and_implicit_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = JsonCheckpointStore(Path(temporary_directory) / "checkpoint.json")
            store.commit(JournalPosition("DEMOJRN3677", 101))

            with self.assertRaises(ValueError):
                store.commit(JournalPosition("DEMOJRN3677", 100))
            with self.assertRaises(ValueError):
                store.commit(JournalPosition("DEMOJRN3676", 1))

    def test_json_checkpoint_requires_an_explicit_receiver_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = JsonCheckpointStore(Path(temporary_directory) / "checkpoint.json")
            previous = JournalPosition("DEMOJRN3677", 155104408)
            next_position = JournalPosition("DEMOJRN3678", 155104409)
            store.commit(previous)
            transition = getattr(store, "transition", None)
            self.assertIsNotNone(transition)
            assert transition is not None

            transition(previous, next_position)

            self.assertEqual(store.load(), next_position)

    def test_dynamo_checkpoint_survives_worker_recreation_and_uses_compare_and_set(self) -> None:
        client = FakeDynamoClient()
        position = JournalPosition("DEMOJRN3677", 101)

        DynamoDbCheckpointStore("example-corp-checkpoints", "sales-cntr", client=client).commit(position)
        restarted = DynamoDbCheckpointStore("example-corp-checkpoints", "sales-cntr", client=client)

        self.assertEqual(restarted.load(), position)
        self.assertEqual(client.put_calls[0]["ConditionExpression"], "attribute_not_exists(#stream)")
        self.assertEqual(client.put_calls[0]["ExpressionAttributeNames"], {"#stream": "stream_id"})

    def test_dynamo_checkpoint_rejects_implicit_rotation_and_compare_and_set_conflict(self) -> None:
        client = FakeDynamoClient()
        store = DynamoDbCheckpointStore("example-corp-checkpoints", "sales-cntr", client=client)
        store.commit(JournalPosition("DEMOJRN3677", 101))

        with self.assertRaises(ValueError):
            store.commit(JournalPosition("DEMOJRN3676", 1))

        client.conditional_failure = True
        with self.assertRaises(RuntimeError):
            store.commit(JournalPosition("DEMOJRN3677", 102))

    def test_dynamo_checkpoint_requires_the_exact_predecessor_for_transition(self) -> None:
        client = FakeDynamoClient()
        store = DynamoDbCheckpointStore("example-corp-checkpoints", "sales-cntr", client=client)
        previous = JournalPosition("DEMOJRN3677", 155104408)
        next_position = JournalPosition("DEMOJRN3678", 155104409)
        store.commit(previous)
        transition = getattr(store, "transition", None)
        self.assertIsNotNone(transition)
        assert transition is not None

        transition(previous, next_position)

        self.assertEqual(store.load(), next_position)


class FakeDynamoClient:
    def __init__(self) -> None:
        self.item: dict[str, dict[str, str]] | None = None
        self.put_calls: list[dict[str, object]] = []
        self.conditional_failure = False

    def get_item(self, *, TableName: str, Key: dict[str, dict[str, str]], ConsistentRead: bool) -> dict[str, object]:
        del TableName, Key, ConsistentRead
        return {} if self.item is None else {"Item": self.item}

    def put_item(self, **kwargs: object) -> None:
        self.put_calls.append(kwargs)
        if self.conditional_failure:
            error = RuntimeError("conditional write failed")
            error.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise error
        self.item = kwargs["Item"]


if __name__ == "__main__":
    unittest.main()
