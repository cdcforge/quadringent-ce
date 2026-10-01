import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest
import yaml

from quadringent.qualification import release_admission as a

NOW = datetime(2026, 9, 30, 17, tzinfo=timezone.utc)
SOURCE = 'a' * 40
TAG = 'v0.2.2'


def sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def raw_json(value):
    return json.dumps(value, separators=(',', ':')).encode()


class Client:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, endpoint):
        self.calls.append(endpoint)
        return deepcopy(self.values[endpoint])


def fixture(tmp_path, override_label=None, duplicate_arch=False, config_arch=None, repository=a.REPO):
    root = tmp_path / 'oci'; root.mkdir()
    files = {}; indexes = {}
    for component in a.COMPONENTS:
        directory = root / component; (directory / 'blobs/sha256').mkdir(parents=True)
        prefix = f'{component}/{component}/'
        def store(path, raw):
            target = directory / path; target.write_bytes(raw); files[prefix + path] = sha(raw)
        def blob(value, media):
            raw = raw_json(value); digest = sha(raw); store('blobs/sha256/' + digest[7:], raw)
            return {'digest': digest, 'size': len(raw), 'mediaType': media}
        variants = []
        for arch in (['amd64', 'amd64'] if duplicate_arch else ['amd64', 'arm64']):
            config = blob({'os': 'linux', 'architecture': config_arch or arch, 'history': [{'comment': arch}], 'config': {'Labels': {
                'org.opencontainers.image.revision': SOURCE, 'org.opencontainers.image.version': '0.2.2',
                'org.opencontainers.image.source': 'https://github.com/' + repository,
                **({override_label[0]:override_label[1]} if override_label else {})}}},
                'application/vnd.oci.image.config.v1+json')
            layer = {'digest': sha(b'public-synthetic-layer'), 'size': 22,
                     'mediaType': 'application/vnd.oci.image.layer.v1.tar+gzip'}
            manifest = blob({'schemaVersion': 2, 'config': config, 'layers': [layer]},
                            'application/vnd.oci.image.manifest.v1+json')
            manifest['platform'] = {'os': 'linux', 'architecture': arch}; variants.append(manifest)
            for descriptor in [config, manifest, layer]:
                files[f'{component}/{component}-{arch}/blobs/sha256/{descriptor["digest"][7:]}'] = descriptor['digest']
            files[prefix + 'blobs/sha256/' + layer['digest'][7:]] = layer['digest']
            files[f'{component}/{component}-{arch}/index.json'] = sha(raw_json({'schemaVersion': 2, 'manifests': [manifest]}))
            files[f'{component}/{component}-{arch}/oci-layout'] = sha(b'{"imageLayoutVersion":"1.0.0"}')
        index = blob({'schemaVersion': 2, 'manifests': variants}, 'application/vnd.oci.image.index.v1+json')
        indexes[component] = index['digest']
        store('index.json', raw_json({'schemaVersion': 2, 'manifests': [index]}))
        store('oci-layout', b'{"imageLayoutVersion":"1.0.0"}')
        files[f'{component}/{component}-secret-files/layout-index.json'] = files[prefix + 'index.json']
    passed = raw_json({'files': files})
    archive = tmp_path / 'receipt.zip'
    def zip_receipt(content, name='passed.json', extra=False):
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr(name, content)
            if extra: z.writestr('extra.json', '{}')
    zip_receipt(passed)
    seal_index = a.REQUIRED_STEPS.index('Sceller les six scans avant toute authentification de publication')
    names = [*a.REQUIRED_STEPS[:seal_index + 1], 'Conserver temporairement le reçu des scans OCI',
             *a.REQUIRED_STEPS[seal_index + 1:seal_index + 3], a.METADATA_UPLOAD_STEP,
             *a.REQUIRED_STEPS[seal_index + 3:]]
    steps = [{'name': n, 'number': i+1, 'status': 'completed', 'conclusion': 'success',
              'started_at': '2026-09-30T16:00:00Z', 'completed_at': '2026-09-30T16:00:00Z'} for i,n in enumerate(names)]
    workflow = yaml.safe_dump({'name': 'release', 'jobs': {'images': {'steps': [
        {'name': a.REQUIRED_STEPS[seal_index], 'run': 'python scripts/release_oci.py seal'},
        {'name': 'Conserver temporairement le reçu des scans OCI', 'uses': 'actions/upload-artifact@v4', 'with': {
            'name': 'oci-scan-receipt', 'path': '${{ runner.temp }}/oci/passed.json',
            'retention-days': 1, 'if-no-files-found': 'error'}},
        {'name': a.REQUIRED_STEPS[13], 'run': 'python scripts/release_oci.py export-metadata'},
        {'name': a.REQUIRED_STEPS[14], 'run': 'trivy fs --scanners secret --exit-code 1'},
        {'name': a.METADATA_UPLOAD_STEP, 'uses': 'actions/upload-artifact@v4', 'with': {
            'name': 'oci-admission-metadata', 'path': '${{ runner.temp }}/oci-admission-metadata/',
            'retention-days': 1, 'if-no-files-found': 'error'}},
        {'uses': 'docker/login-action@v3'}]}}}).encode()
    expected = a.Expected(123, 1, SOURCE, TAG, hashlib.sha256(workflow).hexdigest(),repository=repository)
    base = 'repos/' + repository
    values = {
        base: {'id': 7, 'full_name': repository, 'private': True},
        base+'/actions/runs/123': {'id':123,'run_attempt':1,'event':'workflow_dispatch','head_sha':SOURCE,
            'head_branch':TAG,'path':'.github/workflows/release.yml','status':'completed','conclusion':'success',
            'repository':{'id':7,'full_name':repository,'private':True},'head_repository':{'id':7,'full_name':repository}},
        base+'/git/ref/tags/'+TAG: {'ref':'refs/tags/'+TAG,'object':{'type':'commit','sha':SOURCE}},
        base+'/releases/tags/'+TAG: {'id':456,'tag_name':TAG,'draft':True},
        base+'/contents/.github/workflows/release.yml?ref='+SOURCE: {'encoding':'base64','content':base64.b64encode(workflow).decode()},
        base+'/actions/runs/123/attempts/1/jobs?per_page=100&page=1': {'total_count':4,'jobs':[
            {'id':i,'name':name,'run_id':123,'run_attempt':1,'status':'completed','conclusion':'success',
             'started_at':'2026-09-30T15:00:00Z','completed_at':'2026-09-30T16:30:00Z','steps':steps if name=='images' else [{'name':'Valider les sept contrôles CI du commit exact','number':1,'status':'completed','conclusion':'success','started_at':'2026-09-30T15:00:00Z','completed_at':'2026-09-30T15:00:00Z'}] if name=='validate' else []}
            for i,name in enumerate(['validate','images','chart','release'],1)]},
        base+'/actions/artifacts/99': {'id':99,'name':'oci-scan-receipt','expired':False,
            'digest':sha(archive.read_bytes()),'size_in_bytes':archive.stat().st_size,
            'created_at':'2026-09-30T16:00:00Z','expires_at':'2026-10-01T16:00:00Z','workflow_run':{'id':123,'head_sha':SOURCE,'repository_id':7,'head_repository_id':7}}
    }
    ci_jobs=['tests','quadringent-product-gate','java','chart','cockpit-runtime','verifier-runtime','image-runtime']
    ci_workflow=yaml.safe_dump({'name':'ci','jobs':{name:{} for name in ci_jobs}}).encode()
    values[base+'/contents/.github/workflows/ci.yml?ref='+SOURCE]={'encoding':'base64','content':base64.b64encode(ci_workflow).decode()}
    values[base+'/actions/workflows/ci.yml']={'id':88,'path':'.github/workflows/ci.yml','state':'active'}
    ci_run={'id':777,'run_attempt':1,'workflow_id':88,'event':'push','head_branch':'main','head_sha':SOURCE,'path':'.github/workflows/ci.yml','status':'completed','conclusion':'success','repository':{'id':7,'full_name':repository,'private':True},'head_repository':{'id':7,'full_name':repository}}
    values[base+'/actions/workflows/ci.yml/runs?head_sha='+SOURCE+'&branch=main&event=push&per_page=100&page=1']={'total_count':1,'workflow_runs':[ci_run]}
    values[base+'/actions/runs/777']=ci_run
    values[base+'/actions/runs/777/attempts/1/jobs?per_page=100&page=1']={'total_count':7,'jobs':[{'id':100+i,'name':name,'run_id':777,'run_attempt':1,'status':'completed','conclusion':'success','started_at':'2026-09-30T14:00:00Z','completed_at':'2026-09-30T14:50:00Z'} for i,name in enumerate(ci_jobs)]}
    metadata_archive = tmp_path / 'metadata.zip'
    with zipfile.ZipFile(metadata_archive, 'w') as target:
        for path in root.rglob('*'):
            if path.is_file(): target.writestr(path.relative_to(root).as_posix(), path.read_bytes())
    values[base+'/actions/artifacts/100'] = {**deepcopy(values[base+'/actions/artifacts/99']),
        'id':100, 'name':'oci-admission-metadata', 'digest':sha(metadata_archive.read_bytes()),
        'size_in_bytes':metadata_archive.stat().st_size}
    return expected, root, archive, Client(values), passed, indexes, zip_receipt, metadata_archive


def execute(f):
    return a.admit(f[0], f[1], f[2], 99, f[3], NOW, metadata_archive=f[7], metadata_artifact_id=100)


def test_pass_explicitly_separates_ci_seal_from_local_metadata(tmp_path):
    f=fixture(tmp_path); proof,raw=execute(f)
    assert proof['kind']=='quadringent-oci-runtime-admission'
    assert proof['complete_seal_verified'] and proof['verified_before_cloud'] is False
    assert proof['seal_verification_location']=='github-actions'
    assert proof['local_verification_scope']=='indexes_manifests_configs_only'
    assert proof['local_full_fingerprint_verified'] is False
    assert proof['indexes']==f[5] and raw==f[4]
    assert proof['passed_sha256']==hashlib.sha256(raw).hexdigest()
    assert proof['revision']==SOURCE


@pytest.mark.parametrize('change', ['private','run_failure','wrong_repo','source','tag','attempt','workflow',
 'scan_failed','scan_missing','sign_missing','duplicate_step','job_attempt','expiry','artifact_source',
 'artifact_run','zip_digest','artifact_name','artifact_old_attempt','artifact_expired_date','release_tag'])
def test_authenticated_evidence_drift_refused(tmp_path,change):
    f=fixture(tmp_path);v=f[3].values;b='repos/'+a.REPO;r=v[b+'/actions/runs/123'];artifact=v[b+'/actions/artifacts/99']
    jobs=v[b+'/actions/runs/123/attempts/1/jobs?per_page=100&page=1']['jobs'];steps=jobs[1]['steps']
    if change=='private':v[b]['private']=False
    elif change=='run_failure':r['conclusion']='failure'
    elif change=='wrong_repo':r['repository']['full_name']='other/repository'
    elif change=='source':r['head_sha']='b'*40
    elif change=='tag':r['head_branch']='main'
    elif change=='attempt':r['run_attempt']=2
    elif change=='workflow':v[b+'/contents/.github/workflows/release.yml?ref='+SOURCE]['content']=base64.b64encode(b'changed').decode()
    elif change=='scan_failed':next(s for s in steps if s['name']=='Scan Trivy — capture')['conclusion']='failure'
    elif change=='scan_missing':steps[:]=[s for s in steps if s['name']!='Scan Trivy — verifier']
    elif change=='sign_missing':steps.pop()
    elif change=='duplicate_step':steps.append(deepcopy(steps[4]))
    elif change=='job_attempt':jobs[1]['run_attempt']=2
    elif change=='expiry':artifact['expired']=True
    elif change=='artifact_source':artifact['workflow_run']['head_sha']='b'*40
    elif change=='artifact_run':artifact['workflow_run']['id']=999
    elif change=='zip_digest':artifact['digest']='sha256:'+'0'*64
    elif change=='artifact_name':artifact['name']='other'
    elif change=='artifact_old_attempt':artifact['created_at']='2026-09-29T16:00:00Z'
    elif change=='artifact_expired_date':artifact['expires_at']='2026-09-30T16:00:00Z'
    else:v[b+'/releases/tags/'+TAG]['tag_name']='v0.2.1'
    with pytest.raises(ValueError):execute(f)


@pytest.mark.parametrize('change',['extra','traversal','duplicate_json','auth_data','unsafe_path','bad_hash','oversize'])
def test_receipt_untrusted_structure_refused_even_when_zip_digest_matches(tmp_path,change,monkeypatch):
    f=fixture(tmp_path);d=json.loads(f[4]);raw=f[4];name='passed.json';extra=False
    if change=='extra':extra=True
    elif change=='traversal':name='../passed.json'
    elif change=='duplicate_json':raw=b'{"files":{},"files":{}}'
    elif change=='auth_data':d['Authorization']='private-never-print';raw=raw_json(d)
    elif change=='unsafe_path':d['files']['capture/capture-secret-files/client-name.txt']=sha(b'');raw=raw_json(d)
    elif change=='bad_hash':d['files'][next(iter(d['files']))]='not-sha';raw=raw_json(d)
    else:monkeypatch.setattr(a,'MAX_RECEIPT_BYTES',1)
    f[6](raw,name,extra);artifact=f[3].values['repos/'+a.REPO+'/actions/artifacts/99'];artifact['digest']=sha(f[2].read_bytes());artifact['size_in_bytes']=f[2].stat().st_size
    with pytest.raises(ValueError):execute(f)


@pytest.mark.parametrize('change',['config_bytes','arch','revision','version','source_label','missing_manifest','index_drift','symlink'])
def test_metadata_drift_refused(tmp_path,change):
    f=fixture(tmp_path);directory=f[1]/'capture';top=json.loads((directory/'index.json').read_text());index=json.loads((directory/'blobs/sha256'/top['manifests'][0]['digest'][7:]).read_text());manifest=json.loads((directory/'blobs/sha256'/index['manifests'][0]['digest'][7:]).read_text());config=directory/'blobs/sha256'/manifest['config']['digest'][7:]
    if change=='missing_manifest':(directory/'blobs/sha256'/index['manifests'][0]['digest'][7:]).unlink()
    elif change=='index_drift':(directory/'index.json').write_text('{}')
    elif change=='symlink':raw=config.read_bytes();config.unlink();target=f[1]/'outside';target.write_bytes(raw);config.symlink_to(target)
    else:
        value=json.loads(config.read_text())
        if change=='arch':value['architecture']='s390x'
        elif change=='revision':value['config']['Labels']['org.opencontainers.image.revision']='b'*40
        elif change=='version':value['config']['Labels']['org.opencontainers.image.version']='0.2.1'
        elif change=='source_label':value['config']['Labels']['org.opencontainers.image.source']='https://github.com/other/private'
        else:value['modified']=True
        config.write_bytes(raw_json(value))
    with pytest.raises(ValueError):execute(f)


def test_gh_failure_is_not_absence_or_pass(tmp_path):
    f=fixture(tmp_path)
    def failure(_):raise RuntimeError('http403')
    f[3].get=failure
    with pytest.raises(ValueError):execute(f)


@pytest.mark.parametrize('field,value',[('org.opencontainers.image.revision','b'*40),
 ('org.opencontainers.image.version','0.2.1'),('org.opencontainers.image.source','https://github.com/other/private')])
def test_authentic_hashed_chain_with_wrong_label_still_refused(tmp_path,field,value):
    f=fixture(tmp_path,override_label=(field,value))
    with pytest.raises(ValueError,match='config_labels'):execute(f)


def test_missing_pagination_page_is_not_complete_ci_success(tmp_path):
    f=fixture(tmp_path);b='repos/'+a.REPO;first=f[3].values[b+'/actions/runs/123/attempts/1/jobs?per_page=100&page=1']
    first['jobs']=first['jobs'][:2]
    f[3].values[b+'/actions/runs/123/attempts/1/jobs?per_page=100&page=2']={'total_count':4,'jobs':[]}
    with pytest.raises(ValueError,match='pagination_incomplete'):execute(f)


def test_two_pages_preserve_all_required_jobs(tmp_path):
    f=fixture(tmp_path);b='repos/'+a.REPO;first=f[3].values[b+'/actions/runs/123/attempts/1/jobs?per_page=100&page=1']
    f[3].values[b+'/actions/runs/123/attempts/1/jobs?per_page=100&page=2']={'total_count':4,'jobs':first['jobs'][2:]}
    first['jobs']=first['jobs'][:2]
    assert execute(f)[0]['complete_seal_verified'] is True


def test_central_collectors_accept_authentic_raw_receipt_without_full_local_layers(tmp_path,monkeypatch):
    from quadringent.qualification import proof as native_proof
    monkeypatch.setattr(native_proof,'utc',lambda:NOW.isoformat())
    f=fixture(tmp_path);proof,raw=execute(f);root=f[1]
    (root/'passed.json').write_bytes(raw);admission=root/'runtime-admission.json'
    admission.write_text(json.dumps(proof))
    bindings=native_proof.release_bindings(root,root/'passed.json',SOURCE,admission)
    assert set(bindings)==set(a.COMPONENTS)
    assert all(set(b['variants'])=={'amd64','arm64'} for b in bindings.values())
    assert not any((root/c/'blobs/sha256'/sha(b'public-synthetic-layer')[7:]).exists() for c in a.COMPONENTS)


def test_only_explicit_authenticated_read_api_calls(monkeypatch):
    calls=[]
    def command(argv,**kwargs):
        calls.append(argv)
        return type('Result',(),{'returncode':0,'stdout':b'{}'})()
    monkeypatch.setattr(a.subprocess,'run',command)
    a.GitHub().get('repos/'+a.REPO+'/actions/artifacts/99')
    assert calls==[['gh','api','-X','GET','repos/'+a.REPO+'/actions/artifacts/99']]
    with pytest.raises(ValueError):a.GitHub().get('repos/other/private')


def test_output_private_exclusive_and_no_symlinks(tmp_path):
    target=tmp_path/'proof.json';a.write_private(target,b'private')
    assert target.stat().st_mode & 0o777==0o600
    with pytest.raises(FileExistsError):a.write_private(target,b'changed')
    link=tmp_path/'link';link.symlink_to(target)
    with pytest.raises(ValueError):a.write_private(link,b'changed')
    assert target.read_bytes()==b'private'


@pytest.mark.parametrize('change',['no_ci','pr','sha','branch','failed','queued','job_skipped','job_missing','job_duplicate','latest_failed','validate_step','ci_workflow_jobs'])
def test_canonical_push_main_ci_exact_seven_success_required(tmp_path,change):
    f=fixture(tmp_path);b='repos/'+a.REPO;v=f[3].values;ci=v[b+'/actions/runs/777'];jobs=v[b+'/actions/runs/777/attempts/1/jobs?per_page=100&page=1'];listing=v[b+'/actions/workflows/ci.yml/runs?head_sha='+SOURCE+'&branch=main&event=push&per_page=100&page=1']
    if change=='no_ci':listing.update(total_count=0,workflow_runs=[])
    elif change=='pr':ci['event']='pull_request'
    elif change=='sha':ci['head_sha']='b'*40
    elif change=='branch':ci['head_branch']='feature'
    elif change=='failed':ci['conclusion']='failure'
    elif change=='queued':ci['status']='queued'
    elif change=='job_skipped':jobs['jobs'][0]['conclusion']='skipped'
    elif change=='job_missing':jobs['jobs'].pop();jobs['total_count']=6
    elif change=='job_duplicate':jobs['jobs'][-1]['name']=jobs['jobs'][0]['name']
    elif change=='latest_failed':
        newer={**ci,'id':778,'conclusion':'failure'};listing['workflow_runs'].append(newer);listing['total_count']=2;v[b+'/actions/runs/778']=newer
    elif change=='validate_step':v[b+'/actions/runs/123/attempts/1/jobs?per_page=100&page=1']['jobs'][0]['steps']=[]
    else:v[b+'/contents/.github/workflows/ci.yml?ref='+SOURCE]['content']=base64.b64encode(b'jobs: {}').decode()
    with pytest.raises(ValueError):execute(f)


def test_authentic_hashed_chain_wrong_config_architecture_refused(tmp_path):
    f=fixture(tmp_path,config_arch='s390x')
    with pytest.raises(ValueError,match='config_labels_or_architecture'):execute(f)


def test_raw_receipt_authenticated_zip_is_not_rewritten_or_normalized(tmp_path):
    f=fixture(tmp_path);raw=json.dumps(json.loads(f[4]),indent=4).encode()+b'\n'
    f[6](raw);artifact=f[3].values['repos/'+a.REPO+'/actions/artifacts/99']
    artifact['digest']=sha(f[2].read_bytes());artifact['size_in_bytes']=f[2].stat().st_size
    proof,returned=execute(f)
    assert returned==raw and proof['passed_sha256']==hashlib.sha256(raw).hexdigest()


def test_metadata_artifact_required_and_exact_original_bytes(tmp_path):
    f = fixture(tmp_path)
    with pytest.raises(ValueError):
        a.admit(f[0], f[1], f[2], 99, f[3], NOW, metadata_archive=tmp_path / "absent.zip", metadata_artifact_id=100)


@pytest.mark.parametrize("change", ["run", "source", "expired", "date", "name", "zip_digest", "scan", "upload", "attempt_date"])
def test_metadata_artifact_identity_and_ci_steps_refuse_drift(tmp_path, change):
    f = fixture(tmp_path)
    artifact = f[3].values['repos/'+a.REPO+'/actions/artifacts/100']
    steps = f[3].values['repos/'+a.REPO+'/actions/runs/123/attempts/1/jobs?per_page=100&page=1']['jobs'][1]['steps']
    if change == "run": artifact['workflow_run']['id'] = 999
    elif change == "source": artifact['workflow_run']['head_sha'] = 'b'*40
    elif change == "expired": artifact['expired'] = True
    elif change == "date": artifact['expires_at'] = NOW.isoformat()
    elif change == "name": artifact['name'] = 'other'
    elif change == "zip_digest": artifact['digest'] = 'sha256:'+'0'*64
    elif change == "attempt_date": artifact['created_at'] = '2026-09-29T16:00:00Z'
    else:
        name = a.REQUIRED_STEPS[14] if change == "scan" else a.METADATA_UPLOAD_STEP
        next(step for step in steps if step['name'] == name)['conclusion'] = 'skipped'
    with pytest.raises(ValueError): execute(f)


@pytest.mark.parametrize("change", ["extra", "missing", "duplicate", "traversal", "symlink", "bytes", "oversized", "total"])
def test_even_authenticated_metadata_zip_is_confined_and_matches_receipt(tmp_path, change, monkeypatch):
    f = fixture(tmp_path)
    with zipfile.ZipFile(f[7]) as source:
        entries = [(entry.filename, source.read(entry)) for entry in source.infolist()]
    if change == "extra": entries.append(('capture/extra', b'public'))
    elif change == "missing": entries.pop()
    elif change == "duplicate": entries[-1] = entries[0]
    elif change == "traversal": entries[0] = ('../outside', entries[0][1])
    elif change == "bytes": entries[0] = (entries[0][0], b'{}')
    elif change == "oversized": monkeypatch.setattr(a, 'MAX_METADATA_BYTES', 1)
    elif change == "total": monkeypatch.setattr(a, 'MAX_METADATA_TOTAL_BYTES', 1)
    with zipfile.ZipFile(f[7], 'w') as target:
        for index, (name, raw) in enumerate(entries):
            if change == "symlink" and index == 0:
                item = zipfile.ZipInfo(name); item.external_attr = 0o120777 << 16
                target.writestr(item, raw)
            else: target.writestr(name, raw)
    artifact = f[3].values['repos/'+a.REPO+'/actions/artifacts/100']
    artifact.update(digest=sha(f[7].read_bytes()), size_in_bytes=f[7].stat().st_size)
    with pytest.raises(ValueError): execute(f)


def test_authenticated_metadata_materializes_originals_in_fresh_private_root(tmp_path):
    f = fixture(tmp_path); destination = tmp_path / 'fresh'
    proof, raw = a.admit(f[0], destination, f[2], 99, f[3], NOW,
                        metadata_archive=f[7], metadata_artifact_id=100)
    assert proof['metadata_files_compared'] == 21
    assert destination.stat().st_mode & 0o777 == 0o700
    assert proof['authenticated_ci']['metadata_artifact_id'] == 100
    for path in destination.rglob('*'):
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.read_bytes() == (f[1] / path.relative_to(destination)).read_bytes()
    assert raw == f[4] and not (destination / 'passed.json').exists()


def test_explicit_alternate_repository_and_public_visibility(tmp_path,monkeypatch):
    from dataclasses import replace
    repo='example-org/quadringent'
    f=list(fixture(tmp_path,repository=repo));f[0]=replace(f[0],private=False)
    base='repos/'+repo
    f[3].values[base]['private']=False
    f[3].values[base+'/actions/runs/123']['repository']['private']=False
    result=execute(f)[0]
    assert result['authenticated_ci']['repository']==repo
    assert result['expected_private'] is False


@pytest.mark.parametrize('target',['ci','release'])
def test_rerun_during_admission_is_not_blessed(tmp_path,target):
    f=fixture(tmp_path);original=f[3].get;counts={};run=777 if target=='ci' else 123
    endpoint='repos/'+a.REPO+f'/actions/runs/{run}'
    def get(path):
        counts[path]=counts.get(path,0)+1
        result=original(path)
        if path==endpoint and counts[path]>1:result.update(run_attempt=2,status='in_progress',conclusion=None)
        return result
    f[3].get=get
    with pytest.raises(ValueError):execute(f)


def test_rerun_after_archive_and_metadata_validation_is_refused(tmp_path,monkeypatch):
    f=fixture(tmp_path);original=a.metadata_bindings
    def validated(*args,**kwargs):
        result=original(*args,**kwargs)
        f[3].values['repos/'+a.REPO+'/actions/runs/123'].update(run_attempt=2,status='in_progress',conclusion=None)
        return result
    monkeypatch.setattr(a,'metadata_bindings',validated)
    with pytest.raises(ValueError):execute(f)
