import site_fixture
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quadringent.contract import JournalPosition
from quadringent.proof_windows import begin_window, seal_window
from quadringent.raw import RawBatchWriter
from test_continuous import event
import test_proof_windows

SITE = site_fixture.build_test_site()

NOW = test_proof_windows.NOW


class ClosedWindowRawTests(unittest.TestCase):
    def fixture(self, directory, table='SALE', empty=False, timed=False, stream_id=None):
        root, store, checkpoint, writer = test_proof_windows.ProofWindowTests().setup_store(directory)
        begin_window(store, checkpoint, window_id='w1', started_at=NOW, duration_seconds=600, stream_id=stream_id)
        end = JournalPosition('R1',20)
        source_event = replace(event('R1',12), table=table)
        kwargs = {}
        if not empty:
            manifest = RawBatchWriter(root/'input').write_batch([source_event], high_watermark=end)
            kwargs = dict(manifest_content=(root/'input'/f'batch-{manifest.batch_id}.manifest.json').read_bytes(),
                          payload=(root/'input'/f'batch-{manifest.batch_id}.jsonl').read_bytes())
        writer.capture_receipted_window(start=JournalPosition('R1',10), end=end, previous=JournalPosition('R1',9), scan_completed_at=NOW+timedelta(seconds=600) if timed else None, **kwargs)
        receipt_key = next((root/'objects'/'receipts').glob('*.json')).relative_to(root/'objects').as_posix()
        seal_window(store, checkpoint, window_id='w1', receipt_keys=[receipt_key], closed_at=NOW+timedelta(seconds=600), sealed_at=NOW+timedelta(seconds=600) if timed else None)
        return store, writer, source_event, receipt_key

    def test_exact_population_survives_capture_advancing(self):
        from quadringent.closed_window_raw import collect_closed_window_raw
        with TemporaryDirectory() as directory:
            store, writer, source_event, _ = self.fixture(directory)
            writer.capture_receipted_window(start=JournalPosition('R1',21),end=JournalPosition('R1',30),previous=JournalPosition('R1',20))
            store.put_once('batch-unrelated.jsonl',b'not part of window')
            result = collect_closed_window_raw(store, window_id='w1', site=SITE)
            self.assertEqual(result.event_count,1)
            self.assertEqual(result.event_ids_sha256,hashlib.sha256(source_event.event_id.encode()).hexdigest())
            self.assertEqual(len(result.object_keys),1)
            self.assertEqual(result.window['end'],{'receiver':'R1','sequence':20})
            self.assertEqual(result.window['delivery_latency_state'],'unobserved')

    def test_corrupt_objects_and_foreign_table_fail_closed(self):
        from quadringent.closed_window_raw import collect_closed_window_raw
        for variant in ('payload','manifest','foreign','receipt'):
            with self.subTest(variant=variant), TemporaryDirectory() as directory:
                store, _, _, receipt_key = self.fixture(directory,table='CNTR' if variant=='foreign' else 'SALE')
                receipt=json.loads(store.get(receipt_key))
                target = receipt_key if variant=='receipt' else receipt['raw'].get(variant+'_key')
                original=store.get_bounded
                with patch.object(store,'get_bounded',side_effect=lambda k,limit:b'{}' if k==target else original(k,limit)):
                    with self.assertRaises(ValueError):
                        collect_closed_window_raw(store,window_id='w1', site=SITE)

    def test_budget_exhaustion_is_not_partial_success(self):
        from quadringent.closed_window_raw import collect_closed_window_raw
        with TemporaryDirectory() as directory:
            store, _, _, _ = self.fixture(directory)
            with self.assertRaises(ValueError):
                collect_closed_window_raw(store,window_id='w1',max_raw_bytes=1, site=SITE)

    def test_cumulative_budget_includes_manifest_and_payload(self):
        from quadringent.closed_window_raw import collect_closed_window_raw
        with TemporaryDirectory() as directory:
            store, _, _, key = self.fixture(directory)
            raw=json.loads(store.get(key))['raw']
            total=len(store.get(raw['manifest_key']))+len(store.get(raw['payload_key']))
            self.assertEqual(collect_closed_window_raw(store,window_id='w1',max_raw_bytes=total, site=SITE).event_count,1)
            with self.assertRaises(ValueError):
                collect_closed_window_raw(store,window_id='w1',max_raw_bytes=total-1, site=SITE)

    def test_receipt_swap_after_metadata_validation_is_rejected(self):
        from quadringent.closed_window_raw import collect_closed_window_raw
        with TemporaryDirectory() as directory:
            store, _, _, receipt_key = self.fixture(directory)
            original=store.get_bounded
            reads=0
            def changed_after_validation(key,limit):
                nonlocal reads
                if key==receipt_key:
                    reads+=1
                    if reads>1:
                        return b'{}'
                return original(key,limit)
            with patch.object(store,'get_bounded',side_effect=changed_after_validation):
                with self.assertRaisesRegex(ValueError,'receipt changed during raw acquisition'):
                    collect_closed_window_raw(store,window_id='w1', site=SITE)

    def test_empty_window_has_no_invented_delivery_latency(self):
        from quadringent.closed_window_raw import collect_closed_window_raw
        with TemporaryDirectory() as directory:
            store, _, _, _ = self.fixture(directory,empty=True)
            result=collect_closed_window_raw(store,window_id='w1', site=SITE)
            self.assertEqual(result.event_count,0)
            self.assertEqual(result.object_keys,())
            self.assertEqual(result.window['delivery_latency_state'],'unobserved')
