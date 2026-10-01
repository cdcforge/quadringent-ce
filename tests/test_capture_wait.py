import site_fixture
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch

from quadringent import verification_window as window


SITE = site_fixture.build_test_site()

def snapshot(state):
    payload = json.dumps({'format_version':'as400-console-v1', 'run':{'state':state}}).encode()
    return {'Body':BytesIO(payload), 'ContentLength':len(payload)}


class MissingKey(Exception):
    response = {'Error': {'Code':'NoSuchKey'}}


class CaptureWaitTests(unittest.TestCase):
    def test_missing_then_running_then_closed(self):
        client = Mock()
        client.get_object.side_effect = [MissingKey(), snapshot('RUNNING'), snapshot('STOPPED_BUDGET')]
        with patch.object(window.time, 'monotonic', return_value=0), patch.object(window.time, 'sleep') as sleep:
            window.await_capture_closed(client, run_id='test-run', timeout_seconds=20, site=SITE)
        self.assertEqual(client.get_object.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(client.get_object.call_args.kwargs['Key'],
                         f'{SITE.stream_prefix}/runs/test-run/console-snapshot.json')

    def test_failed_unknown_or_invalid_snapshot_is_not_waited_out(self):
        for state in ('STOPPED_FAIL_CLOSED', 'UNKNOWN', None):
            client = Mock()
            client.get_object.return_value = snapshot(state)
            with patch.object(window.time, 'sleep') as sleep:
                with self.assertRaises(ValueError):
                    window.await_capture_closed(client, run_id='test-run', timeout_seconds=20, site=SITE)
                sleep.assert_not_called()

    def test_access_error_is_not_treated_as_missing(self):
        client = Mock()
        client.get_object.side_effect = PermissionError('denied')
        with self.assertRaises(PermissionError):
            window.await_capture_closed(client, run_id='test-run', timeout_seconds=20, site=SITE)
        self.assertEqual(client.get_object.call_count, 1)

    def test_deadline_does_not_trigger_more_reads(self):
        client = Mock()
        with patch.object(window.time, 'monotonic', side_effect=[0, 20]):
            with self.assertRaises(TimeoutError):
                window.await_capture_closed(client, run_id='test-run', timeout_seconds=20, site=SITE)
        client.get_object.assert_not_called()

    def test_late_terminal_snapshot_is_rejected(self):
        client = Mock()
        client.get_object.return_value = snapshot('STOPPED_BUDGET')
        with patch.object(window.time, 'monotonic', side_effect=[0, 0, 21]):
            with self.assertRaises(TimeoutError):
                window.await_capture_closed(client, run_id='test-run', timeout_seconds=20, site=SITE)

    def test_invalid_run_or_budget_is_rejected_before_s3(self):
        for run_id, budget in (('../other', 20), ('test', 0), ('test', 3661), ('test', True)):
            client = Mock()
            with self.assertRaises(ValueError):
                window.await_capture_closed(client, run_id=run_id, timeout_seconds=budget, site=SITE)
            client.get_object.assert_not_called()
