"""Prépare six vues OCI exactes et refuse leur promotion après une altération.

Aucune opération réseau ni reconstruction : seuls les octets locaux sont lus.
Les rapports secrets et les reçus de scan restent dans le répertoire privé CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile

COMPONENTS = ("capture", "control-plane", "verifier")
ARCHITECTURES = ("amd64", "arm64")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
FORBIDDEN = {".pyc", ".pyo", ".key", ".pem", ".p8", ".p12", ".pfx", ".jks", ".kdb", ".crt", ".cer"}
METADATA_CHUNK_BYTES = 64 * 1024


def file_signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        before = file_signature(os.fstat(source.fileno()))
        digest = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
        if (before != file_signature(os.fstat(source.fileno()))
                or before != file_signature(path.stat(follow_symlinks=False))):
            raise ValueError("fichier modifié pendant le calcul d'empreinte")
        return digest


def descriptor_path(root: Path, descriptor: dict) -> Path:
    digest = descriptor["digest"]
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("digest OCI invalide")
    path = root / "blobs" / "sha256" / digest[7:]
    if path.is_symlink() or sha256(path) != digest or path.stat().st_size != descriptor["size"]:
        raise ValueError("blob OCI altéré")
    return path


def json_blob(root: Path, descriptor: dict) -> dict:
    return json.loads(descriptor_path(root, descriptor).read_bytes())


def application_files(root: Path, layers: list[dict]) -> set[str]:
    """Applique les whiteouts sans extraire ni exécuter le système de fichiers."""
    files: set[str] = set()
    for layer in layers:
        with tarfile.open(descriptor_path(root, layer), "r:*") as archive:
            members = [(PurePosixPath(member.name), member) for member in archive]
        # Les whiteouts retirent seulement les entrées héritées des couches précédentes.
        for path, _ in members:
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("chemin de couche OCI invalide")
            if path.name.startswith(".wh."):
                target = str(path.parent if path.name == ".wh..wh..opq"
                             else path.parent / path.name.removeprefix(".wh."))
                files = {name for name in files if name != target and not name.startswith(target + "/")}
        for path, member in members:
            if (path.parts and path.parts[0] == "app" and not path.name.startswith(".wh.")
                    and (member.isfile() or member.issym() or member.islnk())):
                files.add(str(path))
    return files


def stage_layer_secrets(root: Path, staging: Path, layer: dict) -> None:
    """Copie chaque entrée séparément : aucun whiteout ni lien n'est appliqué.

    Les noms neutres évitent les exclusions par chemin du scanner. Les noms et
    cibles des liens sont eux-mêmes scannés dans les métadonnées textuelles.
    """
    source = descriptor_path(root, layer)
    directory = staging / source.name
    if directory.exists():
        return  # Une couche partagée entre architectures n'a besoin que d'un scan.
    directory.mkdir(mode=0o700)
    metadata = None
    chunk, chunk_size = 0, 0
    try:
        with tarfile.open(source, "r:*") as archive:
            for number, member in enumerate(archive):
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("chemin de couche OCI invalide")
                record = (json.dumps({"entry": number, "name": member.name, "linkname": member.linkname,
                                      "pax_headers": member.pax_headers}) + "\n").encode("utf-8")
                if len(record) > METADATA_CHUNK_BYTES:
                    raise ValueError("métadonnée de couche trop grande pour le scan")
                if metadata is None or chunk_size + len(record) > METADATA_CHUNK_BYTES:
                    if metadata is not None:
                        metadata.close()
                    metadata = open(directory / f"metadata-{chunk:06d}.jsonl", "xb", opener=private_file)
                    chunk += 1
                    chunk_size = 0
                metadata.write(record)
                chunk_size += len(record)
                if member.isfile():
                    payload = archive.extractfile(member)
                    if payload is None:
                        raise ValueError("contenu de couche OCI absent")
                    with payload, open(directory / f"{number:06d}.payload.txt", "xb", opener=private_file) as target:
                        shutil.copyfileobj(payload, target)
    finally:
        if metadata is not None:
            metadata.close()


def private_file(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def prepare(root: Path, expected_digest: str, revision: str) -> dict[str, str]:
    """Vérifie l'index puis crée deux vues monoarchitecture avec les mêmes blobs."""
    top = json.loads((root / "index.json").read_text())
    if json.loads((root / "oci-layout").read_text()) != {"imageLayoutVersion": "1.0.0"}:
        raise ValueError("layout OCI invalide")
    if len(top["manifests"]) != 1 or top["manifests"][0]["digest"] != expected_digest:
        raise ValueError("digest de build différent du layout")
    index = json_blob(root, top["manifests"][0])
    descriptors = index["manifests"]
    platforms = [(item["platform"]["os"], item["platform"]["architecture"]) for item in descriptors]
    if len(platforms) != 2 or set(platforms) != {("linux", arch) for arch in ARCHITECTURES}:
        raise ValueError("index OCI doit contenir exactement amd64 et arm64")
    staging = root.with_name(root.name + "-secret-files")
    staging.mkdir(mode=0o700)
    with open(staging / "layout-index.json", "xb", opener=private_file) as target:
        target.write((root / "index.json").read_bytes())
    variants = {}
    for descriptor in descriptors:
        arch = descriptor["platform"]["architecture"]
        manifest = json_blob(root, descriptor)
        config = json_blob(root, manifest["config"])
        if config.get("os") != "linux" or config.get("architecture") != arch:
            raise ValueError("configuration OCI hors plateforme")
        if config.get("config", {}).get("Labels", {}).get("org.opencontainers.image.revision") != revision:
            raise ValueError("révision OCI différente du SHA à publier")
        forbidden = [path for path in application_files(root, manifest["layers"])
                     if PurePosixPath(path).suffix.lower() in FORBIDDEN or PurePosixPath(path).name.startswith(".env")]
        if forbidden:
            # Aucune valeur, aucun nom privé dans le log du workflow.
            raise ValueError("fichiers applicatifs interdits dans une variante OCI")
        for item, suffix in ((top["manifests"][0], "index"), (descriptor, "manifest"),
                             (manifest["config"], "config")):
            original = descriptor_path(root, item)
            staged = staging / f"{original.name}.{suffix}.json"
            if not staged.exists():
                with open(staged, "xb", opener=private_file) as target:
                    target.write(original.read_bytes())
        for layer in manifest["layers"]:
            stage_layer_secrets(root, staging, layer)
        view = root.with_name(root.name + "-" + arch)
        view.mkdir(mode=0o700)
        (view / "blobs" / "sha256").mkdir(parents=True)
        for item in [descriptor, manifest["config"], *manifest["layers"]]:
            original = descriptor_path(root, item)
            target = view / "blobs" / "sha256" / original.name
            if not target.exists():
                os.link(original, target)
        (view / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
        (view / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [descriptor]}))
        variants[arch] = str(view)
    return variants


def local_files(directory: Path):
    """Un seul stat par entrée pour parcourir les fichiers locaux sans liens."""
    with os.scandir(directory) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("lien symbolique dans un layout local")
            if stat.S_ISDIR(info.st_mode):
                yield from local_files(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                yield Path(entry.path), info
            else:
                raise ValueError("entrée locale hors fichier ou répertoire")


def fingerprints(roots: dict[str, Path]) -> dict[str, str]:
    result = {}
    # Cache strictement local à ce passage : seal/verify relisent les octets.
    cache: dict[tuple[int, int], tuple[tuple[int, ...], str]] = {}
    for component, root in roots.items():
        for directory in [root, *(root.with_name(root.name + "-" + arch) for arch in ARCHITECTURES),
                          root.with_name(root.name + "-secret-files")]:
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("layout local absent ou invalide")
            for path, info in local_files(directory):
                signature = file_signature(info)
                inode = (info.st_dev, info.st_ino)
                cached = cache.get(inode)
                if cached is not None:
                    if cached[0] != signature:
                        raise ValueError("inode modifié pendant le passage d'empreintes")
                    digest = cached[1]
                else:
                    digest = sha256(path)
                if signature != file_signature(path.stat(follow_symlinks=False)):
                    raise ValueError("fichier modifié pendant le passage d'empreintes")
                cache[inode] = signature, digest
                result[f"{component}/{directory.name}/{path.relative_to(directory)}"] = digest
    return result


def _write_snapshot(files: dict[str, str], receipt: Path) -> None:
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump({"files": files}, target, sort_keys=True)


def write_receipt(roots: dict[str, Path], receipt: Path) -> None:
    _write_snapshot(fingerprints(roots), receipt)


def verify_receipt(roots: dict[str, Path], receipt: Path) -> dict[str, str]:
    files = fingerprints(roots)
    if files != json.loads(receipt.read_text())["files"]:
        raise ValueError("octets OCI différents des artefacts scannés")
    return files


def verify_scan_identity(view: Path, arch: str, report: dict) -> None:
    ref = json.loads((view / "index.json").read_text())["manifests"][0]
    config = json_blob(view, ref)["config"]
    metadata = report.get("Metadata", {})
    observed = metadata.get("ImageConfig", {})
    if (metadata.get("ImageID") != config["digest"]
            or observed.get("os") != "linux" or observed.get("architecture") != arch):
        raise ValueError("rapport de scan différent de la variante attendue")


MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_METADATA_TOTAL_BYTES = 42 * 1024 * 1024


def read_metadata(path: Path, maximum: int = MAX_METADATA_BYTES) -> bytes:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("lien dans une métadonnée locale")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("métadonnée hors type ou limite")
        raw = source.read(maximum + 1)
        if (len(raw) > maximum or file_signature(before) != file_signature(os.fstat(source.fileno()))
                or file_signature(before) != file_signature(path.stat(follow_symlinks=False))):
            raise ValueError("métadonnée modifiée pendant la lecture")
        return raw


def metadata_selection(root: Path, files: dict[str, str], revision: str, version: str,
                       repository: str) -> dict[str, bytes]:
    """Sélectionne les 21 originaux scellés, jamais une couche ou une reconstruction."""
    try:
        from .check_publication import scan_text
    except ImportError:
        from check_publication import scan_text
    if (not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)):
        raise ValueError("identité de publication invalide")
    selected: dict[str, bytes] = {}
    def checked(component, relative):
        name = f"{component}/{relative}"
        raw = read_metadata(root / name)
        if files.get(f"{component}/{name}") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise ValueError("métadonnée différente du reçu scellé")
        # Les diagnostics ne contiennent jamais le texte ni les valeurs détectées.
        if scan_text(raw.decode("utf-8"), "metadata"):
            raise ValueError("identité privée dans les métadonnées OCI")
        selected[name] = raw
        if sum(map(len, selected.values())) > MAX_METADATA_TOTAL_BYTES:
            raise ValueError("export de métadonnées trop grand")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("clé JSON OCI dupliquée")
                result[key] = value
            return result
        value = json.loads(raw, object_pairs_hook=unique)
        if scan_text(json.dumps(value, ensure_ascii=False), "metadata"):
            raise ValueError("identité privée dans les métadonnées OCI")
        return value
    def blob(component, descriptor, media):
        digest = descriptor.get("digest", "")
        if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                or type(descriptor.get("size")) is not int or descriptor["size"] < 0
                or descriptor.get("mediaType") != media):
            raise ValueError("descripteur de métadonnée OCI invalide")
        name = f"blobs/sha256/{digest[7:]}"
        value = checked(component, name)
        raw = selected[f"{component}/{name}"]
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest or len(raw) != descriptor["size"]:
            raise ValueError("octets de descripteur OCI différents")
        return value
    for component in COMPONENTS:
        if checked(component, "oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("layout OCI invalide")
        top = checked(component, "index.json")
        if top.get("schemaVersion") != 2 or len(top.get("manifests", [])) != 1:
            raise ValueError("wrapper OCI invalide")
        index = blob(component, top["manifests"][0], "application/vnd.oci.image.index.v1+json")
        if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 2:
            raise ValueError("index OCI invalide")
        seen = set()
        for item in index["manifests"]:
            platform = item.get("platform", {})
            arch = platform.get("architecture")
            if platform.get("os") != "linux" or arch not in ARCHITECTURES or arch in seen:
                raise ValueError("architectures OCI invalides")
            seen.add(arch)
            manifest = blob(component, item, "application/vnd.oci.image.manifest.v1+json")
            if manifest.get("schemaVersion") != 2 or not isinstance(manifest.get("layers"), list):
                raise ValueError("manifeste OCI invalide")
            config = blob(component, manifest["config"], "application/vnd.oci.image.config.v1+json")
            labels = config.get("config", {}).get("Labels", {})
            if (config.get("os") != "linux" or config.get("architecture") != arch
                    or labels.get("org.opencontainers.image.revision") != revision
                    or labels.get("org.opencontainers.image.version") != version
                    or labels.get("org.opencontainers.image.source") != "https://github.com/" + repository):
                raise ValueError("identité ou configuration OCI divergente")
            for reference in [item, manifest["config"], *manifest["layers"]]:
                digest = reference.get("digest", "")
                if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                    raise ValueError("référence OCI invalide")
                relative = f"blobs/sha256/{digest[7:]}"
                if (files.get(f"{component}/{component}/{relative}") != digest
                        or files.get(f"{component}/{component}-{arch}/{relative}") != digest):
                    raise ValueError("référence absente des variantes scellées")
    if len(selected) != 21:
        raise ValueError("export OCI doit contenir exactement 21 originaux")
    return selected


def export_metadata(root: Path, receipt: Path, output: Path, revision: str, version: str,
                    repository: str) -> None:
    """Copie privée bornée, empreintes comparées avant et après la copie."""
    if (output.exists() or any(p.is_symlink() for p in (output, *output.parents))
            or output.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(output.resolve())):
        raise ValueError("destination d'export invalide ou déjà présente")
    receipt_raw = read_metadata(receipt, 64 * 1024 * 1024)
    proof = json.loads(receipt_raw)
    if set(proof) != {"files"} or not isinstance(proof["files"], dict):
        raise ValueError("reçu OCI invalide")
    selected = metadata_selection(root, proof["files"], revision, version, repository)
    output.mkdir(mode=0o700)
    for name, raw in selected.items():
        destination = output / name
        for parent in reversed(destination.parents):
            if parent == output or parent.is_relative_to(output):
                parent.mkdir(mode=0o700, exist_ok=True)
        with open(destination, "xb", opener=private_file) as target:
            target.write(raw)
    # Le reçu et les sources sont relus, sans cache ni hash des couches.
    if read_metadata(receipt, 64 * 1024 * 1024) != receipt_raw:
        raise ValueError("reçu modifié pendant l'export")
    if metadata_selection(root, proof["files"], revision, version, repository) != selected:
        raise ValueError("sources modifiées pendant l'export")
    found = {str(path.relative_to(output)): read_metadata(path) for path, _ in local_files(output)}
    if found != selected:
        raise ValueError("copie OCI différente des originaux scellés")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "seal", "verify", "export-metadata"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--revision")
    for component in COMPONENTS:
        parser.add_argument("--" + component + "-digest")
    parser.add_argument("--private-reports", type=Path)
    parser.add_argument("--public-reports", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--repository")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    roots = {component: args.root / component for component in COMPONENTS}
    if args.mode == "prepare":
        if not args.revision:
            parser.error("révision obligatoire")
        for component, root in roots.items():
            digest = getattr(args, component.replace("-", "_") + "_digest")
            if digest is None:
                parser.error("trois digests obligatoires")
            prepare(root, digest, args.revision)
        write_receipt(roots, args.root / "prepared.json")
    elif args.mode == "seal":
        verified = verify_receipt(roots, args.root / "prepared.json")
        if not all((args.private_reports, args.public_reports, args.version)):
            parser.error("rapports de scan et version obligatoires")
        for component in COMPONENTS:
            for arch in ARCHITECTURES:
                secret = args.private_reports / f"trivy-{component}-{arch}-secrets.json"
                report = json.loads(secret.read_text())
                verify_scan_identity(args.root / f"{component}-{arch}", arch, report)
                if any(result.get("Secrets") for result in report.get("Results", [])):
                    raise ValueError("scan secret non passant")
                suffix = "" if arch == "amd64" else "-arm64"
                sbom = args.public_reports / f"quadringent-{args.version}-{component}{suffix}.spdx.json"
                if not sbom.is_file() or not json.loads(sbom.read_text()).get("spdxVersion"):
                    raise ValueError("SBOM manquant")
            if not (args.public_reports / f"trivy-{component}.sarif").is_file():
                raise ValueError("rapport de vulnérabilités manquant")
            arm64 = json.loads((args.private_reports / f"trivy-{component}-arm64-vuln.json").read_text())
            verify_scan_identity(args.root / f"{component}-arm64", "arm64", arm64)
            layers = json.loads((args.private_reports / f"trivy-{component}-layers-secrets.json").read_text())
            if any(result.get("Secrets") for result in layers.get("Results", [])):
                raise ValueError("scan secret des couches non passant")
        # Seul le snapshot comparé à prepared est scellé ; verify relira avant push.
        _write_snapshot(verified, args.receipt)
    elif args.mode == "export-metadata":
        if not all((args.revision, args.version, args.repository, args.output)):
            parser.error("identité et destination de métadonnées obligatoires")
        export_metadata(args.root, args.receipt, args.output, args.revision, args.version, args.repository)
    else:
        verify_receipt(roots, args.receipt)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, AttributeError, OSError, tarfile.TarError):
        raise SystemExit("artefact OCI ou preuve de scan invalide") from None
