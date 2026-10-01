import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.continuous import ContinuousCaptureService, CapturedWindow, ReceiverSnapshot
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from test_continuous import FakeCatalog, FakeRunner, raw_bytes


class ContinuousReceiptTests(unittest.TestCase):
    def test_pending_closure_blocks_next_source_scan_and_recovers_same_boundary(self):
        from quadringent.proof_windows import begin_window, read_closed_window
        with TemporaryDirectory() as directory:
            root=Path(directory)
            store=FileObjectStore(root/'objects')
            checkpoint=JsonCheckpointStore(root/'checkpoint.json')
            checkpoint.commit(JournalPosition('R1',9))
            start=datetime(2026,9,9,12,tzinfo=UTC)
            begin_window(store,checkpoint,window_id='w1',started_at=start,duration_seconds=600)
            clock=[start]
            class Runner:
                calls=0
                def capture(self,plan):
                    self.calls+=1
                    if self.calls>1:
                        if not (root/'objects'/'windows'/'w1'/'closed.json').exists():
                            raise AssertionError('source resumed before closure')
                    clock[0]=start+timedelta(seconds=600+self.calls-1)
                    return CapturedWindow(plan.end)
            runner=Runner()
            def service():
                return ContinuousCaptureService(FakeCatalog([ReceiverSnapshot('QGPL','R1',1,100)]),runner,RawFirstCaptureCoordinator(store,checkpoint),checkpoint,max_entries=10,receipted_scans=True,utc_now=lambda:clock[0],proof_window_id='w1')
            original=store.put_once
            def fail_closure(key,content):
                if key.endswith('/closed.json'):raise OSError('closure unavailable')
                return original(key,content)
            with patch.object(store,'put_once',side_effect=fail_closure):
                with self.assertRaises(OSError):service().run_once()
                self.assertEqual(checkpoint.load(),JournalPosition('R1',19))
                with self.assertRaises(OSError):service().run_once()
                self.assertEqual(runner.calls,1)
            recovered=service()
            recovered.run_once()
            closed=read_closed_window(store,window_id='w1')
            self.assertEqual(closed['end'],{'receiver':'R1','sequence':19})
            self.assertEqual(closed['closed_at'],(start+timedelta(seconds=600)).isoformat())
            self.assertEqual(checkpoint.load(),JournalPosition('R1',29))
            self.assertEqual(recovered.closed_proof_window,closed)

    def test_receipt_mode_rejects_string_configuration(self):
        with self.assertRaises(ValueError):
            ContinuousCaptureService(None,None,None,None,max_entries=10,receipted_scans='false')

    def test_receipt_timing_separates_publication_from_checkpoint(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            store=FileObjectStore(root/'objects')
            checkpoint=JsonCheckpointStore(root/'checkpoint.json')
            checkpoint.commit(JournalPosition('R1',9))
            with patch('quadringent.object_store.time.perf_counter',side_effect=[1.0,1.25,2.0,2.5]):
                result=RawFirstCaptureCoordinator(store,checkpoint).capture_receipted_window_result(
                    start=JournalPosition('R1',10),end=JournalPosition('R1',19),previous=JournalPosition('R1',9))
            self.assertEqual(result.publish_ms,250)
            self.assertEqual(result.checkpoint_ms,500)
            self.assertEqual(result.payload_bytes,0)
            self.assertEqual(result.receipt['event_count'],0)

    def test_worker_writes_receipt_for_empty_raw_and_rotated_scans(self):
        for empty, rotated in ((True,False),(False,False),(True,True),(False,True)):
            with self.subTest(empty=empty,rotated=rotated), TemporaryDirectory() as directory:
                root=Path(directory)
                store=FileObjectStore(root/'objects')
                checkpoint=JsonCheckpointStore(root/'checkpoint.json')
                checkpoint.commit(JournalPosition('R1',9))
                receiver='R2' if rotated else 'R1'
                catalog=FakeCatalog([ReceiverSnapshot('QGPL','R1',1,9),ReceiverSnapshot('QGPL','R2',10,20)] if rotated else [ReceiverSnapshot('QGPL','R1',1,20)])
                end=JournalPosition(receiver,19)
                manifest,payload=(None,None) if empty else raw_bytes(root/'input',receiver=receiver,sequence=12,high_watermark=end)
                stamp=datetime(2026,9,9,12,tzinfo=UTC)
                service=ContinuousCaptureService(catalog,FakeRunner(CapturedWindow(end,manifest,payload)),RawFirstCaptureCoordinator(store,checkpoint),checkpoint,max_entries=10,receipted_scans=True,utc_now=lambda:stamp)
                result=service.run_once()
                self.assertEqual(result.status,'empty_scan' if empty else 'published')
                files=list((root/'objects'/'receipts').glob('*.json'))
                self.assertEqual(len(files),1)
                receipt=json.loads(files[0].read_bytes())
                self.assertEqual(receipt['previous'],{'receiver':'R1','sequence':9})
                self.assertEqual(receipt['end'],{'receiver':receiver,'sequence':19})
                self.assertEqual(receipt['event_count'],0 if empty else 1)
                self.assertEqual(receipt['scan_completed_at'],stamp.isoformat())
                self.assertEqual(checkpoint.load(),end)

    def test_worker_does_not_advance_when_receipt_publication_fails(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            store=FileObjectStore(root/'objects')
            checkpoint=JsonCheckpointStore(root/'checkpoint.json')
            checkpoint.commit(JournalPosition('R1',9))
            service=ContinuousCaptureService(FakeCatalog([ReceiverSnapshot('QGPL','R1',1,20)]),FakeRunner(CapturedWindow(JournalPosition('R1',19),None,None)),RawFirstCaptureCoordinator(store,checkpoint),checkpoint,max_entries=10,receipted_scans=True)
            with patch.object(store,'put_once',side_effect=OSError('storage unavailable')):
                with self.assertRaises(OSError):service.run_once()
            self.assertEqual(checkpoint.load(),JournalPosition('R1',9))
            self.assertEqual(service.metrics.errors,1)
            self.assertEqual(service.metrics.empty_scans,0)
