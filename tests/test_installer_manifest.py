"""Manifeste de version : digests obligatoires et immuables."""

from __future__ import annotations

from pathlib import Path
import unittest

from quadringent.installer.manifest import InvalidReleaseManifest, ReleaseManifest


class ReleaseManifestTests(unittest.TestCase):
    def test_loads_example_manifest(self) -> None:
        manifest = ReleaseManifest.from_file(Path("deploy/release-manifest.example.json"))
        self.assertTrue(manifest.image_digest.startswith("sha256:"))
        self.assertEqual(len(manifest.image_digest), len("sha256:") + 64)
        self.assertEqual(manifest.repository, "ghcr.io/quadringent/quadringent")
        self.assertTrue(manifest.verifier_image_digest.startswith("sha256:"))

    def test_example_manifest_matches_json_schema(self) -> None:
        import json

        import jsonschema

        schema = json.loads(Path("deploy/release-manifest.schema.json").read_text())
        raw = json.loads(Path("deploy/release-manifest.example.json").read_text())
        jsonschema.validate(raw, schema)

    def test_schema_rejects_missing_verifier_digest(self) -> None:
        import json

        import jsonschema

        schema = json.loads(Path("deploy/release-manifest.schema.json").read_text())
        raw = json.loads(Path("deploy/release-manifest.example.json").read_text())
        del raw["verifierImageDigest"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(raw, schema)

    def test_missing_verifier_digest_raises(self) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(
                json.dumps(
                    {
                        "imageDigest": "sha256:" + "0" * 64,
                        "controlPlaneImageDigest": "sha256:" + "0" * 64,
                        "observabilityImageDigest": "sha256:" + "0" * 64,
                    }
                )
            )
            with self.assertRaises(InvalidReleaseManifest):
                ReleaseManifest.from_file(path)

    def test_repository_override(self) -> None:
        manifest = ReleaseManifest.from_file(
            Path("deploy/release-manifest.example.json"), repository_override="registry.example.com/quadringent"
        )
        self.assertEqual(manifest.repository, "registry.example.com/quadringent")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(InvalidReleaseManifest):
            ReleaseManifest.from_file(Path("deploy/does-not-exist.json"))

    def test_invalid_digest_raises(self, tmp_path=None) -> None:
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"imageDigest": "latest", "controlPlaneImageDigest": "sha256:" + "0" * 64,
                                         "verifierImageDigest": "sha256:" + "0" * 64,
                                         "observabilityImageDigest": "sha256:" + "0" * 64}))
            with self.assertRaises(InvalidReleaseManifest):
                ReleaseManifest.from_file(path)

    def test_invalid_json_raises(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json")
            with self.assertRaises(InvalidReleaseManifest):
                ReleaseManifest.from_file(path)


if __name__ == "__main__":
    unittest.main()
