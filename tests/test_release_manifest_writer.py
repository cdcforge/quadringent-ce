"""Le workflow émet un manifeste que l'installateur peut consommer tel quel."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from quadringent.installer.manifest import ReleaseManifest
from write_release_manifest import write_release_manifest


def test_release_manifest_is_directly_installable(tmp_path: Path) -> None:
    repository = "ghcr.io/example/quadringent"
    digests = ["sha256:" + digit * 64 for digit in "123"]
    manifest_path = tmp_path / "release-manifest.json"
    images_path = tmp_path / "images.json"

    write_release_manifest(
        repository=repository,
        capture=digests[0],
        control_plane=digests[1],
        verifier=digests[2],
        version="0.2.0",
        manifest_path=manifest_path,
        images_path=images_path,
    )

    raw = json.loads(manifest_path.read_text())
    schema = json.loads(Path("deploy/release-manifest.schema.json").read_text())
    jsonschema.validate(raw, schema)
    parsed = ReleaseManifest.from_file(manifest_path)
    assert parsed.repository == repository
    assert (parsed.image_digest, parsed.control_plane_image_digest,
            parsed.verifier_image_digest, parsed.observability_image_digest) == (
        digests[0], digests[1], digests[2], digests[2],
    )
    assert json.loads(images_path.read_text())["images"]["capture"] == f"{repository}@{digests[0]}"


@pytest.mark.parametrize("repository,digest", [
    ("ghcr.io/example/quadringent:latest", "sha256:" + "1" * 64),
    ("ghcr.io/example/quadringent", "latest"),
])
def test_release_manifest_rejects_mutable_or_invalid_images(
    tmp_path: Path, repository: str, digest: str,
) -> None:
    with pytest.raises(ValueError):
        write_release_manifest(
            repository=repository,
            capture=digest,
            control_plane="sha256:" + "2" * 64,
            verifier="sha256:" + "3" * 64,
            version="0.2.0",
            manifest_path=tmp_path / "release-manifest.json",
            images_path=tmp_path / "images.json",
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("owner", ("CdcForge", "Example"))
def test_community_publication_targets_only_the_fresh_runtime_package(tmp_path, owner):
    import os
    import subprocess
    import yaml

    workflow = yaml.safe_load(Path(".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["images"]["steps"]
    registry = next(step for step in steps if step.get("id") == "registry")
    environment_file, output_file = tmp_path / "environment", tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-e", "-c", registry["run"]],
        env={**os.environ, "GITHUB_REPOSITORY_OWNER": owner,
             "GITHUB_ENV": str(environment_file), "GITHUB_OUTPUT": str(output_file)},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    repository = f"ghcr.io/{owner.lower()}/quadringent-ce-runtime"
    assert environment_file.read_text().splitlines() == [f"IMAGE={repository}"]
    assert output_file.read_text().splitlines() == [f"image={repository}"]
    # Les builds, promotions, signatures et manifestes consomment cette destination.
    builds = [step for step in steps if step.get("uses", "").startswith("docker/build-push-action@")]
    assert len(builds) == 3
    assert all("${{ steps.registry.outputs.image }}" in step["with"]["tags"] for step in builds)
    promote = next(step["run"] for step in steps if "skopeo copy --all" in step.get("run", ""))
    assert '"docker://${IMAGE}:${VERSION}-${component}"' in promote
    manifest = next(step["run"] for step in steps if "write_release_manifest.py" in step.get("run", ""))
    assert '--repository "$IMAGE"' in manifest
    signatures = next(step["run"] for step in steps if "cosign sign" in step.get("run", ""))
    assert signatures.count('"${IMAGE}@') == 3
    digests = ["sha256:" + digit * 64 for digit in "123"]
    manifest_path, images_path = tmp_path / "release-manifest.json", tmp_path / "images.json"
    write_release_manifest(repository=repository, capture=digests[0], control_plane=digests[1],
                           verifier=digests[2], version="0.2.1", manifest_path=manifest_path, images_path=images_path)
    assert ReleaseManifest.from_file(manifest_path).repository == repository
    assert all(value.startswith(repository + "@") for value in json.loads(images_path.read_text())["images"].values())


def test_fresh_package_uses_native_repository_token():
    import yaml

    job = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]
    login = next(step for step in job["steps"] if step.get("uses", "").startswith("docker/login-action@"))
    assert login["with"] == {"registry": "ghcr.io", "username": "${{ github.actor }}",
                             "password": "${{ secrets.GITHUB_TOKEN }}"}
    assert job["permissions"]["packages"] == "write"
