"""Les économies CI préservent les tests des images et les permissions minimales."""
from pathlib import Path

import yaml


def workflow():
    return yaml.safe_load(Path('.github/workflows/ci.yml').read_text())


def test_concurrency_cancels_only_the_same_workflow_and_ref():
    ci = workflow()
    assert ci['concurrency'] == {
        'group': 'ci-${{ github.workflow }}-${{ github.ref }}',
        'cancel-in-progress': True,
    }
    assert ci['permissions'] == {'contents': 'read'}
    assert set(ci['jobs']) == {
        'tests', 'java', 'chart', 'image-runtime', 'verifier-runtime',
        'cockpit-runtime', 'quadringent-product-gate',
    }
    assert ci['jobs']['quadringent-product-gate']['needs'] == [
        'image-runtime', 'verifier-runtime', 'cockpit-runtime',
    ]
    for job in ci['jobs'].values():
        assert 'continue-on-error' not in job
        assert job['if'] == (
            "${{ success() && (!(github.event.repository.private && vars.PRIVATE_RUNNER_LABELS != '') || "
            "(github.event_name == 'push' && github.ref == 'refs/heads/main' && "
            "github.workflow_ref == format('{0}/.github/workflows/ci.yml@refs/heads/main', github.repository) && "
            "github.sha == vars.PRIVATE_APPROVED_SHA)) }}"
        )
        assert job.get('permissions', ci['permissions']) == {'contents': 'read'}
        assert all('continue-on-error' not in step for step in job['steps'])


def test_dependency_caches_do_not_replace_installations_or_java_contracts():
    jobs = workflow()['jobs']
    setup = next(s for s in jobs['tests']['steps'] if s.get('uses', '').startswith('actions/setup-python@'))
    assert setup['with']['cache'] == 'pip'
    assert set(setup['with']['cache-dependency-path'].splitlines()) == {'pyproject.toml', 'requirements.txt'}
    assert any("python -m pip install -e '.[dev,api]'" in s.get('run', '') for s in jobs['tests']['steps'])
    java = next(s for s in jobs['java']['steps'] if s.get('uses', '').startswith('actions/setup-java@'))
    assert java['with']['cache'] == 'maven'
    assert java['with']['cache-dependency-path'] == 'java/pom.xml'
    test_java = next(s for s in jobs['tests']['steps'] if s.get('uses', '').startswith('actions/setup-java@'))
    assert test_java['with'] == {'distribution': 'temurin', 'java-version': '21'}
    assert any(s.get('run') == 'sh scripts/test_java_all.sh' for s in jobs['java']['steps'])


def test_cached_images_are_loaded_from_checkout_and_really_smoked():
    ci = workflow()
    assert ci['env']['DOCKER_BUILD_RECORD_UPLOAD'] == 'false'
    scopes = set()
    for name, dockerfile, image in (
        ('image-runtime', 'docker/Dockerfile', 'CDC_IMAGE'),
        ('verifier-runtime', 'docker/verifier.Dockerfile', 'VERIFIER_IMAGE'),
        ('cockpit-runtime', 'docker/control-plane.Dockerfile', 'COCKPIT_IMAGE'),
    ):
        steps = ci['jobs'][name]['steps']
        buildx = next(s for s in steps if s.get('uses', '').startswith('docker/setup-buildx-action@'))
        builds = [s for s in steps if s.get('uses', '').startswith('docker/build-push-action@')]
        assert len(builds) == 1
        build = builds[0]
        assert steps.index(buildx) < steps.index(build)
        settings = build['with']
        assert settings['context'] == '.' and settings['file'] == dockerfile
        assert settings['load'] is True and settings['push'] is False
        assert settings['platforms'] == 'linux/amd64'
        assert settings['tags'] == '${{ env.' + image + ' }}'
        cache = dict(p.split('=', 1) for p in settings['cache-from'].split(','))
        assert cache['type'] == 'gha' and cache['version'] == '2'
        assert '${{' not in cache['scope'] and cache['scope'] not in scopes
        scopes.add(cache['scope'])
        export = settings['cache-to']
        assert "github.event_name != 'pull_request'" in export
        assert f"scope={cache['scope']}" in export and 'mode=min' in export
        assert 'ignore-error=true' in export and 'timeout=2m' in export
        assert 'mode=max' not in export
        assert not {'secrets', 'secret-envs', 'ssh', 'github-token'} & set(settings)
        runs = '\n'.join(s.get('run', '') for s in steps[steps.index(build) + 1:])
        if name == 'image-runtime':
            assert 'docker run --rm "$CDC_IMAGE" --help' in runs
            assert 'runtime-imports-ok' in runs and 'control-plane-imports-ok' in runs
        else:
            script = 'test_verifier_image.sh' if name == 'verifier-runtime' else 'test_control_plane_image.sh'
            assert f'sh scripts/{script} "${image}"' in runs
            assert '--network none' in Path('scripts', script).read_text()


def test_every_job_has_a_cost_timeout_without_skipping_its_gates():
    expected = {
        'tests': 20, 'image-runtime': 15, 'verifier-runtime': 15,
        'cockpit-runtime': 15, 'java': 10, 'chart': 5,
        'quadringent-product-gate': 10,
    }
    assert {name: job['timeout-minutes'] for name, job in workflow()['jobs'].items()} == expected
