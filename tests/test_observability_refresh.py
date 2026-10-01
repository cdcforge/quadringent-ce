import site_fixture
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
import unittest

from test_verification_window import Store, NOW, PREFIX
from test_observability_snapshot import _console_proof
from test_slo import _policy
from test_slo_telemetry import CloudWatchClient, SnowflakeCursor
from quadringent.verification_window import collect_verification_window
from quadringent.snowflake_autonomous import autonomous_proof_s3_target, AUTONOMOUS_PROOF_S3_URI

BUCKET, KEY = autonomous_proof_s3_target(
    AUTONOMOUS_PROOF_S3_URI, expected=AUTONOMOUS_PROOF_S3_URI
)


SITE = site_fixture.build_test_site()

class RefreshStore(Store):
    def __init__(self):
        super().__init__()
        window = collect_verification_window(self, run_id='test-run', now=NOW, site=SITE)
        self.proof = _console_proof()
        self.proof['flux']['id'] = PREFIX.rstrip('/')
        self.proof['generated_at'] = (NOW + timedelta(seconds=30)).isoformat()
        self.proof['run']['started_at'] = NOW.isoformat()
        self.proof['run']['state'] = 'STOPPED_BUDGET'
        self.proof['destination_proof']['observed_at'] = self.proof['generated_at']
        self.proof['destination_proof']['load']['event_count'] = 1
        for key in ('captured_event_count', 'loaded_event_count', 'ledger_event_count', 'distinct_event_count'):
            self.proof['destination_proof']['reconciliation'][key] = 1
        self.proof['counters']['events_published']['value'] = 1
        self.proof['stored_event_identity_proof'] = {'state':'matched', 'basis':'verified_s3_batches',
            'event_count':1, 'event_ids_sha256':window.event_ids_sha256}
        self.etag = '"original"'
        self.writes = []
        self.race = False

    def get_object(self, **kwargs):
        if kwargs['Key'] != KEY:
            return super().get_object(**kwargs)
        if kwargs.get('IfMatch', self.etag) != self.etag:
            raise RuntimeError('precondition failed')
        payload = json.dumps(self.proof).encode()
        return {'Body':BytesIO(payload), 'ContentLength':len(payload), 'ETag':self.etag}

    def head_object(self, **kwargs):
        assert kwargs['Key'].startswith(PREFIX)
        return {'LastModified':NOW}

    def put_object(self, **kwargs):
        assert kwargs['Bucket'] == BUCKET and kwargs['Key'] == KEY
        if self.race or kwargs['IfMatch'] != self.etag:
            raise RuntimeError('precondition failed')
        self.writes.append(kwargs)
        self.proof = json.loads(kwargs['Body'])
        self.etag = '"published"'
        return {'ETag':self.etag}


class Cursor(SnowflakeCursor):
    def execute(self, sql, params=None):
        if 'APPROX_PERCENTILE' in sql:
            assert len(params) == 4 and 'RIGHT(SOURCE_FILE' in sql
            self._row = (12.0, 20.0, 1.0, 1, 0)
        else:
            super().execute(sql, params)


class ObservabilityRefreshTests(unittest.TestCase):
    def test_refresh_uses_archive_run_not_shared_checkpoint_identity(self):
        from quadringent_observability_refresh import prepare_observability
        store = RefreshStore()
        store.proof['flux']['id'] = f'{SITE.stream_prefix}/runs/checkpoint-origin'
        store.proof['verification_archive'] = {'run_id': 'test-run'}
        before = deepcopy(store.proof)
        result, _ = prepare_observability(store, CloudWatchClient(), Cursor(), _policy(), now=NOW+timedelta(days=1))
        self.assertEqual({k:v for k,v in result.items() if k != 'observability'}, before)
        self.assertEqual(store.writes, [])

    def test_explicit_invalid_archive_never_falls_back_to_flux(self):
        from quadringent_observability_refresh import prepare_observability
        for invalid in (None, {}, {'run_id': '../test-run'}, {'run_id': ''}, {'run_id': 1}):
            with self.subTest(invalid=invalid):
                store = RefreshStore()
                store.proof['verification_archive'] = invalid
                with self.assertRaises(ValueError):
                    prepare_observability(store, CloudWatchClient(), Cursor(), _policy(), now=NOW+timedelta(days=1))
                self.assertEqual(store.writes, [])

    def test_cli_output_and_cleanup_failures_happen_before_publication_and_network_ambiguity_is_explicit(self):
        import quadringent_observability_refresh as cli
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import patch
        import sys

        for mode in ('success', 'output', 'cleanup', 'uncertain_put'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                store = RefreshStore()
                cursor = Cursor()
                def close():
                    if mode == 'cleanup':
                        raise OSError('SENSITIVE')
                cursor.close = close
                connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)
                modules = {'boto3': SimpleNamespace(Session=lambda **kwargs: SimpleNamespace(client=lambda name: CloudWatchClient())),
                    'snowflake': SimpleNamespace(connector=SimpleNamespace()), 'snowflake.connector': SimpleNamespace()}
                policy = Path(directory)/'policy.json'
                policy.write_text(json.dumps(_policy().to_mapping()))
                output = Path(directory)/'out.json'
                if mode == 'output':
                    output.mkdir()
                if mode == 'uncertain_put':
                    original_put = store.put_object
                    def put(**kwargs):
                        original_put(**kwargs)
                        raise OSError('SENSITIVE')
                    store.put_object = put
                stdout, stderr = StringIO(), StringIO()
                with patch.dict(sys.modules, modules), patch.object(cli, '_publication_client', return_value=store), patch.object(cli, '_connect_snowflake', return_value=connection), redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(['--policy', str(policy), '--out', str(output), '--publish-confirm', site_fixture.build_test_site().observability_refresh_token])
                self.assertNotIn('SENSITIVE', stdout.getvalue()+stderr.getvalue())
                if mode == 'success':
                    self.assertEqual(code, 0, stderr.getvalue())
                    self.assertEqual(json.loads(stdout.getvalue())['publication_status'], 'confirmed')
                    self.assertEqual(len(store.writes), 1)
                else:
                    self.assertEqual(code, 3)
                    expected = 'unknown' if mode == 'uncertain_put' else 'not_attempted'
                    self.assertEqual(json.loads(stderr.getvalue())['publication_status'], expected)
                    self.assertEqual(len(store.writes), 1 if mode == 'uncertain_put' else 0)

    def test_preview_preserves_proof_and_never_writes(self):
        from quadringent_observability_refresh import prepare_observability
        store = RefreshStore()
        before = deepcopy(store.proof)
        result, etag = prepare_observability(store, CloudWatchClient(), Cursor(), _policy(), now=NOW+timedelta(days=1))
        self.assertEqual(etag, '"original"')
        self.assertEqual(store.writes, [])
        self.assertEqual({k:v for k,v in result.items() if k!='observability'}, before)
        self.assertEqual(result['observability']['slo_report']['status'], 'breach')

    def test_publication_is_conditional_and_keeps_alert_history(self):
        from quadringent_observability_refresh import prepare_observability, publish_prepared_observability
        store = RefreshStore()
        first, etag = prepare_observability(store, CloudWatchClient(), Cursor(), _policy(), now=NOW+timedelta(days=1))
        publish_prepared_observability(store, first, etag)
        second, etag = prepare_observability(store, CloudWatchClient(), Cursor(), _policy(), now=NOW+timedelta(days=1,seconds=60))
        publish_prepared_observability(store, second, etag)
        self.assertEqual(store.writes[0]['IfMatch'], '"original"')
        self.assertEqual(store.writes[1]['IfMatch'], '"published"')
        a = first['observability']['alert_state']['alerts']
        b = second['observability']['alert_state']['alerts']
        self.assertEqual([x['first_fired_at'] for x in a], [x['first_fired_at'] for x in b])
        self.assertEqual(first['generated_at'], second['generated_at'])

    def test_concurrent_update_and_invalid_archive_do_not_publish(self):
        from quadringent_observability_refresh import prepare_observability, publish_prepared_observability
        for failure in ('race', 'identity'):
            with self.subTest(failure=failure):
                store = RefreshStore()
                if failure == 'race': store.race = True
                else: store.proof['stored_event_identity_proof']['event_ids_sha256'] = '0'*64
                with self.assertRaises((ValueError, RuntimeError)):
                    prepared, etag = prepare_observability(store, CloudWatchClient(), Cursor(), _policy(), now=NOW+timedelta(days=1))
                    publish_prepared_observability(store, prepared, etag)
                self.assertEqual(store.writes, [])
