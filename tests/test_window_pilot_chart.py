import subprocess
import unittest
import yaml


class WindowPilotChartTests(unittest.TestCase):
    def test_chain_uses_same_run_and_distinct_supervisor_mode(self):
        result = self.render('pilot.proofWindow.count=3', 'pilot.maxSeconds=2400',
                             'verification.enabled=true', 'verification.publishProof=true')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--proof-window-count', result.stdout)
        self.assertIn('--chain-proof-directory', result.stdout)
        self.assertNotIn('\n            - --window-id\n', result.stdout)
        self.assertNotIn('\n            - --proof-output\n', result.stdout)
        jobs = [document for document in yaml.safe_load_all(result.stdout)
                if document and document.get('kind') == 'Job']
        self.assertEqual(len(jobs), 2)
        args = [job['spec']['template']['spec']['containers'][0]['args'] for job in jobs]
        capture = next(value for value in args if '--proof-window-count' in value)
        verifier = next(value for value in args if '--chain-proof-directory' in value)
        self.assertEqual(capture[capture.index('--proof-window-count')+1], '3')
        self.assertEqual(capture[capture.index('--reserve-run-id')+1],
                         verifier[verifier.index('--run-id')+1])
        self.assertEqual(verifier[verifier.index('--chain-proof-directory')+1], '/work/windows')
        self.assertGreater(int(verifier[verifier.index('--budget-seconds')+1]), 2400)

    def test_chain_requires_strict_budget_and_valid_count(self):
        for value in ('pilot.proofWindow.count=0', 'pilot.proofWindow.count=true',
                      'pilot.proofWindow.count=1.5', 'pilot.proofWindow.count=129',
                      'pilot.maxSeconds=1995', 'pilot.proofWindow.enabled=false'):
            with self.subTest(value=value):
                self.assertNotEqual(self.render('pilot.proofWindow.count=3',
                    'pilot.maxSeconds=1996', 'tuning.readerTimeoutSeconds=15', value).returncode, 0)
        self.assertEqual(self.render('pilot.proofWindow.count=3', 'pilot.maxSeconds=1996',
                                    'tuning.readerTimeoutSeconds=15').returncode, 0)

    def render(self,*values):
        command=['helm','template','cdc','chart','--namespace','quadringent-demo','-f','infra-values/values-int.yaml',
                 '--set','pilot.enabled=true','--set','pilot.runId=windowpilot','--set','pilot.proofWindow.enabled=true',
                 '--set','pilot.proofWindow.id=w1','--set','pilot.maxSeconds=900','--set','bootstrap.mode=checkpoint']
        for value in values:command+=['--set',value]
        return subprocess.run(command,capture_output=True,text=True)

    def test_window_flags_are_rendered(self):
        result=self.render()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--proof-window-id',result.stdout)
        self.assertIn('--proof-window-seconds',result.stdout)

    def test_disabled_mode_does_not_change_capture_arguments(self):
        result=self.render('pilot.proofWindow.enabled=false')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertNotIn('--proof-window-id',result.stdout)

    def test_window_verifier_runs_supervisor_not_historical_cli(self):
        result=self.render('verification.enabled=true','verification.publishProof=true')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('/app/scripts/quadringent_window_supervise.py',result.stdout)
        self.assertIn('--window-id',result.stdout)
        self.assertIn('--budget-seconds',result.stdout)
        self.assertNotIn('--await-capture-seconds',result.stdout)
        self.assertNotIn('--proof-s3-uri',result.stdout)
        self.assertNotEqual(self.render('verification.enabled=true','verification.publishProof=true','verification.slo.enabled=true').returncode,0)

    def test_budget_includes_reader_reserve_and_closure_grace(self):
        self.assertEqual(self.render('pilot.maxSeconds=675','tuning.readerTimeoutSeconds=15').returncode,0)
        self.assertNotEqual(self.render('pilot.maxSeconds=674','tuning.readerTimeoutSeconds=15').returncode,0)
        self.assertNotEqual(self.render('pilot.maxSeconds=675','tuning.readerTimeoutSeconds=15.1').returncode,0)
        self.assertEqual(self.render('pilot.maxSeconds=676','tuning.readerTimeoutSeconds=15.1').returncode,0)

    def test_pilot_job_has_a_meaningful_readiness_probe(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs = [document for document in yaml.safe_load_all(result.stdout)
                if document and document.get('kind') == 'Job'
                and 'pilot' in document['metadata']['name']]
        self.assertEqual(len(jobs), 1)
        container = jobs[0]['spec']['template']['spec']['containers'][0]
        probe = container.get('readinessProbe')
        # La readiness atteste que le lecteur python et son worker JVM
        # tournent — pas seulement que le conteneur est démarré.
        self.assertIsNotNone(probe)
        command = ' '.join(probe['exec']['command'])
        self.assertIn('/proc/', command)
        self.assertIn('python', command)
        self.assertIn('java', command)

    def test_probes_never_attest_themselves(self):
        """Les deux sondes excluent leur propre pid : la readiness exige le
        lecteur python de capture (pas un probe concurrent), la liveness ne
        mesure que les ticks du worker JVM."""
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        jobs = [document for document in yaml.safe_load_all(result.stdout)
                if document and document.get('kind') == 'Job'
                and 'pilot' in document['metadata']['name']]
        container = jobs[0]['spec']['template']['spec']['containers'][0]
        readiness = ' '.join(container['readinessProbe']['exec']['command'])
        liveness = ' '.join(container['livenessProbe']['exec']['command'])
        for command in (readiness, liveness):
            self.assertIn('os.getpid()', command)
            self.assertIn('self_pid', command)
        # La readiness atteste le worker python de capture, pas n'importe
        # quel processus python (un probe aurait le même comm).
        self.assertIn('as400_continuous_capture', readiness)
        self.assertIn('cmdline', readiness)
        # La liveness ne somme que les ticks JVM : le probe lui-même ne peut
        # pas faire bouger le compteur.
        self.assertIn('comm != "java"', liveness)

    def test_shutdown_margin_is_bounded(self):
        for value in ('pilot.shutdownMarginSeconds=0',
                      'pilot.shutdownMarginSeconds=-3',
                      'pilot.shutdownMarginSeconds=abc',
                      'pilot.shutdownMarginSeconds=1.5',
                      'pilot.shutdownMarginSeconds=900',
                      'pilot.shutdownMarginSeconds=1200'):
            with self.subTest(value=value):
                # render() fixe pilot.maxSeconds=900 : une marge >= maxSeconds
                # ou non entière est refusée.
                self.assertNotEqual(self.render(value).returncode, 0)
        self.assertEqual(self.render('pilot.shutdownMarginSeconds=120').returncode, 0)

    def test_unsafe_window_configuration_is_rejected(self):
        for value in ('bootstrap.mode=tail','pilot.maxSeconds=599','pilot.maxSeconds=600','pilot.proofWindow.seconds=599','pilot.proofWindow.seconds=600.5','pilot.maxSeconds=600.5',
                      'ibmi.user=OTHER','ibmi.host=other','storage.checkpointTable=other',
                      'verification.enabled=true','pilot.proofWindow.id=../escape','replicaCount=1'):
            with self.subTest(value=value):
                self.assertNotEqual(self.render(value).returncode,0)
