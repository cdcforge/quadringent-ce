from __future__ import annotations

from pathlib import Path
import glob
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest

import pytest
import yaml


def runtime_module_names() -> tuple[str, ...]:
    return tuple(
        Path(module).stem
        for module in Path("docker/runtime-modules.txt").read_text().split()
    )


class RuntimePackagingTests(unittest.TestCase):
    def test_cockpit_image_is_independent_of_capture_and_serves_ui(self) -> None:
        command = ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo",
                   "-f", "infra-values/values-int.yaml"]
        original = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        changed = subprocess.run(command + ["--set-string", "controlPlane.image.digest=sha256:" + "a" * 64],
                                 check=True, capture_output=True, text=True).stdout
        def documents(value):
            return [part for part in value.split("\n---\n") if "kind: Deployment" in part]
        before = documents(original)
        after = documents(changed)
        capture_before = next(part for part in before if "-control-plane" not in part)
        capture_after = next(part for part in after if "-control-plane" not in part)
        cockpit = next(part for part in after if "-control-plane" in part)
        self.assertEqual(capture_before, capture_after)
        self.assertIn("ghcr.io/quadringent/quadringent@sha256:" + "a" * 64, cockpit)
        self.assertIn("- --ui-dist\n            - /app/ui", cockpit)
        self.assertNotIn("/opt/venv/bin/python", cockpit)
        self.assertIn("- python", cockpit)

    def test_cockpit_rejects_missing_or_mutable_image_digest(self) -> None:
        command = ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo",
                   "-f", "infra-values/values-int.yaml"]
        for digest in ("", "latest", "sha256:short"):
            with self.subTest(digest=digest):
                result = subprocess.run(command + ["--set-string", "controlPlane.image.digest=" + digest],
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(
                    "controlPlane.image.digest" in result.stderr
                    or "/controlPlane/image/digest" in result.stderr,
                    result.stderr,
                )

    def test_pilot_writes_to_its_run_without_changing_shared_paths(self) -> None:
        command = ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo",
                   "-f", "infra-values/values-int.yaml", "--set", "pilot.enabled=true"]
        for run_id in ("canary-one", "canary-two"):
            rendered = subprocess.run(command + ["--set-string", f"pilot.runId={run_id}"],
                                      capture_output=True, text=True, check=True).stdout
            job = next(part for part in rendered.split("---") if "kind: Job" in part)
            self.assertIn('- --reserve-run-id\n            - "' + run_id + '"', job)
            self.assertIn('name: AS400_RAW_PREFIX\n              value: "as400/sales/sale/runs/' + run_id + '"', job)
            self.assertIn('name: AS400_CONSOLE_SNAPSHOT_S3_KEY\n              value: "as400/sales/sale/runs/' + run_id + '/console-snapshot.json"', job)
            config = next(part for part in rendered.split("---") if "kind: ConfigMap" in part and "AS400_RAW_PREFIX:" in part)
            self.assertIn('AS400_RAW_PREFIX: "as400/sales/sale"', config)
            self.assertIn('AS400_CONSOLE_SNAPSHOT_S3_KEY: "as400/sales/sale/console-snapshot.json"', config)

    def test_pilot_rejects_unsafe_run_ids(self) -> None:
        for run_id in ("../other", "UPPER", "trailing-", "a" * 80, "bad/id"):
            with self.subTest(run_id=run_id):
                result = subprocess.run(
                    ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo",
                     "-f", "infra-values/values-int.yaml", "--set", "pilot.enabled=true",
                     "--set-string", f"pilot.runId={run_id}"], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("pilot.runId", result.stderr)

    def test_chart_requires_an_explicit_source_timezone(self) -> None:
        command = ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo",
                   "-f", "infra-values/values-int.yaml"]
        rendered = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertIn('AS400_SOURCE_TIME_ZONE: "Europe/Zurich"', rendered.stdout)
        rejected = subprocess.run(command + ["--set", "ibmi.sourceTimeZone="],
                                  capture_output=True, text=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("ibmi.sourceTimeZone est obligatoire", rejected.stderr)

    def test_control_plane_imports_with_only_the_packaged_runtime_modules(self) -> None:
        # Importing from src hides missing Docker COPY statements. Reproduce the
        # image filesystem in isolation, including only the declared modules.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "quadringent"
            package.mkdir()
            for source in Path("docker/runtime-modules.txt").read_text().split():
                self.assertIn(f"COPY {source} /app/quadringent/", Path("docker/Dockerfile").read_text())
                shutil.copy2(source, package / Path(source).name)
            shutil.copytree("src/quadringent_control_plane", root / "quadringent_control_plane")
            result = subprocess.run(
                [sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); "
                 "import quadringent_control_plane.projection; import quadringent_control_plane.server"],
                cwd=root, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_dev_iam_policy_is_bounded_to_the_rd_bucket_and_prefixes(self) -> None:
        policy = json.loads(Path("infra-values/iam-int-policy.json").read_text())
        statements = {item["Sid"]: item for item in policy["Statement"]}

        self.assertEqual(
            statements["RawObjectsOnly"]["Resource"],
            [
                "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/cntr/*",
                "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/sale/*",
            ],
        )
        self.assertEqual(
            statements["CheckpointTableOnly"]["Resource"],
            "arn:aws:dynamodb:eu-west-3:000000000000:table/example-corp-int-example-corp-checkpoints",
        )
        self.assertNotIn("s3:*", json.dumps(policy))

    def test_control_plane_iam_policy_can_only_read_sale_destination_proofs(self) -> None:
        policy = json.loads(
            Path("infra-values/iam-control-plane-int-policy.json").read_text()
        )

        self.assertEqual(
            policy["Statement"],
            [
                {
                    "Sid": "ReadLatestEvntProofOnly",
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": (
                        "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/"
                        "as400/sales/sale/proofs/cdcforge-autonomous-latest.json"
                    ),
                },
                {
                    "Sid": "ReadWindowDestinationProofOnly",
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": (
                        "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/"
                        "as400/sales/sale/runs/*/windows/*/destination.json"
                    ),
                },
                {
                    "Sid": "ReadLatestFleetConsoleOnly",
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": (
                        "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/"
                        "as400/sales/fleet/console-snapshot.json"
                    ),
                },
                {
                    "Sid": "ReadLatestFleetProofOnly",
                    "Effect": "Allow",
                    "Action": "s3:GetObject",
                    "Resource": (
                        "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/"
                        "as400/sales/fleet/console-proof.json"
                    ),
                }
            ],
        )

    def test_control_plane_irsa_trusts_only_its_dev_service_account(self) -> None:
        trust = json.loads(
            Path("infra-values/iam-control-plane-int-trust-policy.json").read_text()
        )
        statement = trust["Statement"]

        self.assertEqual(len(statement), 1)
        self.assertEqual(statement[0]["Action"], "sts:AssumeRoleWithWebIdentity")
        self.assertEqual(
            statement[0]["Condition"]["StringEquals"],
            {
                (
                    "oidc.eks.eu-west-3.amazonaws.com/id/"
                    "EXAMPLEOIDCPROVIDER:sub"
                ): (
                    "system:serviceaccount:quadringent-demo:"
                    "cdcforge-control-plane"
                ),
                (
                    "oidc.eks.eu-west-3.amazonaws.com/id/"
                    "EXAMPLEOIDCPROVIDER:aud"
                ): "sts.amazonaws.com",
            },
        )

    def test_snowflake_dev_read_policy_preserves_cntr_and_adds_sale(self) -> None:
        policy = json.loads(
            Path("infra-values/iam-snowflake-read-int-policy.json").read_text()
        )
        statements = {item["Sid"]: item for item in policy["Statement"]}

        self.assertEqual(
            statements["ListApprovedRawPrefixes"]["Condition"]["StringLike"][
                "s3:prefix"
            ],
            [
                "as400/sales/cntr/",
                "as400/sales/cntr/*",
                "as400/sales/sale/",
                "as400/sales/sale/*",
            ],
        )
        self.assertEqual(
            statements["ReadApprovedRawObjects"]["Resource"],
            [
                "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/cntr/*",
                "arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/sale/*",
            ],
        )
        self.assertNotIn("s3:*", json.dumps(policy))

    def test_snowflake_dev_setup_preserves_cntr_and_creates_a_separate_sale_stage(self) -> None:
        sql = Path("infra-values/snowflake-int.sql").read_text().upper()

        self.assertIn("ALTER STORAGE INTEGRATION DEV_AS400_RD_S3_INT", sql)
        self.assertIn("/AS400/SALES/CNTR/", sql)
        self.assertIn("/AS400/SALES/SALE/", sql)
        self.assertIn(
            "DEV_RAW.AS400_RD.AS400_RD_SALE_EXTERNAL_STAGE",
            sql,
        )
        self.assertNotIn("CREATE OR REPLACE STORAGE INTEGRATION", sql)
        self.assertNotIn("PROD_RAW", sql)

    def test_runtime_manifest_contains_transitive_console_modules(self) -> None:
        modules = set(runtime_module_names())
        self.assertIn("console_snapshot", modules)
        self.assertIn("lag_history", modules)

    def test_chart_wires_snapshot_and_explicit_plaintext_gate(self) -> None:
        rendered = subprocess.run(
            ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo", "-f", "infra-values/values-int.yaml"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("AS400_CONSOLE_SNAPSHOT_S3_KEY", rendered)
        self.assertIn("AS400_ALLOW_PLAINTEXT", rendered)
        self.assertIn("AS400_JOURNAL_LIBRARY", rendered)
        self.assertIn('ISERIES_JOURNAL_BUFFER_SIZE: "16000000"', rendered)
        self.assertNotIn('ISERIES_JOURNAL_BUFFER_SIZE: "1.6e+07"', rendered)

    def test_chart_anti_affinity_targets_the_popsink_namespace(self) -> None:
        rendered = subprocess.run(
            ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo", "-f", "infra-values/values-int.yaml"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("namespaces:\n                - popsink", rendered)
        self.assertIn("runAsUser: 10001", rendered)

    def test_chart_rejects_an_enabled_snapshot_without_a_key(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "consoleSnapshot.s3Key=",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("consoleSnapshot.s3Key est obligatoire", rendered.stderr)

    def test_chart_rejects_a_non_positive_snapshot_interval(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "consoleSnapshot.intervalSeconds=0",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rendered.returncode, 0)
        self.assertTrue(
            "consoleSnapshot.intervalSeconds doit être > 0" in rendered.stderr
            or "/consoleSnapshot/intervalSeconds" in rendered.stderr,
            rendered.stderr,
        )

    def test_chart_rejects_ibm_i_plaintext(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "as400.allowPlaintext=true",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rendered.returncode, 0)
        self.assertTrue(
            "as400.allowPlaintext doit rester false" in rendered.stderr
            or "/as400/allowPlaintext" in rendered.stderr,
            rendered.stderr,
        )

    def test_chart_rejects_disabled_ibm_i_tls(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "as400.tls=false",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rendered.returncode, 0)
        self.assertTrue(
            "as400.tls doit rester true" in rendered.stderr or "/as400/tls" in rendered.stderr,
            rendered.stderr,
        )

    def test_chart_rejects_an_empty_ibm_i_tls_ca_file(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "as400.tlsCaFile=",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rendered.returncode, 0)
        self.assertTrue(
            "as400.tlsCaFile est obligatoire" in rendered.stderr
            or "as400.tlsCaFile est obligatoire" in rendered.stdout,
            rendered.stderr,
        )

    def test_chart_wires_the_ibm_i_tls_ca_file(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn('AS400_TLS: "true"', rendered)
        self.assertIn('AS400_ALLOW_PLAINTEXT: "false"', rendered)
        self.assertIn('AS400_TLS_CA_FILE: "/app/certs/ibmi-ca.pem"', rendered)

    def test_le_ca_est_fourni_par_le_site_et_monte_en_lecture_seule(self) -> None:
        import yaml
        self.assertFalse(list(Path("docker/certs").glob("*.pem")))
        rendered = subprocess.run(["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo", "-f", "infra-values/values-int.yaml"], check=True, text=True, capture_output=True).stdout
        deploy = next(d for d in yaml.safe_load_all(rendered) if d and d["kind"] == "Deployment" and not d["metadata"]["name"].endswith("control-plane"))
        spec = deploy["spec"]["template"]["spec"]
        volume = next(v for v in spec["volumes"] if v["name"] == "ibmi-ca")
        self.assertEqual(volume["secret"]["secretName"], "quadringent-ibmi-ca")
        mount = next(v for v in spec["containers"][0]["volumeMounts"] if v["name"] == "ibmi-ca")
        self.assertTrue(mount["readOnly"])
        self.assertEqual(mount["mountPath"], "/app/certs")

    def test_les_images_ne_figent_pas_le_ca_d_un_client(self) -> None:
        for filename in ("docker/Dockerfile", "docker/control-plane.Dockerfile"):
            dockerfile = Path(filename).read_text()
            self.assertNotIn("COPY docker/certs/", dockerfile)
            self.assertNotIn("keytool -importcert", dockerfile)
            self.assertNotIn("AS400_ALLOW_PLAINTEXT=true", dockerfile)

    def test_production_source_requires_a_control_plane_url(self) -> None:
        source = Path("ui/src/data/source.ts").read_text()
        self.assertIn("export function configuredControlPlaneUrl(", source)
        self.assertIn("VITE_CONTROL_PLANE_URL is required", source)
        self.assertIn("export function configuredSource(", source)
        self.assertNotIn("/console-dev.json", source)

    def test_ci_builds_and_smokes_the_final_runtime_image(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("image-runtime:", workflow)
        self.assertIn("needs: [image-runtime, verifier-runtime, cockpit-runtime]", workflow)
        jobs = yaml.safe_load(workflow)["jobs"]
        build = next(step for step in jobs["image-runtime"]["steps"]
                     if step.get("uses", "").startswith("docker/build-push-action@"))
        self.assertEqual(build["with"]["file"], "docker/Dockerfile")
        self.assertTrue(build["with"]["load"])
        self.assertFalse(build["with"]["push"])
        self.assertIn("docker run --rm \"$CDC_IMAGE\" --help", workflow)
        self.assertIn("runtime-imports-ok", workflow)

    def test_ci_execute_les_memes_garanties_avec_pytest_unique_et_gitleaks_epingle(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn('GITLEAKS_VERSION: "8.30.1"', workflow)
        self.assertIn("gitleaks_${GITLEAKS_VERSION}_linux_${archive_arch}.tar.gz", workflow)
        self.assertIn('X64) archive_arch=x64; archive_sha256="$GITLEAKS_SHA256"', workflow)
        self.assertIn(
            "ARM64) archive_arch=arm64; archive_sha256="
            "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
            workflow,
        )
        self.assertIn('test "$RUNNER_OS" = "Linux"', workflow)
        self.assertIn('sha256sum --check --strict', workflow)
        self.assertIn(
            "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
            workflow,
        )
        for command in ("python -m pytest -q", "python -m pytest --collect-only -q",
                        "python scripts/raw_checkpoint_fault_matrix.py", "python scripts/check_publication.py",
                        "gitleaks git . --log-opts=HEAD", "gitleaks dir", "python scripts/sync_version.py --check"):
            self.assertIn(command, workflow)
        self.assertIn("python scripts/check_publication.py --history-ref HEAD", workflow)
        self.assertNotIn("unittest discover", workflow)

    def test_release_workflow_publishes_images_and_attaches_chart(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn("packages: write", workflow)
        self.assertIn("secrets.GITHUB_TOKEN", workflow)
        self.assertIn("docker/Dockerfile", workflow)
        self.assertIn("docker/control-plane.Dockerfile", workflow)
        self.assertIn("docker/verifier.Dockerfile", workflow)
        self.assertIn("linux/amd64,linux/arm64", workflow)
        self.assertIn('image="ghcr.io/$owner/quadringent-ce-runtime"', workflow)
        self.assertNotIn('image="ghcr.io/$owner/quadringent-community"', workflow)
        self.assertEqual(workflow.count("org.opencontainers.image.source=https://github.com/${{ github.repository }}"), 3)
        self.assertIn('DOCKER_BUILD_RECORD_UPLOAD: "false"', workflow)
        self.assertIn('helm package chart --version "$RELEASE_VERSION"', workflow)
        self.assertIn("quadringent-*.tgz", workflow)
        self.assertIn("packages/*.tgz", workflow)
        self.assertNotIn("helm push", workflow)

    def test_release_workflow_scans_signs_and_ships_an_sbom_per_image(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

        # le client ne patche jamais les images : chaque digest poussé est
        # scanné, signé et livré avec son SBOM dans la release GitHub
        self.assertIn("aquasecurity/trivy-action@", workflow)
        self.assertIn("severity: HIGH,CRITICAL", workflow)
        self.assertIn("ignore-unfixed: true", workflow)
        jobs = yaml.safe_load(workflow)["jobs"]
        image_scans = [step for step in jobs["images"]["steps"]
                       if step.get("uses", "").startswith("aquasecurity/trivy-action@")
                       and step.get("with", {}).get("scanners") == "vuln"]
        self.assertEqual(len(image_scans), 3)
        for scan in image_scans:
            self.assertEqual(scan["with"]["exit-code"], "1")
            self.assertEqual(scan["with"]["format"], "sarif")
            self.assertEqual(scan["with"]["severity"], "HIGH,CRITICAL")
            self.assertTrue(scan["with"]["ignore-unfixed"])
        self.assertTrue(all("-amd64" in step["with"]["input"] for step in image_scans))
        self.assertIn("format: sarif", workflow)
        # En SARIF, trivy-action ignore severity par défaut et ferait échouer
        # le gate sur des avis LOW/MEDIUM malgré HIGH,CRITICAL ci-dessus.
        self.assertEqual(workflow.count("limit-severities-for-sarif: true"), 3)
        self.assertEqual(workflow.count("format: spdx-json"), 3)
        for component in ("capture", "control_plane", "verifier"):
            self.assertIn(
                "${{ runner.temp }}/oci/" + component.replace("_", "-") + "-amd64",
                workflow,
            )
        self.assertIn("org.opencontainers.image.revision=${{ github.sha }}", workflow)
        self.assertIn("python3 scripts/write_release_manifest.py", workflow)
        self.assertIn("release-manifest.json", workflow)
        self.assertIn("sigstore/cosign-installer@", workflow)
        self.assertIn("cosign sign --yes", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("security-events: write", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("actions/download-artifact@v4", workflow)
        self.assertIn("softprops/action-gh-release@", workflow)
        self.assertIn("github/codeql-action/upload-sarif@", workflow)
        self.assertIn("needs: [images, chart, validate]", workflow)
        self.assertIn("PUBLICATION_APPROVED == 'true'", workflow)
        self.assertIn("environment: publication-approved", workflow)
        self.assertIn("draft: true", workflow)
        self.assertIn('--app-version "$RELEASE_VERSION"', workflow)
        self.assertIn("packages/**/*.whl", workflow)
        self.assertIn("contents: write", workflow)

    def test_release_workflow_scans_arm64_explicitly_before_release(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn('--input "${image}-arm64" --platform linux/arm64', workflow)
        self.assertIn("--scanners vuln --severity HIGH,CRITICAL", workflow)
        self.assertIn("for platform in linux/amd64 linux/arm64", workflow)
        self.assertIn("--scanners secret --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL", workflow)
        self.assertIn("--severity HIGH,CRITICAL", workflow)
        self.assertIn("--ignore-unfixed --exit-code 1", workflow)
        self.assertIn("--format spdx-json", workflow)
        for component in ("CAPTURE", "CONTROL_PLANE", "VERIFIER"):
            self.assertIn(f"{component}_IMAGE: ${{{{ runner.temp }}}}/oci/", workflow)
        self.assertIn("VERSION: ${{ needs.validate.outputs.version }}", workflow)
        for component in ("capture", "control-plane", "verifier"):
            self.assertIn(f"quadringent-${{VERSION}}-{component}-arm64.spdx.json", workflow)

    def test_runtime_image_contains_the_safe_sql_canary_entrypoint(self) -> None:
        dockerfile = Path("docker/Dockerfile").read_text(encoding="utf-8")

        self.assertIn(
            "COPY scripts/as400_sql_journal_capture.py /app/", dockerfile
        )

    def test_runtime_image_contains_the_control_plane_entrypoint(self) -> None:
        dockerfile = Path("docker/Dockerfile").read_text(encoding="utf-8")

        self.assertIn(
            "COPY src/quadringent_control_plane /app/quadringent_control_plane",
            dockerfile,
        )
        self.assertIn(
            "COPY scripts/quadringent_control_plane.py /app/", dockerfile
        )

    def test_runtime_image_contains_the_diagnostic_job_entrypoints(self) -> None:
        """Sonde de source et découverte de tables (chantier « prod-wiring ») :
        Jobs Kubernetes éphémères lancés avec cette image — voir
        ``executor/manifests.py::build_source_probe_job``/
        ``build_table_discovery_job`` et
        ``executor/diagnostic_jobs.py``."""

        dockerfile = Path("docker/Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY scripts/quadringent_source_probe_job.py /app/", dockerfile)
        self.assertIn("COPY scripts/quadringent_table_discovery_job.py /app/", dockerfile)
        # Les deux scripts importent quadringent_control_plane(.v2...) — déjà
        # copié en entier (COPY src/quadringent_control_plane /app/quadringent_control_plane),
        # jamais un sous-ensemble qui romprait silencieusement l'import.
        self.assertIn("COPY src/quadringent_control_plane /app/quadringent_control_plane", dockerfile)

    def test_chart_renders_a_loopback_control_plane_with_a_dedicated_identity(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        documents = rendered.split("\n---\n")
        deployment = next(
            document
            for document in documents
            if "kind: Deployment" in document
            and "name: cdc-quadringent-control-plane" in document
        )
        # Identités IRSA transitoires : les rôles IAM du site portent encore
        # les noms historiques cdcforge-* en attendant leur reprovisionnement.
        service_account = next(
            document
            for document in documents
            if "kind: ServiceAccount" in document
            and "name: cdcforge-control-plane" in document
        )

        self.assertIn("replicas: 1", deployment)
        self.assertIn("- /app/quadringent_control_plane.py", deployment)
        self.assertNotIn("--allow-non-loopback", deployment)
        self.assertIn('- --host\n            - "127.0.0.1"', deployment)
        # Source unique : la preuve combinée du flux flotte — controlPlane.source
        # et launch.fleetConsoleSource doivent coïncider, sinon le pod démarre
        # avec deux sources équivalentes et refuse de monter.
        self.assertIn(
            "live:example-corp:s3://example-corp-000000000000-int-example-corp-raw/"
            "as400/sales/fleet/console-proof.json",
            deployment,
        )
        self.assertNotIn("live:dev-sale", deployment)
        self.assertIn("serviceAccountName: cdcforge-control-plane", deployment)
        self.assertIn("automountServiceAccountToken: false", deployment)
        # Le lecteur de preuves ne reçoit pas le mot de passe source.
        self.assertNotIn("ISERIES_PASSWORD", deployment)
        self.assertNotIn("kind: Service\n", rendered)
        self.assertIn("automountServiceAccountToken: false", service_account)
        self.assertIn(
            "eks.amazonaws.com/role-arn: "
            "\"arn:aws:iam::000000000000:role/"
            "example-platform-int-cdcforge-control-plane\"",
            service_account,
        )

    def test_chart_rejects_a_control_plane_source_outside_sale_dev_proofs(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "controlPlane.source=live:forbidden:s3://forbidden/OTHER_SCHEMA/proof.json",
            ],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("preuve déclarée du site", rendered.stderr)

    def test_continuous_entrypoint_exposes_the_error_circuit_breaker(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/as400_continuous_capture.py",
                "--help",
            ],
            env={"PYTHONPATH": "src"},
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--max-consecutive-errors", result.stdout)

    def test_chart_renders_one_bounded_pilot_job_with_the_deployment_stopped(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "replicaCount=0",
                "--set",
                "pilot.enabled=true",
                "--set",
                "pilot.runId=tdd",
                "--set",
                "pilot.maxSeconds=7200",
                "--set",
                "bootstrap.mode=tail",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("kind: Deployment", rendered)
        self.assertIn("replicas: 0", rendered)
        self.assertIn("kind: Job", rendered)
        self.assertIn("name: cdc-quadringent-pilot-tdd", rendered)
        self.assertIn("activeDeadlineSeconds: 7260", rendered)
        self.assertIn("backoffLimit: 0", rendered)
        self.assertIn("restartPolicy: Never", rendered)
        self.assertIn("- --max-seconds\n            - \"7200\"", rendered)
        self.assertIn("- --max-consecutive-errors\n            - \"3\"", rendered)
        self.assertIn('AS400_BOOTSTRAP_RECEIVER: "__TAIL__"', rendered)
        self.assertEqual(rendered.count("kind: Job"), 1)

    def test_chart_refuses_a_pilot_while_the_permanent_reader_is_enabled(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "replicaCount=1",
                "--set",
                "pilot.enabled=true",
                "--set",
                "pilot.runId=unsafe",
            ],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn(
            "pilot.enabled exige replicaCount=0",
            rendered.stderr,
        )

    def test_coexistence_monitor_exposes_exact_pilot_targets(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/monitor_coexistence.py", "--help"],
            env={"PYTHONPATH": "src"},
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--job-name", result.stdout)
        self.assertIn("--job-namespace", result.stdout)
        self.assertIn("--popsink-namespace", result.stdout)
        self.assertIn("--baseline-file", result.stdout)

    def test_destination_sync_cli_stays_dry_run_and_sale_scoped(self) -> None:
        result = subprocess.run(
            [
                "python3",
                "scripts/quadringent_destination_sync.py",
                "--help",
            ],
            env={**os.environ, "PYTHONPATH": "src"},
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--all-jsonl", result.stdout)
        self.assertIn("--object-keys-file", result.stdout)
        self.assertIn("--execute", result.stdout)
        self.assertIn("ne lit pas IBM i", result.stdout)

    def test_chart_is_hard_locked_to_the_dev_namespace(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo-prod",
                "-f",
                "infra-values/values-int.yaml",
            ],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("site.namespace", rendered.stderr)

    def test_chart_refuses_any_production_promotion_flag(self) -> None:
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "cdc",
                "chart",
                "--namespace",
                "quadringent-demo",
                "-f",
                "infra-values/values-int.yaml",
                "--set",
                "deployment.environment=prod",
            ],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(rendered.returncode, 0)
        self.assertTrue(
            "deployment.environment doit rester un environnement non productif" in rendered.stderr
            or "/deployment/environment" in rendered.stderr,
            rendered.stderr,
        )


def test_release_sarif_contains_only_vulnerability_findings() -> None:
    steps = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]["steps"]
    sarif_scans = [step["with"] for step in steps if step.get("with", {}).get("format") == "sarif"]
    assert len(sarif_scans) == 3
    assert all(set(scan["scanners"].split(",")) == {"vuln"} for scan in sarif_scans)


@pytest.mark.parametrize("secret_scan_fails", (False, True))
def test_release_secret_reports_stay_private_and_detection_stops_gate(tmp_path, secret_scan_fails) -> None:
    steps = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]["steps"]
    gate = next(step["run"] for step in steps if "--scanners secret" in step.get("run", ""))
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner-temp"
    tools = tmp_path / "bin"
    for directory in (workspace, runner_temp, tools):
        directory.mkdir()
    workspace.joinpath("trivy-capture.sarif").write_text("{}")
    workspace.joinpath("images.json").write_text("{}")
    trivy = tools / "trivy"
    trivy.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys
args = sys.argv[1:]
scanner = args[args.index('--scanners') + 1]
platform = args[args.index('--platform') + 1] if '--platform' in args else 'all-layers'
output = Path(args[args.index('--output') + 1])
output.write_text(json.dumps({'scanner': scanner, 'platform': platform}))
sys.exit(37 if scanner == 'secret' and os.environ['SECRET_SCAN_FAILS'] == 'true' else 0)
""")
    trivy.chmod(0o700)
    summary = runner_temp / "summary"
    result = subprocess.run(
        ["bash", "-e", "-c", gate], cwd=workspace, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "RUNNER_TEMP": str(runner_temp),
             "GITHUB_STEP_SUMMARY": str(summary), "SECRET_SCAN_FAILS": str(secret_scan_fails).lower(),
             "CAPTURE_IMAGE": "test-capture", "CONTROL_PLANE_IMAGE": "test-control-plane",
             "VERIFIER_IMAGE": "test-verifier"},
    )
    reports = list(runner_temp.glob("*.json"))
    assert reports
    if secret_scan_fails:
        assert result.returncode == 37
        assert not summary.exists()
    else:
        assert result.returncode == 0, result.stderr
        secrets = [json.loads(path.read_text()) for path in reports if "secrets" in path.name]
        assert len(secrets) == 9
        assert {report["platform"] for report in secrets} == {"linux/amd64", "linux/arm64", "all-layers"}
        assert all(report["scanner"] == "secret" for report in secrets)
        assert summary.exists()
    uploads = [step["with"]["path"] for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
    uploaded = {
        Path(path).resolve() for patterns in uploads for pattern in patterns.splitlines()
        for path in glob.glob(str(workspace / pattern.replace("${RUNNER_TEMP}", str(runner_temp))
                                  .replace("$RUNNER_TEMP", str(runner_temp))), recursive=True)
    }
    assert workspace.joinpath("trivy-capture.sarif").resolve() in uploaded
    assert workspace.joinpath("images.json").resolve() in uploaded
    assert not uploaded.intersection(path.resolve() for path in reports)


@pytest.mark.parametrize("source_identity_fails,scan_exit", ((True, 0), (False, 37), (False, 0)))
def test_pages_checks_source_and_generated_output_before_upload(tmp_path, source_identity_fails, scan_exit) -> None:
    jobs = yaml.safe_load(Path(".github/workflows/pages.yml").read_text())["jobs"]
    pages = jobs["pages"]
    dependencies = pages.get("needs", [])
    if isinstance(dependencies, str):
        dependencies = [dependencies]
    assert dependencies == ["checks"]
    checks = jobs["checks"]
    assert "uses" not in checks
    assert checks["runs-on"] == "ubuntu-latest"
    assert checks["timeout-minutes"] == 5
    assert checks["permissions"] == {"contents": "read", "actions": "read"}
    check_steps = checks["steps"]
    assert check_steps[0]["uses"] == "actions/checkout@v4"
    assert check_steps[1]["uses"] == "actions/setup-python@v5"
    assert check_steps[1]["with"]["python-version"] == "3.12"
    ci_gate = check_steps[2]
    assert len(check_steps) == 3
    assert ci_gate["run"] == (
        'python scripts/check_release_ci.py --repository "$GITHUB_REPOSITORY" --sha "$GITHUB_SHA"'
    )
    assert ci_gate["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "if" not in ci_gate and not ci_gate.get("continue-on-error", False)
    assert "GH_TOKEN" not in checks.get("env", {})
    assert all("GH_TOKEN" not in step.get("env", {}) for step in check_steps[:2])
    assert pages["environment"] == "publication-approved"
    assert "PUBLICATION_APPROVED" in pages["if"]
    steps = pages["steps"]
    upload_index = next(index for index, step in enumerate(steps)
                        if step.get("uses", "").startswith("actions/upload-pages-artifact@"))
    upload = steps[upload_index]
    assert upload["with"]["path"] == "_site"
    assert "if" not in upload
    commands = [step["run"] for step in steps[:upload_index]
                if "run" in step and any(command in step["run"]
                                          for command in ("check_publication.py", "build_site.py", "gitleaks dir"))]
    assert any("check_publication.py" in command for command in commands)
    assert any("gitleaks dir" in command for command in commands)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check_publication.py", "build_site.py"):
        shutil.copy2(Path("scripts") / name, scripts / name)
    site = tmp_path / "site"
    site.mkdir()
    site.joinpath("index.html").write_text("<html>" + (
        ".".join(("10", "1", "2", "3")) if source_identity_fails else "Exemple synthétique"
    ) + "</html>")
    # Les doubles d'outils locaux ne font pas partie de la source publiée.
    tmp_path.joinpath(".gitignore").write_text("/bin/\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "scripts", "site", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Tests", "-c", "user.email=tests@example.test",
                    "commit", "-qm", "Source synthétique"], cwd=tmp_path, check=True, capture_output=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    scanner = tools / "gitleaks"
    scanner.write_text(f"#!{sys.executable}\n" + """
from pathlib import Path
import os
import sys
assert sys.argv[1:3] == ['dir', '_site']
assert Path('_site/index.html').is_file()
Path('scan-ran').touch()
sys.exit(int(os.environ['SCAN_EXIT']))
""")
    scanner.chmod(0o700)
    marker = tmp_path / "would-upload"
    result = subprocess.run(
        ["bash", "-e", "-c", "\n".join(commands) + "\ntouch would-upload"],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tools}:{Path(sys.executable).parent}:{os.environ['PATH']}",
             "REPOSITORY_URL": "https://github.com/example/quadringent", "SCAN_EXIT": str(scan_exit)},
    )
    if source_identity_fails:
        assert result.returncode == 1, result.stderr
        assert not tmp_path.joinpath("_site").exists()
        assert not tmp_path.joinpath("scan-ran").exists()
    elif scan_exit:
        assert result.returncode == scan_exit, result.stderr
        assert tmp_path.joinpath("scan-ran").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert marker.exists()
    if source_identity_fails or scan_exit:
        assert not marker.exists()
