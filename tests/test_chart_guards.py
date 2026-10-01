"""Chaque garde du chart doit refuser sa configuration interdite.

Le chart déclare ses refus en `fail` : ils n'ont de valeur que s'ils sont
opposés à une tentative réelle. Ce test rend le chart une fois par
configuration interdite et exige un échec, puis vérifie que la configuration
réellement déployée passe toujours.

Mesure du 17/09 : les douze gardes ont été opposées à un rendu réel, aucune
n'a laissé passer sa violation.
"""

from __future__ import annotations

import subprocess
import unittest


VALUES = "infra-values/values-int.yaml"
NAMESPACE = "quadringent-demo"

# Les artefacts de lancement sont des fichiers versionnés, passés par
# --set-file exactement comme la commande d'installation documentée.
LAUNCH_FILES = (
    "--set-file", "controlPlane.launch.jobTemplate=infra-values/job-template-int.json",
    "--set-file", "controlPlane.launch.fleetCatalog=infra-values/fleet-catalog-int.json",
    "--set-file", "controlPlane.launch.fleetSidecar=infra-values/fleet-sidecar-int.json",
)

# (nom lisible, arguments supplémentaires pour helm)
VIOLATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("environment hors dev", ("deployment.environment=prod",)),
    ("promotion production autorisée", ("deployment.productionPromotionAllowed=true",)),
    ("deux lecteurs concurrents", ("replicaCount=2",)),
    ("pilote et lecteur permanents ensemble", ("pilot.enabled=true", "replicaCount=1")),
    ("zéro erreur consécutive tolérée", ("safety.maxConsecutiveErrors=0",)),
    ("intervalle de console nul", ("consoleSnapshot.intervalSeconds=0",)),
    ("plaintext IBM i autorisé", ("as400.allowPlaintext=true",)),
    ("TLS désactivé", ("as400.tls=false",)),
    ("TLS sans autorité de certification", ("as400.tlsCaFile=",)),
    ("autorité TLS avec remontée de répertoire", ("as400.tlsCaFile=/app/../etc/passwd",)),
    ("autorité TLS relative", ("as400.tlsCaFile=certs/ca.pem",)),
    ("table du manifeste invalide", ("site.fleetTables[0]=bad name",)),
    ("doublon dans le manifeste", ("site.fleetTables[0]=SALE",)),
    ("colonnes de preuve invalides", ("site.proofKeyColumns[0]=bad.name",)),
    ("préfixe destination invalide", ("site.destinationPrefix=bad-prefix",)),
    ("image sans digest", ("image.digest=",)),
    (
        "taille d'état de flotte invalide",
        (
            "controlPlane.launch.enabled=true",
            "controlPlane.fleetState.persistence.size=beaucoup",
        ),
    ),
)


class ChartGuardTests(unittest.TestCase):
    def test_shared_warehouse_configuration_reaches_the_runtime(self) -> None:
        result = self.render(NAMESPACE, "site.snowflakeWarehouse=SHARED_INGESTION_WH")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QUADRINGENT_SNOWFLAKE_WAREHOUSE: "SHARED_INGESTION_WH"', result.stdout)
        invalid = self.render(NAMESPACE, "site.snowflakeWarehouse=bad-warehouse")
        self.assertNotEqual(invalid.returncode, 0)

    def test_un_prix_exige_sa_devise_et_reciproquement(self):
        for value in ("site.snowflakeCreditPrice=2", "site.costCurrency=EUR"):
            self.assertNotEqual(self.render(NAMESPACE, value).returncode, 0)

    def test_le_tarif_zero_explicite_est_conserve(self):
        result = self.render(NAMESPACE, "site.snowflakeCreditPrice=0", "site.costCurrency=EUR")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('QUADRINGENT_SNOWFLAKE_CREDIT_PRICE: "0"', result.stdout)

    def render(self, namespace: str = NAMESPACE, *values: str) -> subprocess.CompletedProcess[str]:
        command = [
            "helm",
            "template",
            "cdc",
            "chart",
            "--namespace",
            namespace,
            "-f",
            VALUES,
            *LAUNCH_FILES,
        ]
        for value in values:
            command += ["--set", value]
        return subprocess.run(command, capture_output=True, text=True)

    def test_the_deployed_configuration_still_renders(self) -> None:
        """Une garde qui refuse tout ne protège rien : la base doit passer."""

        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kind:", result.stdout)
        # Le préfixe destination déclaré par le site doit parvenir au runtime.
        self.assertIn('QUADRINGENT_DESTINATION_PREFIX: "CDC_FORGE"', result.stdout)

    def test_a_namespace_outside_the_dev_scope_is_refused(self) -> None:
        result = self.render("prod-autre")
        self.assertNotEqual(result.returncode, 0, "un namespace hors périmètre a été rendu")

    def test_every_declared_guard_refuses_its_violation(self) -> None:
        for name, arguments in VIOLATIONS:
            with self.subTest(guard=name):
                result = self.render(NAMESPACE, *arguments)
                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"la garde « {name} » a laissé passer sa configuration interdite",
                )
                self.assertIn("Error", result.stderr)

    def test_fleet_state_persistence_renders_a_pvc(self) -> None:
        """L'état d'orchestration de la flotte doit survivre au pod : la
        persistance déclarée rend un PVC et le volume fleet-state le monte."""

        result = self.render(
            NAMESPACE,
            "controlPlane.launch.enabled=true",
            "controlPlane.fleetState.persistence.enabled=true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kind: PersistentVolumeClaim", result.stdout)
        claim = "cdc-quadringent-control-plane-fleet-state"
        self.assertIn(f"name: {claim}", result.stdout)
        self.assertIn(f"claimName: {claim}", result.stdout)
        self.assertIn("ReadWriteOnce", result.stdout)

    def test_fleet_state_persistence_disabled_keeps_empty_dir(self) -> None:
        """La désactivation explicite revient à l'emptyDir historique."""

        result = self.render(
            NAMESPACE,
            "controlPlane.launch.enabled=true",
            "controlPlane.fleetState.persistence.enabled=false",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("kind: PersistentVolumeClaim", result.stdout)
        self.assertNotIn("persistentVolumeClaim", result.stdout)
        self.assertIn("emptyDir: {}", result.stdout)

    def test_fleet_state_persistence_is_inert_without_launch(self) -> None:
        """Sans lancement de flotte, aucun volume fleet-state ni PVC n'existe."""

        result = self.render(
            NAMESPACE,
            "controlPlane.launch.enabled=false",
            "controlPlane.fleetState.persistence.enabled=true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("kind: PersistentVolumeClaim", result.stdout)
        self.assertNotIn("fleet-state", result.stdout)
