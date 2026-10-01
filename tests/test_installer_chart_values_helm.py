"""Les values Helm générées par l'installateur doivent réellement se rendre
avec `helm template`, dans le même style que les autres tests de la chart
(tests/test_chart_*.py) : schéma + gardes des templates, pas seulement la
forme Python."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from quadringent.installer.manifest import ReleaseManifest
from quadringent.installer.plan import InstallInputs, build_chart_values

MANIFEST = ReleaseManifest.from_file(Path("deploy/release-manifest.example.json"))


def _helm_template(values: dict, *, namespace: str, release: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(values, handle, sort_keys=False, allow_unicode=True)
        values_path = handle.name
    return subprocess.run(
        ["helm", "template", release, "chart", "--namespace", namespace, "-f", values_path],
        capture_output=True,
        text=True,
    )


class GeneratedChartValuesRenderTests(unittest.TestCase):
    def test_aws_vm_values_render_successfully(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(
            inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/demo-int-quadringent-runtime"
        )
        result = _helm_template(values, namespace=inputs.namespace, release=inputs.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        self.assertTrue(any(doc["kind"] == "ConfigMap" for doc in documents))

    def test_aws_cluster_values_render_successfully(self) -> None:
        inputs = InstallInputs(cloud="aws", target="cluster", region="eu-west-3", name="demo-int")
        values = build_chart_values(
            inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/demo-int-quadringent-irsa"
        )
        result = _helm_template(values, namespace=inputs.namespace, release=inputs.name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gcp_vm_values_render_successfully(self) -> None:
        inputs = InstallInputs(cloud="gcp", target="vm", region="europe-west1", name="demo-int", project="example-gcp-project")
        values = build_chart_values(
            inputs, MANIFEST, control_plane_role_arn="",
            control_plane_gcp_service_account="demo-int-quadringent-runtime@example-gcp-project.iam.gserviceaccount.com",
        )
        result = _helm_template(values, namespace=inputs.namespace, release=inputs.name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_values_reject_helm_lint_warnings_as_clean(self) -> None:
        inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
        values = build_chart_values(
            inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/demo-int-quadringent-runtime"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump(values, handle, sort_keys=False, allow_unicode=True)
            values_path = handle.name
        result = subprocess.run(
            ["helm", "lint", "chart", "--namespace", inputs.namespace, "-f", values_path],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
