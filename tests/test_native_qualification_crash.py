"""Crash lecteur natif : contrats locaux, aucune action sur un cluster."""
from copy import deepcopy
from argparse import Namespace
import json
import time

import pytest

from quadringent.qualification import proof, cli


def crash_fixture():
    from test_native_qualification_proof import PureProofTests
    before,after,operations=PureProofTests().resume_fixture()
    before['reader'].update(pod='reader-pod',node='node1',replicaset='rs1',replicaset_uid='rs-uid',container_id='containerd://old',restart_count=0)
    after['reader'].update(uid='d1',pod='reader-pod',pod_uid='old',node='node1',replicaset='rs1',replicaset_uid='rs-uid',container_id='containerd://new',restart_count=1,running_started_utc='2026-09-30T10:00:03.5+00:00',desired_replicas=1,ready_replicas=1)
    for item in (before,after):
        for key in ('reader','loader'):
            w=item[key];w['selected_pod_uids']=[w['pod_uid']];digest=w['images'][0]['image_id'].split('://')[1]
            w['image_binding']={'verified':True,'observed_digest':digest,'index':digest,'manifest':digest,'config':digest,
                                'architecture':'amd64','revision':'a'*40,'receipt_sha256':'b'*64}
    scope={k:before[k] for k in ('pipeline','table','table_id','namespace','context')}
    request={**scope,'attempts':1,'action':'SIGKILL_reader_child','environment':'dev','nonce':'a'*32,'started_utc':'2026-09-30T10:00:01+00:00',
             'old_reader':deepcopy(before['reader']),'process_stamp':{'pid':2,'starttime':'123','init_starttime':'100','boot_id':'a'*8+'-'+ 'b'*4+'-'+ 'c'*4+'-'+ 'd'*4+'-'+ 'e'*12,'pid_namespace':42,'exe_inode':99,'exe_device':1}}
    crash={**scope,'kind':'native-reader-crash-proof','observed_utc':'2026-09-30T10:00:04+00:00','reader':deepcopy(after['reader']),
           'request':request,'injection':{'armed':True,'exec_exit_code':137,'nonce':'a'*32,'attempts':1,'process_stamp':deepcopy(request['process_stamp'])},
           'terminated':{'containerID':'containerd://old','exitCode':137,'signal':0,'finishedAt':'2026-09-30T10:00:02+00:00'},
           'process_termination_verified':True,'native_cdc_qualified':False}
    operations['mutations'][0]['write_started_utc']='2026-09-30T10:00:03+00:00'
    return before,after,crash,operations['mutations']


def test_crash_oracles_distinct_from_pause_and_not_latency():
    before,after,crash,mutations=crash_fixture()
    result=proof.evaluate_crash(before,after,{'crash':crash,'mutations':mutations})
    assert result['status']=='PASS' and result['crash_recovery_qualified'] is True
    assert result['native_cdc_qualified'] is False and result['maximum_10s_qualified'] is False
    assert 'pause' not in result['scope']


@pytest.mark.parametrize('change',['pod','deployment','rs','container','count','exit','time','nonce','missing_data','duplicates','pause','intent','stamp','scope','oom','exec_error','bool_exit','surge'])
def test_crash_refuses_false_or_unbound_evidence(change):
    before,after,crash,mutations=crash_fixture()
    if change=='pod':crash['reader']['pod_uid']='different'
    if change=='deployment':crash['reader']['uid']='different'
    if change=='rs':crash['reader']['replicaset_uid']='different'
    if change=='container':crash['reader']['container_id']='containerd://old'
    if change=='count':crash['reader']['restart_count']=2
    if change=='exit':crash['terminated']['exitCode']=0
    if change=='time':crash['terminated']['finishedAt']='2026-09-30T09:59:00+00:00'
    if change=='nonce':crash['injection']['nonce']='other'
    if change=='missing_data':after['source_journal_positions']=None
    if change=='duplicates':after['duplicate_event_ids']=1
    if change=='pause':crash['kind']='native-reader-pause-proof'
    if change=='bool_exit':crash['injection']['exec_exit_code']=False
    if change=='surge':crash['reader']['selected_pod_uids'].append('terminating-pod')
    if change=='stamp':crash['injection']['process_stamp']['starttime']='different'
    if change=='scope':crash['request']['namespace']='different'
    if change=='oom':crash['terminated']['reason']='OOMKilled'
    if change=='exec_error':crash['injection']['exec_exit_code']=1
    if change=='intent':crash['request']['old_reader']['container_id']='different'
    with pytest.raises(ValueError):proof.evaluate_crash(before,after,{'crash':crash,'mutations':mutations})


def test_crash_config_requires_separate_explicit_dev_optin():
    from quadringent.qualification.actions import validate_crash_actions
    validate_crash_actions({'enabled':True,'environment':'dev','crash_reader':True})
    for config in ({},{'enabled':True,'environment':'dev'},{'enabled':True,'environment':'prod','crash_reader':True}):
        with pytest.raises(ValueError):validate_crash_actions(config)


def test_process_script_uses_pidfd_guard_and_only_reader_child():
    from quadringent.qualification.actions import process_script
    read=process_script()
    assert '/app/as400_continuous_capture.py' in read and 'tini' in read
    kill=process_script({'pid':2,'starttime':'123','init_starttime':'100','boot_id':'a'*8+'-'+ 'b'*4+'-'+ 'c'*4+'-'+ 'd'*4+'-'+ 'e'*12,'pid_namespace':42,'exe_inode':99,'exe_device':1},nonce='a'*32)
    assert 'pidfd_open' in kill and 'pidfd_send_signal' in kill and 'SIGKILL' in kill
    assert kill.index('!= expected') < kill.index('pidfd_send_signal')
    assert 'flush=True' in kill


def test_intention_failure_prevents_exec_and_races_fail_before_kill():
    from quadringent.qualification import actions
    before,_,_,_=crash_fixture();args=Namespace(**{k:before[k] for k in ('pipeline','table','table_id','namespace','context')},reader_deployment='reader',capture_digest='sha256:'+'a'*64)
    calls=[]
    class Probe:
        deadline=time.monotonic()+10
        def __init__(self):self.args=args
        def workload(self,*unused):return deepcopy(before['reader']),{}
        def exec_json(self,*unused):return {'pid':2,'starttime':'123','init_starttime':'100','boot_id':'a'*8+'-'+ 'b'*4+'-'+ 'c'*4+'-'+ 'd'*4+'-'+ 'e'*12,'pid_namespace':42,'exe_inode':99,'exe_device':1}
        def kubectl(self,*command,**kwargs):
            assert command[:2]==('get','node')
            return {'metadata':{'name':'node1'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}
        def crash_exec(self,*unused):calls.append('kill');raise AssertionError('should not execute')
    def reject(_):raise OSError('private intention unavailable')
    with pytest.raises(OSError):actions.inject_reader_crash(Probe(),before,{'enabled':True,'environment':'dev','crash_reader':True},on_request=reject)
    assert calls==[]


def test_capture_uses_official_tini_and_preserves_reader_arguments():
    from pathlib import Path
    docker=(Path(__file__).parents[1]/'docker/Dockerfile').read_text()
    assert 'libpcre2-8-0 tini' in docker
    assert 'test -s /usr/share/doc/tini/copyright' in docker
    assert 'ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/venv/bin/python", "/app/as400_continuous_capture.py"]' in docker

@pytest.mark.parametrize('failure',['pod_race','container_race','surge','timeout'])
def test_one_injection_only_and_no_kill_on_incarnation_race(failure):
    from quadringent.qualification import actions
    before,_,_,_=crash_fixture()
    args=Namespace(**{k:before[k] for k in ('pipeline','table','table_id','namespace','context')},reader_deployment='reader',capture_digest='sha256:'+'a'*64)
    calls=[]
    class Probe:
        def __init__(self):self.args=args;self.reads=0
        def workload(self,*unused):
            self.reads+=1;reader=deepcopy(before['reader'])
            if self.reads==2 and failure=='pod_race':reader['pod_uid']='replacement'
            if self.reads==2 and failure=='container_race':reader['container_id']='replacement'
            if self.reads==2 and failure=='surge':reader['selected_pod_uids'].append('terminating-pod')
            return reader,{}
        def exec_json(self,*unused):return {'pid':2,'starttime':'123','init_starttime':'100','boot_id':'a'*8+'-'+ 'b'*4+'-'+ 'c'*4+'-'+ 'd'*4+'-'+ 'e'*12,'pid_namespace':42,'exe_inode':99,'exe_device':1}
        def kubectl(self,*command,**kwargs):return {'metadata':{'name':'node1'},'status':{'conditions':[{'type':'Ready','status':'True'}]}}
        def crash_exec(self,*unused):
            calls.append('kill')
            raise TimeoutError('ambiguous exec')
    intentions=[]
    with pytest.raises((ValueError,TimeoutError)):
        actions.inject_reader_crash(Probe(),before,{'enabled':True,'environment':'dev','crash_reader':True},on_request=intentions.append)
    assert len(intentions)==1
    assert calls==(['kill'] if failure=='timeout' else [])


def test_crash_phases_are_explicit_and_legacy_defaults_preserved():
    assert {'crash','bind-crash-mutations','evaluate-crash'}<=set(cli.PHASES)
    assert 'crash_proof' in cli.REFS
    assert cli.validate_actions({'enabled':True,'environment':'dev','crash_reader':True}) is None
    assert cli.validate_actions({'enabled':True,'environment':'dev','api_config':'private.json'})=='private.json'
    before,after,crash,mutations=crash_fixture()
    mutations[0]['write_started_utc']='2026-09-30T10:00:01.5+00:00'
    with pytest.raises(ValueError):proof.evaluate_crash(before,after,{'crash':crash,'mutations':mutations})


def test_ack_after_restarted_container_start_is_not_downtime():
    before,after,crash,mutations=crash_fixture()
    mutations[0]['write_ack_utc']='2026-09-30T10:00:03.6+00:00'
    with pytest.raises(ValueError):proof.evaluate_crash(before,after,{'crash':crash,'mutations':mutations})


def test_failed_durable_intention_does_not_return_success(tmp_path,monkeypatch):
    def unavailable(_):raise OSError('cannot persist')
    monkeypatch.setattr(proof.os,'fsync',unavailable)
    with pytest.raises(OSError):proof.write_private(tmp_path/'intent.json',{'action':'SIGKILL_reader_child'})
