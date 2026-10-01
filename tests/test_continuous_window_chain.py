from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.continuous import ContinuousCaptureService, CapturedWindow, ReceiverSnapshot
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.proof_windows import begin_window, read_closed_window
from test_continuous import FakeCatalog


class ContinuousWindowChainTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = FileObjectStore(root / 'objects')
        self.checkpoint = JsonCheckpointStore(root / 'checkpoint.json')
        from quadringent.contract import JournalPosition
        self.checkpoint.commit(JournalPosition('R1', 9))
        self.start = datetime(2026, 9, 9, 12, tzinfo=UTC)
        self.clock = self.start
        self.calls = []
        self.scan_seconds = 600
        begin_window(self.store, self.checkpoint, window_id='first', stream_id='dev-sale',
                     started_at=self.start, duration_seconds=600)
        owner = self
        class Runner:
            def capture(self, plan):
                owner.calls.append(plan)
                owner.clock = owner.start + timedelta(seconds=owner.scan_seconds * len(owner.calls))
                return CapturedWindow(plan.end)
        self.runner = Runner()

    def service(self, count=3, root='first'):
        return ContinuousCaptureService(
            FakeCatalog([ReceiverSnapshot('QGPL', 'R1', 1, 1000)]), self.runner,
            RawFirstCaptureCoordinator(self.store, self.checkpoint), self.checkpoint,
            max_entries=10, receipted_scans=True, utc_now=lambda: self.clock,
            proof_window_id=root, proof_window_count=count)

    def test_three_windows_stop_without_fourth_source_read(self):
        service = self.service()
        service.run(max_polls=5)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(service.proof_windows_complete)
        self.assertEqual(service.closed_proof_window['end']['sequence'], 39)
        self.assertEqual(read_closed_window(self.store, window_id='first')['end']['sequence'], 19)
        self.assertEqual(service.run_once().status, 'proof_complete')
        self.assertEqual(len(self.calls), 3)

    def test_restart_follows_durable_chain_and_does_not_rescan(self):
        self.service().run_once()
        resumed = self.service()
        resumed.run(max_polls=5)
        self.assertEqual([(p.start.sequence, p.end.sequence) for p in self.calls], [(10, 19), (20, 29), (30, 39)])
        self.assertTrue(resumed.proof_windows_complete)
        self.service().run(max_polls=5)
        self.assertEqual(len(self.calls), 3)

    def test_failed_successor_prevents_next_scan_until_recovered(self):
        original = self.store.put_once
        def fail(key, content):
            if key.endswith('successor.json'): raise OSError('unavailable')
            return original(key, content)
        service = self.service()
        with patch.object(self.store, 'put_once', side_effect=fail):
            with self.assertRaises(OSError): service.run_once()
            with self.assertRaises(OSError): service.run_once()
            self.assertEqual(len(self.calls), 1)
        service.run(max_polls=5)
        self.assertEqual(len(self.calls), 3)

    def test_window_budget_must_be_a_strict_bounded_integer(self):
        for count in (True, 0, -1, 129, '3'):
            with self.subTest(count=count), self.assertRaises(ValueError): self.service(count)

    def test_restart_inside_second_window_preserves_all_six_scans(self):
        self.scan_seconds = 300
        service = self.service()
        service.run(max_polls=3)
        self.assertFalse(service.proof_windows_complete)
        resumed = self.service()
        resumed.run(max_polls=10)
        self.assertTrue(resumed.proof_windows_complete)
        self.assertEqual([(p.start.sequence, p.end.sequence) for p in self.calls],
                         [(10, 19), (20, 29), (30, 39), (40, 49), (50, 59), (60, 69)])
        self.assertEqual(resumed.closed_proof_window['intent']['previous']['sequence'], 49)
        self.assertEqual(resumed.closed_proof_window['end']['sequence'], 69)

    def test_lost_successor_intent_response_recovers_before_next_scan(self):
        original = self.store.put_once
        def lost(key, content):
            result = original(key, content)
            if key.endswith('intent.json'): raise OSError('response lost')
            return result
        with patch.object(self.store, 'put_once', side_effect=lost):
            with self.assertRaises(OSError): self.service().run_once()
            self.assertEqual(len(self.calls), 1)
        resumed = self.service()
        resumed.run(max_polls=5)
        self.assertTrue(resumed.proof_windows_complete)
        self.assertEqual(len(self.calls), 3)

    def test_restart_cannot_change_or_omit_declared_window_count(self):
        self.service().run_once()
        for count in (1, 2, 4, None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.service(count).run_once()
            self.assertEqual(len(self.calls), 1)

    def test_lost_budget_response_never_starts_source_before_confirmation(self):
        original = self.store.put_once
        def lost(key, content):
            result = original(key, content)
            if key.endswith('chain.json'): raise OSError('response lost')
            return result
        with patch.object(self.store, 'put_once', side_effect=lost):
            with self.assertRaises(OSError): self.service().run_once()
            self.assertEqual(self.calls, [])
        self.service().run(max_polls=5)
        self.assertEqual(len(self.calls), 3)

    def test_cannot_add_budget_retroactively_to_legacy_capture(self):
        self.service(None).run_once()
        with self.assertRaises(ValueError): self.service().run_once()
        self.assertEqual(len(self.calls), 1)

    def test_successor_cannot_become_new_root_with_fresh_budget(self):
        first = self.service()
        first.run_once()
        with self.assertRaises(ValueError):
            self.service(128, root=first.active_proof_window_id).run_once()
        self.assertEqual(len(self.calls), 1)

    def test_chain_mode_cannot_be_disabled_inside_the_same_run(self):
        self.service().run_once()
        with self.assertRaises(ValueError): self.service(None, root=None).run_once()
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__': unittest.main()
