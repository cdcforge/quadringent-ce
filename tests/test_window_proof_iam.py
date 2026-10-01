import fnmatch
import json
from pathlib import Path
import unittest


class WindowProofIamTests(unittest.TestCase):
    def test_cockpit_reads_only_destination_proofs_without_write_or_list(self):
        policy = json.loads(Path('infra-values/iam-control-plane-int-policy.json').read_text())
        statements = policy['Statement']
        prefix = 'arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/sale/'
        self.assertEqual({item['Action'] for item in statements}, {'s3:GetObject'})
        self.assertEqual({item['Effect'] for item in statements}, {'Allow'})
        resources = {item['Resource'] for item in statements}
        fleet_prefix = 'arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/fleet/'
        self.assertEqual(resources, {prefix+'proofs/cdcforge-autonomous-latest.json',
                                    prefix+'runs/*/windows/*/destination.json',
                                    fleet_prefix+'console-snapshot.json',
                                    fleet_prefix+'console-proof.json'})
        self.assertTrue(any(fnmatch.fnmatchcase(prefix+'runs/r1/windows/w1/destination.json', pattern)
                            for pattern in resources))
        for resource in (prefix+'runs/r1/batch-a.jsonl', prefix+'runs/r1/console-snapshot.json',
                         prefix+'runs/r1/windows/w1/closed.json', prefix+'runs/r1/reservation.json',
                         prefix+'runs/r1/windows/w1/checkpoint.json',
                         fleet_prefix+'console-snapshot.json.bak',
                         fleet_prefix+'console-proof.json.bak',
                         fleet_prefix+'runs/r1/console-proof.json',
                         fleet_prefix+'runs/r1/console-snapshot.json',
                         'arn:aws:s3:::other/as400/sales/sale/runs/r1/windows/w1/destination.json',
                         prefix.replace('/sale/', '/cntr/')+'runs/r1/windows/w1/destination.json'):
            with self.subTest(resource=resource):
                self.assertFalse(any(fnmatch.fnmatchcase(resource, pattern) for pattern in resources))

    def test_write_scope_is_only_legacy_proof_and_window_destination_sidecars(self):
        policy=json.loads(Path('infra-values/iam-verifier-int-policy.json').read_text())
        writes=[statement for statement in policy['Statement'] if statement['Action']=='s3:PutObject']
        prefix='arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/sale/'
        fleet_prefix='arn:aws:s3:::example-corp-000000000000-int-example-corp-raw/as400/sales/fleet/'
        self.assertEqual({item['Resource'] for item in writes},{
            prefix+'proofs/cdcforge-autonomous-latest.json',prefix+'runs/*/windows/*/destination.json',
            fleet_prefix+'console-proof.json',fleet_prefix+'load-ledger.json'})
        for resource in (prefix+'runs/r1/batch-a.jsonl',prefix+'runs/r1/console-snapshot.json',
                         prefix+'runs/r1/windows/w1/closed.json',prefix+'runs/r1/reservation.json',
                         fleet_prefix+'console-snapshot.json',fleet_prefix+'console-proof.json.bak',
                         fleet_prefix+'runs/r1/console-proof.json',
                         'arn:aws:s3:::other/as400/sales/sale/runs/r1/windows/w1/destination.json'):
            self.assertFalse(any(fnmatch.fnmatchcase(resource,item['Resource']) for item in writes))
