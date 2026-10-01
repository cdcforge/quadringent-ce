"""Produit le manifeste d'images directement consommé par ``quadringent install``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

_DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def write_release_manifest(
    *,
    repository: str,
    capture: str,
    control_plane: str,
    verifier: str,
    version: str,
    manifest_path: Path,
    images_path: Path,
) -> None:
    if not repository or "@" in repository or ":" in repository.rsplit("/", 1)[-1]:
        raise ValueError("repository doit être un dépôt OCI sans digest ni tag")
    for name, digest in (("capture", capture), ("control_plane", control_plane), ("verifier", verifier)):
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"digest {name} invalide")

    manifest = {
        "repository": repository,
        "imageDigest": capture,
        "controlPlaneImageDigest": control_plane,
        "verifierImageDigest": verifier,
        "observabilityImageDigest": verifier,
    }
    images = {
        "version": version,
        "images": {
            "capture": f"{repository}@{capture}",
            "control_plane": f"{repository}@{control_plane}",
            "verifier": f"{repository}@{verifier}",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    images_path.write_text(json.dumps(images, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--control-plane", required=True)
    parser.add_argument("--verifier", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--manifest", type=Path, default=Path("release-manifest.json"))
    parser.add_argument("--images", type=Path, default=Path("images.json"))
    args = parser.parse_args()
    write_release_manifest(
        repository=args.repository,
        capture=args.capture,
        control_plane=args.control_plane,
        verifier=args.verifier,
        version=args.version,
        manifest_path=args.manifest,
        images_path=args.images,
    )


if __name__ == "__main__":
    main()
