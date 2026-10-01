"""Le préflight refuse une vulnérabilité connue et un scan vide ou partiel."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_release_dependencies.py'


def helper():
    spec = importlib.util.spec_from_file_location('release_dependencies', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock(version):
    return f'urllib3=={version} \\\n    --hash=sha256:{"a" * 64}\n# via requests\nrequests==2.34.2 \\\n    --hash=sha256:{"b" * 64}\n'.encode()


def fixture(tmp_path, version='2.8.0'):
    module = helper()
    source = tmp_path / 'source'
    (source / 'docker').mkdir(parents=True)
    for component in ('control-plane', 'verifier'):
        (source / 'docker' / f'{component}-requirements.txt').write_bytes(lock(version))
    root = tmp_path / 'private'
    module.prepare(root, source)
    report = {'SchemaVersion': 2, 'ArtifactType': 'filesystem', 'Results': [
        {'Target': f'{component}/requirements.txt', 'Class': 'lang-pkgs', 'Type': 'pip',
         'Packages': [{'Name': 'urllib3', 'Version': version}, {'Name': 'requests', 'Version': '2.34.2'}]}
        for component in ('control-plane', 'verifier')]}
    return module, root, report


def save(root, report):
    (root / 'report.json').write_text(json.dumps(report))


def test_release_dependency_scan_is_a_validate_gate():
    jobs = yaml.safe_load(Path('.github/workflows/release.yml').read_text())['jobs']
    validate = jobs['validate']
    scans = [step for step in validate['steps'] if step.get('with', {}).get('scan-type') == 'fs']
    assert len(scans) == 1, 'aucun scan précoce avant les six builds OCI'
    scan = scans[0]
    assert scan['uses'] == 'aquasecurity/trivy-action@v0.36.0'
    assert scan['with']['scanners'] == 'vuln'
    assert scan['with']['severity'] == 'HIGH,CRITICAL'
    assert scan['with']['ignore-unfixed'] is True
    assert scan['with']['exit-code'] == '1'
    assert scan['with']['list-all-pkgs'] is True
    assert scan['with']['format'] == 'json'
    assert scan['with']['scan-ref'].endswith('/release-dependencies/locks')
    assert scan['with']['output'].startswith('${{ runner.temp }}/')
    assert jobs['images']['needs'] == ['validate']
    assert validate['timeout-minutes'] == 5
    checking = [step for step in validate['steps'] if 'check_release_dependencies.py check' in step.get('run', '')]
    assert len(checking) == 1 and 'always()' in checking[0]['if']
    assert validate['steps'].index(checking[0]) > validate['steps'].index(scan)
    assert not any('upload-artifact' in step.get('uses', '') for step in validate['steps'])


def test_old_urllib3_findings_stop_the_gate(tmp_path):
    module, root, report = fixture(tmp_path, '2.7.0')
    report['Results'][0]['Vulnerabilities'] = [
        {'VulnerabilityID': f'CVE-2026-TEST-{number}', 'PkgName': 'urllib3', 'InstalledVersion': '2.7.0',
         'FixedVersion': '2.8.0', 'Severity': 'HIGH'} for number in (1, 2)]
    save(root, report)
    with pytest.raises(ValueError, match='vulnérabilités'):
        module.check(root)
    result = subprocess.run([sys.executable, str(SCRIPT), 'check', '--root', str(root)], capture_output=True)
    assert result.returncode == 1
    assert b'CVE-2026-TEST' not in result.stdout + result.stderr


def test_new_urllib3_exact_native_inventory_passes(tmp_path):
    module, root, report = fixture(tmp_path)
    save(root, report)
    assert module.check(root) == {'control-plane': 2, 'verifier': 2}
    for component in ('control-plane', 'verifier'):
        assert (root / 'locks' / component / 'requirements.txt').read_bytes() == lock('2.8.0')
        assert (root / 'locks' / component / 'requirements.txt').stat().st_mode & 0o777 == 0o600
    assert (root / 'report.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('change', ['missing-report', 'missing-result', 'empty', 'package-missing', 'extra-package',
                                  'wrong-version', 'duplicate-result', 'duplicate-package', 'wrong-analyzer',
                                  'wrong-class', 'wrong-target', 'wrong-schema', 'wrong-artifact', 'bad-json', 'changed-lock'])
def test_incomplete_or_unexpected_scan_is_not_a_pass(tmp_path, change):
    module, root, report = fixture(tmp_path)
    if change == 'missing-result': report['Results'].pop()
    if change == 'empty': report['Results'] = []
    if change == 'package-missing': report['Results'][0]['Packages'].pop()
    if change == 'extra-package': report['Results'][0]['Packages'].append({'Name': 'unexpected', 'Version': '1.0'})
    if change == 'wrong-version': report['Results'][0]['Packages'][0]['Version'] = '2.7.0'
    if change == 'duplicate-result': report['Results'].append(deepcopy(report['Results'][0]))
    if change == 'duplicate-package': report['Results'][0]['Packages'].append(deepcopy(report['Results'][0]['Packages'][0]))
    if change == 'wrong-analyzer': report['Results'][0]['Type'] = 'python-pkg'
    if change == 'wrong-class': report['Results'][0]['Class'] = 'os-pkgs'
    if change == 'wrong-target': report['Results'][0]['Target'] = '../requirements.txt'
    if change == 'wrong-schema': report['SchemaVersion'] = 3
    if change == 'wrong-artifact': report['ArtifactType'] = 'container_image'
    if change != 'missing-report': save(root, report)
    if change == 'bad-json': (root / 'report.json').write_text('{')
    if change == 'changed-lock': (root / 'locks/control-plane/requirements.txt').write_bytes(lock('2.7.0'))
    with pytest.raises((ValueError, OSError)): module.check(root)


@pytest.mark.parametrize('content', [b'', b'urllib3>=2.8.0\n', b'urllib3==2.8.0\n',
                                    lock('2.8.0') + lock('2.8.0'), b'-r other.txt\n'])
def test_unpinned_unhashed_or_ambiguous_lock_is_refused(content):
    with pytest.raises(ValueError): helper().locked_packages(content)
