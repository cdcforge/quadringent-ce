from __future__ import annotations

import site_fixture

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import sys

from test_slo import _proof
from test_slo_telemetry import CloudWatchClient, NOW, S3Client, SnowflakeCursor


SITE = site_fixture.build_test_site()

class SloCollectCliTests(unittest.TestCase):
    def test_main_passes_the_verified_population_and_blocks_mismatched_identity(self):
        import quadringent_slo_collect as cli
        from test_verification_window import Store, NOW as ARCHIVE_TIME
        from quadringent.verification_window import collect_verification_window
        from unittest.mock import Mock
        from contextlib import redirect_stderr

        store = Store()
        window = collect_verification_window(store, run_id='test-run', now=ARCHIVE_TIME, site=SITE)
        proof = _proof()
        proof['flux'] = {'id': f'{SITE.stream_prefix}/runs/test-run'}
        proof['stored_event_identity_proof'] = {'state': 'matched', 'basis': 'verified_s3_batches',
            'event_count': 1, 'event_ids_sha256': window.event_ids_sha256}
        connect = Mock(return_value=SimpleNamespace(cursor=lambda: SimpleNamespace(close=lambda: None), close=lambda: None))
        connector = SimpleNamespace(connect=connect)
        session = SimpleNamespace(client=lambda service: store if service == 's3' else CloudWatchClient())
        modules = {'boto3': SimpleNamespace(Session=lambda **kwargs: session),
            'snowflake': SimpleNamespace(connector=connector), 'snowflake.connector': connector}

        def collect(storage, cloudwatch, cursor, **kwargs):
            # Boundary contract: the CLI must not fall back to a global window.
            self.assertEqual(kwargs['object_keys'], window.object_keys)
            self.assertEqual(kwargs['expected_event_count'], 1)
            return {'collected_at': ARCHIVE_TIME.isoformat(), 'collection_errors': []}

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'proof.json'
            output = Path(directory) / 'telemetry.json'
            args = ['--proof', str(source), '--out', str(output), '--run-id', 'test-run', '--now', ARCHIVE_TIME.isoformat()]
            with patch.dict(sys.modules, modules), patch.object(cli, 'collect_slo_telemetry', side_effect=collect), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                source.write_text(json.dumps(proof))
                self.assertEqual(cli.main(args), 0)
                self.assertEqual(json.loads(output.read_text())['collection_errors'], [])
                connect.reset_mock()
                proof['stored_event_identity_proof']['event_count'] = 2
                source.write_text(json.dumps(proof))
                self.assertEqual(cli.main(args), 3)
                connect.assert_not_called()

    def test_archive_scope_is_matched_to_the_stored_identity_proof(self):
        from quadringent_slo_collect import _reconciled_archive
        from test_verification_window import Store, NOW as ARCHIVE_TIME
        from quadringent.verification_window import collect_verification_window
        from datetime import timedelta

        store = Store()
        initial = collect_verification_window(store, run_id='test-run', now=ARCHIVE_TIME, site=SITE)
        proof = {
            'flux': {'id': f'{SITE.stream_prefix}/runs/test-run'},
            'stored_event_identity_proof': {'state': 'matched', 'basis': 'verified_s3_batches',
                'event_count': 1, 'event_ids_sha256': initial.event_ids_sha256},
        }
        archive = _reconciled_archive(proof, store, 'test-run', ARCHIVE_TIME + timedelta(days=2))
        self.assertEqual(archive.event_count, 1)
        for field, invalid in [('event_count', 2), ('event_ids_sha256', '0' * 64), ('state', 'unknown')]:
            with self.subTest(field=field):
                changed = {**proof, 'stored_event_identity_proof': {**proof['stored_event_identity_proof'], field: invalid}}
                with self.assertRaises(ValueError):
                    _reconciled_archive(changed, store, 'test-run', ARCHIVE_TIME + timedelta(days=2))
        store.calls.clear()
        with self.assertRaises(ValueError):
            _reconciled_archive(proof, store, 'another-run', ARCHIVE_TIME)
        self.assertEqual(store.calls, [])

    def test_cli_collects_the_bounded_dev_sources_and_writes_atomically(self) -> None:
        import quadringent_slo_collect

        class Session:
            def client(self, service: str):
                if service == "s3":
                    return S3Client()
                if service == "cloudwatch":
                    return CloudWatchClient()
                raise AssertionError("unexpected AWS service")

        class Connection:
            def cursor(self) -> SnowflakeCursor:
                return SnowflakeCursor()

            def close(self) -> None:
                pass

        boto3_module = ModuleType("boto3")
        boto3_module.Session = lambda **kwargs: Session()
        connector_module = ModuleType("snowflake.connector")
        connection_options = []

        def connect(**kwargs):
            connection_options.append(kwargs)
            return Connection()

        connector_module.connect = connect
        snowflake_module = ModuleType("snowflake")
        snowflake_module.connector = connector_module

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "telemetry.json"
            proof = Path(directory) / "proof.json"
            proof.write_text(json.dumps(_proof()), encoding="utf-8")
            with patch.dict(
                sys.modules,
                {
                    "boto3": boto3_module,
                    "snowflake": snowflake_module,
                    "snowflake.connector": connector_module,
                },
            ):
                stdout = StringIO()
                with redirect_stdout(stdout):
                    return_code = quadringent_slo_collect.main(
                        [
                            "--proof", str(proof),
                            "--out", str(output),
                            "--now", NOW.isoformat(),
                        ]
                    )
            telemetry = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(return_code, 0)
        self.assertEqual(connection_options[0].get("warehouse"), SITE.warehouse_name)
        self.assertEqual(connection_options[0].get("database"), SITE.snowflake_scope.database)
        self.assertEqual(connection_options[0].get("schema"), SITE.snowflake_scope.schema)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "pass")
        self.assertEqual(telemetry["collection_errors"], [])
        self.assertEqual(telemetry["snowpipe_pending_files"], 0)
        self.assertEqual(telemetry["s3_requests_24h"], 20)


if __name__ == "__main__":
    unittest.main()
