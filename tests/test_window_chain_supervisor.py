import json
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock

from quadringent_window_supervise import supervise_chain
import quadringent_window_supervise as supervisor
import quadringent_window_chain_read as reader


class ChainSupervisorTests(unittest.TestCase):
    def test_verifier_image_includes_every_copied_script(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root/'docker/verifier.Dockerfile').read_text()
        exceptions = (root/'docker/verifier.Dockerfile.dockerignore').read_text().splitlines()
        for line in dockerfile.splitlines():
            if line.startswith('COPY scripts/'):
                source = line.split()[1]
                self.assertIn('!'+source, exceptions)
                self.assertTrue((root/source).is_file())

    def test_cli_rejects_conflicting_modes(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            supervisor.main(['--run-id', 'r1', '--window-id', 'w1', '--chain-proof-directory', 'unused'])
        self.assertEqual(error.exception.code, 2)

    def test_reader_only_initial_missing_contract_means_pending(self):
        for initial_missing, expected_code in ((True, 2), (False, 3)):
            with self.subTest(initial_missing=initial_missing), patch.dict('sys.modules', {'boto3': MagicMock()}), patch('quadringent_autonomous_verify._publication_client'), patch.object(reader, 'S3ObjectStore') as stores, patch.object(reader, 'read_window_chain', side_effect=FileNotFoundError('missing receipt')) as read, redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                if initial_missing: stores.return_value.get_bounded.side_effect = FileNotFoundError('contract')
                else: stores.return_value.get_bounded.return_value = b'{}'
                self.assertEqual(reader.main(['--run-id', 'r1']), expected_code)
                if initial_missing:
                    self.assertEqual(json.loads(output.getvalue())['status'], 'pending')
                    read.assert_not_called()
                else: read.assert_called_once()

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.clock = 0
        self.calls = []
        self.chain = {'initial_window_id': 'w1', 'window_count': 2, 'stream_id': 'dev-sale',
                      'closed_window_ids': ['w1', 'w2'], 'capture_complete': True}

    def sleep(self, seconds): self.clock += seconds

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        identity = command[command.index('--window-id')+1]
        return CompletedProcess(command, 0, json.dumps({'status': 'matched', 'run_id': 'r1',
            'window_id': identity, 'publication_state': 'confirmed', 'proof_published': True}), '')

    def supervise(self, **kwargs):
        return supervise_chain(run_id='r1', proof_directory=self.directory,
            load_chain=kwargs.pop('load_chain', lambda timeout: self.chain),
            budget_seconds=60, attempt_seconds=10, interval_seconds=1,
            run=kwargs.pop('run', self.fake_run), monotonic=lambda: self.clock, sleep=self.sleep, **kwargs)

    def test_verifies_each_window_in_order_and_separates_proof_paths(self):
        result = self.supervise()
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['matched_window_ids'], ['w1', 'w2'])
        self.assertFalse(result['capture_started'])
        self.assertEqual([c[c.index('--proof-output')+1] for c in self.calls],
                         [str(self.directory/'w1.json'), str(self.directory/'w2.json')])

    def test_empty_first_window_does_not_skip_to_second(self):
        def run(command, **kwargs):
            report = self.fake_run(command, **kwargs)
            value = json.loads(report.stdout)
            value['status'] = 'not_tested'
            return CompletedProcess(command, 2, json.dumps(value), '')
        result = self.supervise(run=run)
        self.assertEqual(result['status'], 'not_tested')
        self.assertEqual(result['matched_window_ids'], [])
        self.assertEqual(len(self.calls), 1)

    def test_pending_chain_and_reader_timeouts_share_global_budget(self):
        def load(timeout):
            self.clock += timeout
            raise TimeoutExpired('reader', timeout)
        result = self.supervise(load_chain=load)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(self.clock, 60)
        self.assertFalse(self.calls)

    def test_read_failure_never_advances(self):
        def load(timeout): raise PermissionError('private')
        self.assertEqual(self.supervise(load_chain=load)['status'], 'chain_read_failed')
        self.assertFalse(self.calls)

    def test_reordered_prefix_after_first_success_is_rejected(self):
        def load(timeout):
            return self.chain if not self.calls else {**self.chain, 'closed_window_ids': ['w2', 'w1']}
        self.assertEqual(self.supervise(load_chain=load)['status'], 'chain_changed')
        self.assertEqual(len(self.calls), 1)

    def test_restart_revalidates_saved_proof_instead_of_skipping(self):
        (self.directory/'w1.json').touch()
        self.assertEqual(self.supervise()['status'], 'matched')
        self.assertIn('--resume-publication', self.calls[0])
