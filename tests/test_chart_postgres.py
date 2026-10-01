"""Composant Postgres embarqué (chart/templates/postgres.yaml).

postgres.enabled vaut true par défaut (décision produit « un chart pour les
deux modes ») : ce module vérifie le rendu du StatefulSet/Secret/Service/
NetworkPolicy, l'exclusivité mutuelle avec externalDatabase, et une
comparaison dorée confirmant que infra-values/values-int.yaml (qui déclare
explicitement postgres.enabled=false, migration v2 non planifiée pour ce
site) ne rend plus aucune ressource Postgres.
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


class PostgresComponentTests(unittest.TestCase):
    def test_golden_int_values_render_without_any_postgres_resource(self) -> None:
        """example-corp (INT) déclare explicitement postgres.enabled=false :
        aucune régression du rendu existant malgré le nouveau défaut true."""

        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("kind: StatefulSet", result.stdout)
        self.assertNotIn("cdc-quadringent-postgres", result.stdout)

    def test_postgres_enabled_renders_statefulset_secret_and_service(self) -> None:
        result = render("postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kind: StatefulSet", result.stdout)
        self.assertIn("name: cdc-quadringent-postgres\n", result.stdout)
        self.assertIn("name: cdc-quadringent-postgres-credentials", result.stdout)
        self.assertIn("helm.sh/resource-policy: keep", result.stdout)
        self.assertIn('image: docker.io/library/postgres@sha256:', result.stdout)

    def test_postgres_password_is_generated_when_no_secret_exists(self) -> None:
        """`helm template` n'a jamais accès à un cluster réel : `lookup`
        retourne toujours {} hors ligne, donc une nouvelle valeur est
        toujours générée dans ce chemin de test (branche « génère »). Le
        contrat de conservation (`lookup` + `resource-policy: keep`)
        n'est exercé que par un vrai `helm install`/`upgrade`."""

        first = render("postgres.enabled=true")
        second = render("postgres.enabled=true")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

        def password(output: str) -> str:
            for line in output.splitlines():
                if line.strip().startswith("POSTGRES_PASSWORD:"):
                    return line.strip()
            raise AssertionError("POSTGRES_PASSWORD absent du rendu")

        # Deux rendus indépendants génèrent des mots de passe distincts,
        # faute de secret existant à lire hors ligne.
        self.assertNotEqual(password(first.stdout), password(second.stdout))

    def test_postgres_and_external_database_are_mutually_exclusive(self) -> None:
        result = render("postgres.enabled=true", "externalDatabase.url=postgresql://ext/db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutuellement exclusifs", result.stderr)

        result = render("postgres.enabled=true", "externalDatabase.existingSecret=ext-db-secret")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutuellement exclusifs", result.stderr)

    def test_postgres_disabled_with_external_database_declared_renders_cleanly(self) -> None:
        result = render("postgres.enabled=false", "externalDatabase.url=postgresql://ext/db")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("kind: StatefulSet", result.stdout)

    def test_network_policy_restricts_postgres_to_the_control_plane(self) -> None:
        result = render("postgres.enabled=true", "networkPolicy.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = result.stdout.split("\n---\n")
        policy = next(
            document
            for document in documents
            if "kind: NetworkPolicy" in document and "name: cdc-quadringent-postgres" in document
        )
        self.assertIn("app.kubernetes.io/component: control-plane", policy)
        self.assertIn("port: 5432", policy)
        self.assertNotIn("policyTypes:\n    - Ingress\n    - Egress", policy)

    def test_network_policy_absent_by_default(self) -> None:
        result = render("postgres.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("kind: NetworkPolicy", result.stdout)

    def test_backup_cronjob_requires_control_plane_enabled(self) -> None:
        result = render("postgres.enabled=true", "postgres.backup.enabled=true", "controlPlane.enabled=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("postgres.backup.enabled exige controlPlane.enabled", result.stderr)

    def test_backup_cronjob_renders_an_init_container_and_a_publish_container(self) -> None:
        result = render("postgres.enabled=true", "postgres.backup.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = result.stdout.split("\n---\n")
        cronjob = next(
            document
            for document in documents
            if "kind: CronJob" in document and "name: cdc-quadringent-postgres-backup" in document
        )
        self.assertIn("name: pg-dump", cronjob)
        self.assertIn("name: publish", cronjob)
        self.assertIn("quadringent_postgres_backup.py", cronjob)
        self.assertIn("--dump-file", cronjob)

    def test_invalid_image_digest_is_refused(self) -> None:
        result = render("postgres.enabled=true", "postgres.image.digest=sha256:not-a-real-digest")
        self.assertNotEqual(result.returncode, 0)
        # Refusé au niveau du schéma (pattern strict, comme le reste des
        # digests d'image de la chart) avant même d'atteindre la garde du
        # template — même contrat que image.digest/controlPlane.image.digest.
        self.assertIn("does not match pattern", result.stderr)


if __name__ == "__main__":
    unittest.main()
