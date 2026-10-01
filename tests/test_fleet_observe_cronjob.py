import subprocess
import unittest
from pathlib import Path

import yaml


VERIFIER_SA = yaml.safe_load(
    Path("infra-values/values-int.yaml").read_text()
)["verifierServiceAccountName"]

DIGEST = "sha256:60a99ffc72cb79b8990124752321fe0a4091e762f99effbcdc367e0e27cba627"


class FleetObserveCronTests(unittest.TestCase):
    def render(self, *extra):
        return subprocess.run([
            'helm', 'template', 'cdc', 'chart', '--namespace', 'quadringent-demo',
            '-f', 'infra-values/values-int.yaml', *extra,
        ], capture_output=True, text=True)

    def enabled(self, *extra):
        return self.render('--set', 'fleetObserve.enabled=true',
                           '--set', f'fleetObserve.imageDigest={DIGEST}', *extra)

    def test_disabled_when_explicitly_turned_off(self):
        # values-int déclare le relevé : seule une coupure explicite l'ôte.
        result = self.render('--set', 'fleetObserve.enabled=false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fleet-observe', result.stdout)

    def test_suspended_cron_measures_and_publishes_on_the_dedicated_key(self):
        result = self.enabled('--set', 'fleetObserve.suspend=true')
        self.assertEqual(result.returncode, 0, result.stderr)
        cron = next(doc for doc in result.stdout.split('---')
                    if 'kind: CronJob' in doc and 'fleet-observe' in doc)
        for expected in ('concurrencyPolicy: Forbid', 'suspend: true',
                         'activeDeadlineSeconds: 300', 'backoffLimit: 0',
                         'schedule: "*/4 * * * *"',
                         'quadringent_fleet_observe.py',
                         '--execute', '--aws-default-credentials',
                         '--snowflake-oidc-token-file',
                         '--manifest-budget 1500',
                         f'serviceAccountName: {VERIFIER_SA}',
                         'readOnlyRootFilesystem: true',
                         'audience: snowflakecomputing.com'):
            self.assertIn(expected, cron)
        # Jamais de credentials locaux ni de secret injecte.
        self.assertNotIn('secretKeyRef', cron)

    def test_capture_deployment_unchanged_when_enabled(self):
        before = [d for d in self.render().stdout.split('---')
                  if 'kind: Deployment' in d]
        after = [d for d in self.enabled().stdout.split('---')
                 if 'kind: Deployment' in d]
        self.assertEqual(before, after)

    def test_rejects_unsafe_configuration(self):
        for setting in ('fleetObserve.imageDigest=latest',
                        'fleetObserve.imageDigest=main',
                        'fleetObserve.schedule=* * * * *',
                        'fleetObserve.schedule=*/15 * * * *'):
            with self.subTest(setting=setting):
                self.assertNotEqual(
                    self.enabled('--set-string', setting).returncode, 0)
        self.assertNotEqual(self.enabled('--set-json',
                                         'fleetObserve.suspend="x"').returncode, 0)
        self.assertNotEqual(self.enabled('--namespace', 'other').returncode, 0)
        # Un budget nul ou négatif laisserait le registre se figer :
        # refusé au rendu, jamais au run.
        for setting in ('fleetObserve.manifestBudget=0',
                        'fleetObserve.manifestBudget=-10'):
            with self.subTest(setting=setting):
                self.assertNotEqual(
                    self.enabled('--set', setting).returncode, 0)

    def test_explicit_resume(self):
        result = self.enabled('--set', 'fleetObserve.suspend=false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('suspend: false', result.stdout)

    def test_observer_script_imports_its_sibling_module(self):
        """Le préambule retire le répertoire du script de sys.path en tête
        puis le remet en fin : les modules frères (quadringent_autonomous_verify)
        doivent rester importables — sinon le CronJob meurt en
        ModuleNotFoundError (régression observée au premier run INT)."""

        result = subprocess.run(
            ['python3', 'scripts/quadringent_fleet_observe.py', '--help'],
            capture_output=True, text=True,
        )
        self.assertNotIn('ModuleNotFoundError', result.stderr)
        self.assertNotIn('No module named', result.stderr)
        # Sans site installé l'échec attendu est la configuration de site,
        # jamais un import : la chaîne d'imports est prouvée exécutable.
        if result.returncode != 0:
            self.assertIn('SiteConfigurationError', result.stderr)
