"""Collecte native bornée en lecture seule, depuis les conteneurs natifs.

Oracle HISTORY indépendant de Snowflake : instantané publié lié au Job,
plus relecture DISPLAY_JOURNAL complète jusqu'au checkpoint du lecteur.
Aucune commande DML, scale, création de ressource ou lecture API Secret.
"""
from __future__ import annotations
import argparse
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from . import proof as n

SCOPE=('pipeline','table','table_id','namespace','context')
ALLOWED=set(SCOPE)|{'kubeconfig','reader_deployment','loader_deployment','capture_digest','loader_digest','columns','pk',
 'database','schema','history','mirror','journal_library','journal_name','expected_role_arn','identity','copy_run_id','copy_boundary',
 'expected_gcp_service_account','expected_gcp_project','expected_gcs_bucket','gcs_read_key','max_seconds','poll_seconds',
 'expected_gcp_instance_id','expected_gcp_instance_name','expected_gcp_zone','expected_gcp_machine_type',
 'release_oci_root','release_receipt','expected_revision','release_admission','expected_history_mode'}


def load_config(path):
    return config_values(json.loads(Path(path).read_text()))


def config_values(data):
    data=dict(data)
    if not isinstance(data,dict) or set(data)-ALLOWED:
        raise ValueError('configuration inconnue ou contenant des entrées sensibles')
    required={'kubeconfig',*SCOPE,'reader_deployment','loader_deployment','capture_digest','loader_digest','columns','pk',
              'database','schema','history','mirror','journal_library','journal_name','identity','copy_run_id','copy_boundary',
              'release_oci_root','release_receipt','expected_revision','release_admission'}
    mode=data.get('identity')
    if mode not in {'irsa','vm-metadata','gke-workload-identity','gcp-vm-metadata'}:raise ValueError('mode identité inconnu')
    required|=({'expected_gcp_service_account','expected_gcp_project','expected_gcs_bucket','gcs_read_key'}
               if mode in {'gke-workload-identity','gcp-vm-metadata'} else {'expected_role_arn'})
    vm_fields={'expected_gcp_instance_id','expected_gcp_instance_name','expected_gcp_zone','expected_gcp_machine_type'}
    if mode=='gcp-vm-metadata':required|=vm_fields
    elif vm_fields & set(data):raise ValueError('champs instance VM GCP hors mode explicite')
    if any(data.get(k) in (None,'') for k in required):raise ValueError('configuration native incomplète')
    if mode in {'gke-workload-identity','gcp-vm-metadata'}:
        project=data['expected_gcp_project'];gsa=data['expected_gcp_service_account']
        if (not isinstance(project,str) or re.fullmatch(r'[a-z][a-z0-9-]{4,62}',project) is None or
                not isinstance(gsa,str) or re.fullmatch(r'[a-z][a-z0-9-]{0,29}@'+re.escape(project)+r'\.iam\.gserviceaccount\.com',gsa) is None):
            raise ValueError('projet/GSA GCP attendus invalides')
    if mode=='gcp-vm-metadata':
        patterns={'expected_gcp_instance_id':r'[1-9][0-9]{0,20}',
                  'expected_gcp_instance_name':r'[a-z][a-z0-9-]{0,62}',
                  'expected_gcp_zone':r'[a-z][a-z0-9-]{1,61}-[a-z]',
                  'expected_gcp_machine_type':r'[a-z][a-z0-9-]{1,62}'}
        if any(not isinstance(data[k],str) or re.fullmatch(pattern,data[k]) is None for k,pattern in patterns.items()):
            raise ValueError('ID, nom, zone ou SKU VM GCP attendus invalides')
    if any(n.DIGEST.fullmatch(data[k]) is None for k in ('capture_digest','loader_digest')):raise ValueError('digests requis')
    if data.get('expected_history_mode', 'streaming') != 'streaming':raise ValueError('Streaming attendu explicitement')
    data.setdefault('expected_history_mode','streaming')
    data.setdefault('expected_role_arn',None)
    data.setdefault('max_seconds',120);data.setdefault('poll_seconds',1)
    if type(data['max_seconds']) is not int or not 1<=data['max_seconds']<=120:raise ValueError('deadline invalide')
    if type(data['poll_seconds']) not in (int,float) or not 1 <= data['poll_seconds'] <= 10:raise ValueError('poll invalide')
    for key in ('before','after','history_oracle','baseline','receipt'):
        data[key]=None
    return Namespace(**data)


def scope_matches(proof,args):
    if not isinstance(proof,dict) or any(proof.get(k)!=getattr(args,k) for k in SCOPE):
        raise ValueError('preuve hors périmètre')


def history_row(record,columns,library,table):
    if record.get('source_system')!='ibmi' or record.get('library')!=library or record.get('table')!=table:
        raise ValueError('événement source hors table')
    operation=record.get('operation')
    if operation not in {'c','u_before','u_after','d'}:raise ValueError('opération non qualifiable')
    sequence=int(n.normalize(record.get('journal_sequence'),'int'))
    if sequence<0:raise ValueError('séquence invalide')
    receiver=record.get('journal_receiver');journal=record.get('journal')
    if not isinstance(receiver,str) or not receiver or not isinstance(journal,str) or not journal:raise ValueError('position absente')
    event=hashlib.sha256(f'ibmi|{journal}|{receiver}|{sequence}'.encode()).hexdigest()
    if record.get('event_id')!=event:raise ValueError('identité événement divergente')
    before,after=record.get('before'),record.get('after')
    if operation in {'c','u_after'}:
        if before is not None:raise ValueError('image avant inattendue')
        image=after
    else:
        if after is not None:raise ValueError('image après inattendue')
        image=before
    if not isinstance(image,dict) or not set(columns)<=set(image) or set(image)-set(columns)-{'_rrn'}:
        raise ValueError('image complète absente : delete RRN seul non qualifiable')
    return {**{k:n.normalize(image[k],kind) for k,kind in columns.items()},
            'EVENT_ID':event,'OPERATION':operation,'JOURNAL_RECEIVER':receiver,'JOURNAL_SEQUENCE':str(sequence)}


def validate_oracle_parts(copy,journal,positions,checkpoint,args,columns):
    boundary=copy.get('boundary',{})
    if (copy.get('pipeline_id')!=args.pipeline or copy.get('table_id')!=args.table_id or copy.get('run_id')!=args.copy_run_id or
        str(boundary.get('receiver_name'))+':'+str(boundary.get('last_sequence'))!=args.copy_boundary):
        raise ValueError('preuve copie hors tentative/frontière')
    low=boundary.get('last_sequence');receiver=boundary.get('receiver_name')
    high=checkpoint.get('sequence') if isinstance(checkpoint,dict) else None
    if (type(low) is not int or type(high) is not int or low<0 or high<low or high-low>1000 or
            checkpoint.get('receiver')!=receiver):raise ValueError('checkpoint absent, rotation ou fenêtre trop grande')
    snapshot=copy.get('records');rows=copy.get('rows_copied')
    if type(rows) is not int or not 1<=rows<=n.MAX_ROWS or not isinstance(snapshot,list) or len(snapshot)!=rows:
        raise ValueError('instantané complet absent')
    snapshot=sorted(snapshot,key=lambda e:int(n.normalize(e.get('journal_sequence'),'int')))
    if not copy.get('artifacts') or any(a.get('verified') is not True for a in copy['artifacts']):
        raise ValueError('intégrité objets copie absente')
    library,table=args.table.split('.')
    for index,event in enumerate(snapshot,1):
        if (event.get('journal_receiver')!='SNAPSHOT:'+args.copy_run_id or event.get('journal')!='SNAPSHOT:'+args.table or
                event.get('operation')!='c' or event.get('journal_sequence')!=index):raise ValueError('époque/ordinal instantané invalide')
    if journal.get('scan_complete') is not True or journal.get('receiver')!=receiver or journal.get('start')!=low+1 or journal.get('end')!=high:
        raise ValueError('scan indépendant incomplet ou hors frontière')
    events=journal.get('records')
    if not isinstance(events,list) or type(journal.get('decoded')) is not int or journal['decoded']!=len(events):
        raise ValueError('compte événements indépendant incomplet')
    actual=[]
    for event in events:
        sequence=int(n.normalize(event.get('journal_sequence'),'int'))
        if event.get('journal_receiver')!=receiver or not low<sequence<=high or event.get('journal')!=args.journal_name:
            raise ValueError('événement journal hors fenêtre')
        actual.append((receiver,sequence))
    if positions.get('kind')!='ibmi-source-rowpos-reader' or not isinstance(positions.get('positions'),list):
        raise ValueError('oracle ROWPOS absent')
    expected=[]
    for row in positions['positions']:
        seq=int(n.normalize(row.get('SEQUENCE_NUMBER'),'int'))
        if row.get('JOURNAL_RECEIVER_NAME')!=receiver:raise ValueError('rotation hors périmètre')
        if low<seq<=high:expected.append((receiver,seq))
    if len(set(expected))!=len(expected) or len(set(actual))!=len(actual) or set(expected)!=set(actual):
        raise ValueError('couverture ROWPOS/image incomplète ou dupliquée')
    history=[history_row(e,columns,library,table) for e in snapshot+events]
    if len(history)>n.MAX_ROWS:raise ValueError('budget total HISTORY dépassé')
    n.history_proof(history,history)
    return {'kind':'ibmi-journal-and-snapshot-oracle',**{k:getattr(args,k) for k in SCOPE},
            'copy_run_id':args.copy_run_id,'copy_boundary':args.copy_boundary,'events':history,
            'checkpoint':checkpoint,'copy_artifacts':copy['artifacts'],'source_journal_positions':positions,
            'journal_scan':{k:journal[k] for k in ('receiver','start','end','decoded','scan_complete')},
            'observed_utc':n.utc(),'independent_of_snowflake':True,'native_cdc_qualified':False}


def bind_mutation_receipts(acks,oracle,pause,resume,before,args):
    # Le collecteur lie les ACK reçus à de vraies images source ; il ne
    # fabrique ni ACK, ni nombre de lignes affectées, ni positions journal.
    for item in (oracle,pause,resume,before):scope_matches(item,args)
    if (oracle.get('kind')!='ibmi-journal-and-snapshot-oracle' or oracle.get('independent_of_snowflake') is not True or
            oracle.get('copy_run_id')!=args.copy_run_id or oracle.get('copy_boundary')!=args.copy_boundary or
            before.get('snapshot_checks_passed') is not True or pause.get('kind')!='native-reader-pause-proof' or
            pause.get('reader',{}).get('deployment_absent') is not True or pause['reader'].get('owned_pods')!=[] or
            resume.get('kind')!='native-reader-resume-proof' or resume.get('reader',{}).get('ready_replicas')!=1):
        raise ValueError('contexte oracle/pause/reprise incomplet')
    return _bind_mutations(acks,oracle,before,args,pause['observed_utc'],resume['observed_utc'])


def bind_crash_mutation_receipts(acks,oracle,crash,before,args):
    for item in (oracle,crash,before):scope_matches(item,args)
    if (oracle.get('kind')!='ibmi-journal-and-snapshot-oracle' or oracle.get('independent_of_snowflake') is not True or
            oracle.get('copy_run_id')!=args.copy_run_id or oracle.get('copy_boundary')!=args.copy_boundary or
            before.get('snapshot_checks_passed') is not True):
        raise ValueError('oracle crash indépendant incomplet')
    lower,upper=n.validate_crash_replacement(before,{**before,'reader':crash['reader'],'observed_utc':crash['observed_utc']},crash)
    return _bind_mutations(acks,oracle,before,args,lower.isoformat(),upper.isoformat())


def _bind_mutations(acks,oracle,before,args,lower,upper):
    if not isinstance(acks,list) or not 1<=len(acks)<=20:raise ValueError('ACK mutations absents')
    columns=args.columns if isinstance(args.columns,dict) else json.loads(Path(args.columns).read_text())
    events=oracle['events'];n.history_proof(events,events)
    index={(e['JOURNAL_RECEIVER'],int(e['JOURNAL_SEQUENCE'])):e for e in events}
    used=set();receipts=[]
    for ack in acks:
        scope_matches(ack,args)
        start,end=n.validate_receipt(ack,before['observed_utc'])
        if (not n.stamp(lower)<=start<=end<n.stamp(upper) or
                type(ack.get('affected_rows')) is not int or ack['affected_rows']!=1 or 'expected_events' in ack):
            raise ValueError('ACK réel pendant pause absent ou images déclarées refusées')
        positions=ack.get('source_positions');shape={'insert':['c'],'update':['u_before','u_after'],'delete':['d']}.get(ack.get('operation'))
        mutation=ack.get('mutation_identity')
        if positions is None and shape is not None and isinstance(mutation,dict):
            # Positions réellement relues, jamais déduites d'un simple tail.
            checkpoint=before['checkpoint']
            matches=[e for e in events if e.get('OPERATION')==shape[-1] and e.get('JOURNAL_RECEIVER')==checkpoint['receiver']
                     and checkpoint['sequence']<int(e['JOURNAL_SEQUENCE'])<=oracle['checkpoint']['sequence']
                     and all(k in columns and n.normalize(e.get(k),columns[k])==n.normalize(v,columns[k]) for k,v in mutation.items())]
            if len(matches)!=1:raise ValueError('image source acquittée absente ou ambiguë')
            last=matches[0];sequence=int(last['JOURNAL_SEQUENCE']);receiver=last['JOURNAL_RECEIVER']
            if ack['operation']=='update':
                preceding=index.get((receiver,sequence-1))
                if preceding is None or preceding.get('OPERATION')!='u_before' or n.normalize(preceding.get(args.pk),columns[args.pk])!=n.normalize(mutation.get(args.pk),columns[args.pk]):
                    raise ValueError('paire source avant/après non liée : entrée exacte requise')
                positions=[{'receiver':receiver,'sequence':sequence-1},{'receiver':receiver,'sequence':sequence}]
            else:positions=[{'receiver':receiver,'sequence':sequence}]
        if shape is None or not isinstance(positions,list) or len(positions)!=len(shape):raise ValueError('positions acquittement absentes')
        selected=[]
        for position in positions:
            key=(position['receiver'],int(n.normalize(position['sequence'],'int')))
            checkpoint=before['checkpoint']
            if (key in used or key not in index or key[0]!=checkpoint['receiver'] or key[1]<=checkpoint['sequence'] or
                    key[0]!=oracle['checkpoint']['receiver'] or key[1]>oracle['checkpoint']['sequence']):
                raise ValueError('position mutation hors reprise ou ambiguë')
            used.add(key);selected.append(index[key])
        if [e['OPERATION'] for e in selected]!=shape:raise ValueError('opération ACK différente des images source')
        required={args.pk} if ack['operation']=='delete' else set(columns)
        if (not isinstance(mutation,dict) or set(mutation)!=required or
                any(n.normalize(e.get(args.pk),columns[args.pk])!=n.normalize(mutation[args.pk],columns[args.pk]) for e in selected)):
            raise ValueError('identité mutation acquittée différente des images source')
        if ack['operation']!='delete' and any(n.normalize(selected[-1].get(k),columns[k])!=
                                             n.normalize(mutation[k],columns[k]) for k in required):
            raise ValueError('valeurs acquittées différentes de l’image source indépendante')
        receipts.append({**ack,'source_positions':positions,'expected_events':selected,
                         'expected_events_pending':False,'source_positions_origin':'independent-journal-image+ROWPOS',
                         'source_oracle_observed_utc':oracle['observed_utc']})
    return receipts


class LiveCollector:
    def __init__(self,probe):self.probe=probe;self.args=probe.args
    def copy(self,loader):
        a=self.args;p=self.probe
        # Utilise les mêmes octets immuables que le Job, jamais HISTORY.
        script=f'''import hashlib,json,os,sys
sys.path.insert(0,'/app')
import quadringent_destination_loader as l
from quadringent.storage_backend import StorageBackend
from quadringent.storage_layout import snapshot_prefix
from quadringent.raw import read_raw_batch
tables=[t for t in l.parse_table_set(os.environ['QUADRINGENT_LOADER_TABLE_SET_JSON']) if t.table_id=={a.table_id!r}]
if len(tables)!=1:raise SystemExit(1)
t=tables[0];plan=l.build_plan(t,database={a.database!r},schema={a.schema!r})
if (os.environ['QUADRINGENT_DESTINATION_DATABASE']!={a.database!r} or os.environ['QUADRINGENT_DESTINATION_SCHEMA']!={a.schema!r} or
 t.schema_name.upper()!={p.library!r} or t.table_name.upper()!={p.table!r} or list(t.key_columns)!=[{a.pk!r}] or
 [x['name'] for x in t.columns]!={list(p.columns)!r} or plan.history_table!={a.history!r} or plan.mirror_table!={a.mirror!r}):raise SystemExit(1)
groups={{'int':{{'int','integer','smallint','bigint'}},'decimal':{{'decimal','numeric','decfloat'}},'text':{{'char','varchar','clob'}},'date':{{'date'}},'timestamp':{{'timestamp'}}}}
if any(str(x['kind']).lower() not in groups[{p.columns!r}[x['name']]] for x in t.columns):raise SystemExit(1)
storage=StorageBackend.from_environment(os.environ);root=storage.object_store('')
raw=root.get_bounded(t.evidence_key,262144);evidence=json.loads(raw)
if evidence.get('pipeline_id')!={a.pipeline!r} or evidence.get('table_id')!={a.table_id!r} or evidence.get('run_id')!={a.copy_run_id!r}:raise SystemExit(1)
batches=evidence.get('snapshot_batches')
if not isinstance(batches,list) or not 1<=len(batches)<=100:raise SystemExit(1)
store=storage.object_store(snapshot_prefix(os.environ.get('AS400_RAW_PREFIX',''),t.table_name))
records=[];artifacts=[];keys=set()
for ref in batches:
 if set(ref)!={{'payload_key','manifest_key'}}:raise SystemExit(1)
 for key in ref.values():
  if not isinstance(key,str) or '/' in key or not key.startswith('batch-') or key in keys:raise SystemExit(1)
  keys.add(key)
 payload=store.get_bounded(ref['payload_key'],2000000);manifest=store.get_bounded(ref['manifest_key'],262144)
 batch=read_raw_batch(manifest,payload,preserve_decimals=True)
 if batch.manifest.high_watermark.receiver!='SNAPSHOT:'+{a.copy_run_id!r} or batch.manifest.high_watermark.sequence!=max(e.position.sequence for e in batch.events):raise SystemExit(1)
 records.extend(e.to_record() for e in batch.events)
 if len(records)>1000:raise SystemExit(1)
 artifacts.append({{**ref,'verified':True,'payload_sha256':hashlib.sha256(payload).hexdigest(),'manifest_sha256':hashlib.sha256(manifest).hexdigest(),'events':len(batch.events)}})
evidence.update(records=records,artifacts=[{{'evidence_key':t.evidence_key,'verified':True,'sha256':hashlib.sha256(raw).hexdigest()}},*artifacts])
print(json.dumps(evidence,default=str))
'''
        return p.exec_json(loader[0]['pod'],loader[0]['container'],script)

    def journal(self,reader,copy,checkpoint):
        a=self.args;p=self.probe;b=copy['boundary'];low=b['last_sequence'];high=checkpoint['sequence']
        if checkpoint['receiver']!=b['receiver_name'] or not 0<=high-low<=1000:raise ValueError('rotation ou budget fenêtre dépassé')
        if high==low:return {'receiver':b['receiver_name'],'start':low+1,'end':high,'decoded':0,'scan_complete':True,'records':[]}
        request={'cmd':'sql_window','receiver':b['receiver_name'],'receiver_library':b['receiver_library'],'start_sequence':str(low+1),
                 'end_sequence':str(high),'max_server_entries':high-low,'max_decoded_entries':1000,'high_watermark_sequence':str(high)}
        command=['java','-cp','/app/probe.jar:/app/lib/*','io.quadringent.as400.PersistentJournalWorker']
        script=f'''import json,os,re,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,'/app')
from quadringent.raw import read_raw_batch
bootstrap=json.loads(os.environ['AS400_TABLE_BOOTSTRAP_JSON'])
if os.environ['ISERIES_SCHEMA'].upper()!={p.library!r} or not any(t['table_id']=={a.table_id!r} and t['schema'].upper()=={p.library!r} and t['table'].upper()=={p.table!r} for t in bootstrap):raise SystemExit(1)
env=dict(os.environ);env.update(ISERIES_SCHEMA={p.library!r},ISERIES_TABLE={p.table!r},ISERIES_TABLES={p.table!r},AS400_SQL_QUERY_TIMEOUT_SECONDS='10',AS400_SOCKET_TIMEOUT_MS='15000',AS400_RETRIEVE_TIMEOUT_MS='15000')
with tempfile.TemporaryDirectory(prefix='quadringent-readonly-oracle-') as directory:
 request={request!r};request['raw_directory']=directory
 run=subprocess.run({command!r},input=json.dumps(request)+'\\n'+json.dumps({{'cmd':'shutdown'}})+'\\n',env=env,text=True,capture_output=True,timeout=25)
 lines=run.stdout.splitlines()
 summaries=[x for x in lines if x.startswith('summary ')]
 if run.returncode or lines.count('worker_ready')!=1 or lines.count('window_done')!=1 or len(summaries)!=1 or any(x.startswith(('window_error=','connect_error=')) for x in lines):raise SystemExit(1)
 summary=dict(re.findall(r'(\\w+)=([^ ]+)',summaries[0]))
 if summary.get('scan_complete')!='true':raise SystemExit(1)
 records=[];hashes=[]
 for manifest in sorted(Path(directory).glob('batch-*.manifest.json')):
  payload=manifest.with_name(manifest.name.replace('.manifest.json','.jsonl'))
  batch=read_raw_batch(manifest.read_bytes(),payload.read_bytes(),preserve_decimals=True)
  if batch.manifest.high_watermark.receiver!=request['receiver'] or batch.manifest.high_watermark.sequence!=int(request['end_sequence']):raise SystemExit(1)
  records.extend(e.to_record() for e in batch.events);hashes.append(batch.manifest.payload_sha256)
 if int(summary['decoded'])!=len(records) or len(records)>1000:raise SystemExit(1)
 print(json.dumps({{'receiver':request['receiver'],'start':int(request['start_sequence']),'end':int(request['end_sequence']),
 'decoded':len(records),'scan_complete':True,'records':records,'payload_sha256':hashes}},default=str))
'''
        return p.exec_json(reader[0]['pod'],reader[0]['container'],script)

    def oracle(self):
        p=self.probe;p.deadline=min(getattr(p,"deadline",float("inf")),time.monotonic()+self.args.max_seconds)
        reader=p.workload(self.args.reader_deployment,self.args.capture_digest)
        loader=p.workload(self.args.loader_deployment,self.args.loader_digest)
        copy=self.copy(loader)
        checkpoint=p.checkpoint(reader,loader)
        if not checkpoint:raise ValueError('checkpoint absent')
        positions=p.source_positions(reader,{**{k:getattr(self.args,k) for k in SCOPE},
                                            'checkpoint':{'receiver':copy['boundary']['receiver_name'],'sequence':copy['boundary']['last_sequence']}})
        journal=self.journal(reader,copy,checkpoint)
        after=p.checkpoint(reader,loader)
        if after!=checkpoint:raise ValueError('checkpoint a bougé pendant oracle, recommencer quand source au repos')
        report=validate_oracle_parts(copy,journal,positions,checkpoint,self.args,p.columns)
        report.update(reader=reader[0],loader=loader[0])
        if time.monotonic()>p.deadline:raise RuntimeError('native_probe_deadline_exceeded')
        return report
