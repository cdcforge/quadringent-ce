"""Tests locaux : aucune connexion ni lecture de secret."""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quadringent.qualification import proof as p


class PureProofTests(unittest.TestCase):
    def resume_fixture(self):
        def workload(container, uid, digest):
            return {'uid':uid,'deployment':container,'selector':{'app':container},'pod_uid':'old','container':container,'service_account':'native-sa','identity_mode':'irsa',
                    'images':[{'name':container,'image_id':'containerd://sha256:'+digest*64}],
                    'identity_effective':{'verified':True,'method':'assume-role-with-web-identity',
                                          'arn':'arn:aws:sts::000000000000:assumed-role/native/session-before'}}
        common={'pipeline':'p1','table':'S.T','table_id':'t1','namespace':'dev','context':'aws-dev',
                'snapshot_checks_passed':True,'history_validation':{'equal':True},'reconciliation':{'equal':True},
                'duplicate_event_ids':0,'reader':workload('reader','d1','a'),
                'loader':workload('destination-loader','d2','c')}
        event={'ID':2,'NOTE':'new','EVENT_ID':'e11','OPERATION':'c','JOURNAL_RECEIVER':'R1','JOURNAL_SEQUENCE':11}
        old={'ID':1,'NOTE':'old','EVENT_ID':'e10','OPERATION':'c','JOURNAL_RECEIVER':'R1','JOURNAL_SEQUENCE':10}
        before={**common,'observed_utc':'2026-09-30T10:00:00+00:00','checkpoint':{'receiver':'R1','sequence':10},'history':[old]}
        after={**common,'observed_utc':'2026-09-30T10:00:05+00:00','checkpoint':{'receiver':'R1','sequence':11},'history':[old,event],
               'reader':{**common['reader'],'uid':'d3','pod_uid':'new'},
               'source_journal_positions':{'kind':'ibmi-source-rowpos-reader','positions':[{'JOURNAL_RECEIVER_NAME':'R1','SEQUENCE_NUMBER':11}]}}
        pause={**common,'kind':'native-reader-pause-proof','observed_utc':'2026-09-30T10:00:01+00:00',
               'reader':{'deployment':'reader','selector':{'app':'reader'},'deployment_absent':True,'owned_pods':[]}}
        resume={**common,'kind':'native-reader-resume-proof','observed_utc':'2026-09-30T10:00:04+00:00',
                'reader':{**common['reader'],'uid':'d3','desired_replicas':1,'ready_replicas':1,'pod_uid':'new'}}
        mutation={**common,'write_started_utc':'2026-09-30T10:00:02+00:00','write_ack_utc':'2026-09-30T10:00:03+00:00',
                  'acknowledged':True,'affected_rows':1,'operation':'insert','expected_events':[event]}
        return deepcopy(before),deepcopy(after),deepcopy({'pause':pause,'resume':resume,'mutations':[mutation]})


    def test_resume_same_selected_digest_ignores_sidecars_and_sts_session_names(self):
        before,after,operations=self.resume_fixture()
        for component in ('reader','loader'):
            after[component]['images'][0]['image_id']='docker-pullable://registry/image@'+after[component]['images'][0]['image_id'].split('://')[1]
            after[component]['images'].insert(0,{'name':'sidecar','image_id':'containerd://sha256:'+'f'*64})
            after[component]['identity_effective']['arn']='arn:aws:sts::000000000000:assumed-role/native/session-after'
        operations['resume']['reader']['identity_effective']['arn']='arn:aws:sts::000000000000:assumed-role/native/session-resume'
        self.assertEqual(p.evaluate_resume(before,after,None,operations)['status'],'PASS')

    def test_resume_rejects_changed_native_identity_context(self):
        for field,value in [('service_account','other-sa'),('identity_mode','vm-metadata')]:
            with self.subTest(field=field):
                before,after,operations=self.resume_fixture();operations['resume']['reader'][field]=value
                with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)
        for component in ('reader','loader'):
            with self.subTest(component=component):
                before,after,operations=self.resume_fixture()
                after[component]['identity_effective']['arn']='arn:aws:sts::000000000000:assumed-role/other/session'
                with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)

    def test_same_receiver_resume_qualifies_with_complete_evidence(self):
        before,after,operations=self.resume_fixture()
        result=p.evaluate_resume(before,after,None,operations)
        self.assertEqual(result['status'],'PASS')
        self.assertTrue(result['resume_qualified'])
        self.assertFalse(result['rotation_qualified'])

    def test_resume_rejects_mutation_outside_pause_or_uncovered(self):
        before,after,operations=self.resume_fixture()
        operations['mutations'][0]['write_started_utc']='2026-09-30T10:00:00+00:00'
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)
        before,after,operations=self.resume_fixture();after['checkpoint']['sequence']=10
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)

    def test_resume_rejects_live_pause_and_same_old_pod(self):
        before,after,operations=self.resume_fixture();operations['pause']['reader']['owned_pods']=['still-running']
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)
        before,after,operations=self.resume_fixture();after['reader']['pod_uid']='old'
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)

    def test_resume_requires_independent_positions_and_refuses_rotation(self):
        before,after,operations=self.resume_fixture();after.pop('source_journal_positions')
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)
        before,after,operations=self.resume_fixture();after['checkpoint']['receiver']='R2'
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,['R1','R2'],operations)
        before,after,operations=self.resume_fixture()
        after['source_journal_positions']['positions'].append({'JOURNAL_RECEIVER_NAME':'R2','SEQUENCE_NUMBER':1})
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)

    def test_resume_requires_retained_history_and_matching_operation(self):
        before,after,operations=self.resume_fixture();after['history']=after['history'][1:]
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)
        before,after,operations=self.resume_fixture();operations['mutations'][0]['operation']='delete'
        with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,operations)

    def reader_probe(self,before):
        from argparse import Namespace
        probe=object.__new__(p.NativeProbe)
        probe.args=Namespace(**{k:before[k] for k in ('pipeline','table','table_id','namespace','context')},reader_deployment='reader',
                             capture_digest='sha256:'+'a'*64,loader_digest='sha256:'+'c'*64,identity='irsa',
                             expected_role_arn='arn:aws:iam::000000000000:role/native',kubeconfig='/synthetic-unused')
        return probe

    def test_pause_collector_confirms_absence_and_refuses_remaining_pods(self):
        before,_,_=self.resume_fixture();probe=self.reader_probe(before)
        calls=[]
        def observed(*args):
            calls.append(args)
            return {'items':[]} if args[1]=='deployments' else {'items':[{'metadata':{'name':'old'}}]}
        probe.kubectl=observed
        with self.assertRaises(ValueError): probe.reader_state('pause',before)
        probe.kubectl=lambda *args: {'items':[]}
        proof=probe.reader_state('pause',before)
        self.assertEqual(proof['kind'],'native-reader-pause-proof')
        self.assertTrue(proof['reader']['deployment_absent'])
        self.assertNotIn('uid',proof['reader'])
        self.assertIn(('get','deployments','--field-selector','metadata.name=reader','-o','json'),calls)
        self.assertIn(('get','pods','-l','app=reader','-o','json'),calls)

    def test_pause_permission_error_is_not_absence(self):
        from subprocess import CompletedProcess
        before,_,_=self.resume_fixture();probe=self.reader_probe(before)
        with patch.object(p.subprocess,'run',return_value=CompletedProcess([],1,stdout='',stderr='Forbidden')) as command:
            with self.assertRaises(RuntimeError): probe.reader_state('pause',before)
            self.assertEqual(command.call_count,1)

    def test_pause_still_present_scaled_zero_is_not_native_absence(self):
        before,_,_=self.resume_fixture();probe=self.reader_probe(before)
        probe.kubectl=lambda *args: {'items':[{'metadata':{'uid':'d1'},'spec':{'replicas':0}}]}
        with self.assertRaises(ValueError): probe.reader_state('pause',before)

    def test_pause_rejects_changed_candidate_before_api_access(self):
        before,_,_=self.resume_fixture();probe=self.reader_probe(before)
        probe.args.capture_digest='sha256:'+'b'*64
        with patch.object(probe,'kubectl') as command:
            with self.assertRaises(ValueError): probe.reader_state('pause',before)
            command.assert_not_called()

    def test_resume_collector_accepts_recreated_native_reader(self):
        before,after,_=self.resume_fixture();probe=self.reader_probe(before)
        native={**after['reader'],'desired_replicas':1,'ready_replicas':1}
        probe.workload=lambda *args: (native,{})
        with patch.object(probe,'kubectl',side_effect=AssertionError('aucune lecture ancienne UID')):
            proof=probe.reader_state('resume',before)
        self.assertEqual(proof['reader']['uid'],'d3')
        self.assertNotEqual(proof['reader']['uid'],before['reader']['uid'])

    def test_pause_resume_reject_scope_selector_or_reused_deployment_uid(self):
        before,after,operations=self.resume_fixture()
        for stage,field,value in [('pause','selector',{'app':'other'}),('resume','deployment','other'),('resume','uid','d1')]:
            with self.subTest(stage=stage,field=field):
                bad=deepcopy(operations);bad[stage]['reader'][field]=value
                with self.assertRaises(ValueError): p.evaluate_resume(before,after,None,bad)
        probe=self.reader_probe(before)
        with patch.object(probe,'kubectl') as command:
            wrong=deepcopy(before);wrong['context']='other'
            with self.assertRaises(ValueError): probe.reader_state('pause',wrong)
            wrong=deepcopy(before);wrong['reader']['deployment']='other'
            with self.assertRaises(ValueError): probe.reader_state('pause',wrong)
            command.assert_not_called()

    def test_generated_source_positions_probe_compiles(self):
        from argparse import Namespace
        before,_,_=self.resume_fixture();probe=object.__new__(p.NativeProbe)
        probe.args=Namespace(**{k:before[k] for k in ('pipeline','table','table_id','namespace','context')},journal_library='S',journal_name='J',pk='ID')
        probe.library='S';probe.table='T';probe.columns={'ID':'int','NOTE':'text'}
        def fake_exec(pod,container,script):
            compile(script,'generated-source-rowpos','exec');self.assertIn('SRC_ROWPOS_COUNT',script)
            return {'kind':'ibmi-source-rowpos-reader','positions':[]}
        probe.exec_json=fake_exec
        probe.source_positions(({'pod':'reader','container':'reader'},{}),before)

    def test_ack_must_be_true_boolean(self):
        with self.assertRaises(ValueError):
            p.validate_receipt({"acknowledged":"true","write_started_utc":"2026-09-30T10:00:01+00:00",
                                "write_ack_utc":"2026-09-30T10:00:02+00:00"},"2026-09-30T10:00:00+00:00")

    def test_negative_latency_rejected(self):
        with self.assertRaises(ValueError):
            p.latencies("2026-09-30T10:00:01+00:00","2026-09-30T10:00:02+00:00","2026-09-30T10:00:00+00:00")

    def test_history_without_oracle_is_incomplete(self):
        with self.assertRaises(ValueError): p.history_proof([],None)

    def test_history_corruption_and_missing_coverage_rejected(self):
        event={"ID":1,"EVENT_ID":"e1","OPERATION":"c","JOURNAL_RECEIVER":"R1","JOURNAL_SEQUENCE":1}
        with self.assertRaises(ValueError): p.history_proof([], [event])
        with self.assertRaises(ValueError): p.history_proof([{**event,"ID":2}],[event])
        with self.assertRaises(ValueError): p.history_proof([{**event,"JOURNAL_SEQUENCE":2}],[event])

    def test_unknown_resume_and_missing_operation_receipt_rejected(self):
        incomplete={"snapshot_checks_passed":True,"checkpoint":None,"reconciliation":{"equal":True},"duplicate_event_ids":0}
        with self.assertRaises(ValueError): p.evaluate_resume(incomplete,incomplete,None,None)

    def test_provider_and_role_both_required(self):
        with self.assertRaises(ValueError):
            p.identity_proof({"method":"env","arn":"arn:aws:sts::000000000000:assumed-role/test/session"},
                             "vm-metadata","arn:aws:iam::000000000000:role/test")

    def test_effective_identity_accepts_only_matching_role(self):
        result=p.identity_proof({"method":"iam-role","arn":"arn:aws:sts::000000000000:assumed-role/test/session"},
                                "vm-metadata","arn:aws:iam::000000000000:role/test")
        self.assertTrue(result['verified'])
        with self.assertRaises(ValueError):
            p.identity_proof({"method":"iam-role","arn":"arn:aws:sts::000000000000:assumed-role/other/session"},
                             "vm-metadata","arn:aws:iam::000000000000:role/test")

    def test_history_exact_oracle_and_duplicate_positions(self):
        event={"ID":1,"EVENT_ID":"e1","OPERATION":"c","JOURNAL_RECEIVER":"R1","JOURNAL_SEQUENCE":1}
        self.assertTrue(p.history_proof([event],[event])['equal'])
        with self.assertRaises(ValueError):
            p.history_proof([event,{**event,'EVENT_ID':'e2'}],[event,{**event,'EVENT_ID':'e2'}])

    def test_expired_deadline_does_not_call_kubectl(self):
        probe=object.__new__(p.NativeProbe);probe.deadline=0
        with patch.object(p.subprocess,'run') as command:
            with self.assertRaises(RuntimeError): probe.kubectl('get','pods')
            command.assert_not_called()

    def test_generated_snowflake_probe_compiles_and_binds_native_contract(self):
        from argparse import Namespace
        probe=object.__new__(p.NativeProbe)
        probe.columns={'ID':'int','NOTE':'text'};probe.library='SOURCE';probe.table='ORDERS'
        probe.args=Namespace(database='DB',schema='SCHEMA',table_id='table1',pk='ID',history='ORDERS_HISTORY',mirror='ORDERS_MIRROR')
        def fake_exec(pod,container,script):
            compile(script,'generated-snowflake-probe','exec')
            for required in ['QUADRINGENT_LOADER_TABLE_SET_JSON','l.build_plan','plan.history_table','STATEMENT_TIMEOUT_IN_SECONDS','history_mode']:
                self.assertIn(required,script)
            return {"history_mode":"streaming"}
        probe.exec_json=fake_exec
        probe.snowflake(({'pod':'pod','container':'destination-loader'},
                         {'QUADRINGENT_DESTINATION_DATABASE':'DB','QUADRINGENT_DESTINATION_SCHEMA':'SCHEMA'}))


    def test_out_input_collision_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path=Path(directory)/"baseline.json";path.write_text("{}")
            with self.assertRaises(ValueError): p.check_output(path,[path])

    def test_exact_decimal_and_null(self):
        result = p.compare_rows([{"ID":1,"VALUE":"1.20","NOTE":None}],
                                [{"ID":"1","VALUE":"1.2","NOTE":None}],
                                {"ID":"int","VALUE":"decimal","NOTE":"text"},"ID")
        self.assertTrue(result["equal"])

    def test_spaces_and_empty_are_not_normalized_away(self):
        for a,b in [("x ","x"),(None,""),(""," ")]:
            result=p.compare_rows([{"ID":1,"NOTE":a}],[{"ID":1,"NOTE":b}],{"ID":"int","NOTE":"text"},"ID")
            self.assertFalse(result["equal"])

    def test_duplicate_primary_keys_rejected(self):
        with self.assertRaises(ValueError):
            p.compare_rows([{"ID":1},{"ID":"1"}],[],{"ID":"int"},"ID")

    def test_fractional_integer_rejected(self):
        with self.assertRaises(ValueError): p.normalize("1.1","int")

    def test_null_primary_key_rejected(self):
        with self.assertRaises(ValueError):
            p.compare_rows([{"ID":None}],[],{"ID":"int"},"ID")

    def test_checkpoint_missing_is_unmeasured(self):
        self.assertEqual(p.resume_evidence(None,{"receiver":"A","sequence":2})["status"],"unmeasured")

    def test_checkpoint_regression_and_rotation(self):
        before={"receiver":"A","sequence":9}
        self.assertEqual(p.resume_evidence(before,{"receiver":"A","sequence":8})["status"],"FAIL")
        after={"receiver":"B","sequence":1}
        self.assertEqual(p.resume_evidence(before,after)["status"],"unmeasured")
        self.assertEqual(p.resume_evidence(before,after,["A","B"])["status"],"PASS")

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory);target=root/"real.json";target.write_text("protected")
            link=root/"link.json";link.symlink_to(target)
            with self.assertRaises((OSError,ValueError)): p.write_private(link,{"example":1})
            self.assertEqual(target.read_text(),"protected")

    def test_private_output_permissions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            target=Path(directory)/"proof.json"
            p.write_private(target,{"status":"PASS"})
            self.assertEqual(target.stat().st_mode & 0o777,0o600)

    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            target=Path(directory)/"proof.json";target.write_text("protected")
            with self.assertRaises(OSError): p.write_private(target,{"status":"PASS"})
            self.assertEqual(target.read_text(),"protected")

    def test_ack_in_future_rejected(self):
        receipt={"acknowledged":True,"write_started_utc":"2026-09-30T10:00:01+00:00","write_ack_utc":"2026-09-30T10:00:09+00:00"}
        with patch.object(p,'utc',return_value='2026-09-30T10:00:05+00:00'):
            with self.assertRaises(ValueError): p.validate_receipt(receipt,'2026-09-30T10:00:00+00:00')

    def test_observe_real_timestamps_with_fake_connector(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root=Path(directory)
            baseline={"pipeline":"p1","table":"S.T","table_id":"t1","namespace":"dev","context":"test","observed_utc":"2026-09-30T10:00:00+00:00","snapshot_checks_passed":True,
                      "history":[{"ID":1,"NOTE":"old"}],"mirror":[{"ID":1,"NOTE":"old"}]}
            receipt={"pipeline":"p1","table":"S.T","table_id":"t1","namespace":"dev","context":"test","operation":"update","acknowledged":True,
                     "write_started_utc":"2026-09-30T10:00:01+00:00","write_ack_utc":"2026-09-30T10:00:02+00:00",
                     "expected":{"ID":1,"NOTE":"unique"},"mutation_identity":{"ID":1,"NOTE":"unique"}}
            for name,data in [("baseline",baseline),("receipt",receipt)]: (root/name).write_text(json.dumps(data))
            from argparse import Namespace
            probe=object.__new__(p.NativeProbe)
            probe.args=Namespace(receipt=str(root/"receipt"),baseline=str(root/"baseline"),out=str(root/"out.json"),
                                 pipeline="p1",table="S.T",table_id="t1",namespace="dev",context="test",pk="ID",loader_deployment="loader",loader_digest="digest",
                                 max_seconds=3,poll_seconds=1)
            probe.columns={"ID":"int","NOTE":"text"}
            probe.workload=lambda *args: ({}, {})
            probe.snowflake=lambda *args: {"history":[{"ID":1,"NOTE":"unique","OPERATION":"u_after"}],"mirror":[{"ID":1,"NOTE":"unique"}],"history_mode":"streaming"}
            with patch.object(p,"utc",return_value="2026-09-30T10:00:05+00:00"):
                report=probe.observe()
            self.assertEqual(report["status"],"visible")
            self.assertEqual(report["mirror_from_ack_s"],3)
            self.assertEqual(report["mirror_from_start_s"],4)

    def test_native_workload_rejects_wrong_digest(self):
        from argparse import Namespace
        probe=object.__new__(p.NativeProbe)
        probe.args=Namespace(table_id="t1",identity="irsa")
        deployment={"metadata":{"uid":"d1"},"spec":{"template":{"metadata":{"labels":{"quadringent.io/managed-by":"quadringent-control-plane","quadringent.io/component":"reader"},
                      "annotations":{"quadringent.io/table-ids":"t1"}}},"selector":{"matchLabels":{"app":"reader"}}}}
        pod={"metadata":{"name":"reader","uid":"pod1","ownerReferences":[{"kind":"ReplicaSet","name":"rs1","uid":"r1","controller":True}]},"spec":{"containers":[{"name":"reader","image":"image@wrong"}]},
             "status":{"phase":"Running","containerStatuses":[{"name":"reader","ready":True,"imageID":"image@wrong"}]}}
        rs={"metadata":{"uid":"r1","ownerReferences":[{"kind":"Deployment","uid":"d1","controller":True}]}}
        probe.kubectl=lambda *args: deployment if args[1]=="deployment" else rs if args[1]=="replicaset" else {"items":[pod]}
        with self.assertRaisesRegex(ValueError,"digest exécuté"):
            probe.workload("reader","sha256:"+"a"*64)


class GcpVmIdentityTests(unittest.TestCase):
    def effective(self):
        return {'method':'google-compute-metadata','credential_type':'google.auth.compute_engine.credentials.Credentials',
                'refreshed':True,'project':'qualification-project','service_account_email':'runtime@qualification-project.iam.gserviceaccount.com',
                'instance':{'id':'123456789','name':'quadringent-test-vm','zone':'europe-west1-b','machine_type':'e2-standard-2',
                            'service_account_email':'runtime@qualification-project.iam.gserviceaccount.com',
                            'scopes':['https://www.googleapis.com/auth/cloud-platform']},
                'gcs_read':{'verified':True,'bucket':'qualification-proof','key':'copy/evidence.json','bytes':12,'sha256':'a'*64,'generation':'1'}}

    def expected(self):
        return {'expected_gcp_service_account':'runtime@qualification-project.iam.gserviceaccount.com',
                'expected_gcp_project':'qualification-project','expected_gcs_bucket':'qualification-proof','expected_gcs_key':'copy/evidence.json',
                'expected_gcp_instance_id':'123456789','expected_gcp_instance_name':'quadringent-test-vm',
                'expected_gcp_zone':'europe-west1-b','expected_gcp_machine_type':'e2-standard-2'}

    def test_gcp_vm_sdk_identity_accepts_complete_exact_instance(self):
        self.assertTrue(p.identity_proof(self.effective(),'gcp-vm-metadata',None,**self.expected())['verified'])

    def test_gcp_vm_rejects_scope_missing_instance_and_secret_payload(self):
        cases=[('project','other'),('service_account_email','other@qualification-project.iam.gserviceaccount.com'),
               ('credential_type','google.oauth2.credentials.Credentials'),('method','iam-role'),('refreshed',False),
               ('instance',{}),('access_token','forbidden'),('api_key','forbidden')]
        for key,value in cases:
            with self.subTest(key=key):
                actual=self.effective();actual[key]=value
                with self.assertRaises(ValueError):p.identity_proof(actual,'gcp-vm-metadata',None,**self.expected())
        for key,value in [('id','999'),('name','other-vm'),('zone','europe-west1-c'),('machine_type','e2-medium'),
                          ('service_account_email','other'),('scopes',[]),('token','forbidden')]:
            with self.subTest(instance=key):
                actual=self.effective();actual['instance'][key]=value
                with self.assertRaises(ValueError):p.identity_proof(actual,'gcp-vm-metadata',None,**self.expected())

    def probe(self):
        from argparse import Namespace
        probe=object.__new__(p.NativeProbe)
        kwargs=self.expected();kwargs['gcs_read_key']=kwargs.pop('expected_gcs_key')
        probe.args=Namespace(**kwargs,expected_role_arn=None,identity='gcp-vm-metadata',table_id='t1',pipeline='p1',
                             copy_run_id='run1',copy_boundary='R1:10')
        return probe


    def workload_fixture(self):
        digest='sha256:'+'a'*64
        deployment={'metadata':{'uid':'d1','generation':1},'spec':{'replicas':1,'template':{'metadata':{
            'labels':{'quadringent.io/managed-by':'quadringent-control-plane','quadringent.io/component':'reader'},
            'annotations':{'quadringent.io/table-ids':'t1'}}},'selector':{'matchLabels':{'app':'reader'}}},
            'status':{'readyReplicas':1,'observedGeneration':1}}
        pod={'metadata':{'name':'reader','uid':'p1','ownerReferences':[{'kind':'ReplicaSet','name':'rs1','uid':'r1','controller':True}]},
             'spec':{'nodeName':'quadringent-test-vm','serviceAccountName':'quadringent-capture','containers':[{'name':'reader','image':'private/image@'+digest}]},
             'status':{'phase':'Running','containerStatuses':[{'name':'reader','ready':True,'imageID':'private/image@'+digest}]}}
        rs={'metadata':{'uid':'r1','ownerReferences':[{'kind':'Deployment','uid':'d1','controller':True}]}}
        node={'metadata':{'name':'quadringent-test-vm','labels':{'kubernetes.io/arch':'amd64','kubernetes.io/os':'linux'}},
              'status':{'nodeInfo':{'architecture':'amd64','operatingSystem':'linux'}}}
        sa={'metadata':{'name':'quadringent-capture','annotations':{}}}
        probe=self.probe();calls=[]
        def kube(*args):
            calls.append(args)
            return {'deployment':deployment,'pods':{'items':[pod]},'replicaset':rs,'node':node,'serviceaccount':sa}[args[1]]
        probe.kubectl=kube
        scripts=[]
        def execute(pod,container,script):scripts.append(script);return self.effective()
        probe.exec_json=execute
        return probe,digest,pod,node,scripts,calls

    def test_gcp_vm_workload_uses_google_sdk_and_binds_exact_kubernetes_node(self):
        probe,digest,pod,node,scripts,calls=self.workload_fixture()
        proof,_=probe.workload('reader',digest)
        self.assertEqual(proof['node'],'quadringent-test-vm')
        self.assertEqual(proof['identity_mode'],'gcp-vm-metadata')
        self.assertTrue(proof['identity_effective']['verified'])
        self.assertEqual(len(scripts),1);self.assertNotIn('boto3',scripts[0])
        self.assertTrue(any(x[1]=='node' for x in calls))
        for bad in ['other-vm','']:
            probe,digest,pod,node,scripts,calls=self.workload_fixture();pod['spec']['nodeName']=bad
            with self.assertRaises(ValueError):probe.workload('reader',digest)
            self.assertEqual(scripts,[])

    def test_gcp_vm_empty_effective_sdk_payload_is_rejected(self):
        probe,digest,*_=self.workload_fixture();probe.exec_json=lambda *args:{}
        with self.assertRaises(ValueError):probe.workload('reader',digest)

    def test_gcp_vm_workload_candidate_keeps_instance_and_sku_stable(self):
        probe,digest,*_=self.workload_fixture();proof,_=probe.workload('reader',digest)
        original=p.workload_candidate(proof,'reader')
        changed=deepcopy(proof);changed['identity_effective']['instance']['id']='987654321'
        self.assertNotEqual(original,p.workload_candidate(changed,'reader'))
        changed=deepcopy(proof);changed['node']='other-vm'
        with self.assertRaises(ValueError):p.workload_candidate(changed,'reader')


if __name__ == "__main__": unittest.main()
