"""Le diagnostic pré-vol rapporte des échecs lisibles et reste fail-closed."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import yaml

import site_fixture

import quadringent_preflight as preflight
from quadringent_control_plane.k8s_jobs import JobsResponse


SITE = site_fixture.build_test_site()
VALUES = "infra-values/values-int.yaml"
NAMESPACE = "quadringent-demo"


def _clock() -> float:
    return 0.0


class FakeAwsError(Exception):
    """Erreur AWS factice : seul le code d'erreur est exposé, comme botocore."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self, *, writable: bool = True) -> None:
        self.writable = writable
        self.objects: dict[str, bytes] = {}
        self.calls: list[str] = []

    def head_object(self, **kwargs):
        self.calls.append("head_object")
        raise FakeAwsError("404")

    def put_object(self, **kwargs):
        self.calls.append("put_object")
        if not self.writable:
            raise FakeAwsError("AccessDenied")
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_object(self, **kwargs):
        self.calls.append("get_object")
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]]), "Metadata": {}}


class FakeStsClient:
    def __init__(self, account: str) -> None:
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account}


class FakeDynamoClient:
    def __init__(
        self,
        *,
        status: str = "ACTIVE",
        describe_error: Exception | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.status = status
        self.describe_error = describe_error
        self.get_error = get_error
        self.calls: list[str] = []

    def describe_table(self, **kwargs):
        self.calls.append("describe_table")
        if self.describe_error is not None:
            raise self.describe_error
        return {"Table": {"TableStatus": self.status}}

    def get_item(self, **kwargs):
        self.calls.append("get_item")
        if self.get_error is not None:
            raise self.get_error
        return {}


class FakeSession:
    def __init__(self, clients: dict) -> None:
        self._clients = clients

    def client(self, service: str, config=None):
        return self._clients[service]


def _aws_session(*, account: str = SITE.aws_account_id, writable: bool = True,
                 dynamo: FakeDynamoClient | None = None) -> FakeSession:
    return FakeSession(
        {
            "sts": FakeStsClient(account),
            "s3": FakeS3Client(writable=writable),
            "dynamodb": dynamo or FakeDynamoClient(),
        }
    )


class FakeSnowflakeCursor:
    """Curseur factice : les réponses sont indexées par préfixe de requête."""

    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.executed: list[str] = []
        self._rows: list = []
        self.description = ()
        self.closed = False

    def execute(self, sql: str):
        self.executed.append(sql)
        outcome = self.plan.get("_default")
        for prefix, candidate in self.plan.items():
            if prefix != "_default" and sql.startswith(prefix):
                outcome = candidate
                break
        if outcome is None:
            self._rows, self.description = [], ()
            return self
        if isinstance(outcome, Exception):
            raise outcome
        rows, description = outcome
        self._rows = rows
        self.description = description
        return self

    def fetchall(self):
        return self._rows

    def close(self) -> None:
        self.closed = True


class FakeSnowflakeConnection:
    def __init__(self, cursor: FakeSnowflakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> FakeSnowflakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, connection=None, error: Exception | None = None) -> None:
        self.connection = connection
        self.error = error
        self.kwargs = None

    def connect(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.connection


def _granted_plan() -> dict:
    return {
        "SELECT CURRENT_ROLE()": (
            [("QUADRINGENT_TEST_VERIFIER_ROLE",)],
            (("CURRENT_ROLE()",),),
        ),
        "SHOW GRANTS ON SCHEMA": (
            [("2026-01-01", "CREATE STAGE", "SCHEMA", "ACME_RAW.IBMI_TEST",
              "ROLE", "QUADRINGENT_TEST_VERIFIER_ROLE", "false", "SYSADMIN")],
            (("CREATED_ON",), ("PRIVILEGE",), ("GRANTED_ON",), ("NAME",),
             ("GRANTED_TO",), ("GRANTEE_NAME",), ("GRANT_OPTION",), ("GRANTED_BY",)),
        ),
    }


class FakeTransportFactory:
    """Fabrique un transport factice qui dépile des JobsResponse planifiées."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, context, **kwargs):
        def transport(method, path, body, content_type):
            self.calls.append((method, path, json.loads(body) if body else None))
            if not self.responses:
                raise AssertionError("aucune réponse planifiée")
            return self.responses.pop(0)

        return transport


def _sa_root(directory: Path, namespace: str = NAMESPACE) -> Path:
    (directory / "token").write_text("header.payload.signature\n", encoding="utf-8")
    (directory / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    (directory / "namespace").write_text(namespace, encoding="utf-8")
    return directory


def _providers(**overrides) -> preflight.Providers:
    defaults = {
        "opener": lambda *a, **k: _NullConnection(),
        "session": _aws_session(),
        "aws_error": None,
        "snowflake_connector": None,
        "sa_root": Path("/nonexistent"),
        "transport_factory": FakeTransportFactory([]),
        "clock": _clock,
    }
    defaults.update(overrides)
    return preflight.Providers(**defaults)


class _NullConnection:
    def close(self) -> None:
        pass


def _args(**overrides) -> argparse.Namespace:
    values = {
        "timeout_seconds": 3.0,
        "connection_name": SITE.snowflake_connection,
        "snowflake_oidc_token_file": None,
        "aws_profile": SITE.aws_profile,
        "aws_default_credentials": False,
        "json": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class IbmiTcpCheckTests(unittest.TestCase):
    def test_all_tls_ports_reachable_is_ok(self) -> None:
        attempts = []

        def opener(address, timeout):
            attempts.append(address)
            return _NullConnection()

        result = preflight.check_ibmi_tcp(
            SITE, {}, 3.0, opener=opener, clock=_clock
        )

        self.assertEqual(result.status, "OK")
        self.assertTrue(result.required)
        self.assertEqual(
            [address[1] for address in attempts], [9471, 9475, 9476]
        )
        self.assertIn(SITE.ibmi_host, result.message)

    def test_plaintext_defaults_to_the_unsecured_ports(self) -> None:
        attempts = []
        result = preflight.check_ibmi_tcp(
            SITE,
            {"AS400_TLS": "false", "AS400_ALLOW_PLAINTEXT": "true"},
            3.0,
            opener=lambda address, timeout: attempts.append(address) or _NullConnection(),
            clock=_clock,
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual([a[1] for a in attempts], [8471, 8475, 8476])

    def test_declared_port_overrides_win(self) -> None:
        attempts = []
        environ = {"AS400_DATABASE_PORT": "446", "AS400_SIGNON_PORT": "449"}
        result = preflight.check_ibmi_tcp(
            SITE,
            environ,
            3.0,
            opener=lambda address, timeout: attempts.append(address) or _NullConnection(),
            clock=_clock,
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual([a[1] for a in attempts], [446, 9475, 449])

    def test_unreachable_port_fails_with_an_actionable_message(self) -> None:
        def opener(address, timeout):
            if address[1] == 9476:
                raise OSError("connection refused")
            return _NullConnection()

        result = preflight.check_ibmi_tcp(
            SITE, {}, 3.0, opener=opener, clock=_clock
        )

        self.assertEqual(result.status, "FAIL")
        self.assertTrue(result.required)
        self.assertIn(SITE.ibmi_host, result.message)
        self.assertIn("signon:9476", result.message)
        self.assertIn("routage", result.message)
        self.assertNotIn("Traceback", result.message)
        self.assertNotIn("connection refused", result.message)

    def test_invalid_port_override_fails_clearly(self) -> None:
        result = preflight.check_ibmi_tcp(
            SITE,
            {"AS400_DATABASE_PORT": "99999"},
            3.0,
            opener=lambda *a, **k: _NullConnection(),
            clock=_clock,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("AS400_DATABASE_PORT", result.message)


class S3CheckTests(unittest.TestCase):
    def test_write_and_read_probe_under_the_proof_prefix(self) -> None:
        session = _aws_session()

        result = preflight.check_s3(SITE, session, 3.0, clock=_clock)

        self.assertEqual(result.status, "OK")
        s3 = session._clients["s3"]
        self.assertEqual(s3.calls, ["head_object", "put_object", "get_object"])
        probe_key = next(iter(s3.objects))
        self.assertTrue(
            probe_key.startswith(f"{SITE.stream_prefix}/"),
            probe_key,
        )
        self.assertIn("preflight/", probe_key)

    def test_wrong_aws_account_fails_before_touching_s3(self) -> None:
        session = _aws_session(account="000000000000")

        result = preflight.check_s3(SITE, session, 3.0, clock=_clock)

        self.assertEqual(result.status, "FAIL")
        self.assertIn("000000000000", result.message)
        self.assertIn(SITE.aws_account_id, result.message)
        self.assertNotIn("put_object", session._clients["s3"].calls)

    def test_access_denied_maps_to_an_iam_message(self) -> None:
        session = _aws_session(writable=False)

        result = preflight.check_s3(SITE, session, 3.0, clock=_clock)

        self.assertEqual(result.status, "FAIL")
        self.assertIn("IAM", result.message)
        self.assertIn(SITE.raw_bucket, result.message)
        self.assertNotIn("AccessDenied", result.message)

    def test_missing_session_uses_the_explicit_reason(self) -> None:
        result = preflight.check_s3(
            SITE, None, 3.0, aws_error="boto3 absent de l'image", clock=_clock
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("boto3", result.message)


class DynamoDbCheckTests(unittest.TestCase):
    def test_active_table_and_confirmed_read_is_ok(self) -> None:
        dynamo = FakeDynamoClient()

        result = preflight.check_dynamodb(
            SITE, _aws_session(dynamo=dynamo), 3.0, clock=_clock
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(dynamo.calls, ["describe_table", "get_item"])
        self.assertIn(SITE.checkpoint_table, result.message)

    def test_missing_table_fails_with_the_table_name(self) -> None:
        dynamo = FakeDynamoClient(describe_error=FakeAwsError("ResourceNotFoundException"))

        result = preflight.check_dynamodb(
            SITE, _aws_session(dynamo=dynamo), 3.0, clock=_clock
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn(SITE.checkpoint_table, result.message)
        self.assertIn("inexistante", result.message)

    def test_non_active_table_fails_with_the_state(self) -> None:
        dynamo = FakeDynamoClient(status="CREATING")

        result = preflight.check_dynamodb(
            SITE, _aws_session(dynamo=dynamo), 3.0, clock=_clock
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("CREATING", result.message)

    def test_probe_read_never_writes(self) -> None:
        dynamo = FakeDynamoClient()
        preflight.check_dynamodb(SITE, _aws_session(dynamo=dynamo), 3.0, clock=_clock)

        self.assertNotIn("put_item", dynamo.calls)
        self.assertNotIn("delete_item", dynamo.calls)


class SnowflakeCheckTests(unittest.TestCase):
    def test_no_credentials_configured_skips(self) -> None:
        result = preflight.check_snowflake(
            SITE,
            connector=FakeConnector(),
            connection_name=None,
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "SKIP")
        self.assertIn("aucun identifiant", result.message)

    def test_missing_connector_skips_even_when_configured(self) -> None:
        result = preflight.check_snowflake(
            SITE,
            connector=None,
            connection_name="acme",
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "SKIP")
        self.assertIn("connector", result.message)

    def test_oidc_requested_but_connector_missing_fails_closed(self) -> None:
        result = preflight.check_snowflake(
            SITE,
            connector=None,
            connection_name=None,
            oidc_token_file="/var/run/secrets/snowflake/token",
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertTrue(result.required)
        self.assertIn("snowflakeOidc", result.message)
        self.assertIn("vérificateur", result.message)

    def test_oidc_requires_a_real_token_file(self) -> None:
        result = preflight.check_snowflake(
            SITE,
            connector=FakeConnector(),
            connection_name=None,
            oidc_token_file="/nonexistent/token",
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("jeton OIDC", result.message)

    def test_configured_destination_and_stage_privilege_is_ok(self) -> None:
        cursor = FakeSnowflakeCursor(_granted_plan())
        connector = FakeConnector(FakeSnowflakeConnection(cursor))

        result = preflight.check_snowflake(
            SITE,
            connector=connector,
            connection_name="acme",
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "OK")
        self.assertIn(SITE.destination_namespace, result.message)
        self.assertIn("USE DATABASE ACME_RAW", cursor.executed)
        self.assertIn("USE SCHEMA ACME_RAW.IBMI_TEST", cursor.executed)

    def test_stage_privilege_can_be_proven_by_a_bounded_probe(self) -> None:
        plan = {
            "SELECT CURRENT_ROLE()": ([("LOADER_ROLE",)], (("CURRENT_ROLE()",),)),
            "SHOW GRANTS ON SCHEMA": ([], (("PRIVILEGE",), ("GRANTEE_NAME",))),
        }
        cursor = FakeSnowflakeCursor(plan)
        connector = FakeConnector(FakeSnowflakeConnection(cursor))

        result = preflight.check_snowflake(
            SITE,
            connector=connector,
            connection_name="acme",
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "OK")
        self.assertIn(
            "CREATE STAGE IF NOT EXISTS ACME_RAW.IBMI_TEST.QUADRINGENT_PREFLIGHT_SONDE",
            cursor.executed,
        )
        self.assertIn(
            "DROP STAGE IF EXISTS ACME_RAW.IBMI_TEST.QUADRINGENT_PREFLIGHT_SONDE",
            cursor.executed,
        )

    def test_connection_refusal_is_a_clear_failure(self) -> None:
        connector = FakeConnector(error=RuntimeError("naughty secret detail"))

        result = preflight.check_snowflake(
            SITE,
            connector=connector,
            connection_name="acme",
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("connexion Snowflake refusée", result.message)
        self.assertIn(SITE.snowflake_account, result.message)
        self.assertNotIn("naughty secret detail", result.message)

    def test_unreachable_schema_fails_on_the_declared_destination(self) -> None:
        plan = {
            "SELECT CURRENT_ROLE()": ([("LOADER_ROLE",)], (("CURRENT_ROLE()",),)),
            "USE SCHEMA": RuntimeError("does not exist"),
        }
        cursor = FakeSnowflakeCursor(plan)
        connector = FakeConnector(FakeSnowflakeConnection(cursor))

        result = preflight.check_snowflake(
            SITE,
            connector=connector,
            connection_name="acme",
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("ACME_RAW", result.message)
        self.assertIn("IBMI_TEST", result.message)

    def test_missing_create_stage_privilege_fails(self) -> None:
        plan = {
            "SELECT CURRENT_ROLE()": ([("READER_ROLE",)], (("CURRENT_ROLE()",),)),
            "SHOW GRANTS ON SCHEMA": ([], (("PRIVILEGE",), ("GRANTEE_NAME",))),
            "CREATE STAGE": RuntimeError("insufficient privileges"),
        }
        cursor = FakeSnowflakeCursor(plan)
        connector = FakeConnector(FakeSnowflakeConnection(cursor))

        result = preflight.check_snowflake(
            SITE,
            connector=connector,
            connection_name="acme",
            oidc_token_file=None,
            timeout_seconds=3.0,
            clock=_clock,
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("READER_ROLE", result.message)
        self.assertIn("CREATE STAGE", result.message)


class KubernetesCheckTests(unittest.TestCase):
    def test_out_of_cluster_skips(self) -> None:
        result = preflight.check_kubernetes({}, 3.0, clock=_clock)

        self.assertEqual(result.status, "SKIP")
        self.assertFalse(result.required)

    def test_missing_token_skips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = preflight.check_kubernetes(
                {"KUBERNETES_SERVICE_HOST": "192.0.2.10"},
                3.0,
                sa_root=directory,
                clock=_clock,
            )

        self.assertEqual(result.status, "SKIP")

    def test_allowed_review_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeTransportFactory(
                [JobsResponse(200, {"status": {"allowed": True}})]
            )
            result = preflight.check_kubernetes(
                {"KUBERNETES_SERVICE_HOST": "192.0.2.10"},
                3.0,
                sa_root=_sa_root(Path(directory)),
                transport_factory=factory,
                clock=_clock,
            )

        self.assertEqual(result.status, "OK")
        method, path, body = factory.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("selfsubjectaccessreviews", path)
        self.assertEqual(
            body["spec"]["resourceAttributes"],
            {
                "namespace": NAMESPACE,
                "verb": "create",
                "group": "batch",
                "resource": "jobs",
            },
        )

    def test_denied_review_fails_without_gating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeTransportFactory(
                [JobsResponse(200, {"status": {"allowed": False}})]
            )
            result = preflight.check_kubernetes(
                {"KUBERNETES_SERVICE_HOST": "192.0.2.10"},
                3.0,
                sa_root=_sa_root(Path(directory)),
                transport_factory=factory,
                clock=_clock,
            )

        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.required)
        self.assertIn(NAMESPACE, result.message)
        self.assertIn("ServiceAccount", result.message)

    def test_api_refusal_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            factory = FakeTransportFactory([JobsResponse(403, None)])
            result = preflight.check_kubernetes(
                {"KUBERNETES_SERVICE_HOST": "192.0.2.10"},
                3.0,
                sa_root=_sa_root(Path(directory)),
                transport_factory=factory,
                clock=_clock,
            )

        self.assertEqual(result.status, "FAIL")


class AggregationTests(unittest.TestCase):
    def test_overall_is_fail_closed_on_required_checks(self) -> None:
        ok = preflight.CheckResult("a", "l", "OK", "m")
        skip = preflight.CheckResult("b", "l", "SKIP", "m")
        info_fail = preflight.CheckResult("c", "l", "FAIL", "m", required=False)
        required_fail = preflight.CheckResult("d", "l", "FAIL", "m")

        self.assertEqual(preflight.overall_status([ok, skip, info_fail]), "OK")
        self.assertEqual(preflight.overall_status([ok, required_fail]), "FAIL")

    def test_run_preflight_orders_the_five_checks(self) -> None:
        checks = preflight.run_preflight(SITE, _args(), {}, _providers())

        self.assertEqual(
            [check.name for check in checks],
            [
                "ibmi_tcp",
                "s3_sonde",
                "dynamodb_checkpoints",
                "snowflake_destination",
                "kubernetes_jobs",
            ],
        )

    def test_a_raising_check_becomes_a_readable_failure(self) -> None:
        def broken(*args, **kwargs):
            raise RuntimeError("internal boom")

        checks = preflight.run_preflight(
            SITE, _args(), {}, _providers(opener=broken)
        )

        self.assertEqual(checks[0].status, "FAIL")
        self.assertIn("erreur interne", checks[0].message)
        self.assertNotIn("internal boom", checks[0].message)


class CliTests(unittest.TestCase):
    def _run_main(self, argv: list[str], **env) -> tuple[int, str]:
        opener = lambda address, timeout: _NullConnection()
        session = _aws_session()
        environ = {"ISERIES_PASSWORD": "motdepasse-ibm-i-secret"}
        environ.update(env)
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with patch.dict(os.environ, environ, clear=False), \
                patch.object(preflight.socket, "create_connection", opener), \
                patch.object(preflight, "_aws_session", return_value=(session, None)), \
                patch.object(preflight, "_snowflake_connector", return_value=None), \
                patch.object(
                    preflight,
                    "SERVICE_ACCOUNT_ROOT",
                    _sa_root(Path(directory)),
                ), \
                patch.object(
                    preflight,
                    "https_transport",
                    FakeTransportFactory([JobsResponse(200, {"status": {"allowed": True}})]),
                ), \
                redirect_stdout(stdout):
                code = preflight.main(argv)
            return code, stdout.getvalue()

    def test_exit_zero_when_everything_passes(self) -> None:
        code, output = self._run_main(
            ["--timeout-seconds", "1", "--json"],
            KUBERNETES_SERVICE_HOST="192.0.2.10",
        )

        self.assertEqual(code, 0, output)
        report = json.loads(output)
        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(report["checks"]), 5)
        self.assertNotIn("motdepasse-ibm-i-secret", output)

    def test_fail_closed_exit_code_on_a_required_failure(self) -> None:
        def refusing(address, timeout):
            raise OSError("refused")

        with tempfile.TemporaryDirectory() as directory:
            session = _aws_session()
            stdout = io.StringIO()
            with patch.dict(os.environ, {}, clear=False), \
                patch.object(preflight.socket, "create_connection", refusing), \
                patch.object(preflight, "_aws_session", return_value=(session, None)), \
                patch.object(preflight, "_snowflake_connector", return_value=None), \
                patch.object(preflight, "SERVICE_ACCOUNT_ROOT", Path(directory)), \
                redirect_stdout(stdout):
                code = preflight.main(["--timeout-seconds", "1"])

        self.assertEqual(code, 1)
        self.assertIn("[FAIL]", stdout.getvalue())
        self.assertIn("ÉCHEC", stdout.getvalue())
        json.loads(stdout.getvalue().rsplit("\n", 2)[-2])

    def test_checklist_is_readable_and_secret_free(self) -> None:
        code, output = self._run_main(["--timeout-seconds", "1"])

        self.assertEqual(code, 0, output)
        for marker in ("[OK", "[SKIP", "Résultat :"):
            self.assertIn(marker, output)
        self.assertIn("Pré-vol Quadringent", output)
        self.assertNotIn("motdepasse-ibm-i-secret", output)


class PreflightChartTests(unittest.TestCase):
    def render(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "helm", "template", "cdc", "chart",
                "--namespace", NAMESPACE,
                "-f", VALUES,
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def enabled(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.render("--set", "preflight.enabled=true", *extra)

    def documents(self, result) -> list[dict]:
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def job(self, result) -> dict:
        return next(
            document
            for document in self.documents(result)
            if document.get("kind") == "Job"
            and document["metadata"]["name"] == "quadringent-preflight"
        )

    def test_absent_when_disabled(self) -> None:
        result = self.render()

        self.assertEqual(result.returncode, 0, result.stderr)
        names = [d["metadata"]["name"] for d in self.documents(result)]
        self.assertNotIn("quadringent-preflight", names)

    def test_job_renders_when_enabled(self) -> None:
        result = self.enabled()

        self.assertEqual(result.returncode, 0, result.stderr)
        job = self.job(result)
        self.assertEqual(job["apiVersion"], "batch/v1")
        self.assertEqual(job["metadata"]["namespace"], NAMESPACE)
        spec = job["spec"]["template"]["spec"]
        self.assertEqual(spec["serviceAccountName"], "as400-snowflake-capture")
        self.assertTrue(spec["automountServiceAccountToken"])
        self.assertEqual(spec["restartPolicy"], "Never")
        container = spec["containers"][0]
        self.assertEqual(
            container["command"], ["python", "/app/scripts/quadringent_preflight.py"]
        )
        self.assertEqual(
            container["image"],
            "ghcr.io/quadringent/quadringent@sha256:91f87c4757f4ae1cf09f91dad593926178016c14f3d6edb709ba03319d0b286d",
        )
        env_from = [
            ref["configMapRef"]["name"]
            for ref in container["envFrom"]
            if "configMapRef" in ref
        ]
        self.assertIn("cdc-quadringent-site", env_from)
        self.assertIn("cdc-quadringent-tuning", env_from)
        env_names = {entry["name"] for entry in container.get("env", [])}
        self.assertNotIn("ISERIES_PASSWORD", env_names)
        for entry in container.get("env", []):
            self.assertNotIn("secretKeyRef", entry.get("valueFrom", {}))

    def test_image_override_is_used(self) -> None:
        digest = "sha256:" + "b" * 64
        result = self.enabled(
            "--set", "preflight.image.repository=ghcr.io/quadringent/quadringent",
            "--set", f"preflight.image.digest={digest}",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        container = self.job(result)["spec"]["template"]["spec"]["containers"][0]
        self.assertTrue(container["image"].endswith("@" + digest))

    def test_mutable_or_missing_digest_is_refused(self) -> None:
        for digest in ("latest", "", "sha256:short"):
            with self.subTest(digest=digest):
                result = self.enabled(
                    "--set-string", "preflight.image.digest=" + digest,
                    "--set-string", "controlPlane.image.digest=",
                )
                self.assertNotEqual(result.returncode, 0)
                # Un digest vide est refusé par la garde du template (message
                # explicite) ; un digest malformé est désormais refusé plus tôt
                # par values.schema.json (erreur JSON Schema sur le chemin).
                self.assertTrue(
                    "preflight.image.digest" in result.stderr
                    or "/preflight/image/digest" in result.stderr,
                    result.stderr,
                )

    def test_service_account_override(self) -> None:
        result = self.enabled(
            "--set", "preflight.serviceAccountName=quadringent-control-plane"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.job(result)["spec"]["template"]["spec"]
        self.assertEqual(spec["serviceAccountName"], "quadringent-control-plane")

    def test_snowflake_oidc_wires_the_token(self) -> None:
        result = self.enabled("--set", "preflight.snowflakeOidc=true")

        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.job(result)["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertIn("--snowflake-oidc-token-file", container["args"])
        self.assertIn("/var/run/secrets/snowflake/token", container["args"])
        mounts = {mount["mountPath"] for mount in container["volumeMounts"]}
        self.assertIn("/var/run/secrets/snowflake", mounts)
        audiences = [
            source["serviceAccountToken"]["audience"]
            for volume in spec["volumes"]
            for source in volume.get("projected", {}).get("sources", [])
            if "serviceAccountToken" in source
        ]
        self.assertIn("snowflakecomputing.com", audiences)

    def tuning_configmap(self, result) -> dict:
        return next(
            document["data"]
            for document in self.documents(result)
            if document.get("kind") == "ConfigMap"
            and document["metadata"]["name"].endswith("-tuning")
        )

    def test_ibmi_port_overrides_reach_the_shared_tuning_configmap(self) -> None:
        result = self.enabled(
            "--set", "as400.databasePort=446",
            "--set-string", "as400.signonPort=449",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.tuning_configmap(result)
        self.assertEqual(data["AS400_DATABASE_PORT"], "446")
        self.assertEqual(data["AS400_SIGNON_PORT"], "449")
        self.assertNotIn("AS400_COMMAND_PORT", data)

    def test_invalid_ibmi_port_override_is_refused(self) -> None:
        for value in ("99999", "abc", "-1"):
            with self.subTest(value=value):
                result = self.enabled(
                    "--set-string", f"as400.databasePort={value}"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("as400.databasePort", result.stderr)

    def test_no_oidc_token_by_default(self) -> None:
        result = self.enabled()

        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.job(result)["spec"]["template"]["spec"]
        container = spec["containers"][0]
        self.assertNotIn("args", container)
        for volume in spec["volumes"]:
            self.assertNotIn("projected", volume)


if __name__ == "__main__":
    unittest.main()
