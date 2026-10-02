"""Phases natives explicites, configuration et preuves privées, erreurs expurgées."""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from . import actions, collect, proof, release_admission as admission

PHASES = ('admit-release','oracle','baseline','snapshot','observe','positions','pause','resume','bind-mutations','evaluate','crash','bind-crash-mutations','evaluate-crash')
REFS = {'crash_proof','before','after','history_oracle','baseline','receipt','ack_receipts','pause_proof','resume_proof','mutation_receipts','observations','observation_receipts'}


def sha(path):
    return hashlib.sha256(admission.read_regular(path, admission.MAX_RECEIPT_BYTES)).hexdigest()


def private_json(path):
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    if mode != 0o600:raise ValueError('fichier privé 0600 requis')
    return admission.document(admission.read_regular(path, admission.MAX_RECEIPT_BYTES))


def private_dir(path):
    path = Path(path)
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('dossier symlink refusé')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError('dossier privé 0700 requis')
    return path


def validate_actions(value):
    if not isinstance(value,dict) or set(value)-{'enabled','environment','api_config','crash_reader'}:
        raise ValueError('actions non prises en charge')
    if value.get('crash_reader') is True:
        actions.validate_crash_actions(value)
        if 'api_config' not in value:return None
    if value.get('enabled',False) is False:return None
    if value.get('enabled') is not True or value.get('environment') != 'dev' or not isinstance(value.get('api_config'),str):
        raise ValueError('actions natives exigent activation explicite DEV')
    return value['api_config']


def release_expected(value):
    required={'mode','repository','visibility','run_id','attempt','source_sha','tag','workflow_sha256',
              'artifact_id','artifact_zip','metadata_artifact_id','metadata_artifact_zip','metadata_root'}
    if not isinstance(value,dict) or not required <= set(value) or set(value)-required-{'admission','admission_sha256'}:
        raise ValueError('configuration admission incomplète')
    if value['mode'] not in {'fresh','conserved'} or value['visibility'] not in {'private','public'}:
        raise ValueError('visibilité et provenance explicites requises')
    return admission.Expected(value['run_id'],value['attempt'],value['source_sha'],value['tag'],value['workflow_sha256'],
                              value['repository'],value['visibility']=='private')


def release_proof(value,output):
    expected=release_expected(value);root=Path(value['metadata_root'])
    if value['mode']=='fresh':
        report,raw=admission.admit(expected,root,value['artifact_zip'],value['artifact_id'],
                                   admission.GitHub(expected.repository),datetime.now(timezone.utc),
                                   metadata_archive=value['metadata_artifact_zip'],metadata_artifact_id=value['metadata_artifact_id'])
        # Conserve aussi les identités originales des archives pour vérification ultérieure hors rétention CI.
        report['conserved_artifacts']={}
        for key,path,maximum,digest_field in (
                ('receipt',value['artifact_zip'],admission.MAX_ZIP_BYTES,'artifact_zip_digest'),
                ('metadata',value['metadata_artifact_zip'],admission.MAX_METADATA_ZIP_BYTES,'metadata_artifact_zip_digest')):
            archive_bytes=admission.read_regular(path,maximum)
            archive_digest=admission.digest(archive_bytes)
            if archive_digest!=report['authenticated_ci'][digest_field]:
                raise ValueError('archive modifiée avant conservation de l’ancre')
            report['conserved_artifacts'][key]={'digest':archive_digest,'size_in_bytes':len(archive_bytes)}
        private_dir(root)
        receipt=root/'passed.json'
        if receipt.exists():
            if admission.read_regular(receipt,admission.MAX_RECEIPT_BYTES) != raw:raise ValueError('reçu conservé différent')
        else:admission.write_private(receipt,raw)
        proof.write_private(output,report)
        return output
    # Le SHA d'admission est une ancre locale explicitement épinglée par l'opérateur lors de l'admission fraîche.
    # Ce mode n'affirme aucune authentification GitHub nouvelle après expiration des artefacts.
    path=value.get('admission');pinned=value.get('admission_sha256','')
    if not path or not admission.HEX.fullmatch(pinned) or sha(path) != pinned:
        raise ValueError('admission conservée non épinglée')
    report=private_json(path);ci=report['authenticated_ci']
    if (ci['repository']!=expected.repository or ci['run_id']!=expected.run_id or ci['run_attempt']!=expected.attempt or
            ci['workflow_sha256']!=expected.workflow_sha256 or ci['artifact_id']!=value['artifact_id'] or
            ci['metadata_artifact_id']!=value['metadata_artifact_id'] or report['expected_private']!=expected.private or
            report['tag']!=expected.tag or report['revision']!=expected.source):
        raise ValueError('admission conservée hors release')
    archives=report['conserved_artifacts']
    if archives['receipt']['digest']!=ci['artifact_zip_digest'] or archives['metadata']['digest']!=ci['metadata_artifact_zip_digest']:
        raise ValueError('archives hors preuve authentifiée')
    raw,files=admission.receipt_bytes(value['artifact_zip'],archives['receipt'])
    originals=admission.metadata_archive_bytes(value['metadata_artifact_zip'],archives['metadata'],files)
    admission.materialize_metadata(root,originals,receipt_raw=raw)
    if admission.read_regular(root/'passed.json',admission.MAX_RECEIPT_BYTES)!=raw:raise ValueError('reçu conservé altéré')
    proof.release_bindings(root,root/'passed.json',expected.source,path)
    return path


def load_site(path, *, admission_only=False):
    data=private_json(path)
    if not isinstance(data,dict) or set(data)-{'native','release','refs','actions'} or 'release' not in data or (not admission_only and 'native' not in data):
        raise ValueError('configuration native inconnue')
    refs=data.get('refs',{})
    if not isinstance(refs,dict) or set(refs)-REFS:raise ValueError('références inconnues')
    for key,value in refs.items():
        paths=value if key in {'observations','observation_receipts'} else [value]
        if not isinstance(paths,list) or not paths or any(not isinstance(p,str) for p in paths):raise ValueError('références invalides')
        for item in paths:private_json(item)
    validate_actions(data.get('actions',{}));release_expected(data['release'])
    if admission_only:return data
    native=data['native']
    if not isinstance(native,dict) or set(native)-collect.ALLOWED or native.get('expected_history_mode')!='streaming':
        raise ValueError('contrat natif Streaming explicite requis')
    # Les chemins et identités admission sont exclusivement dérivés du contrat release, jamais d'un booléen fourni.
    if set(native)&{'release_oci_root','release_receipt','release_admission','expected_revision'}:
        raise ValueError('double configuration release refusée')
    private_json(native['columns'])
    for key in ('namespace','reader_deployment','loader_deployment'):
        if not isinstance(native.get(key),str) or re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',native[key]) is None:
            raise ValueError('périmètre Kubernetes invalide')
    if not isinstance(native.get('context'),str) or not native['context'] or native['context'].startswith('-') or any(c.isspace() for c in native['context']):
        raise ValueError('contexte Kubernetes explicite invalide')
    kubeconfig=Path(native['kubeconfig'])
    if not kubeconfig.is_absolute() or any(p.is_symlink() for p in [kubeconfig,*kubeconfig.parents]):
        raise ValueError('kubeconfig explicite régulier requis')
    if not stat.S_ISREG(kubeconfig.stat().st_mode):raise ValueError('kubeconfig régulier requis')
    if not isinstance(native.get('pipeline'),str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}',native['pipeline']) is None:
        raise ValueError('pipeline invalide')
    return data


def evaluate(before,after,operations,observations,receipts,baseline,*,expected_history_mode):
    for snapshot in (before,after):
        for component in ('reader','loader'):
            if snapshot.get(component,{}).get('image_binding',{}).get('verified') is not True:
                raise ValueError('liaison release exécutée absente')
    result=proof.evaluate_resume(before,after,None,operations)
    if expected_history_mode!='streaming' or any(s.get('history_mode')!=expected_history_mode for s in (before,after)):
        raise ValueError('mode HISTORY non attesté')
    if len(observations)!=len(receipts) or len(receipts)!=len(operations['mutations']) or not 1<=len(receipts)<=20:
        raise ValueError('couverture latence incomplète')
    scope={k:before[k] for k in collect.SCOPE};seen=set();bounds=[]
    for observed,path,bound in zip(observations,receipts,operations['mutations'],strict=True):
        ack=private_json(path);digest=sha(path)
        if (digest in seen or observed.get('receipt_sha256')!=digest or observed.get('baseline_sha256')!=sha(baseline) or
                any(observed.get(k)!=v or ack.get(k)!=v for k,v in scope.items()) or
                observed.get('status')!='visible' or observed.get('latency_kind')!='first_observed_upper_bound' or
                observed.get('threshold_s')!=10 or observed.get('observed_threshold_met') is not True or
                observed.get('history_mode')!=expected_history_mode or
                ack.get('expected',ack.get('mutation_identity'))!=ack.get('mutation_identity') or
                any(ack.get(k)!=bound.get(k) for k in ('write_started_utc','write_ack_utc','operation','mutation_identity')) or
                any(observed.get(k)!=ack.get(k) for k in ('write_started_utc','write_ack_utc'))):
            raise ValueError('observation latence non liée aux ACK source')
        proof.validate_receipt(ack,before['observed_utc']);seen.add(digest)
        for layer in ('history','mirror'):
            durations=proof.latencies(ack['write_started_utc'],ack['write_ack_utc'],observed[layer+'_visible_utc'])
            if max(durations.values())>10:raise ValueError('visibilité dépasse 10 secondes')
            bounds.append(durations['from_start_s'])
    if private_json(baseline)!=before:raise ValueError('baseline latence différente de before')
    return {**result,'native_cdc_qualified':True,'scope':'same_receiver_pause_mutations_resume_and_observed_visibility',
            'observed_threshold_met':True,'observed_mutations':len(receipts),'observed_max_from_start_s':max(bounds),
            'maximum_10s_qualified':False,'rotation_qualified':False,
            'limitations':['receiver_rotation_not_observed','observed_bounds_are_not_a_universal_latency_slo']}


class DeadlineApi:
    """Adaptateur local du client installé : timeout résiduel avant chaque appel."""
    def __init__(self,client,deadline):self.client=client;self.deadline=deadline
    def __enter__(self):self.client.__enter__();return self
    def __exit__(self,*args):self.client.__exit__(*args)
    def call(self,method,*args,**kwargs):
        remaining=min(30,self.deadline-time.monotonic())
        if remaining<=0:raise ValueError('deadline API expirée')
        if hasattr(self.client,'_http'):
            import httpx
            self.client._http.timeout=httpx.Timeout(remaining)
        return getattr(self.client,method)(*args,**kwargs)
    def get(self,*args,**kwargs):return self.call('get',*args,**kwargs)
    def write(self,*args,**kwargs):return self.call('write',*args,**kwargs)


def native_action(phase,args,actions,*,on_request=None):
    if phase not in {'pause','resume'}:raise ValueError('action hors périmètre')
    path=validate_actions(actions)
    if path is None:return {'executed':False,'mode':'read_only_observation'}
    private_json(path)
    from quadringent.installer.api_client import ApiClient,load_config
    # env={} empêche la configuration explicite de viser une autre cible via l'environnement local.
    config=load_config(env={},config_path=Path(path))
    if not config.token:raise ValueError('action API authentifiée requise')
    start=proof.utc()
    deadline=getattr(args,'deadline',time.monotonic()+60)
    with DeadlineApi(ApiClient(config),deadline) as client:
        selected=client.get(f'/v2/pipelines/{args.pipeline}')
        if selected.exit_code!=0 or not isinstance(selected.body,dict) or selected.body.get('id')!=args.pipeline:
            raise ValueError('pipeline API absent')
        cursor=None;seen=set();found=[]
        for _ in range(20):
            if time.monotonic()>=deadline:raise ValueError('listing API hors budget')
            listed=client.get('/v2/pipelines',params={'limit':100,**({'cursor':cursor} if cursor else {})})
            if listed.exit_code!=0 or not isinstance(listed.body,dict) or not isinstance(listed.body.get('items'),list):
                raise ValueError('périmètre API non observable')
            found.extend(item for item in listed.body['items'] if item.get('id')==args.pipeline)
            cursor=listed.body.get('next_cursor')
            if cursor is None:break
            if not isinstance(cursor,str) or cursor in seen:raise ValueError('pagination API incohérente')
            seen.add(cursor)
        else:raise ValueError('listing API trop grand')
        if len(found)!=1 or found[0].get('table_id')!=args.table_id:
            raise ValueError('pipeline API hors table')
        key='native-qualification-'+uuid.uuid4().hex
        request={'action':phase,'pipeline':args.pipeline,'table_id':args.table_id,'environment':'dev',
                 'started_utc':start,'idempotency_key':key,'executed':None,'status':'request_planned'}
        if on_request:on_request(request)
        result=client.write('POST',f'/v2/pipelines/{args.pipeline}/actions/{phase}',json_body={'dry_run':False},idempotency_key=key)
        if result.exit_code!=0:raise ValueError('action native refusée ou acquittement absent')
        verified=client.get(f'/v2/pipelines/{args.pipeline}')
        expected_state='paused' if phase=='pause' else 'live'
        if verified.exit_code!=0 or not isinstance(verified.body,dict) or verified.body.get('id')!=args.pipeline or verified.body.get('declared_state')!=expected_state:
            raise ValueError('transition API native non vérifiée')
    return {'executed':True,'environment':'dev','action':phase,'pipeline':args.pipeline,
            'started_utc':start,'acknowledged_utc':proof.utc(),'idempotency_key':key}


def run_native(config,out_dir,phase,*,stdout=None):
    stdout=stdout or sys.stdout
    try:
        if phase not in PHASES:raise ValueError('phase inconnue')
        data=load_site(config,admission_only=phase=='admit-release');out=private_dir(out_dir);target=out/(phase+'.json')
        if target.exists() or (out/(phase+'.action.json')).exists() or (out/(phase+'.action-request.json')).exists():raise ValueError('preuve ou action existante')
        release=data['release']
        if phase=='admit-release' and release['mode']!='fresh':raise ValueError('admission fraîche requise')
        if phase!='admit-release' and release['mode']!='conserved':raise ValueError('admission épinglée préalable requise')
        admission_path=release_proof(release,target if phase=='admit-release' else out/(phase+'.admission.json'))
        if phase=='admit-release':
            print(json.dumps({'status':'admitted','admission_sha256':sha(admission_path),'proof':str(target.resolve()),
                              'native_cdc_qualified':False}),file=stdout);return 0
        native={**data['native'],'release_oci_root':release['metadata_root'],'release_receipt':str(Path(release['metadata_root'])/'passed.json'),
                'release_admission':str(admission_path),'expected_revision':release['source_sha']}
        # Réutilise le validateur natif éprouvé sans créer de configuration temporaire persistante.
        args=collect.config_values(native)
        refs=data.get('refs',{})
        for key in ('before','after','history_oracle','baseline','receipt'):setattr(args,key,refs.get(key))
        args.out=str(target);probe=proof.NativeProbe(args);probe.deadline=time.monotonic()+args.max_seconds;args.deadline=probe.deadline
        def read(key):return private_json(refs[key])
        if phase=='oracle':report=collect.LiveCollector(probe).oracle()
        elif phase=='baseline':
            oracle=collect.LiveCollector(probe).oracle();oracle_path=out/'baseline.oracle.json';proof.write_private(oracle_path,oracle)
            args.history_oracle=str(oracle_path);report=probe.snapshot()
        elif phase=='snapshot':
            if not args.history_oracle:raise ValueError('oracle requis')
            report=probe.snapshot()
        elif phase=='observe':
            if not args.baseline or not args.receipt:raise ValueError('baseline et ACK requis')
            report=probe.observe()
        elif phase=='positions':
            before=read('before');collect.scope_matches(before,args)
            reader=probe.workload(args.reader_deployment,args.capture_digest)
            report={**probe.source_positions(reader,before),**{k:getattr(args,k) for k in collect.SCOPE},'observed_utc':proof.utc()}
        elif phase=='crash':
            before=read('before');collect.scope_matches(before,args)
            request,injection=actions.inject_reader_crash(probe,before,data.get('actions',{}),
                on_request=lambda event:proof.write_private(out/'crash.action-request.json',event))
            proof.write_private(out/'crash.action.json',{'request':request,'injection':injection})
            while True:
                try:report=probe.crash_state(before,request,injection);break
                except (ValueError,RuntimeError):
                    if time.monotonic()+args.poll_seconds>=probe.deadline:raise
                    time.sleep(args.poll_seconds)
        elif phase=='bind-crash-mutations':
            report=collect.bind_crash_mutation_receipts(*(read(k) for k in ('ack_receipts','history_oracle','crash_proof','before')),args)
        elif phase in {'pause','resume'}:
            before=read('before');collect.scope_matches(before,args)
            # Valide le candidat initial AVANT toute action ; reprise autorisée uniquement depuis une pause réellement observée.
            if before.get('snapshot_checks_passed') is not True:raise ValueError('baseline non qualifiée')
            if phase=='resume':
                paused=read('pause_proof');collect.scope_matches(paused,args)
                if paused.get('reader',{}).get('deployment_absent') is not True or paused['reader'].get('owned_pods')!=[]:
                    raise ValueError('absence native non attestée')
            if phase=='pause' and validate_actions(data.get('actions',{})):
                current=probe.workload(args.reader_deployment,args.capture_digest)[0]
                if proof.workload_candidate(current,'reader')!=proof.workload_candidate(before['reader'],'reader'):
                    raise ValueError('lecteur courant hors baseline')
            action=native_action(phase,args,data.get('actions',{}),on_request=lambda event:proof.write_private(out/(phase+'.action-request.json'),event))
            proof.write_private(out/(phase+'.action.json'),action)
            while True:
                try:report=probe.reader_state(phase,before);break
                except (ValueError,RuntimeError):
                    if time.monotonic()+args.poll_seconds>=probe.deadline:raise
                    time.sleep(args.poll_seconds)
            report['native_action']=action
        elif phase=='bind-mutations':
            report=collect.bind_mutation_receipts(*(read(k) for k in ('ack_receipts','history_oracle','pause_proof','resume_proof','before')),args)
        else:
            before,after=read('before'),read('after')
            for item in (before,after):collect.scope_matches(item,args)
            # L'identité et le candidat exécutés sont réobservés ; aucune ressource supplémentaire n'est créée.
            for deployment,digest,key in ((args.reader_deployment,args.capture_digest,'reader'),(args.loader_deployment,args.loader_digest,'loader')):
                current=probe.workload(deployment,digest)[0]
                if phase=='evaluate-crash' and key=='reader' and actions.reader_incarnation(current)!=actions.reader_incarnation(after[key]):
                    raise ValueError('incarnation ou unicité lecteur changée après crash')
                if proof.workload_candidate(current,current['container'])!=proof.workload_candidate(after[key],current['container']):
                    raise ValueError('candidat courant différent du snapshot')
            if phase=='evaluate-crash':
                report=proof.evaluate_crash(before,after,{'crash':read('crash_proof'),'mutations':read('mutation_receipts')})
            else:
                report=evaluate(before,after,{key:read(ref) for key,ref in (('pause','pause_proof'),('resume','resume_proof'),('mutations','mutation_receipts'))},
                                [private_json(path) for path in refs['observations']],[Path(path) for path in refs['observation_receipts']],
                                refs['baseline'],expected_history_mode=args.expected_history_mode)
        proof.write_private(target,report)
        failed=isinstance(report,dict) and (report.get('snapshot_checks_passed') is False or report.get('status') in {'timeout','INCOMPLETE'} or report.get('observed_threshold_met') is False)
        print(json.dumps({'status':'INCOMPLETE' if failed else 'recorded','phase':phase,'proof':str(target.resolve()),
                          'native_cdc_qualified':isinstance(report,dict) and report.get('native_cdc_qualified') is True}),file=stdout)
        return int(failed)
    except Exception:  # Le CLI ne publie jamais une traceback contenant la configuration ou le script distant.
        print(json.dumps({'status':'INCOMPLETE','phase':phase,'native_cdc_qualified':False,
                          'reason':'private_configuration_or_native_evidence_unavailable'}),file=stdout)
        return 1


def add_parser(subparsers):
    group=subparsers.add_parser('qualification',help='Qualifier une installation native existante')
    commands=group.add_subparsers(dest='qualification_mode',required=True)
    native=commands.add_parser('native',help='Preuves natives privées, lecture seule par défaut')
    native.add_argument('--config',required=True)
    native.add_argument('--out-dir',required=True)
    native.add_argument('--phase',choices=PHASES,default='baseline')
