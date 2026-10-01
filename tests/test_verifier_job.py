import subprocess
import unittest
import json
from pathlib import Path
import tempfile

import yaml


VALUES = yaml.safe_load(Path("infra-values/values-int.yaml").read_text())
VERIFIER_SA = VALUES["verifierServiceAccountName"]
PROOF_NAME = VALUES["site"].get(
    "autonomousProofName", "quadringent-autonomous-latest.json"
)


def render(*extra):
    return subprocess.run(['helm','template','cdc','chart','--namespace','quadringent-demo',
        '-f','infra-values/values-int.yaml','--set','pilot.enabled=true',
        '--set-string','pilot.runId=canary-one','--set','pilot.maxSeconds=600',
        '--set','verification.enabled=true',*extra], capture_output=True,text=True)


class VerifierJobTests(unittest.TestCase):
    def test_slo_job_mounts_the_explicit_policy_readonly(self):
        result = render('--set', 'verification.slo.enabled=true',
                        '--set-file', 'verification.slo.policyJson=infra-values/slo-policy-dev.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        job = next(part for part in result.stdout.split('---') if 'name: verifier' in part)
        self.assertIn('--slo-policy', job)
        self.assertIn('/etc/quadringent-slo/policy.json', job)
        self.assertIn('mountPath: /etc/quadringent-slo', job)
        self.assertIn('name: quadringent-slo-canary-one', result.stdout)

    def test_slo_without_explicit_policy_is_rejected(self):
        self.assertNotEqual(render('--set', 'verification.slo.enabled=true').returncode, 0)

    def test_slo_invalid_thresholds_are_rejected_before_job_creation(self):
        baseline = json.loads(Path('infra-values/slo-policy-dev.json').read_text())
        for invalid in ('unknown_key', -1, True, '120'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                policy = dict(baseline)
                if invalid == 'unknown_key':
                    policy['wrong_name'] = policy.pop('capture_freshness_seconds')
                else:
                    policy['capture_freshness_seconds'] = invalid
                path = Path(directory) / 'policy.json'
                path.write_text(json.dumps(policy))
                result = render('--set', 'verification.slo.enabled=true', '--set-file',
                                f'verification.slo.policyJson={path}')
                self.assertNotEqual(result.returncode, 0)

    def test_job_uses_dedicated_identity_and_bounded_offline_safe_defaults(self):
        result = render()
        self.assertEqual(result.returncode,0,result.stderr)
        jobs = [part for part in result.stdout.split('---') if 'kind: Job' in part]
        self.assertEqual(len(jobs),2)
        job = next(part for part in jobs if 'name: verifier' in part)
        for value in (f'serviceAccountName: {VERIFIER_SA}', 'automountServiceAccountToken: false',
                      'activeDeadlineSeconds: 900', 'backoffLimit: 0',
                      'readOnlyRootFilesystem: true', 'runAsNonRoot: true',
                      'audience: snowflakecomputing.com', 'AWS_EC2_METADATA_DISABLED',
                      '--await-capture-seconds', '--wait-seconds', '--aws-default-credentials',
                      '--snowflake-oidc-token-file', 'canary-one'):
            self.assertIn(value,job)
        for forbidden in ('ISERIES_PASSWORD', 'connection-name', 'tolerations:', '--proof-s3-uri'):
            self.assertNotIn(forbidden,job)
        # Seule source d'environnement injectée : le ConfigMap d'identité de
        # site (QUADRINGENT_*), jamais le câblage de capture ni un secret.
        self.assertEqual(job.count('configMapRef:'), 1)
        self.assertIn('name: cdc-quadringent-site', job)

    def test_publish_requires_explicit_value_and_targets_only_cockpit_key(self):
        result = render('--set','verification.publishProof=true')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PUBLISH_AS400_RD_AUTONOMOUS_PROOF_DEV',result.stdout)
        self.assertIn(f'as400/sales/sale/proofs/{PROOF_NAME}',result.stdout)

    def test_invalid_scope_and_budget_refuse_render(self):
        for setting in ('pilot.enabled=false', 'pilot.maxSeconds=4000',
                        'verification.imageDigest=latest', 'consoleSnapshot.enabled=false',
                        'storage.rawPrefix=as400/sales/cntr', 'ibmi.table=CNTR',
                        'deployment.environment=prod'):
            with self.subTest(setting=setting):
                self.assertNotEqual(render('--set',setting).returncode,0)

    def test_disabled_verifier_adds_no_job(self):
        result=render('--set','verification.enabled=false')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(sum('kind: Job' in part for part in result.stdout.split('---')),1)

    def test_image_repository_override_is_honoured(self):
        # Régression : le Job verifier codait en dur le dépôt public
        # ghcr.io/quadringent/quadringent, ignorant image.repository — un
        # site avec un registre privé (--image-repository de l'installateur)
        # se retrouvait donc à tirer une image publique sans rapport avec
        # son manifeste de version, en silence.
        result = render('--set-string', 'image.repository=registry.example.com/quadringent')
        self.assertEqual(result.returncode, 0, result.stderr)
        job = next(part for part in result.stdout.split('---') if 'name: verifier' in part)
        self.assertIn('image: "registry.example.com/quadringent@', job)
        self.assertNotIn('ghcr.io/quadringent/quadringent@', job)
