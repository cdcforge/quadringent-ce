"""Garanties hors ligne : six variantes exactes avant le premier upload OCI."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import patch

import pytest
import yaml


def blob(root, data):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    path = root / "blobs" / "sha256" / digest[7:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"digest": digest, "size": len(data)}


def document(root, data, media):
    return {**blob(root, json.dumps(data, separators=(",", ":")).encode()), "mediaType": media}


def fixture_layout(root, forbidden_arch=None, labels=None):
    root.mkdir(parents=True)
    manifests = []
    for arch in ("amd64", "arm64"):
        config = document(root, {"architecture": arch, "os": "linux",
                                "config": {"Labels": {"org.opencontainers.image.revision": "a" * 40,
                                "org.opencontainers.image.version": "0.2.2",
                                "org.opencontainers.image.source": "https://github.com/example/quadringent", **(labels or {})}}},
                          "application/vnd.oci.image.config.v1+json")
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as layer:
            path = f"app/{arch}.pem" if arch == forbidden_arch else f"app/{arch}.txt"
            data = b"synthetic-public-marker"
            member = tarfile.TarInfo(path)
            member.size = len(data)
            layer.addfile(member, io.BytesIO(data))
        layer = {**blob(root, gzip.compress(buffer.getvalue(), mtime=0)),
                 "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
        manifest = document(root, {"schemaVersion": 2, "config": config, "layers": [layer]},
                            "application/vnd.oci.image.manifest.v1+json")
        manifests.append({**manifest, "platform": {"os": "linux", "architecture": arch}})
    index = document(root, {"schemaVersion": 2, "manifests": manifests},
                     "application/vnd.oci.image.index.v1+json")
    (root / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [index]}))
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    return index["digest"], manifests


def test_release_builds_local_and_scans_before_any_registry_upload():
    steps = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]["steps"]
    builds = [step for step in steps if step.get("uses", "").startswith("docker/build-push-action@")]
    assert len(builds) == 3
    assert all(step["with"].get("push") is False for step in builds)
    assert all("type=oci" in step["with"]["outputs"] for step in builds)
    login = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("docker/login-action@"))
    seal = next(i for i, step in enumerate(steps) if "release_oci.py seal" in step.get("run", ""))
    assert seal < login
    assert not any("if" in step for step in steps[seal:login + 1])
    assert not any(step.get("uses", "").startswith("docker/build-push-action@") for step in steps[login:])
    promote = next(step["run"] for step in steps if "skopeo copy --all" in step.get("run", ""))
    assert "--preserve-digests" in promote
    assert "release_oci.py verify" in promote


def test_scan_receipts_use_the_same_version_as_all_six_sbom_outputs():
    steps = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]["steps"]
    seal = next(step for step in steps if "release_oci.py seal" in step.get("run", ""))
    arm64 = next(step for step in steps if step.get("name") == "SBOM SPDX — variantes arm64")
    assert seal["env"]["VERSION"] == arm64["env"]["VERSION"] == "${{ steps.version.outputs.version }}"


def test_scan_receipt_is_separate_from_public_release_assets():
    jobs = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]
    steps = jobs["images"]["steps"]
    upload = next(step for step in steps if step.get("with", {}).get("name") == "oci-scan-receipt")
    seal = next(i for i, step in enumerate(steps) if "release_oci.py seal" in step.get("run", ""))
    login = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("docker/login-action@"))
    assert seal < steps.index(upload) < login
    assert "if" not in upload
    assert upload["with"]["path"] == "${{ runner.temp }}/oci/passed.json"
    assert upload["with"]["retention-days"] == 1
    assert upload["with"]["if-no-files-found"] == "error"
    downloads = [step["with"] for step in jobs["release"]["steps"]
                 if step.get("uses", "").startswith("actions/download-artifact@")]
    assert downloads == [{"name": "sbom-rapports-trivy", "path": "supply-chain"},
                         {"name": "release-packages", "path": "packages"}]
    attachment = next(step["with"]["files"] for step in jobs["release"]["steps"]
                      if step.get("uses", "").startswith("softprops/action-gh-release@"))
    assert set(attachment.split()) == {"supply-chain/*", "packages/**/*.whl",
                                       "packages/**/*.tar.gz", "packages/*.tgz"}


def test_prepared_variants_are_distinct_exact_manifests(tmp_path):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, manifests = fixture_layout(root)
    variants = release_oci.prepare(root, digest, "a" * 40)
    assert set(variants) == {"amd64", "arm64"}
    for arch, manifest in zip(("amd64", "arm64"), manifests, strict=True):
        view = Path(variants[arch])
        assert json.loads((view / "index.json").read_text())["manifests"][0]["digest"] == manifest["digest"]
        assert (view / "blobs" / "sha256" / manifest["digest"][7:]).read_bytes() == (
            root / "blobs" / "sha256" / manifest["digest"][7:]).read_bytes()


@pytest.mark.parametrize("failure", ["digest", "revision", "content"])
def test_unverified_content_cannot_reach_the_scans(tmp_path, failure):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, _ = fixture_layout(root, forbidden_arch="arm64" if failure == "content" else None)
    with pytest.raises(ValueError):
        release_oci.prepare(root, "sha256:" + "b" * 64 if failure == "digest" else digest,
                            "c" * 40 if failure == "revision" else "a" * 40)


@pytest.mark.parametrize("changed_input", ["oci", "layer_scan", "layer_metadata"])
def test_changed_blob_after_scan_cannot_be_promoted(tmp_path, changed_input):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, _ = fixture_layout(root)
    variants = release_oci.prepare(root, digest, "a" * 40)
    receipt = tmp_path / "passed.json"
    release_oci.write_receipt({"capture": root}, receipt)
    path = next((Path(variants["arm64"]) / "blobs" / "sha256").iterdir())
    if changed_input == "layer_scan":
        path = next(root.with_name(root.name + "-secret-files").rglob("*.payload.txt"))
    if changed_input == "layer_metadata":
        path = next(root.with_name(root.name + "-secret-files").rglob("metadata-*.jsonl"))
    path.write_bytes(path.read_bytes() + b"altered-after-scan")
    with pytest.raises(ValueError):
        release_oci.verify_receipt({"capture": root}, receipt)


@pytest.mark.parametrize("mismatch", ("platform", "config"))
def test_a_report_from_another_variant_cannot_seal_the_gate(tmp_path, mismatch):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, _ = fixture_layout(root)
    variants = release_oci.prepare(root, digest, "a" * 40)
    manifest_ref = json.loads((Path(variants["arm64"]) / "index.json").read_text())["manifests"][0]
    manifest = release_oci.json_blob(root, manifest_ref)
    report = {"Metadata": {"ImageID": manifest["config"]["digest"],
                           "ImageConfig": {"architecture": "arm64", "os": "linux"}}}
    if mismatch == "platform":
        report["Metadata"]["ImageConfig"]["architecture"] = "amd64"
    else:
        report["Metadata"]["ImageID"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError):
        release_oci.verify_scan_identity(Path(variants["arm64"]), "arm64", report)


def test_removed_file_remains_in_private_layer_secret_scan(tmp_path):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, manifests = fixture_layout(root)
    descriptor = manifests[1]
    manifest = release_oci.json_blob(root, descriptor)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        archive.addfile(tarfile.TarInfo("app/.wh.arm64.txt"))
    manifest["layers"].append({**blob(root, gzip.compress(payload.getvalue(), mtime=0)),
                               "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"})
    manifests[1] = {**document(root, manifest, descriptor["mediaType"]), "platform": descriptor["platform"]}
    index = document(root, {"schemaVersion": 2, "manifests": manifests},
                     "application/vnd.oci.image.index.v1+json")
    (root / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [index]}))
    release_oci.prepare(root, index["digest"], "a" * 40)
    assert "app/arm64.txt" not in release_oci.application_files(root, manifest["layers"])
    staged = root.with_name(root.name + "-secret-files")
    deleted_payload = staged / manifest["layers"][0]["digest"][7:] / "000000.payload.txt"
    assert deleted_payload.read_bytes() == b"synthetic-public-marker"
    assert deleted_payload.stat().st_mode & 0o777 == 0o600
    assert list(staged.glob("*.config.json"))


def test_layer_links_are_scanned_as_metadata_and_never_extracted(tmp_path):
    from scripts import release_oci
    root, staging = tmp_path / "capture", tmp_path / "staged"
    staging.mkdir(mode=0o700)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("app/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/outside/synthetic-public-marker"
        archive.addfile(member)
    descriptor = blob(root, gzip.compress(buffer.getvalue(), mtime=0))
    release_oci.stage_layer_secrets(root, staging, descriptor)
    header = next(staging.rglob("metadata-*.jsonl"))
    assert json.loads(header.read_text())["linkname"] == member.linkname
    assert not list(staging.rglob("*.payload.txt"))
    assert not any(path.is_symlink() for path in staging.rglob("*"))


def test_layer_metadata_chunks_keep_all_headers_and_flat_payloads(tmp_path):
    from scripts import release_oci
    root, staging = tmp_path / "capture", tmp_path / "staged"
    staging.mkdir(mode=0o700)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for number in range(800):
            member = tarfile.TarInfo(f"app/nested/{number}.txt")
            member.pax_headers = {"comment": "synthetic-public-marker-" + str(number) + "x" * 80}
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    descriptor = blob(root, gzip.compress(buffer.getvalue(), mtime=0))
    release_oci.stage_layer_secrets(root, staging, descriptor)
    directory = staging / descriptor["digest"][7:]
    chunks = sorted(directory.glob("metadata-*.jsonl"))
    assert len(chunks) > 1
    assert all(path.stat().st_size <= 64 * 1024 and path.stat().st_mode & 0o777 == 0o600 for path in chunks)
    records = [json.loads(line) for path in chunks for line in path.read_text().splitlines()]
    assert [record["entry"] for record in records] == list(range(800))
    assert [record["name"] for record in records] == [f"app/nested/{number}.txt" for number in range(800)]
    assert all(record["pax_headers"]["comment"].startswith("synthetic-public-marker-") for record in records)
    assert not any(path.is_dir() for path in directory.iterdir())
    assert len(list(directory.glob("*.payload.txt"))) == 800
    assert all(path.read_bytes() == b"x" for path in directory.glob("*.payload.txt"))


def test_oversized_single_metadata_record_is_refused(tmp_path):
    from scripts import release_oci
    root, staging = tmp_path / "capture", tmp_path / "staged"
    staging.mkdir(mode=0o700)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("app/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "synthetic-public-marker"
        member.pax_headers = {"comment": "x" * (64 * 1024)}
        archive.addfile(member)
    descriptor = blob(root, gzip.compress(buffer.getvalue(), mtime=0))
    with pytest.raises(ValueError):
        release_oci.stage_layer_secrets(root, staging, descriptor)


def test_fingerprint_hashes_hardlinked_inode_once_per_fresh_pass(tmp_path):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, _ = fixture_layout(root)
    release_oci.prepare(root, digest, "a" * 40)
    with patch.object(release_oci, "sha256", wraps=release_oci.sha256) as hashed:
        first = release_oci.fingerprints({"capture": root})
        calls = len(hashed.call_args_list)
        inodes = {(call.args[0].stat().st_dev, call.args[0].stat().st_ino) for call in hashed.call_args_list}
        assert calls == len(inodes)
        assert release_oci.fingerprints({"capture": root}) == first
        assert len(hashed.call_args_list) == 2 * calls


def test_file_changed_during_hash_is_refused(tmp_path):
    from scripts import release_oci
    path = tmp_path / "payload.txt"
    path.write_bytes(b"synthetic-public-marker")
    original = hashlib.file_digest
    def mutate(source, algorithm):
        result = original(source, algorithm)
        path.write_bytes(b"altered-during-hash")
        return result
    with patch.object(hashlib, "file_digest", side_effect=mutate), pytest.raises(ValueError):
        release_oci.sha256(path)


def test_inode_cache_requires_fresh_signature_for_each_alias(tmp_path):
    from scripts import release_oci
    root = tmp_path / "capture"
    digest, manifests = fixture_layout(root)
    release_oci.prepare(root, digest, "a" * 40)
    shared = root / "blobs" / "sha256" / manifests[0]["digest"][7:]
    original = os.scandir
    def mutate_before_alias(directory):
        if Path(directory) == root.with_name(root.name + "-amd64"):
            shared.write_bytes(shared.read_bytes() + b"altered-before-cache-reuse")
        return original(directory)
    with patch.object(os, "scandir", side_effect=mutate_before_alias), pytest.raises(ValueError):
        release_oci.fingerprints({"capture": root})


@pytest.mark.parametrize("failure_target", ["verifier-arm64", "verifier-secret-files", None])
def test_six_local_scans_gate_login_and_promotion(tmp_path, failure_target):
    from scripts import release_oci
    steps = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]["steps"]
    private, public, tools = (tmp_path / name for name in ("private", "public", "tools"))
    for directory in (private, public, tools):
        directory.mkdir()
    roots = {name: private / "oci" / name for name in release_oci.COMPONENTS}
    digests = {}
    for name, root in roots.items():
        digest, _ = fixture_layout(root)
        digests[name] = digest
        release_oci.prepare(root, digest, "a" * 40)
    release_oci.write_receipt(roots, private / "oci" / "prepared.json")
    trace = private / "calls.jsonl"
    executable = tools / "trivy"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
image=args[args.index('--input')+1] if '--input' in args else args[-1]
scanner=args[args.index('--scanners')+1] if '--scanners' in args else 'sbom'
output=Path(args[args.index('--output')+1])
with open(os.environ['TRACE'], 'a') as target:
 target.write(json.dumps({'scanner':scanner,'input':image})+'\\n')
failure=scanner=='secret' and bool(os.environ['FAIL_SECRET']) and image.endswith(os.environ['FAIL_SECRET'])
data={'Results':[{'Secrets':[{'RuleID':'synthetic'}]}]} if failure else {'Results':[]}
if args[0]=='image':
 root=Path(image)
 manifest_ref=json.loads((root/'index.json').read_text())['manifests'][0]
 manifest=json.loads((root/'blobs'/'sha256'/manifest_ref['digest'][7:]).read_text())
 config=json.loads((root/'blobs'/'sha256'/manifest['config']['digest'][7:]).read_text())
 data['Metadata']={'ImageID':manifest['config']['digest'],'ImageConfig':config}
if scanner=='sbom': data={'spdxVersion':'SPDX-2.3'}
output.write_text(json.dumps(data))
sys.exit(37 if failure else 0)
''')
    executable.chmod(0o700)
    skopeo = tools / "skopeo"
    skopeo.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
root=Path(next(value.removeprefix('oci:') for value in args if value.startswith('oci:')))
digest=json.loads((root/'index.json').read_text())['manifests'][0]['digest']
Path(args[args.index('--digestfile')+1]).write_text(digest+'\\n')
with open(os.environ['TRACE'],'a') as target: target.write(json.dumps({'operation':'push'})+'\\n')
''')
    skopeo.chmod(0o700)
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "TRACE": str(trace),
           "FAIL_SECRET": failure_target or "", "RUNNER_TEMP": str(private),
           "GITHUB_WORKSPACE": str(public), "GITHUB_STEP_SUMMARY": str(private / "summary"),
           "VERSION": "v0.2.0", "IMAGE": "ghcr.io/example/synthetic", "DOCKER_CONFIG": str(private),
           "CAPTURE_IMAGE": str(roots["capture"]), "CONTROL_PLANE_IMAGE": str(roots["control-plane"]),
           "VERIFIER_IMAGE": str(roots["verifier"]), "CAPTURE_DIGEST": digests["capture"],
           "CONTROL_PLANE_DIGEST": digests["control-plane"], "VERIFIER_DIGEST": digests["verifier"]}
    # Les actions SARIF utilisent déjà les trois vues locales monoarchitecture.
    for name in roots:
        subprocess.run([str(executable), "image", "--input", str(roots[name]) + "-amd64",
                        "--scanners", "vuln", "--output", str(public / f"trivy-{name}.sarif")],
                       env=env, check=True)
    gate = next(step["run"] for step in steps if "--scanners secret" in step.get("run", ""))
    result = subprocess.run(["bash", "-e", "-c", gate], cwd=public, env=env, capture_output=True)
    if failure_target:
        assert result.returncode == 37
        assert not (private / "oci" / "passed.json").exists()
    else:
        assert result.returncode == 0
        for name in roots:
            for arch in release_oci.ARCHITECTURES:
                suffix = "" if arch == "amd64" else "-arm64"
                output = public / f"quadringent-v0.2.0-{name}{suffix}.spdx.json"
                subprocess.run([str(executable), "image", "--input", str(roots[name]) + "-" + arch,
                                "--output", str(output)], env=env, check=True)
        seal = next(step["run"] for step in steps if "release_oci.py seal" in step.get("run", ""))
        promote = next(step["run"] for step in steps if "skopeo copy --all" in step.get("run", ""))
        repo = Path.cwd()
        for script in (seal, promote):
            script = script.replace("python3 scripts/release_oci.py", f'"{sys.executable}" "{repo}/scripts/release_oci.py"')
            if "skopeo copy" in script:
                with trace.open("a") as target:
                    target.write('{"operation":"login"}\n')
            result = subprocess.run(["bash", "-e", "-c", script], cwd=public, env=env, capture_output=True)
            assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    if failure_target:
        assert not any(call.get("operation") in {"push", "login"} for call in calls)
    if not failure_target:
        secret_inputs = {call["input"] for call in calls if call.get("scanner") == "secret"}
        assert secret_inputs == {str(root) + "-" + arch for root in roots.values() for arch in release_oci.ARCHITECTURES} | {
            str(root) + "-secret-files" for root in roots.values()}
        login = next(i for i, call in enumerate(calls) if call.get("operation") == "login")
        assert all(i < login for i, call in enumerate(calls) if call.get("scanner"))
        assert sum(call.get("operation") == "push" for call in calls) == 3


def seal_fixture(tmp_path):
    from scripts import release_oci
    oci, private, public = (tmp_path / name for name in ("oci", "private", "public"))
    private.mkdir()
    public.mkdir()
    roots = {}
    for component in release_oci.COMPONENTS:
        root = oci / component
        digest, _ = fixture_layout(root)
        release_oci.prepare(root, digest, "a" * 40)
        roots[component] = root
        for arch in release_oci.ARCHITECTURES:
            view = oci / f"{component}-{arch}"
            descriptor = json.loads((view / "index.json").read_text())["manifests"][0]
            manifest = release_oci.json_blob(view, descriptor)
            report = {"Metadata": {"ImageID": manifest["config"]["digest"],
                                   "ImageConfig": {"architecture": arch, "os": "linux"}}, "Results": []}
            (private / f"trivy-{component}-{arch}-secrets.json").write_text(json.dumps(report))
            if arch == "arm64":
                (private / f"trivy-{component}-arm64-vuln.json").write_text(json.dumps(report))
            suffix = "" if arch == "amd64" else "-arm64"
            (public / f"quadringent-v0.2.1-{component}{suffix}.spdx.json").write_text('{"spdxVersion":"SPDX-2.3"}')
        (private / f"trivy-{component}-layers-secrets.json").write_text('{"Results":[]}')
        (public / f"trivy-{component}.sarif").write_text('{"runs":[]}')
    release_oci.write_receipt(roots, oci / "prepared.json")
    passed = oci / "passed.json"
    arguments = ["release_oci.py", "seal", "--root", str(oci), "--receipt", str(passed),
                 "--private-reports", str(private), "--public-reports", str(public), "--version", "v0.2.1"]
    return roots, passed, arguments


def test_seal_does_not_bless_mutation_after_verified_snapshot(tmp_path, monkeypatch):
    from scripts import release_oci
    roots, passed, arguments = seal_fixture(tmp_path)
    payload = next(roots["capture"].with_name("capture-secret-files").rglob("*.payload.txt"))
    original = release_oci.fingerprints
    calls = []

    def mutate_after_snapshot(selected):
        snapshot = original(selected)
        calls.append(snapshot)
        if len(calls) == 1:
            payload.write_bytes(payload.read_bytes() + b"changed-after-verified-snapshot")
        return snapshot

    with patch.object(release_oci, "fingerprints", side_effect=mutate_after_snapshot):
        monkeypatch.setattr(sys, "argv", arguments)
        assert release_oci.main() == 0
    # Le contrôle frais avant promotion doit refuser les octets altérés.
    with pytest.raises(ValueError, match="octets OCI différents"):
        release_oci.verify_receipt(roots, passed)
    assert len(calls) == 1
    assert passed.stat().st_mode & 0o777 == 0o600
    assert json.loads(passed.read_text()) == json.loads(passed.with_name("prepared.json").read_text())


def test_seal_hashes_once_and_verify_uses_a_fresh_pass(tmp_path, monkeypatch):
    from scripts import release_oci
    roots, passed, arguments = seal_fixture(tmp_path)
    monkeypatch.setattr(sys, "argv", arguments)
    with patch.object(release_oci, "fingerprints", wraps=release_oci.fingerprints) as measured:
        assert release_oci.main() == 0
        assert measured.call_count == 1
        release_oci.verify_receipt(roots, passed)
        assert measured.call_count == 2


@pytest.mark.parametrize("existing", ("file", "symlink"))
def test_seal_never_overwrites_an_existing_receipt(tmp_path, monkeypatch, existing):
    from scripts import release_oci
    _, passed, arguments = seal_fixture(tmp_path)
    target = passed.with_name("unrelated.json")
    target.write_text("original")
    if existing == "symlink":
        passed.symlink_to(target)
    else:
        passed.write_text("original")
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(FileExistsError):
        release_oci.main()
    assert passed.read_text() == target.read_text() == "original"


def test_seal_rejects_a_mismatching_prepared_receipt(tmp_path, monkeypatch):
    from scripts import release_oci
    _, passed, arguments = seal_fixture(tmp_path)
    passed.with_name("prepared.json").write_text('{"files":{}}')
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(ValueError, match="octets OCI différents"):
        release_oci.main()
    assert not passed.exists()


def portable_fixture(tmp_path, **kwargs):
    from scripts import release_oci
    root = tmp_path / "oci"
    roots = {}
    for component in release_oci.COMPONENTS:
        roots[component] = root / component
        digest, _ = fixture_layout(roots[component], **kwargs)
        release_oci.prepare(roots[component], digest, "a" * 40)
    receipt = root / "passed.json"
    release_oci.write_receipt(roots, receipt)
    return root, receipt


def test_export_preserves_all_21_original_metadata_bytes_without_layers(tmp_path):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path)
    output = tmp_path / "metadata"
    release_oci.export_metadata(root, receipt, output, "a" * 40, "0.2.2", "example/quadringent")
    files = [path for path in output.rglob("*") if path.is_file()]
    assert len(files) == 21
    for path in files:
        relative = path.relative_to(output)
        assert path.read_bytes() == (root / relative).read_bytes()
        assert path.stat().st_mode & 0o777 == 0o600
    assert (output.stat().st_mode & 0o777) == 0o700
    assert not (output / "passed.json").exists()


@pytest.mark.parametrize("drift", ("revision", "version", "source", "wrapper", "receipt", "symlink", "oversized"))
def test_export_refuses_unscanned_or_invalid_metadata(tmp_path, drift):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path)
    revision, version, repository = "a" * 40, "0.2.2", "example/quadringent"
    if drift == "revision": revision = "b" * 40
    elif drift == "version": version = "0.2.3"
    elif drift == "source": repository = "other/quadringent"
    elif drift == "wrapper": (root / "capture/index.json").write_bytes(b"{}")
    elif drift == "receipt": receipt.write_text('{"files":{}}')
    elif drift == "symlink":
        wrapper = root / "capture/index.json"
        original = tmp_path / "original"
        wrapper.rename(original); wrapper.symlink_to(original)
    else: (root / "capture/index.json").write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        release_oci.export_metadata(root, receipt, tmp_path / "metadata", revision, version, repository)


def test_export_rejects_mutation_during_copy(tmp_path, monkeypatch):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path)
    original = release_oci.private_file
    def mutate_copy(path, flags):
        if str(path).endswith("capture/index.json"):
            (root / "capture/index.json").write_bytes(b"{}")
        return original(path, flags)
    monkeypatch.setattr(release_oci, "private_file", mutate_copy)
    with pytest.raises(ValueError):
        release_oci.export_metadata(root, receipt, tmp_path / "metadata", "a" * 40, "0.2.2", "example/quadringent")


def test_export_checks_publication_identity_without_printing_private_text(tmp_path):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path, labels={"synthetic": ".".join(("10", "1", "2", "3"))})
    with pytest.raises(ValueError, match="identité privée"):
        release_oci.export_metadata(root, receipt, tmp_path / "metadata", "a" * 40, "0.2.2", "example/quadringent")
    assert not (tmp_path / "metadata").exists()


def test_metadata_export_and_secret_gate_precede_all_private_uploads_and_login():
    jobs = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]
    steps = jobs["images"]["steps"]
    export = next(s for s in steps if "release_oci.py export-metadata" in s.get("run", ""))
    scan = next(s for s in steps if s.get("name") == "Scanner les secrets des métadonnées OCI exportées")
    login = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("docker/login-action@"))
    seal = next(i for i, s in enumerate(steps) if "release_oci.py seal" in s.get("run", ""))
    assert seal < steps.index(export) < steps.index(scan) < login
    assert "umask 077" in scan["run"] and "--exit-code 1" in scan["run"]
    assert "--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in scan["run"]
    assert "--scanners secret" in scan["run"] and "trivy fs" in scan["run"]
    uploads = [s for s in steps if s.get("with", {}).get("name") in {"oci-scan-receipt", "oci-admission-metadata"}]
    assert len(uploads) == 2
    for upload in uploads:
        assert steps.index(scan) < steps.index(upload) < login
        assert "if" not in upload and "continue-on-error" not in upload
        assert upload["with"]["retention-days"] == 1 and upload["with"]["if-no-files-found"] == "error"
    metadata = next(s for s in uploads if s["with"]["name"] == "oci-admission-metadata")
    assert metadata["with"]["path"] == "${{ runner.temp }}/oci-admission-metadata/"
    assert not any(s.get("with", {}).get("name") == "oci-admission-metadata" for s in jobs["release"]["steps"])


@pytest.mark.parametrize("secret_fails", (False, True))
def test_metadata_secret_failure_stops_before_upload_or_registry_login(tmp_path, secret_fails):
    steps = yaml.safe_load(Path(".github/workflows/release.yml").read_text())["jobs"]["images"]["steps"]
    gate = next(s["run"] for s in steps if s.get("name") == "Scanner les secrets des métadonnées OCI exportées")
    tools = tmp_path / "bin"; tools.mkdir()
    trivy = tools / "trivy"
    trivy.write_text("#!/bin/sh\nexit " + ("37" if secret_fails else "0") + "\n")
    trivy.chmod(0o700)
    marker = tmp_path / "upload-and-login"
    result = subprocess.run(["bash", "-e", "-c", gate + '\ntouch "' + str(marker) + '"'],
                            env={**os.environ, "RUNNER_TEMP": str(tmp_path), "PATH": str(tools)+":"+os.environ["PATH"]},
                            capture_output=True)
    assert result.returncode == (37 if secret_fails else 0)
    assert marker.exists() is not secret_fails


@pytest.mark.parametrize("drift", ("duplicate_arch", "media_type", "traversal"))
def test_matching_receipt_does_not_override_oci_metadata_contract(tmp_path, drift):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path)
    component = root / "capture"
    top = json.loads((component / "index.json").read_bytes())
    if drift == "duplicate_arch":
        index = release_oci.json_blob(component, top["manifests"][0])
        index["manifests"][1]["platform"]["architecture"] = "amd64"
        top["manifests"][0] = document(component, index, "application/vnd.oci.image.index.v1+json")
    elif drift == "media_type": top["manifests"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+gzip"
    else: top["manifests"][0]["digest"] = "sha256:../../outside"
    (component / "index.json").write_text(json.dumps(top))
    receipt.unlink()
    release_oci.write_receipt({c:root/c for c in release_oci.COMPONENTS}, receipt)
    with pytest.raises(ValueError):
        release_oci.export_metadata(root, receipt, tmp_path / "metadata", "a" * 40, "0.2.2", "example/quadringent")


@pytest.mark.parametrize("destination", ("existing", "inside", "parent_link"))
def test_export_destination_is_exclusive_and_confined(tmp_path, destination):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path)
    output = tmp_path / "metadata"
    if destination == "existing": output.mkdir()
    elif destination == "inside": output = root / "metadata"
    else:
        linked = tmp_path / "linked"; linked.symlink_to(tmp_path, target_is_directory=True)
        output = linked / "metadata"
    with pytest.raises(ValueError):
        release_oci.export_metadata(root, receipt, output, "a" * 40, "0.2.2", "example/quadringent")


def test_export_total_size_is_bounded(tmp_path, monkeypatch):
    from scripts import release_oci
    root, receipt = portable_fixture(tmp_path)
    monkeypatch.setattr(release_oci, "MAX_METADATA_TOTAL_BYTES", 1)
    with pytest.raises(ValueError, match="trop grand"):
        release_oci.export_metadata(root, receipt, tmp_path / "metadata", "a" * 40, "0.2.2", "example/quadringent")


def test_export_cli_uses_native_workflow_arguments(tmp_path):
    root, receipt = portable_fixture(tmp_path)
    output = tmp_path / "metadata"
    result = subprocess.run([sys.executable, "scripts/release_oci.py", "export-metadata", "--root", str(root),
                             "--receipt", str(receipt), "--revision", "a"*40, "--version", "0.2.2",
                             "--repository", "example/quadringent", "--output", str(output)], capture_output=True)
    assert result.returncode == 0
    assert len([p for p in output.rglob("*") if p.is_file()]) == 21
