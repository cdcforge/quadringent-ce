from datetime import UTC, datetime, timedelta
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest

from quadringent import proof_windows as windows
from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator

NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


class WindowSuccessorTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = FileObjectStore(root / 'objects')
        self.checkpoint = JsonCheckpointStore(root / 'checkpoint.json')
        self.checkpoint.commit(JournalPosition('R1', 9))
        self.writer = RawFirstCaptureCoordinator(self.store, self.checkpoint)
        windows.begin_window(self.store, self.checkpoint, window_id='first', stream_id='dev-sale',
                             started_at=NOW, duration_seconds=600)
        self.close('first', NOW + timedelta(seconds=600))

    def close(self, window_id, at):
        previous = self.checkpoint.load()
        self.writer.capture_receipted_window(
            start=JournalPosition('R1', previous.sequence + 1),
            end=JournalPosition('R1', previous.sequence + 10), previous=previous,
            scan_completed_at=at)
        return windows.close_eligible_window(self.store, self.checkpoint, window_id=window_id, now=at)

    def successor(self, predecessor='first', now=NOW + timedelta(seconds=602), store=None):
        return windows.prepare_successor_window(store or self.store, self.checkpoint,
                                                predecessor_id=predecessor, now=now)

    def test_three_windows_keep_exact_boundaries_and_no_redating(self):
        second = self.successor()
        self.assertEqual(second['previous'], {'receiver': 'R1', 'sequence': 19})
        self.assertEqual(second['started_at'], (NOW + timedelta(seconds=600)).isoformat())
        self.assertEqual(second, self.successor(now=NOW + timedelta(seconds=603)))
        second_closed = self.close(second['window_id'], NOW + timedelta(seconds=1200))
        third = self.successor(second['window_id'], NOW + timedelta(seconds=1202))
        self.assertEqual(third['previous'], second_closed['end'])
        self.assertEqual(third['started_at'], second_closed['closed_at'])
        third_closed = self.close(third['window_id'], NOW + timedelta(seconds=1800))
        self.assertEqual(third_closed['end'], {'receiver': 'R1', 'sequence': 39})
        self.assertEqual(third_closed['event_count'], 0)
        self.assertEqual(third_closed['delivery_latency_state'], 'unobserved')

    def lost_response(self, suffix):
        from unittest.mock import patch
        original = self.store.put_once
        def lost(key, content):
            original(key, content)
            if key.endswith(suffix): raise OSError('response lost')
        with patch.object(self.store, 'put_once', side_effect=lost):
            with self.assertRaises(OSError): self.successor()
        recovered = self.successor(now=NOW + timedelta(seconds=603))
        self.assertEqual(recovered, self.successor(now=NOW + timedelta(seconds=604)))

    def test_lost_link_response_resumes(self):
        self.lost_response('successor.json')

    def test_lost_intent_response_resumes(self):
        self.lost_response('intent.json')

    def test_missing_intent_cannot_be_created_after_checkpoint_advanced(self):
        previous = self.checkpoint.load()
        self.writer.capture_receipted_window(start=JournalPosition('R1', 20), end=JournalPosition('R1', 29),
                                            previous=previous, scan_completed_at=NOW + timedelta(seconds=610))
        with self.assertRaises(ValueError): self.successor(now=NOW + timedelta(seconds=610))

    def test_unclosed_predecessor_and_expired_new_window_are_rejected(self):
        with self.assertRaises(FileNotFoundError): self.successor('absent')
        with self.assertRaises(ValueError): self.successor(now=NOW + timedelta(seconds=1300))
        with self.assertRaises(ValueError): self.successor(now=NOW + timedelta(seconds=599))

    def test_existing_successor_resumes_after_committed_scan(self):
        second = self.successor()
        previous = self.checkpoint.load()
        self.writer.capture_receipted_window(start=JournalPosition('R1', 20), end=JournalPosition('R1', 29),
                                            previous=previous, scan_completed_at=NOW + timedelta(seconds=610))
        self.assertEqual(second, self.successor(now=NOW + timedelta(seconds=611)))

    def test_existing_intent_with_missing_or_corrupt_link_is_not_repaired(self):
        from unittest.mock import patch
        self.successor()
        original = self.store.get_bounded
        for replacement in (None, b'null', b'{"unexpected":true}'):
            def read(key, limit):
                if key.endswith('successor.json'):
                    if replacement is None: raise FileNotFoundError(key)
                    return replacement
                return original(key, limit)
            with self.subTest(replacement=replacement), patch.object(self.store, 'get_bounded', side_effect=read), patch.object(self.store, 'put_once') as put:
                with self.assertRaises(ValueError): self.successor()
                put.assert_not_called()

    def test_successor_intent_with_other_stream_is_rejected(self):
        from unittest.mock import patch
        second = self.successor()
        original = self.store.get_bounded
        def read(key, limit):
            if key == f"windows/{second['window_id']}/intent.json":
                return json.dumps(dict(second, stream_id='other')).encode()
            return original(key, limit)
        with patch.object(self.store, 'get_bounded', side_effect=read):
            with self.assertRaises(ValueError): self.successor()

    def test_expired_open_successor_fails_but_valid_closed_successor_can_resume(self):
        second = self.successor()
        later = NOW + timedelta(days=1)
        with self.assertRaises(ValueError): self.successor(now=later)
        self.close(second['window_id'], NOW + timedelta(seconds=1200))
        self.assertEqual(self.successor(now=later), second)

    def test_missing_receipt_of_existing_closure_is_not_treated_as_open(self):
        from unittest.mock import patch
        second = self.successor()
        closed = self.close(second['window_id'], NOW + timedelta(seconds=1200))
        key_to_hide = closed['receipts'][0]['key']
        original = self.store.get_bounded
        def read(key, limit):
            if key == key_to_hide: raise FileNotFoundError(key)
            return original(key, limit)
        with patch.object(self.store, 'get_bounded', side_effect=read):
            with self.assertRaises(FileNotFoundError):
                self.successor(now=NOW + timedelta(seconds=1201))

    def test_boolean_predecessor_end_is_rejected_before_publication(self):
        from unittest.mock import patch
        checkpoint = JsonCheckpointStore(Path(self.directory.name) / 'tiny-checkpoint.json')
        checkpoint.commit(JournalPosition('R1', 0))
        windows.begin_window(self.store, checkpoint, window_id='tiny', stream_id='dev-sale',
                             started_at=NOW, duration_seconds=600)
        writer = RawFirstCaptureCoordinator(self.store, checkpoint)
        writer.capture_receipted_window(start=JournalPosition('R1', 1), end=JournalPosition('R1', 1),
                                        previous=JournalPosition('R1', 0), scan_completed_at=NOW + timedelta(seconds=600))
        closed = windows.close_eligible_window(self.store, checkpoint, window_id='tiny', now=NOW + timedelta(seconds=600))
        invalid = dict(closed, end={'receiver': 'R1', 'sequence': True})
        original = self.store.get_bounded
        def read(key, limit):
            if key == 'windows/tiny/closed.json': return json.dumps(invalid).encode()
            return original(key, limit)
        with patch.object(self.store, 'get_bounded', side_effect=read), patch.object(self.store, 'put_once') as put:
            with self.assertRaises(ValueError):
                windows.prepare_successor_window(self.store, checkpoint, predecessor_id='tiny', now=NOW + timedelta(seconds=601))
            put.assert_not_called()


if __name__ == '__main__':
    unittest.main()
