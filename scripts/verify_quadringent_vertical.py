#!/usr/bin/env python3
"""Vérifie localement la verticale Quadringent, sans aucune mutation cloud.

Le contrôle lit le dépôt et le bundle UI, rend la chart Helm, importe le chemin
runtime et démarre un serveur HTTP éphémère lié à ``127.0.0.1`` sur un port
attribué par le système. Aucun service externe, secret ou horloge murale n'est
nécessaire pour les preuves fonctionnelles.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from threading import Thread
from urllib.parse import quote

# Ce fichier partage son préfixe avec le package et vit à côté du lanceur
# ``quadringent_control_plane.py``. L'exécution directe place ``scripts/`` avant
# ``PYTHONPATH`` ; le retirer empêche Python de prendre le lanceur pour le
# package.
_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
_script_path_index = (
    sys.path.index(_SCRIPT_DIRECTORY) if _SCRIPT_DIRECTORY in sys.path else None
)
if _script_path_index is not None:
    sys.path.pop(_script_path_index)
try:
    from quadringent_control_plane.repository import (
        ProjectionRepository,
        parse_source_spec,
    )
    from quadringent_control_plane.server import serve
finally:
    if _script_path_index is not None:
        sys.path.insert(_script_path_index, _SCRIPT_DIRECTORY)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNTIME_MODULES = {"console_snapshot", "lag_history"}
SECRET_KEY_PARTS = {
    "authorization",
    "credential",
    "credentials",
    "passwd",
    "password",
    "secret",
    "token",
}
SECRET_KEYS = {
    "api_key",
    "apikey",
    "api_token",
    "client_secret",
    "clientsecret",
    "private_key",
    "privatekey",
    "refresh_token",
    "refreshtoken",
}
RAW_API_FIELDS = {
    "after",
    "before",
    "last_error",
    "payload",
    "raw_payload",
    "raw_error",
    "stacktrace",
    "traceback",
}
DEMO_BUNDLE_SIGNATURES = {
    "console-dev.json",
    "vite_use_fixture",
    "fixture://",
    "/fixtures/",
    "ui/fixtures",
    "data/fixtures",
    "fixture_fallback",
}
SENSITIVE_KEY_SEQUENCES = {("api", "key"), ("private", "key")}
JAVASCRIPT_EXTENSIONS = {".js", ".mjs"}
_VITE_SNAPSHOT_CACHE: dict[
    tuple[Path, str, str], tuple[bool, str, dict[str, str]]
] = {}


class _ModuleScriptParser(HTMLParser):
    """Collecte les scripts module sans casser les attributs contenant ``>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str | None] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "script":
            return
        values = {name.casefold(): value for name, value in attrs}
        script_type = values.get("type")
        if script_type is None or script_type.casefold() != "module":
            return
        self.sources.append(values.get("src"))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)


@dataclass(frozen=True)
class Check:
    id: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SseRevisionFrame:
    event: str
    revision: int


@dataclass(frozen=True)
class VerificationResult:
    checks: tuple[Check, ...]
    prerequisites: tuple[str, ...] = ()

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(check.id for check in self.checks if not check.passed)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.prerequisites


class _Checks:
    def __init__(self) -> None:
        self.items: list[Check] = []

    def add(self, check_id: str, passed: bool, detail: str) -> None:
        self.items.append(Check(check_id, bool(passed), detail))


def verify_vertical(
    *,
    overview: Mapping[str, object],
    production_bundle: Path,
    rendered_helm: str | None = None,
    root: Path = ROOT,
) -> VerificationResult:
    """Retourne toutes les preuves locales, sans arrêter au premier échec."""

    checks = _Checks()
    prerequisites: list[str] = []

    node_prerequisite = _node_runtime_prerequisite()
    if node_prerequisite is not None:
        prerequisites.append(node_prerequisite)

    _check_runtime(root, checks)

    if rendered_helm is None:
        rendered_helm, prerequisite = _render_helm(root)
        if prerequisite is not None:
            prerequisites.append(prerequisite)
    _check_helm(rendered_helm, checks)
    _check_bundle(production_bundle, root, checks)
    _check_truth(overview, checks)
    _check_api_fixture(overview, checks)
    _check_local_api(checks)

    return VerificationResult(tuple(checks.items), tuple(prerequisites))


def _node_runtime_prerequisite() -> str | None:
    try:
        completed = subprocess.run(
            ["node", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return "Node.js 24 ou 26 absent: installer Node.js 24 LTS"
    except subprocess.TimeoutExpired:
        return "Node.js 24 ou 26 non vérifiable: node --version a expiré"
    version = completed.stdout.strip()
    if completed.returncode != 0 or re.fullmatch(r"v(?:24|26)(?:\.\d+){1,2}", version) is None:
        detected = version or f"exit {completed.returncode}"
        return f"Node.js 24 ou 26 requis; version détectée: {detected}"
    return None


def _check_runtime(root: Path, checks: _Checks) -> None:
    manifest = root / "docker" / "runtime-modules.txt"
    try:
        entries = tuple(
            line.strip()
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    except OSError as error:
        checks.add("runtime_manifest_complete", False, f"manifest illisible: {type(error).__name__}")
        checks.add("worker_runtime_imports", False, "imports non tentés sans manifest")
        return

    module_names = {Path(entry).stem for entry in entries}
    missing = sorted(EXPECTED_RUNTIME_MODULES - module_names)
    invalid_paths = sorted(
        entry for entry in entries if not (root / entry).is_file()
    )
    checks.add(
        "runtime_manifest_complete",
        not missing and not invalid_paths,
        "console_snapshot et lag_history présents; tous les chemins existent"
        if not missing and not invalid_paths
        else f"modules manquants={missing}; chemins invalides={invalid_paths}",
    )

    failures: list[str] = []
    import_names = [
        f"quadringent.{Path(entry).stem}"
        for entry in entries
        if Path(entry).stem != "__init__"
    ]
    for module_name in import_names:
        try:
            importlib.import_module(module_name)
        except Exception as error:  # la preuve doit rapporter aussi les deps manquantes
            failures.append(f"{module_name}:{type(error).__name__}")
    worker_path = root / "scripts" / "as400_continuous_capture.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "_quadringent_verified_worker_entrypoint", worker_path
        )
        if spec is None or spec.loader is None:
            raise ImportError("point d'entrée sans loader")
        worker_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker_module)
    except Exception as error:
        failures.append(f"as400_continuous_capture:{type(error).__name__}")
    checks.add(
        "worker_runtime_imports",
        not failures,
        "modules runtime et point d'entrée worker importables"
        if not failures
        else "échecs d'import: " + ", ".join(failures),
    )


def _declared_site_namespace(values_path: Path) -> str | None:
    """Namespace déclaré par le site dans le fichier de values.

    La chart refuse tout rendu hors du namespace déclaré : le vérificateur le
    lit donc dans les values au lieu de porter un nom d'installation.
    """

    try:
        text = values_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(
        r"(?m)^site:\s*\n(?:[ \t]+[^\n]*\n|[ \t]*#[^\n]*\n)*?[ \t]+namespace:\s*\"?([a-z0-9][a-z0-9-]*)\"?[ \t]*$",
        text,
    )
    return match.group(1) if match else None


def _render_helm(root: Path) -> tuple[str, str | None]:
    values_path = root / "infra-values" / "values-int.yaml"
    namespace = _declared_site_namespace(values_path)
    if namespace is None:
        return "", "values-int.yaml: site.namespace absent"
    command = [
        "helm",
        "template",
        "cdc",
        str(root / "chart"),
        "--namespace",
        namespace,
        "-f",
        str(values_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=root,
        )
    except FileNotFoundError:
        return "", "helm: command not found"
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1:] or ["erreur sans détail"]
        return "", f"helm template: exit {completed.returncode}: {detail[0]}"
    return completed.stdout, None


def _check_helm(rendered: str, checks: _Checks) -> None:
    requirements = {
        "snapshot": "AS400_CONSOLE_SNAPSHOT_S3_KEY" in rendered,
        "journal": "AS400_JOURNAL_LIBRARY" in rendered,
        "tls": re.search(r'AS400_TLS:\s*["\']?true["\']?', rendered) is not None,
        "plaintext_closed": re.search(
            r'AS400_ALLOW_PLAINTEXT:\s*["\']?false["\']?', rendered
        )
        is not None,
    }
    missing = sorted(name for name, present in requirements.items() if not present)
    checks.add(
        "helm_snapshot_tls",
        not missing,
        "snapshot activé, TLS explicite et plaintext interdit"
        if not missing
        else "garde-fous absents: " + ", ".join(missing),
    )


def _check_bundle(bundle: Path, root: Path, checks: _Checks) -> None:
    symlinks = sorted(
        str(path.relative_to(bundle))
        for path in bundle.rglob("*")
        if path.is_symlink()
    ) if bundle.is_dir() and not bundle.is_symlink() else ["dist"]
    files = sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink()
    ) if bundle.is_dir() and not bundle.is_symlink() else []
    offenders: list[str] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered_path = str(path.relative_to(bundle)).lower()
        lowered_content = content.lower()
        if "fixture" in lowered_path or any(
            signature in lowered_content for signature in DEMO_BUNDLE_SIGNATURES
        ):
            offenders.append(str(path.relative_to(bundle)))
    vite_build_ok, vite_detail = _vite_bundle_proof(bundle, root=root)
    if symlinks:
        vite_build_ok = False
        vite_detail = "liens symboliques interdits: " + ", ".join(symlinks)
    build_guard_ok, build_guard_detail = _run_build_guard(root, bundle)
    checks.add(
        "ui_build_exists",
        vite_build_ok,
        vite_detail,
    )
    checks.add(
        "production_bundle_has_no_demo",
        vite_build_ok and build_guard_ok and not offenders,
        "aucune fixture de démonstration dans le bundle"
        if vite_build_ok and build_guard_ok and not offenders
        else (
            "traces demo: " + ", ".join(offenders)
            if offenders
            else (
                build_guard_detail
                if not build_guard_ok
                else "bundle Vite non vérifié"
            )
        ),
    )

    router_ok, router_detail = _execute_router_contract(root)
    checks.add(
        "ui_route_inventory",
        vite_build_ok and router_ok,
        "matrice fonctionnelle router.ts exécutée et bundle Vite vérifié"
        if vite_build_ok and router_ok
        else router_detail if not router_ok else "bundle Vite non vérifié",
    )


def _execute_router_contract(root: Path) -> tuple[bool, str]:
    router_path = root / "ui" / "src" / "router.ts"
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return False, "racine du dépôt absente"
    current = root
    for part in ("ui", "src", "router.ts"):
        current = current / part
        if current.is_symlink():
            return False, "router.ts ou un de ses parents est symbolique"
    try:
        router = router_path.resolve(strict=True)
        router.relative_to(resolved_root)
    except (OSError, ValueError):
        return False, "router.ts hors de la racine du dépôt"
    if not router.is_file():
        return False, "router.ts absent ou symbolique"
    success_marker = "QUADRINGENT_ROUTER_CONTRACT_OK"
    harness = f"""
import {{ readFileSync }} from 'node:fs';
import {{ stripTypeScriptTypes }} from 'node:module';
import vm from 'node:vm';
const IntrinsicError = Error;
const ownProperty = Object.getOwnPropertyDescriptor.bind(Object);
const ownKeys = Reflect.ownKeys.bind(Reflect);
const safeWrite = process.stdout.write.bind(process.stdout);
const accepted = [
  ['#/overview', 'overview', undefined, undefined, 1],
  ['#/pipelines', 'pipelines', undefined, undefined, 1],
  ['#/pipeline/dev-cntr', 'pipeline', 'dev-cntr', undefined, 2],
  ['#/pipeline/dev-cntr/overview', 'pipeline', 'dev-cntr', 'overview', 3],
  ['#/pipeline/dev-cntr/live', 'pipeline', 'dev-cntr', 'live', 3],
  ['#/incidents', 'incidents', undefined, undefined, 1],
  ['#/usage', 'usage', undefined, undefined, 1],
  ['#/setup', 'setup', undefined, undefined, 1],
];
const rejected = [
  '#/pipeline/dev-cntr/integrity',
  '#/pipeline/dev-cntr/runs',
  '#/pipeline/%E0%A4%A',
  '#/pipeline/dev-cntr/live/extra',
];
const context = vm.createContext({{ decodeURIComponent, encodeURIComponent, Set, URLSearchParams }});
const source = stripTypeScriptTypes(
  readFileSync({json.dumps(str(router))}, 'utf8'),
  {{ mode: 'strip' }},
);
const routeModule = new vm.SourceTextModule(source, {{
  context,
  identifier: {json.dumps(router.as_uri())},
}});
await routeModule.link(async (specifier) => {{
  if (specifier !== 'react') throw new IntrinsicError(`import inattendu: ${{specifier}}`);
  return new vm.SyntheticModule(['useEffect', 'useState'], function () {{
    this.setExport('useEffect', () => undefined);
    this.setExport('useState', () => [undefined, () => undefined]);
  }}, {{ context }});
}});
await routeModule.evaluate();
const parseRoute = routeModule.namespace.parseRoute;
const href = routeModule.namespace.href;
if (typeof parseRoute !== 'function' || typeof href !== 'function') throw new IntrinsicError('exports route absents');
function assertRoute(actual, name, id, tab, keyCount, context) {{
  if (actual === null || typeof actual !== 'object') throw new IntrinsicError(context);
  if (ownKeys(actual).length !== keyCount) throw new IntrinsicError(context);
  if (ownProperty(actual, 'name')?.value !== name) throw new IntrinsicError(context);
  if (id !== undefined && ownProperty(actual, 'id')?.value !== id) throw new IntrinsicError(context);
  if (tab !== undefined && ownProperty(actual, 'tab')?.value !== tab) throw new IntrinsicError(context);
}}
function checkAccepted(index) {{
  const path = accepted[index][0];
  const name = accepted[index][1];
  const id = accepted[index][2];
  const tab = accepted[index][3];
  const keyCount = accepted[index][4];
  const actual = parseRoute(path);
  assertRoute(actual, name, id, tab, keyCount, `route ${{path}}`);
  const route = tab === undefined
    ? (id === undefined ? {{name}} : {{name, id}})
    : {{name, id, tab}};
  assertRoute(parseRoute(href(route)), name, id, tab, keyCount, `href ${{path}}`);
}}
function checkRejected(index) {{
  const path = rejected[index];
  assertRoute(parseRoute(path), 'overview', undefined, undefined, 1, `rejet ${{path}}`);
}}
checkAccepted(0); checkAccepted(1); checkAccepted(2); checkAccepted(3);
checkAccepted(4); checkAccepted(5); checkAccepted(6); checkAccepted(7);
checkRejected(0); checkRejected(1); checkRejected(2); checkRejected(3);
if (href({{name:'overview'}}) !== '#/' || href({{name:'usage'}}) !== '#/usage' || href({{name:'setup'}}) !== '#/setup') throw new IntrinsicError('href canonique');
safeWrite({json.dumps(success_marker)});
"""
    try:
        completed = subprocess.run(
            [
                "node",
                "--no-warnings",
                "--experimental-vm-modules",
                "--input-type=module",
                "-e",
                harness,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=root,
        )
    except FileNotFoundError:
        return False, "node: command not found"
    except subprocess.TimeoutExpired:
        return False, "matrice router.ts: timeout"
    if completed.returncode != 0:
        return False, f"matrice router.ts: exit {completed.returncode}"
    if completed.stdout != success_marker:
        return False, "matrice router.ts interrompue avant toutes les assertions"
    return True, "matrice router.ts exécutée"


def _vite_bundle_proof(bundle: Path, *, root: Path = ROOT) -> tuple[bool, str]:
    index = bundle / "index.html"
    manifest_path = bundle / ".vite" / "manifest.json"
    try:
        markup = index.read_text(encoding="utf-8")
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "index.html ou manifeste Vite absent/invalide"
    if not isinstance(manifest_value, Mapping):
        return False, "manifeste Vite non objet"
    module_assets = _module_script_sources(markup)
    entries = [
        (key, value)
        for key, value in manifest_value.items()
        if isinstance(key, str)
        and isinstance(value, Mapping)
        and value.get("isEntry") is True
    ]
    if len(entries) != 1:
        return False, "le manifeste doit déclarer un entrypoint unique"
    entry_key, entry = entries[0]
    if entry_key != "index.html" or entry.get("src") != "index.html":
        return False, "entrypoint manifeste inattendu"
    source_ok, source_detail = _source_entry_contract(root)
    if not source_ok:
        return False, source_detail
    entry_file = entry.get("file")
    if (
        not isinstance(entry_file, str)
        or not _is_canonical_dist_path(entry_file)
        or not entry_file.endswith(".js")
        or module_assets != [f"/{entry_file}"]
    ):
        return False, "entrypoint manifeste non référencé par index.html"
    closure_ok, closure_detail, javascript_files = _manifest_closure(
        bundle, manifest_value, entry_key
    )
    if not closure_ok:
        return False, closure_detail
    for javascript in javascript_files:
        parseable, parse_detail = _javascript_is_parseable(javascript)
        if not parseable:
            return False, parse_detail
    provenance_ok, provenance_detail = _isolated_vite_proof(bundle, root)
    if not provenance_ok:
        return False, provenance_detail
    return True, (
        "build Vite reproduit depuis /src/main.tsx, manifeste fermé et "
        f"entrypoint vérifié: /{entry_file}"
    )


def _manifest_closure(
    bundle: Path, manifest: Mapping[str, object], entry_key: str
) -> tuple[bool, str, tuple[Path, ...]]:
    pending = [entry_key]
    visited: set[str] = set()
    javascript: list[Path] = []
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        if not _is_canonical_dist_path(key):
            return False, f"clé logique manifeste non canonique: {key}", ()
        item = manifest.get(key)
        if not isinstance(item, Mapping):
            return False, f"dépendance manifeste absente: {key}", ()
        visited.add(key)
        file_value = item.get("file")
        if not isinstance(file_value, str):
            return False, f"fichier manifeste absent: {key}", ()
        file_ok, file_path = _closed_dist_file(bundle, file_value)
        if not file_ok or file_path is None:
            return False, f"fichier manifeste hors dist ou absent: {key}", ()
        if file_path.suffix not in JAVASCRIPT_EXTENSIONS:
            return False, f"chunk manifeste non JavaScript: {file_value}", ()
        javascript.append(file_path)
        for field in ("css", "assets"):
            values = item.get(field, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                return False, f"champ manifeste invalide: {key}.{field}", ()
            for value in values:
                dependency_ok, _ = _closed_dist_file(bundle, value)
                if not dependency_ok:
                    return False, f"asset manifeste hors dist ou absent: {value}", ()
        for field in ("imports", "dynamicImports"):
            values = item.get(field, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                return False, f"champ manifeste invalide: {key}.{field}", ()
            pending.extend(values)
    return True, "fermeture manifeste vérifiée", tuple(javascript)


def _closed_dist_file(bundle: Path, relative: str) -> tuple[bool, Path | None]:
    if not _is_canonical_dist_path(relative):
        return False, None
    candidate = bundle / relative
    try:
        resolved_bundle = bundle.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError:
        return False, None
    try:
        resolved_candidate.relative_to(resolved_bundle)
    except ValueError:
        return False, None
    current = candidate
    while current != bundle:
        if current.is_symlink():
            return False, None
        current = current.parent
    return resolved_candidate.is_file(), resolved_candidate


def _is_canonical_dist_path(relative: str) -> bool:
    if not relative or "\\" in relative:
        return False
    segments = relative.split("/")
    return (
        all(segment not in {"", ".", ".."} for segment in segments)
        and quote(relative, safe="/-._~") == relative
    )


def _source_entry_contract(root: Path) -> tuple[bool, str]:
    ui = root / "ui"
    index = ui / "index.html"
    main = ui / "src" / "main.tsx"
    source = ui / "src"
    for path in (ui, index, source, main):
        if path.is_symlink():
            return False, "entrée source UI ou un de ses parents est symbolique"
    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_symlink():
                return False, f"source UI symbolique interdite: {path.relative_to(ui)}"
    try:
        markup = index.read_text(encoding="utf-8")
    except OSError:
        return False, "ui/index.html absent"
    sources = _module_script_sources(markup)
    if sources != ["/src/main.tsx"] or not main.is_file():
        return False, "ui/index.html doit référencer uniquement /src/main.tsx"
    return True, "entrée source /src/main.tsx vérifiée"


def _module_script_sources(markup: str) -> list[str | None]:
    parser = _ModuleScriptParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return []
    return parser.sources


def _ui_source_fingerprint(root: Path) -> tuple[bool, str, str]:
    ui = root / "ui"
    if not ui.is_dir() or ui.is_symlink():
        return False, "répertoire ui absent ou symbolique", ""
    digest = hashlib.sha256()
    excluded = {"node_modules", "dist", ".git"}
    try:
        paths = sorted(ui.rglob("*"), key=lambda path: path.relative_to(ui).as_posix())
        for path in paths:
            relative = path.relative_to(ui)
            if relative.parts and relative.parts[0] in excluded:
                continue
            if path.is_symlink():
                return False, f"entrée de build UI symbolique: {relative.as_posix()}", ""
            if not path.is_file():
                continue
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError as error:
        return False, f"lecture des entrées UI impossible: {error}", ""
    return True, "empreinte des entrées UI calculée", digest.hexdigest()


def _isolated_vite_proof(bundle: Path, root: Path) -> tuple[bool, str]:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return False, "racine du dépôt absente"
    fingerprint_ok, fingerprint_detail, fingerprint = _ui_source_fingerprint(root)
    if not fingerprint_ok:
        return False, fingerprint_detail
    vite = root / "ui" / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.is_file():
        return False, "dépendances UI absentes: exécuter npm ci --prefix ui"
    license_digest = hashlib.sha256()
    try:
        for license_file in ("LICENSE", "NOTICE"):
            license_digest.update(license_file.encode("utf-8"))
            license_digest.update((root / license_file).read_bytes())
    except OSError as error:
        return False, f"licences du build UI absentes: {error}"
    cache_key = (resolved_root, fingerprint, license_digest.hexdigest())
    cached = _VITE_SNAPSHOT_CACHE.get(cache_key)
    if cached is None:
        with tempfile.TemporaryDirectory() as directory:
            isolated_ui = Path(directory) / "ui"
            shutil.copytree(
                root / "ui",
                isolated_ui,
                symlinks=True,
                ignore=shutil.ignore_patterns("dist", "node_modules"),
            )
            isolated_ok, isolated_detail, _ = _ui_source_fingerprint(Path(directory))
            if not isolated_ok:
                cached = (False, isolated_detail, {})
                _VITE_SNAPSHOT_CACHE[cache_key] = cached
                return False, isolated_detail
            (isolated_ui / "node_modules").symlink_to(
                (root / "ui" / "node_modules").resolve(strict=True),
                target_is_directory=True,
            )
            for license_file in ("LICENSE", "NOTICE"):
                shutil.copy2(root / license_file, Path(directory) / license_file)
            rebuilt = isolated_ui / "dist"
            try:
                completed = subprocess.run(
                    ["npm", "run", "build"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=isolated_ui,
                )
            except FileNotFoundError:
                cached = (False, "npm ou Node.js 24/26 absent: installer Node.js 24 LTS", {})
            except subprocess.TimeoutExpired:
                cached = (False, "reconstruction UI isolée: timeout", {})
            else:
                if completed.returncode != 0:
                    cached = (
                        False,
                        f"reconstruction UI isolée: exit {completed.returncode}",
                        {},
                    )
                else:
                    cached = (
                        True,
                        "reconstruction UI isolée réussie",
                        _tree_hashes(rebuilt),
                    )
        _VITE_SNAPSHOT_CACHE[cache_key] = cached
    rebuilt_ok, rebuilt_detail, expected = cached
    if not rebuilt_ok:
        return False, rebuilt_detail
    actual = _tree_hashes(bundle)
    if actual != expected:
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = sorted(
            path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
        )
        return False, (
            "artefacts différents de la reconstruction Vite isolée: "
            f"absents={missing}; supplémentaires={extra}; modifiés={changed}"
        )
    return True, (
        "artefacts exécutables identiques à la reconstruction Vite isolée; "
        "chemins sources des sourcemaps normalisés"
    )


def _tree_hashes(directory: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not directory.is_dir() or directory.is_symlink():
        return result
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(directory).as_posix()
            content = path.read_bytes()
            if relative == ".vite/manifest.json":
                try:
                    manifest = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                else:
                    if isinstance(manifest, dict):
                        normalized_manifest: dict[str, object] = {}
                        for key, value in manifest.items():
                            normalized_key = _canonical_manifest_dependency_path(key)
                            if isinstance(value, dict):
                                value = dict(value)
                                source = value.get("src")
                                if isinstance(source, str):
                                    value["src"] = _canonical_manifest_dependency_path(source)
                            normalized_manifest[normalized_key] = value
                        content = json.dumps(
                            normalized_manifest, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
            elif path.suffix == ".map":
                try:
                    source_map = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                else:
                    if isinstance(source_map, dict):
                        # Vite rend les chemins ``sources`` relatifs au répertoire
                        # temporaire. Le code, les mappings et sourcesContent restent
                        # déterministes; seul ce tableau de localisation est neutralisé.
                        source_map.pop("sources", None)
                        content = json.dumps(
                            source_map, sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
            result[relative] = hashlib.sha256(content).hexdigest()
    return result


def _canonical_manifest_dependency_path(value: str) -> str:
    marker = "node_modules/"
    position = value.rfind(marker)
    return value[position:] if position >= 0 else value


def _javascript_is_parseable(path: Path) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["node", "--check", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, "node: command not found"
    except subprocess.TimeoutExpired:
        return False, "node --check: timeout"
    if completed.returncode != 0:
        return False, f"node --check: exit {completed.returncode}"
    return True, "JavaScript parseable"


def _run_build_guard(root: Path, bundle: Path) -> tuple[bool, str]:
    script = root / "ui" / "scripts" / "verify-build.mjs"
    if not script.is_file() or script.is_symlink():
        return False, "verify-build.mjs absent ou symbolique"
    try:
        completed = subprocess.run(
            ["node", str(script), str(bundle)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=root,
        )
    except FileNotFoundError:
        return False, "node: command not found"
    except subprocess.TimeoutExpired:
        return False, "verify-build.mjs: timeout"
    if completed.returncode != 0:
        return False, f"verify-build.mjs: exit {completed.returncode}"
    with tempfile.TemporaryDirectory() as directory:
        adversarial_dist = Path(directory)
        shutil.copytree(bundle, adversarial_dist, dirs_exist_ok=True)
        (adversarial_dist / "assets" / "forbidden-proof.js").write_text(
            "const source = 'console-dev.json';",
            encoding="utf-8",
        )
        try:
            negative = subprocess.run(
                ["node", str(script), str(adversarial_dist)],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                cwd=root,
            )
        except subprocess.TimeoutExpired:
            return False, "verify-build.mjs: timeout sur le cas négatif"
    if negative.returncode == 0:
        return False, "verify-build.mjs n'a pas rejeté une signature de démonstration"
    return True, "verify-build.mjs exécuté en cas positif et négatif"


def _pipelines(overview: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    value = overview.get("pipelines")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _quality(pipeline: Mapping[str, object]) -> Mapping[str, object]:
    value = pipeline.get("quality")
    return value if isinstance(value, Mapping) else {}


def _check_truth(overview: Mapping[str, object], checks: _Checks) -> None:
    pipelines = _pipelines(overview)
    simulations = [
        item for item in pipelines if _quality(item).get("evidence_kind") == "simulation"
    ]
    stale = [item for item in pipelines if _quality(item).get("freshness") == "stale"]
    checks.add(
        "simulation_is_not_live",
        bool(simulations) and all(item.get("status") != "healthy" for item in simulations),
        "toute simulation reste non healthy"
        if simulations and all(item.get("status") != "healthy" for item in simulations)
        else "simulation absente ou présentée healthy",
    )
    checks.add(
        "stale_is_not_healthy",
        all(item.get("status") != "healthy" for item in stale),
        "aucune observation stale n'est healthy"
        if all(item.get("status") != "healthy" for item in stale)
        else "une observation stale est présentée healthy",
    )

    destination_visible = False
    for item in pipelines:
        stages = item.get("stages")
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if not isinstance(stage, Mapping) or stage.get("id") != "destination":
                continue
            visible_text = " ".join(
                str(stage.get(key, "")) for key in ("headline", "detail")
            ).lower()
            if stage.get("status") in {"unknown", "degraded"} and (
                "non observ" in visible_text or "non prouv" in visible_text
            ):
                destination_visible = True
    checks.add(
        "destination_unobserved_visible",
        destination_visible,
        "la limite Snowflake non observée est explicite"
        if destination_visible
        else "aucune étape destination non observée explicite",
    )


def _check_api_fixture(overview: Mapping[str, object], checks: _Checks) -> None:
    offenders: list[str] = []

    def walk(value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = str(key)
                child_path = f"{path}.{name}"
                normalized = _normalize_key(name)
                key_parts = tuple(part for part in normalized.split("_") if part)
                if (
                    normalized in SECRET_KEYS
                    or bool(set(key_parts) & SECRET_KEY_PARTS)
                    or _contains_sensitive_sequence(key_parts)
                    or normalized in RAW_API_FIELDS
                ):
                    offenders.append(child_path)
                elif normalized == "error" and child is not None:
                    if not (
                        isinstance(child, Mapping)
                        and set(map(str, child.keys())) <= {"code", "correlation_id"}
                    ):
                        offenders.append(child_path)
                walk(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(overview, "$api")
    checks.add(
        "api_fixture_is_sanitized",
        not offenders,
        "aucune clé de secret, payload métier ou erreur brute"
        if not offenders
        else "champs interdits: " + ", ".join(offenders),
    )


def _normalize_key(value: str) -> str:
    separated_acronyms = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    separated_words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated_acronyms)
    return re.sub(r"[^a-z0-9]+", "_", separated_words.lower()).strip("_")


def _contains_sensitive_sequence(parts: Sequence[str]) -> bool:
    return any(
        tuple(parts[index : index + len(sequence)]) == sequence
        for sequence in SENSITIVE_KEY_SEQUENCES
        for index in range(len(parts) - len(sequence) + 1)
    )


def parse_sse_frame(frame: bytes) -> SseRevisionFrame:
    """Parse un frame de révision SSE et verrouille ``id == data.revision``."""

    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("frame SSE non UTF-8") from error
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith(":"):
            continue
        name, separator, raw_value = line.partition(":")
        if not separator or name not in {"id", "event", "data"} or name in fields:
            raise ValueError("forme SSE invalide")
        fields[name] = raw_value.removeprefix(" ")
    if set(fields) != {"id", "event", "data"} or not fields["id"].isdecimal():
        raise ValueError("forme SSE invalide")
    try:
        payload = json.loads(fields["data"])
    except json.JSONDecodeError as error:
        raise ValueError("data SSE invalide") from error
    revision = payload.get("revision") if isinstance(payload, Mapping) else None
    event_id = int(fields["id"])
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision != event_id
    ):
        raise ValueError("révision SSE incohérente entre id et data")
    if not fields["event"]:
        raise ValueError("événement SSE absent")
    return SseRevisionFrame(fields["event"], revision)


def validate_sse_revision_sequence(
    frames: Sequence[bytes],
) -> tuple[SseRevisionFrame, ...]:
    parsed = tuple(parse_sse_frame(frame) for frame in frames)
    if any(
        current.revision <= previous.revision
        for previous, current in zip(parsed, parsed[1:])
    ):
        raise ValueError("les révisions SSE doivent être strictement croissantes")
    return parsed


def _api_document(counter: int) -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": "2000-01-01T00:00:00+00:00",
        "flux": {"id": "verification", "label": "Vérification locale"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "R", "sequence": 1},
            "source_tail": {"receiver": "R", "sequence": 2},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": counter}},
    }


def _request_json(port: int, path: str) -> tuple[int, Mapping[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        return response.status, body
    finally:
        connection.close()


def _check_local_api(checks: _Checks) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "console.json"
        path.write_text(json.dumps(_api_document(1)), encoding="utf-8")
        repository = ProjectionRepository(
            [parse_source_spec(f"simulation:vertical-check:file://{path}")]
        )
        first = repository.refresh()
        server = serve(repository, port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            overview_status, overview = _request_json(server.server_port, "/v1/overview")
            list_status, listing = _request_json(server.server_port, "/v1/pipelines")
            detail_status, detail = _request_json(
                server.server_port, "/v1/pipelines/vertical-check"
            )
            route_shape = (
                overview_status == 200
                and list_status == 200
                and detail_status == 200
                and overview.get("revision") == first.revision
                and listing.get("revision") == first.revision
                and detail.get("revision") == first.revision
                and isinstance(overview.get("pipelines"), list)
                and isinstance(listing.get("pipelines"), list)
                and isinstance(detail.get("pipeline"), Mapping)
            )
            checks.add(
                "rest_overview_list_detail",
                route_shape,
                "overview, liste et détail REST cohérents"
                if route_shape
                else "forme ou révision REST incohérente",
            )

            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            connection.request("GET", "/v1/events")
            response = connection.getresponse()
            cursor_bytes = b"".join(response.fp.readline() for _ in range(4))
            path.write_text(json.dumps(_api_document(2)), encoding="utf-8")
            second = repository.refresh()
            update_bytes = b"".join(response.fp.readline() for _ in range(4))
            connection.close()
            cursor, update = validate_sse_revision_sequence(
                (cursor_bytes, update_bytes)
            )
            sse_ok = (
                response.status == 200
                and cursor.event == "stream.cursor"
                and cursor.revision == first.revision
                and update.event == "projection.updated"
                and update.revision == second.revision
                and second.revision == first.revision + 1
            )
            checks.add(
                "sse_revision_shape",
                sse_ok,
                "frames SSE nommés et révisions strictement monotones"
                if sse_ok
                else "frames SSE ou révisions invalides",
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            checks.add("rest_overview_list_detail", False, f"serveur local: {type(error).__name__}")
            checks.add("sse_revision_shape", False, f"serveur local: {type(error).__name__}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def _load_product_overview(root: Path) -> Mapping[str, object]:
    fixture = (root / "ui" / "fixtures" / "console-dev.json").resolve()
    repository = ProjectionRepository(
        [parse_source_spec(f"simulation:demo:file://{fixture}")]
    )
    return repository.refresh().to_dict()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Prérequis du gate complet: Node.js 24 LTS, npm ci --prefix ui, "
            "npm run build --prefix ui et Helm."
        ),
    )
    parser.add_argument(
        "--ui-dist",
        required=True,
        type=Path,
        help="bundle Vite de production à contrôler",
    )
    args = parser.parse_args(argv)
    bundle = args.ui_dist if args.ui_dist.is_absolute() else ROOT / args.ui_dist
    result = verify_vertical(
        overview=_load_product_overview(ROOT),
        production_bundle=bundle,
        root=ROOT,
    )
    for check in result.checks:
        state = "PASS" if check.passed else "FAIL"
        print(f"{state} {check.id}: {check.detail}")
    for prerequisite in result.prerequisites:
        print(f"PREREQUISITE {prerequisite}")
    print(
        "RESULT "
        + (
            f"PASS ({len(result.checks)} contrôles)"
            if result.ok
            else f"FAIL ({len(result.failures)} échec(s), {len(result.prerequisites)} prérequis manquant(s))"
        )
    )
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
