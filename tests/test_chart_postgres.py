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
import shutil
import unittest
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parents[1]

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
    return subprocess.run(command, capture_output=True, text=True, cwd=REPOSITORY)


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
        self.assertIn("--validation-file", cronjob)
        self.assertIn("pg_restore --file=/dev/null /backup/postgres.dump", cronjob)
        self.assertIn("sha256sum /backup/postgres.dump", cronjob)
        self.assertIn('command: ["sh", "-ec"]', cronjob)
        self.assertLess(cronjob.index("pg_restore --file=/dev/null"), cronjob.index("printf"))

    def test_backup_network_policy_matches_exact_release_labels(self) -> None:
        """L'accès PostgreSQL du backup reste lié au nom et à l'instance du site."""
        import yaml
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                result = render("postgres.enabled=true", "networkPolicy.enabled=true", "postgres.backup.enabled=" + str(enabled).lower())
                self.assertEqual(result.returncode, 0, result.stderr)
                documents = [item for item in yaml.safe_load_all(result.stdout) if item]
                policy = next(item for item in documents if item["kind"] == "NetworkPolicy" and item["metadata"]["name"].endswith("postgres"))
                selectors = [peer["podSelector"]["matchLabels"] for rule in policy["spec"]["ingress"] for peer in rule["from"]]
                backups = [labels for labels in selectors if labels.get("app.kubernetes.io/component") == "postgres-backup"]
                if not enabled:
                    self.assertEqual(backups, [])
                    self.assertEqual(len(selectors), 1)
                    continue
                self.assertEqual(backups, [{"app.kubernetes.io/name": "quadringent", "app.kubernetes.io/instance": "cdc", "app.kubernetes.io/component": "postgres-backup"}])
                job = next(item for item in documents if item["kind"] == "CronJob" and item["metadata"]["name"].endswith("postgres-backup"))
                labels = job["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
                for key, value in backups[0].items():
                    self.assertEqual(labels[key], value)
                self.assertEqual(policy["spec"]["ingress"][0]["ports"], [{"port": 5432, "protocol": "TCP"}])

    def test_invalid_image_digest_is_refused(self) -> None:
        result = render("postgres.enabled=true", "postgres.image.digest=sha256:not-a-real-digest")
        self.assertNotEqual(result.returncode, 0)
        # Refusé au niveau du schéma (pattern strict, comme le reste des
        # digests d'image de la chart) avant même d'atteindre la garde du
        # template — même contrat que image.digest/controlPlane.image.digest.
        self.assertIn("does not match pattern", result.stderr)


class PostgresClaimUpgradeTests(unittest.TestCase):
    """Le vrai lookup Helm lit une API locale, sans cluster ni credentials."""

    def render_existing(self, existing: dict | None, chart: str = "chart", forbidden: bool = False, expected_error: str | None = None) -> tuple[dict, list[str]]:
        paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                paths.append(path)
                payload: dict = {}
                code = 200
                if path == "/version":
                    payload = {"gitVersion": "v1.31.0", "major": "1", "minor": "31"}
                elif path == "/api":
                    payload = {"kind": "APIVersions", "versions": ["v1"]}
                elif path == "/apis":
                    payload = {"kind": "APIGroupList", "groups": [{"name": "apps", "versions": [{"groupVersion": "apps/v1", "version": "v1"}], "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"}}]}
                elif path in {"/api/v1", "/apis/apps/v1"}:
                    resources = [("secrets", "Secret"), ("configmaps", "ConfigMap"), ("services", "Service"), ("serviceaccounts", "ServiceAccount")] if path == "/api/v1" else [("statefulsets", "StatefulSet"), ("deployments", "Deployment")]
                    payload = {"kind": "APIResourceList", "groupVersion": "v1" if path == "/api/v1" else "apps/v1", "resources": [{"name": name, "kind": kind, "namespaced": True, "verbs": ["get", "list"]} for name, kind in resources]}
                elif path.endswith("/statefulsets/cdc-quadringent-postgres") and forbidden:
                    code = 403
                    payload = {"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "Forbidden", "message": "lookup forbidden", "code": 403}
                elif path.endswith("/statefulsets/cdc-quadringent-postgres") and existing is not None:
                    payload = existing
                else:
                    code = 404
                    payload = {"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "NotFound", "code": 404}
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory)/"kubeconfig"
                config.write_text(yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "clusters": [{"name": "local", "cluster": {"server": f"http://127.0.0.1:{server.server_port}"}}], "contexts": [{"name": "local", "context": {"cluster": "local", "user": "local"}}], "current-context": "local", "users": [{"name": "local", "user": {}}]}))
                config.chmod(0o600)
                command = ["helm", "template", "cdc", chart, "--namespace", NAMESPACE, "-f", VALUES, *LAUNCH_FILES, "--set", "postgres.enabled=true", "--set", "observability.enabled=false,fleetObserve.enabled=false", "--dry-run=server", "--disable-openapi-validation", "--kubeconfig", str(config)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=30, cwd=REPOSITORY)
                if expected_error is not None:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected_error, result.stderr)
                    return {}, paths
                self.assertEqual(result.returncode, 0, result.stderr)
                document = next(row for row in yaml.safe_load_all(result.stdout) if row and row["kind"] == "StatefulSet")
                return document, paths
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_existing_023_claim_labels_survive_upgrade_and_rollback(self) -> None:
        """03→05 conserve le template03 exact et le rendu rollback03 identique."""
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory)/"legacy"
            candidate = Path(directory)/"candidate"
            for chart in (legacy, candidate):
                shutil.copytree(REPOSITORY/"chart", chart)
            # Contrat historique03/04 : les labels généraux étaient utilisés dans
            # le template PVC. Fixture autonome, indépendante de Git/checkout shallow.
            original = (legacy/"templates"/"postgres.yaml").read_text()
            start = original.index("{{- /* Le template PVC est immutable.")
            end = original.index("{{- /* Génération une fois", start)
            original = original[:start] + original[end:]
            original = original.replace("        {{- toYaml $claimMetadata | nindent 8 }}", "        name: data\n        labels:\n          {{- include \"quadringent.labels\" . | nindent 10 }}\n          app.kubernetes.io/component: postgres")
            (legacy/"templates"/"postgres.yaml").write_text(original)
            for chart, version in ((legacy, "0.2.3"), (candidate, "0.2.5")):
                metadata = yaml.safe_load((chart/"Chart.yaml").read_text())
                metadata["version"] = metadata["appVersion"] = version
                (chart/"Chart.yaml").write_text(yaml.safe_dump(metadata))
            baseline, _ = self.render_existing(None, str(legacy))
            baseline["metadata"]["uid"] = "existing-statefulset-uid"
            baseline["metadata"]["annotations"] = {"meta.helm.sh/release-name": "cdc", "meta.helm.sh/release-namespace": NAMESPACE}
            upgraded, paths = self.render_existing(baseline, str(candidate))
            # Helm ajoute ces annotations aux ressources appliquées, pas au template.
            upgraded["metadata"]["annotations"] = baseline["metadata"]["annotations"]
            rollback, _ = self.render_existing(upgraded, str(legacy))
            self.assertIn("/apis/apps/v1/namespaces/" + NAMESPACE + "/statefulsets/cdc-quadringent-postgres", paths)
            for document in (upgraded, rollback):
                self.assertEqual(document["spec"]["volumeClaimTemplates"], baseline["spec"]["volumeClaimTemplates"])
                self.assertEqual(document["metadata"]["name"], baseline["metadata"]["name"])
                self.assertEqual(document["spec"]["selector"], baseline["spec"]["selector"])
            self.assertEqual(upgraded["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"], "0.2.5")
            self.assertEqual(rollback["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/version"], "0.2.3")

    def test_existing_024_annotations_and_labels_are_preserved(self) -> None:
        rendered, _ = self.render_existing(None)
        rendered["metadata"]["annotations"] = {"meta.helm.sh/release-name": "cdc", "meta.helm.sh/release-namespace": NAMESPACE}
        metadata = rendered["spec"]["volumeClaimTemplates"][0]["metadata"]
        metadata["labels"]["app.kubernetes.io/version"] = "0.2.4"
        metadata["annotations"] = {"example.org/retained": "original"}
        upgraded, _ = self.render_existing(rendered)
        self.assertEqual(upgraded["spec"]["volumeClaimTemplates"], rendered["spec"]["volumeClaimTemplates"])

    def test_statefulset_lookup_forbidden_refuses_upgrade(self) -> None:
        """Une permission manquante ne devient pas une nouvelle installation."""
        self.render_existing(None, forbidden=True, expected_error="lookup forbidden")

    def test_unexpected_existing_claim_refuses_upgrade(self) -> None:
        existing, _ = self.render_existing(None)
        existing["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] = "other"
        self.render_existing(existing, expected_error="template PVC data unique obligatoire")

    def test_new_claim_labels_are_independent_of_app_version(self) -> None:
        rendered, _ = self.render_existing(None)
        labels = rendered["spec"]["volumeClaimTemplates"][0]["metadata"]["labels"]
        self.assertNotIn("app.kubernetes.io/version", labels)
        self.assertEqual(labels["app.kubernetes.io/instance"], "cdc")
        self.assertEqual(labels["app.kubernetes.io/component"], "postgres")


if __name__ == "__main__":
    unittest.main()
