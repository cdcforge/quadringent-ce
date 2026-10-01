"""Exécute la vraie commande de collecte CI avec des sorties locales synthétiques."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

import pytest
import yaml


def _collection_steps():
    steps = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())["jobs"]["tests"]["steps"]
    collect = next(step for step in steps if "--collect-only" in step.get("run", ""))
    upload = next(step for step in steps if step.get("with", {}).get("name") == "pytest-collection")
    return steps, collect, upload


def _execute_collection(tmp_path, *, collection_exit=0, token="nom-parametre-synthetique", scanner=None):
    _, collect, _ = _collection_steps()
    workspace, runner_temp, tools = (tmp_path / name for name in ("workspace", "runner-temp", "bin"))
    for directory in (workspace, runner_temp, tools):
        directory.mkdir()
    shutil.copyfile(".gitleaks.toml", workspace / ".gitleaks.toml")
    python = tools / "python"
    python.write_text(f"#!{sys.executable}\n" + """
import os
import sys
assert sys.argv[1:] == ['-m', 'pytest', '--collect-only', '-q']
print('test_collect.py::test_parameter[' + os.environ['COLLECTION_PARAMETER'] + ']')
print('diagnostic stderr : ' + os.environ['COLLECTION_PARAMETER'], file=sys.stderr)
sys.exit(int(os.environ['COLLECTION_EXIT']))
""")
    python.chmod(0o700)
    if scanner is not None:
        gitleaks = tools / "gitleaks"
        gitleaks.write_text(f"#!{sys.executable}\n" + scanner)
        gitleaks.chmod(0o700)
    outputs = runner_temp / "outputs"
    outputs.touch()
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", collect["run"]], cwd=workspace,
        capture_output=True, text=True,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "RUNNER_TEMP": str(runner_temp),
             "GITHUB_OUTPUT": str(outputs), "COLLECTION_EXIT": str(collection_exit),
             "COLLECTION_PARAMETER": token},
    )
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    return result, values, runner_temp, workspace


def _upload_allowed(collect, upload, values):
    # Vérifie le contrat GitHub de l'upload puis évalue sa seule condition de données.
    assert collect.get("id")
    assert upload.get("if") == f"always() && steps.{collect['id']}.outputs.safe == 'true'"
    assert upload["with"]["path"] == "${{ steps." + collect["id"] + ".outputs.path }}"
    return values.get("safe") == "true"


def test_collection_installs_scanner_before_capture_and_gates_upload():
    steps, collect, upload = _collection_steps()
    install = next(step for step in steps if step.get("env", {}).get("GITLEAKS_VERSION"))
    assert steps.index(install) < steps.index(collect) < steps.index(upload)
    assert not _upload_allowed(collect, upload, {})


@pytest.mark.parametrize("collection_exit", (0, 2))
def test_clean_collection_is_scanned_before_tail_and_can_upload_on_failure(tmp_path, collection_exit):
    scanner = """
from pathlib import Path
import sys
args = sys.argv[1:]
assert args[0] == 'dir'
assert '--redact' in args and '--no-banner' in args
assert args[args.index('--config') + 1] == '.gitleaks.toml'
files = list(Path(args[1]).glob('*.txt'))
assert len(files) == 1
assert 'diagnostic stderr' in files[0].read_text()
print('scan-clean-finished', flush=True)
"""
    result, values, runner_temp, workspace = _execute_collection(
        tmp_path, collection_exit=collection_exit, scanner=scanner,
    )
    assert result.returncode == collection_exit
    _, collect, upload = _collection_steps()
    assert _upload_allowed(collect, upload, values)
    output = Path(values["path"])
    assert output.is_relative_to(runner_temp) and not output.is_relative_to(workspace)
    assert "test_collect.py::test_parameter" in output.read_text()
    assert "diagnostic stderr" in output.read_text()
    if collection_exit:
        assert result.stdout.index("scan-clean-finished") < result.stdout.index("test_collect.py")
    else:
        assert "test_collect.py" not in result.stdout


@pytest.mark.parametrize("collection_exit", (0, 2))
def test_scanner_error_blocks_tail_and_upload_even_if_collection_failed(tmp_path, collection_exit):
    result, values, _, _ = _execute_collection(
        tmp_path, collection_exit=collection_exit, scanner="import sys\nsys.exit(37)\n",
    )
    assert result.returncode == 37
    assert "test_collect.py" not in result.stdout + result.stderr
    _, collect, upload = _collection_steps()
    assert not _upload_allowed(collect, upload, values)


@pytest.mark.parametrize("collection_exit", (0, 2))
def test_runtime_generated_secret_is_not_logged_or_uploaded(tmp_path, collection_exit):
    if shutil.which("gitleaks") is None:
        pytest.skip("Gitleaks local absent ; installé avant la collecte dans la CI")
    # Aucune valeur sensible n'est stockée dans la source suivie.
    token = ".".join(("quadringent1", secrets.token_urlsafe(40), secrets.token_urlsafe(60)))
    result, values, runner_temp, workspace = _execute_collection(
        tmp_path, collection_exit=collection_exit, token=token,
    )
    exposed = token in result.stdout or token in result.stderr
    assert not exposed, "la collecte a exposé la valeur synthétique avant validation"
    assert result.returncode != 0
    _, collect, upload = _collection_steps()
    assert not _upload_allowed(collect, upload, values)
    assert any(token in path.read_text() for path in runner_temp.rglob("*.txt"))
    assert not list(workspace.glob("*.txt"))
