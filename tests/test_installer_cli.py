"""CLI `quadringent install|uninstall|status` : dry-run, exécution via un
runner injecté (hors ligne, aucune commande externe réelle), et gestion des
erreurs de pré-vol."""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from quadringent.installer.cli import run
from quadringent.installer.runner import CommandResult, RecordingRunner

MANIFEST_ARG = "deploy/release-manifest.example.json"


class DryRunTests(unittest.TestCase):
    def _run(self, argv):
        runner = RecordingRunner()
        out = io.StringIO()
        code = run(argv, runner=runner, stdout=out)
        return code, out.getvalue(), runner

    def test_dry_run_prints_plan_and_executes_nothing(self) -> None:
        code, output, runner = self._run(
            ["install", "--cloud", "aws", "--target", "vm", "--region", "eu-west-3", "--name", "demo-int", "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Installation Quadringent", output)
        self.assertIn("terraform", output)
        self.assertIn("helm", output)
        self.assertIn("--dry-run : aucune commande exécutée", output)
        self.assertEqual(runner.calls, [])

    def test_dry_run_gcp_cluster(self) -> None:
        code, output, runner = self._run(
            [
                "install", "--cloud", "gcp", "--target", "cluster", "--region", "europe-west1", "--name", "demo-int",
                "--project", "example-gcp-project", "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("gke-addon", output)
        self.assertEqual(runner.calls, [])

    def test_release_assets_are_resolved_outside_the_source_checkout(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as empty, patch.object(os, "getcwd", return_value=empty):
            code, output, runner = self._run(
                [
                    "install", "--cloud", "gcp", "--target", "cluster", "--region", "europe-west1",
                    "--name", "demo-int", "--project", "example-gcp-project", "--dry-run",
                    "--assets-dir", str(source_root),
                    "--release-manifest", str(source_root / MANIFEST_ARG),
                ]
            )
        self.assertEqual(code, 0, output)
        self.assertIn(str(source_root / "deploy/terraform/gcp/base"), output)
        self.assertIn(str(source_root / "chart"), output)
        self.assertEqual(runner.calls, [])

    def test_missing_release_assets_fail_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            code, output, runner = self._run(
                [
                    "install", "--cloud", "gcp", "--target", "cluster", "--region", "europe-west1",
                    "--name", "demo-int", "--project", "example-gcp-project", "--dry-run",
                    "--assets-dir", empty,
                    "--release-manifest", str(Path(__file__).resolve().parents[1] / MANIFEST_ARG),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("Artefacts d'installation introuvables", output)
        self.assertEqual(runner.calls, [])

    def test_dry_run_gcp_without_project_is_rejected(self) -> None:
        code, output, _ = self._run(
            ["install", "--cloud", "gcp", "--target", "cluster", "--region", "europe-west1", "--name", "demo-int", "--dry-run"]
        )
        self.assertEqual(code, 2)
        self.assertIn("Entrées invalides", output)
        self.assertIn("--project", output)

    def test_dry_run_existing_bucket_and_checkpoint_table(self) -> None:
        code, output, runner = self._run(
            [
                "install", "--cloud", "aws", "--target", "cluster", "--region", "eu-west-3", "--name", "demo-int",
                "--existing-bucket", "already-there-bucket",
                "--existing-checkpoint-table", "already-there-table",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("already-there-bucket", output)
        self.assertIn("already-there-table", output)
        self.assertEqual(runner.calls, [])

    def test_existing_checkpoint_table_rejected_on_gcp(self) -> None:
        code, output, _ = self._run(
            [
                "install", "--cloud", "gcp", "--target", "vm", "--region", "europe-west1", "--name", "demo-int",
                "--existing-checkpoint-table", "already-there-table",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("Entrées invalides", output)

    def test_dry_run_invalid_inputs_reports_error(self) -> None:
        code, output, _ = self._run(
            ["install", "--cloud", "aws", "--target", "vm", "--region", "eu-west-3", "--name", "Bad_Name", "--dry-run"]
        )
        self.assertEqual(code, 2)
        self.assertIn("Entrées invalides", output)

    def test_dry_run_invalid_manifest(self) -> None:
        code, output, _ = self._run(
            [
                "install", "--cloud", "aws", "--target", "vm", "--region", "eu-west-3", "--name", "demo",
                "--release-manifest", "deploy/does-not-exist.json", "--dry-run",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("Manifeste de version invalide", output)

    def test_uninstall_dry_run_warns_about_terraform_resources(self) -> None:
        code, output, runner = self._run(["uninstall", "--name", "demo-int", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("helm uninstall", output)
        self.assertIn("ne sont pas", output)
        self.assertEqual(runner.calls, [])

    def test_uninstall_refuses_unknown_site_before_touching_helm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = RecordingRunner()
            out = io.StringIO()
            code = run(["uninstall", "--name", "demo-int", "--workdir", tmp], runner=runner, stdout=out)
            self.assertEqual(code, 2)
            self.assertNotIn("helm", [call.argv[0] for call in runner.calls])


class ExecutedInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="quadringent-install-test-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def test_successful_install_writes_state_and_prints_activation_message(self) -> None:
        # Sorties Terraform réelles scriptées (gap (3)) : sans elles,
        # l'installateur refuse désormais de publier une identité fictive.
        outputs = json.dumps({
            "runtime_role_arn": {"value": "arn:aws:iam::000000000000:role/demo-int-quadringent-runtime"},
            "runtime_instance_profile_name": {"value": "demo-int-quadringent-runtime"},
            "role_arn": {"value": "arn:aws:iam::000000000000:role/demo-int-quadringent-irsa"},
            "account_id": {"value": "000000000000"},
        })
        runner = RecordingRunner(
            available_tools=frozenset({"terraform", "helm", "kubectl"}),
            scripted_results={("terraform", "output", "-json"): CommandResult(("terraform", "output", "-json"), 0, outputs, "")},
        )
        out = io.StringIO()
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as empty, patch.object(os, "getcwd", return_value=empty):
            code = run(
                [
                    "install", "--cloud", "aws", "--target", "cluster", "--region", "eu-west-3", "--name", "demo-int",
                    "--eks-oidc-provider-arn", "arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
                    "--eks-oidc-provider-url", "oidc.eks.eu-west-3.amazonaws.com/id/ABC",
                    "--workdir", str(self.workdir), "--release-manifest", str(source_root / MANIFEST_ARG),
                    "--assets-dir", str(source_root), "--skip-preflight", "--yes",
                ],
                runner=runner,
                stdout=out,
            )
        self.assertEqual(code, 0, out.getvalue())
        output = out.getvalue()
        self.assertIn("Lien d'activation admin", output)
        state = json.loads((self.workdir / "install-state.json").read_text())
        self.assertEqual(state["status"], "installed")
        self.assertTrue((self.workdir / "chart-values.generated.yaml").exists())
        self.assertTrue((self.workdir / "terraform/base/main.tf").exists())
        self.assertIn(
            str(source_root / "chart"),
            [call.argv[4] for call in runner.calls if call.argv[:3] == ("helm", "upgrade", "--install")],
        )
        # terraform + helm commands were actually invoked (recorded, not run for real).
        commands = [call.argv[0] for call in runner.calls]
        self.assertIn("terraform", commands)
        self.assertIn("helm", commands)

    def test_preflight_failure_stops_before_any_command(self) -> None:
        runner = RecordingRunner(available_tools=frozenset())  # aucun outil présent
        out = io.StringIO()
        code = run(
            [
                "install", "--cloud", "aws", "--target", "vm", "--region", "eu-west-3", "--name", "demo-int",
                "--workdir", str(self.workdir), "--release-manifest", MANIFEST_ARG,
            ],
            runner=runner,
            stdout=out,
        )
        self.assertEqual(code, 1)
        self.assertIn("Pré-vol échoué", out.getvalue())
        self.assertEqual(runner.calls, [])

    def test_command_failure_stops_the_plan_and_reports(self) -> None:
        runner = RecordingRunner(available_tools=frozenset({"terraform", "helm", "kubectl"}), default_returncode=1)
        out = io.StringIO()
        code = run(
            [
                "install", "--cloud", "aws", "--target", "vm", "--region", "eu-west-3", "--name", "demo-int",
                "--vpc-id", "vpc-abc123", "--subnet-id", "subnet-abc123",
                "--workdir", str(self.workdir), "--release-manifest", MANIFEST_ARG, "--skip-preflight", "--yes",
            ],
            runner=runner,
            stdout=out,
        )
        self.assertEqual(code, 1)
        self.assertIn("Échec", out.getvalue())
        state = json.loads((self.workdir / "install-state.json").read_text())
        self.assertEqual(state["status"], "failed")

    def test_status_reports_last_known_state(self) -> None:
        outputs = json.dumps({
            "runtime_role_arn": {"value": "arn:aws:iam::000000000000:role/demo-int-quadringent-runtime"},
            "runtime_instance_profile_name": {"value": "demo-int-quadringent-runtime"},
            "role_arn": {"value": "arn:aws:iam::000000000000:role/demo-int-quadringent-irsa"},
            "account_id": {"value": "000000000000"},
        })
        runner = RecordingRunner(
            available_tools=frozenset({"terraform", "helm", "kubectl"}),
            scripted_results={("terraform", "output", "-json"): CommandResult(("terraform", "output", "-json"), 0, outputs, "")},
        )
        out = io.StringIO()
        run(
            [
                "install", "--cloud", "aws", "--target", "cluster", "--region", "eu-west-3", "--name", "demo-int",
                "--eks-oidc-provider-arn", "arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
                "--eks-oidc-provider-url", "oidc.eks.eu-west-3.amazonaws.com/id/ABC",
                "--workdir", str(self.workdir), "--release-manifest", MANIFEST_ARG, "--skip-preflight", "--yes",
            ],
            runner=runner,
            stdout=out,
        )
        status_out = io.StringIO()
        code = run(["status", "--name", "demo-int", "--workdir", str(self.workdir)], runner=runner, stdout=status_out)
        self.assertEqual(code, 0)
        self.assertIn('"status": "installed"', status_out.getvalue())

    def test_status_without_prior_install_reports_unknown(self) -> None:
        out = io.StringIO()
        code = run(["status", "--name", "never-installed", "--workdir", str(self.workdir / "other")], runner=RecordingRunner(), stdout=out)
        self.assertEqual(code, 1)
        self.assertIn("Aucun état connu", out.getvalue())

    def test_vm_uninstall_uses_ssm_kubeconfig_and_preserves_site_identity(self) -> None:
        state = {
            "cloud": "aws", "target": "vm", "region": "eu-west-3", "name": "demo-int",
            "namespace": "quadringent", "status": "installed", "aws_profile": "aws-test",
        }
        (self.workdir / "install-state.json").write_text(json.dumps(state))
        outputs = json.dumps({"instance_id": {"value": "i-0123456789abcdef0"}})
        runner = RecordingRunner(scripted_results={
            ("terraform", "output", "-json"): CommandResult(("terraform", "output", "-json"), 0, outputs),
        })

        @contextmanager
        def fake_vm_connector(instance_id, workdir, received_runner, env):
            self.assertEqual(instance_id, "i-0123456789abcdef0")
            self.assertEqual(env["AWS_PROFILE"], "aws-test")
            yield {**env, "KUBECONFIG": str(workdir / "k3s-kubeconfig.yaml")}

        with patch("quadringent.installer.cli.connect_aws_vm", fake_vm_connector):
            code = run(["uninstall", "--name", "demo-int", "--workdir", str(self.workdir)], runner=runner, stdout=io.StringIO())
        self.assertEqual(code, 0)
        helm = [entry for entry in runner.invocations if entry[0][:2] == ("helm", "uninstall")]
        self.assertEqual(len(helm), 1)
        self.assertEqual(helm[0][2]["KUBECONFIG"], str(self.workdir / "k3s-kubeconfig.yaml"))
        after = json.loads((self.workdir / "install-state.json").read_text())
        self.assertEqual(after["status"], "uninstalled")
        self.assertEqual(after["aws_profile"], "aws-test")

    def test_gcp_vm_cli_keeps_project_and_network_for_install(self) -> None:
        captured = []

        def fake_install(inputs, manifest, workdir, runner, stdout, **kwargs):
            captured.append(inputs)
            return 0

        with patch("quadringent.installer.cli.execute_install", side_effect=fake_install):
            code = run([
                "install", "--cloud", "gcp", "--target", "vm", "--region", "europe-west1",
                "--name", "demo-int", "--project", "example-gcp-project",
                "--gcp-network", "test-vpc", "--gcp-subnetwork", "test-subnet",
                "--gcp-zone", "europe-west1-c", "--workdir", str(self.workdir), "--skip-preflight",
            ], runner=RecordingRunner(), stdout=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(captured[0].gcp_network, "test-vpc")
        self.assertEqual(captured[0].gcp_subnetwork, "test-subnet")
        self.assertEqual(captured[0].gcp_zone, "europe-west1-c")
        state = json.loads((self.workdir / "install-state.json").read_text())
        self.assertEqual(state["project"], "example-gcp-project")

    def test_gcp_vm_uninstall_uses_iap_kubeconfig(self) -> None:
        state = {
            "cloud": "gcp", "target": "vm", "region": "europe-west1", "project": "example-gcp-project",
            "name": "demo-int", "namespace": "quadringent", "status": "installed",
        }
        (self.workdir / "install-state.json").write_text(json.dumps(state))
        outputs = json.dumps({
            "instance_name": {"value": "demo-int-quadringent-vm"},
            "zone": {"value": "europe-west1-b"},
        })
        runner = RecordingRunner(scripted_results={
            ("terraform", "output", "-json"): CommandResult(("terraform", "output", "-json"), 0, outputs),
        })

        @contextmanager
        def fake_gcp_connector(instance_name, zone, project, workdir, received_runner, env):
            self.assertEqual((instance_name, zone, project),
                             ("demo-int-quadringent-vm", "europe-west1-b", "example-gcp-project"))
            yield {**env, "KUBECONFIG": str(workdir / "k3s-kubeconfig.yaml")}

        with patch("quadringent.installer.cli.connect_gcp_vm", fake_gcp_connector):
            code = run(["uninstall", "--name", "demo-int", "--workdir", str(self.workdir)],
                       runner=runner, stdout=io.StringIO())
        self.assertEqual(code, 0)
        helm = [entry for entry in runner.invocations if entry[0][:2] == ("helm", "uninstall")]
        self.assertEqual(helm[0][2]["KUBECONFIG"], str(self.workdir / "k3s-kubeconfig.yaml"))

    def test_vm_tunnel_reports_kubectl_command_for_the_recorded_vm(self) -> None:
        state = {
            "cloud": "aws", "target": "vm", "region": "eu-west-3", "name": "demo-int",
            "namespace": "quadringent", "status": "installed", "aws_profile": "aws-test",
        }
        (self.workdir / "install-state.json").write_text(json.dumps(state))
        outputs = json.dumps({"instance_id": {"value": "i-0123456789abcdef0"}})
        runner = RecordingRunner(scripted_results={
            ("terraform", "output", "-json"): CommandResult(("terraform", "output", "-json"), 0, outputs),
        })

        @contextmanager
        def fake_vm_connector(instance_id, workdir, received_runner, env):
            self.assertEqual(instance_id, "i-0123456789abcdef0")
            yield {**env, "KUBECONFIG": str(workdir / "k3s-kubeconfig.yaml")}

        out = io.StringIO()
        with patch("quadringent.installer.cli.connect_aws_vm", fake_vm_connector), patch(
            "quadringent.installer.cli.time.sleep", side_effect=KeyboardInterrupt
        ):
            code = run(["vm-tunnel", "--name", "demo-int", "--workdir", str(self.workdir)], runner=runner, stdout=out)
        self.assertEqual(code, 0)
        self.assertIn("kubectl -n quadringent port-forward deployment/demo-int-quadringent-control-plane 8844:8844", out.getvalue())
        self.assertIn("Tunnel SSM fermé", out.getvalue())

    def test_gcp_vm_tunnel_reports_kubectl_command(self) -> None:
        state = {
            "cloud": "gcp", "target": "vm", "region": "europe-west1", "project": "example-gcp-project",
            "name": "demo-int", "namespace": "quadringent", "status": "installed",
        }
        (self.workdir / "install-state.json").write_text(json.dumps(state))
        outputs = json.dumps({
            "instance_name": {"value": "demo-int-quadringent-vm"},
            "zone": {"value": "europe-west1-b"},
        })
        runner = RecordingRunner(scripted_results={
            ("terraform", "output", "-json"): CommandResult(("terraform", "output", "-json"), 0, outputs),
        })

        @contextmanager
        def fake_gcp_connector(instance_name, zone, project, workdir, received_runner, env):
            yield {**env, "KUBECONFIG": str(workdir / "k3s-kubeconfig.yaml")}

        out = io.StringIO()
        with patch("quadringent.installer.cli.connect_gcp_vm", fake_gcp_connector), patch(
            "quadringent.installer.cli.time.sleep", side_effect=KeyboardInterrupt
        ):
            code = run(["vm-tunnel", "--name", "demo-int", "--workdir", str(self.workdir)],
                       runner=runner, stdout=out)
        self.assertEqual(code, 0)
        self.assertIn("kubectl -n quadringent port-forward deployment/demo-int-quadringent-control-plane 8844:8844", out.getvalue())
        self.assertIn("Tunnel IAP fermé", out.getvalue())


class DocSamplesMatchRealOutputTests(unittest.TestCase):
    """Les échantillons `--dry-run` de docs/product/install-default.md (gap
    (d)) ne doivent jamais diverger silencieusement du plan réellement
    généré : ce test extrait chaque bloc et le compare à la sortie réelle de
    la CLI, avec le répertoire de travail documenté (`--workdir` fixé plutôt
    que ~/.quadringent/<name>)."""

    DOC = Path("docs/product/install-default.md")
    WORKDIR = "/home/operateur/.quadringent/demo-int"

    def _fenced_block_after(self, marker: str) -> str:
        text = self.DOC.read_text(encoding="utf-8")
        after = text.split(marker, 1)[1]
        # Le premier bloc ``` après le marqueur est la commande, le second
        # (celui qui commence par "Installation Quadringent") est la sortie.
        blocks = after.split("```")
        # blocks[1] = commande, blocks[3] = sortie
        return blocks[3].strip("\n")

    def _run_dry_run(self, argv: list[str]) -> str:
        runner = RecordingRunner()
        out = io.StringIO()
        code = run([*argv, "--workdir", self.WORKDIR, "--dry-run"], runner=runner, stdout=out)
        self.assertEqual(code, 0)
        return out.getvalue().strip("\n")

    def test_gcp_cluster_existing_bucket_sample_is_accurate(self) -> None:
        documented = self._fenced_block_after("### Exemple `--dry-run` — GCP, cluster existant, bucket GCS réutilisé")
        actual = self._run_dry_run(
            [
                "install", "--cloud", "gcp", "--target", "cluster", "--region", "europe-west1", "--name", "demo-int",
                "--project", "example-gcp-project", "--existing-bucket", "preexisting-gcs-bucket",
            ]
        )
        self.assertEqual(documented, actual)

    def test_aws_cluster_sample_is_accurate(self) -> None:
        documented = self._fenced_block_after("### Exemple `--dry-run` — AWS, cluster existant")
        actual = self._run_dry_run(
            ["install", "--cloud", "aws", "--target", "cluster", "--region", "eu-west-3", "--name", "demo-int"]
        )
        self.assertEqual(documented, actual)


if __name__ == "__main__":
    unittest.main()
