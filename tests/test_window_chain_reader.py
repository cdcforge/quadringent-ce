from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from quadringent import proof_windows as windows
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator


class WindowChainReaderTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = FileObjectStore(root / 'objects')
        self.checkpoint = JsonCheckpointStore(root / 'checkpoint.json')
        self.checkpoint.commit(JournalPosition('R1', 9))
        self.now = datetime(2026, 9, 9, 12, tzinfo=UTC)
        windows.begin_window(self.store, self.checkpoint, window_id='first', stream_id='dev-sale',
                             started_at=self.now, duration_seconds=600)
        windows.prepare_window_chain(self.store, self.checkpoint, initial_window_id='first', window_count=2)

    def close(self, identity, seconds):
        previous = self.checkpoint.load()
        writer = RawFirstCaptureCoordinator(self.store, self.checkpoint)
        at = self.now + timedelta(seconds=seconds)
        writer.capture_receipted_window(start=JournalPosition('R1', previous.sequence+1),
                                       end=JournalPosition('R1', previous.sequence+10),
                                       previous=previous, scan_completed_at=at)
        windows.close_eligible_window(self.store, self.checkpoint, window_id=identity, now=at)

    def read(self):
        with patch.object(self.store, 'put_once', side_effect=AssertionError('reader must not write')):
            return windows.read_window_chain(self.store, now=self.now+timedelta(seconds=1300))

    def test_open_root_then_link_wait_then_complete(self):
        result = self.read()
        self.assertEqual(result['closed_window_ids'], [])
        self.assertEqual(result['pending_window_id'], 'first')
        self.close('first', 600)
        result = self.read()
        self.assertEqual(result['closed_window_ids'], ['first'])
        self.assertFalse(result['capture_complete'])
        second = windows.prepare_successor_window(self.store, self.checkpoint, predecessor_id='first',
                                                  now=self.now+timedelta(seconds=600))
        self.assertEqual(self.read()['pending_window_id'], second['window_id'])
        self.close(second['window_id'], 1200)
        result = self.read()
        self.assertTrue(result['capture_complete'])
        self.assertEqual(result['closed_window_ids'], ['first', second['window_id']])
        self.assertIsNone(result['pending_window_id'])

    def test_corrupt_contract_and_link_are_not_pending(self):
        self.close('first', 600)
        original = self.store.get_bounded
        for key, payload in (('window-chain.json', b'null'),
                             ('windows/first/successor.json', b'null')):
            with self.subTest(key=key), patch.object(self.store, 'get_bounded',
                    side_effect=lambda path, limit: payload if path == key else original(path, limit)):
                with self.assertRaises(ValueError): self.read()

    def test_missing_receipt_of_closed_window_is_not_an_open_window(self):
        self.close('first', 600)
        original = self.store.get_bounded
        def read(key, limit):
            if 'receipt' in key:
                raise FileNotFoundError(key)
            return original(key, limit)
        with patch.object(self.store, 'get_bounded', side_effect=read):
            with self.assertRaises(FileNotFoundError): self.read()

    def test_root_digest_change_is_rejected(self):
        original = self.store.get_bounded
        def read(key, limit):
            value = original(key, limit)
            if key == 'window-chain.json':
                contract = json.loads(value)
                contract['initial_intent_sha256'] = '0'*64
                return json.dumps(contract).encode()
            return value
        with patch.object(self.store, 'get_bounded', side_effect=read):
            with self.assertRaises(ValueError): self.read()

    def test_successor_intent_cannot_change_and_missing_intent_waits(self):
        self.close('first', 600)
        second = windows.prepare_successor_window(self.store, self.checkpoint, predecessor_id='first',
                                                  now=self.now+timedelta(seconds=600))
        key = 'windows/' + second['window_id'] + '/intent.json'
        original = self.store.get_bounded
        for missing in (False, True):
            def read(path, limit):
                value = original(path, limit)
                if path == key:
                    if missing: raise FileNotFoundError(path)
                    intent = json.loads(value)
                    intent['previous']['sequence'] += 1
                    return json.dumps(intent).encode()
                return value
            with self.subTest(missing=missing), patch.object(self.store, 'get_bounded', side_effect=read):
                if missing:
                    result = self.read()
                    self.assertEqual(result['closed_window_ids'], ['first'])
                    self.assertEqual(result['pending_window_id'], second['window_id'])
                    self.assertFalse(result['capture_complete'])
                else:
                    with self.assertRaises(ValueError): self.read()

    def test_reader_refuses_future_closure_and_over_budget_successor(self):
        self.close('first', 600)
        with self.assertRaises(ValueError):
            windows.read_window_chain(self.store, now=self.now+timedelta(seconds=599))
        second = windows.prepare_successor_window(self.store, self.checkpoint, predecessor_id='first',
                                                  now=self.now+timedelta(seconds=600))
        self.close(second['window_id'], 1200)
        self.store.put_once('windows/'+second['window_id']+'/successor.json', b'{}')
        with self.assertRaisesRegex(ValueError, 'exceeds durable window budget'): self.read()

    def test_permission_error_is_not_pending(self):
        with patch.object(self.store, 'get_bounded', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError): self.read()

    def test_boolean_budget_and_redirected_link_are_rejected(self):
        self.close('first', 600)
        windows.prepare_successor_window(self.store, self.checkpoint, predecessor_id='first',
                                         now=self.now+timedelta(seconds=600))
        original = self.store.get_bounded
        for target in ('window-chain.json', 'windows/first/successor.json'):
            def read(key, limit):
                payload = original(key, limit)
                if key == target:
                    document = json.loads(payload)
                    if key == 'window-chain.json': document['window_count'] = True
                    else: document['successor_intent']['window_id'] = 'other'
                    return json.dumps(document).encode()
                return payload
            with self.subTest(target=target), patch.object(self.store, 'get_bounded', side_effect=read):
                with self.assertRaises(ValueError): self.read()
