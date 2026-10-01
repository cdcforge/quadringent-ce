"""Validation de chart/values.schema.json.

Helm applique automatiquement values.schema.json au rendu (`helm template`
et `helm lint`) : ces tests s'appuient sur ce mécanisme natif plutôt que de
réimplémenter une validation JSON Schema en Python — la même version de
Helm que celle utilisée pour déployer le chart est ainsi la seule source de
vérité.

Trois familles de cas :
- les values par défaut du chart (site non déclaré) respectent le schéma ;
- chaque exemple synthétique de infra-values/ le respecte aussi ;
- un jeu de configurations invalides (typo, tag mouvant, digest malformé,
  promotion production, tuning négatif, mauvais type) est rejeté par le
  schéma, avec un message qui identifie le champ fautif.
"""

from __future__ import annotations

import subprocess
import unittest

NAMESPACE = "quadringent-demo"
VALUES_INT = "infra-values/values-int.yaml"

LAUNCH_FILES = (
    "--set-file", "controlPlane.launch.jobTemplate=infra-values/job-template-int.json",
    "--set-file", "controlPlane.launch.fleetCatalog=infra-values/fleet-catalog-int.json",
    "--set-file", "controlPlane.launch.fleetSidecar=infra-values/fleet-sidecar-int.json",
)

SCHEMA_ERROR_MARKER = "values don't meet the specifications of the schema"

# (nom lisible, values file ou None, arguments --set)
INVALID_CASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clé inconnue (typo)", ("image.digestt=sha256:" + "a" * 64,)),
    ("tag mouvant au lieu d'un digest", ("image.digest=latest",)),
    ("digest trop court", ("image.digest=sha256:abcdef",)),
    ("digest sans préfixe sha256", ("image.digest=" + "a" * 64,)),
    ("promotion production autorisée", ("deployment.productionPromotionAllowed=true",)),
    ("environnement hors liste", ("deployment.environment=prod",)),
    ("tuning négatif", ("tuning.batchEntries=-1",)),
    ("tuning à zéro", ("tuning.readerTimeoutSeconds=0",)),
    ("replicaCount de mauvais type", ("replicaCount=oops",)),
    ("replicaCount hors 0/1", ("replicaCount=2",)),
    ("safety.maxConsecutiveErrors hors borne", ("safety.maxConsecutiveErrors=4",)),
    ("port control-plane de mauvais type", ("controlPlane.port=oops",)),
    ("clé inconnue imbriquée", ("site.unknownField=x",)),
)


class ChartValuesSchemaTests(unittest.TestCase):
    def render(self, values_file: str | None, *sets: str) -> subprocess.CompletedProcess[str]:
        command = ["helm", "template", "cdc", "chart", "--namespace", NAMESPACE]
        if values_file:
            command += ["-f", values_file, *LAUNCH_FILES]
        for value in sets:
            command += ["--set", value]
        return subprocess.run(command, capture_output=True, text=True)

    def test_default_values_respect_the_schema(self) -> None:
        """Sans site déclaré, le rendu échoue sur la garde `required` du
        template (message dédié), jamais sur une violation du schéma : les
        types et motifs par défaut (chaînes vides, entiers, booléens) sont
        tous valides."""

        result = self.render(None)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SCHEMA_ERROR_MARKER, result.stderr, result.stderr)
        self.assertIn("site.namespace est obligatoire", result.stderr)

    def test_values_int_example_respects_the_schema(self) -> None:
        result = self.render(VALUES_INT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SCHEMA_ERROR_MARKER, result.stderr, result.stderr)

    def test_values_int_ecr_overlay_respects_the_schema(self) -> None:
        """values-int-ecr.yaml est un overlay partiel (images ECR) appliqué
        par-dessus values-int.yaml, jamais utilisé seul."""

        command = [
            "helm", "template", "cdc", "chart", "--namespace", NAMESPACE,
            "-f", VALUES_INT, "-f", "infra-values/values-int-ecr.yaml", *LAUNCH_FILES,
        ]
        overlay_result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(overlay_result.returncode, 0, overlay_result.stderr)
        self.assertNotIn(SCHEMA_ERROR_MARKER, overlay_result.stderr, overlay_result.stderr)

    def test_cdcforge_dev_nodepool_is_not_chart_values(self) -> None:
        """cdcforge-dev-nodepool.yaml est un manifeste Kubernetes brut
        (EC2NodeClass Karpenter), pas un fichier de values du chart : il est
        volontairement exclu de la validation par values.schema.json."""

        content = open("infra-values/cdcforge-dev-nodepool.yaml", encoding="utf-8").read()
        self.assertIn("kind: EC2NodeClass", content)

    def test_every_invalid_configuration_is_rejected_by_the_schema(self) -> None:
        for name, sets in INVALID_CASES:
            with self.subTest(case=name):
                result = self.render(VALUES_INT, *sets)
                self.assertNotEqual(result.returncode, 0, f"« {name} » aurait dû être refusé")
                self.assertIn(
                    SCHEMA_ERROR_MARKER,
                    result.stderr,
                    f"« {name} » n'a pas été refusé par values.schema.json : {result.stderr}",
                )

    def test_schema_file_is_valid_json(self) -> None:
        import json

        with open("chart/values.schema.json", encoding="utf-8") as handle:
            schema = json.load(handle)
        self.assertEqual(schema.get("type"), "object")
        self.assertFalse(schema.get("additionalProperties"))


if __name__ == "__main__":
    unittest.main()
