import subprocess
import unittest


class WindowCockpitChartTests(unittest.TestCase):
    prefix='s3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/runs/'

    def render(self,uri=None):
        args=['helm','template','cdc','chart','--namespace','quadringent-demo',
              '-f','infra-values/values-int.yaml']
        if uri is not None:
            args+=['--set-string','controlPlane.windowProof='+uri]
        return subprocess.run(args,capture_output=True,text=True)

    def arguments(self,result):
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def test_disabled_by_default(self):
        self.assertNotIn('--window-proof',self.arguments(self.render()))

    def test_exact_dev_sidecar_binding_is_rendered(self):
        uri=self.prefix+'r1/windows/w1/destination.json'
        args=self.arguments(self.render(uri))
        self.assertIn('--window-proof',args)
        self.assertIn('"dev-sale='+uri+'"',args)

    def test_exact_dev_chain_binding_is_rendered(self):
        uri = self.prefix+'r1/window-chain.json'
        self.assertIn('"dev-sale='+uri+'"', self.arguments(self.render(uri)))

    def test_foreign_or_ambiguous_paths_are_rejected(self):
        for uri in ('file:///tmp/proof.json',self.prefix+'../r1/windows/w1/destination.json',
                    self.prefix+'r1/windows/w1/closed.json',
                    self.prefix+'r1/windows/w1/destination.json?x=1',
                    self.prefix+'r1/windows/w1/destination.json\n',
                    self.prefix+'R1/windows/w1/destination.json',
                    's3://other/as400/sales/sale/runs/r1/windows/w1/destination.json'):
            with self.subTest(uri=uri):
                self.assertNotEqual(self.render(uri).returncode,0)
