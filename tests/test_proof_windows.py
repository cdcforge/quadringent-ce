from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator

NOW = datetime(2026,9,9,10,tzinfo=UTC)


class ProofWindowTests(unittest.TestCase):
    def test_prepare_resumes_original_intent_without_redating(self):
        from quadringent.proof_windows import prepare_window
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            args=dict(window_id='w1',stream_id='as400/sales/sale',duration_seconds=600)
            first=prepare_window(store,checkpoint,now=NOW,**args)
            resumed=prepare_window(store,checkpoint,now=NOW+timedelta(seconds=30),**args)
            self.assertEqual(first,resumed)
            with self.assertRaises(ValueError):
                prepare_window(store,checkpoint,now=NOW+timedelta(seconds=30),**{**args,'stream_id':'other'})
            with self.assertRaises(ValueError):
                prepare_window(store,checkpoint,now=NOW+timedelta(seconds=700),**args)

    def test_prepare_requires_checkpoint_even_if_window_was_closed(self):
        from quadringent.proof_windows import prepare_window
        from unittest.mock import Mock
        store=Mock()
        checkpoint=Mock()
        checkpoint.load.return_value=None
        with self.assertRaises(ValueError):
            prepare_window(store,checkpoint,window_id='w1',stream_id='dev-sale',now=NOW,duration_seconds=600)
        store.get_bounded.assert_not_called()
        store.put_once.assert_not_called()

    def test_stream_binding_is_immutable_and_survives_closure(self):
        from quadringent.proof_windows import begin_window, seal_window, read_closed_window
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            args=dict(window_id='w1',started_at=NOW,duration_seconds=600)
            intent=begin_window(store,checkpoint,stream_id='dev-sale',**args)
            self.assertEqual(intent['format_version'],'quadringent-window-intent-v2')
            with self.assertRaises(ValueError):
                begin_window(store,checkpoint,stream_id='other',**args)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            key=next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
            seal_window(store,checkpoint,receipt_keys=[key],closed_at=NOW+timedelta(seconds=600),window_id='w1')
            self.assertEqual(read_closed_window(store,window_id='w1')['intent']['stream_id'],'dev-sale')

    def test_invalid_stream_is_rejected_before_writing(self):
        from quadringent.proof_windows import begin_window
        for stream in ('', '../other', 'a/../b', 'a//b', True, 'a'*513):
            with self.subTest(stream=stream),TemporaryDirectory() as directory:
                root,store,checkpoint,writer=self.setup_store(directory)
                with self.assertRaises(ValueError):
                    begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600,stream_id=stream)
                self.assertFalse((root/'objects'/'windows').exists())

    def test_boolean_predecessor_is_not_an_integer_position(self):
        from quadringent.proof_windows import begin_window, seal_window
        from unittest.mock import patch
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            checkpoint=JsonCheckpointStore(root/'other-checkpoint.json')
            checkpoint.commit(JournalPosition('R1',1))
            writer=RawFirstCaptureCoordinator(store,checkpoint)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',2),end=JournalPosition('R1',3),previous=JournalPosition('R1',1))
            key=next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
            receipt=json.loads(store.get(key))
            receipt['previous']['sequence']=True
            original=store.get_bounded
            with patch.object(store,'get_bounded',side_effect=lambda k,limit:json.dumps(receipt).encode() if k==key else original(k,limit)):
                with self.assertRaises(ValueError):
                    seal_window(store,checkpoint,window_id='w1',receipt_keys=[key],closed_at=NOW+timedelta(seconds=600))

    def test_lost_closure_response_is_recovered_without_reselecting_window(self):
        from quadringent.proof_windows import begin_window,seal_window,read_closed_window
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            key=next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
            class LostResponse:
                def get(self,key): return store.get(key)
                def get_bounded(self,key,limit): return store.get_bounded(key,limit)
                def put_once(self,key,content):
                    store.put_once(key,content)
                    raise OSError('response lost')
            with self.assertRaises(OSError):
                seal_window(LostResponse(),checkpoint,window_id='w1',receipt_keys=[key],closed_at=NOW+timedelta(seconds=600))
            recovered=read_closed_window(store,window_id='w1')
            self.assertEqual(recovered['closed_at'],(NOW+timedelta(seconds=600)).isoformat())
            with self.assertRaises(ValueError):
                seal_window(store,checkpoint,window_id='w1',receipt_keys=[key],closed_at=NOW+timedelta(seconds=601))

    def test_intent_cannot_be_redefined_and_nonempty_receipt_counts_are_preserved(self):
        from quadringent.proof_windows import begin_window,seal_window,read_closed_window
        from test_continuous import raw_bytes
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            with self.assertRaises(ValueError):
                begin_window(store,checkpoint,window_id='w1',started_at=NOW+timedelta(seconds=1),duration_seconds=600)
            end=JournalPosition('R1',20)
            manifest,payload=raw_bytes(root/'input',receiver='R1',sequence=12,high_watermark=end)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=end,previous=JournalPosition('R1',9),manifest_content=manifest,payload=payload)
            key=next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
            seal_window(store,checkpoint,window_id='w1',receipt_keys=[key],closed_at=NOW+timedelta(seconds=600))
            recovered=read_closed_window(store,window_id='w1')
            self.assertEqual(recovered['event_count'],1)
            self.assertEqual(recovered['delivery_latency_state'],'unobserved')

    def setup_store(self, directory):
        root=Path(directory)
        checkpoint=JsonCheckpointStore(root/'checkpoint.json')
        checkpoint.commit(JournalPosition('R1',9))
        store=FileObjectStore(root/'objects')
        return root,store,checkpoint,RawFirstCaptureCoordinator(store,checkpoint)

    def test_closed_window_is_recoverable_while_capture_advances(self):
        from quadringent.proof_windows import begin_window, seal_window, read_closed_window
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            key=next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
            sealed=seal_window(store,checkpoint,window_id='w1',receipt_keys=[key],closed_at=NOW+timedelta(seconds=600))
            # A subsequent scan must not modify the immutable preceding proof.
            writer.capture_receipted_window(start=JournalPosition('R1',21),end=JournalPosition('R1',30),previous=JournalPosition('R1',20))
            recovered=read_closed_window(FileObjectStore(root/'objects'),window_id='w1')
            self.assertEqual(recovered,sealed)
            self.assertEqual(recovered['end'],{'receiver':'R1','sequence':20})
            self.assertEqual(recovered['event_count'],0)
            self.assertEqual(recovered['delivery_latency_state'],'unobserved')
            self.assertEqual(checkpoint.load(),JournalPosition('R1',30))

    def test_no_short_window_gap_duplicate_or_uncommitted_end(self):
        from quadringent.proof_windows import begin_window, seal_window
        for failure in ('early','late','gap','duplicate','uncommitted'):
            with self.subTest(failure=failure),TemporaryDirectory() as directory:
                root,store,checkpoint,writer=self.setup_store(directory)
                begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
                writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
                writer.capture_receipted_window(start=JournalPosition('R1',21),end=JournalPosition('R1',30),previous=JournalPosition('R1',20))
                keys=sorted((p.relative_to(root/'objects').as_posix() for p in (root/'objects'/'receipts').glob('*.json')),
                            key=lambda k:json.loads(store.get(k))['start']['sequence'])
                if failure=='gap': keys=keys[1:]
                if failure=='duplicate': keys=[keys[0],keys[0],keys[1]]
                if failure=='uncommitted': checkpoint.commit(JournalPosition('R1',31))
                with self.assertRaises(ValueError):
                    seal_window(store,checkpoint,window_id='w1',receipt_keys=keys,
                                closed_at=NOW+timedelta(seconds=599 if failure=='early' else 661 if failure=='late' else 600))
                self.assertFalse((root/'objects'/'windows'/'w1'/'closed.json').exists())

    def test_changed_receipt_cannot_be_used_after_closure(self):
        from quadringent.proof_windows import begin_window,seal_window,read_closed_window
        from unittest.mock import patch
        with TemporaryDirectory() as directory:
            root,store,checkpoint,writer=self.setup_store(directory)
            begin_window(store,checkpoint,window_id='w1',started_at=NOW,duration_seconds=600)
            writer.capture_receipted_window(start=JournalPosition('R1',10),end=JournalPosition('R1',20),previous=JournalPosition('R1',9))
            key=next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
            seal_window(store,checkpoint,window_id='w1',receipt_keys=[key],closed_at=NOW+timedelta(seconds=600))
            original=store.get_bounded
            with patch.object(store,'get_bounded',side_effect=lambda k,limit:b'{}' if k==key else original(k,limit)):
                with self.assertRaises(ValueError):read_closed_window(store,window_id='w1')
