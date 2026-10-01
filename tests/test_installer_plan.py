"""Plan d'installation : entrées, tfvars, values Helm générées."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from quadringent.installer.manifest import ReleaseManifest
from quadringent.installer.plan import (
    CAPTURE_KUBERNETES_SERVICE_ACCOUNT_NAME,
    InstallInputs,
    InvalidInstallInputs,
    activation_message,
    addon_tfvars_for,
    build_chart_values,
    build_plan,
    fetch_first_admin_activation_token,
    vm_tfvars_for,
)
from quadringent.installer.runner import RecordingRunner, CommandResult
from quadringent.installer.preflight import run_preflight

MANIFEST = ReleaseManifest(
    repository="ghcr.io/quadringent/quadringent",
    image_digest="sha256:" + "0" * 64,
    control_plane_image_digest="sha256:" + "1" * 64,
    verifier_image_digest="sha256:" + "3" * 64,
    observability_image_digest="sha256:" + "2" * 64,
)


def _helm_template(values: dict, *, namespace: str = "quadringent", release: str = "demo-int") -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(values, handle, sort_keys=False, allow_unicode=True)
        values_path = handle.name
    return subprocess.run(
        ["helm", "template", release, "chart", "--namespace", namespace, "-f", values_path],
        capture_output=True,
        text=True,
    )


GCP_PROJECT = "example-gcp-project"


class InstallInputsTests(unittest.TestCase):
    def test_valid_inputs(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        self.assertEqual(inputs.namespace, "quadringent")

    def test_rejects_bad_cloud(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(cloud="azure", target="vm", region="eu-west-3", name="demo")

    def test_rejects_bad_target(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(cloud="aws", target="bare-metal", region="eu-west-3", name="demo")

    def test_rejects_bad_name(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="Demo_Int")

    def test_rejects_bad_aws_region(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(cloud="aws", target="vm", region="not-a-region", name="demo")

    def test_gcp_region_not_aws_shaped_is_allowed(self) -> None:
        InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo", project=GCP_PROJECT)

    def test_existing_checkpoint_table_rejected_on_gcp(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(
                cloud="gcp", target="vm", region="europe-west1", name="demo",
                existing_checkpoint_table="some-table",
            )

    def test_project_required_for_gcp(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo")

    def test_project_rejected_on_aws(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo", project="some-project")

    def test_aws_profile_rejected_on_gcp(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(
                cloud="gcp", target="vm", region="europe-west1", name="demo",
                project=GCP_PROJECT, aws_profile="default",
            )

    def test_aws_profile_accepted_on_aws(self) -> None:
        InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo", aws_profile="default")

    def test_eks_oidc_arn_and_url_must_match(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(
                cloud="aws", target="cluster", region="eu-west-3", name="demo",
                eks_oidc_provider_arn="arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-3.amazonaws.com/id/ABC",
                eks_oidc_provider_url="oidc.eks.eu-west-3.amazonaws.com/id/DIFFERENT",
            )

    def test_eks_oidc_region_must_match_target_region(self) -> None:
        with self.assertRaises(InvalidInstallInputs):
            InstallInputs(
                cloud="aws", target="cluster", region="eu-west-3", name="demo",
                eks_oidc_provider_arn="arn:aws:iam::000000000000:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/ABC",
                eks_oidc_provider_url="oidc.eks.eu-west-1.amazonaws.com/id/ABC",
            )

    def test_vm_network_is_written_to_actual_tfvars(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="vm", region="eu-west-3", name="demo",
            vpc_id="vpc-abc123", subnet_id="subnet-def456",
        )
        values = vm_tfvars_for(inputs, {"runtime_instance_profile_name": "role-profile"})
        self.assertEqual(values["vpc_id"], "vpc-abc123")
        self.assertEqual(values["subnet_id"], "subnet-def456")
        self.assertEqual(values["instance_profile_name"], "role-profile")

    def test_explicit_aws_profile_satisfies_preflight_without_global_env(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo", aws_profile="demo-int")
        results = run_preflight(inputs, RecordingRunner(), environ={})
        self.assertTrue(all(item.ok for item in results))


class BuildPlanTests(unittest.TestCase):
    def test_aws_vm_plan_has_base_vm_and_helm_steps(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        plan = build_plan(inputs, Path("/tmp/quadringent-test-workdir"), MANIFEST)
        descriptions = [step.description for step in plan.steps]
        self.assertTrue(any("socle" in d for d in descriptions))
        self.assertTrue(any("VM" in d for d in descriptions))
        self.assertTrue(any("kubeconfig" in d for d in descriptions))
        self.assertTrue(any("helm template" in step.render() for step in plan.steps))
        self.assertTrue(any("helm upgrade --install" in step.render() for step in plan.steps))
        commands = [step.argv[0] for step in plan.steps if step.kind == "command"]
        self.assertIn("terraform", commands)
        self.assertIn("helm", commands)

    def test_aws_cluster_plan_uses_eks_addon(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        plan = build_plan(inputs, Path("/tmp/quadringent-test-workdir"), MANIFEST)
        # gap (1) : le module source (repo) n'apparaît plus jamais en cwd —
        # seule la copie privée workdir/terraform/<module> l'est. Le module
        # source apparaît côté `source` de l'étape copy_module.
        sources = [s.source for s in plan.steps if s.kind == "copy_module"]
        self.assertTrue(any("eks-addon" in s for s in sources))
        cwds = [s.cwd for s in plan.steps if s.cwd]
        self.assertTrue(all("deploy/terraform" not in c for c in cwds))
        self.assertTrue(any("terraform/addon" in c for c in cwds))

    def test_gcp_vm_plan_uses_gcp_modules(self) -> None:
        inputs = InstallInputs(
            cloud="gcp", target="vm", region="europe-west1", name="demo-int", project=GCP_PROJECT,
            gcp_network="test-vpc", gcp_subnetwork="test-subnet",
        )
        plan = build_plan(inputs, Path("/tmp/quadringent-test-workdir"), MANIFEST)
        sources = [s.source for s in plan.steps if s.kind == "copy_module"]
        self.assertTrue(any("gcp/base" in s for s in sources))
        self.assertTrue(any("gcp/vm" in s for s in sources))
        cwds = [s.cwd for s in plan.steps if s.cwd]
        self.assertTrue(any("terraform/base" in c for c in cwds))
        self.assertTrue(any("terraform/vm" in c for c in cwds))
        descriptions = [step.description for step in plan.steps]
        self.assertTrue(any("kubeconfig k3s" in d and "IAP" in d for d in descriptions))
        self.assertFalse(any("non prise en charge" in d for d in descriptions))
        tunnel = next(step for step in plan.steps if "tunnel SSH IAP" in step.description)
        self.assertEqual(tunnel.argv[:3], ("gcloud", "compute", "ssh"))
        self.assertIn("--tunnel-through-iap", tunnel.argv)
        self.assertNotIn("start-iap-tunnel", tunnel.argv)

    def test_gcp_vm_network_and_identity_are_in_actual_tfvars(self) -> None:
        inputs = InstallInputs(
            cloud="gcp", target="vm", region="europe-west1", name="demo-int", project=GCP_PROJECT,
            gcp_network="test-vpc", gcp_subnetwork="test-subnet", gcp_zone="europe-west1-c",
        )
        values = vm_tfvars_for(inputs, {"service_account_email": "runtime@example-project.iam.gserviceaccount.com"})
        self.assertEqual(values["network"], "test-vpc")
        self.assertEqual(values["subnetwork"], "test-subnet")
        self.assertEqual(values["zone"], "europe-west1-c")
        self.assertEqual(values["service_account_email"], "runtime@example-project.iam.gserviceaccount.com")

    def test_render_text_lists_every_step(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        plan = build_plan(inputs, Path("/tmp/quadringent-test-workdir"), MANIFEST)
        text = plan.render_text()
        for index in range(1, len(plan.steps) + 1):
            self.assertIn(f"{index}.", text)


class ChartValuesTests(unittest.TestCase):
    def test_aws_values_enable_control_plane(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertTrue(values["controlPlane"]["enabled"])
        self.assertEqual(values["replicaCount"], 0)
        self.assertEqual(values["storage"]["backend"], "aws")
        self.assertIn("checkpointTable", values["storage"])
        self.assertFalse(values["controlPlane"]["launch"]["enabled"])

    def test_gcp_vm_enables_control_plane_with_vm_metadata_identity(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, MANIFEST, control_plane_role_arn="",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        self.assertTrue(values["controlPlane"]["enabled"])
        self.assertTrue(values["controlPlane"]["v2"]["enabled"])
        self.assertEqual(values["gcpIdentityMode"], "vm-metadata")
        self.assertTrue(values["serviceAccount"]["create"])
        self.assertEqual(values["storage"]["backend"], "gcs")
        self.assertIn("checkpointBucket", values["storage"])

    def test_gcp_cluster_enables_control_plane_v2_with_workload_identity(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, MANIFEST,
            control_plane_role_arn="arn:aws:iam::000000000000:role/unused",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        self.assertTrue(values["controlPlane"]["enabled"])
        self.assertTrue(values["controlPlane"]["v2"]["enabled"])
        service_account = values["controlPlane"]["serviceAccount"]
        self.assertEqual(
            service_account["gcpServiceAccount"],
            "demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        # roleArn (AWS/IRSA) ne doit jamais apparaître sur GCS : la chart le
        # refuse explicitement (mutuellement exclusif avec gcpServiceAccount).
        self.assertNotIn("roleArn", service_account)

    def test_gcp_cluster_renders_control_plane_v2_and_postgres_via_helm(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, MANIFEST,
            control_plane_role_arn="arn:aws:iam::000000000000:role/unused",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        result = _helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        kinds_and_names = {(doc["kind"], doc["metadata"]["name"]) for doc in documents}
        self.assertIn(("Deployment", "demo-int-quadringent-control-plane"), kinds_and_names)
        self.assertIn(("StatefulSet", "demo-int-quadringent-postgres"), kinds_and_names)
        control_plane_sa = next(
            doc for doc in documents if doc["kind"] == "ServiceAccount" and doc["metadata"]["name"] == "quadringent-control-plane"
        )
        self.assertEqual(
            control_plane_sa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"],
            "demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        control_plane_deployment = next(
            doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "demo-int-quadringent-control-plane"
        )
        container_names = {c["name"] for c in control_plane_deployment["spec"]["template"]["spec"]["containers"]}
        self.assertIn("control-plane-v2", container_names)

    def test_gcp_vm_renders_v2_and_postgres_without_gke_annotation(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, MANIFEST, control_plane_role_arn="",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        result = _helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        names = {(doc["kind"], doc["metadata"]["name"]) for doc in documents}
        self.assertIn(("Deployment", "demo-int-quadringent-control-plane"), names)
        self.assertIn(("StatefulSet", "demo-int-quadringent-postgres"), names)
        accounts = [doc for doc in documents if doc["kind"] == "ServiceAccount"]
        self.assertEqual(len(accounts), 2)
        for account in accounts:
            self.assertNotIn("iam.gke.io/gcp-service-account", account["metadata"]["annotations"])

    def test_image_repository_override_applies_to_all_three_images(self) -> None:
        manifest = ReleaseManifest.from_file(
            Path("deploy/release-manifest.example.json"), repository_override="registry.example.test/quadringent"
        )
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, manifest,
            control_plane_role_arn="arn:aws:iam::000000000000:role/unused",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        # Capture (image lecteur).
        self.assertEqual(values["image"]["repository"], "registry.example.test/quadringent")
        # Control plane (v1 + v2, v2 hérite de controlPlane.image quand vide).
        self.assertEqual(values["controlPlane"]["image"]["repository"], "registry.example.test/quadringent")
        self.assertEqual(values["controlPlane"]["image"]["allowedRepositories"], ["registry.example.test/quadringent"])
        # Vérificateur : chart/templates/verifier-job.yaml compose son image
        # depuis image.repository (même dépôt que le lecteur) + un digest
        # dédié — pas de champ verification.repository séparé.
        self.assertEqual(values["verification"]["imageDigest"], manifest.verifier_image_digest)

    def test_aws_values_enable_control_plane_v2_by_default(self) -> None:
        # Sans v2 (ni Postgres, actif par défaut côté chart), aucun compte
        # admin ne peut jamais s'activer — voir
        # fetch_first_admin_activation_token.
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertTrue(values["controlPlane"]["v2"]["enabled"])
        # postgres.enabled n'est pas surchargé : le défaut de la chart
        # (postgres.yaml, actif) s'applique tel quel.
        self.assertNotIn("postgres", values)

    def test_image_digest_comes_from_manifest(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertEqual(values["image"]["digest"], MANIFEST.image_digest)
        self.assertEqual(values["controlPlane"]["image"]["digest"], MANIFEST.control_plane_image_digest)
        self.assertEqual(values["verification"]["imageDigest"], MANIFEST.verifier_image_digest)

    def test_existing_bucket_and_checkpoint_table_are_reused_in_values(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="vm", region="eu-west-3", name="demo-int",
            existing_bucket="already-there-bucket",
            existing_checkpoint_table="already-there-table",
        )
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertEqual(values["storage"]["rawBucket"], "already-there-bucket")
        self.assertEqual(values["storage"]["checkpointTable"], "already-there-table")

    def test_existing_bucket_reused_in_gcp_checkpoint_bucket(self) -> None:
        inputs = InstallInputs(
            cloud="gcp", target="vm", region="europe-west1", name="demo-int",
            existing_bucket="already-there-bucket", project=GCP_PROJECT,
        )
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertEqual(values["storage"]["rawBucket"], "already-there-bucket")
        self.assertEqual(values["storage"]["checkpointBucket"], "already-there-bucket")

    def test_default_generated_names_when_no_existing_storage(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertEqual(values["storage"]["rawBucket"], "demo-int-quadringent-raw")
        self.assertEqual(values["storage"]["checkpointTable"], "demo-int-quadringent-checkpoints")

    def test_aws_site_checkpoint_table_matches_storage(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        self.assertEqual(values["site"]["checkpointTable"], values["storage"]["checkpointTable"])

    def test_gcp_site_checkpoint_table_stays_empty_never_a_fictional_value(self) -> None:
        """Avant ce correctif, site.checkpointTable publiait toujours
        "<name>-quadringent-checkpoints", y compris sur GCS où aucune table
        DynamoDB n'existe (storage.checkpointBucket porte l'équivalent)."""
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, MANIFEST,
            control_plane_role_arn="arn:aws:iam::000000000000:role/unused",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        self.assertEqual(values["site"]["checkpointTable"], "")
        self.assertNotIn("quadringent-checkpoints", values["site"]["checkpointTable"])

    def test_capture_service_account_is_created_with_the_same_identity_as_control_plane(self) -> None:
        """Avant ce correctif, l'installateur publiait serviceAccount:
        {create: false, name: "default"} : les pods de capture tournaient
        sans aucune identité cloud."""
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/x")
        capture = values["serviceAccount"]
        control_plane = values["controlPlane"]["serviceAccount"]
        self.assertTrue(capture["create"])
        self.assertEqual(capture["name"], "quadringent-capture")
        self.assertEqual(capture["roleArn"], control_plane["roleArn"])

    def test_capture_service_account_uses_gcp_identity_on_gcs(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        values = build_chart_values(
            inputs, MANIFEST,
            control_plane_role_arn="",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        capture = values["serviceAccount"]
        self.assertTrue(capture["create"])
        self.assertEqual(capture["name"], "quadringent-capture")
        self.assertEqual(
            capture["gcpServiceAccount"],
            "demo-int-quadringent-runtime@example-project.iam.gserviceaccount.com",
        )
        self.assertNotIn("roleArn", capture)


class AddonTfvarsCaptureIdentityTests(unittest.TestCase):
    def test_aws_addon_tfvars_declare_the_capture_service_account_name(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        tfvars = addon_tfvars_for(inputs)
        self.assertEqual(tfvars["capture_service_account_name"], CAPTURE_KUBERNETES_SERVICE_ACCOUNT_NAME)

    def test_gcp_addon_tfvars_declare_the_capture_kubernetes_service_account_name(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="cluster", region="europe-west1", name="demo-int", project=GCP_PROJECT)
        tfvars = addon_tfvars_for(inputs)
        self.assertEqual(tfvars["capture_kubernetes_service_account_name"], CAPTURE_KUBERNETES_SERVICE_ACCOUNT_NAME)


class ExistingStorageTfvarsTests(unittest.TestCase):
    def test_existing_bucket_written_to_base_tfvars(self) -> None:
        inputs = InstallInputs(
            cloud="aws", target="vm", region="eu-west-3", name="demo-int",
            existing_bucket="already-there-bucket", existing_checkpoint_table="already-there-table",
        )
        plan = build_plan(inputs, Path("/tmp/quadringent-test-workdir"), MANIFEST)
        write_step = next(
            step for step in plan.steps
            if step.kind == "write_file" and step.path.endswith("terraform/base/site.auto.tfvars.json")
        )
        content = json.loads(write_step.content)
        self.assertEqual(content["existing_bucket_name"], "already-there-bucket")
        self.assertEqual(content["existing_checkpoint_table_name"], "already-there-table")

    def test_no_existing_storage_omits_tfvars_keys(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        plan = build_plan(inputs, Path("/tmp/quadringent-test-workdir"), MANIFEST)
        write_step = next(
            step for step in plan.steps
            if step.kind == "write_file" and step.path.endswith("terraform/base/site.auto.tfvars.json")
        )
        content = json.loads(write_step.content)
        self.assertNotIn("existing_bucket_name", content)
        self.assertNotIn("existing_checkpoint_table_name", content)


class FirstAdminActivationTests(unittest.TestCase):
    """Gap (c) : le lien d'activation admin doit porter un jeton réellement
    émis par le control plane v2 (POST /v2/setup/first-admin), jamais un
    lien fictif — récupéré par kubectl exec (loopback dans le pod), jamais
    un port-forward exposé pour cet appel unique."""

    INPUTS = InstallInputs(
        cloud="aws", target="cluster", region="eu-west-3", name="demo-int", admin_email="admin@example.com"
    )
    EXEC_ARGV = (
        "kubectl", "-n", "quadringent", "exec", "deployment/demo-int-quadringent-control-plane",
        "-c", "control-plane-v2", "--", "python", "-c",
    )

    def test_v2_disabled_never_attempts_the_call(self) -> None:
        runner = RecordingRunner()
        token = fetch_first_admin_activation_token(self.INPUTS, runner, v2_enabled=False)
        self.assertIsNone(token)
        self.assertEqual(runner.calls, [])

    def test_without_admin_email_never_attempts_the_call(self) -> None:
        runner = RecordingRunner()
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        self.assertIsNone(fetch_first_admin_activation_token(inputs, runner, v2_enabled=True))
        self.assertEqual(runner.calls, [])
        self.assertIn("--admin-email", activation_message(inputs))

    def test_successful_response_returns_the_token(self) -> None:
        body = json.dumps({"before": None, "after": {"activation_token": "demo"}})
        runner = _ScriptedExecRunner(stdout=body, returncode=0)
        token = fetch_first_admin_activation_token(self.INPUTS, runner, v2_enabled=True)
        self.assertEqual(token, "demo")
        self.assertEqual(len(runner.calls), 1)
        argv = runner.calls[0]
        self.assertEqual(argv[:9], self.EXEC_ARGV[:9])
        self.assertIn("/v2/setup/first-admin", argv[-1])
        self.assertIn("127.0.0.1:8845", argv[-1])

    def test_admin_already_exists_returns_none_not_a_fake_token(self) -> None:
        # 409 : idempotent_write renvoie un code d'échec, le script Python
        # sort en erreur (raise SystemExit(1)) -> commande non "ok".
        runner = _ScriptedExecRunner(stdout='{"error":"capability_unavailable"}', returncode=1)
        token = fetch_first_admin_activation_token(self.INPUTS, runner, v2_enabled=True)
        self.assertIsNone(token)

    def test_unexpected_response_shape_returns_none(self) -> None:
        runner = _ScriptedExecRunner(stdout="not json at all", returncode=0)
        token = fetch_first_admin_activation_token(self.INPUTS, runner, v2_enabled=True)
        self.assertIsNone(token)

    def test_activation_message_uses_real_token_in_link(self) -> None:
        message = activation_message(self.INPUTS, activation_token="demo")
        self.assertIn("token=demo", message)

    def test_activation_message_link_is_hash_based_for_the_ui_router(self) -> None:
        # ui/src/router.ts est entièrement basé sur location.hash
        # (WizardActivate.tsx sert la route wizard/activate) : le lien doit
        # porter le jeton dans le fragment, jamais comme chemin serveur.
        message = activation_message(self.INPUTS, activation_token="demo")
        self.assertIn("http://127.0.0.1:8844/#/wizard/activate?token=demo", message)

    def test_activation_message_without_token_never_fabricates_a_link(self) -> None:
        message = activation_message(self.INPUTS, activation_token=None)
        self.assertNotIn("/activate?token=", message)
        self.assertIn("non émis", message)


class _ScriptedExecRunner:
    """Runner de test minimal : toute commande renvoie le même résultat
    scripté, quel que soit l'argv exact (le script Python inline varie peu
    mais n'a pas besoin d'être comparé caractère à caractère ici)."""

    def __init__(self, *, stdout: str, returncode: int) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, cwd=None, env=None):
        self.calls.append(tuple(argv))
        return CommandResult(tuple(argv), self.returncode, self.stdout, "")

    def which(self, tool: str) -> str | None:
        return f"/usr/bin/{tool}"


if __name__ == "__main__":
    unittest.main()
