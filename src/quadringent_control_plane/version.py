"""Version installée, ou métadonnée source pendant le développement."""
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
import tomllib


def version() -> str:
    try:
        return package_version("quadringent")
    except PackageNotFoundError:
        for parent in list(Path(__file__).resolve().parents)[:3]:
            source = parent / "pyproject.toml"
            if source.is_file():
                project = tomllib.loads(source.read_text())["project"]
                if project["name"] == "quadringent":
                    return str(project["version"])
        raise RuntimeError("Métadonnées de version absentes : installer Quadringent") from None
