"""Le RBAC de lancement de Jobs ne doit exister que lorsqu'il est demandé."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tomllib
import unittest

import yaml



VALUES = "infra-values/values-int.yaml"
TEMPLATE = json.dumps(
    {
        "format_version": "quadringent-job-template-v1",
        "container": "capture",
        "pod": {
            "restartPolicy": "Never",
            "serviceAccountName": "as400-snowflake-capture",
            "containers": [{"name": "capture"}],
        },
        "backoff_limit": 0,
    }
)
CATALOG = json.dumps({"format_version": "quadringent-fleet-catalog-v1"})
SIDECAR = json.dumps({"format_version": "quadringent-fleet-ui-sidecar-v1"})


class ControlPlaneJobsChartTests(unittest.TestCase):
    def render(self, *values: str) -> subprocess.CompletedProcess[str]:
        command = [
            "helm",
            "template",
            "cdc",
            "chart",
            "--namespace",
            "quadringent-demo",
            "-f",
            VALUES,
        ]
        for value in values:
            command += ["--set", value]
        return subprocess.run(command, capture_output=True, text=True)

    def render_launch(self, *values: str) -> subprocess.CompletedProcess[str]:
        command = [
            "helm",
            "template",
            "cdc",
            "chart",
            "--namespace",
            "quadringent-demo",
            "-f",
            VALUES,
            "--set",
            "controlPlane.launch.enabled=true",
            "--set-json",
            f"controlPlane.launch.jobTemplate={json.dumps(TEMPLATE)}",
            "--set-json",
            f"controlPlane.launch.fleetCatalog={json.dumps(CATALOG)}",
            "--set-json",
            f"controlPlane.launch.fleetSidecar={json.dumps(SIDECAR)}",
        ]
        for value in values:
            command += ["--set", value]
        return subprocess.run(command, capture_output=True, text=True)

    def documents(self, result: subprocess.CompletedProcess[str]) -> list[dict]:
        return [document for document in yaml.safe_load_all(result.stdout) if document]

    def test_les_couts_exigent_le_fichier_du_pvc_persistant(self) -> None:
        source = "controlPlane.infrastructureCostsSource=file:///var/lib/quadringent-fleet/infrastructure-costs.json"
        refused = self.render_launch(source, "controlPlane.fleetState.persistence.enabled=false")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("PVC flotte persistant", refused.stderr)
        refused = self.render_launch("controlPlane.infrastructureCostsSource=https://example.invalid/costs.json")
        self.assertNotEqual(refused.returncode, 0)
        result = self.render_launch(source, "controlPlane.fleetState.persistence.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = self.documents(result)
        deployment = next(d for d in documents if d['kind']=='Deployment' and d['metadata']['name'].endswith('control-plane'))
        args = deployment['spec']['template']['spec']['containers'][0]['args']
        self.assertEqual(args[args.index('--infrastructure-costs-source')+1], source.split('=',1)[1])
        role = next(d for d in documents if d['kind']=='Role')
        self.assertEqual(role['rules'], [{'apiGroups':['batch'],'resources':['jobs'],'verbs':['create','get','patch']}])

    def test_launch_disabled_keeps_read_only_identity(self) -> None:
        result = self.render("controlPlane.launch.enabled=false")

        self.assertEqual(result.returncode, 0, result.stderr)
        kinds = [document["kind"] for document in self.documents(result)]
        self.assertNotIn("Role", kinds)
        self.assertNotIn("RoleBinding", kinds)
        for document in self.documents(result):
            if document["kind"] in {"ServiceAccount", "Deployment"}:
                self.assertFalse(document.get("automountServiceAccountToken", False))
            if document["kind"] == "Deployment":
                self.assertFalse(
                    document["spec"]["template"]["spec"].get("automountServiceAccountToken", False)
                )

    def test_launch_enabled_grants_jobs_create_and_get_only(self) -> None:
        result = self.render_launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        documents = self.documents(result)
        roles = [document for document in documents if document["kind"] == "Role"]
        bindings = [document for document in documents if document["kind"] == "RoleBinding"]
        self.assertEqual(len(roles), 1)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(
            roles[0]["rules"],
            [{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "patch"]}],
        )
        self.assertNotIn("delete", roles[0]["rules"][0]["verbs"])
        self.assertNotIn("deletecollection", roles[0]["rules"][0]["verbs"])
        service_account = next(
            document for document in documents if document["kind"] == "ServiceAccount"
        )
        self.assertTrue(service_account["automountServiceAccountToken"])
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        self.assertTrue(deployment["spec"]["template"]["spec"]["automountServiceAccountToken"])
        self.assertEqual(
            bindings[0]["subjects"][0]["name"],
            service_account["metadata"]["name"],
        )
        self.assertEqual(bindings[0]["roleRef"]["kind"], "Role")

    def test_launch_requires_a_job_template(self) -> None:
        result = self.render("controlPlane.launch.enabled=true")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("jobTemplate", result.stderr)

    def test_launch_is_refused_outside_the_dev_namespace(self) -> None:
        command = [
            "helm",
            "template",
            "cdc",
            "chart",
            "--namespace",
            "default",
            "-f",
            VALUES,
            "--set",
            "controlPlane.launch.enabled=true",
            "--set-json",
            f"controlPlane.launch.jobTemplate={json.dumps(TEMPLATE)}",
            "--set-json",
            f"controlPlane.launch.fleetCatalog={json.dumps(CATALOG)}",
            "--set-json",
            f"controlPlane.launch.fleetSidecar={json.dumps(SIDECAR)}",
        ]
        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)

    def test_launch_requires_a_fleet_catalog(self) -> None:
        command = [
            "helm",
            "template",
            "cdc",
            "chart",
            "--namespace",
            "quadringent-demo",
            "-f",
            VALUES,
            "--set",
            "controlPlane.launch.enabled=true",
            "--set-json",
            f"controlPlane.launch.jobTemplate={json.dumps(TEMPLATE)}",
        ]
        result = subprocess.run(command, capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fleetCatalog", result.stderr)

    def test_control_plane_registry_must_be_allow_listed(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.image.repository=registry.example.com/mirror/quadringent",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allowedRepositories", result.stderr)

    def test_control_plane_registry_accepts_a_site_mirror(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.image.repository=registry.example.com/mirror/quadringent",
            "controlPlane.image.allowedRepositories[1]=registry.example.com/mirror/quadringent",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document
            for document in self.documents(result)
            if document["kind"] == "Deployment"
        )
        self.assertTrue(
            deployment["spec"]["template"]["spec"]["containers"][0]["image"].startswith(
                "registry.example.com/mirror/quadringent@sha256:"
            )
        )

    def test_launch_wires_the_fleet_executor(self) -> None:
        result = self.render_launch()

        self.assertEqual(result.returncode, 0, result.stderr)
        documents = self.documents(result)
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        args = container["args"]
        for flag in (
            "--fleet-catalog",
            "--fleet-state-dir",
            "--fleet-job-template",
            "--fleet-raw-prefix-root",
        ):
            self.assertIn(flag, args)
        mounts = {mount["mountPath"] for mount in container.get("volumeMounts", [])}
        self.assertIn("/etc/quadringent-launch", mounts)
        self.assertIn("/var/lib/quadringent-fleet", mounts)
        env_from = [
            ref["configMapRef"]["name"]
            for ref in container["envFrom"]
            if "configMapRef" in ref
        ]
        self.assertIn("cdc-quadringent-tuning", env_from)
        env_names = {entry["name"] for entry in container["env"]}
        self.assertIn("ISERIES_PASSWORD", env_names)
        secret_env = next(
            entry for entry in container["env"] if entry["name"] == "ISERIES_PASSWORD"
        )
        self.assertIn("secretKeyRef", secret_env["valueFrom"])

    def test_auth_requires_operator_or_admin_groups(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.auth.enabled=true",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("operatorGroups ou adminGroups", result.stderr)

    def test_public_host_requires_auth(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.host=0.0.0.0",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("auth.enabled", result.stderr)

    def test_auth_wires_trusted_headers_and_public_host(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false", "controlPlane.auth.enabled=true",
            "controlPlane.auth.operatorGroups={ops}", "controlPlane.auth.adminGroups={admins}",
            "controlPlane.host=0.0.0.0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(d for d in self.documents(result) if d["kind"] == "Deployment")
        args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        for flag in ("--auth-user-header", "--auth-groups-header", "--auth-operator-groups", "--auth-admin-groups"):
            self.assertIn(flag, args)
        self.assertIn("0.0.0.0", args)
        self.assertNotIn("--license-key", args)
        self.assertEqual(args[args.index("--auth-operator-groups") + 1], "ops")

    def test_auth_disabled_keeps_loopback_and_no_license_arg(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.licenseKey=",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document
            for document in self.documents(result)
            if document["kind"] == "Deployment"
        )
        args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("127.0.0.1", args)
        self.assertNotIn("--license-key", args)
        self.assertNotIn("--auth-user-header", args)

    def test_aucun_secret_de_licence_n_est_requis(self) -> None:
        # Une ancienne référence est tolérée sans être injectée dans le pod.
        for overrides in ((), ("controlPlane.licenseSecret.name=ancienne-reference", "controlPlane.licenseSecret.key=license")):
            result = self.render("controlPlane.launch.enabled=false", *overrides)
            self.assertEqual(result.returncode, 0, result.stderr)
            deployment = next(d for d in self.documents(result) if d["kind"] == "Deployment")
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            self.assertNotIn("--license-key", container["args"])
            self.assertNotIn("QUADRINGENT_LICENSE_KEY", {item["name"] for item in container["env"]})
            self.assertNotIn("ancienne-reference", json.dumps(container))

    def test_les_values_ne_portent_plus_de_licence_signee(self) -> None:
        values = yaml.safe_load(Path(VALUES).read_text(encoding="utf-8"))
        self.assertFalse(values["controlPlane"].get("licenseKey"))
        self.assertNotIn("quadringent1.", Path(VALUES).read_text())

    def test_une_licence_en_clair_est_refusee(self) -> None:
        result = self.render("controlPlane.licenseKey=ne-pas-publier")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aucune licence commerciale requise", result.stderr)

    def test_auth_proxy_secret_comes_from_a_secret_key_ref(self) -> None:
        """Le secret partagé proxy ne traverse jamais values.yaml en clair."""

        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.auth.enabled=true",
            "controlPlane.auth.operatorGroups=ops",
            "controlPlane.auth.proxySecret.secretName=quadringent-proxy",
            "controlPlane.auth.proxySecret.secretKey=shared-secret",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document
            for document in self.documents(result)
            if document["kind"] == "Deployment"
        )
        env = {
            entry["name"]: entry
            for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        secret_ref = env["QUADRINGENT_AUTH_PROXY_SECRET"]["valueFrom"]["secretKeyRef"]
        self.assertEqual(secret_ref["name"], "quadringent-proxy")
        self.assertEqual(secret_ref["key"], "shared-secret")
        self.assertNotIn("value", env["QUADRINGENT_AUTH_PROXY_SECRET"])

    def test_telemetry_state_gets_a_writable_volume(self) -> None:
        """L'identifiant d'installation haché se persiste : un emptyDir monté
        sur le state_dir, sinon chaque ping apparaît comme une installation."""

        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.telemetry.enabled=true",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document
            for document in self.documents(result)
            if document["kind"] == "Deployment"
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        env = {entry["name"]: entry.get("value") for entry in pod_spec["containers"][0]["env"]}
        state_dir = env["QUADRINGENT_STATE_DIR"]
        mounts = {m["mountPath"] for m in pod_spec["containers"][0].get("volumeMounts", [])}
        self.assertIn(state_dir, mounts)

    def test_telemetry_is_opt_in_and_version_is_pinned(self) -> None:
        """La télémétrie n'émet que si le site l'active ; la version suit la chart."""
        off = self.render("controlPlane.launch.enabled=false")
        self.assertEqual(off.returncode, 0, off.stderr)
        deployment = next(
            document
            for document in self.documents(off)
            if document["kind"] == "Deployment"
        )
        env = {entry["name"]: entry.get("value")
               for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertNotIn("QUADRINGENT_TELEMETRY", env)
        version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(env["QUADRINGENT_VERSION"], version)

        on = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.telemetry.enabled=true",
            "controlPlane.telemetry.endpoint=https://collector.example/v1",
        )
        self.assertEqual(on.returncode, 0, on.stderr)
        deployment = next(
            document
            for document in self.documents(on)
            if document["kind"] == "Deployment"
        )
        env = {entry["name"]: entry.get("value")
               for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["QUADRINGENT_TELEMETRY"], "on")
        self.assertEqual(env["QUADRINGENT_TELEMETRY_URL"], "https://collector.example/v1")

    def test_fleet_proof_targets_the_persistent_sidecar(self) -> None:
        """Avec le PVC d'état, la preuve flotte lit le sidecar régénéré par le
        relevé catalogue — pas la graine figée du ConfigMap de lancement."""

        result = self.render_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document for document in self.documents(result) if document["kind"] == "Deployment"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        args = container["args"]
        proof = args[args.index("--fleet-proof") + 1]
        self.assertTrue(
            proof.endswith("=file:///var/lib/quadringent-fleet/fleet-sidecar.json"),
            proof,
        )
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        self.assertEqual(env["QUADRINGENT_FLEET_STATE_PERSISTENT"], "true")
        self.assertEqual(env["QUADRINGENT_CATALOG_REFRESH_SECONDS"], "300")
        volumes = {
            volume["name"]: volume
            for volume in deployment["spec"]["template"]["spec"]["volumes"]
        }
        self.assertIn("persistentVolumeClaim", volumes["fleet-state"])

    def test_fleet_state_volume_is_writable_by_the_container(self) -> None:
        """Le PVC provisionné est root-owned : sans fsGroup aligné sur le
        runAsGroup du conteneur, la graine et les reçus runtime échouent en
        Permission denied (régression observée au déploiement INT)."""

        result = self.render_launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document for document in self.documents(result) if document["kind"] == "Deployment"
        )
        pod_spec = deployment["spec"]["template"]["spec"]
        pod_security = pod_spec.get("securityContext") or {}
        container_security = pod_spec["containers"][0]["securityContext"]
        self.assertEqual(pod_security.get("fsGroup"), container_security["runAsGroup"])

    def test_fleet_proof_falls_back_to_launch_configmap_without_pvc(self) -> None:
        """Sans PVC, le sidecar reste celui du ConfigMap : le relevé n'a pas
        de destination durable et la capacité refresh reste indisponible."""

        result = self.render_launch("controlPlane.fleetState.persistence.enabled=false")
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(
            document for document in self.documents(result) if document["kind"] == "Deployment"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        args = container["args"]
        proof = args[args.index("--fleet-proof") + 1]
        self.assertTrue(
            proof.endswith("=file:///etc/quadringent-launch/fleet-sidecar.json"),
            proof,
        )
        env_names = {entry["name"] for entry in container["env"]}
        self.assertNotIn("QUADRINGENT_FLEET_STATE_PERSISTENT", env_names)
        self.assertNotIn("QUADRINGENT_CATALOG_REFRESH_SECONDS", env_names)
        volumes = {
            volume["name"]: volume
            for volume in deployment["spec"]["template"]["spec"]["volumes"]
        }
        self.assertIn("emptyDir", volumes["fleet-state"])

    def test_catalog_refresh_interval_is_bounded(self) -> None:
        for value in ("-1", "86401"):
            result = self.render_launch(f"controlPlane.catalogRefreshSeconds={value}")
            self.assertNotEqual(result.returncode, 0, value)
            self.assertIn("catalogRefreshSeconds", result.stderr)
        result = self.render_launch("controlPlane.catalogRefreshSeconds=0")
        self.assertEqual(result.returncode, 0, result.stderr)
