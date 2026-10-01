import subprocess
import unittest
from pathlib import Path

import yaml


VERIFIER_SA = yaml.safe_load(
    Path("infra-values/values-int.yaml").read_text()
)["verifierServiceAccountName"]


class ObservabilityCronTests(unittest.TestCase):
    def render(self, *extra):
        return subprocess.run([
            'helm', 'template', 'cdc', 'chart', '--namespace', 'quadringent-demo',
            '-f', 'infra-values/values-int.yaml', *extra,
        ], capture_output=True, text=True)

    def enabled(self, *extra):
        return self.render('--set', 'observability.enabled=true', '--set-file',
                           'observability.policyJson=infra-values/slo-policy-dev.json', *extra)

    def test_disabled_when_explicitly_turned_off(self):
        result = self.render('--set', 'observability.enabled=false')
        self.assertEqual(result.returncode, 0, result.stderr)
        # La sonde SLO disparaît ; le relevé de livraison flotte, activité
        # distincte, peut rester déclaré par les values du site.
        self.assertNotIn('component: observability', result.stdout)

    def test_suspended_independent_observer_and_capture_unchanged(self):
        result = self.enabled('--set', 'observability.suspend=true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('kind: CronJob', result.stdout)
        cron = next(doc for doc in result.stdout.split('---') if 'kind: CronJob' in doc and 'fleet-observe' not in doc)
        for expected in ('concurrencyPolicy: Forbid', 'suspend: true',
                         'activeDeadlineSeconds: 600', 'backoffLimit: 0',
                         'quadringent_observability_refresh.py', f'serviceAccountName: {VERIFIER_SA}',
                         'readOnlyRootFilesystem: true', 'REFRESH_AS400_RD_OBSERVABILITY_DEV'):
            self.assertIn(expected, cron)
        self.assertNotIn('secretKeyRef', cron)
        before = [d for d in self.render().stdout.split('---') if 'kind: Deployment' in d]
        after = [d for d in result.stdout.split('---') if 'kind: Deployment' in d]
        self.assertEqual(before, after)

    def test_alerts_run_after_refresh_and_stay_silent_without_destination(self):
        result = self.enabled('--set', 'observability.suspend=false')
        self.assertEqual(result.returncode, 0, result.stderr)
        cron = next(doc for doc in result.stdout.split('---') if 'kind: CronJob' in doc and 'fleet-observe' not in doc)
        # Le rafraîchissement chaîne la réconciliation des alertes SLO.
        self.assertIn('quadringent_observability_refresh.py', cron)
        self.assertIn('quadringent_slo_alerts.py', cron)
        # Sans destination déclarée, aucune variable d'alerte n'est injectée
        # et l'état d'alerte ne peut pas faire échouer le CronJob.
        self.assertNotIn('name: QUADRINGENT_ALERT_', cron)
        self.assertIn('|| true', cron)

    def test_alert_destinations_are_declared_and_propagate(self):
        result = self.enabled(
            '--set', 'observability.alerts.webhookUrl=https://hooks.example.invalid/x',
            '--set', 'observability.alerts.snsTopic=arn:aws:sns:eu-west-3:000000000001:alerts')
        self.assertEqual(result.returncode, 0, result.stderr)
        cron = next(doc for doc in result.stdout.split('---') if 'kind: CronJob' in doc and 'fleet-observe' not in doc)
        self.assertIn('name: QUADRINGENT_ALERT_WEBHOOK_URL', cron)
        self.assertIn('value: "https://hooks.example.invalid/x"', cron)
        self.assertIn('name: QUADRINGENT_ALERT_SNS_TOPIC', cron)
        self.assertIn('value: "arn:aws:sns:eu-west-3:000000000001:alerts"', cron)

    def test_alert_destinations_reject_non_strings(self):
        self.assertNotEqual(self.enabled(
            '--set-json', 'observability.alerts.snsTopic=42').returncode, 0)
        self.assertNotEqual(self.enabled(
            '--set-json', 'observability.alerts=["x"]').returncode, 0)

    def test_rejects_unsafe_configuration(self):
        for setting in ('deployment.environment=prod', 'observability.imageDigest=latest',
                        'storage.rawBucket=other',
                        'observability.schedule=* * * * *'):
            with self.subTest(setting=setting):
                self.assertNotEqual(self.enabled('--set-string', setting).returncode, 0)
        self.assertNotEqual(self.enabled('--namespace', 'other').returncode, 0)
        self.assertNotEqual(self.render('--set', 'observability.enabled=true',
                                       '--set-json', 'observability.policyJson="{}"').returncode, 0)

    def test_explicit_resume(self):
        result = self.enabled('--set', 'observability.suspend=false')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('suspend: false', result.stdout)
