import site_fixture
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

import quadringent_autonomous_verify as cli
from quadringent import snowflake_autonomous as destination
from test_snowflake_autonomous import VerificationCursor


SITE = site_fixture.build_test_site()

class VerifierWaitTests(unittest.TestCase):
    def test_cli_rejects_unbounded_or_historical_wait_before_connecting(self):
        from contextlib import redirect_stderr
        from io import StringIO
        for options in (['--run-id','x','--wait-seconds','121'],
                        ['--run-id','x','--wait-seconds','-1'],
                        ['--wait-seconds','10']):
            with self.subTest(options=options), \
                 patch('sys.argv', ['verify','--run-tag','TEST','--proof-output','unused'] + options), \
                 patch.object(cli, '_connect_snowflake') as connect, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as result:
                    cli.main()
                self.assertEqual(result.exception.code, 2)
                connect.assert_not_called()

    def test_only_a_partial_file_load_is_retryable(self):
        capture = {'counters':{'events_published':{'value':7}}}
        for cursor, pending in (
            (VerificationCursor(raw_rows=0, distinct_events=0, source_files=0, canonical_rows=0), True),
            (VerificationCursor(raw_rows=3, distinct_events=3, source_files=1, canonical_rows=3), True),
            (VerificationCursor(raw_rows=3, distinct_events=2, source_files=1, canonical_rows=2), False),
            (VerificationCursor(raw_rows=3, distinct_events=3, source_files=2, canonical_rows=3), False),
            (VerificationCursor(execution_state='PAUSED'), False),
        ):
            with self.subTest(pending=pending, counts=cursor.raw_rows):
                try:
                    destination.verify_autonomous_destination(cursor, capture,
                        destination.default_autonomous_plan(), object_keys=(
                            f'{SITE.stream_prefix}/batch-a.jsonl', f'{SITE.stream_prefix}/batch-b.jsonl'),
                        run_tag='TEST', observed_at=datetime.now(timezone.utc), site=SITE)
                except (ValueError, RuntimeError) as error:
                    self.assertEqual(isinstance(error, destination.DestinationLoadPending), pending)
                else:
                    self.fail('Unreconciled input was accepted')

    def test_retry_converges_and_closes_each_cursor(self):
        connection = Mock()
        capture = {'generated_at':datetime.now(timezone.utc).isoformat()}
        with patch.object(cli, 'verify_autonomous_destination', side_effect=[
            destination.DestinationLoadPending('pending'), {'proof':True}]) as verify, \
             patch.object(cli.time, 'sleep') as sleep, \
             patch.object(cli.time, 'monotonic', return_value=0):
            result, _ = cli._verify_with_wait(connection, capture, (), 'TEST', None, 10)
        self.assertEqual(result, {'proof':True})
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(connection.cursor.return_value.close.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_integrity_or_auth_failure_is_never_retried(self):
        for error in (ValueError('identities differ'), RuntimeError('authentication failed')):
            with patch.object(cli, 'verify_autonomous_destination', side_effect=error) as verify, \
                 patch.object(cli.time, 'sleep') as sleep:
                with self.assertRaises(type(error)):
                    cli._verify_with_wait(Mock(), {'generated_at':datetime.now(timezone.utc).isoformat()}, (), 'TEST', None, 10)
                self.assertEqual(verify.call_count, 1)
                sleep.assert_not_called()

    def test_deadline_stops_pending_without_another_query(self):
        with patch.object(cli, 'verify_autonomous_destination', side_effect=destination.DestinationLoadPending('pending')) as verify, \
             patch.object(cli.time, 'monotonic', side_effect=[0, 0, 10]), \
             patch.object(cli.time, 'sleep') as sleep:
            with self.assertRaises(TimeoutError):
                cli._verify_with_wait(Mock(), {'generated_at':datetime.now(timezone.utc).isoformat()}, (), 'TEST', None, 10)
        self.assertEqual(verify.call_count, 1)
        sleep.assert_called_once_with(5)

    def test_expired_capture_is_rejected_before_query(self):
        with patch.object(cli, 'verify_autonomous_destination') as verify:
            with self.assertRaisesRegex(ValueError, 'expired'):
                cli._verify_with_wait(Mock(), {'generated_at':'2020-01-01T00:00:00+00:00'}, (), 'TEST', None, 10)
            verify.assert_not_called()

    def test_success_after_deadline_is_not_returned_as_proof(self):
        with patch.object(cli, 'verify_autonomous_destination', return_value={}), \
             patch.object(cli.time, 'monotonic', side_effect=[0, 11]):
            with self.assertRaises(TimeoutError):
                cli._verify_with_wait(Mock(), {'generated_at':datetime.now(timezone.utc).isoformat()}, (), 'TEST', None, 10)
