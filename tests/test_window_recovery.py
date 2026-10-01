from datetime import timedelta
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quadringent.contract import JournalPosition
from quadringent.proof_windows import begin_window
import test_proof_windows

NOW=test_proof_windows.NOW


class WindowRecoveryTests(unittest.TestCase):
    def test_legacy_receipt_cannot_gain_a_retroactive_scan_time(self):
        with TemporaryDirectory() as directory:
            _,_,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            args=dict(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            writer.capture_receipted_window(**args)
            with self.assertRaises(ValueError):writer.capture_receipted_window(**args,scan_completed_at=NOW)
            self.assertEqual(checkpoint.load(),args['end'])

    def test_replay_preserves_prepared_scan_time_after_checkpoint_failure(self):
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            args=dict(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            with patch.object(type(checkpoint),'compare_and_set',side_effect=OSError('crash')):
                with self.assertRaises(OSError):writer.capture_receipted_window(**args,scan_completed_at=NOW)
            recovered=writer.capture_receipted_window(**args,scan_completed_at=NOW+timedelta(hours=1))
            self.assertEqual(recovered['scan_completed_at'],NOW.isoformat())
            self.assertEqual(writer.capture_receipted_window(**args,scan_completed_at=NOW+timedelta(hours=2)),recovered)

    def test_lost_index_response_replays_and_collision_blocks_checkpoint(self):
        from quadringent.object_store import receipt_index_key
        for mode in ('lost','collision'):
            with self.subTest(mode=mode),TemporaryDirectory() as directory:
                _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
                args=dict(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
                if mode=='collision':store.put_once(receipt_index_key(args['end']),b'{}')
                original=store.put_once
                def lost_response(key,content):
                    result=original(key,content)
                    if mode=='lost' and key.startswith('scan-index/'):raise OSError('response lost')
                    return result
                with patch.object(store,'put_once',side_effect=lost_response):
                    with self.assertRaises(OSError if mode=='lost' else ValueError):writer.capture_receipted_window(**args)
                self.assertEqual(checkpoint.load(),args['previous'])
                if mode=='lost':
                    writer.capture_receipted_window(**args)
                    self.assertEqual(checkpoint.load(),args['end'])

    def test_receipt_swap_between_index_walk_and_seal_is_rejected(self):
        from quadringent.proof_windows import recover_and_seal_window
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            original=store.get_bounded
            reads=0
            def swap(key,limit):
                nonlocal reads
                content=original(key,limit)
                if key.startswith('receipts/'):
                    reads+=1
                    if reads>1:
                        receipt=json.loads(content)
                        receipt['raw']={'payload_key':'batch-'+('a'*32)+'.jsonl','manifest_key':'batch-'+('a'*32)+'.manifest.json','payload_sha256':'b'*64,'manifest_sha256':'c'*64}
                        return json.dumps(receipt).encode()
                return content
            with patch.object(store,'get_bounded',side_effect=swap):
                with self.assertRaises(ValueError):
                    recover_and_seal_window(store,checkpoint,window_id='w1',closed_at=NOW+timedelta(seconds=600))

    def test_committed_missing_index_is_not_recreated(self):
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            args=dict(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            writer.capture_receipted_window(**args)
            original=store.get
            def missing_index(key):
                if key.startswith('scan-index/'):raise FileNotFoundError(key)
                return original(key)
            with patch.object(store,'get',side_effect=missing_index),patch.object(store,'put_once',side_effect=AssertionError('must not rebuild committed evidence')):
                with self.assertRaises(FileNotFoundError):writer.capture_receipted_window(**args)
            self.assertEqual(checkpoint.load(),args['end'])

    def test_recovery_does_not_enumerate_historical_receipts(self):
        from quadringent.proof_windows import recover_and_seal_window
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            with patch.object(store,'list_receipt_keys',side_effect=AssertionError('global scan forbidden')):
                result=recover_and_seal_window(store,checkpoint,window_id='w1',closed_at=NOW+timedelta(seconds=600))
            self.assertEqual(len(result['receipts']),1)

    def test_index_failure_leaves_checkpoint_unchanged(self):
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            original=store.put_once
            def fail_index(key,content):
                if key.startswith('scan-index/'):
                    raise OSError('index unavailable')
                return original(key,content)
            with patch.object(store,'put_once',side_effect=fail_index):
                with self.assertRaises(OSError):
                    writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            self.assertEqual(checkpoint.load(),JournalPosition('R1',9))

    def test_corrupt_existing_closure_is_not_rebuilt(self):
        from quadringent.proof_windows import recover_and_seal_window
        with TemporaryDirectory() as directory:
            _,store,checkpoint,_=test_proof_windows.ProofWindowTests().setup_store(directory)
            store.put_once('windows/w1/closed.json',b'{}')
            with self.assertRaises(ValueError):
                recover_and_seal_window(store,checkpoint,window_id='w1',closed_at=NOW)

    def test_s3_access_denied_does_not_trigger_recovery_fallback(self):
        from quadringent.object_store import S3ObjectStore
        from quadringent.proof_windows import recover_and_seal_window
        class Denied(Exception):
            response={'Error':{'Code':'AccessDenied'}}
        class Client:
            def get_object(self,**kwargs):raise Denied('access denied')
        with self.assertRaises(Denied):
            recover_and_seal_window(S3ObjectStore('bucket','run',client=Client()),None,window_id='w1',closed_at=NOW)

    def test_recovery_seals_committed_chain_and_ignores_uncommitted_tail(self):
        from quadringent.proof_windows import recover_and_seal_window
        with TemporaryDirectory() as directory:
            _,store,checkpoint,writer=test_proof_windows.ProofWindowTests().setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            with patch.object(type(checkpoint),'compare_and_set',side_effect=OSError('crash')):
                with self.assertRaises(OSError):
                    writer.capture_receipted_window(start=JournalPosition('R1',21),end=JournalPosition('R1',30),previous=JournalPosition('R1',20))
            result=recover_and_seal_window(store,checkpoint,window_id='w1',closed_at=NOW+timedelta(seconds=600))
            self.assertEqual(result['end'],{'receiver':'R1','sequence':20})
            self.assertEqual(len(result['receipts']),1)
            writer.capture_receipted_window(start=JournalPosition('R1',21),end=JournalPosition('R1',30),previous=JournalPosition('R1',20))
            self.assertEqual(recover_and_seal_window(store,checkpoint,window_id='w1',closed_at=NOW+timedelta(days=2)),result)

    def test_missing_committed_receipt_prevents_closure(self):
        from quadringent.proof_windows import recover_and_seal_window
        with TemporaryDirectory() as directory:
            root,store,checkpoint,_=test_proof_windows.ProofWindowTests().setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            checkpoint.commit(JournalPosition('R1',20))
            with self.assertRaises(ValueError):
                recover_and_seal_window(store,checkpoint,window_id='w1',closed_at=NOW+timedelta(seconds=600))
            self.assertFalse((root/'objects'/'windows'/'w1'/'closed.json').exists())

    def test_receipt_listing_rejects_overflow(self):
        with TemporaryDirectory() as directory:
            _,store,_,_=test_proof_windows.ProofWindowTests().setup_store(directory)
            store.put_once('receipts/a.json',b'{}')
            store.put_once('receipts/b.json',b'{}')
            with self.assertRaises(ValueError):store.list_receipt_keys(1)

    def test_s3_listing_pagination_is_scoped_and_bounded(self):
        from quadringent.object_store import S3ObjectStore
        class Client:
            def list_objects_v2(self, **kwargs):
                if kwargs['Bucket']!='bucket' or kwargs['Prefix']!='run/receipts/':
                    raise AssertionError('listing escaped store')
                if 'ContinuationToken' not in kwargs:
                    return {'Contents':[{'Key':'run/receipts/a.json'}], 'IsTruncated':True,'NextContinuationToken':'next'}
                return {'Contents':[{'Key':'run/receipts/b.json'}], 'IsTruncated':False}
        store=S3ObjectStore('bucket','run',client=Client())
        self.assertEqual(store.list_receipt_keys(2),('receipts/a.json','receipts/b.json'))
        with self.assertRaises(ValueError):store.list_receipt_keys(1)
