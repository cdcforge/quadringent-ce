"""Pré-vol : présence des outils, détection des identifiants (sans valeurs),
joignabilité IBM i optionnelle (sonde injectable)."""

from __future__ import annotations

import unittest

from quadringent.installer.plan import InstallInputs
from quadringent.installer.preflight import (
    check_cloud_credentials,
    check_ibmi_reachability,
    check_tools,
    run_preflight,
)
from quadringent.installer.runner import RecordingRunner
from quadringent.installer.runner import CommandResult


class ToolChecksTests(unittest.TestCase):
    def test_all_tools_present(self) -> None:
        runner = RecordingRunner(available_tools=frozenset({"terraform", "helm", "kubectl"}))
        results = check_tools(runner)
        self.assertTrue(all(result.ok for result in results))

    def test_missing_tool_reported(self) -> None:
        runner = RecordingRunner(available_tools=frozenset({"terraform"}))
        results = check_tools(runner)
        by_name = {result.name: result for result in results}
        self.assertFalse(by_name["outil helm"].ok)
        self.assertIn("introuvable", by_name["outil helm"].detail)


class CredentialChecksTests(unittest.TestCase):
    def test_aws_credentials_detected_without_leaking_value(self) -> None:
        result = check_cloud_credentials("aws", {"AWS_PROFILE": "super-secret-profile"})
        self.assertTrue(result.ok)
        self.assertNotIn("super-secret-profile", result.detail)
        self.assertIn("AWS_PROFILE", result.detail)

    def test_aws_credentials_missing(self) -> None:
        result = check_cloud_credentials("aws", {})
        self.assertFalse(result.ok)

    def test_gcp_credentials_detected(self) -> None:
        result = check_cloud_credentials("gcp", {"GOOGLE_APPLICATION_CREDENTIALS": "/path/to/key.json"})
        self.assertTrue(result.ok)


class IbmiReachabilityTests(unittest.TestCase):
    def test_all_ports_reachable(self) -> None:
        def fake_socket(address, timeout):
            class _Sock:
                def close(self):
                    pass

            return _Sock()

        results = check_ibmi_reachability("ibmi.example.com", socket_factory=fake_socket)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.ok for result in results))

    def test_unreachable_port_reported(self) -> None:
        def fake_socket(address, timeout):
            raise OSError("connection refused")

        results = check_ibmi_reachability("ibmi.example.com", socket_factory=fake_socket)
        self.assertTrue(all(not result.ok for result in results))


class RunPreflightTests(unittest.TestCase):
    def test_gcp_cli_and_adc_are_detected_without_exposing_tokens(self) -> None:
        cli_argv = ("gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
        adc_argv = ("gcloud", "auth", "application-default", "print-access-token")
        runner = RecordingRunner(
            available_tools=frozenset({"terraform", "helm", "kubectl", "gcloud"}),
            scripted_results={
                cli_argv: CommandResult(cli_argv, 0, "user@example.test\n"),
                adc_argv: CommandResult(adc_argv, 0, "opaque-test-token\n"),
            },
        )
        inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo", project="example-gcp-project")
        results = run_preflight(inputs, runner, environ={})
        self.assertTrue(all(item.ok for item in results))
        self.assertNotIn("opaque-test-token", repr(results))
        self.assertNotIn("user@example.test", repr(results))

    def test_gcp_vm_requires_cli_login_even_with_key_file_environment(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo", project="example-gcp-project")
        runner = RecordingRunner(available_tools=frozenset({"terraform", "helm", "kubectl", "gcloud"}))
        results = run_preflight(inputs, runner, environ={"GOOGLE_APPLICATION_CREDENTIALS": "/path/to/key.json"})
        credentials = next(item for item in results if item.name == "identifiants cloud")
        self.assertFalse(credentials.ok)
        self.assertNotIn("/path/to/key.json", credentials.detail)

    def test_aggregates_tool_and_credential_checks(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo")
        runner = RecordingRunner()
        results = run_preflight(inputs, runner, environ={"AWS_PROFILE": "x"})
        names = [result.name for result in results]
        self.assertIn("outil terraform", names)
        self.assertIn("identifiants cloud", names)

    def test_includes_ibmi_probe_when_requested(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo")
        runner = RecordingRunner()

        def fake_socket(address, timeout):
            raise OSError("unreachable")

        import quadringent.installer.preflight as preflight_module

        original = preflight_module.check_ibmi_reachability
        preflight_module.check_ibmi_reachability = lambda host, **_: original(host, socket_factory=fake_socket)
        try:
            results = run_preflight(inputs, runner, check_ibmi_host="ibmi.example.com", environ={"AWS_PROFILE": "x"})
        finally:
            preflight_module.check_ibmi_reachability = original
        self.assertTrue(any("IBM i" in result.name for result in results))


if __name__ == "__main__":
    unittest.main()
