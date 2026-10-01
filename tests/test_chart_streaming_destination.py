"""Rendu de la chart avec site.destinationMode=streaming (Snowpipe Streaming).

Par défaut la chart continue de rendre le chemin COPY/MERGE existant sans
rien déclarer de nouveau (compatibilité ascendante). Le Deployment "capture"
(lecteur de journal IBM i) ne charge jamais Snowflake — raw-first, voir
``continuous.py`` — donc cette chart ne monte encore aucun Secret de profil
Snowpipe Streaming dans un Pod ; elle publie seulement le contrat déclaratif
(chemin attendu) que la charge qui exécutera le chargeur (hors périmètre de
cette chart pour l'instant) devra honorer. Ce module vérifie que :

- l'absence de site.destinationMode rend QUADRINGENT_DESTINATION_MODE=copy_merge
  sans exposer QUADRINGENT_STREAMING_PROFILE_JSON ;
- site.destinationMode=streaming expose QUADRINGENT_STREAMING_PROFILE_JSON
  à partir de streaming.mountPath/streaming.profileSecret.key ;
- une valeur inconnue de site.destinationMode est refusée au rendu (schéma).
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


class StreamingDestinationRenderTests(unittest.TestCase):
    def test_default_render_stays_on_copy_merge(self) -> None:
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QUADRINGENT_DESTINATION_MODE: "copy_merge"', result.stdout)
        self.assertNotIn("QUADRINGENT_STREAMING_PROFILE_JSON", result.stdout)

    def test_streaming_mode_exposes_the_expected_profile_path(self) -> None:
        result = render("site.destinationMode=streaming")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QUADRINGENT_DESTINATION_MODE: "streaming"', result.stdout)
        self.assertIn(
            'QUADRINGENT_STREAMING_PROFILE_JSON: "/app/secrets/streaming/profile.json"', result.stdout
        )

    def test_streaming_mode_honors_a_custom_mount_path_and_key(self) -> None:
        result = render(
            "site.destinationMode=streaming",
            "streaming.mountPath=/var/run/secrets/streaming",
            "streaming.profileSecret.key=sf-profile.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            'QUADRINGENT_STREAMING_PROFILE_JSON: "/var/run/secrets/streaming/sf-profile.json"',
            result.stdout,
        )

    def test_unknown_destination_mode_is_rejected(self) -> None:
        # Refusé par values.schema.json avant même d'atteindre le template
        # (garde de defense en profondeur dans configmap-site.yaml derrière).
        result = render("site.destinationMode=dynamic_table")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match pattern", result.stderr)
        self.assertIn("destinationMode", result.stderr)


if __name__ == "__main__":
    unittest.main()
