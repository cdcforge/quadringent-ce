"""Tests privés, sans réseau, Kubernetes ni secrets."""
from copy import deepcopy
import unittest
from quadringent.qualification import proof as n

class GkeIdentityTests(unittest.TestCase):
    def identity(self):
        return {'method':'google-compute-metadata','credential_type':'google.auth.compute_engine.credentials.Credentials',
                'service_account_email':'reader@qualification-project.iam.gserviceaccount.com',
                'project':'qualification-project','refreshed':True,
                'gcs_read':{'verified':True,'bucket':'qualification-bucket','key':'copy/proof.json',
                            'bytes':123,'sha256':'a'*64}}
    def verify(self, value, mode='gke-workload-identity'):
        return n.identity_proof(value,mode,None,expected_gcp_service_account='reader@qualification-project.iam.gserviceaccount.com',
                                expected_gcp_project='qualification-project',expected_gcs_bucket='qualification-bucket',
                                expected_gcs_key='copy/proof.json')
    def test_gke_metadata_identity_accepted(self):
        self.assertTrue(self.verify(self.identity())['verified'])
    def test_gke_rejects_static_user_unrefreshed_wrong_account_or_project(self):
        for field,value in [('method','service-account-file'),('credential_type','google.oauth2.credentials.Credentials'),
                            ('refreshed',False),('service_account_email','other@qualification-project.iam.gserviceaccount.com'),
                            ('project','other-project')]:
            with self.subTest(field=field):
                identity=self.identity();identity[field]=value
                with self.assertRaises(ValueError):self.verify(identity)
    def test_gke_refuses_partial_or_wrong_gcs_read(self):
        for field,value in [('verified',False),('bucket','other-bucket'),('key','unrelated.json'),('bytes',0),('sha256','bad')]:
            with self.subTest(field=field):
                identity=self.identity();identity['gcs_read'][field]=value
                with self.assertRaises(ValueError):self.verify(identity)
        identity=self.identity();identity.pop('gcs_read')
        with self.assertRaises(ValueError):self.verify(identity)
    def test_unknown_mode_refused_even_aws_identity(self):
        value={'method':'assume-role-with-web-identity','arn':'arn:aws:sts::000000000000:assumed-role/native/session'}
        with self.assertRaises(ValueError):n.identity_proof(value,'unknown','arn:aws:iam::000000000000:role/native')
    def test_gke_candidate_identity_stable_and_changes_refused(self):
        identity=self.verify(self.identity())
        proof={'container':'reader','images':[{'name':'reader','image_id':'containerd://sha256:'+'a'*64}],
               'service_account':'reader-ksa','identity_mode':'gke-workload-identity','identity_effective':identity}
        before=n.workload_candidate(proof,'reader');same=deepcopy(proof)
        self.assertEqual(before,n.workload_candidate(same,'reader'))
        same['identity_effective']['service_account_email']='other@qualification-project.iam.gserviceaccount.com'
        self.assertNotEqual(before,n.workload_candidate(same,'reader'))


class OracleCollectorTests(unittest.TestCase):
    def fixture(self):
        from argparse import Namespace
        import hashlib
        args=Namespace(pipeline='p1',table='S.T',table_id='t1',namespace='dev',context='site',
                       copy_run_id='run1',copy_boundary='R1:10',journal_library='S',journal_name='J')
        def event(journal,receiver,seq,note):
            return {'source_system':'ibmi','journal':journal,'library':'S','table':'T','operation':'c',
                    'journal_receiver':receiver,'journal_sequence':seq,'event_id':hashlib.sha256(f'ibmi|{journal}|{receiver}|{seq}'.encode()).hexdigest(),
                    'before':None,'after':{'ID':seq,'NOTE':note}}
        copy={'pipeline_id':'p1','table_id':'t1','run_id':'run1','boundary':{'receiver_name':'R1','receiver_library':'S','last_sequence':10},
              'rows_copied':1,'records':[event('SNAPSHOT:S.T','SNAPSHOT:run1',1,'old')],
              'artifacts':[{'verified':True,'sha256':'a'*64}]}
        journal={'receiver':'R1','start':11,'end':11,'decoded':1,'scan_complete':True,'records':[event('J','R1',11,'new')]}
        positions={'kind':'ibmi-source-rowpos-reader','positions':[{'JOURNAL_RECEIVER_NAME':'R1','SEQUENCE_NUMBER':11}]}
        return copy,journal,positions,{'receiver':'R1','sequence':11},args,{'ID':'int','NOTE':'text'}
    def test_complete_oracle_is_independent_of_snowflake(self):
        from quadringent.qualification import collect as l
        result=l.validate_oracle_parts(*self.fixture())
        self.assertEqual(len(result['events']),2);self.assertTrue(result['independent_of_snowflake'])
        self.assertFalse(result['native_cdc_qualified'])
    def test_partial_scope_missing_rows_or_positions_refused(self):
        from quadringent.qualification import collect as l
        for change in ('partial','scope','snapshot-count','rowpos','duplicate','wrong-event-id','image','wrong-run','rotation'):
            with self.subTest(change=change):
                copy,journal,positions,checkpoint,args,columns=self.fixture()
                if change=='partial':journal['scan_complete']=False
                if change=='scope':journal['records'][0]['table']='OTHER'
                if change=='snapshot-count':copy['rows_copied']=2
                if change=='rowpos':positions['positions']=[]
                if change=='duplicate':positions['positions']*=2
                if change=='wrong-event-id':journal['records'][0]['event_id']='b'*64
                if change=='image':journal['records'][0]['after']={'_rrn':11}
                if change=='wrong-run':copy['run_id']='other'
                if change=='rotation':checkpoint['receiver']='R2'
                with self.assertRaises(ValueError):l.validate_oracle_parts(copy,journal,positions,checkpoint,args,columns)
    def test_rbac_error_propagates_without_partial_oracle(self):
        from quadringent.qualification import collect as l
        from argparse import Namespace
        from unittest.mock import Mock
        probe=Mock(spec=n.NativeProbe);probe.args=Namespace(max_seconds=30,reader_deployment='r',loader_deployment='l',capture_digest='a',loader_digest='b')
        probe.workload.side_effect=RuntimeError('native_kubernetes_probe_failed')
        with self.assertRaises(RuntimeError):l.LiveCollector(probe).oracle()
        probe.exec_json.assert_not_called()
    def test_scope_and_config_secret_entries_refused(self):
        from quadringent.qualification import collect as l
        import json,tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config';path.write_text(json.dumps({'password':'never-allowed'}))
            with self.assertRaises(ValueError):l.load_config(path)
        *_,args,columns=self.fixture()
        with self.assertRaises(ValueError):l.scope_matches({'pipeline':'other'},args)

class ImageBindingTests(unittest.TestCase):
    def mapping(self):
        return {'index':'sha256:'+'a'*64,'revision':'revision','variants':{
            'amd64':{'manifest':'sha256:'+'b'*64,'config':'sha256:'+'c'*64},
            'arm64':{'manifest':'sha256:'+'d'*64,'config':'sha256:'+'e'*64}},'receipt_sha256':'f'*64}
    def test_image_id_index_manifest_or_config_bound_to_arch(self):
        for char in ('a','b','c'):
            binding=n.image_binding(self.mapping(),'sha256:'+'a'*64,'containerd://sha256:'+char*64,'amd64')
            self.assertEqual(binding['index'],'sha256:'+'a'*64)
    def test_unrelated_digest_other_arch_and_wrong_spec_refused(self):
        for spec,observed,arch in [('a','d','amd64'),('a','b','arm64'),('a','f','amd64'),('b','b','amd64'),('a','a','s390x')]:
            with self.subTest(spec=spec,observed=observed,arch=arch):
                with self.assertRaises(ValueError):n.image_binding(self.mapping(),'sha256:'+spec*64,'containerd://sha256:'+observed*64,arch)

class NativeGkeCollectorTests(unittest.TestCase):
    def probe(self,annotation=True,architecture='amd64',observed='b'):
        from argparse import Namespace
        from unittest.mock import Mock
        probe=object.__new__(n.NativeProbe)
        probe.args=Namespace(reader_deployment='reader',loader_deployment='loader',table_id='t1',identity='gke-workload-identity',expected_role_arn=None,
         expected_gcp_service_account='reader@qualification-project.iam.gserviceaccount.com',expected_gcp_project='qualification-project',
         expected_gcs_bucket='qualification-bucket',gcs_read_key='copy/proof.json',pipeline='p1',copy_run_id='run1',copy_boundary='R1:10')
        probe.release_images={'capture':ImageBindingTests().mapping()}
        deployment={'metadata':{'uid':'d1','generation':1},'status':{'readyReplicas':1,'observedGeneration':1},'spec':{'replicas':1,
          'template':{'metadata':{'labels':{'quadringent.io/managed-by':'quadringent-control-plane','quadringent.io/component':'reader'},
                                   'annotations':{'quadringent.io/table-ids':'t1'}}},'selector':{'matchLabels':{'app':'reader'}}}}
        pod={'metadata':{'name':'reader-pod','uid':'pod1','ownerReferences':[{'kind':'ReplicaSet','name':'rs','uid':'rs1','controller':True}]},
          'spec':{'nodeName':'node1','serviceAccountName':'reader-ksa','containers':[{'name':'reader','image':'registry/capture@sha256:'+'a'*64}]},
          'status':{'phase':'Running','containerStatuses':[{'name':'reader','ready':True,'imageID':'containerd://sha256:'+observed*64}]}}
        rs={'metadata':{'uid':'rs1','ownerReferences':[{'kind':'Deployment','uid':'d1','controller':True}]}}
        sa={'metadata':{'annotations':{'iam.gke.io/gcp-service-account':probe.args.expected_gcp_service_account if annotation else 'wrong@other.iam.gserviceaccount.com'}}}
        node={'metadata':{'name':'node1','labels':{'kubernetes.io/arch':architecture,'kubernetes.io/os':'linux'}},
              'status':{'nodeInfo':{'architecture':architecture,'operatingSystem':'linux'}}}
        objects={'deployment':deployment,'pods':{'items':[pod]},'replicaset':rs,'serviceaccount':sa,'node':node}
        probe.kubectl=Mock(side_effect=lambda *args:objects[args[1]])
        probe.exec_json=Mock(return_value=GkeIdentityTests().identity())
        return probe
    def test_actual_metadata_script_and_exact_container(self):
        probe=self.probe();proof,_=probe.workload('reader','sha256:'+'a'*64)
        self.assertTrue(proof['identity_effective']['verified']);self.assertEqual(proof['image_binding']['architecture'],'amd64')
        pod,container,script=probe.exec_json.call_args.args
        self.assertEqual((pod,container),('reader-pod','reader'))
        for required in ('credentials.refresh(request)','type(credentials) is not metadata_credentials.Credentials',
                         'download_as_bytes(if_generation_match=blob.generation','pipeline_id','table_id','run_id'):
            self.assertIn(required,script)
        self.assertNotIn('credentials.token',script)
        compile(script,'identity-inline','exec')
    def test_wrong_annotation_refused_before_sdk(self):
        probe=self.probe(annotation=False)
        with self.assertRaises(ValueError):probe.workload('reader','sha256:'+'a'*64)
        probe.exec_json.assert_not_called()
    def test_node_rbac_or_other_arch_refused_before_sdk(self):
        other_arch=self.probe(architecture='arm64')
        with self.assertRaises(ValueError):other_arch.workload('reader','sha256:'+'a'*64)
        other_arch.exec_json.assert_not_called()
        denied_probe=self.probe();original=denied_probe.kubectl.side_effect
        def denied(*args):
            if args[1]=='node':raise RuntimeError('native_kubernetes_probe_failed')
            return original(*args)
        denied_probe.kubectl.side_effect=denied
        with self.assertRaises(RuntimeError):denied_probe.workload('reader','sha256:'+'a'*64)
        denied_probe.exec_json.assert_not_called()


class HistoryTypedValuesTests(unittest.TestCase):
    def test_exact_numeric_normalization_preserves_precision_null_and_spaces(self):
        base={'ID':'1','AMOUNT':'1.2000000000000000000000000000000000001','NOTE':'x ','EVENT_ID':'e','OPERATION':'c','JOURNAL_RECEIVER':'R','JOURNAL_SEQUENCE':1}
        columns={'ID':'int','AMOUNT':'decimal','NOTE':'text'}
        clean=n.normalize_history([base],columns)[0]
        self.assertEqual(clean['AMOUNT'],'1.2000000000000000000000000000000000001');self.assertEqual(clean['NOTE'],'x ')
        other={**base,'AMOUNT':'1.200'}
        self.assertEqual(n.normalize_history([other],columns)[0]['AMOUNT'],'1.2')
        other['NOTE']=None;self.assertIsNone(n.normalize_history([other],columns)[0]['NOTE'])
        with self.assertRaises(ValueError):n.history_proof([clean],n.normalize_history([{**base,'AMOUNT':'1.2'}],columns))


class MutationBindingTests(unittest.TestCase):
    def fixture(self):
        from argparse import Namespace
        from test_native_qualification_proof import PureProofTests
        before,after,ops=PureProofTests().resume_fixture()
        for item in (before,after,ops['pause'],ops['resume'],ops['mutations'][0]):item['table']='S.T'
        args=Namespace(**{k:before[k] for k in ('pipeline','table','table_id','namespace','context')},copy_run_id='run1',copy_boundary='R1:10',pk='ID',columns={'ID':'int','N':'int','NOTE':'text'})
        event={**after['history'][-1],'ID':6001,'N':6002,'NOTE':'qdt test1234'}
        ack={k:v for k,v in ops['mutations'][0].items() if k!='expected_events'}
        ack.update(source_positions=[{'receiver':'R1','sequence':11}],mutation_identity={'ID':6001,'N':6002,'NOTE':'qdt test1234'})
        oracle={'kind':'ibmi-journal-and-snapshot-oracle',**{k:before[k] for k in ('pipeline','table','table_id','namespace','context')},
                'independent_of_snowflake':True,'copy_run_id':'run1','copy_boundary':'R1:10','events':[before['history'][0],event],
                'checkpoint':after['checkpoint'],'observed_utc':after['observed_utc']}
        return [ack],oracle,ops['pause'],ops['resume'],before,args
    def test_real_ack_bound_to_independent_row_image(self):
        from quadringent.qualification import collect as l
        from unittest.mock import patch
        with patch.object(n,'utc',return_value='2026-09-30T10:00:06+00:00'):
            result=l.bind_mutation_receipts(*self.fixture())
        self.assertEqual(result[0]['expected_events'][0]['N'],6002)
    def test_positions_collected_from_unique_independent_image_not_tail(self):
        from quadringent.qualification import collect as l
        from unittest.mock import patch
        data=list(self.fixture());data[0][0].pop('source_positions')
        with patch.object(n,'utc',return_value='2026-09-30T10:00:06+00:00'):
            result=l.bind_mutation_receipts(*data)
        self.assertEqual(result[0]['source_positions'],[{'receiver':'R1','sequence':11}])
        data[1]['events'].append({**data[1]['events'][-1],'EVENT_ID':'duplicate-match','JOURNAL_SEQUENCE':12})
        data[1]['checkpoint']['sequence']=12
        with patch.object(n,'utc',return_value='2026-09-30T10:00:06+00:00'):
            with self.assertRaises(ValueError):l.bind_mutation_receipts(*data)
    def test_absent_ack_wrong_value_or_unrelated_source_event_refused(self):
        from quadringent.qualification import collect as l
        from unittest.mock import patch
        for change in ('ack','rows','identity','value','position','declared-events'):
            with self.subTest(change=change):
                data=list(self.fixture());ack=data[0][0]
                if change=='ack':ack['acknowledged']='true'
                if change=='rows':ack['affected_rows']=2
                if change=='identity':ack['mutation_identity']['ID']=6003
                if change=='value':ack['mutation_identity']['N']=6003
                if change=='position':ack['source_positions'][0]['sequence']=10
                if change=='declared-events':ack['expected_events']=[]
                with patch.object(n,'utc',return_value='2026-09-30T10:00:06+00:00'):
                    with self.assertRaises(ValueError):l.bind_mutation_receipts(*data)

class InlineReadOnlyScriptsTests(unittest.TestCase):
    def test_copy_and_journal_payloads_compile_and_are_bounded(self):
        from quadringent.qualification import collect as l
        from argparse import Namespace
        from unittest.mock import Mock
        args=Namespace(table_id='t1',table='S.T',pipeline='p1',copy_run_id='run1',copy_boundary='R1:10',database='DB',schema='DEV',pk='ID',history='T_HISTORY',mirror='T_MIRROR')
        probe=Namespace(args=args,columns={'ID':'int','NOTE':'text'},library='S',table='T',exec_json=Mock(return_value={}))
        collector=l.LiveCollector(probe);workload=({'pod':'native','container':'reader'},{})
        collector.copy(workload)
        copy_script=probe.exec_json.call_args.args[2];compile(copy_script,'copy-inline','exec')
        collector.journal(workload,{'boundary':{'last_sequence':10,'receiver_name':'R1','receiver_library':'S'}},{'receiver':'R1','sequence':11})
        journal_script=probe.exec_json.call_args.args[2];compile(journal_script,'journal-inline','exec')
        self.assertIn("'cmd': 'sql_window'",journal_script);self.assertIn("'max_server_entries': 1",journal_script)
        self.assertNotIn("'cmd': 'exec'",journal_script);self.assertNotIn('put_once(',copy_script)

class GcpVmConfigTests(unittest.TestCase):
    def config(self):
        from quadringent.qualification import collect as l
        data={k:'synthetic' for k in l.ALLOWED}
        for k in ('expected_role_arn','max_seconds','poll_seconds'):data.pop(k,None)
        data.update(expected_history_mode='streaming',identity='gcp-vm-metadata',capture_digest='sha256:'+'a'*64,loader_digest='sha256:'+'a'*64,
                    expected_gcp_project='qualification-project',expected_gcp_service_account='runtime@qualification-project.iam.gserviceaccount.com',
                    expected_gcp_instance_id='123456789',expected_gcp_instance_name='quadringent-test-vm',
                    expected_gcp_zone='europe-west1-b',expected_gcp_machine_type='e2-standard-2')
        return data
    def load(self,data):
        from quadringent.qualification import collect as l
        import json,tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';path.write_text(json.dumps(data))
            return l.load_config(path)
    def test_typed_gcp_vm_config_reaches_native_probe_without_aws_role(self):
        args=self.load(self.config())
        self.assertEqual(args.identity,'gcp-vm-metadata');self.assertIsNone(args.expected_role_arn)
        from test_native_qualification_proof import GcpVmIdentityTests
        probe=GcpVmIdentityTests().probe();probe.args=args
        self.assertTrue(probe.verify_identity(GcpVmIdentityTests().effective() | {
            'gcs_read':{'verified':True,'bucket':args.expected_gcs_bucket,'key':args.gcs_read_key,
                        'bytes':12,'sha256':'a'*64,'generation':'1'}})['verified'])
    def test_gcp_vm_config_requires_all_instance_fields_and_gsa_project(self):
        for key in ('expected_gcp_instance_id','expected_gcp_instance_name','expected_gcp_zone','expected_gcp_machine_type'):
            with self.subTest(missing=key):
                data=self.config();data.pop(key)
                with self.assertRaises(ValueError):self.load(data)
        for key,value in [('expected_gcp_instance_id','not-id'),('expected_gcp_zone','not-zone'),
                          ('expected_gcp_instance_name','UPPER'),('expected_gcp_machine_type',''),
                          ('expected_gcp_service_account','wrong@other-project.iam.gserviceaccount.com')]:
            with self.subTest(invalid=key):
                data=self.config();data[key]=value
                with self.assertRaises(ValueError):self.load(data)
    def test_gcp_vm_config_refuses_aws_fallback_and_sensitive_entries(self):
        for key,value in [('identity','vm-metadata'),('identity','unknown'),('api_key','forbidden'),('access_token','forbidden')]:
            with self.subTest(key=key,value=value):
                data=self.config();data[key]=value
                with self.assertRaises(ValueError):self.load(data)


if __name__=="__main__":unittest.main()
