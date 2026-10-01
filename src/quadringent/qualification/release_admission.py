"""Admission depuis preuves CI authentifiées, sans scan local ni reçu fabriqué."""
from dataclasses import dataclass

REPO = 'cdcforge/quadringent-community'
COMPONENTS = ('capture', 'control-plane', 'verifier')
REQUIRED_STEPS = (
    'Image de capture (multi-architecture)', 'Image control-plane', 'Image verifier',
    'Vérifier les six variantes locales et leurs digests exacts',
    'Scan Trivy — capture', 'Scan Trivy — control-plane', 'Scan Trivy — verifier',
    'Scan arm64 et secrets des variantes et de toutes leurs couches',
    'SBOM SPDX — capture', 'SBOM SPDX — control-plane', 'SBOM SPDX — verifier',
    'SBOM SPDX — variantes arm64', 'Sceller les six scans avant toute authentification de publication',
    'Exporter les 21 métadonnées OCI originales scellées',
    'Scanner les secrets des métadonnées OCI exportées',
    'Promouvoir exactement les trois index OCI scannés', 'Signer les digests publiés',
)


@dataclass(frozen=True)
class Expected:
    run_id: int
    attempt: int
    source: str
    tag: str
    workflow_sha256: str
    repository: str = REPO
    private: bool = True

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import zipfile

import yaml

MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_ZIP_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_METADATA_TOTAL_BYTES = 42 * 1024 * 1024
MAX_METADATA_ZIP_BYTES = 48 * 1024 * 1024
METADATA_UPLOAD_STEP = "Conserver temporairement les métadonnées OCI originales"
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
HEX = re.compile(r'[0-9a-f]{64}\Z')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def document(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def date(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'date_without_timezone')
    return parsed.astimezone(timezone.utc)


def read_regular(path, maximum):
    path = Path(path)
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'local_symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= maximum, 'local_file_size_or_type')
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        require(len(raw) <= maximum and (before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'file_changed_during_read')
    return raw


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


class GitHub:
    """Lecture uniquement, via la session gh native ; aucune trace d'en-têtes."""
    def __init__(self, repository=REPO):
        self.repository = repository
        self.deadline = time.monotonic()+180

    def get(self, endpoint):
        require(endpoint == 'repos/' + self.repository or endpoint.startswith('repos/' + self.repository + '/'), 'api_scope')
        remaining=min(30,self.deadline-time.monotonic())
        require(remaining>0,'github_observation_deadline')
        result = subprocess.run(['gh', 'api', '-X', 'GET', endpoint], capture_output=True,
                                timeout=remaining, check=False)
        require(result.returncode == 0, 'github_api_failure')
        return document(result.stdout)


def receipt_bytes(archive, artifact):
    raw = read_regular(archive, MAX_ZIP_BYTES)
    require(artifact.get('digest') == digest(raw) and artifact.get('size_in_bytes') == len(raw),
            'authenticated_archive_digest_or_size')
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        entries = source.infolist()
        require(len(entries) == 1 and entries[0].filename == 'passed.json', 'receipt_archive_members')
        entry = entries[0]
        mode = entry.external_attr >> 16
        require(not entry.is_dir() and not stat.S_ISLNK(mode) and not entry.flag_bits & 1
                and entry.file_size <= MAX_RECEIPT_BYTES, 'receipt_archive_type_or_size')
        raw = source.read(entry)
    require(len(raw) <= MAX_RECEIPT_BYTES, 'receipt_size')
    receipt = document(raw)
    require(set(receipt) == {'files'} and isinstance(receipt['files'], dict)
            and 0 < len(receipt['files']) <= 250000, 'receipt_shape')
    directories = set()
    for name, value in receipt['files'].items():
        require(isinstance(name, str) and isinstance(value, str) and DIGEST.fullmatch(value), 'receipt_digest')
        parts = name.split('/')
        require(len(parts) >= 3 and len(name) <= 260 and parts[0] in COMPONENTS, 'receipt_path')
        component, directory, relative = parts[0], parts[1], '/'.join(parts[2:])
        require(directory in {component, component + '-amd64', component + '-arm64', component + '-secret-files'},
                'receipt_directory')
        if directory.endswith('-secret-files'):
            neutral = (relative == 'layout-index.json' or
                       re.fullmatch(r'[0-9a-f]{64}\.(index|manifest|config)\.json', relative) or
                       re.fullmatch(r'[0-9a-f]{64}/(metadata-[0-9]{6}\.jsonl|[0-9]{6}\.payload\.txt)', relative))
        else:
            neutral = relative in {'index.json', 'oci-layout'} or re.fullmatch(r'blobs/sha256/[0-9a-f]{64}', relative)
        require(neutral, 'non_neutral_receipt_path')
        directories.add((component, directory))
    require(directories == {(c, c + suffix) for c in COMPONENTS for suffix in ('', '-amd64', '-arm64', '-secret-files')},
            'receipt_variant_or_layers_missing')
    return raw, receipt['files']


CI_JOBS = {'tests','quadringent-product-gate','java','chart','cockpit-runtime','verifier-runtime','image-runtime'}
CI_GATE_STEP = 'Valider les sept contrôles CI du commit exact'


def ci_evidence(expected, repo, client, validate_finished):
    base = 'repos/' + expected.repository
    identity = client.get(base + '/actions/workflows/ci.yml')
    require(identity.get('path') == '.github/workflows/ci.yml' and identity.get('state') == 'active', 'canonical_ci_workflow')
    content = client.get(base + '/contents/.github/workflows/ci.yml?ref=' + expected.source)
    require(content.get('encoding') == 'base64', 'ci_workflow_encoding')
    raw = base64.b64decode(''.join(content['content'].split()),validate=True)
    require(set(yaml.safe_load(raw)['jobs']) == CI_JOBS, 'canonical_ci_seven_jobs_source')
    runs, total, page = [], None, 1
    while True:
        listing = client.get(base + '/actions/workflows/ci.yml/runs?head_sha=' + expected.source +
                             f'&branch=main&event=push&per_page=100&page={page}')
        require(isinstance(listing.get('total_count'),int) and isinstance(listing.get('workflow_runs'),list), 'ci_runs_pagination')
        if total is None:total=listing['total_count']
        require(total == listing['total_count'] and 0 < total <= 1000, 'ci_run_absent_or_ambiguous')
        runs += listing['workflow_runs']
        if len(runs) >= total:break
        require(listing['workflow_runs'], 'ci_runs_incomplete');page += 1
    require(len(runs)==total and len({r['id'] for r in runs})==total, 'ci_run_duplicates')
    run_id=max(r['id'] for r in runs)
    ci=client.get(base+f'/actions/runs/{run_id}')
    require(ci.get('id')==run_id and ci.get('workflow_id')==identity['id']
            and ci.get('event')=='push' and ci.get('head_branch')=='main' and ci.get('head_sha')==expected.source
            and str(ci.get('path','')).split('@')[0]=='.github/workflows/ci.yml'
            and ci.get('status')=='completed' and ci.get('conclusion')=='success'
            and ci.get('repository',{}).get('id')==repo['id'] and ci.get('repository',{}).get('full_name')==expected.repository
            and ci.get('head_repository',{}).get('id')==repo['id'], 'exact_main_push_ci_not_success')
    attempt=ci.get('run_attempt');require(isinstance(attempt,int) and attempt>0, 'ci_attempt')
    jobs,total,page=[],None,1
    while True:
        listing=client.get(base+f'/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')
        require(isinstance(listing.get('total_count'),int) and isinstance(listing.get('jobs'),list), 'ci_jobs_pagination')
        if total is None:total=listing['total_count']
        require(total==listing['total_count'] and total==7, 'ci_requires_exact_seven_jobs')
        jobs+=listing['jobs']
        if len(jobs)>=total:break
        require(listing['jobs'], 'ci_jobs_missing');page+=1
    require(len(jobs)==7 and len({j['id'] for j in jobs})==7 and {j.get('name') for j in jobs}==CI_JOBS, 'ci_jobs_missing_or_duplicate')
    for job in jobs:
        require(job.get('run_id')==run_id and job.get('run_attempt')==attempt
                and job.get('status')=='completed' and job.get('conclusion')=='success', 'ci_job_not_success')
        require(date(job['started_at'])<=date(job['completed_at'])<=validate_finished, 'ci_completed_after_release_validation')
    refreshed=client.get(base+f'/actions/runs/{run_id}')
    require(all(refreshed.get(k)==ci.get(k) for k in ('id','run_attempt','workflow_id','event','head_branch','head_sha','path','status','conclusion')),
            'ci_changed_during_admission')
    return {'run_id':run_id,'run_attempt':attempt,'source_sha':expected.source,'event':'push','branch':'main',
            'workflow_sha256':hashlib.sha256(raw).hexdigest(),'seven_jobs_success':sorted(CI_JOBS)}


def authenticated_evidence(expected, artifact_id, metadata_artifact_id, client, now):
    require(isinstance(expected.run_id, int) and expected.run_id > 0 and expected.attempt > 0 and artifact_id > 0
            and metadata_artifact_id > 0 and metadata_artifact_id != artifact_id,
            'positive_identifiers')
    require(re.fullmatch(r'[0-9a-f]{40}', expected.source) and HEX.fullmatch(expected.workflow_sha256)
            and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', expected.tag), 'explicit_source_version_workflow')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', expected.repository) and type(expected.private) is bool, 'explicit_repository_visibility')
    base = 'repos/' + expected.repository
    repo = client.get(base)
    require(repo.get('full_name') == expected.repository and repo.get('private') is expected.private, 'repository_not_expected_private')
    run = client.get(base + f'/actions/runs/{expected.run_id}')
    require(run.get('id') == expected.run_id and run.get('run_attempt') == expected.attempt
            and run.get('head_sha') == expected.source and run.get('head_branch') == expected.tag
            and run.get('event') == 'workflow_dispatch'
            and str(run.get('path', '')).split('@')[0] == '.github/workflows/release.yml'
            and run.get('status') == 'completed' and run.get('conclusion') == 'success', 'run_identity_or_success')
    require(run.get('repository', {}).get('full_name') == expected.repository and run['repository'].get('id') == repo['id']
            and run['repository'].get('private') is expected.private
            and run.get('head_repository', {}).get('id') == repo['id'], 'run_repository')
    ref = client.get(base + '/git/ref/tags/' + expected.tag)
    require(ref.get('ref') == 'refs/tags/' + expected.tag, 'tag_ref')
    target = ref['object']
    for _ in range(4):
        if target['type'] != 'tag': break
        target = client.get(base + '/git/tags/' + target['sha'])['object']
    require(target.get('type') == 'commit' and target.get('sha') == expected.source, 'tag_source_drift')
    release = client.get(base + '/releases/tags/' + expected.tag)
    require(release.get('tag_name') == expected.tag and isinstance(release.get('id'), int), 'release_identity')
    content = client.get(base + '/contents/.github/workflows/release.yml?ref=' + expected.source)
    require(content.get('encoding') == 'base64', 'workflow_encoding')
    workflow_raw = base64.b64decode(''.join(content['content'].split()), validate=True)
    require(hashlib.sha256(workflow_raw).hexdigest() == expected.workflow_sha256, 'reviewed_workflow_drift')
    workflow = yaml.safe_load(workflow_raw)
    image_steps = workflow['jobs']['images']['steps']
    uploads = [(i, s) for i, s in enumerate(image_steps) if s.get('with', {}).get('name') == 'oci-scan-receipt']
    require(len(uploads) == 1, 'single_receipt_upload')
    position, upload = uploads[0]
    require(upload.get('uses') == 'actions/upload-artifact@v4' and isinstance(upload.get('name'), str)
            and upload['with'].get('path') == '${{ runner.temp }}/oci/passed.json'
            and upload['with'].get('retention-days') == 1 and upload['with'].get('if-no-files-found') == 'error'
            and 'if' not in upload and not upload.get('continue-on-error') and not upload['with'].get('overwrite'),
            'receipt_upload_contract')
    seal_pos = next(i for i, s in enumerate(image_steps) if 'release_oci.py seal' in s.get('run', ''))
    login_pos = next(i for i, s in enumerate(image_steps) if s.get('uses', '').startswith('docker/login-action@'))
    require(seal_pos < position < login_pos, 'receipt_upload_position')
    metadata_uploads = [(i, step) for i, step in enumerate(image_steps)
                        if step.get('with', {}).get('name') == 'oci-admission-metadata']
    require(len(metadata_uploads) == 1, 'single_metadata_upload')
    metadata_position, metadata_upload = metadata_uploads[0]
    require(metadata_upload.get('name') == METADATA_UPLOAD_STEP
            and metadata_upload.get('uses') == 'actions/upload-artifact@v4'
            and metadata_upload['with'].get('path') == '${{ runner.temp }}/oci-admission-metadata/'
            and metadata_upload['with'].get('retention-days') == 1
            and metadata_upload['with'].get('if-no-files-found') == 'error'
            and 'if' not in metadata_upload and not metadata_upload.get('continue-on-error')
            and not metadata_upload['with'].get('overwrite'), 'metadata_upload_contract')
    export_positions = [i for i, step in enumerate(image_steps) if step.get('name') == REQUIRED_STEPS[13]]
    scan_positions = [i for i, step in enumerate(image_steps) if step.get('name') == REQUIRED_STEPS[14]]
    require(len(export_positions) == len(scan_positions) == 1
            and seal_pos < export_positions[0] < scan_positions[0] < metadata_position < login_pos,
            'metadata_export_scan_upload_order')
    jobs, total, page = [], None, 1
    while True:
        listing = client.get(base + f'/actions/runs/{expected.run_id}/attempts/{expected.attempt}/jobs?per_page=100&page={page}')
        require(isinstance(listing.get('total_count'), int) and isinstance(listing.get('jobs'), list), 'jobs_pagination_shape')
        if total is None: total = listing['total_count']
        require(total == listing['total_count'] and 0 < total <= 2000, 'jobs_pagination_count')
        jobs += listing['jobs']
        if len(jobs) >= total: break
        require(listing['jobs'], 'jobs_pagination_incomplete'); page += 1
    require(len(jobs) == total and len({j['id'] for j in jobs}) == total, 'jobs_missing_or_duplicate')
    required = {}
    for name in ('validate', 'images', 'chart', 'release'):
        matching = [j for j in jobs if j.get('name') == name]
        require(len(matching) == 1, 'required_job_missing_or_duplicate')
        job = matching[0]
        require(job.get('run_id') == expected.run_id and job.get('run_attempt') == expected.attempt
                and job.get('status') == 'completed' and job.get('conclusion') == 'success', 'job_identity_or_success')
        require(date(job['started_at']) <= date(job['completed_at']) <= now, 'job_dates')
        required[name] = job
    validation_steps = [step for step in required['validate']['steps'] if step.get('name') == CI_GATE_STEP]
    require(len(validation_steps) == 1 and validation_steps[0].get('status') == 'completed'
            and validation_steps[0].get('conclusion') == 'success', 'canonical_ci_validation_step')
    ci = ci_evidence(expected, repo, client, date(validation_steps[0]['completed_at']))
    job = required['images']; observed = {}
    for name in [*REQUIRED_STEPS, upload['name'], METADATA_UPLOAD_STEP]:
        matches = [s for s in job['steps'] if s.get('name') == name]
        require(len(matches) == 1, 'scan_or_seal_step_missing_or_duplicate')
        step = matches[0]
        require(step.get('status') == 'completed' and step.get('conclusion') == 'success', 'required_step_not_success')
        require(date(job['started_at']) <= date(step['started_at']) <= date(step['completed_at']) <= date(job['completed_at']),
                'step_dates')
        observed[name] = step
    ordered = [observed[name]['number'] for name in REQUIRED_STEPS]
    require(ordered == sorted(set(ordered)), 'required_step_order')
    seal = observed[REQUIRED_STEPS[12]]; exported = observed[upload['name']]
    require(seal['number'] < exported['number'] < observed['Promouvoir exactement les trois index OCI scannés']['number']
            and date(seal['completed_at']) <= date(exported['started_at']), 'seal_upload_order')
    require(observed[REQUIRED_STEPS[14]]['number'] < observed[METADATA_UPLOAD_STEP]['number']
            < observed['Promouvoir exactement les trois index OCI scannés']['number']
            and date(observed[REQUIRED_STEPS[14]]['completed_at']) <= date(observed[METADATA_UPLOAD_STEP]['started_at']),
            'metadata_scan_upload_order')
    artifact = client.get(base + f'/actions/artifacts/{artifact_id}')
    link = artifact.get('workflow_run', {})
    require(artifact.get('id') == artifact_id and artifact.get('name') == 'oci-scan-receipt'
            and artifact.get('expired') is False and link.get('id') == expected.run_id
            and link.get('head_sha') == expected.source and link.get('repository_id') == repo['id']
            and link.get('head_repository_id') == repo['id'], 'artifact_identity_or_expiry')
    require(date(exported['started_at']) <= date(artifact['created_at']) <= date(exported['completed_at']),
            'artifact_not_from_current_attempt_upload')
    require(now < date(artifact['expires_at']), 'artifact_expired_date')
    metadata_artifact = client.get(base + f'/actions/artifacts/{metadata_artifact_id}')
    metadata_link = metadata_artifact.get('workflow_run', {})
    require(metadata_artifact.get('id') == metadata_artifact_id
            and metadata_artifact.get('name') == 'oci-admission-metadata'
            and metadata_artifact.get('expired') is False and metadata_link.get('id') == expected.run_id
            and metadata_link.get('head_sha') == expected.source and metadata_link.get('repository_id') == repo['id']
            and metadata_link.get('head_repository_id') == repo['id'], 'metadata_artifact_identity_or_expiry')
    metadata_exported = observed[METADATA_UPLOAD_STEP]
    require(date(metadata_exported['started_at']) <= date(metadata_artifact['created_at'])
            <= date(metadata_exported['completed_at']), 'metadata_artifact_not_current_attempt')
    require(now < date(metadata_artifact['expires_at']), 'metadata_artifact_expired_date')
    refreshed=client.get(base+f'/actions/runs/{expected.run_id}')
    require(all(refreshed.get(k)==run.get(k) for k in ('id','run_attempt','head_sha','head_branch','event','path','status','conclusion','repository','head_repository')),
            'release_changed_during_admission')
    return artifact, metadata_artifact, {'repository': expected.repository, 'run_id': expected.run_id, 'run_attempt': expected.attempt,
                      'workflow_sha256': expected.workflow_sha256, 'artifact_id': artifact_id,
                      'artifact_zip_digest': artifact['digest'], 'release_id': release['id'],
                      'metadata_artifact_id': metadata_artifact_id,
                      'metadata_artifact_zip_digest': metadata_artifact['digest'],
                      'required_steps_success': list(observed), 'canonical_ci': ci}


def metadata_bindings(expected, root, files):
    root = Path(root); indexes = {}; verified = 0
    for component in COMPONENTS:
        directory = root / component; allowed = set()
        def checked(relative):
            nonlocal verified
            raw = read_regular(directory / relative, MAX_METADATA_BYTES)
            require(files.get(f'{component}/{component}/{relative}') == digest(raw), 'metadata_receipt_drift')
            allowed.add(relative); verified += 1
            return document(raw)
        def blob(descriptor):
            require(DIGEST.fullmatch(descriptor.get('digest', '')) and isinstance(descriptor.get('size'), int), 'descriptor')
            relative = 'blobs/sha256/' + descriptor['digest'][7:]
            value = checked(relative);raw = read_regular(directory / relative, MAX_METADATA_BYTES)
            require(digest(raw) == descriptor['digest'] and len(raw) == descriptor['size'], 'descriptor_bytes')
            return value
        require(checked('oci-layout') == {'imageLayoutVersion': '1.0.0'}, 'oci_layout')
        top = checked('index.json');require(top.get('schemaVersion') == 2 and len(top['manifests']) == 1, 'top_index')
        descriptor = top['manifests'][0];index = blob(descriptor)
        require(descriptor.get('mediaType') == 'application/vnd.oci.image.index.v1+json'
                and index.get('schemaVersion') == 2 and len(index['manifests']) == 2, 'two_variants_index')
        indexes[component] = descriptor['digest'];variants = set()
        for item in index['manifests']:
            platform = item['platform'];arch = platform['architecture']
            require(platform.get('os') == 'linux' and arch in {'amd64','arm64'} and arch not in variants, 'variant_architecture')
            variants.add(arch);manifest = blob(item)
            require(item.get('mediaType') == 'application/vnd.oci.image.manifest.v1+json'
                    and manifest.get('schemaVersion') == 2, 'manifest_type')
            config_descriptor = manifest['config'];config = blob(config_descriptor)
            labels = config.get('config', {}).get('Labels', {})
            require(config_descriptor.get('mediaType') == 'application/vnd.oci.image.config.v1+json'
                    and config.get('os') == 'linux' and config.get('architecture') == arch
                    and labels.get('org.opencontainers.image.revision') == expected.source
                    and labels.get('org.opencontainers.image.version') == expected.tag[1:]
                    and labels.get('org.opencontainers.image.source') == 'https://github.com/' + expected.repository, 'config_labels_or_architecture')
            for referenced in [item, config_descriptor, *manifest['layers']]:
                require(DIGEST.fullmatch(referenced.get('digest', '')), 'referenced_blob_digest')
                relative = 'blobs/sha256/' + referenced['digest'][7:]
                require(files.get(f'{component}/{component}/{relative}') == referenced['digest']
                        and files.get(f'{component}/{component}-{arch}/{relative}') == referenced['digest'],
                        'scan_view_or_layer_reference_drift')
        require(variants == {'amd64','arm64'}, 'both_architectures')
        found = set()
        for path in directory.rglob('*'):
            require(not path.is_symlink(), 'metadata_directory_symlink')
            if path.is_file():found.add(path.relative_to(directory).as_posix())
            else:require(path.is_dir(), 'metadata_special_file')
        require(found == allowed, 'metadata_only_layout_required')
    return indexes, verified


def metadata_archive_bytes(archive, artifact, files):
    raw = read_regular(archive, MAX_METADATA_ZIP_BYTES)
    require(artifact.get('digest') == digest(raw) and artifact.get('size_in_bytes') == len(raw),
            'authenticated_metadata_archive_digest_or_size')
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        entries = source.infolist()
        require(len(entries) == 21 and len({entry.filename for entry in entries}) == 21,
                'metadata_archive_exact_21_unique_files')
        require(sum(entry.file_size for entry in entries) <= MAX_METADATA_TOTAL_BYTES, 'metadata_archive_total_size')
        for entry in entries:
            mode = entry.external_attr >> 16
            require(not entry.is_dir() and stat.S_IFMT(mode) in {0, stat.S_IFREG}
                    and not entry.flag_bits & 1 and entry.file_size <= MAX_METADATA_BYTES,
                    'metadata_archive_file_type_or_size')
            require(re.fullmatch(r'(capture|control-plane|verifier)/(index\.json|oci-layout|blobs/sha256/[0-9a-f]{64})',
                                 entry.filename), 'metadata_archive_neutral_path')
            content = source.read(entry)
            component = entry.filename.split('/')[0]
            require(files.get(component + '/' + entry.filename) == digest(content), 'metadata_archive_receipt_drift')
            result[entry.filename] = content
    return result


def materialize_metadata(root, originals, *, receipt_raw=None):
    root = Path(root)
    require(not any(p.is_symlink() for p in [root, *root.parents]), 'metadata_output_symlink')
    if root.exists():
        require(root.is_dir(), 'metadata_output_type')
        found = set()
        for path in root.rglob('*'):
            require(not path.is_symlink(), 'metadata_directory_symlink')
            if path == root/'passed.json':
                require(receipt_raw is not None and read_regular(path,MAX_RECEIPT_BYTES)==receipt_raw,'existing_receipt_not_authenticated')
            elif path.is_file(): found.add(path.relative_to(root).as_posix())
            else: require(path.is_dir(), 'metadata_special_file')
        require(found == set(originals), 'metadata_only_authenticated_files_required')
        for name, raw in originals.items():
            require(read_regular(root/name, MAX_METADATA_BYTES) == raw, 'local_metadata_archive_drift')
    else:
        root.mkdir(mode=0o700)
        for name, raw in originals.items():
            path = root/name
            for parent in reversed(path.parents):
                if parent == root or parent.is_relative_to(root): parent.mkdir(mode=0o700, exist_ok=True)
            write_private(path, raw)


def admit(expected, root, archive, artifact_id, client, now, *, metadata_archive, metadata_artifact_id):
    try:
        artifact, metadata_artifact, evidence = authenticated_evidence(expected, artifact_id, metadata_artifact_id, client, now)
        raw, files = receipt_bytes(archive, artifact)
        originals = metadata_archive_bytes(metadata_archive, metadata_artifact, files)
        materialize_metadata(root, originals, receipt_raw=raw)
        indexes, count = metadata_bindings(expected, root, files)
        # Les lectures locales peuvent durer : réobserve les deux attempts après les ZIP/métadonnées.
        ci=evidence['canonical_ci']
        for run_id,attempt,branch,event,path in (
                (expected.run_id,expected.attempt,expected.tag,'workflow_dispatch','.github/workflows/release.yml'),
                (ci['run_id'],ci['run_attempt'],'main','push','.github/workflows/ci.yml')):
            latest=client.get('repos/'+expected.repository+f'/actions/runs/{run_id}')
            require(latest.get('id')==run_id and latest.get('run_attempt')==attempt
                    and latest.get('head_sha')==expected.source and latest.get('head_branch')==branch
                    and latest.get('event')==event and str(latest.get('path','')).split('@')[0]==path
                    and latest.get('repository',{}).get('full_name')==expected.repository
                    and latest.get('status')=='completed' and latest.get('conclusion')=='success',
                    'attempt_changed_after_metadata_verification')
        proof = {'kind':'quadringent-oci-runtime-admission','verified_before_cloud':False,
                 'complete_seal_verified':True,'seal_verification_location':'github-actions',
                 'local_verification_scope':'indexes_manifests_configs_only','local_full_fingerprint_verified':False,
                 'repository':expected.repository,'expected_private':expected.private,'metadata_files_compared':count,'revision':expected.source,'version':expected.tag[1:],
                 'tag':expected.tag,'passed_sha256':hashlib.sha256(raw).hexdigest(),'indexes':indexes,
                 'admitted_utc':now.isoformat(),'authenticated_ci':evidence}
        return proof, raw
    except (OSError, KeyError, TypeError, AttributeError, IndexError, RuntimeError, StopIteration, zipfile.BadZipFile, yaml.YAMLError) as error:
        raise ValueError('admission_evidence_unavailable_or_invalid') from error


def write_private(path, raw):
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'output_symlink')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:stream.write(raw)
