"""Dérive les métadonnées de release de la seule version éditable."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import tomllib


def synchronize(root: Path, *, check: bool) -> list[str]:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version) is None:
        raise ValueError("Version stable x.y.z requise")
    changes: dict[str, str] = {}
    for filename in ("ui/package.json", "ui/package-lock.json"):
        payload = json.loads((root / filename).read_text())
        payload["version"] = version
        if "packages" in payload:
            payload["packages"][""]["version"] = version
        changes[filename] = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    chart = (root / "chart/Chart.yaml").read_text()
    chart = re.sub(r"(?m)^version:.*$", f"version: {version}", chart)
    changes["chart/Chart.yaml"] = re.sub(r"(?m)^appVersion:.*$", f'appVersion: "{version}"', chart)
    java = (root / "java/pom.xml").read_text()
    changes["java/pom.xml"] = re.sub(r"<version>[^<]+</version>", f"<version>{version}</version>", java, count=1)
    changes["ui/src/version.ts"] = f"// Généré par scripts/sync_version.py depuis pyproject.toml.\nexport const productVersion = '{version}';\n"
    changed = []
    for filename, content in changes.items():
        path = root / filename
        if not path.exists() or path.read_text() != content:
            changed.append(filename)
            if not check:
                path.write_text(content)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--release", help="Tag vX.Y.Z devant correspondre à pyproject.toml")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    canonical = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if args.release is not None and args.release != "v" + canonical:
        parser.error("Le tag doit correspondre exactement à la version de pyproject.toml")
    changed = synchronize(root, check=args.check)
    print(json.dumps({"version": canonical, "different": changed}))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
