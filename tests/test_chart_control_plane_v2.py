"""Second conteneur control-plane-v2 (FastAPI/uvicorn) — même Pod que v1.

controlPlane.v2.enabled est désactivé par défaut : v1 seule reste le
comportement historique. Ce module vérifie le câblage du conteneur v2 avec
le Postgres embarqué et avec un Postgres externe, les secrets applicatifs
(SECRET_KEY/TOKEN_PEPPER, conservés entre upgrades), les probes /v2/healthz,
et les gardes d'exclusivité/configuration.
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


def render(*values: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "cdc", "chart", "--namespace", NAMESPACE, "-f", VALUES, *LAUNCH_FILES]
    for value in values:
        command += ["--set", value]
    return subprocess.run(command, capture_output=True, text=True)


class ControlPlaneV2Tests(unittest.TestCase):
    def test_disabled_by_default_v1_only_renders(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("control-plane-v2", result.stdout)
        self.assertNotIn("cdc-quadringent-v2-secrets", result.stdout)

    def test_requires_postgres_or_external_database(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "controlPlane.v2.enabled exige postgres.enabled=true ou externalDatabase.url/existingSecret",
            result.stderr,
        )

    def test_postgres_and_external_database_together_are_refused(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true", "postgres.enabled=true", "externalDatabase.url=postgresql://ext/db"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutuellement exclusifs", result.stderr)

    def test_v2_port_must_differ_from_v1_port(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true", "controlPlane.v2.port=8844")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("controlPlane.v2.port doit différer de controlPlane.port", result.stderr)

    def test_enabled_with_embedded_postgres_composes_the_dsn_at_startup(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: control-plane-v2", result.stdout)
        self.assertIn('QUADRINGENT_V2_DATABASE_URL="postgresql+psycopg://', result.stdout)
        self.assertIn("name: POSTGRES_HOST", result.stdout)
        self.assertIn("cdc-quadringent-postgres-credentials", result.stdout)
        self.assertIn("key: POSTGRES_PASSWORD", result.stdout)

    def test_enabled_with_external_database_url_passes_it_directly(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true",
            "postgres.enabled=false",
            "externalDatabase.url=postgresql+psycopg://user:pass@ext-host:5432/quadringent",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'value: "postgresql+psycopg://user:pass@ext-host:5432/quadringent"', result.stdout
        )
        self.assertNotIn("POSTGRES_HOST", result.stdout)

    def test_enabled_with_external_database_existing_secret_references_it(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true",
            "postgres.enabled=false",
            "externalDatabase.existingSecret=site-managed-db",
            "externalDatabase.existingSecretKey=dsn",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('name: "site-managed-db"', result.stdout)
        self.assertIn('key: "dsn"', result.stdout)

    def test_v2_secrets_are_generated_and_kept_across_upgrades(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = result.stdout.split("\n---\n")
        secret = next(
            document
            for document in documents
            if "kind: Secret" in document and "name: cdc-quadringent-v2-secrets" in document
        )
        self.assertIn("helm.sh/resource-policy: keep", secret)
        self.assertIn("SECRET_KEY:", secret)
        self.assertIn("TOKEN_PEPPER:", secret)

    def test_v2_healthz_probes_are_wired(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- /v2/healthz", result.stdout)  # sonde exec en loopback
        self.assertIn("containerPort: 8845", result.stdout)

    def test_poll_intervals_are_declared_for_dynamic_workers(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true",
                        "controlPlane.v2.readerPollSeconds=1", "controlPlane.v2.loaderPollSeconds=1",
                        "controlPlane.v2.loaderFlushEachBatch=true",
                        "controlPlane.v2.loaderHistoryMode=sql")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: QUADRINGENT_V2_READER_POLL_SECONDS", result.stdout)
        self.assertIn("name: QUADRINGENT_V2_LOADER_POLL_SECONDS", result.stdout)
        self.assertIn("name: QUADRINGENT_V2_LOADER_FLUSH_EACH_BATCH", result.stdout)
        self.assertIn("name: QUADRINGENT_V2_LOADER_HISTORY_MODE", result.stdout)

    def test_invalid_v2_image_digest_is_refused(self) -> None:
        result = render(
            "controlPlane.v2.enabled=true", "postgres.enabled=true", "controlPlane.v2.image.digest=sha256:bad"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_v1_container_receives_the_v2_upstream_port_when_v2_is_enabled(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        v1_container = result.stdout.split("- name: control-plane-v2")[0]
        self.assertIn("- --v2-upstream-port", v1_container)
        self.assertIn('- "8845"', v1_container)

    def test_v1_container_has_no_v2_upstream_port_when_v2_is_disabled(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--v2-upstream-port", result.stdout)

    # -- Exécuteur de pipeline v2 (chantier « pipeline-exec ») ----------------

    def test_v2_executor_env_vars_are_wired(self) -> None:
        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("QUADRINGENT_V2_CAPTURE_SERVICE_ACCOUNT", result.stdout)
        # infra-values/values-int.yaml déclare serviceAccount.name=as400-snowflake-capture.
        self.assertIn('value: "as400-snowflake-capture"', result.stdout)
        self.assertIn("QUADRINGENT_V2_READER_TIMEOUT_SECONDS", result.stdout)

    def test_v2_executor_rbac_covers_exactly_the_verbs_used(self) -> None:
        """Deployments : create/get/update/delete (``KubernetesDeploymentsClient``).
        Jobs : create/get/delete (copie initiale, rejeu). Secrets durables :
        create/get/patch (``upsert_secret`` — jamais delete, contrairement aux
        Secrets éphémères des Jobs de diagnostic)."""

        result = render("controlPlane.v2.enabled=true", "postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = result.stdout.split("\n---\n")
        role = next(
            document
            for document in documents
            if "kind: Role" in document and "control-plane-v2-executor" in document
        )
        self.assertIn('apiGroups: ["apps"]', role)
        self.assertIn('resources: ["deployments"]', role)
        self.assertIn('verbs: ["create", "get", "update", "delete"]', role)
        self.assertIn('resources: ["jobs"]', role)
        self.assertIn('resources: ["secrets"]', role)
        self.assertIn('verbs: ["create", "get", "patch"]', role)
        binding = next(
            document
            for document in documents
            if "kind: RoleBinding" in document and "control-plane-v2-executor" in document
        )
        self.assertIn("name: cdcforge-control-plane", binding)

    def test_v2_executor_rbac_absent_when_v2_disabled(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("control-plane-v2-executor", result.stdout)


if __name__ == "__main__":
    unittest.main()
