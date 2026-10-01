"""Le contexte de build Docker contient tout ce que chaque Dockerfile copie.

Les ``.dockerignore`` de ce dépôt sont des listes blanches : un fichier ajouté
au Dockerfile mais oublié dans la liste casse la construction réelle (constaté
le 23 septembre 2026 : le sous-paquet ``v2``, ses migrations et deux scripts
étaient exclus de l'image du control plane). Les règles sont évaluées comme
par Docker : dernière règle correspondante gagnante, ``!`` pour réinclure.
"""
from __future__ import annotations

from pathlib import Path
import shlex
import subprocess

import pathspec
import pytest

ROOT = Path(__file__).resolve().parents[1]
# ``COPY ui/`` est volontairement restreint par la liste blanche aux seuls
# fichiers du build (package, config Vite, sources, scripts de build) : le
# build de l'UI et ``verify-build.mjs`` vérifient ce contexte eux-mêmes.
INTENTIONALLY_NARROWED = ("ui/",)
PAIRS = [
    (ROOT / "docker/Dockerfile", ROOT / "docker/Dockerfile.dockerignore"),
    (ROOT / "docker/control-plane.Dockerfile", ROOT / "docker/control-plane.Dockerfile.dockerignore"),
    (ROOT / "docker/verifier.Dockerfile", ROOT / "docker/verifier.Dockerfile.dockerignore"),
]


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def _copy_sources(dockerfile: Path) -> list[str]:
    sources: list[str] = []
    for raw in dockerfile.read_text().splitlines():
        line = raw.strip()
        if not line.upper().startswith("COPY ") or "--from=" in line:
            continue
        parts = [part for part in shlex.split(line[5:]) if not part.startswith("--")]
        sources.extend(parts[:-1])
    return sources


@pytest.mark.parametrize("dockerfile,ignore", PAIRS, ids=lambda path: path.name)
def test_every_copied_source_is_in_the_build_context(dockerfile: Path, ignore: Path) -> None:
    spec = pathspec.PathSpec.from_lines("gitignore", ignore.read_text().splitlines())
    tracked = _tracked_files()
    missing: list[str] = []
    for source in _copy_sources(dockerfile):
        source = source.rstrip("/")
        under = [path for path in tracked if path == source or path.startswith(source + "/")]
        if not under:
            missing.append(f"{source} (absent du dépôt)")
            continue
        if source + "/" in INTENTIONALLY_NARROWED:
            continue
        missing.extend(path for path in under if "/__pycache__/" not in path and spec.match_file(path))
    assert missing == [], f"{dockerfile.name} copie des fichiers exclus du contexte : {missing[:30]}"


@pytest.mark.parametrize("dockerfile,ignore", PAIRS, ids=lambda path: path.name)
def test_runtime_context_excludes_local_bytecode_and_credentials(dockerfile: Path, ignore: Path) -> None:
    """Le build local n'embarque ni bytecode ignoré ni fichier d'accès.

    Un COPY de paquet entier inclurait sinon les fichiers laissés par les
    essais précédents, même lorsqu'ils sont ignorés par Git.
    """
    spec = pathspec.PathSpec.from_lines("gitignore", ignore.read_text().splitlines())
    excluded = [
        "src/quadringent/__pycache__/stale.cpython-312.pyc",
        "src/quadringent_control_plane/v2/__pycache__/stale.cpython-312.pyc",
        "scripts/__pycache__/stale.cpython-312.pyc",
        "src/quadringent_control_plane/.env.local",
        "src/quadringent_control_plane/private.key",
        "src/quadringent_control_plane/site.jks",
        "src/quadringent_control_plane/client.crt",
        "licenses/untracked-private.key",
        "ui/src/.env.local",
    ]
    assert all(spec.match_file(path) for path in excluded), f"{dockerfile.name} inclut des résidus de build locaux"


def _quadringent_imports(package_dir: Path) -> set[str]:
    """Modules ``quadringent.*`` importés, transitivement, par un paquet."""
    import ast

    seen: set[str] = set()
    pending = [path for path in package_dir.rglob("*.py") if "__pycache__" not in path.parts]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited or not path.exists():
            continue
        visited.add(path)
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                parts = name.split(".")
                if parts[0] != "quadringent" or len(parts) < 2:
                    continue
                module = ROOT / "src/quadringent" / f"{parts[1]}.py"
                if module.exists():
                    seen.add(f"src/quadringent/{parts[1]}.py")
                    pending.append(module)
    return seen


def test_control_plane_image_ships_every_quadringent_module_it_imports() -> None:
    """La liste de modules copiés à la main doit couvrir tout ce que le control plane importe
    (constaté : ``table_discovery`` manquait, l'app v2 ne démarrait pas dans l'image)."""
    dockerfile = ROOT / "docker/control-plane.Dockerfile"
    copied = {source for source in _copy_sources(dockerfile) if source.startswith("src/quadringent/")}
    needed = _quadringent_imports(ROOT / "src/quadringent_control_plane") | {"src/quadringent/__init__.py"}
    missing = sorted(needed - copied)
    assert missing == [], f"modules importés mais non copiés dans l'image : {missing}"


@pytest.mark.parametrize("dockerfile", sorted((ROOT / "docker").glob("*Dockerfile")), ids=lambda p: p.name)
def test_final_user_is_numeric(dockerfile: Path) -> None:
    """Kubernetes refuse ``runAsNonRoot`` avec un utilisateur non numérique
    (« cannot verify user is non-root ») : chaque image finit par un UID."""
    users = [line.split(None, 1)[1].strip() for line in dockerfile.read_text().splitlines() if line.startswith("USER ")]
    assert users, f"{dockerfile.name} ne déclare aucun USER"
    assert users[-1].split(":")[0].isdigit(), f"{dockerfile.name} : USER {users[-1]} n'est pas numérique"
