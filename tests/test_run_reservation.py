import site_fixture
import importlib
import json
import unittest
from unittest.mock import Mock, patch


SITE = site_fixture.build_test_site()

class RunReservationTests(unittest.TestCase):
    def reserve(self, client, **changes):
        from quadringent.run_reservation import reserve_run
        options = dict(bucket=SITE.raw_bucket,
                       prefix=f'{SITE.stream_prefix}/runs/new-run', run_id='new-run')
        options.update(changes)
        return reserve_run(client, **options, site=SITE)

    def test_new_run_requires_an_atomic_create(self):
        client = Mock()
        client.list_objects_v2.return_value = {'KeyCount': 0}
        self.reserve(client)
        request = client.put_object.call_args.kwargs
        self.assertEqual(request['IfNoneMatch'], '*')
        self.assertEqual(request['Key'], f'{SITE.stream_prefix}/runs/new-run/reservation.json')
        self.assertEqual(json.loads(request['Body']),
                         {'format_version': 'quadringent-run-reservation-v1', 'run_id': 'new-run'})

    def test_fleet_run_reserves_its_neutral_prefix(self):
        client = Mock()
        client.list_objects_v2.return_value = {'KeyCount': 0}
        self.reserve(client, prefix=f'{SITE.raw_prefix_root}/fleet/runs/new-run')
        self.assertEqual(client.put_object.call_args.kwargs['Key'],
                         f'{SITE.raw_prefix_root}/fleet/runs/new-run/reservation.json')

    def test_same_run_resumes_its_own_reservation(self):
        body = json.dumps({'format_version': 'quadringent-run-reservation-v1',
                           'run_id': 'new-run'}).encode()
        client = Mock()
        client.list_objects_v2.return_value = {
            'KeyCount': 1, 'Contents': [{'Key': f'{SITE.stream_prefix}/runs/new-run/reservation.json'}]}
        client.get_object.return_value = {'Body': Mock(read=Mock(return_value=body))}
        self.reserve(client)
        client.put_object.assert_not_called()

    def test_legacy_marker_from_before_the_rename_still_resumes(self):
        """Une réservation suspendue puis reprise de part et d'autre du
        renommage produit reste la même autorité."""
        body = json.dumps({'format_version': 'cdcforge-run-reservation-v1',
                           'run_id': 'new-run'}).encode()
        client = Mock()
        client.list_objects_v2.return_value = {
            'KeyCount': 1, 'Contents': [{'Key': f'{SITE.stream_prefix}/runs/new-run/reservation.json'}]}
        client.get_object.return_value = {'Body': Mock(read=Mock(return_value=body))}
        self.reserve(client)
        client.put_object.assert_not_called()

    def test_existing_prefix_never_gets_a_reservation(self):
        client = Mock()
        client.list_objects_v2.return_value = {'KeyCount': 1, 'Contents': [{'Key': 'existing'}]}
        with self.assertRaises(ValueError):
            self.reserve(client)
        client.put_object.assert_not_called()

    def test_race_or_access_failure_never_becomes_success(self):
        client = Mock()
        client.list_objects_v2.return_value = {'KeyCount': 0}
        client.put_object.side_effect = RuntimeError('conditional-write-refused')
        with self.assertRaisesRegex(RuntimeError, 'conditional-write-refused'):
            self.reserve(client)
        self.assertEqual(client.put_object.call_count, 1)

    def test_another_runs_marker_never_passes(self):
        body = json.dumps({'format_version': 'quadringent-run-reservation-v1',
                           'run_id': 'other-run'}).encode()
        client = Mock()
        client.list_objects_v2.return_value = {
            'KeyCount': 1, 'Contents': [{'Key': f'{SITE.stream_prefix}/runs/new-run/reservation.json'}]}
        client.get_object.return_value = {'Body': Mock(read=Mock(return_value=body))}
        with self.assertRaises(ValueError):
            self.reserve(client)
        client.put_object.assert_not_called()

    def test_invalid_target_is_rejected_before_any_s3_call(self):
        for changes in ({'bucket': 'other'}, {'prefix': 'other/lane'},
                        {'run_id': '../bad'}, {'prefix': f'{SITE.stream_prefix}/runs/other'}):
            with self.subTest(changes=changes):
                client = Mock()
                with self.assertRaises(ValueError):
                    self.reserve(client, **changes)
                self.assertEqual(client.mock_calls, [])

    def test_capture_does_not_initialize_source_after_reservation_failure(self):
        capture = importlib.import_module('as400_continuous_capture')
        with patch('sys.argv', ['capture', '--reserve-run-id', 'new-run']), \
             patch.object(capture, '_reserve_run', side_effect=ValueError('refused')), \
             patch.object(capture, 'DynamoDbCheckpointStore') as checkpoint,patch.object(capture,'DynamoDbSourceGate'), \
             patch.object(capture, 'PersistentJavaWorker') as worker:
            with self.assertRaisesRegex(ValueError, 'refused'):
                capture.main()
            checkpoint.assert_not_called()
            worker.assert_not_called()

    def test_wrong_aws_account_never_creates_an_s3_client(self):
        capture = importlib.import_module('as400_continuous_capture')
        sdk = Mock()
        session = sdk.Session.return_value
        session.client.return_value.get_caller_identity.return_value = {'Account': 'other'}
        with patch.dict('sys.modules', {'boto3': sdk}):
            with self.assertRaisesRegex(ValueError, 'declared site AWS account'):
                capture._reserve_run('new-run')
        session.client.assert_called_once_with('sts')
