from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from quadringent_control_plane.window_chain import BoundedChainStore, project_window_chain
from quadringent_control_plane.repository import bind_window_proofs, parse_source_spec, ProjectionRepository
from test_window_delivery_projection import evidence, fresh_running_document, NOW, LIVE_SOURCE, SITE


class ChainProjectionTests(unittest.TestCase):
    def test_chain_transport_failure_preserves_capture_and_counters(self):
        source = parse_source_spec('live:dev-sale:file:///capture.json', environment=SITE.environment)
        sources = bind_window_proofs([source], [f'dev-sale=s3://{SITE.raw_bucket}/{SITE.stream_prefix}/runs/r1/window-chain.json'])
        with patch('quadringent_control_plane.repository._read_document', return_value=fresh_running_document()), patch('quadringent_control_plane.repository._s3_client', side_effect=RuntimeError('private credential detail')):
            result = ProjectionRepository(sources).refresh().pipelines[0]
            baseline = ProjectionRepository([source]).refresh().pipelines[0]
        self.assertEqual(result.status, baseline.status)
        self.assertEqual(result.counters, baseline.counters)
        self.assertEqual(result.window_delivery['state'], 'unavailable')
        self.assertNotIn('private', json.dumps(result.to_dict()))

    def test_real_local_empty_chain_is_not_delivery_proof(self):
        from datetime import timedelta
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
        from quadringent.checkpoint import JsonCheckpointStore
        from quadringent.contract import JournalPosition
        from quadringent.proof_windows import begin_window, prepare_window_chain, close_eligible_window
        from quadringent.window_destination import verify_closed_window_destination
        from test_window_delivery_projection import STREAM_ID
        with TemporaryDirectory() as directory:
            root = Path(directory)/'r1'
            store = FileObjectStore(root)
            checkpoint = JsonCheckpointStore(Path(directory)/'checkpoint.json')
            previous = JournalPosition('R1', 9)
            checkpoint.commit(previous)
            begin_window(store, checkpoint, window_id='w1', started_at=NOW-timedelta(seconds=620),
                         duration_seconds=600, stream_id=STREAM_ID)
            prepare_window_chain(store, checkpoint, initial_window_id='w1', window_count=1)
            RawFirstCaptureCoordinator(store, checkpoint).capture_receipted_window(
                start=JournalPosition('R1',10), end=JournalPosition('R1',20), previous=previous,
                scan_completed_at=NOW-timedelta(seconds=20))
            close_eligible_window(store, checkpoint, window_id='w1', now=NOW-timedelta(seconds=20))
            proof = verify_closed_window_destination(None, store, run_id='r1', window_id='w1', observed_at=NOW, site=SITE)
            store.put_once('windows/w1/destination.json', json.dumps(proof).encode())
            source = parse_source_spec('live:dev-sale:file:///capture.json', environment=SITE.environment)
            sources = bind_window_proofs([source], ['dev-sale='+(root/'window-chain.json').as_uri()])
            with patch('quadringent_control_plane.repository._read_document', return_value=fresh_running_document()):
                pipeline = ProjectionRepository(sources).refresh().pipelines[0]
            self.assertEqual(pipeline.window_delivery['state'], 'not_tested')
            self.assertEqual(pipeline.window_delivery['quality']['evidence_kind'], 'simulation')

    def setUp(self):
        self.proofs = {}
        for identity in ('w1', 'w2'):
            proof = evidence()
            proof['window_id'] = identity
            proof['window']['window_id'] = identity
            proof['window']['intent']['window_id'] = identity
            self.proofs[identity] = proof
        self.closed = {key: deepcopy(value['window']) for key, value in self.proofs.items()}
        self.chain = {'window_count': 2, 'closed_window_ids': ['w1', 'w2'], 'capture_complete': True}
        self.calls = []

    def project(self, backend='s3'):
        def read(key, limit):
            self.calls.append(key)
            identity = key.split('/')[1]
            if identity not in self.proofs: raise FileNotFoundError(key)
            return json.dumps(self.proofs[identity]).encode()
        with patch('quadringent_control_plane.window_chain.read_window_chain', return_value=self.chain), patch('quadringent_control_plane.window_chain.read_closed_window', side_effect=lambda store, window_id: self.closed[window_id]):
            return project_window_chain(BoundedChainStore(read), run_id='r1',
                flux=fresh_running_document()['flux'], source=LIVE_SOURCE, now=NOW, storage_backend=backend)

    def test_complete_prefix_selects_last_window_without_upgrading_local(self):
        result = self.project('local')
        self.assertEqual(result['state'], 'matched')
        self.assertEqual(result['window_id'], 'w2')
        self.assertEqual(result['chain']['matched_windows'], 2)
        self.assertEqual(result['quality']['evidence_kind'], 'simulation')
        self.assertEqual(result['scope'], 'closed_window_only')

    def test_missing_first_proof_never_reads_second(self):
        del self.proofs['w1']
        result = self.project()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(self.calls, ['windows/w1/destination.json'])

    def test_changed_closure_or_foreign_run_is_not_accepted(self):
        self.proofs['w1']['window']['end']['sequence'] += 1
        self.assertEqual(self.project()['state'], 'invalid')
        self.proofs['w1']['window'] = deepcopy(self.closed['w1'])
        self.proofs['w1']['archive_run_id'] = 'other'
        self.assertEqual(self.project()['state'], 'invalid')

    def test_open_chain_cannot_claim_matched(self):
        self.chain.update(closed_window_ids=['w1'], capture_complete=False)
        result = self.project()
        self.assertEqual(result['state'], 'unavailable')
        self.assertEqual(result['chain']['matched_windows'], 1)

    def test_metadata_cache_is_bounded_and_reuses_bytes(self):
        calls = []
        store = BoundedChainStore(lambda key, limit: calls.append(key) or b'abc')
        self.assertEqual(store.get_bounded('one', 3), b'abc')
        self.assertEqual(store.get_bounded('one', 3), b'abc')
        self.assertEqual(calls, ['one'])
        with self.assertRaises(ValueError): store.get_bounded('one', 2)
        store.cache = {str(index): b'x' for index in range(1024)}
        with self.assertRaises(ValueError): store.get_bounded('next', 3)

    def test_binding_only_accepts_dev_run_contract(self):
        source = parse_source_spec('live:dev-sale:file:///capture.json', environment=SITE.environment)
        root = f's3://{SITE.raw_bucket}/{SITE.stream_prefix}/runs/r1/'
        self.assertTrue(bind_window_proofs([source], ['dev-sale='+root+'window-chain.json']))
        for suffix in ('../window-chain.json', 'other.json', 'window-chainXjson'):
            with self.assertRaises(ValueError): bind_window_proofs([source], ['dev-sale='+root+suffix])
