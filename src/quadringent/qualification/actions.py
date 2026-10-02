"""SIGKILL DEV borné du lecteur enfant : intention durable, aucune répétition."""
from __future__ import annotations

import json
import re
import uuid

from . import proof


def validate_crash_actions(value):
    if (not isinstance(value,dict) or set(value)-{'enabled','environment','api_config','crash_reader'}
            or value.get('enabled') is not True or value.get('environment')!='dev'
            or value.get('crash_reader') is not True):
        raise ValueError('crash lecteur exige activation DEV explicite distincte')


def validate_process_stamp(stamp):
    keys={'pid','starttime','init_starttime','boot_id','pid_namespace','exe_inode','exe_device'}
    if (not isinstance(stamp,dict) or set(stamp)!=keys or type(stamp['pid']) is not int or stamp['pid']<=1
            or any(type(stamp[k]) is not int or stamp[k]<0 for k in ('pid_namespace','exe_inode','exe_device'))
            or any(not isinstance(stamp[k],str) or not stamp[k].isdigit() for k in ('starttime','init_starttime'))
            or not isinstance(stamp['boot_id'],str) or not re.fullmatch(r'[0-9a-f-]{36}',stamp['boot_id'])):
        raise ValueError('garde processus complète absente')


def process_script(expected=None, *, nonce=None):
    """pidfd garde l'incarnation même si le PID meurt après la dernière lecture."""
    common='''import os,json,signal,hashlib
from pathlib import Path
def argv(pid):return [x.decode() for x in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\\0') if x]
def stat_fields(pid):return Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
def read_stamp():
 if argv(1)!=['/usr/bin/tini','-g','--','/opt/venv/bin/python','/app/as400_continuous_capture.py']:raise ValueError('init mismatch')
 candidates=[int(x) for x in Path('/proc/1/task/1/children').read_text().split()]
 readers=[pid for pid in candidates if argv(pid)==['/opt/venv/bin/python','/app/as400_continuous_capture.py']]
 if len(readers)!=1:raise ValueError('reader child ambiguous')
 pid=readers[0];fields=stat_fields(pid)
 if pid<=1 or int(fields[1])!=1 or os.stat(f'/proc/{pid}').st_uid!=os.geteuid():raise ValueError('reader owner mismatch')
 exe=os.stat(f'/proc/{pid}/exe');wanted=os.stat('/opt/venv/bin/python')
 if (exe.st_dev,exe.st_ino)!=(wanted.st_dev,wanted.st_ino):raise ValueError('reader executable mismatch')
 namespace=os.stat('/proc/self/ns/pid').st_ino
 if namespace!=os.stat('/proc/1/ns/pid').st_ino:raise ValueError('pid namespace mismatch')
 return {'pid':pid,'starttime':fields[19],'init_starttime':stat_fields(1)[19],
         'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
         'pid_namespace':namespace,'exe_inode':exe.st_ino,'exe_device':exe.st_dev}
'''
    if expected is None:return common+"print(json.dumps(read_stamp()))\n"
    validate_process_stamp(expected)
    if not re.fullmatch(r'[0-9a-f]{32}',nonce or ''):
        raise ValueError('garde processus ou nonce invalide')
    return common+f'''expected={expected!r}
fd=os.pidfd_open(expected['pid'],0)
try:
 if read_stamp() != expected:raise ValueError('reader incarnation changed')
 print('QDT_CRASH_ARMED '+json.dumps({{'nonce':{nonce!r},'process_stamp':expected}}),flush=True)
 signal.pidfd_send_signal(fd,signal.SIGKILL,None,0)
finally:os.close(fd)
'''


def reader_incarnation(reader):
    fields=('deployment','uid','pod','pod_uid','container','container_id','restart_count','replicaset','replicaset_uid','node')
    if (any(reader.get(k) in (None,'') for k in fields) or reader['container']!='reader'
            or type(reader['restart_count']) is not int or reader['restart_count']<0
            or reader.get('selected_pod_uids')!=[reader.get('pod_uid')]
            or reader.get('image_binding',{}).get('verified') is not True):
        raise ValueError('incarnation lecteur native incomplète')
    return tuple(reader[k] for k in fields),proof.workload_candidate(reader,'reader')


def inject_reader_crash(probe,before,actions,*,on_request):
    validate_crash_actions(actions)
    scope=('pipeline','table','table_id','namespace','context')
    if (before.get('snapshot_checks_passed') is not True or
            any(before.get(k)!=getattr(probe.args,k) for k in scope)):
        raise ValueError('baseline crash invalide')
    previous=before['reader'];reference=reader_incarnation(previous)
    current,_=probe.workload(probe.args.reader_deployment,probe.args.capture_digest)
    if reader_incarnation(current)!=reference:raise ValueError('lecteur courant hors baseline')
    node=probe.kubectl('get','node',current['node'],'-o','json')
    if (node.get('metadata',{}).get('name')!=current['node'] or
            not any(c.get('type')=='Ready' and c.get('status')=='True' for c in node.get('status',{}).get('conditions',[]))):
        raise ValueError('nœud lecteur non prêt')
    stamp=probe.exec_json(current['pod'],'reader',process_script())
    validate_process_stamp(stamp)
    request={**{k:getattr(probe.args,k) for k in scope},'action':'SIGKILL_reader_child','environment':'dev',
             'nonce':uuid.uuid4().hex,'started_utc':proof.utc(),'old_reader':current,'process_stamp':stamp,
             'status':'request_planned','attempts':1}
    # Une erreur de persistance empêche toute mutation. Un reçu existant interdit un rejeu CLI.
    on_request(request)
    rechecked,_=probe.workload(probe.args.reader_deployment,probe.args.capture_digest)
    if reader_incarnation(rechecked)!=reference:raise ValueError('pod ou conteneur changé avant injection')
    injection=probe.crash_exec(current['pod'],'reader',process_script(stamp,nonce=request['nonce']))
    if (injection.get('armed') is not True or injection.get('nonce')!=request['nonce']
            or injection.get('process_stamp')!=stamp or type(injection.get('exec_exit_code')) is not int or injection.get('exec_exit_code') not in (0,137)):
        raise ValueError('injection non liée à la garde processus ; aucune répétition')
    return request,{**injection,'attempts':1}
