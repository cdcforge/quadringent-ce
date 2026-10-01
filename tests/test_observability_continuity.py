"""Persistent alert history must survive runs and fail closed on lost history."""
import site_fixture
import io
import json
import unittest
from copy import deepcopy

from quadringent import observability_snapshot as snapshots
from quadringent.slo_alerts import reconcile_alerts
from quadringent.snowflake_autonomous import AUTONOMOUS_PROOF_S3_URI, publish_autonomous_proof
from test_observability_snapshot import _console_proof, _report


SITE = site_fixture.build_test_site()

class StorageError(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class Storage:
    def __init__(self, document=None, error=None):
        self.body = io.BytesIO(json.dumps(document).encode())
        self.error = error
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise StorageError(self.error)
        return {'Body': self.body, 'ContentLength': len(self.body.getvalue()), 'ETag': '"version-one"'}


class ContinuityTests(unittest.TestCase):
    def test_publication_api_rejects_unconditional_overwrite(self):
        from unittest.mock import Mock
        with self.assertRaises(ValueError):
            publish_autonomous_proof(Mock(), AUTONOMOUS_PROOF_S3_URI, b'{}', expected=AUTONOMOUS_PROOF_S3_URI)

    def test_cli_cannot_silently_drop_existing_alerts_without_slo_policy(self):
        import quadringent_autonomous_verify as cli
        import sys
        from contextlib import redirect_stderr
        from unittest.mock import Mock, patch
        report = _report(status='breach')
        storage = Storage(snapshots.attach_observability_snapshot(_console_proof(), report, reconcile_alerts(report, site=SITE), site=SITE))
        argv = ['verify', '--capture-snapshot', '/unused-capture', '--object-keys-file', '/unused-keys',
                '--run-tag', 'TEST', '--proof-output', '/unused-output',
                '--proof-s3-uri', AUTONOMOUS_PROOF_S3_URI, '--publish-confirm', SITE.publish_confirmation_token]
        with patch.object(sys, 'argv', argv), patch.dict(sys.modules, {'boto3': Mock()}), \
             patch.object(cli, '_publication_client', return_value=storage), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(), 1)
        self.assertEqual(json.loads(errors.getvalue())['error_type'], 'ValueError')
        self.assertTrue(storage.body.closed)

    def test_oversized_or_truncated_proof_closes_stream_and_fails(self):
        for declared, payload in [(1048577, b'{}'), (100, b'{}')]:
            class InvalidSize(Storage):
                def get_object(self, **kwargs):
                    response = super().get_object(**kwargs)
                    response['ContentLength'] = declared
                    return response
            storage = InvalidSize()
            storage.body = io.BytesIO(payload)
            with self.subTest(declared=declared), self.assertRaises(ValueError):
                self.read(storage)
            self.assertTrue(storage.body.closed)

    def read(self, client):
        reader = getattr(snapshots, 'read_previous_observability', None)
        self.assertTrue(callable(reader), 'persistent alert state reader is missing')
        return reader(client, site=SITE)

    def test_prior_firing_state_is_restored_with_its_publication_version(self):
        proof = _console_proof()
        report = _report(status='breach')
        document = snapshots.attach_observability_snapshot(proof, report, reconcile_alerts(report, site=SITE), site=SITE)
        storage = Storage(document)
        state, etag = self.read(storage)
        self.assertEqual(etag, '"version-one"')
        self.assertEqual(next(a for a in state['alerts'] if a['check_id'] == 'snowpipe_queue')['lifecycle_state'], 'firing')
        self.assertTrue(storage.body.closed)
        self.assertEqual(storage.calls[0]['Key'], SITE.autonomous_proof_key)

    def test_only_missing_key_allows_first_publication(self):
        self.assertEqual(self.read(Storage(error='NoSuchKey')), (None, None))
        for code in ['AccessDenied', 'NoSuchBucket', 'InternalError']:
            with self.subTest(code=code), self.assertRaises(StorageError):
                self.read(Storage(error=code))

    def test_legacy_proof_has_no_history_but_keeps_concurrency_token(self):
        self.assertEqual(self.read(Storage(_console_proof())), (None, '"version-one"'))

    def test_real_isolated_run_flux_identity_is_accepted_without_broadening_scope(self):
        proof = _console_proof()
        proof['flux']['id'] = f'{SITE.stream_prefix}/runs/lan0908d'
        self.assertEqual(self.read(Storage(proof)), (None, '"version-one"'))
        for invalid in [f'{SITE.stream_prefix}/runs/', f'{SITE.stream_prefix}/runs/../cntr',
                        f'{SITE.raw_prefix_root}/cntr/runs/lan0908d', f'{SITE.stream_prefix}/runs/a/b']:
            proof['flux']['id'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.read(Storage(proof))

    def test_corrupt_or_wrong_scope_history_is_not_reset(self):
        report = _report(status='breach')
        document = snapshots.attach_observability_snapshot(_console_proof(), report, reconcile_alerts(report, site=SITE), site=SITE)
        for change in ['digest', 'scope', 'null', 'format']:
            bad = deepcopy(document)
            if change == 'digest':
                bad['observability']['alert_state']['source_report_digest'] = '0' * 64
            elif change == 'scope':
                bad['observability']['environment'] = 'prod'
            elif change == 'null':
                bad['observability'] = None
            else:
                bad['format_version'] = 'invalid'
            storage = Storage(bad)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read(storage)
            self.assertTrue(storage.body.closed)

    def test_conditional_publication_does_not_retry_conflicting_write(self):
        class Writer:
            calls = []
            def put_object(self, **kwargs):
                self.calls.append(kwargs)
                raise StorageError('PreconditionFailed')
        writer = Writer()
        with self.assertRaises(StorageError):
            publish_autonomous_proof(writer, AUTONOMOUS_PROOF_S3_URI, b'{}', expected=AUTONOMOUS_PROOF_S3_URI, expected_etag='"version-one"')
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]['IfMatch'], '"version-one"')

    def test_first_publication_cannot_overwrite_a_concurrent_creator(self):
        class Writer:
            def put_object(self, **kwargs):
                self.request = kwargs
        writer = Writer()
        publish_autonomous_proof(writer, AUTONOMOUS_PROOF_S3_URI, b'{}', expected=AUTONOMOUS_PROOF_S3_URI, create_only=True)
        self.assertEqual(writer.request['IfNoneMatch'], '*')
