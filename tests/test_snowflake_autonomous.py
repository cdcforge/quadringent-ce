from __future__ import annotations

import site_fixture

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import unittest

from quadringent.snowflake_loader import SnowflakeAutonomousLoadPlan
from quadringent.destination_sync import DestinationSyncError


SITE = site_fixture.build_test_site()

ROOT = Path(__file__).parents[1]
AUTONOMOUS_PROOF_URI = SITE.autonomous_proof_s3_uri


class ProvisioningCursor:
    def __init__(
        self,
        pipe_rows: list[tuple[object, ...]] | None = None,
        *,
        legacy_dynamic_table: bool = False,
        fail_prefix: str | None = None,
        fail_error: Exception | None = None,
    ) -> None:
        self.executed: list[str] = []
        self.description = None
        self._pipe_rows = pipe_rows if pipe_rows is not None else [
            (
                "QUADRINGENT_SALE_PIPE",
                "arn:aws:sqs:us-east-1:000000000001:sf-snowpipe",
            )
        ]
        self._legacy_dynamic_table = legacy_dynamic_table
        self._fail_prefix = fail_prefix
        self._fail_error = fail_error or RuntimeError("controlled provisioning failure")
        self._rows: list[tuple[object, ...]] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self._fail_prefix and sql.startswith(self._fail_prefix):
            self._fail_prefix = None
            raise self._fail_error
        if sql.startswith("SHOW DYNAMIC TABLES"):
            self.description = (("name",),)
            self._rows = (
                [("QUADRINGENT_SALE_CANONICAL",)]
                if self._legacy_dynamic_table
                else []
            )
        elif sql.startswith("SHOW PIPES"):
            self.description = (("name",), ("notification_channel",))
            self._rows = self._pipe_rows
        else:
            self.description = None
            self._rows = []

    def fetchall(self):
        return list(self._rows)


class VerificationCursor:
    def __init__(
        self,
        *,
        raw_rows: int = 7,
        distinct_events: int = 7,
        source_files: int = 2,
        canonical_rows: int = 7,
        execution_state: str = "RUNNING",
    ) -> None:
        self.raw_rows = raw_rows
        self.distinct_events = distinct_events
        self.source_files = source_files
        self.canonical_rows = canonical_rows
        self.execution_state = execution_state
        self.executed: list[str] = []
        self._row: tuple[object, ...] | None = None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if "SYSTEM$PIPE_STATUS" in sql:
            self._row = (
                json.dumps(
                    {
                        "executionState": self.execution_state,
                        "pendingFileCount": 0,
                    }
                ),
            )
        elif "COUNT(DISTINCT SOURCE_FILE)" in sql:
            self._row = (self.raw_rows, self.distinct_events, self.source_files)
        elif "QUADRINGENT_SALE_CANONICAL" in sql:
            self._row = (self.canonical_rows,)
        else:
            raise AssertionError(f"unexpected verification query: {sql}")

    def fetchone(self):
        return self._row


def plan() -> SnowflakeAutonomousLoadPlan:
    return SnowflakeAutonomousLoadPlan(
        scope=SITE.snowflake_scope,
        stage=SITE.proof_stage,
        raw_table=SITE.proof_raw_table,
        canonical_table=SITE.proof_canonical_table,
        pipe=SITE.proof_pipe,
        warehouse=SITE.warehouse_name,
    )


class SnowflakeAutonomousProvisioningTests(unittest.TestCase):
    def test_provision_treats_an_already_suspended_warehouse_as_success(self) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        class WarehouseAlreadySuspendedError(RuntimeError):
            errno = 90064

        cursor = ProvisioningCursor(
            legacy_dynamic_table=True,
            fail_prefix="ALTER WAREHOUSE IF EXISTS",
            fail_error=WarehouseAlreadySuspendedError("already suspended"),
        )

        result = provision_autonomous_destination(cursor, plan(), site=SITE)

        self.assertEqual(result["status"], "READY_FOR_S3_NOTIFICATION")
        self.assertEqual(
            sum(sql.startswith("DROP DYNAMIC TABLE") for sql in cursor.executed),
            1,
        )
        self.assertFalse(
            any(sql.startswith("DROP VIEW IF EXISTS") for sql in cursor.executed)
        )

    def test_pause_treats_an_already_suspended_warehouse_as_success(self) -> None:
        from quadringent.snowflake_autonomous import pause_autonomous_destination

        class WarehouseAlreadySuspendedError(RuntimeError):
            errno = 90064

        cursor = ProvisioningCursor(
            fail_prefix="ALTER WAREHOUSE IF EXISTS",
            fail_error=WarehouseAlreadySuspendedError("already suspended"),
        )

        result = pause_autonomous_destination(cursor, plan(), site=SITE)

        self.assertEqual(result["status"], "PAUSED")

    def test_provision_does_not_hide_other_warehouse_suspend_errors(self) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        class WarehousePermissionError(RuntimeError):
            errno = 12345

        cursor = ProvisioningCursor(
            legacy_dynamic_table=True,
            fail_prefix="ALTER WAREHOUSE IF EXISTS",
            fail_error=WarehousePermissionError("not authorized"),
        )

        with self.assertRaisesRegex(WarehousePermissionError, "not authorized"):
            provision_autonomous_destination(cursor, plan(), site=SITE)

        self.assertTrue(
            any(sql.startswith("DROP VIEW IF EXISTS") for sql in cursor.executed)
        )
        self.assertTrue(
            any(sql.startswith("CREATE OR ALTER DYNAMIC TABLE") for sql in cursor.executed)
        )

    def test_provision_returns_the_snowpipe_notification_channel(self) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        result = provision_autonomous_destination(ProvisioningCursor(), plan(), site=SITE)

        self.assertEqual(result["status"], "READY_FOR_S3_NOTIFICATION")
        self.assertEqual(result["environment"], "test")
        self.assertEqual(
            result["notification_channel"],
            "arn:aws:sqs:us-east-1:000000000001:sf-snowpipe",
        )
        self.assertEqual(result["pipe"], "ACME_RAW.IBMI_TEST.QUADRINGENT_SALE_PIPE")
        self.assertNotIn("credential", json.dumps(result).lower())

    def test_provision_fails_closed_when_pipe_cannot_be_identified(self) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        with self.assertRaises(RuntimeError):
            provision_autonomous_destination(ProvisioningCursor(pipe_rows=[]), plan(), site=SITE)

    def test_provision_migrates_the_exact_legacy_dynamic_table_after_preparation(
        self,
    ) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        cursor = ProvisioningCursor(legacy_dynamic_table=True)
        provision_autonomous_destination(cursor, plan(), site=SITE)

        prepare_pipe = next(
            index
            for index, sql in enumerate(cursor.executed)
            if sql.startswith("CREATE OR ALTER PIPE")
        )
        drop_legacy = next(
            index
            for index, sql in enumerate(cursor.executed)
            if sql.startswith("DROP DYNAMIC TABLE")
        )
        create_view = next(
            index
            for index, sql in enumerate(cursor.executed)
            if sql.startswith("CREATE OR REPLACE VIEW")
        )
        self.assertLess(prepare_pipe, drop_legacy)
        self.assertLess(drop_legacy, create_view)
        self.assertEqual(
            sum(sql.startswith("DROP DYNAMIC TABLE") for sql in cursor.executed),
            1,
        )

    def test_provision_does_not_drop_an_existing_view_on_rerun(self) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        cursor = ProvisioningCursor(legacy_dynamic_table=False)
        provision_autonomous_destination(cursor, plan(), site=SITE)

        self.assertFalse(
            any(sql.startswith("DROP DYNAMIC TABLE") for sql in cursor.executed)
        )
        self.assertTrue(
            any(sql.startswith("CREATE OR REPLACE VIEW") for sql in cursor.executed)
        )

    def test_failed_migration_pauses_loader_and_restores_dynamic_table(self) -> None:
        from quadringent.snowflake_autonomous import provision_autonomous_destination

        cursor = ProvisioningCursor(
            legacy_dynamic_table=True,
            fail_prefix="CREATE OR REPLACE VIEW",
        )

        with self.assertRaisesRegex(RuntimeError, "controlled provisioning failure"):
            provision_autonomous_destination(cursor, plan(), site=SITE)

        self.assertTrue(
            any(
                sql.startswith("ALTER PIPE IF EXISTS")
                and sql.endswith("PIPE_EXECUTION_PAUSED = TRUE")
                for sql in cursor.executed
            )
        )
        self.assertTrue(
            any(
                sql == f'ALTER WAREHOUSE IF EXISTS "{SITE.warehouse_name}" SUSPEND'
                for sql in cursor.executed
            )
        )
        self.assertTrue(
            any(sql.startswith("DROP VIEW IF EXISTS") for sql in cursor.executed)
        )
        self.assertTrue(
            any(sql.startswith("CREATE OR ALTER DYNAMIC TABLE") for sql in cursor.executed)
        )
        self.assertTrue(
            any(
                sql.startswith("ALTER DYNAMIC TABLE") and sql.endswith(" SUSPEND")
                for sql in cursor.executed
            )
        )

    def test_setup_cli_is_dry_run_by_default(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/quadringent_snowpipe_setup.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertEqual(payload["environment"], "test")
        self.assertEqual(payload["statement_count"], 6)
        self.assertEqual(payload["confirmation"], SITE.confirmation_token("AUTONOMOUS_LOAD"))
        self.assertNotIn("password", result.stdout.lower())

    def test_setup_cli_requires_exact_confirmation_before_connecting(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_snowpipe_setup.py",
                "--execute",
                "--confirm",
                "wrong",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("exact site confirmation is required", result.stderr)

    def test_setup_cli_rollback_requires_its_own_exact_confirmation(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_snowpipe_setup.py",
                "--rollback",
                "--execute",
                "--confirm",
                "wrong",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("exact site rollback confirmation is required", result.stderr)

    def test_read_only_verifier_cli_exposes_bounded_inputs(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "src"
        result = subprocess.run(
            [sys.executable, "scripts/quadringent_autonomous_verify.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--capture-snapshot", result.stdout)
        self.assertIn("--object-keys-file", result.stdout)
        self.assertIn("--proof-output", result.stdout)
        self.assertIn("--proof-s3-uri", result.stdout)
        self.assertIn("--publish-confirm", result.stdout)
        self.assertNotIn("--execute", result.stdout)

    def test_proof_publisher_accepts_only_the_versioned_dev_cockpit_key(self) -> None:
        from quadringent.snowflake_autonomous import autonomous_proof_s3_target

        self.assertEqual(
            autonomous_proof_s3_target(
                AUTONOMOUS_PROOF_URI, expected=SITE.autonomous_proof_s3_uri
            ),
            (
                SITE.raw_bucket,
                SITE.autonomous_proof_key,
            ),
        )
        for unsafe in (
            "s3://other/ibmi/ledger/sale/proofs/quadringent-autonomous-latest.json",
            f"s3://{SITE.raw_bucket}/ibmi/ledger/sale/batch.jsonl",
            f"s3://{SITE.raw_bucket}/ibmi/ledger/sale/proofs/other.json",
            AUTONOMOUS_PROOF_URI + "?versionId=unsafe",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                autonomous_proof_s3_target(
                    unsafe, expected=SITE.autonomous_proof_s3_uri
                )

    def test_proof_publisher_writes_only_the_bounded_no_store_json(self) -> None:
        from quadringent.snowflake_autonomous import publish_autonomous_proof

        calls: list[dict[str, object]] = []

        class FakeClient:
            def put_object(self, **kwargs: object) -> None:
                calls.append(kwargs)

        publish_autonomous_proof(FakeClient(), AUTONOMOUS_PROOF_URI, b'{"ok":true}\n',
                             expected=SITE.autonomous_proof_s3_uri, create_only=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            {
                "Bucket": SITE.raw_bucket,
                "Key": SITE.autonomous_proof_key,
                "Body": b'{"ok":true}\n',
                "ContentType": "application/json",
                "CacheControl": "no-store",
                "IfNoneMatch": "*",
            },
        )

    def test_proof_publish_requires_the_exact_dev_confirmation(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_autonomous_verify.py",
                "--capture-snapshot",
                "/missing-capture.json",
                "--object-keys-file",
                "/missing-keys.txt",
                "--run-tag",
                "AUTONOMY",
                "--proof-output",
                "/tmp/missing-proof.json",
                "--proof-s3-uri",
                AUTONOMOUS_PROOF_URI,
                "--publish-confirm",
                "wrong",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("exact site proof publication confirmation is required", result.stderr)


class SnowflakeAutonomousVerificationTests(unittest.TestCase):
    def test_importing_verifier_does_not_remove_sibling_scripts_from_path(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; from pathlib import Path; "
             "sys.path.insert(0, str(Path('scripts').resolve())); "
             "import quadringent_autonomous_verify; import quadringent_slo_collect"],
            cwd=ROOT, capture_output=True, text=True, check=False,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_publication_authentication_uses_explicit_profile_or_default_chain(self) -> None:
        import quadringent_autonomous_verify as cli

        self.assertTrue(callable(getattr(cli, "_publication_client", None)))
        for profile in ("acme-test", None):
            with self.subTest(profile=profile):
                calls = []
                s3 = object()

                class Session:
                    def client(self, service):
                        calls.append(service)
                        if service == "sts":
                            return self
                        if service == "s3":
                            return s3
                        raise AssertionError(service)

                    def get_caller_identity(self):
                        return {"Account": "000000000001"}

                class SDK:
                    def Session(self, **kwargs):
                        calls.append(kwargs)
                        return Session()

                self.assertIs(cli._publication_client(SDK(), profile), s3)
                expected = {"region_name": "us-east-1"}
                if profile is not None:
                    expected["profile_name"] = profile
                self.assertEqual(calls, [expected, "sts", "s3"])

    def test_publication_refuses_wrong_or_missing_aws_account_before_s3(self) -> None:
        import quadringent_autonomous_verify as cli

        self.assertTrue(callable(getattr(cli, "_publication_client", None)))
        for identity in ({"Account": "000000000000"}, {}, {"Account": None}):
            with self.subTest(identity=identity):
                class SDK:
                    def Session(self, **kwargs):
                        return self

                    def client(self, service):
                        if service != "sts":
                            raise AssertionError("S3 must not be constructed")
                        return self

                    def get_caller_identity(self):
                        return identity

                with self.assertRaises(ValueError):
                    cli._publication_client(SDK(), None)

    def test_verifier_refuses_conflicting_aws_auth_modes_before_io(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/quadringent_autonomous_verify.py",
             "--capture-snapshot", "/missing", "--object-keys-file", "/missing",
             "--run-tag", "TEST", "--proof-output", "/missing",
             "--aws-profile", "acme-test", "--aws-default-credentials"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_verifier_attributes_view_queries_to_the_dedicated_warehouse(self) -> None:
        from quadringent_autonomous_verify import _connect_snowflake

        connection = object()

        class Connector:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def connect(self, **kwargs: object):
                self.calls.append(kwargs)
                return connection

        connector = Connector()

        result = _connect_snowflake(connector, "acme")

        self.assertIs(result, connection)
        self.assertEqual(len(connector.calls), 1)
        self.assertEqual(connector.calls[0]["connection_name"], "acme")
        self.assertEqual(connector.calls[0]["warehouse"], SITE.warehouse_name)
        self.assertEqual(
            connector.calls[0]["session_parameters"],
            {"QUERY_TAG": "QUADRINGENT_AUTONOMY_VERIFY_READONLY"},
        )

    def test_verifier_oidc_uses_only_dedicated_dev_connection(self) -> None:
        from quadringent_autonomous_verify import _connect_snowflake
        from unittest.mock import Mock

        connector = Mock()
        result = _connect_snowflake(connector, None, oidc_token_file="/var/run/secrets/snowflake/token")
        self.assertIs(result, connector.connect.return_value)
        options = connector.connect.call_args.kwargs
        self.assertEqual(options["authenticator"], "WORKLOAD_IDENTITY")
        self.assertEqual(options["workload_identity_provider"], "OIDC")
        self.assertEqual(options["account"], SITE.snowflake_account)
        self.assertEqual(options["role"], SITE.verifier_role_name)
        self.assertEqual(options["warehouse"], SITE.warehouse_name)
        self.assertEqual(options["database"], SITE.destination_database)
        self.assertEqual(options["schema"], SITE.destination_schema)
        self.assertEqual(options["token_file_path"], "/var/run/secrets/snowflake/token")
        for forbidden in ("connection_name", "password", "private_key", "token"):
            self.assertNotIn(forbidden, options)

    def test_verifier_oidc_failure_never_falls_back_to_local_profile(self) -> None:
        from quadringent_autonomous_verify import _connect_snowflake
        from unittest.mock import Mock

        connector = Mock()
        connector.connect.side_effect = RuntimeError("controlled auth failure")
        with self.assertRaises(RuntimeError):
            _connect_snowflake(connector, None, oidc_token_file="/missing/token")
        self.assertEqual(connector.connect.call_count, 1)
        self.assertNotIn("connection_name", connector.connect.call_args.kwargs)

    def test_verifier_auth_contract_rejects_invalid_modes_before_connect(self) -> None:
        from quadringent_autonomous_verify import _connect_snowflake
        from unittest.mock import Mock

        for profile, token in (("acme", "/token"), (None, None), (None, "relative/token"), (None, "")):
            with self.subTest(profile=profile, token=token):
                connector = Mock()
                with self.assertRaises(ValueError):
                    _connect_snowflake(connector, profile, oidc_token_file=token)
                connector.connect.assert_not_called()

    def test_verifier_cli_refuses_mixed_snowflake_authentication(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/quadringent_autonomous_verify.py",
             "--capture-snapshot", "/missing", "--object-keys-file", "/missing",
             "--run-tag", "TEST", "--proof-output", "/missing",
             "--connection-name", "acme", "--snowflake-oidc-token-file", "/token"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_verifier_cli_rejects_mixed_and_incomplete_input_modes(self) -> None:
        for inputs in ([], ['--capture-snapshot','/missing'],
                       ['--run-id','test-run','--capture-snapshot','/missing']):
            with self.subTest(inputs=inputs):
                result = subprocess.run(
                    [sys.executable,'scripts/quadringent_autonomous_verify.py',
                     '--run-tag','TEST','--proof-output','/missing',*inputs],
                    cwd=ROOT,capture_output=True,text=True,check=False)
                self.assertEqual(result.returncode,2)
                self.assertIn('error:',result.stderr)

    def test_verifier_main_routes_oidc_without_using_default_example_profile(self) -> None:
        import quadringent_autonomous_verify as cli
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory
        from types import ModuleType
        from unittest.mock import Mock, patch

        connector = Mock()
        snowflake = ModuleType("snowflake")
        snowflake.connector = connector
        with TemporaryDirectory() as temporary:
            capture = Path(temporary) / "capture.json"
            keys = Path(temporary) / "keys.txt"
            output = Path(temporary) / "proof.json"
            capture.write_text("{}")
            keys.write_text("batch-test.jsonl\n")
            argv = ["verify", "--capture-snapshot", str(capture),
                    "--object-keys-file", str(keys), "--proof-output", str(output),
                    "--run-tag", "TEST", "--snowflake-oidc-token-file", "/projected/token"]
            with patch.object(sys, "argv", argv), patch.dict(sys.modules, {
                "snowflake": snowflake, "snowflake.connector": connector,
            }), patch.object(cli, "verify_autonomous_destination", return_value={"test": True}), \
                    patch.object(cli, "_validate_cockpit_document"), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(connector.connect.call_count, 1)
            self.assertNotIn("connection_name", connector.connect.call_args.kwargs)
            self.assertEqual(connector.connect.call_args.kwargs["token_file_path"], "/projected/token")
            connector.connect.return_value.close.assert_called_once()
            self.assertEqual(json.loads(output.read_text()), {"test": True})

    def test_verifier_main_acquires_run_before_destination_verification(self) -> None:
        import quadringent_autonomous_verify as cli
        from contextlib import redirect_stdout
        from io import StringIO
        from tempfile import TemporaryDirectory
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock, patch

        connector = Mock()
        snowflake = ModuleType('snowflake')
        snowflake.connector = connector
        sdk = ModuleType('boto3')
        storage = object()
        window = SimpleNamespace(capture={'test_capture':True}, object_keys=('test.jsonl',),
                                 event_ids_sha256='a'*64)
        order = []
        def closed(*args, **kwargs):
            order.append('closed')
        def acquire(*args, **kwargs):
            self.assertEqual(order, ['closed'])
            order.append('acquired')
            return window
        with TemporaryDirectory() as temporary:
            argv=['verify','--run-id','test-run','--run-tag','TEST','--proof-output',
                  str(Path(temporary)/'proof.json'),'--aws-default-credentials',
                  '--snowflake-oidc-token-file','/projected/token', '--await-capture-seconds','660']
            with patch.object(sys,'argv',argv), patch.dict(sys.modules,{
                'snowflake':snowflake,'snowflake.connector':connector,'boto3':sdk,
            }), patch.object(cli,'_publication_client',return_value=storage) as aws, \
                    patch.object(cli,'await_capture_closed',side_effect=closed) as awaited, \
                    patch.object(cli,'collect_verification_window',side_effect=acquire) as collect, \
                    patch.object(cli,'verify_autonomous_destination',return_value={}) as verify, \
                    patch.object(cli,'_validate_cockpit_document'), redirect_stdout(StringIO()):
                self.assertEqual(cli.main(),0)
            aws.assert_called_once_with(sdk,None)
            awaited.assert_called_once_with(storage, run_id='test-run', timeout_seconds=660, site=SITE)
            self.assertEqual(order, ['closed', 'acquired'])
            self.assertIs(collect.call_args.args[0],storage)
            self.assertEqual(collect.call_args.kwargs['run_id'],'test-run')
            self.assertIs(verify.call_args.args[1],window.capture)
            self.assertEqual(verify.call_args.kwargs['object_keys'],window.object_keys)
            self.assertEqual(verify.call_args.kwargs['expected_event_ids_sha256'],window.event_ids_sha256)
            self.assertEqual(json.loads((Path(temporary)/'proof.json').read_text())['verification_archive'],
                             {'run_id': 'test-run'})

    def test_cockpit_gate_rejects_overlapping_lag_before_publication(self) -> None:
        from quadringent_autonomous_verify import _validate_cockpit_document
        from quadringent_control_plane.model import ProjectionError

        observed_at = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        document = {
            "format_version": "as400-console-v1",
            "generated_at": observed_at.isoformat(),
            "flux": {"id": SITE.stream_prefix, "label": "SALE"},
            "run": {"state": "STOPPED_BUDGET", "last_error": None},
            "position": {
                "checkpoint": {"receiver": "TRNJRN3862", "sequence": 42},
                "source_tail": {"receiver": "TRNJRN3862", "sequence": 43},
            },
            "lag": {
                "current": {"value": 1},
                "verdict": {"value": "BOUNDED"},
            },
            "counters": {
                "events_published": {"value": 7},
                "errors": {"value": 0},
            },
        }

        _validate_cockpit_document(document, observed_at=observed_at)
        document["lag"]["series"] = {
            "resolution_s": 10.0,
            "sample_count": 2,
            "unknown_sample_count": 0,
            "buckets": [
                {
                    "start_s": 1205.0,
                    "end_s": 1214.0,
                    "min": 1,
                    "max": 1,
                    "last": 1,
                    "samples": 1,
                    "unknown_samples": 0,
                },
                {
                    "start_s": 1210.0,
                    "end_s": 1219.0,
                    "min": 1,
                    "max": 1,
                    "last": 1,
                    "samples": 1,
                    "unknown_samples": 0,
                },
            ],
        }

        with self.assertRaises(ProjectionError):
            _validate_cockpit_document(document, observed_at=observed_at)

    def test_verify_reconciles_a_capture_without_mutating_snowflake(self) -> None:
        from quadringent import snowflake_autonomous

        self.assertTrue(
            hasattr(snowflake_autonomous, "verify_autonomous_destination"),
            "the read-only autonomous verifier is not implemented",
        )
        verify_autonomous_destination = snowflake_autonomous.verify_autonomous_destination

        cursor = VerificationCursor()
        capture = {
            "format_version": "as400-console-v1",
            "position": {
                "checkpoint": {"receiver": "TRNJRN3857", "sequence": 118796348}
            },
            "counters": {"events_published": {"value": 7}},
        }

        combined = verify_autonomous_destination(
            cursor,
            capture,
            plan(),
            object_keys=(
                f"{SITE.stream_prefix}/batch-a.jsonl",
                f"{SITE.stream_prefix}/batch-b.jsonl",
            ),
            run_tag="AUTONOMY20260901",
            observed_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        site=SITE)

        self.assertEqual(combined["destination"]["source_events"], 7)
        self.assertEqual(combined["destination"]["raw_rows"], 7)
        self.assertEqual(combined["destination"]["canonical_rows"], 7)
        self.assertEqual(combined["destination"]["duplicates"], 0)
        self.assertTrue(cursor.executed)
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql in cursor.executed))

    def test_verify_fails_when_pipe_is_not_running(self) -> None:
        from quadringent import snowflake_autonomous

        self.assertTrue(
            hasattr(snowflake_autonomous, "verify_autonomous_destination"),
            "the read-only autonomous verifier is not implemented",
        )
        verify_autonomous_destination = snowflake_autonomous.verify_autonomous_destination

        with self.assertRaises(RuntimeError):
            verify_autonomous_destination(
                VerificationCursor(execution_state="PAUSED"),
                {
                    "format_version": "as400-console-v1",
                    "position": {
                        "checkpoint": {"receiver": "TRNJRN3857", "sequence": 118796348}
                    },
                    "counters": {"events_published": {"value": 7}},
                },
                plan(),
                object_keys=(f"{SITE.stream_prefix}/batch-a.jsonl",),
                run_tag="AUTONOMY20260901",
                observed_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            site=SITE)

    def test_verify_rejects_a_non_sale_key_before_querying(self) -> None:
        from quadringent import snowflake_autonomous

        self.assertTrue(
            hasattr(snowflake_autonomous, "verify_autonomous_destination"),
            "the read-only autonomous verifier is not implemented",
        )
        verify_autonomous_destination = snowflake_autonomous.verify_autonomous_destination

        cursor = VerificationCursor()
        with self.assertRaises(DestinationSyncError):
            verify_autonomous_destination(
                cursor,
                {
                    "format_version": "as400-console-v1",
                    "position": {
                        "checkpoint": {"receiver": "TRNJRN3857", "sequence": 118796348}
                    },
                    "counters": {"events_published": {"value": 7}},
                },
                plan(),
                object_keys=("popsink/batch-a.jsonl",),
                run_tag="AUTONOMY20260901",
                observed_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            site=SITE)
        self.assertEqual(cursor.executed, [])


if __name__ == "__main__":
    unittest.main()
