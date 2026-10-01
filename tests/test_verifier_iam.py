"""Regression boundary; effective authorization still requires AWS runtime proof."""
import json
from pathlib import Path
import unittest


class VerifierIamBoundaryTests(unittest.TestCase):
    def test_identity_policy_cannot_gain_extra_actions_or_resources(self):
        policy=json.loads(Path('infra-values/iam-verifier-int-policy.json').read_text())
        bucket='arn:aws:s3:::example-corp-000000000000-int-example-corp-raw'
        expected={
            ('s3:ListBucket', bucket): {'StringLike':{'s3:prefix':[
                'as400/sales/sale/runs/*',
                'as400/sales/fleet/*',
                'as400/sales/*/journal/*',
            ]}},
            ('s3:GetObject', bucket+'/as400/sales/sale/runs/*'): {},
            ('s3:GetObject', bucket+'/as400/sales/sale/proofs/cdcforge-autonomous-latest.json'): {},
            ('s3:GetObject', bucket+'/as400/sales/fleet/console-snapshot.json'): {},
            ('s3:GetObject', bucket+'/as400/sales/fleet/console-proof.json'): {},
            ('s3:PutObject', bucket+'/as400/sales/sale/proofs/cdcforge-autonomous-latest.json'): {},
            ('s3:PutObject', bucket+'/as400/sales/sale/runs/*/windows/*/destination.json'): {},
            ('s3:PutObject', bucket+'/as400/sales/fleet/console-proof.json'): {},
            ('s3:GetObject', bucket+'/as400/sales/*/journal/*.manifest.json'): {},
            ('s3:GetObject', bucket+'/as400/sales/fleet/load-ledger.json'): {},
            ('s3:PutObject', bucket+'/as400/sales/fleet/load-ledger.json'): {},
            ('cloudwatch:GetMetricStatistics', '*'): {'StringEquals': {'aws:RequestedRegion': 'eu-west-3'}},
        }
        self.assertEqual(len(policy['Statement']),len(expected))
        for statement in policy['Statement']:
            self.assertEqual(statement['Effect'],'Allow')
            key = (statement['Action'], statement['Resource'])
            self.assertIn(key,expected)
            condition=expected.pop(key)
            self.assertEqual(statement.get('Condition',{}),condition)
            self.assertLessEqual(set(statement),{'Sid','Effect','Action','Resource','Condition'})
        self.assertEqual(expected,{})

    def test_trust_cannot_expand_beyond_the_dedicated_service_account(self):
        policy=json.loads(Path('infra-values/iam-verifier-int-trust-policy.json').read_text())
        issuer='oidc.eks.eu-west-3.amazonaws.com/id/EXAMPLEOIDCPROVIDER'
        self.assertEqual(len(policy['Statement']),1)
        statement=policy['Statement'][0]
        self.assertEqual(statement['Action'],'sts:AssumeRoleWithWebIdentity')
        self.assertEqual(statement['Effect'],'Allow')
        self.assertEqual(statement['Principal'],{'Federated':'arn:aws:iam::000000000000:oidc-provider/'+issuer})
        self.assertEqual(statement['Condition'],{'StringEquals':{
            issuer+':sub':'system:serviceaccount:quadringent-demo:cdcforge-verifier',
            issuer+':aud':'sts.amazonaws.com'}})
        self.assertLessEqual(set(statement),{'Sid','Effect','Action','Principal','Condition'})
