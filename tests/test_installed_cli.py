"""Contrats du point d'entrée public, distinct des imports de développement."""
import subprocess
import sys
import tomllib
from pathlib import Path


def test_la_version_du_runtime_est_celle_du_paquet() -> None:
    from quadringent_control_plane.version import version
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert version() == project["project"]["version"]


def test_le_module_cli_expose_une_aide_sans_configuration_de_site() -> None:
    result = subprocess.run([sys.executable, "-m", "quadringent_control_plane.cli", "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--source" in result.stdout
    assert "--license-key" not in result.stdout


def test_le_collecteur_expose_son_perimetre_sans_acces_cloud() -> None:
    result = subprocess.run([sys.executable, "-m", "quadringent.cost_collect", "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--namespace" in result.stdout
    assert "--cluster-currency" in result.stdout
