"""Rendu de la chart avec storage.backend=gcs (gaps (b) et (c) de
docs/product/install-default.md).

Avant ce correctif, `configmap-site.yaml` exigeait `site.awsAccountId` et
`aws.region` — et `control-plane.yaml` exigeait un `controlPlane.
serviceAccount.roleArn` au format ARN AWS — quel que soit le backend de
stockage déclaré, rendant le rendu impossible pour une installation GCP sans
valeurs de remplissage fictives. Ce module vérifie que :

- storage.backend=gcs rend sans compte AWS ni région AWS ni ARN IAM ;
- l'identité Workload Identity GCP (`gcpServiceAccount`) est exigée et validée
  à sa place, avec l'annotation `iam.gke.io/gcp-service-account` ;
- les deux backends restent mutuellement exclusifs (déclarer les champs de
  l'autre backend est un refus explicite) ;
- le rendu du fichier de values AWS existant (`infra-values/values-int.yaml`)
  reste identique en comportement (comparaison dorée).
"""

from __future__ import annotations

import subprocess
import unittest

VALUES = "infra-values/values-int.yaml"
NAMESPACE = "quadringent-demo"

LAUNCH_FILES = (
    "--set-file", "controlPlane.launch.jobTemplate=infra-values/job-template-int.json",
    "--set-file", "controlPlane.launch.fleetCatalog=infra-values/fleet-catalog-int.json",
    "--set-file", "controlPlane.launch.fleetSidecar=infra-values/fleet-sidecar-int.json",
)

# Bascule minimale vers un rendu GCS valide, à partir des values INT (AWS).
GCS_OVERRIDES = (
    "storage.backend=gcs",
    "storage.checkpointBucket=example-corp-int-example-corp-checkpoints",
    "site.awsAccountId=",
    "aws.region=",
    # site.checkpointTable (table DynamoDB) ne s'applique qu'à storage.backend=aws,
    # comme site.awsAccountId/aws.region ci-dessus — values-int.yaml (AWS) le
    # déclare, doit être vidé pour un rendu GCS valide.
    "site.checkpointTable=",
    "controlPlane.serviceAccount.roleArn=",
    "controlPlane.serviceAccount.gcpServiceAccount=quadringent-cp@example-corp-int.iam.gserviceaccount.com",
)


def render(*values: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "cdc", "chart", "--namespace", NAMESPACE, "-f", VALUES, *LAUNCH_FILES]
    for value in values:
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True)


def install_notes(*values: str) -> str:
    command = [
        "helm", "install", "cdc", "chart", "--namespace", NAMESPACE,
        "--dry-run=client", "-f", VALUES, *LAUNCH_FILES,
    ]
    for value in values:
        command += ["--set", value]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.rsplit("NOTES:", 1)[-1]


class GcsBackendRenderTests(unittest.TestCase):
    def test_install_notes_match_gcs_backend_and_v2(self) -> None:
        notes = install_notes(*GCS_OVERRIDES, "controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertIn("Raw          : gs://", notes)
        self.assertIn("API v2        : kubectl", notes)
        self.assertNotIn("s3://", notes)
        self.assertNotIn("Seuil S3", notes)
        self.assertNotIn("Table        : .", notes)

    def test_install_notes_do_not_claim_to_create_an_aws_alarm(self) -> None:
        notes = install_notes()
        self.assertIn("Raw          : s3://", notes)
        self.assertIn("la chart ne la crée pas", notes)

    def test_gcs_backend_renders_without_aws_identity(self) -> None:
        result = render(*GCS_OVERRIDES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QUADRINGENT_AWS_ACCOUNT_ID: ""', result.stdout)
        self.assertIn('QUADRINGENT_AWS_REGION: ""', result.stdout)
        self.assertNotIn("eks.amazonaws.com/role-arn", result.stdout)
        # Seul le conteneur control-plane posait AWS_REGION sans garde ; les
        # autres charges (preflight, désactivé par défaut) sont hors périmètre.
        self.assertNotIn("- name: AWS_REGION", result.stdout)

    def test_gcs_backend_annotates_workload_identity(self) -> None:
        result = render(*GCS_OVERRIDES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'iam.gke.io/gcp-service-account: "quadringent-cp@example-corp-int.iam.gserviceaccount.com"',
            result.stdout,
        )

    def test_gcs_backend_requires_gcp_service_account(self) -> None:
        overrides = tuple(
            v for v in GCS_OVERRIDES if not v.startswith("controlPlane.serviceAccount.gcpServiceAccount")
        )
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gcpServiceAccount est obligatoire", result.stderr)

    def test_gcs_backend_rejects_invalid_gcp_service_account(self) -> None:
        overrides = tuple(
            v for v in GCS_OVERRIDES if not v.startswith("controlPlane.serviceAccount.gcpServiceAccount")
        ) + ("controlPlane.serviceAccount.gcpServiceAccount=not-an-email",)
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gcpServiceAccount doit être une adresse de compte de service GCP valide", result.stderr)

    def test_gcs_backend_rejects_aws_role_arn(self) -> None:
        overrides = tuple(v for v in GCS_OVERRIDES if not v.startswith("controlPlane.serviceAccount.roleArn"))
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("roleArn ne s'applique qu'à storage.backend=aws", result.stderr)

    def test_gcs_backend_rejects_declared_aws_account_id(self) -> None:
        overrides = tuple(v for v in GCS_OVERRIDES if not v.startswith("site.awsAccountId"))
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("site.awsAccountId ne s'applique qu'à storage.backend=aws", result.stderr)

    def test_gcs_backend_rejects_declared_aws_region(self) -> None:
        overrides = tuple(v for v in GCS_OVERRIDES if not v.startswith("aws.region"))
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aws.region ne s'applique qu'à storage.backend=aws", result.stderr)

    def test_gcs_backend_rejects_declared_checkpoint_table(self) -> None:
        overrides = tuple(v for v in GCS_OVERRIDES if not v.startswith("site.checkpointTable"))
        result = render(*overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("site.checkpointTable ne s'applique qu'à storage.backend=aws", result.stderr)

    def test_gcs_backend_publishes_checkpoint_bucket(self) -> None:
        """entrypoint.py::build_pipeline_executor lit QUADRINGENT_CHECKPOINT_BUCKET
        pour storage_backend=gcs — sans cette clé, l'EvidenceReader résolvait
        toujours un emplacement de checkpoints vide sur GCS."""
        result = render(*GCS_OVERRIDES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'QUADRINGENT_CHECKPOINT_BUCKET: "example-corp-int-example-corp-checkpoints"',
            result.stdout,
        )
        self.assertIn('QUADRINGENT_CHECKPOINT_TABLE: ""', result.stdout)

    def test_aws_backend_still_requires_role_arn(self) -> None:
        result = render("controlPlane.serviceAccount.roleArn=")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("roleArn est obligatoire lorsque storage.backend=aws", result.stderr)

    def test_aws_backend_rejects_gcp_service_account(self) -> None:
        result = render("controlPlane.serviceAccount.gcpServiceAccount=cp@proj.iam.gserviceaccount.com")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gcpServiceAccount ne s'applique qu'à storage.backend=gcs", result.stderr)

    def test_aws_backend_still_requires_account_id_and_region(self) -> None:
        # controlPlane désactivé pour isoler la garde de configmap-site.yaml :
        # avec le control plane actif, sa propre garde roleArn (qui référence
        # aussi site.awsAccountId) peut être atteinte en premier par Helm.
        result = render("site.awsAccountId=", "controlPlane.enabled=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("awsAccountId est obligatoire lorsque storage.backend=aws", result.stderr)

        result = render("aws.region=", "controlPlane.enabled=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aws.region est obligatoire lorsque storage.backend=aws", result.stderr)

    def test_golden_aws_render_is_unchanged_in_behaviour(self) -> None:
        """La configuration AWS existante (infra-values/values-int.yaml) doit
        continuer à rendre exactement l'identité IRSA et les champs AWS
        attendus — aucune régression pour les sites déjà déployés."""

        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QUADRINGENT_AWS_ACCOUNT_ID: "000000000000"', result.stdout)
        self.assertIn('QUADRINGENT_AWS_REGION: "eu-west-3"', result.stdout)
        self.assertIn(
            'eks.amazonaws.com/role-arn: "arn:aws:iam::000000000000:role/example-platform-int-cdcforge-control-plane"',
            result.stdout,
        )
        self.assertNotIn("iam.gke.io/gcp-service-account", result.stdout)


if __name__ == "__main__":
    unittest.main()
