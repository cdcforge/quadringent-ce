import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from test_continuous import raw_bytes


class ScanReceiptTests(unittest.TestCase):
    def test_writer_race_cannot_replace_receipt_predecessor(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            checkpoint=JsonCheckpointStore(root/'checkpoint.json')
            previous=JournalPosition('R1',9)
            checkpoint.commit(previous)
            base=FileObjectStore(root/'objects')
            class RacingStore:
                def get(self,key): return base.get(key)
                def put_once(self,key,content):
                    result=base.put_once(key,content)
                    if key.startswith('receipts/'):
                        checkpoint.commit(JournalPosition('R1',20))
                    return result
            with self.assertRaises((ValueError,RuntimeError)):
                RawFirstCaptureCoordinator(RacingStore(),checkpoint).capture_receipted_window(
                    start=JournalPosition('R1',10),end=JournalPosition('R1',30),previous=previous)
            self.assertEqual(checkpoint.load(),JournalPosition('R1',20))

    def test_advanced_checkpoint_cannot_manufacture_a_missing_receipt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root/'checkpoint.json')
            end = JournalPosition('R1',20)
            checkpoint.commit(end)
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root/'objects'),checkpoint)
            with self.assertRaises(FileNotFoundError):
                coordinator.capture_receipted_window(start=JournalPosition('R1',10),end=end,previous=None)
            self.assertEqual(list((root/'objects').iterdir()),[])

    def test_conflicting_receipt_cannot_advance_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root/'checkpoint.json')
            class UnavailableCheckpoint:
                def load(self): return None
                def commit(self, position): raise OSError('unavailable')
                def compare_and_set(self, previous, position): raise OSError('unavailable')
            store = FileObjectStore(root/'objects')
            args = dict(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=None)
            with self.assertRaises(OSError):
                RawFirstCaptureCoordinator(store,UnavailableCheckpoint()).capture_receipted_window(**args)
            with self.assertRaises(ValueError):
                RawFirstCaptureCoordinator(store,checkpoint).capture_receipted_window(**{**args,'end':JournalPosition('R1',21)})
            self.assertIsNone(checkpoint.load())

    def test_raw_outside_scan_or_unpaired_raw_is_rejected_before_io(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            checkpoint=JsonCheckpointStore(root/'checkpoint.json')
            coordinator=RawFirstCaptureCoordinator(FileObjectStore(root/'objects'),checkpoint)
            end=JournalPosition('R1',20)
            manifest,payload=raw_bytes(root/'input',receiver='R1',sequence=9,high_watermark=end)
            for raw in ({'manifest_content':manifest,'payload':payload},{'manifest_content':manifest}):
                with self.assertRaises(ValueError):
                    coordinator.capture_receipted_window(start=JournalPosition('R1',10),end=end,previous=None,**raw)
            self.assertIsNone(checkpoint.load())
            self.assertEqual(list((root/'objects').iterdir()),[])

    def test_receipt_failure_never_advances_checkpoint_and_replay_recovers(self):
        for mode in ('before_receipt', 'lost_response', 'checkpoint'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                start, end = JournalPosition('R1', 10), JournalPosition('R1', 20)
                manifest, payload = raw_bytes(root/'input', receiver='R1', sequence=12, high_watermark=end)
                base = FileObjectStore(root/'objects')
                checkpoint = JsonCheckpointStore(root/'checkpoint.json')
                class Store:
                    def put_once(self, key, content):
                        if key.startswith('receipts/') and mode == 'before_receipt':
                            raise OSError('receipt unavailable')
                        result = base.put_once(key, content)
                        if key.startswith('receipts/') and mode == 'lost_response':
                            raise OSError('response lost')
                        return result
                    def get(self, key): return base.get(key)
                class Checkpoint:
                    def load(self): return checkpoint.load()
                    def commit(self, position):
                        raise OSError('checkpoint unavailable')
                    def compare_and_set(self, previous, position):
                        raise OSError('checkpoint unavailable')
                coordinator = RawFirstCaptureCoordinator(Store(), Checkpoint() if mode == 'checkpoint' else checkpoint)
                args = dict(start=start, end=end, previous=None, manifest_content=manifest, payload=payload)
                with self.assertRaises(OSError):
                    coordinator.capture_receipted_window(**args)
                self.assertIsNone(checkpoint.load())
                restarted = RawFirstCaptureCoordinator(base, checkpoint)
                receipt = restarted.capture_receipted_window(**args)
                again = restarted.capture_receipted_window(**args)
                self.assertEqual(receipt, again)
                self.assertEqual(checkpoint.load(), end)
                receipts = list((root/'objects'/'receipts').glob('*.json'))
                self.assertEqual(len(receipts), 1)
                document = json.loads(receipts[0].read_text())
                self.assertEqual(document['event_count'], 1)
                self.assertEqual(document['start'], {'receiver':'R1','sequence':10})
                self.assertEqual(document['end'], {'receiver':'R1','sequence':20})
                self.assertIsNotNone(document['raw'])

    def test_empty_scan_and_explicit_rotation_have_durable_receipts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root/'checkpoint.json')
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root/'objects'), checkpoint)
            end = JournalPosition('R1', 20)
            coordinator.capture_receipted_window(start=JournalPosition('R1',10), end=end, previous=None)
            coordinator.capture_receipted_window(start=JournalPosition('R2',1), end=JournalPosition('R2',5), previous=end)
            records = [json.loads(p.read_text()) for p in (root/'objects'/'receipts').glob('*.json')]
            self.assertEqual(len(records), 2)
            self.assertTrue(all(r['event_count']==0 and r['raw'] is None for r in records))
            self.assertEqual(checkpoint.load(), JournalPosition('R2',5))

    def test_invalid_predecessor_or_range_does_not_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = JsonCheckpointStore(root/'checkpoint.json')
            checkpoint.commit(JournalPosition('R1',9))
            coordinator = RawFirstCaptureCoordinator(FileObjectStore(root/'objects'), checkpoint)
            for start, end, previous in [
                (JournalPosition('R1',11), JournalPosition('R1',20), JournalPosition('R1',9)),
                (JournalPosition('R1',10), JournalPosition('R2',20), JournalPosition('R1',9)),
                (JournalPosition('R1',10), JournalPosition('R1',20), None),
            ]:
                with self.assertRaises(ValueError):
                    coordinator.capture_receipted_window(start=start,end=end,previous=previous)
            self.assertEqual(list((root/'objects').iterdir()), [])
