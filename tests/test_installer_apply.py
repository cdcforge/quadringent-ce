"""Exécution réelle de l'installation (apply.py) : copie du module dans
l'espace de travail privé du site (gap (1)), résumé de plan + confirmation +
refus de destruction (gap (5)), lecture des sorties Terraform réelles
(gap (3)). Entièrement hors ligne : RecordingRunner script chaque réponse,
aucune commande externe n'est jamais exécutée."""

from __future__ import annotations

import json
import io
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

import yaml

from quadringent.installer.apply import (
    TerraformApplyRefused,
    _control_plane_identity,
    _summarize_plan,
    apply_terraform_module,
    execute_install,
)
from quadringent.installer.manifest import ReleaseManifest
from quadringent.installer.plan import InstallInputs, _yaml_dump
from quadringent.installer.runner import CommandResult, RecordingRunner

MANIFEST = ReleaseManifest(
    repository="ghcr.io/quadringent/quadringent",
    image_digest="sha256:" + "0" * 64,
    control_plane_image_digest="sha256:" + "1" * 64,
    verifier_image_digest="sha256:" + "3" * 64,
    observability_image_digest="sha256:" + "2" * 64,
)


def _plan_show_result(*, creates=1, updates=0, deletes=0) -> CommandResult:
    resource_changes = []
    for _ in range(creates):
        resource_changes.append({"change": {"actions": ["create"]}})
    for _ in range(updates):
        resource_changes.append({"change": {"actions": ["update"]}})
    for _ in range(deletes):
        resource_changes.append({"change": {"actions": ["delete"]}})
    payload = json.dumps({"resource_changes": resource_changes})
    return CommandResult(("terraform", "show", "-json", "tfplan"), 0, payload, "")


def _outputs_result(values: dict) -> CommandResult:
    payload = json.dumps({key: {"value": value} for key, value in values.items()})
    return CommandResult(("terraform", "output", "-json"), 0, payload, "")


class SummarizePlanTests(unittest.TestCase):
    def test_generated_values_quote_numeric_aws_account_id(self) -> None:
        rendered = _yaml_dump({"site": {"awsAccountId": "000000000001"}})
        self.assertIn('awsAccountId: "000000000001"', rendered)
        self.assertEqual(yaml.safe_load(rendered)["site"]["awsAccountId"], "000000000001")

    def test_counts_creates_updates_deletes(self) -> None:
        payload = json.dumps(
            {
                "resource_changes": [
                    {"change": {"actions": ["create"]}},
                    {"change": {"actions": ["create"]}},
                    {"change": {"actions": ["update"]}},
                    {"change": {"actions": ["delete", "create"]}},
                ]
            }
        )
        self.assertEqual(_summarize_plan(payload), (3, 1, 1))

    def test_empty_or_invalid_json_is_zero(self) -> None:
        self.assertEqual(_summarize_plan(""), (0, 0, 0))
        self.assertEqual(_summarize_plan("not json"), (0, 0, 0))


class ApplyTerraformModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source_dir = Path(self.tmp.name) / "source-module"
        self.source_dir.mkdir()
        (self.source_dir / "main.tf").write_text("# terraform module\n")
        self.module_workdir = Path(self.tmp.name) / "workdir" / "terraform" / "base"

    def _runner(self, *, plan_result=None, outputs=None) -> RecordingRunner:
        scripted = {}
        if plan_result is not None:
            scripted[("terraform", "show", "-json", "tfplan")] = plan_result
        if outputs is not None:
            scripted[("terraform", "output", "-json")] = _outputs_result(outputs)
        return RecordingRunner(scripted_results=scripted)

    def test_copies_module_into_private_workdir_never_the_repo_tree(self) -> None:
        runner = self._runner(plan_result=_plan_show_result(), outputs={"runtime_role_arn": "arn:aws:iam::123:role/x"})
        apply_terraform_module(
            module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
            tfvars={"name": "demo"}, runner=runner, stdout=Path("/dev/null").open("w"),
            yes=True,
        )
        self.assertTrue((self.module_workdir / "main.tf").exists())
        self.assertFalse((self.source_dir / "site.auto.tfvars.json").exists())
        tfvars_content = json.loads((self.module_workdir / "site.auto.tfvars.json").read_text())
        self.assertEqual(tfvars_content, {"name": "demo"})

    def test_returns_parsed_outputs(self) -> None:
        runner = self._runner(
            plan_result=_plan_show_result(),
            outputs={"runtime_role_arn": "arn:aws:iam::123:role/x", "runtime_instance_profile_name": "demo"},
        )
        outputs = apply_terraform_module(
            module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
            tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=True,
        )
        self.assertEqual(outputs["runtime_role_arn"], "arn:aws:iam::123:role/x")
        self.assertEqual(outputs["runtime_instance_profile_name"], "demo")

    def test_refuses_destruction_without_allow_destroy(self) -> None:
        runner = self._runner(plan_result=_plan_show_result(deletes=1))
        with self.assertRaises(TerraformApplyRefused):
            apply_terraform_module(
                module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
                tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=True, allow_destroy=False,
            )
        # jamais d'apply exécuté après un refus.
        applied = [c for c in runner.invocations if c[0][:2] == ("terraform", "apply")]
        self.assertEqual(applied, [])

    def test_destruction_allowed_with_allow_destroy(self) -> None:
        runner = self._runner(plan_result=_plan_show_result(deletes=1), outputs={"runtime_role_arn": "x"})
        apply_terraform_module(
            module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
            tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=True, allow_destroy=True,
        )
        applied = [c for c in runner.invocations if c[0][:2] == ("terraform", "apply")]
        self.assertEqual(len(applied), 1)

    def test_confirmation_required_without_yes(self) -> None:
        runner = self._runner(plan_result=_plan_show_result())
        with self.assertRaises(TerraformApplyRefused):
            apply_terraform_module(
                module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
                tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=False,
                confirm=lambda prompt: False,
            )
        applied = [c for c in runner.invocations if c[0][:2] == ("terraform", "apply")]
        self.assertEqual(applied, [])

    def test_confirmation_accepted_proceeds(self) -> None:
        runner = self._runner(plan_result=_plan_show_result(), outputs={"runtime_role_arn": "x"})
        apply_terraform_module(
            module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
            tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=False,
            confirm=lambda prompt: True,
        )
        applied = [c for c in runner.invocations if c[0][:2] == ("terraform", "apply")]
        self.assertEqual(len(applied), 1)

    def test_yes_skips_confirmation_entirely(self) -> None:
        called = []
        runner = self._runner(plan_result=_plan_show_result(), outputs={"runtime_role_arn": "x"})
        apply_terraform_module(
            module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
            tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=True,
            confirm=lambda prompt: called.append(prompt) or True,
        )
        self.assertEqual(called, [])

    def test_uses_env_for_every_command(self) -> None:
        runner = self._runner(plan_result=_plan_show_result(), outputs={"runtime_role_arn": "x"})
        apply_terraform_module(
            module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
            tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=True,
            env={"AWS_PROFILE": "demo-int"},
        )
        terraform_calls = [c for c in runner.invocations if c[0][0] == "terraform"]
        self.assertTrue(terraform_calls)
        for _, _, env in terraform_calls:
            self.assertEqual(env, {"AWS_PROFILE": "demo-int"})

    def test_failed_init_raises_without_planning(self) -> None:
        runner = RecordingRunner(default_returncode=1)
        with self.assertRaises(TerraformApplyRefused):
            apply_terraform_module(
                module_label="du socle", source_dir=str(self.source_dir), module_workdir=self.module_workdir,
                tfvars={}, runner=runner, stdout=Path("/dev/null").open("w"), yes=True,
            )
        planned = [c for c in runner.invocations if c[0][:2] == ("terraform", "plan")]
        self.assertEqual(planned, [])


class ControlPlaneIdentityTests(unittest.TestCase):
    def test_aws_cluster_uses_addon_role_arn(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        role_arn, gcp_sa = _control_plane_identity(
            inputs, base_outputs={"runtime_role_arn": "arn:base"}, addon_outputs={"role_arn": "arn:addon"}
        )
        self.assertEqual(role_arn, "arn:addon")
        self.assertIsNone(gcp_sa)

    def test_aws_vm_falls_back_to_base_role_arn(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        role_arn, gcp_sa = _control_plane_identity(inputs, base_outputs={"runtime_role_arn": "arn:base"}, addon_outputs={})
        self.assertEqual(role_arn, "arn:base")

    def test_aws_missing_identity_raises(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        with self.assertRaises(TerraformApplyRefused):
            _control_plane_identity(inputs, base_outputs={}, addon_outputs={})

    def test_gcp_cluster_uses_addon_service_account(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project="p")
        role_arn, gcp_sa = _control_plane_identity(
            inputs, base_outputs={"service_account_email": "base@p.iam.gserviceaccount.com"},
            addon_outputs={"service_account_email": "addon@p.iam.gserviceaccount.com"},
        )
        self.assertEqual(gcp_sa, "addon@p.iam.gserviceaccount.com")
        self.assertEqual(role_arn, "")

    def test_gcp_missing_identity_raises(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project="p")
        with self.assertRaises(TerraformApplyRefused):
            _control_plane_identity(inputs, base_outputs={}, addon_outputs={})


class ExecuteInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        self.stdout = Path(self.tmp.name, "out.log").open("w")

    def test_aws_cluster_end_to_end_resolves_identity_and_installs(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="cluster", region="eu-west-3", name="demo-int",
            eks_oidc_provider_arn="arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
            eks_oidc_provider_url="oidc.eks.eu-west-3.amazonaws.com/id/ABC",
        )
        runner = RecordingRunner(
            scripted_results={
                ("terraform", "show", "-json", "tfplan"): _plan_show_result(),
                ("terraform", "output", "-json"): _outputs_result({
                    "role_arn": "arn:aws:iam::123:role/irsa", "account_id": "000000000000",
                }),
            }
        )
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True)
        self.assertEqual(code, 0)
        values = yaml.safe_load((self.workdir / "chart-values.generated.yaml").read_text())
        self.assertEqual(values["controlPlane"]["serviceAccount"]["roleArn"], "arn:aws:iam::123:role/irsa")
        self.assertEqual(values["site"]["awsAccountId"], "000000000000")
        addon_tfvars = json.loads((self.workdir / "terraform/addon/site.auto.tfvars.json").read_text())
        self.assertEqual(addon_tfvars["oidc_provider_url"], inputs.eks_oidc_provider_url)
        self.assertEqual(addon_tfvars["oidc_provider_arn"], inputs.eks_oidc_provider_arn)
        for argv, _, env in runner.invocations:
            if argv[0] in ("terraform", "kubectl"):
                self.assertEqual(env["AWS_REGION"], "eu-west-3")
                self.assertEqual(env["AWS_DEFAULT_REGION"], "eu-west-3")

    def test_aws_cluster_missing_oidc_fails_before_any_terraform(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        runner = RecordingRunner()
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True)
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])

    def test_aws_cluster_oidc_account_must_match_the_real_base_account(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="cluster", region="eu-west-3", name="demo-int",
            eks_oidc_provider_arn="arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
            eks_oidc_provider_url="oidc.eks.eu-west-3.amazonaws.com/id/ABC",
        )
        runner = RecordingRunner(scripted_results={
            ("terraform", "show", "-json", "tfplan"): _plan_show_result(),
            ("terraform", "output", "-json"): _outputs_result({"account_id": "000000000001"}),
        })
        self.assertEqual(execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True), 1)
        self.assertFalse((self.workdir / "terraform/addon").exists())

    def test_aws_vm_missing_network_fails_before_any_terraform(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        runner = RecordingRunner()
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True)
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])

    def test_gcp_vm_missing_network_fails_before_any_terraform(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo-int", project="example-gcp-project")
        runner = RecordingRunner()
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True)
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])

    def test_gcp_vm_uses_private_iap_kubeconfig_and_vm_identity(self) -> None:
        inputs = InstallInputs(
            cloud="gcp", target="vm", region="europe-west1", name="demo-int", project="example-gcp-project",
            gcp_network="test-vpc", gcp_subnetwork="test-subnet", image_pull_secret="gar-qual",
        )
        runner = RecordingRunner(scripted_results={
            ("terraform", "show", "-json", "tfplan"): _plan_show_result(),
            ("terraform", "output", "-json"): _outputs_result({
                "service_account_email": "demo-int-quadringent-runtime@example-gcp-project.iam.gserviceaccount.com",
                "instance_name": "demo-int-quadringent-vm", "zone": "europe-west1-b",
            }),
        })
        connected = []

        @contextmanager
        def fake_gcp_connector(instance_name, zone, project, workdir, received_runner, env):
            connected.append((instance_name, zone, project))
            self.assertIs(received_runner, runner)
            yield {**env, "KUBECONFIG": str(workdir / "k3s-kubeconfig.yaml")}

        output = io.StringIO()
        code = execute_install(
            inputs, MANIFEST, self.workdir, runner, output,
            yes=True, gcp_vm_connector=fake_gcp_connector,
        )
        self.assertEqual(code, 0)
        self.assertEqual(connected, [("demo-int-quadringent-vm", "europe-west1-b", "example-gcp-project")])
        tfvars = json.loads((self.workdir / "terraform/vm/site.auto.tfvars.json").read_text())
        self.assertEqual(tfvars["network"], "test-vpc")
        self.assertEqual(tfvars["subnetwork"], "test-subnet")
        values = yaml.safe_load((self.workdir / "chart-values.generated.yaml").read_text())
        self.assertEqual(values["gcpIdentityMode"], "vm-metadata")
        self.assertTrue(values["controlPlane"]["v2"]["enabled"])
        self.assertIn("quadringent vm-tunnel", output.getvalue())
        for argv, _, env in runner.invocations:
            if argv[0] in ("helm", "kubectl"):
                self.assertEqual(env["KUBECONFIG"], str(self.workdir / "k3s-kubeconfig.yaml"))

    def test_aws_vm_uses_private_ssm_kubeconfig_for_chart_and_rollout(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="vm", region="eu-west-3", name="demo-int",
            vpc_id="vpc-abc123", subnet_id="subnet-abc123",
            vm_instance_type="m7i-flex.large", image_pull_secret="ecr-qual",
        )
        runner = RecordingRunner(scripted_results={
            ("terraform", "show", "-json", "tfplan"): _plan_show_result(),
            ("terraform", "output", "-json"): _outputs_result({
                "account_id": "000000000001",
                "runtime_role_arn": "arn:aws:iam::000000000001:role/demo-int",
                "runtime_role_name": "demo-int",
                "runtime_instance_profile_name": "demo-int",
                "instance_id": "i-0123456789abcdef0",
            }),
        })
        connected = []

        @contextmanager
        def fake_vm_connector(instance_id, workdir, received_runner, env):
            connected.append((instance_id, workdir))
            self.assertIs(received_runner, runner)
            yield {**env, "KUBECONFIG": str(workdir / "k3s-kubeconfig.yaml")}

        output = io.StringIO()
        code = execute_install(
            inputs, MANIFEST, self.workdir, runner, output,
            yes=True, vm_connector=fake_vm_connector,
        )
        self.assertEqual(code, 0)
        self.assertEqual(connected[0][0], "i-0123456789abcdef0")
        vm_tfvars = json.loads((self.workdir / "terraform/vm/site.auto.tfvars.json").read_text())
        self.assertEqual(vm_tfvars["instance_type"], "m7i-flex.large")
        self.assertEqual(vm_tfvars["architecture"], "x86_64")
        self.assertEqual(vm_tfvars["instance_role_name"], "demo-int")
        values = yaml.safe_load((self.workdir / "chart-values.generated.yaml").read_text())
        self.assertEqual(values["image"]["pullSecret"], "ecr-qual")
        self.assertIn(f"quadringent vm-tunnel --name demo-int --workdir {self.workdir}", output.getvalue())
        for argv, _, env in runner.invocations:
            if argv[0] in ("helm", "kubectl"):
                self.assertEqual(env["KUBECONFIG"], str(self.workdir / "k3s-kubeconfig.yaml"))

    def test_missing_identity_fails_the_whole_install(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="cluster", region="eu-west-3", name="demo-int",
            eks_oidc_provider_arn="arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
            eks_oidc_provider_url="oidc.eks.eu-west-3.amazonaws.com/id/ABC",
        )
        runner = RecordingRunner(
            scripted_results={
                ("terraform", "show", "-json", "tfplan"): _plan_show_result(),
                ("terraform", "output", "-json"): _outputs_result({"account_id": "000000000000"}),
            }
        )
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True)
        self.assertEqual(code, 1)
        self.assertFalse((self.workdir / "chart-values.generated.yaml").exists())

    def test_destructive_plan_without_allow_destroy_stops_install(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="vm", region="eu-west-3", name="demo-int",
            vpc_id="vpc-abc123", subnet_id="subnet-abc123",
        )
        runner = RecordingRunner(
            scripted_results={("terraform", "show", "-json", "tfplan"): _plan_show_result(deletes=1)}
        )
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True, allow_destroy=False)
        self.assertEqual(code, 1)


    def test_control_plane_rollout_timeout_fails_the_install(self) -> None:
        """Constaté sur GKE le 23 septembre 2026 : ``rollout status`` expirait
        (postgres refusé par ``runAsNonRoot``) et l'installeur sortait en 0."""
        inputs = InstallInputs(
            cloud="aws", target="cluster", region="eu-west-3", name="demo-int",
            eks_oidc_provider_arn="arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
            eks_oidc_provider_url="oidc.eks.eu-west-3.amazonaws.com/id/ABC",
        )
        rollout = (
            "kubectl", "-n", inputs.namespace, "rollout", "status",
            "deployment/demo-int-quadringent-control-plane", "--timeout=180s",
        )
        runner = RecordingRunner(
            scripted_results={
                ("terraform", "show", "-json", "tfplan"): _plan_show_result(),
                ("terraform", "output", "-json"): _outputs_result({
                    "role_arn": "arn:aws:iam::123:role/irsa", "account_id": "000000000000",
                }),
                rollout: CommandResult(rollout, 1, "", "error: timed out waiting for the condition"),
            }
        )
        code = execute_install(inputs, MANIFEST, self.workdir, runner, self.stdout, yes=True)
        self.assertEqual(code, 1)
        self.stdout.flush()
        output = Path(self.tmp.name, "out.log").read_text()
        self.assertIn("timed out waiting for the condition", output)
        self.assertNotIn("Lien d'activation admin", output)


if __name__ == "__main__":
    unittest.main()
