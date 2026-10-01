"""Gap (a) de docs/product/install-default.md : la chart doit se rendre sans
connexion IBM i déclarée (`site.connectionDeclared: false`), sans jamais
poser de valeur fictive à la place — et le rendu historique (une connexion
déclarée, comme `infra-values/values-int.yaml`) doit rester strictement
inchangé (`site.connectionDeclared` par défaut à `true` côté chart)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from quadringent.installer.manifest import ReleaseManifest
from quadringent.installer.plan import InstallInputs, build_chart_values

MANIFEST = ReleaseManifest.from_file(Path("deploy/release-manifest.example.json"))


def _helm_template(values: dict, *, namespace: str = "quadringent", release: str = "demo-int") -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(values, handle, sort_keys=False, allow_unicode=True)
        values_path = handle.name
    return subprocess.run(
        ["helm", "template", release, "chart", "--namespace", namespace, "-f", values_path],
        capture_output=True,
        text=True,
    )


def _no_connection_values() -> dict:
    inputs = InstallInputs(cloud="aws", target="vm", region="eu-west-3", name="demo-int")
    values = build_chart_values(inputs, MANIFEST, control_plane_role_arn="arn:aws:iam::000000000000:role/demo-int-quadringent-runtime")
    return values


class GoldenRenderUnchangedTests(unittest.TestCase):
    """`infra-values/values-int.yaml` ne déclare pas `site.connectionDeclared` :
    le défaut du chart (`true`) doit reproduire exactement le rendu d'avant
    gap (a), gel byte à byte plutôt qu'une simple absence d'erreur."""

    def test_values_int_renders_byte_identical(self) -> None:
        result = subprocess.run(
            ["helm", "template", "cdc", "chart", "--namespace", "quadringent-demo", "-f", "infra-values/values-int.yaml"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        golden = Path("tests/golden/values-int-render.yaml")
        expected = golden.read_text(encoding="utf-8")
        self.assertEqual(result.stdout, expected)


class NoConnectionDeclaredTests(unittest.TestCase):
    def test_installer_default_values_render_without_any_connection(self) -> None:
        # Ce que `quadringent install` génère réellement en mode par défaut,
        # avant l'assistant de connexion IBM i (chantier hors périmètre).
        values = _no_connection_values()
        self.assertFalse(values["site"]["connectionDeclared"])
        self.assertEqual(values["site"]["ibmiHost"], "")
        self.assertEqual(values["ibmi"]["host"], "")
        self.assertEqual(values["as400"]["tlsCaFile"], "")

        result = _helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        kinds_and_names = {(doc["kind"], doc["metadata"]["name"]) for doc in documents}

        # Aucun lecteur : rien à lire sans connexion déclarée.
        self.assertNotIn(("Deployment", "demo-int-quadringent"), kinds_and_names)
        # Le control plane et Postgres restent actifs (site « control plane
        # actif, aucune source connectée »).
        self.assertIn(("Deployment", "demo-int-quadringent-control-plane"), kinds_and_names)
        self.assertIn(("StatefulSet", "demo-int-quadringent-postgres"), kinds_and_names)

        site_configmap = next(
            doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"].endswith("-site")
        )
        self.assertEqual(site_configmap["data"]["QUADRINGENT_IBMI_HOST"], "")
        self.assertEqual(site_configmap["data"]["QUADRINGENT_PROOF_TABLE"], "")
        # Champs réellement connus de l'installateur à ce stade : conservés.
        self.assertEqual(site_configmap["data"]["QUADRINGENT_SITE_ID"], "demo-int")
        self.assertEqual(site_configmap["data"]["QUADRINGENT_RAW_BUCKET"], "demo-int-quadringent-raw")

        tuning_configmap = next(
            doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"].endswith("-tuning")
        )
        self.assertNotIn("ISERIES_HOST", tuning_configmap["data"])
        self.assertNotIn("AS400_TLS_CA_FILE", tuning_configmap["data"])
        # Le stockage, connu de l'installateur, reste publié.
        self.assertEqual(tuning_configmap["data"]["AS400_RAW_BUCKET"], "demo-int-quadringent-raw")

    def test_partial_connection_fields_are_refused_not_silently_accepted(self) -> None:
        # site.connectionDeclared=false exige un vide complet : une valeur
        # partiellement déclarée serait une preuve à moitié fictive.
        values = _no_connection_values()
        values["site"]["ibmiHost"] = "192.0.2.10"
        result = _helm_template(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("connectionDeclared=false", result.stderr)

    def test_reader_replica_count_requires_connection_declared(self) -> None:
        values = _no_connection_values()
        values["replicaCount"] = 1
        result = _helm_template(values)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("aucun lecteur ne peut démarrer sans connexion IBM i déclarée", result.stderr)


if __name__ == "__main__":
    unittest.main()
