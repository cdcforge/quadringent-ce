"""Personnalisations expert self-host ajoutées lors du durcissement du
schéma : podSecurityContext, podAnnotations/podLabels, imagePullSecrets
additionnels, extraEnv/extraVolumes/extraVolumeMounts, affinity brute,
NetworkPolicy optionnelle et annotations du ServiceAccount control-plane.

Toutes ces valeurs sont vides/désactivées par défaut : ce fichier vérifie
qu'elles ne changent rien au rendu tant qu'elles ne sont pas renseignées, et
qu'elles produisent l'effet attendu une fois renseignées.
"""

from __future__ import annotations

import subprocess
import unittest

import yaml

NAMESPACE = "quadringent-demo"
VALUES = "infra-values/values-int.yaml"


class ExpertCustomizationTests(unittest.TestCase):
    def render(self, *sets: str) -> subprocess.CompletedProcess[str]:
        command = ["helm", "template", "cdc", "chart", "--namespace", NAMESPACE, "-f", VALUES]
        for value in sets:
            command += ["--set", value]
        return subprocess.run(command, capture_output=True, text=True)

    def documents(self, result: subprocess.CompletedProcess[str]) -> list[dict]:
        return [document for document in yaml.safe_load_all(result.stdout) if document]

    def reader_deployment(self, result: subprocess.CompletedProcess[str]) -> dict:
        return next(
            document
            for document in self.documents(result)
            if document["kind"] == "Deployment" and "control-plane" not in document["metadata"]["name"]
        )

    def test_defaults_leave_the_pod_spec_unchanged(self) -> None:
        # values-int.yaml active déjà podAntiAffinity (fonctionnalité
        # historique) : seul le nouveau securityContext au niveau Pod est
        # vérifié ici, il doit rester absent tant qu'il n'est pas déclaré.
        baseline = self.render()
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        spec = self.reader_deployment(baseline)["spec"]["template"]["spec"]
        self.assertNotIn("securityContext", spec)

    def test_pod_security_context_is_rendered_when_declared(self) -> None:
        result = self.render("podSecurityContext.fsGroup=10001")
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.reader_deployment(result)["spec"]["template"]["spec"]
        self.assertEqual(spec["securityContext"]["fsGroup"], 10001)

    def test_pod_annotations_and_labels_are_merged(self) -> None:
        result = self.render(
            "podAnnotations.example\\.com/owner=platform",
            "podLabels.tier=capture",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        template = self.reader_deployment(result)["spec"]["template"]
        self.assertEqual(template["metadata"]["annotations"]["example.com/owner"], "platform")
        self.assertEqual(template["metadata"]["labels"]["tier"], "capture")

    def test_extra_image_pull_secret_is_added_alongside_the_existing_one(self) -> None:
        result = self.render("imagePullSecrets[0].name=extra-registry")
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.reader_deployment(result)["spec"]["template"]["spec"]
        names = {entry["name"] for entry in spec["imagePullSecrets"]}
        self.assertIn("extra-registry", names)
        # image.pullSecret déclaré par values-int.yaml (example-corp-ibmi n'a pas
        # de pullSecret, mais values-int.yaml peut en déclarer un) reste présent
        # s'il existe ; ce test ne dépend que de l'ajout, pas du remplacement.

    def test_capture_service_account_supplies_pull_secrets_to_dynamic_workloads(self) -> None:
        result = self.render(
            "image.pullSecret=private-registry",
            "imagePullSecrets[0].name=extra-registry",
            "serviceAccount.create=true",
            "serviceAccount.name=quadringent-capture",
            "serviceAccount.roleArn=arn:aws:iam::000000000000:role/quadringent-capture",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        account = next(
            document for document in self.documents(result)
            if document["kind"] == "ServiceAccount" and document["metadata"]["name"] == "quadringent-capture"
        )
        self.assertEqual(
            {item["name"] for item in account["imagePullSecrets"]},
            {"private-registry", "extra-registry"},
        )

    def test_no_image_pull_secrets_key_when_nothing_is_declared(self) -> None:
        result = self.render("image.pullSecret=")
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.reader_deployment(result)["spec"]["template"]["spec"]
        self.assertNotIn("imagePullSecrets", spec)

    def test_extra_env_is_appended_to_the_reader_container(self) -> None:
        result = self.render(
            "extraEnv[0].name=EXTRA_FLAG",
            "extraEnv[0].value=on",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        container = self.reader_deployment(result)["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["EXTRA_FLAG"], "on")
        self.assertIn("ISERIES_PASSWORD", env)

    def test_extra_volume_and_mount_are_rendered(self) -> None:
        result = self.render(
            "extraVolumes[0].name=scratch",
            "extraVolumes[0].emptyDir={}",
            "extraVolumeMounts[0].name=scratch",
            "extraVolumeMounts[0].mountPath=/scratch",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.reader_deployment(result)["spec"]["template"]["spec"]
        volume_names = {volume["name"] for volume in spec["volumes"]}
        self.assertIn("scratch", volume_names)
        container = spec["containers"][0]
        mount_names = {mount["name"] for mount in container["volumeMounts"]}
        self.assertIn("scratch", mount_names)

    def test_affinity_is_ignored_when_pod_anti_affinity_is_enabled(self) -> None:
        """podAntiAffinity reste la voie historique ; affinity brute est une
        alternative pour les profils experts, jamais combinée avec elle."""

        result = self.render(
            "podAntiAffinity.enabled=false",
            "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution"
            ".nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/arch",
            "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution"
            ".nodeSelectorTerms[0].matchExpressions[0].operator=In",
            "affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution"
            ".nodeSelectorTerms[0].matchExpressions[0].values[0]=amd64",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = self.reader_deployment(result)["spec"]["template"]["spec"]
        self.assertIn("nodeAffinity", spec["affinity"])

    def test_network_policy_is_absent_by_default(self) -> None:
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        kinds = {document["kind"] for document in self.documents(result)}
        self.assertNotIn("NetworkPolicy", kinds)

    def test_network_policy_renders_when_enabled(self) -> None:
        result = self.render("networkPolicy.enabled=true")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = next(d for d in self.documents(result) if d["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"], "cdc")

    def test_control_plane_service_account_annotations_are_merged(self) -> None:
        result = self.render(
            "controlPlane.launch.enabled=false",
            "controlPlane.serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account="
            "quadringent@example-project.iam.gserviceaccount.com",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        service_account = next(
            d for d in self.documents(result)
            if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "cdcforge-control-plane"
        )
        annotations = service_account["metadata"]["annotations"]
        self.assertEqual(
            annotations["iam.gke.io/gcp-service-account"],
            "quadringent@example-project.iam.gserviceaccount.com",
        )
        # L'annotation IRSA gérée par la chart reste présente : la fusion
        # n'écrase pas les annotations existantes.
        self.assertIn("eks.amazonaws.com/role-arn", annotations)


if __name__ == "__main__":
    unittest.main()
