"""Sondes natives en lecture seule sur les workloads natifs du produit.

Ne crée aucun Job, ne change aucun Deployment, ne lit aucun Secret via l'API.
Les connexions sont ouvertes dans les pods avec leur identité existante.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_ROWS = 1000


def check_output(path, inputs):
    target = Path(path).resolve()
    if any(target == Path(p).resolve() for p in inputs if p):
        raise ValueError("sortie identique à une entrée")
    if Path(path).exists() or Path(path).is_symlink():
        raise ValueError("sortie préexistante refusée")


def validate_receipt(receipt, baseline_time):
    start, ack = stamp(receipt["write_started_utc"]), stamp(receipt["write_ack_utc"])
    if receipt.get("acknowledged") is not True or not stamp(baseline_time) <= start <= ack <= stamp(utc()):
        raise ValueError("reçu temporel ou acquittement invalide")
    return start, ack


def latencies(start, ack, visible):
    start, ack, visible = (stamp(v) for v in (start, ack, visible))
    if not start <= ack <= visible:
        raise ValueError("latence négative ou ordre temporel invalide")
    return {"from_start_s": (visible-start).total_seconds(), "from_ack_s": (visible-ack).total_seconds()}


def history_proof(actual, expected):
    if not isinstance(expected,list) or not expected or not actual:
        raise ValueError("oracle historique complet absent ou historique vide")
    def indexed(rows):
        out={};positions=set()
        for row in rows:
            event=row.get("EVENT_ID")
            if (not isinstance(event,str) or not event or event in out or
                    row.get("OPERATION") not in {"c","u_before","u_after","d"} or
                    not isinstance(row.get("JOURNAL_RECEIVER"),str) or not row["JOURNAL_RECEIVER"]):
                raise ValueError("historique dupliqué ou métadonnées invalides")
            sequence=normalize(row.get("JOURNAL_SEQUENCE"),"int")
            if sequence is None or int(sequence)<0:
                raise ValueError("position historique invalide")
            position=(row['JOURNAL_RECEIVER'],sequence)
            if position in positions: raise ValueError('position historique dupliquée')
            positions.add(position)
            clean={k:str(v) if v is not None else None for k,v in row.items()}
            clean["JOURNAL_SEQUENCE"]=sequence
            out[event]=clean
        return out
    if indexed(actual) != indexed(expected):
        raise ValueError("couverture, contenu ou positions historiques divergents")
    return {"equal":True,"events":len(actual),"oracle_required":True}


def identity_proof(actual, mode, expected, *, expected_gcp_service_account=None,
                   expected_gcp_project=None, expected_gcs_bucket=None, expected_gcs_key=None,
                   expected_gcp_instance_id=None, expected_gcp_instance_name=None,
                   expected_gcp_zone=None, expected_gcp_machine_type=None):
    if not isinstance(actual,dict):
        raise ValueError('payload identité effectif invalide')
    if mode in {'gke-workload-identity','gcp-vm-metadata'}:
        read=actual.get('gcs_read',{})
        if (not isinstance(read,dict) or not isinstance(expected_gcp_project,str) or not expected_gcp_project or
                not isinstance(expected_gcp_service_account,str) or not expected_gcp_service_account or
                not expected_gcp_service_account.endswith('@'+expected_gcp_project+'.iam.gserviceaccount.com') or
                actual.get('method')!='google-compute-metadata' or
                actual.get('credential_type')!='google.auth.compute_engine.credentials.Credentials' or
                actual.get('refreshed') is not True or actual.get('project')!=expected_gcp_project or
                actual.get('service_account_email')!=expected_gcp_service_account or
                not expected_gcs_bucket or not expected_gcs_key or read.get('verified') is not True or
                read.get('bucket')!=expected_gcs_bucket or read.get('key')!=expected_gcs_key or
                type(read.get('bytes')) is not int or read['bytes']<=0 or
                re.fullmatch('[0-9a-f]{64}',read.get('sha256','')) is None):
            raise ValueError('identité metadata GKE ou lecture GCS effective non prouvée')
        if mode=='gcp-vm-metadata':
            instance=actual.get('instance')
            allowed={'method','credential_type','refreshed','project','service_account_email','gcs_read','instance','verified'}
            instance_allowed={'id','name','zone','machine_type','service_account_email','scopes'}
            expected_fields={'id':expected_gcp_instance_id,'name':expected_gcp_instance_name,
                             'zone':expected_gcp_zone,'machine_type':expected_gcp_machine_type,
                             'service_account_email':expected_gcp_service_account}
            if (set(actual)-allowed or not isinstance(instance,dict) or set(instance)!=instance_allowed or
                    any(not isinstance(v,str) or not v or instance.get(k)!=v for k,v in expected_fields.items()) or
                    re.fullmatch(r'[1-9][0-9]{0,20}',expected_gcp_instance_id or '') is None or
                    re.fullmatch(r'[a-z][a-z0-9-]{0,62}',expected_gcp_instance_name or '') is None or
                    re.fullmatch(r'[a-z][a-z0-9-]{1,61}-[a-z]',expected_gcp_zone or '') is None or
                    re.fullmatch(r'[a-z][a-z0-9-]{1,62}',expected_gcp_machine_type or '') is None or
                    not isinstance(instance['scopes'],list) or
                    instance['scopes']!=['https://www.googleapis.com/auth/cloud-platform'] or
                    set(read)-{'verified','bucket','key','bytes','sha256','generation'} or
                    re.fullmatch(r'[1-9][0-9]*',read.get('generation','')) is None):
                raise ValueError('instance, scope metadata VM GCP ou payload privé non prouvés')
        return {**actual,'verified':True}
    if mode not in {'irsa','vm-metadata'}:
        raise ValueError('mode identité inconnu')
    role=re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/(.+)", expected or "")
    provider="iam-role" if mode=="vm-metadata" else "assume-role-with-web-identity"
    if (role is None or actual.get("method")!=provider or
            not actual.get("arn","").startswith(f"arn:aws:sts::{role[1]}:assumed-role/{role[2].split('/')[-1]}/")):
        raise ValueError("identité SDK effective non prouvée")
    return {**actual,"verified":True}


def observed_image_digest(image_id):
    match=re.search(r'sha256:[0-9a-f]{64}\Z',image_id or '')
    if match is None:
        raise ValueError('digest exécuté absent ou invalide')
    return match[0]


def image_binding(mapping,expected_index,image_id,architecture):
    if mapping.get('index')!=expected_index or architecture not in {'amd64','arm64'}:
        raise ValueError('index OCI ou architecture différents du candidat scanné')
    variant=mapping.get('variants',{}).get(architecture)
    if (not isinstance(variant,dict) or not mapping.get('revision') or
            re.fullmatch('[0-9a-f]{64}',mapping.get('receipt_sha256','')) is None or
            any(DIGEST.fullmatch(v or '') is None for v in (expected_index,variant.get('manifest'),variant.get('config')))):
        raise ValueError('liaison OCI scellée incomplète')
    observed=observed_image_digest(image_id)
    if observed not in {expected_index,variant['manifest'],variant['config']}:
        raise ValueError('digest exécuté non lié à la variante OCI du nœud')
    return {**variant,'index':expected_index,'architecture':architecture,'observed_digest':observed,
            'revision':mapping['revision'],'receipt_sha256':mapping['receipt_sha256'],'verified':True,
            **{k:mapping[k] for k in ('admission_sha256','admitted_utc') if k in mapping}}


def release_bindings(root, receipt, revision, admission_path):
    """Compare les 21 originaux au reçu admis ; ne revalide pas les couches localement."""
    from . import release_admission as release
    root, receipt, admission_path = Path(root), Path(receipt), Path(admission_path)
    if receipt.name != 'passed.json' or receipt.parent.resolve() != root.resolve():
        raise ValueError('reçu original du layout exact requis')
    raw = release.read_regular(receipt, release.MAX_RECEIPT_BYTES)
    admission_raw = release.read_regular(admission_path, release.MAX_RECEIPT_BYTES)
    admission = release.document(admission_raw)
    ci = admission.get('authenticated_ci', {})
    if (admission.get('kind') != 'quadringent-oci-runtime-admission' or
            admission.get('complete_seal_verified') is not True or
            admission.get('seal_verification_location') != 'github-actions' or
            admission.get('local_verification_scope') != 'indexes_manifests_configs_only' or
            admission.get('local_full_fingerprint_verified') is not False or
            admission.get('metadata_files_compared') != 21 or
            admission.get('passed_sha256') != hashlib.sha256(raw).hexdigest() or
            admission.get('revision') != revision or
            not set(release.REQUIRED_STEPS).issubset(ci.get('required_steps_success', [])) or
            ci.get('canonical_ci', {}).get('source_sha') != revision or
            set(ci.get('canonical_ci', {}).get('seven_jobs_success', [])) != set(release.CI_JOBS) or
            stamp(admission['admitted_utc']) > stamp(utc())):
        raise ValueError('admission complète et authentifiée absente')
    expected = release.Expected(ci['run_id'], ci['run_attempt'], revision, admission['tag'],
                                ci['workflow_sha256'], ci['repository'], admission.get('expected_private', True))
    indexes, count = release.metadata_bindings(expected, root, release.document(raw)['files'])
    if indexes != admission['indexes'] or count != 21:
        raise ValueError('index différent de la preuve admise')
    mappings = {}
    for component in release.COMPONENTS:
        def blob(digest):
            return release.document(release.read_regular(root/component/'blobs/sha256'/digest[7:], release.MAX_METADATA_BYTES))
        index = blob(indexes[component]); variants = {}
        for item in index['manifests']:
            manifest = blob(item['digest'])
            variants[item['platform']['architecture']] = {'manifest':item['digest'], 'config':manifest['config']['digest']}
        mappings[component] = {'index':indexes[component], 'variants':variants, 'revision':revision,
                              'receipt_sha256':hashlib.sha256(raw).hexdigest(),
                              'admission_sha256':hashlib.sha256(admission_raw).hexdigest()}
    if release.read_regular(receipt, release.MAX_RECEIPT_BYTES) != raw or release.read_regular(admission_path, release.MAX_RECEIPT_BYTES) != admission_raw:
        raise ValueError('admission modifiée pendant lecture')
    return mappings


def workload_candidate(proof,container):
    """Identité stable du conteneur produit, sans sidecars ni session STS."""
    images=[item for item in proof.get('images',[]) if item.get('name')==container]
    if proof.get('container')!=container or len(images)!=1:
        raise ValueError('conteneur produit ambigu ou absent')
    digest=observed_image_digest(images[0].get('image_id'))
    binding=proof.get('image_binding')
    if binding is not None:
        if (binding.get('verified') is not True or binding.get('observed_digest')!=digest or
                digest not in {binding.get('index'),binding.get('manifest'),binding.get('config')} or
                binding.get('architecture') not in {'amd64','arm64'}):
            raise ValueError('liaison image exécutée invalide')
        digest=binding['index']
        # L'architecture et les octets du sceau font partie du candidat stable.
        digest=(digest,binding['architecture'],binding['manifest'],binding['config'],binding['revision'],binding['receipt_sha256'],binding.get('admission_sha256'))
    effective=proof.get('identity_effective',{})
    mode=proof.get('identity_mode')
    if mode in {'gke-workload-identity','gcp-vm-metadata'}:
        read=effective.get('gcs_read',{})
        instance=effective.get('instance',{})
        identity_proof(effective,mode,None,expected_gcp_service_account=effective.get('service_account_email'),
                       expected_gcp_project=effective.get('project'),expected_gcs_bucket=read.get('bucket'),
                       expected_gcs_key=read.get('key'),expected_gcp_instance_id=instance.get('id'),
                       expected_gcp_instance_name=instance.get('name'),expected_gcp_zone=instance.get('zone'),
                       expected_gcp_machine_type=instance.get('machine_type'))
        if effective.get('verified') is not True or not proof.get('service_account'):
            raise ValueError('contexte identité native incomplet')
        stable=(digest,mode,proof['service_account'],'google-compute-metadata',effective['service_account_email'],effective['project'],read['bucket'],read['key'])
        if mode=='gcp-vm-metadata':
            if proof.get('node')!=instance['name']:
                raise ValueError('nœud Kubernetes différent de la VM GCP attestée')
            stable+=(instance['id'],instance['name'],instance['zone'],instance['machine_type'],tuple(instance['scopes']))
        return stable
    provider={'irsa':'assume-role-with-web-identity','vm-metadata':'iam-role'}.get(mode)
    role=re.fullmatch(r'(arn:aws:sts::[0-9]{12}:assumed-role/[^/]+)/[^/]+',effective.get('arn',''))
    if (effective.get('verified') is not True or provider is None or effective.get('method')!=provider
            or role is None or not isinstance(proof.get('service_account'),str) or not proof['service_account']):
        raise ValueError('contexte identité native incomplet')
    return digest,mode,proof['service_account'],provider,role[1]


def reader_reference(proof):
    name,selector,uid=proof.get('deployment'),proof.get('selector'),proof.get('uid')
    if (not isinstance(name,str) or not name or not isinstance(uid,str) or not uid
            or not isinstance(selector,dict) or not selector
            or any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in selector.items())):
        raise ValueError('référence lecteur native incomplète')
    return name,selector,uid


def evaluate_resume(before,after,order,operations):
    if not all(item.get("snapshot_checks_passed") is True and item.get("checkpoint")
               and item.get("history_validation",{}).get("equal") is True
               and item.get("reconciliation",{}).get("equal") is True
               and item.get("duplicate_event_ids") == 0
               and item.get('reader',{}).get('identity_effective',{}).get('verified') is True for item in (before,after)):
        raise ValueError("contrôles avant/après incomplets")
    if before['checkpoint']['receiver']!=after['checkpoint']['receiver']:
        raise ValueError('rotation hors périmètre')
    resume=resume_evidence(before["checkpoint"],after["checkpoint"],None)
    if resume["status"] != "PASS" or not resume["changed"]:
        raise ValueError("progression de reprise non prouvée")
    if not operations or not all(operations.get(k) for k in ('pause','resume','mutations')):
        raise ValueError('preuves pause/reprise/mutations manquantes')
    pause,ready=operations['pause'],operations['resume']
    mutations=operations['mutations']
    if not isinstance(mutations,list) or not 1<=len(mutations)<=20:
        raise ValueError('budget mutations invalide')
    for item in (after,pause,ready,*mutations):
        if any(item.get(k)!=before.get(k) or before.get(k) is None for k in ('pipeline','table','table_id','namespace','context')):
            raise ValueError('preuve hors périmètre')
    if (pause.get('kind')!='native-reader-pause-proof' or ready.get('kind')!='native-reader-resume-proof' or
            pause['reader'].get('deployment_absent') is not True or
            any(k in pause['reader'] for k in ('uid','pod_uid','desired_replicas','ready_replicas')) or
            pause['reader'].get('owned_pods')!=[] or ready['reader'].get('desired_replicas')!=1 or
            ready['reader'].get('ready_replicas')!=1 or ready['reader'].get('identity_effective',{}).get('verified') is not True):
        raise ValueError('pause ou reprise native non prouvée')
    name,selector,uid=reader_reference(before['reader'])
    after_name,after_selector,after_uid=reader_reference(after['reader'])
    ready_name,ready_selector,ready_uid=reader_reference(ready['reader'])
    if (pause['reader'].get('deployment')!=name or pause['reader'].get('selector')!=selector or
            (after_name,after_selector)!=(name,selector) or (ready_name,ready_selector)!=(name,selector) or
            after_uid!=ready_uid or after_uid==uid or
            ready['reader'].get('pod_uid')!=after['reader'].get('pod_uid') or
            not after['reader'].get('pod_uid') or not before['reader'].get('pod_uid') or
            after['reader'].get('pod_uid')==before['reader'].get('pod_uid')):
        raise ValueError('UID lecteur ou pod de reprise invalide')
    candidate=workload_candidate(before['reader'],'reader')
    if any(workload_candidate(item['reader'],'reader')!=candidate for item in (ready,after)):
        raise ValueError('candidat lecteur ou identité changés pendant reprise')
    if workload_candidate(before['loader'],'destination-loader')!=workload_candidate(after['loader'],'destination-loader'):
        raise ValueError('candidat chargeur ou identité changés pendant reprise')
    paused_at,resumed_at=stamp(pause['observed_utc']),stamp(ready['observed_utc'])
    if not stamp(before['observed_utc'])<=paused_at<resumed_at<=stamp(after['observed_utc']):
        raise ValueError('chronologie reprise incohérente')
    proof=after.get('source_journal_positions')
    if not proof or proof.get('kind')!='ibmi-source-rowpos-reader':
        raise ValueError('oracle indépendant positions source absent')
    receiver=before['checkpoint']['receiver'];low=before['checkpoint']['sequence'];high=after['checkpoint']['sequence']
    expected={};positions=set()
    for receipt in mutations:
        start,ack=stamp(receipt['write_started_utc']),stamp(receipt['write_ack_utc'])
        if (receipt.get('acknowledged') is not True or not paused_at<=start<=ack<resumed_at or
                type(receipt.get('affected_rows')) is not int or receipt['affected_rows']!=1):
            raise ValueError('mutation non acquittée pendant arrêt')
        events=receipt.get('expected_events')
        if not isinstance(events,list) or not 1<=len(events)<=2:
            raise ValueError('événements attendus mutation absents')
        shape={'insert':['c'],'update':['u_before','u_after'],'delete':['d']}.get(receipt.get('operation'))
        if shape is None or [event.get('OPERATION') for event in events]!=shape:
            raise ValueError('opération acquittée différente des images attendues')
        for event in events:
            history_proof([event],[event])
            position=(event['JOURNAL_RECEIVER'],int(normalize(event['JOURNAL_SEQUENCE'],'int')))
            if position[0]!=receiver or not low<position[1]<=high or position in positions or event['EVENT_ID'] in expected:
                raise ValueError('checkpoint ne couvre pas les mutations ou position ambiguë')
            positions.add(position);expected[event['EVENT_ID']]=event
    actual=[row for row in after['history'] if row['EVENT_ID'] in expected]
    history_proof(actual,list(expected.values()))
    if any(row.get('JOURNAL_RECEIVER_NAME')!=receiver for row in proof.get('positions',[])):
        raise ValueError('rotation observée par oracle source, hors périmètre')
    source_positions=[(row['JOURNAL_RECEIVER_NAME'],int(normalize(row['SEQUENCE_NUMBER'],'int')))
                      for row in proof.get('positions',[]) if int(normalize(row['SEQUENCE_NUMBER'],'int'))>low]
    if len(set(source_positions))!=len(source_positions) or set(source_positions)!=positions:
        raise ValueError('couverture des positions source divergente')
    before_ids={row['EVENT_ID'] for row in before['history']}
    if before_ids.intersection(expected): raise ValueError('mutation déjà présente avant pause')
    retained=[row for row in after['history'] if row['EVENT_ID'] in before_ids]
    history_proof(retained,before['history'])
    return {'status':'PASS','resume_qualified':True,'rotation_qualified':False,'native_cdc_qualified':False,
            'scope':'same_receiver_pause_mutations_resume','receiver':receiver,'checkpoint_before':low,'checkpoint_after':high,
            'mutations':len(mutations),'events':len(expected),'history_exact':True,'source_mirror_exact':True}


def utc():
    return datetime.now(timezone.utc).isoformat()


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("horodatage sans fuseau")
    return result


def identifier(value):
    if not ID.fullmatch(value):
        raise ValueError("identifiant SQL invalide")
    return value


def write_private(path, data):
    path = Path(path)
    if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError("sortie symlink refusée")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write("\n")


def normalize(value, kind):
    if value is None:
        return None
    if kind == "decimal":
        number=Decimal(str(value))
        if not number.is_finite():raise ValueError('décimal non fini')
        # Decimal.normalize() arrondit au contexte par défaut (28 chiffres),
        # trop court pour NUMBER(38). Formater puis retirer seulement les
        # zéros fractionnaires conserve tous les chiffres du contrat.
        result=format(number,'f')
        if '.' in result:result=result.rstrip('0').rstrip('.')
        return '0' if result=='-0' else result
    if kind == "int":
        number = Decimal(str(value))
        if number != number.to_integral_value():
            raise ValueError("entier non exact")
        return str(int(number))
    if kind in {"date", "timestamp"}:
        return str(value).replace("T", " ") if kind == "timestamp" else str(value)
    if kind == "text":
        return str(value)  # Préserve CHAR, espaces, NULL et chaîne vide.
    raise ValueError("type de colonne non pris en charge")


def compare_rows(source, mirror, columns, pk):
    def index(rows):
        result = {}
        for row in rows:
            if set(row) != set(columns):
                raise ValueError("colonnes relues inattendues")
            clean = {key: normalize(row[key], kind) for key, kind in columns.items()}
            key = clean[pk]
            if key is None:
                raise ValueError("clé primaire NULL")
            if key in result:
                raise ValueError("clé primaire dupliquée")
            result[key] = clean
        return result
    left, right = index(source), index(mirror)
    differences = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            differences.append({"key": key, "source": left.get(key), "mirror": right.get(key)})
    return {"equal": not differences, "source_count": len(left), "mirror_count": len(right),
            "differences": differences}


def normalize_history(rows,columns):
    # Normalisation selon les types du contrat vérifié : Decimal exact,
    # jamais float, sans trim des CHAR ni assimilation NULL/chaîne vide.
    result=[]
    for row in rows:
        if not set(columns)<=set(row):raise ValueError('colonnes historique absentes')
        result.append({**row,**{key:normalize(row[key],kind) for key,kind in columns.items()}})
    return result


def resume_evidence(before, after, receiver_order=None):
    if not before or not after:
        return {"status": "unmeasured", "reason": "checkpoint_missing"}
    if before["receiver"] == after["receiver"]:
        forward = after["sequence"] >= before["sequence"]
    elif receiver_order and before["receiver"] in receiver_order and after["receiver"] in receiver_order:
        forward = receiver_order.index(after["receiver"]) > receiver_order.index(before["receiver"])
    else:
        return {"status": "unmeasured", "reason": "receiver_order_missing"}
    return {"status": "PASS" if forward else "FAIL", "changed": before != after}


class NativeProbe:
    def __init__(self, args):
        self.args = args
        self.columns = json.loads(Path(args.columns).read_text())
        for name, kind in self.columns.items():
            identifier(name)
            normalize(None, kind)
        if args.pk not in self.columns or any(kind not in {"int", "decimal", "text", "date", "timestamp"}
                                               for kind in self.columns.values()):
            raise ValueError("contrat de colonnes invalide")
        self.library, self.table = (identifier(v) for v in args.table.split("."))
        for value in (args.database, args.schema, args.history, args.mirror, args.pk,
                      args.journal_library, args.journal_name):
            identifier(value)
        self.release_images=None
        if getattr(args,'release_oci_root',None):
            self.release_images=release_bindings(args.release_oci_root,args.release_receipt,args.expected_revision,args.release_admission)
            if (args.capture_digest!=self.release_images['capture']['index'] or
                    args.loader_digest!=self.release_images['control-plane']['index'] or args.reader_deployment==args.loader_deployment):
                raise ValueError('index capture/chargeur différent du candidat scellé')

    def kubectl(self, *command, stdin=None):
        remaining = getattr(self,"deadline",None)
        remaining = min(35,remaining-time.monotonic()) if remaining is not None else 35
        if remaining <= 0: raise RuntimeError("native_probe_deadline_exceeded")
        run = subprocess.run(["kubectl", "--kubeconfig", self.args.kubeconfig,
                              "--context", self.args.context, "-n", self.args.namespace,
                              *command], input=stdin, capture_output=True, text=True,
                             timeout=remaining, check=False)
        if getattr(self,"deadline",float("inf")) < time.monotonic():
            raise RuntimeError("native_probe_deadline_exceeded")
        if run.returncode:
            raise RuntimeError("native_kubernetes_probe_failed")
        if len(run.stdout.encode()) > 4_000_000:
            raise RuntimeError("native_probe_output_budget_exceeded")
        return json.loads(run.stdout)

    def exec_json(self, pod, container, script):
        return self.kubectl("exec", "-i", pod, "-c", container, "--", "python", "-P", "-", stdin=script)

    def verify_identity(self, identity):
        return identity_proof(identity,self.args.identity,getattr(self.args,'expected_role_arn',None),
                              expected_gcp_service_account=getattr(self.args,'expected_gcp_service_account',None),
                              expected_gcp_project=getattr(self.args,'expected_gcp_project',None),
                              expected_gcs_bucket=getattr(self.args,'expected_gcs_bucket',None),
                              expected_gcs_key=getattr(self.args,'gcs_read_key',None),
                              expected_gcp_instance_id=getattr(self.args,'expected_gcp_instance_id',None),
                              expected_gcp_instance_name=getattr(self.args,'expected_gcp_instance_name',None),
                              expected_gcp_zone=getattr(self.args,'expected_gcp_zone',None),
                              expected_gcp_machine_type=getattr(self.args,'expected_gcp_machine_type',None))

    def gcp_vm_identity_script(self):
        return self.gke_identity_script(vm=True)

    def gke_identity_script(self,vm=False):
        # Aucun jeton n'est renvoyé. Le GET est borné à la preuve de copie
        # exacte du run, avec les mêmes credentials metadata que le produit.
        instance_checks=''
        instance_output=''
        if vm:
            instance_checks=f'''info=_metadata.get_service_account_info(request,service_account='default')
if not isinstance(info,dict) or info.get('email')!={self.args.expected_gcp_service_account!r}: raise SystemExit(1)
instance={{'id':str(_metadata.get(request,'instance/id')),
 'name':_metadata.get(request,'instance/name'),
 'zone':_metadata.get(request,'instance/zone').rsplit('/',1)[-1],
 'machine_type':_metadata.get(request,'instance/machine-type').rsplit('/',1)[-1],
 'service_account_email':info['email'],'scopes':info.get('scopes')}}
if (instance['id']!={self.args.expected_gcp_instance_id!r} or instance['name']!={self.args.expected_gcp_instance_name!r} or
 instance['zone']!={self.args.expected_gcp_zone!r} or instance['machine_type']!={self.args.expected_gcp_machine_type!r} or
 instance['scopes']!=['https://www.googleapis.com/auth/cloud-platform']): raise SystemExit(1)
'''
            instance_output=",'instance':instance"
        return f'''import hashlib,json,os,sys
sys.path.insert(0,'/app')
import google.auth
from google.auth.compute_engine import credentials as metadata_credentials, _metadata
from google.auth.transport.requests import Request
from google.cloud import storage
class BoundedRequest(Request):
 def __call__(self,*args,**kwargs):
  kwargs['timeout']=10
  return super().__call__(*args,**kwargs)
if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or os.environ.get('GOOGLE_API_KEY'): raise SystemExit(1)
if os.environ.get('QUADRINGENT_STORAGE_BACKEND')!='gcs' or os.environ.get('AS400_RAW_BUCKET')!={self.args.expected_gcs_bucket!r}: raise SystemExit(1)
credentials,project=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
if type(credentials) is not metadata_credentials.Credentials: raise SystemExit(1)
request=BoundedRequest();credentials.refresh(request)
project=_metadata.get_project_id(request)
if project!={self.args.expected_gcp_project!r} or credentials.service_account_email!={self.args.expected_gcp_service_account!r}: raise SystemExit(1)
{instance_checks}if 'QUADRINGENT_LOADER_TABLE_SET_JSON' in os.environ:
 import quadringent_destination_loader as loader
 tables=[t for t in loader.parse_table_set(os.environ['QUADRINGENT_LOADER_TABLE_SET_JSON']) if t.table_id=={self.args.table_id!r}]
 if len(tables)!=1 or tables[0].evidence_key!={self.args.gcs_read_key!r}: raise SystemExit(1)
client=storage.Client(project=project,credentials=credentials)
blob=client.bucket({self.args.expected_gcs_bucket!r}).blob({self.args.gcs_read_key!r})
blob.reload(timeout=10,retry=None)
if not blob.size or blob.size>262144: raise SystemExit(1)
payload=blob.download_as_bytes(if_generation_match=blob.generation,timeout=10,retry=None)
evidence=json.loads(payload)
boundary=evidence.get('boundary',{{}})
if (evidence.get('pipeline_id')!={self.args.pipeline!r} or evidence.get('table_id')!={self.args.table_id!r} or
 evidence.get('run_id')!={self.args.copy_run_id!r} or str(boundary.get('receiver_name'))+':'+str(boundary.get('last_sequence'))!={self.args.copy_boundary!r}): raise SystemExit(1)
print(json.dumps({{'method':'google-compute-metadata','credential_type':'google.auth.compute_engine.credentials.Credentials',
 'service_account_email':credentials.service_account_email,'project':project,'refreshed':True,
 'gcs_read':{{'verified':True,'bucket':{self.args.expected_gcs_bucket!r},'key':{self.args.gcs_read_key!r},
 'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest(),'generation':str(blob.generation)}}{instance_output}}}))
'''

    def workload(self, name, expected_digest):
        deployment = self.kubectl("get", "deployment", name, "-o", "json")
        template = deployment["spec"]["template"]
        meta = template["metadata"]
        if meta.get("labels", {}).get("quadringent.io/managed-by") != "quadringent-control-plane":
            raise ValueError("workload hors chemin produit")
        if self.args.table_id not in meta.get("annotations", {}).get("quadringent.io/table-ids", "").split(","):
            raise ValueError("table absente du workload produit")
        selector = ",".join(f"{k}={v}" for k, v in deployment["spec"]["selector"]["matchLabels"].items())
        pods = self.kubectl("get", "pods", "-l", selector, "-o", "json")["items"]
        ready = [p for p in pods if p["status"].get("phase") == "Running"
                 and not p["metadata"].get("deletionTimestamp")
                 and p["status"].get("containerStatuses")
                 and all(c.get("ready") for c in p["status"]["containerStatuses"])]
        if len(ready) != 1:
            raise ValueError("workload doit avoir exactement un pod prêt")
        pod = ready[0]
        controllers=[o for o in pod["metadata"].get("ownerReferences",[]) if o.get("controller") is True]
        if len(controllers)!=1 or controllers[0]["kind"]!="ReplicaSet":
            raise ValueError("owner du pod non prouvé")
        owner=controllers[0]
        rs=self.kubectl("get","replicaset",owner["name"],"-o","json")
        rsowners=[o for o in rs["metadata"].get("ownerReferences",[]) if o.get("controller") is True]
        if (rs["metadata"]["uid"]!=owner["uid"] or len(rsowners)!=1 or
                rsowners[0]["kind"]!="Deployment" or rsowners[0]["uid"]!=deployment["metadata"]["uid"]):
            raise ValueError("chaîne UID Deployment/RS/pod invalide")
        statuses = pod["status"]["containerStatuses"]
        component=meta["labels"].get("quadringent.io/component")
        container_name={"reader":"reader","destination-loader":"destination-loader"}.get(component)
        selected=[c for c in statuses if c["name"]==container_name]
        if len(selected)!=1:
            raise ValueError("digest exécuté différent du candidat")
        binding=None
        node=None
        if getattr(self,'release_images',None) or self.args.identity=='gcp-vm-metadata':
            node=self.kubectl('get','node',pod['spec']['nodeName'],'-o','json')
            architecture=node['status']['nodeInfo']['architecture']
            if (node['metadata']['name']!=pod['spec']['nodeName'] or node['status']['nodeInfo']['operatingSystem']!='linux' or
                    node['metadata'].get('labels',{}).get('kubernetes.io/arch')!=architecture or
                    node['metadata'].get('labels',{}).get('kubernetes.io/os')!='linux'):
                raise ValueError('architecture du nœud non prouvée')
            if self.args.identity=='gcp-vm-metadata' and node['metadata']['name']!=self.args.expected_gcp_instance_name:
                raise ValueError('nœud Kubernetes hors VM GCP attendue')
            if getattr(self,'release_images',None):
                release_component={'reader':('capture',self.args.reader_deployment),
                                   'destination-loader':('control-plane',self.args.loader_deployment)}.get(component)
                if release_component is None or name!=release_component[1]:
                    raise ValueError('Deployment différent du composant produit attendu')
                binding=image_binding(self.release_images[release_component[0]],expected_digest,selected[0].get('imageID'),architecture)
        if not binding and observed_image_digest(selected[0].get('imageID'))!=expected_digest:
            raise ValueError('digest exécuté différent du candidat')
        matching = [c for c in pod["spec"]["containers"] if c["name"]==container_name and c["image"].endswith("@" + expected_digest)]
        if len(matching) != 1:
            raise ValueError("conteneur digest ambigu")
        if (deployment['spec'].get('replicas',1)!=1 or deployment.get('status',{}).get('readyReplicas',0)!=1 or
                deployment.get('status',{}).get('observedGeneration',0)<deployment['metadata']['generation']):
            raise ValueError('workload prêt mais génération ou réplica non stabilisé')
        sa_name = pod["spec"]["serviceAccountName"]
        sa = self.kubectl("get", "serviceaccount", sa_name, "-o", "json")
        env = {e["name"]: e["value"] for e in matching[0].get("env", []) if "value" in e}
        if self.args.identity == "irsa" and not sa["metadata"].get("annotations", {}).get("eks.amazonaws.com/role-arn"):
            raise ValueError("IRSA non déclaré")
        if self.args.identity=='gke-workload-identity':
            if sa['metadata'].get('annotations',{}).get('iam.gke.io/gcp-service-account')!=self.args.expected_gcp_service_account:
                raise ValueError('annotation KSA différente de la GSA attendue')
            script=self.gke_identity_script()
        elif self.args.identity=='gcp-vm-metadata':
            if sa['metadata'].get('annotations',{}).get('iam.gke.io/gcp-service-account'):
                raise ValueError('identité VM GCP déclarée comme Workload Identity GKE')
            script=self.gcp_vm_identity_script()
        else:
            script="import boto3,json; s=boto3.Session(); print(json.dumps({'method':s.get_credentials().method,'arn':s.client('sts').get_caller_identity()['Arn']}))"
        identity=self.exec_json(pod["metadata"]["name"],container_name,script)
        effective=self.verify_identity(identity)
        proof = {"deployment": name, "uid": deployment["metadata"]["uid"], "pod": pod["metadata"]["name"],
                 "selector": deployment['spec']['selector']['matchLabels'],
                 "pod_uid": pod["metadata"]["uid"], "container": matching[0]["name"],
                 "images": [{"name": c["name"], "image_id": c["imageID"]} for c in statuses],
                 "service_account": sa_name, "identity_mode": self.args.identity,
                 "irsa_declared": bool(sa["metadata"].get("annotations", {}).get("eks.amazonaws.com/role-arn"))}
        proof["identity_effective"]=effective
        if self.args.identity=='gcp-vm-metadata':proof['node']=node['metadata']['name']
        if binding:proof['image_binding']=binding
        proof.update(desired_replicas=deployment['spec'].get('replicas',1),
                     ready_replicas=deployment.get('status',{}).get('readyReplicas',0))
        return proof, env

    def reader_state(self,mode,before):
        if before.get('snapshot_checks_passed') is not True or any(before.get(k)!=getattr(self.args,k)
                                                                  for k in ('pipeline','table','table_id','namespace','context')):
            raise ValueError('baseline pause/reprise invalide')
        name,labels,uid=reader_reference(before['reader'])
        candidate=workload_candidate(before['reader'],'reader')
        loader_candidate=workload_candidate(before['loader'],'destination-loader')
        capture_index=candidate[0][0] if isinstance(candidate[0],tuple) else candidate[0]
        loader_index=loader_candidate[0][0] if isinstance(loader_candidate[0],tuple) else loader_candidate[0]
        if (name!=self.args.reader_deployment or capture_index!=self.args.capture_digest or
                loader_index!=self.args.loader_digest or candidate[1]!=self.args.identity or
                loader_candidate[1]!=self.args.identity):
            raise ValueError('candidat ou nom différents de la baseline')
        for component in ('reader','loader'):
            self.verify_identity(before[component]['identity_effective'])
        if mode=='pause':
            # Une erreur API/RBAC remonte de kubectl ; seul un listing réussi
            # et vide du nom attendu prouve l'absence du Deployment natif.
            deployments=self.kubectl('get','deployments','--field-selector','metadata.name='+name,'-o','json')['items']
            if deployments:
                raise ValueError('Deployment lecteur encore présent')
            selector=','.join(f'{k}={v}' for k,v in labels.items())
            pods=self.kubectl('get','pods','-l',selector,'-o','json')['items']
            if pods:
                raise ValueError('lecteur pas complètement arrêté')
            reader={'deployment':name,'selector':dict(labels),'deployment_absent':True,'owned_pods':[]}
        else:
            reader,_=self.workload(self.args.reader_deployment,self.args.capture_digest)
            new_name,new_labels,new_uid=reader_reference(reader)
            if (new_name!=name or new_labels!=labels or new_uid==uid or
                    not reader.get('pod_uid') or reader['pod_uid']==before['reader'].get('pod_uid') or
                    workload_candidate(reader,'reader')!=candidate or
                    reader['desired_replicas']!=1 or reader['ready_replicas']!=1):
                raise ValueError('lecteur pas complètement repris')
        return {'kind':'native-reader-'+mode+'-proof','observed_utc':utc(),'reader':reader,
                **{k:getattr(self.args,k) for k in ('pipeline','table','table_id','namespace','context')}}

    def checkpoint(self, reader, loader):
        proof, env = reader
        target = loader[0]  # Reste accessible pendant une pause du lecteur, via son identité produit.
        values = {key: env[key] for key in (
            "QUADRINGENT_STORAGE_BACKEND",
            "AS400_STREAM_KEY",
            "AS400_RAW_BUCKET",
        )}
        location = "AS400_CHECKPOINT_BUCKET" if values["QUADRINGENT_STORAGE_BACKEND"] == "gcs" else "AS400_CHECKPOINT_TABLE"
        values[location] = env[location]
        script = ("import json,os,sys; sys.path.insert(0,'/app'); "
                  "from quadringent.storage_backend import StorageBackend; "
                  f"os.environ.update({values!r}); "
                  "p=StorageBackend.from_environment(os.environ).checkpoint_store(os.environ['AS400_STREAM_KEY']).load(); "
                  "print(json.dumps(None if p is None else {'receiver':p.receiver,'sequence':p.sequence}))")
        return self.exec_json(target["pod"], target["container"], script)

    def source(self, reader):
        proof, env = reader
        if self.library != env["ISERIES_SCHEMA"].upper():
            raise ValueError("bibliothèque différente du lecteur")
        tables = json.loads(env["AS400_TABLE_BOOTSTRAP_JSON"])
        if not any(t["table_id"] == self.args.table_id and t["schema"].upper() == self.library
                   and t["table"].upper() == self.table for t in tables):
            raise ValueError("table source différente du contrat natif")
        command = ["java", "-cp", "/app/probe.jar:/app/lib/*",
                   "io.quadringent.as400.QualificationSourceDriver", "dump", self.library, self.table,
                   self.args.journal_library, self.args.journal_name, self.args.pk, ",".join(self.columns)]
        script = f'''import json,os,subprocess
stdin='\\n'.join(os.environ[k] for k in ('ISERIES_HOST','ISERIES_USER','ISERIES_PASSWORD'))+'\\n'
p=subprocess.run({command!r},input=stdin,text=True,capture_output=True,timeout=25)
if p.returncode: raise SystemExit(1)
rows=[json.loads(x[len('SRC_ROW='):]) for x in p.stdout.splitlines() if x.startswith('SRC_ROW=')]
counts=[x for x in p.stdout.splitlines() if x.startswith('SRC_ROW_COUNT=')]
if counts != ['SRC_ROW_COUNT='+str(len(rows))] or len(rows)>{MAX_ROWS}: raise SystemExit(1)
print(json.dumps(rows))
'''
        return self.exec_json(proof["pod"], proof["container"], script)

    def snowflake(self, loader):
        proof = loader[0]
        env = loader[1]
        if (env.get("QUADRINGENT_DESTINATION_DATABASE") != self.args.database or
                env.get("QUADRINGENT_DESTINATION_SCHEMA") != self.args.schema):
            raise ValueError("destination différente du chargeur natif")
        base = f'"{self.args.database}"."{self.args.schema}"'
        fields = ",".join(f'"{x}"' for x in self.columns)
        mirror = f'SELECT {fields} FROM {base}."{self.args.mirror}" LIMIT {MAX_ROWS + 1}'
        technical=["EVENT_ID","OPERATION","JOURNAL_RECEIVER","JOURNAL_SEQUENCE"]
        history = f'SELECT {fields}, '+','.join('"'+x+'"' for x in technical)+f' FROM {base}."{self.args.history}" LIMIT {MAX_ROWS + 1}'
        script = f'''import json,os,sys
sys.path.insert(0,'/app')
import quadringent_destination_loader as l
tables=l.parse_table_set(os.environ['QUADRINGENT_LOADER_TABLE_SET_JSON'])
tables=[t for t in tables if t.table_id=={self.args.table_id!r}]
if len(tables)!=1: raise SystemExit(1)
t=tables[0];plan=l.build_plan(t,database={self.args.database!r},schema={self.args.schema!r})
if (t.schema_name.upper()!= {self.library!r} or t.table_name.upper()!= {self.table!r} or
 list(t.key_columns)!=[{self.args.pk!r}] or [x['name'] for x in t.columns]!={list(self.columns)!r} or
 plan.history_table!={self.args.history!r} or plan.mirror_table!={self.args.mirror!r}): raise SystemExit(1)
groups={{'int':{{'int','integer','smallint','bigint'}},'decimal':{{'decimal','numeric','decfloat'}},
 'text':{{'char','varchar','clob'}},'date':{{'date'}},'timestamp':{{'timestamp'}}}}
if any(str(x['kind']).lower() not in groups[{self.columns!r}[x['name']]] for x in t.columns): raise SystemExit(1)
c=l._connect_snowflake(account=os.environ['SNOWFLAKE_ACCOUNT'],user=os.environ['SNOWFLAKE_USER'],role=os.environ['SNOWFLAKE_ROLE'],private_key_pem=os.environ['SNOWFLAKE_PRIVATE_KEY_PEM'],warehouse=l._snowflake_role_to_warehouse(os.environ['SNOWFLAKE_ROLE']))
try:
 cursor=c.cursor();cursor.execute("ALTER SESSION SET QUERY_TAG = 'quadringent-native-qualification-probe'")
 cursor.execute('ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 10')
 result={{}}
 for name,sql,columns in [('mirror',{mirror!r},{list(self.columns)!r}),('history',{history!r},{list(self.columns)!r}+{technical!r})]:
  cursor.execute(sql,timeout=10);rows=cursor.fetchall()
  if len(rows)>{MAX_ROWS}: raise SystemExit(1)
  result[name]=[dict(zip(columns,row)) for row in rows]
 result['history_mode']=os.environ.get('QUADRINGENT_HISTORY_MODE','streaming')
 result['copy_evidence_key']=t.evidence_key
 print(json.dumps(result,default=str))
finally: c.close()
'''
        data = self.exec_json(proof["pod"], proof["container"], script)
        if data.get('history_mode', '').strip().lower() != getattr(self.args, 'expected_history_mode', 'streaming'):
            raise ValueError('mode HISTORY différent du mode attendu')
        return data

    def source_positions(self,reader,before):
        proof=reader[0];checkpoint=before.get('checkpoint')
        if not checkpoint or any(before.get(k)!=getattr(self.args,k) for k in ('pipeline','table','table_id','namespace','context')):
            raise ValueError('baseline positions source invalide')
        command=['java','-cp','/app/probe.jar:/app/lib/*','io.quadringent.as400.QualificationSourceDriver','rowpos',
                 self.library,self.table,self.args.journal_library,self.args.journal_name,self.args.pk,','.join(self.columns),
                 checkpoint['receiver'],str(checkpoint['sequence'])]
        script=f'''import json,os,subprocess
stdin='\\n'.join(os.environ[k] for k in ('ISERIES_HOST','ISERIES_USER','ISERIES_PASSWORD'))+'\\n'
p=subprocess.run({command!r},input=stdin,text=True,capture_output=True,timeout=25)
if p.returncode: raise SystemExit(1)
rows=[json.loads(x[len('SRC_ROWPOS='):]) for x in p.stdout.splitlines() if x.startswith('SRC_ROWPOS=')]
counts=[x for x in p.stdout.splitlines() if x.startswith('SRC_ROWPOS_COUNT=')]
if counts != ['SRC_ROWPOS_COUNT='+str(len(rows))] or len(rows)>{MAX_ROWS}: raise SystemExit(1)
print(json.dumps({{'kind':'ibmi-source-rowpos-reader','positions':rows}}))
'''
        return self.exec_json(proof['pod'],proof['container'],script)

    def snapshot(self):
        self.deadline=min(getattr(self,"deadline",float("inf")),time.monotonic()+self.args.max_seconds)
        reader = self.workload(self.args.reader_deployment, self.args.capture_digest)
        loader = self.workload(self.args.loader_deployment, self.args.loader_digest)
        jobs = self.kubectl("get", "jobs", "-l", f"quadringent.io/pipeline-id={self.args.pipeline}", "-o", "json")["items"]
        matching_jobs = [j for j in jobs if j["metadata"].get("labels", {}).get("quadringent.io/table-id") == self.args.table_id
                         and j["metadata"].get("labels", {}).get("quadringent.io/component") == "initial-copy"
                         and j["metadata"].get("annotations",{}).get("quadringent.io/run-id")==self.args.copy_run_id
                         and j["metadata"].get("annotations",{}).get("quadringent.io/bootstrap-boundary")==self.args.copy_boundary]
        job_proof = [{"name": j["metadata"]["name"], "uid": j["metadata"]["uid"],
                      "succeeded": j.get("status", {}).get("succeeded", 0),
                      "failed": j.get("status", {}).get("failed", 0),
                      "images": [c["image"] for c in j["spec"]["template"]["spec"]["containers"]]}
                     for j in matching_jobs]
        source = self.source(reader)
        warehouse = self.snowflake(loader)
        bound_jobs=[]
        for j in matching_jobs:
            containers=[c for c in j['spec']['template']['spec']['containers'] if c['name']=='initial-copy']
            if len(containers)!=1: continue
            env={e['name']:e['value'] for e in containers[0].get('env',[]) if 'value' in e}
            if env.get('AS400_EVIDENCE_KEY')==warehouse['copy_evidence_key'] and env.get('AS400_PIPELINE_ID')==self.args.pipeline:
                bound_jobs.append(j['metadata']['uid'])
        oracle=json.loads(Path(self.args.history_oracle).read_text()) if self.args.history_oracle else None
        if oracle is not None and (oracle.get('pipeline')!=self.args.pipeline or oracle.get('table')!=self.args.table or
                                  oracle.get('copy_run_id')!=self.args.copy_run_id or oracle.get('copy_boundary')!=self.args.copy_boundary or
                                  oracle.get('kind')!='ibmi-journal-and-snapshot-oracle' or oracle.get('independent_of_snowflake') is not True or
                                  any(oracle.get(k)!=getattr(self.args,k) for k in ('table_id','namespace','context'))):
            raise ValueError('oracle historique hors périmètre')
        warehouse['history']=normalize_history(warehouse['history'],self.columns)
        history=history_proof(warehouse['history'],normalize_history(oracle['events'],self.columns) if oracle else None)
        event_ids = [r["EVENT_ID"] for r in warehouse["history"]]
        if any(not isinstance(v,str) or not v for v in event_ids):
            raise ValueError("EVENT_ID absent de l'historique")
        result = {"observed_utc": utc(), "pipeline": self.args.pipeline, "table": self.args.table,
                "namespace":self.args.namespace,"context":self.args.context,
                "table_id": self.args.table_id, "reader": reader[0], "loader": loader[0], "jobs": job_proof,
                "copy_job_observed_succeeded": any(j["uid"] in bound_jobs and j["succeeded"] and not j["failed"] and
                                                      any(i.endswith('@'+self.args.capture_digest) for i in j["images"])
                                                      for j in job_proof),
                "checkpoint": self.checkpoint(reader, loader), "source": source, **warehouse,
                "history_validation":history,"native_cdc_qualified":False,
                "duplicate_event_ids": len(event_ids)-len(set(event_ids)),
                "reconciliation": compare_rows(source, warehouse["mirror"], self.columns, self.args.pk)}
        result["snapshot_checks_passed"] = bool(result["copy_job_observed_succeeded"] and result["checkpoint"]
                                               and not result["duplicate_event_ids"] and result["reconciliation"]["equal"])
        if self.args.before:
            result['source_journal_positions']=self.source_positions(reader,json.loads(Path(self.args.before).read_text()))
            result['observed_utc']=utc()
        return result

    def observe(self):
        receipt = json.loads(Path(self.args.receipt).read_text())
        baseline = json.loads(Path(self.args.baseline).read_text())
        for data in (receipt, baseline):
            if any(data.get(k) != getattr(self.args,k) for k in ("pipeline","table","table_id","namespace","context")):
                raise ValueError("preuve hors périmètre")
        start, ack = validate_receipt(receipt,baseline["observed_utc"])
        if baseline.get('snapshot_checks_passed') is not True:
            raise ValueError('baseline non qualifiée')
        expected = receipt.get("mutation_identity")
        operation = receipt.get('operation')
        if operation not in {'insert','update','delete'} or receipt.get('expected',expected) != expected:
            raise ValueError('sélecteur différent de la mutation acquittée')
        if not expected or self.args.pk not in expected or any(k not in self.columns for k in expected):
            raise ValueError("sélecteur de mutation invalide")
        def matches(row):
            return all(normalize(row[k], self.columns[k]) == normalize(v, self.columns[k]) for k,v in expected.items())
        history_operation={'insert':'c','update':'u_after','delete':'d'}[operation]
        def history_matches(row):return row.get('OPERATION')==history_operation and matches(row)
        if any(history_matches(r) for r in baseline['history']):raise ValueError('événement déjà présent dans la baseline')
        if operation=='delete':
            if set(expected)!={self.args.pk} or not any(matches(r) for r in baseline['mirror']):
                raise ValueError('suppression sans ligne initiale attestée')
        elif any(matches(r) for r in baseline['mirror']):raise ValueError('marqueur déjà présent dans la baseline')
        deadline = min(getattr(self,"deadline",float("inf")),time.monotonic() + self.args.max_seconds)
        self.deadline=deadline
        loader = self.workload(self.args.loader_deployment, self.args.loader_digest)
        report = {"pipeline": self.args.pipeline, "table": self.args.table, "table_id":self.args.table_id, "namespace":self.args.namespace, "context":self.args.context, "receipt_sha256":hashlib.sha256(Path(self.args.receipt).read_bytes()).hexdigest(), "write_started_utc": start.isoformat(),
                  "write_ack_utc": ack.isoformat(), "poll_interval_s": self.args.poll_seconds,
                  "baseline_sha256": hashlib.sha256(Path(self.args.baseline).read_bytes()).hexdigest(),
                  "status": "timeout", "latency_kind": "first_observed_upper_bound", "threshold_s":10,
                  "maximum_10s_qualified":False,"native_cdc_qualified":False}
        while time.monotonic() < deadline:
            data = self.snowflake(loader)
            if time.monotonic()>deadline: raise RuntimeError('native_probe_deadline_exceeded')
            report['history_mode']=data['history_mode']
            visible = stamp(utc())
            for layer in ("history", "mirror"):
                visible_layer=(any(history_matches(row) for row in data['history']) if layer=='history' else
                               not any(matches(row) for row in data['mirror']) if operation=='delete' else
                               any(matches(row) for row in data['mirror']))
                if layer+"_visible_utc" not in report and visible_layer:
                    durations=latencies(start.isoformat(),ack.isoformat(),visible.isoformat())
                    report.update({layer+"_visible_utc": visible.isoformat(),layer+"_from_ack_s":durations['from_ack_s'],
                                   layer+"_from_start_s":durations['from_start_s']})
            if all(layer+"_visible_utc" in report for layer in ("history", "mirror")):
                report["status"] = "visible"
                report['observed_threshold_met']=all(report[layer+'_from_start_s']<=10 and report[layer+'_from_ack_s']<=10
                                                      for layer in ('history','mirror'))
                return report
            time.sleep(min(self.args.poll_seconds,max(0,deadline-time.monotonic())))
        return report
