"""Contrats de qualification native, sans réseau ni workload synthétisé en production."""
import json
from copy import deepcopy
from io import StringIO
from pathlib import Path

import pytest

from quadringent.qualification import cli
from quadringent.installer.cli import run


def private(path, data):
    path.write_text(json.dumps(data));path.chmod(0o600)
    return str(path)


def test_installer_routes_native_help():
    with pytest.raises(SystemExit) as error:
        run(['qualification', 'native', '--help'])
    assert error.value.code == 0


def test_private_config_and_error_do_not_print_payload(tmp_path):
    config=tmp_path/'site.json';config.write_text('{"token":"never-show-test-value"}');config.chmod(0o644)
    output=StringIO()
    assert cli.run_native(str(config),str(tmp_path/'out'),'evaluate',stdout=output)==1
    assert 'never-show-test-value' not in output.getvalue()
    assert 'INCOMPLETE' in output.getvalue()


def fixture(tmp_path):
    from test_native_qualification_proof import PureProofTests
    before,after,operations=PureProofTests().resume_fixture()
    before['history_mode']=after['history_mode']='streaming'
    for item in (before,after,operations['resume']):
        for key in ('reader','loader'):
            image=item[key];digest=image['images'][0]['image_id'].split('://')[1]
            image['image_binding']={'verified':True,'observed_digest':digest,'index':digest,'manifest':digest,'config':digest,
                                    'architecture':'amd64','revision':'a'*40,'receipt_sha256':'b'*64}
    ack=operations['mutations'][0]
    ack['expected']={'ID':2,'NOTE':'new'}
    ack['mutation_identity']=dict(ack['expected'])
    ack_path=Path(private(tmp_path/'ack.json',ack))
    baseline_path=Path(private(tmp_path/'baseline.json',before))
    observed={k:before[k] for k in ('pipeline','table','table_id','namespace','context')}
    observed.update(status='visible',observed_threshold_met=True,threshold_s=10,history_mode='streaming',
                    latency_kind='first_observed_upper_bound',write_started_utc=ack['write_started_utc'],write_ack_utc=ack['write_ack_utc'],
                    history_visible_utc='2026-09-30T10:00:05+00:00',mirror_visible_utc='2026-09-30T10:00:05+00:00',
                    receipt_sha256=cli.sha(ack_path),baseline_sha256=cli.sha(baseline_path))
    return before,after,operations,[observed],[ack_path],baseline_path


def test_complete_bounded_resume_and_real_receipt_visibility_can_pass(tmp_path):
    data=fixture(tmp_path)
    result=cli.evaluate(*data,expected_history_mode='streaming')
    assert result['native_cdc_qualified'] is True
    assert result['rotation_qualified'] is False
    assert result['maximum_10s_qualified'] is False


@pytest.mark.parametrize('change',['missing','slow','ack','receipt','scope','mode','baseline','duplicate','start'])
def test_missing_or_unbound_visibility_never_passes(tmp_path,change):
    data=list(fixture(tmp_path));observed=data[3][0]
    if change=='missing':data[3]=[]
    if change=='slow':observed['mirror_visible_utc']='2026-09-30T10:00:30+00:00'
    if change=='ack':observed['write_ack_utc']='2026-09-30T10:00:02+00:00'
    if change=='receipt':observed['receipt_sha256']='f'*64
    if change=='scope':observed['namespace']='other'
    if change=='mode':observed['history_mode']='sql'
    if change=='baseline':observed['baseline_sha256']='f'*64
    if change=='duplicate':data[3].append(deepcopy(observed))
    if change=='start':observed['write_started_utc']='2026-09-30T10:00:03+00:00'
    with pytest.raises(ValueError):cli.evaluate(*data,expected_history_mode='streaming')


def test_actions_default_readonly_and_dev_scoped():
    assert cli.validate_actions({}) is None
    for config in ({'enabled':True},{'enabled':True,'environment':'prod','api_config':'test'},
                   {'enabled':True,'environment':'dev','api_config':'test','scale':0}):
        with pytest.raises(ValueError):cli.validate_actions(config)


def test_fresh_conserved_lifecycle_and_receipt_confinement(tmp_path,monkeypatch):
    from test_native_qualification_admission import fixture as admission_fixture, NOW
    f=admission_fixture(tmp_path)
    release={'mode':'fresh','repository':f[0].repository,'visibility':'private','run_id':123,'attempt':1,
             'source_sha':f[0].source,'tag':f[0].tag,'workflow_sha256':f[0].workflow_sha256,'artifact_id':99,
             'artifact_zip':str(f[2]),'metadata_artifact_id':100,'metadata_artifact_zip':str(f[7]),'metadata_root':str(f[1])}
    f[1].chmod(0o700)
    monkeypatch.setattr(cli.admission,'GitHub',lambda repo:f[3])
    class Clock:
        @staticmethod
        def now(zone):return NOW
    monkeypatch.setattr(cli,'datetime',Clock)
    proof_path=tmp_path/'admission.json'
    cli.release_proof(release,proof_path)
    release.update(mode='conserved',admission=str(proof_path),admission_sha256=cli.sha(proof_path))
    assert cli.release_proof(release,tmp_path/'unused.json')==str(proof_path)
    assert not (tmp_path/'unused.json').exists()
    (f[1]/'passed.json').write_bytes(b'{}')
    with pytest.raises(ValueError):cli.release_proof(release,tmp_path/'unused.json')


@pytest.mark.parametrize('change',['pin','boolean','metadata','extra'])
def test_conserved_drift_refused(tmp_path,monkeypatch,change):
    from test_native_qualification_admission import fixture as admission_fixture, execute
    f=admission_fixture(tmp_path);report,raw=execute(f)
    report['conserved_artifacts']={'receipt':{'digest':cli.admission.digest(f[2].read_bytes()),'size_in_bytes':f[2].stat().st_size},
                                  'metadata':{'digest':cli.admission.digest(f[7].read_bytes()),'size_in_bytes':f[7].stat().st_size}}
    path=Path(private(tmp_path/'admission.json',report));(f[1]/'passed.json').write_bytes(raw)
    release={'mode':'conserved','repository':f[0].repository,'visibility':'private','run_id':123,'attempt':1,'source_sha':f[0].source,
             'tag':f[0].tag,'workflow_sha256':f[0].workflow_sha256,'artifact_id':99,'artifact_zip':str(f[2]),'metadata_artifact_id':100,
             'metadata_artifact_zip':str(f[7]),'metadata_root':str(f[1]),'admission':str(path),'admission_sha256':cli.sha(path)}
    if change=='pin':release['admission_sha256']='f'*64
    if change=='boolean':private(path,{'complete_seal_verified':True});release['admission_sha256']=cli.sha(path)
    if change=='metadata':(f[1]/'capture/index.json').write_bytes(b'{}')
    if change=='extra':(f[1]/'extra.json').write_bytes(b'{}')
    with pytest.raises((ValueError,KeyError)):cli.release_proof(release,tmp_path/'unused.json')


def test_action_checks_api_scope_before_post(tmp_path,monkeypatch):
    from argparse import Namespace
    from quadringent.installer import api_client
    path=private(tmp_path/'api.json',{'url':'https://example.invalid','token':'test-do-not-print'})
    class Client:
        writes=[]
        def __init__(self,config):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,path,**kwargs):
            body={'id':'p1','declared_state':'live'} if path.endswith('/p1') else {'items':[{'id':'p1','table_id':'wrong'}],'next_cursor':None}
            return Namespace(exit_code=0,body=body)
        def write(self,*args,**kwargs):self.writes.append(args);return Namespace(exit_code=0,idempotency_key='test')
    monkeypatch.setattr(api_client,'ApiClient',Client)
    with pytest.raises(ValueError):cli.native_action('pause',Namespace(pipeline='p1',table_id='t1'),{'enabled':True,'environment':'dev','api_config':path})
    assert Client.writes==[]


def test_delete_observes_history_tombstone_and_mirror_absence(tmp_path,monkeypatch):
    from argparse import Namespace
    from quadringent.qualification import proof
    scope={'pipeline':'p1','table':'S.T','table_id':'t1','namespace':'dev','context':'test'}
    baseline={**scope,'snapshot_checks_passed':True,'observed_utc':'2026-09-30T10:00:00+00:00',
              'history':[{'ID':1,'NOTE':'old','OPERATION':'c'}],'mirror':[{'ID':1,'NOTE':'old'}]}
    ack={**scope,'acknowledged':True,'operation':'delete','expected':{'ID':1},'mutation_identity':{'ID':1},
         'write_started_utc':'2026-09-30T10:00:01+00:00','write_ack_utc':'2026-09-30T10:00:02+00:00'}
    p=object.__new__(proof.NativeProbe);p.columns={'ID':'int','NOTE':'text'}
    p.args=Namespace(**scope,baseline=private(tmp_path/'base',baseline),receipt=private(tmp_path/'ack',ack),pk='ID',
                     loader_deployment='loader',loader_digest='sha256:'+'a'*64,max_seconds=1,poll_seconds=1)
    p.workload=lambda *args:({}, {})
    p.snowflake=lambda *args:{'history':[{'ID':1,'NOTE':'old','OPERATION':'d'}],'mirror':[],'history_mode':'streaming'}
    monkeypatch.setattr(proof,'utc',lambda:'2026-09-30T10:00:03+00:00')
    assert p.observe()['observed_threshold_met'] is True


def test_native_reader_and_loader_bind_different_release_components(tmp_path,monkeypatch):
    from argparse import Namespace
    from quadringent.qualification import proof
    columns=private(tmp_path/'columns.json',{'ID':'int'})
    args=Namespace(columns=columns,pk='ID',table='S.T',database='DB',schema='S',history='H',mirror='M',journal_library='S',journal_name='J',
                   reader_deployment='reader',loader_deployment='loader',release_oci_root='unused',release_receipt='unused',
                   expected_revision='a'*40,release_admission='unused',capture_digest='sha256:'+'a'*64,loader_digest='sha256:'+'b'*64)
    monkeypatch.setattr(proof,'release_bindings',lambda *args:{'capture':{'index':'sha256:'+'a'*64},'control-plane':{'index':'sha256:'+'b'*64}})
    assert proof.NativeProbe(args).release_images['control-plane']['index']==args.loader_digest
    args.loader_digest=args.capture_digest
    with pytest.raises(ValueError):proof.NativeProbe(args)


def test_loader_arch_manifest_uses_control_plane_not_capture():
    from test_native_qualification_proof import GcpVmIdentityTests
    from quadringent.qualification import proof
    p,index,pod,node,scripts,calls=GcpVmIdentityTests().workload_fixture()
    p.args.reader_deployment='reader';p.args.loader_deployment='loader'
    cp_index='sha256:'+'b'*64
    variants={'amd64':{'manifest':'sha256:'+'c'*64,'config':'sha256:'+'e'*64},
              'arm64':{'manifest':'sha256:'+'d'*64,'config':'sha256:'+'f'*64}}
    p.release_images={'capture':{'index':index,'variants':{'amd64':{'manifest':'sha256:'+'1'*64,'config':'sha256:'+'2'*64}},'revision':'a'*40,'receipt_sha256':'3'*64},
                      'control-plane':{'index':cp_index,'variants':variants,'revision':'a'*40,'receipt_sha256':'3'*64}}
    original=p.kubectl
    def kube(*args):
        value=original(*args)
        if args[1]=='deployment':value['spec']['template']['metadata']['labels']['quadringent.io/component']='destination-loader'
        return value
    p.kubectl=kube
    pod['spec']['containers'][0].update(name='destination-loader',image='example/image@'+cp_index)
    pod['status']['containerStatuses'][0].update(name='destination-loader',imageID='containerd://'+variants['amd64']['manifest'])
    result=p.workload('loader',cp_index)[0]
    assert result['image_binding']['manifest']==variants['amd64']['manifest']
    pod['status']['containerStatuses'][0]['imageID']='containerd://'+variants['arm64']['manifest']
    with pytest.raises(ValueError):p.workload('loader',cp_index)


def test_checkpoint_generated_script_executes_with_public_storage_backend(monkeypatch,capsys):
    from argparse import Namespace
    from quadringent.qualification import proof
    from quadringent.storage_backend import StorageBackend
    p=object.__new__(proof.NativeProbe);p.args=Namespace()
    calls=[]
    class Store:
        @staticmethod
        def load():return Namespace(receiver='R1',sequence=42)
    class Backend:
        def checkpoint_store(self,key):calls.append(key);return Store()
    monkeypatch.setattr(StorageBackend,'from_environment',classmethod(lambda cls,env:Backend()))
    for key in ('QUADRINGENT_STORAGE_BACKEND','AS400_STREAM_KEY','AS400_CHECKPOINT_BUCKET','AS400_RAW_BUCKET'):
        monkeypatch.setenv(key,'test')
    def execute(pod,container,script):
        assert 'as400_continuous_capture' not in script
        compile(script,'checkpoint-probe','exec');exec(script,{})
        return json.loads(capsys.readouterr().out)
    p.exec_json=execute
    env={'QUADRINGENT_STORAGE_BACKEND':'gcs','AS400_STREAM_KEY':'source|S.J',
         'AS400_CHECKPOINT_BUCKET':'state','AS400_RAW_BUCKET':'raw'}
    result=p.checkpoint(({},env),({'pod':'native-loader','container':'destination-loader'},{}))
    assert result=={'receiver':'R1','sequence':42}
    assert calls==['source|S.J']


def test_action_never_posts_after_common_deadline(tmp_path,monkeypatch):
    from argparse import Namespace
    from quadringent.installer import api_client
    path=private(tmp_path/'api.json',{'url':'https://example.invalid','token':'test'})
    clock=[0]
    class Client:
        writes=[]
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,path,**kwargs):
            clock[0]+=2
            return Namespace(exit_code=0,body={'id':'p1','declared_state':'live'} if path.endswith('/p1') else {'items':[{'id':'p1','table_id':'t1'}],'next_cursor':None})
        def write(self,*args,**kwargs):self.writes.append(args);return Namespace(exit_code=0,idempotency_key='test')
    monkeypatch.setattr(api_client,'ApiClient',Client)
    monkeypatch.setattr(cli.time,'monotonic',lambda:clock[0])
    with pytest.raises(ValueError):cli.native_action('pause',Namespace(pipeline='p1',table_id='t1',deadline=1),{'enabled':True,'environment':'dev','api_config':path})
    assert Client.writes==[]


def test_action_verifies_scope_transition_and_keeps_request_receipt(tmp_path,monkeypatch):
    from argparse import Namespace
    from quadringent.installer import api_client
    path=private(tmp_path/'api.json',{'url':'https://example.invalid','token':'test-never-print'})
    events=[]
    class Client:
        def __init__(self,config):self.state='live'
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,path,**kwargs):
            body={'id':'p1','declared_state':self.state} if path.endswith('/p1') else {'items':[{'id':'p1','table_id':'t1'}],'next_cursor':None}
            return Namespace(exit_code=0,body=body)
        def write(self,method,path,**kwargs):
            assert len(events)==1 and events[0]['executed'] is None
            assert method=='POST' and path=='/v2/pipelines/p1/actions/pause'
            assert kwargs['json_body']=={'dry_run':False}
            self.state='paused';return Namespace(exit_code=0,idempotency_key=kwargs['idempotency_key'])
    monkeypatch.setattr(api_client,'ApiClient',Client)
    result=cli.native_action('pause',Namespace(pipeline='p1',table_id='t1'),{'enabled':True,'environment':'dev','api_config':path},on_request=events.append)
    assert result['executed'] is True
    assert result['idempotency_key']==events[0]['idempotency_key']
    assert 'token' not in json.dumps(result)


def test_deadline_adapter_updates_real_httpx_request_timeouts(monkeypatch):
    import httpx
    from quadringent.installer.api_client import ApiClient,ClientConfig
    observed=[]
    def response(request):
        observed.append(request.extensions['timeout'])
        return httpx.Response(200,json={'id':'p1'})
    client=ApiClient(ClientConfig('https://example.invalid','test'),transport=httpx.MockTransport(response))
    monkeypatch.setattr(cli.time,'monotonic',lambda:12)
    with cli.DeadlineApi(client,15) as bounded:assert bounded.get('/v2/pipelines/p1').exit_code==0
    assert all(value==3 for value in observed[0].values())


@pytest.mark.parametrize('phase',['snapshot','oracle'])
def test_snapshot_preserves_expired_phase_budget_before_subprocess(monkeypatch,phase):
    from argparse import Namespace
    from unittest.mock import Mock
    from quadringent.qualification import proof
    p=object.__new__(proof.NativeProbe)
    p.args=Namespace(max_seconds=10,reader_deployment='reader',capture_digest='unused',kubeconfig='/unused',context='test',namespace='dev')
    p.deadline=0
    p.workload=lambda *args:p.kubectl('get','deployment','reader','-o','json')
    command=Mock(side_effect=RuntimeError('stop'))
    monkeypatch.setattr(proof.subprocess,'run',command);monkeypatch.setattr(proof.time,'monotonic',lambda:5)
    with pytest.raises(RuntimeError):
        if phase=='snapshot':p.snapshot()
        else:cli.collect.LiveCollector(p).oracle()
    command.assert_not_called()


def test_destructive_or_unknown_native_action_is_refused_without_api_access():
    for phase in ('remove','restart-initial-copy','scale','apply'):
        with pytest.raises(ValueError):cli.native_action(phase,None,{})


@pytest.mark.parametrize('archive',['receipt','metadata'])
def test_fresh_admission_refuses_archive_replaced_before_anchor(tmp_path,monkeypatch,archive):
    from test_native_qualification_admission import fixture as admission_fixture, NOW
    f=admission_fixture(tmp_path);f[1].chmod(0o700)
    value={'mode':'fresh','repository':f[0].repository,'visibility':'private','run_id':123,'attempt':1,'source_sha':f[0].source,
           'tag':f[0].tag,'workflow_sha256':f[0].workflow_sha256,'artifact_id':99,'artifact_zip':str(f[2]),'metadata_artifact_id':100,
           'metadata_artifact_zip':str(f[7]),'metadata_root':str(f[1])}
    original=cli.admission.admit
    def admit(*args,**kwargs):
        result=original(*args,**kwargs)
        (f[2] if archive=='receipt' else f[7]).write_bytes(b'changed-after-admission')
        return result
    class Clock:
        @staticmethod
        def now(zone):return NOW
    monkeypatch.setattr(cli,'datetime',Clock);monkeypatch.setattr(cli.admission,'GitHub',lambda repo:f[3])
    monkeypatch.setattr(cli.admission,'admit',admit)
    with pytest.raises(ValueError):cli.release_proof(value,tmp_path/'anchor.json')
    assert not (tmp_path/'anchor.json').exists()
    assert not (f[1]/'passed.json').exists()
