from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verify_quadringent_vertical import (
    _execute_router_contract,
    _node_runtime_prerequisite,
    _source_entry_contract,
    _tree_hashes,
    _ui_source_fingerprint,
    _vite_bundle_proof,
    parse_sse_frame,
    validate_sse_revision_sequence,
    verify_vertical,
)

VALID_RENDERED_HELM = """
apiVersion: v1
kind: ConfigMap
data:
  AS400_CONSOLE_SNAPSHOT_S3_KEY: "as400/dev/console.json"
  AS400_TLS: "true"
  AS400_ALLOW_PLAINTEXT: "false"
  AS400_JOURNAL_LIBRARY: "DEMOLIB"
"""


def pipeline(*, evidence_kind: str = "simulation", freshness: str = "fresh", status: str = "degraded") -> dict[str, object]:
    return {
        "id": "demo",
        "environment": "local",
        "status": status,
        "quality": {
            "coverage": "partial",
            "freshness": freshness,
            "evidence_kind": evidence_kind,
        },
        "summary": "Simulation : livraison Snowflake non prouvée",
        "observed_at": "2026-08-28T10:00:00+00:00",
        "stages": [
            {
                "id": "destination",
                "status": "unknown",
                "observed_at": None,
                "headline": "Destination non observée",
                "detail": "Aucune preuve d'application Snowflake",
            }
        ],
        "lag_sequences": 0,
        "lag_seconds": None,
        "lag_series": None,
        "counters": {"events_published": 1},
        "incident": None,
    }


def overview(item: dict[str, object]) -> dict[str, object]:
    return {
        "revision": 1,
        "generated_at": "2026-08-28T10:00:00+00:00",
        "pipelines": [item],
        "sources": [
            {
                "id": "demo",
                "evidence_kind": item["quality"]["evidence_kind"],
                "environment": "local",
                "status": "available",
                "error": None,
            }
        ],
    }


class QuadringentVerticalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.built = tempfile.TemporaryDirectory()
        cls.built_dist = Path(cls.built.name) / "dist"
        completed = subprocess.run(
            ["npm", "run", "build", "--prefix", "ui"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout + completed.stderr)
        shutil.copytree(Path("ui/dist"), cls.built_dist)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.built.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        self.dist = self.temp_root / "dist"
        shutil.copytree(self.built_dist, self.dist)
        self.manifest_path = self.dist / ".vite" / "manifest.json"
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.entry_file = manifest["index.html"]["file"]
        self.entry_path = self.dist / self.entry_file
        self.css_files = tuple(manifest["index.html"].get("css", []))
        self.product_entry_content = self.entry_path.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_vertical_verifier_rejects_demo_as_live(self) -> None:
        unsafe = overview(pipeline(status="healthy"))

        result = verify_vertical(
            overview=unsafe,
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("simulation_is_not_live", result.failures)

    def test_node_24_lts_baseline_accepts_node_26_compatibility(self) -> None:
        with patch(
            "verify_quadringent_vertical.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["node", "--version"], 0, stdout="v24.20.0\n", stderr=""
            ),
        ):
            self.assertIsNone(_node_runtime_prerequisite())
        with patch(
            "verify_quadringent_vertical.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["node", "--version"], 0, stdout="v26.8.1\n", stderr=""
            ),
        ):
            self.assertIsNone(_node_runtime_prerequisite())
        with patch(
            "verify_quadringent_vertical.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["node", "--version"], 0, stdout="v22.23.2\n", stderr=""
            ),
        ):
            self.assertEqual(
                _node_runtime_prerequisite(),
                "Node.js 24 ou 26 requis; version détectée: v22.23.2",
            )

    def test_tree_hashes_normalizes_symlinked_node_modules_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct" / ".vite"
            isolated = root / "isolated" / ".vite"
            direct.mkdir(parents=True)
            isolated.mkdir(parents=True)
            direct_manifest = {
                "node_modules/@fontsource/font.woff2": {
                    "file": "assets/font-123.woff2",
                    "src": "node_modules/@fontsource/font.woff2",
                }
            }
            isolated_manifest = {
                "../../checkout/ui/node_modules/@fontsource/font.woff2": {
                    "file": "assets/font-123.woff2",
                    "src": "../../checkout/ui/node_modules/@fontsource/font.woff2",
                }
            }
            (direct / "manifest.json").write_text(json.dumps(direct_manifest), encoding="utf-8")
            (isolated / "manifest.json").write_text(json.dumps(isolated_manifest), encoding="utf-8")

            self.assertEqual(_tree_hashes(root / "direct"), _tree_hashes(root / "isolated"))

    def test_bundle_rejects_modified_dependency_notices(self) -> None:
        notice = self.dist / "assets" / "third-party-notices.txt"
        self.assertTrue(notice.is_file())
        notice.write_text("avis remplacé", encoding="utf-8")

        verified, _ = _vite_bundle_proof(self.dist)

        self.assertFalse(verified)

    def test_simulation_is_not_live(self) -> None:
        safe = overview(pipeline())

        result = verify_vertical(
            overview=safe,
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertNotIn("simulation_is_not_live", result.failures)
        self.assertNotIn("destination_unobserved_visible", result.failures)

    def test_stale_is_never_healthy(self) -> None:
        unsafe = overview(pipeline(evidence_kind="live", freshness="stale", status="healthy"))

        result = verify_vertical(
            overview=unsafe,
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("stale_is_not_healthy", result.failures)

    def test_bundle_rejects_demo_fixture_trace(self) -> None:
        self.entry_path.write_text(
            "fetch('/console-dev.json')", encoding="utf-8"
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("production_bundle_has_no_demo", result.failures)

    def test_api_fixture_rejects_secret_like_and_raw_error_fields(self) -> None:
        unsafe = overview(pipeline())
        unsafe["pipelines"][0]["api_token"] = "never"
        unsafe["pipelines"][0]["last_error"] = {"stacktrace": "never"}

        result = verify_vertical(
            overview=unsafe,
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("api_fixture_is_sanitized", result.failures)

    def test_api_fixture_normalizes_camel_snake_and_kebab_sensitive_keys(self) -> None:
        for unsafe_key in (
            "apiToken",
            "api_token",
            "api-token",
            "authorization",
            "rawPayload",
            "raw_payload",
            "raw-payload",
            "x-api-key",
            "serviceApiKey",
            "signingPrivateKey",
            "x-private-key",
            "bearerAuthorization",
            "apikey",
            "privatekey",
            "clientsecret",
            "refreshtoken",
        ):
            with self.subTest(unsafe_key=unsafe_key):
                unsafe = overview(pipeline())
                unsafe["pipelines"][0][unsafe_key] = "value-must-not-be-reported"

                result = verify_vertical(
                    overview=unsafe,
                    production_bundle=self.dist,
                    rendered_helm=VALID_RENDERED_HELM,
                )

                self.assertIn("api_fixture_is_sanitized", result.failures)
                detail = next(
                    check.detail
                    for check in result.checks
                    if check.id == "api_fixture_is_sanitized"
                )
                self.assertNotIn("value-must-not-be-reported", detail)

    def test_api_fixture_does_not_reject_unrelated_embedded_substrings(self) -> None:
        safe = overview(pipeline())
        safe["pipelines"][0]["monkey"] = "public"
        safe["pipelines"][0]["tokenizer"] = "public"
        safe["pipelines"][0]["secretary"] = "public"

        result = verify_vertical(
            overview=safe,
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertNotIn("api_fixture_is_sanitized", result.failures)

    def test_sse_frame_rejects_an_id_data_revision_mismatch(self) -> None:
        frame = b'id: 2\nevent: projection.updated\ndata: {"revision":1}\n\n'

        with self.assertRaisesRegex(ValueError, "révision SSE incohérente"):
            parse_sse_frame(frame)

    def test_sse_sequence_rejects_constant_data_revision_and_non_monotone_ids(self) -> None:
        cursor = b'id: 1\nevent: stream.cursor\ndata: {"revision":1}\n\n'
        constant_data = b'id: 2\nevent: projection.updated\ndata: {"revision":1}\n\n'
        backwards = b'id: 1\nevent: projection.updated\ndata: {"revision":1}\n\n'

        with self.assertRaisesRegex(ValueError, "révision SSE incohérente"):
            validate_sse_revision_sequence((cursor, constant_data))
        with self.assertRaisesRegex(ValueError, "strictement croissantes"):
            validate_sse_revision_sequence((cursor, backwards))

    def test_arbitrary_route_words_and_fixture_flag_are_not_a_verified_bundle(self) -> None:
        (self.dist / "index.html").write_text(
            '<script src="/assets/app.js"></script>', encoding="utf-8"
        )
        self.entry_path.unlink()
        (self.dist / "assets" / "app.js").write_text(
            "VITE_USE_FIXTURE overview pipelines pipeline incidents usage",
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("production_bundle_has_no_demo", result.failures)
        self.assertIn("ui_route_inventory", result.failures)

    def test_invalid_javascript_cannot_masquerade_as_a_hashed_vite_entrypoint(self) -> None:
        self.entry_path.write_text(
            'const broken = ; "/v1" "/overview" "/pipelines" "/events" '
            '"incidents" "usage" EventSource createRoot',
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("ui_build_exists", result.failures)
        self.assertIn("ui_route_inventory", result.failures)

    def test_parseable_javascript_without_product_markers_is_not_a_product_build(self) -> None:
        self.entry_path.write_text(
            "(()=>{})()", encoding="utf-8"
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("ui_build_exists", result.failures)

    def test_comment_only_product_markers_do_not_certify_a_bundle(self) -> None:
        self.entry_path.write_text(
            '// "/v1" "/overview" "/pipelines" /events "incidents" "usage" '
            "EventSource createRoot\n(()=>{})()",
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("ui_build_exists", result.failures)

    def test_template_interpolation_comments_do_not_certify_a_bundle(self) -> None:
        self.entry_path.write_text(
            '`${/* "/v1" "/overview" "/pipelines" /events '
            '"incidents" "usage" EventSource createRoot */0}`',
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("ui_build_exists", result.failures)

    def test_dead_product_words_do_not_certify_a_bundle(self) -> None:
        attacks = (
            (
                "if (false) { const words = [\"/v1\", \"/overview\", "
                "\"/pipelines\", \"/events\", \"incidents\", \"usage\", "
                "EventSource, createRoot]; }"
            ),
            (
                "function neverCalled() { return [\"/v1\", \"/overview\", "
                "\"/pipelines\", \"/events\", \"incidents\", \"usage\", "
                "EventSource, createRoot]; }"
            ),
        )
        for source in attacks:
            with self.subTest(source=source):
                self.entry_path.write_text(source, encoding="utf-8")

                verified, _ = _vite_bundle_proof(self.dist)

                self.assertFalse(verified)

    def test_inline_module_cannot_accompany_the_canonical_dist_entry(self) -> None:
        index = self.dist / "index.html"
        index.write_text(
            index.read_text(encoding="utf-8")
            + '<script data-proof=">" type="module">globalThis.forged = true</script>',
            encoding="utf-8",
        )

        verified, _ = _vite_bundle_proof(self.dist)
        guarded = subprocess.run(
            ["node", "ui/scripts/verify-build.mjs", str(self.dist)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertFalse(verified)
        self.assertNotEqual(guarded.returncode, 0)

    def test_inline_module_cannot_accompany_the_exact_source_entry(self) -> None:
        fake_root = self.temp_root / "inline-source"
        source = fake_root / "ui" / "src"
        source.mkdir(parents=True)
        (source / "main.tsx").write_text("export {};", encoding="utf-8")
        (fake_root / "ui" / "index.html").write_text(
            '<script type="module" src="/src/main.tsx"></script>'
            '<script data-proof=">" type="module">globalThis.forged = true</script>',
            encoding="utf-8",
        )

        verified, _ = _source_entry_contract(fake_root)

        self.assertFalse(verified)

    def test_transitive_source_symlink_is_rejected(self) -> None:
        fake_root = self.temp_root / "source-symlink"
        source = fake_root / "ui" / "src"
        source.mkdir(parents=True)
        (source / "main.tsx").write_text(
            "import './outside-payload.ts';", encoding="utf-8"
        )
        outside = self.temp_root / "outside-payload.ts"
        outside.write_text("globalThis.forged = true;", encoding="utf-8")
        (source / "outside-payload.ts").symlink_to(outside)
        (fake_root / "ui" / "index.html").write_text(
            '<script type="module" src="/src/main.tsx"></script>',
            encoding="utf-8",
        )

        verified, detail = _source_entry_contract(fake_root)

        self.assertFalse(verified)
        self.assertIn("symbolique", detail)

    def test_ui_source_fingerprint_changes_with_source_content(self) -> None:
        fake_root = self.temp_root / "fingerprint"
        source = fake_root / "ui" / "src"
        source.mkdir(parents=True)
        main = source / "main.tsx"
        main.write_text("export const revision = 1;", encoding="utf-8")
        (fake_root / "ui" / "index.html").write_text(
            '<script type="module" src="/src/main.tsx"></script>',
            encoding="utf-8",
        )

        before_ok, _, before = _ui_source_fingerprint(fake_root)
        main.write_text("export const revision = 2;", encoding="utf-8")
        after_ok, _, after = _ui_source_fingerprint(fake_root)
        coverage = fake_root / "ui" / "coverage" / "cache-probe.ts"
        coverage.parent.mkdir()
        coverage.write_text("export const probe = 1;", encoding="utf-8")
        coverage_ok, _, with_coverage = _ui_source_fingerprint(fake_root)
        coverage.write_text("export const probe = 2;", encoding="utf-8")
        mutated_ok, _, mutated_coverage = _ui_source_fingerprint(fake_root)

        self.assertTrue(before_ok)
        self.assertTrue(after_ok)
        self.assertNotEqual(before, after)
        self.assertTrue(coverage_ok)
        self.assertTrue(mutated_ok)
        self.assertNotEqual(with_coverage, mutated_coverage)

    def test_html_attribute_aliases_and_entities_cannot_forge_module_entries(self) -> None:
        index = self.dist / "index.html"
        original = index.read_text(encoding="utf-8")
        attacks = (
            f'<script data-type="module" data-src="/{self.entry_file}"></script>',
            original
            + '<script data-proof=">" type="mod&#117;le">globalThis.forged = true</script>',
        )
        for position, markup in enumerate(attacks):
            with self.subTest(position=position):
                index.write_text(markup, encoding="utf-8")
                verified, _ = _vite_bundle_proof(self.dist)
                guarded = subprocess.run(
                    ["node", "ui/scripts/verify-build.mjs", str(self.dist)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertFalse(verified)
                self.assertNotEqual(guarded.returncode, 0)
        index.write_text(original, encoding="utf-8")

    def test_manifest_rejects_non_canonical_logical_paths_and_non_js_entry(self) -> None:
        attacks = (
            "assets/./index-AbCd1234.js",
            "assets/cache/../index-AbCd1234.js",
            r"assets\index-AbCd1234.js",
            "assets/%69ndex-AbCd1234.js",
            "assets/index-AbCd1234.css",
        )
        for entry_file in attacks:
            with self.subTest(entry_file=entry_file):
                path = self.dist / entry_file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self.product_entry_content, encoding="utf-8")
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                manifest["index.html"]["file"] = entry_file
                manifest["index.html"]["css"] = []
                self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                (self.dist / "index.html").write_text(
                    f'<script type="module" src="/{entry_file}"></script>',
                    encoding="utf-8",
                )

                verified, _ = _vite_bundle_proof(self.dist)

                self.assertFalse(verified)

                manifest["index.html"]["file"] = self.entry_file
                manifest["index.html"]["css"] = list(self.css_files)
                self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_manifest_import_to_a_missing_asset_fails_bundle_closure(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["index.html"]["imports"] = ["_missing.js"]
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("ui_build_exists", result.failures)

    def test_manifest_rejects_non_canonical_logical_import_keys(self) -> None:
        attacks = (
            "./_forged.js",
            "../_forged.js",
            r"chunks\_forged.js",
            "%5fforged.js",
            "https://example.invalid/forged.js",
        )
        for logical_key in attacks:
            with self.subTest(logical_key=logical_key):
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                manifest["index.html"]["imports"] = [logical_key]
                manifest[logical_key] = {
                    "file": self.entry_file,
                    "imports": [],
                    "dynamicImports": [],
                }
                self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                verified, detail = _vite_bundle_proof(self.dist)

                self.assertFalse(verified)
                self.assertIn("clé logique manifeste non canonique", detail)

    def test_manifest_import_and_dynamic_import_must_reference_parseable_javascript(self) -> None:
        for field in ("imports", "dynamicImports"):
            with self.subTest(field=field):
                forged_key = f"_forged-{field}.txt"
                (self.dist / "assets" / f"forged-{field}.txt").write_text(
                    "const invalid = ;", encoding="utf-8"
                )
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                manifest["index.html"][field] = [forged_key]
                manifest[forged_key] = {
                    "file": f"assets/forged-{field}.txt",
                    "name": f"forged-{field}",
                    "imports": [],
                    "dynamicImports": [],
                }
                self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                verified, _ = _vite_bundle_proof(self.dist)

                self.assertFalse(verified)
                manifest["index.html"][field] = []
                manifest.pop(forged_key)
                self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_external_symlink_cannot_satisfy_a_manifest_asset(self) -> None:
        external = self.temp_root / "external.js"
        external.write_text("(()=>{})()", encoding="utf-8")
        entrypoint = self.entry_path
        entrypoint.unlink()
        entrypoint.symlink_to(external)

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
        )

        self.assertIn("ui_build_exists", result.failures)

    def test_route_inventory_rejects_a_lexical_router_symlink(self) -> None:
        fake_root = self.temp_root / "symlink-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.symlink_to(Path("ui/src/router.ts").resolve())
        verify_script = fake_root / "ui" / "scripts" / "verify-build.mjs"
        verify_script.parent.mkdir(parents=True)
        verify_script.write_text(
            Path("ui/scripts/verify-build.mjs").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        verified, _ = _execute_router_contract(fake_root)

        self.assertFalse(verified)

    def test_route_inventory_cannot_forge_assertions_by_mutating_json_stringify(self) -> None:
        fake_root = self.temp_root / "mutating-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            """
JSON.stringify = () => 'forged';
export function parseRoute(_hash: string) { return { name: 'forged' }; }
export function href(route: {name: string}) {
  if (route.name === 'overview') return '#/';
  if (route.name === 'usage') return '#/usage';
  return '#/forged';
}
""".strip(),
            encoding="utf-8",
        )

        verified, _ = _execute_router_contract(fake_root)

        self.assertFalse(verified)

    def test_route_inventory_cannot_skip_cases_by_mutating_array_iterator(self) -> None:
        fake_root = self.temp_root / "mutating-array-iterator-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            """
globalThis.Array.prototype[Symbol.iterator] = function* () {};
export function parseRoute(_hash: string) { return { name: 'forged' }; }
export function href(route: {name: string}) {
  if (route.name === 'overview') return '#/';
  if (route.name === 'usage') return '#/usage';
  return '#/forged';
}
""".strip(),
            encoding="utf-8",
        )

        verified, _ = _execute_router_contract(fake_root)

        self.assertFalse(verified)

    def test_route_inventory_requires_completion_after_import(self) -> None:
        fake_root = self.temp_root / "early-exit-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            """
if (typeof process !== 'undefined') process.exit(0);
export function parseRoute(_hash: string) { return { name: 'forged' }; }
export function href(_route: {name: string}) { return '#/forged'; }
""".strip(),
            encoding="utf-8",
        )

        verified, detail = _execute_router_contract(fake_root)

        self.assertFalse(verified)
        self.assertIn("matrice router.ts", detail)

    def test_route_inventory_cannot_forge_the_completion_marker(self) -> None:
        fake_root = self.temp_root / "forged-marker-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            """
if (typeof process !== 'undefined') {
  process.stdout.write('QUADRINGENT_ROUTER_CONTRACT_OK');
  process.exit(0);
}
export function parseRoute(_hash: string) { return { name: 'forged' }; }
export function href(_route: {name: string}) { return '#/forged'; }
""".strip(),
            encoding="utf-8",
        )

        verified, _ = _execute_router_contract(fake_root)

        self.assertFalse(verified)

    def test_route_inventory_cannot_mutate_captured_object_array_or_json_intrinsics(self) -> None:
        mutations = (
            "Object.getOwnPropertyDescriptor = () => ({value:'forged'});",
            "Reflect.ownKeys = () => ['name'];",
            "Array.isArray = () => true;",
            "JSON.stringify = () => 'forged';",
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                fake_root = self.temp_root / f"mutating-intrinsic-{index}"
                router = fake_root / "ui" / "src" / "router.ts"
                router.parent.mkdir(parents=True)
                router.write_text(
                    f"""
{mutation}
export function parseRoute(_hash: string) {{ return {{ name: 'forged' }}; }}
export function href(route: {{name: string}}) {{
  if (route.name === 'overview') return '#/';
  if (route.name === 'usage') return '#/usage';
  return '#/forged';
}}
""".strip(),
                    encoding="utf-8",
                )

                verified, _ = _execute_router_contract(fake_root)

                self.assertFalse(verified)

    def test_route_inventory_rejects_contract_phrases_outside_router_structure(self) -> None:
        fake_root = Path(self.temporary.name) / "fake-root"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            "path === 'overview' path === 'pipelines' path === 'incidents' "
            "path === 'usage' ^(?:pipeline|pipelines) "
            "new Set<PipelineTab>(['overview', 'live'])",
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
            root=fake_root,
        )

        self.assertIn("ui_route_inventory", result.failures)

    def test_route_inventory_ignores_line_and_block_commented_router_code(self) -> None:
        contract = """
if (!path || path === 'overview') return { name: 'overview' };
if (path === 'pipelines') return { name: 'pipelines' };
if (path === 'incidents') return { name: 'incidents' };
if (path === 'usage') return { name: 'usage' };
const pipelineTabs = new Set<PipelineTab>(['overview', 'live']);
const match = /^(?:pipeline|pipelines)\\/([^/?#]+)$/.exec(path);
if (!match) return { name: 'overview' };
""".strip()
        for style, commented in (
            ("line", "\n".join(f"// {line}" for line in contract.splitlines())),
            ("block", f"/*\n{contract}\n*/"),
        ):
            with self.subTest(style=style):
                fake_root = Path(self.temporary.name) / f"comments-{style}"
                router = fake_root / "ui" / "src" / "router.ts"
                router.parent.mkdir(parents=True, exist_ok=True)
                router.write_text(commented, encoding="utf-8")

                result = verify_vertical(
                    overview=overview(pipeline()),
                    production_bundle=self.dist,
                    rendered_helm=VALID_RENDERED_HELM,
                    root=fake_root,
                )

                self.assertIn("ui_route_inventory", result.failures)

    def test_route_inventory_executes_behavior_not_dead_structural_code(self) -> None:
        fake_root = self.temp_root / "wrong-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            """
export function parseRoute(_hash: string) { return { name: 'overview' }; }
export function href(_route: unknown) { return '#/'; }
function dead(path: string) {
  if (!path || path === 'overview') return { name: 'overview' };
  if (path === 'pipelines') return { name: 'pipelines' };
  if (path === 'incidents') return { name: 'incidents' };
  if (path === 'usage') return { name: 'usage' };
  const pipelineTabs = new Set<PipelineTab>(['overview', 'live']);
  const match = /^(?:pipeline|pipelines)\\/([^/?#]+)$/.exec(path);
  if (!match) return { name: 'overview' };
  return pipelineTabs;
}
""".strip(),
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
            root=fake_root,
        )

        self.assertIn("ui_route_inventory", result.failures)

    def test_route_inventory_rejects_a_non_round_tripping_pipeline_href(self) -> None:
        fake_root = self.temp_root / "wrong-href-router"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            """
const known = new Map<string, unknown>([
  ['#/overview', {name:'overview'}],
  ['#/pipelines', {name:'pipelines'}],
  ['#/pipeline/dev-cntr', {name:'pipeline', id:'dev-cntr'}],
  ['#/pipeline/dev-cntr/overview', {name:'pipeline', id:'dev-cntr', tab:'overview'}],
  ['#/pipeline/dev-cntr/live', {name:'pipeline', id:'dev-cntr', tab:'live'}],
  ['#/pipeline/wrong', {name:'pipeline', id:'wrong'}],
  ['#/incidents', {name:'incidents'}],
  ['#/usage', {name:'usage'}],
]);
export function parseRoute(hash: string) { return known.get(hash) ?? {name:'overview'}; }
export function href(route: {name: string}) {
  if (route.name === 'overview') return '#/';
  if (route.name === 'pipeline') return '#/pipeline/wrong';
  return `#/${route.name}`;
}
""".strip(),
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
            root=fake_root,
        )

        self.assertIn("ui_route_inventory", result.failures)

    def test_verify_build_accepts_explicit_safe_dist_and_rejects_demo(self) -> None:
        script = Path("ui/scripts/verify-build.mjs")
        safe = subprocess.run(
            ["node", str(script), str(self.dist)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(safe.returncode, 0, safe.stderr)

        self.entry_path.write_text(
            "const VITE_USE_FIXTURE = true", encoding="utf-8"
        )
        unsafe = subprocess.run(
            ["node", str(script), str(self.dist)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(unsafe.returncode, 0)
        self.assertIn("fixture", unsafe.stderr.lower())

    def test_verify_build_rejects_non_javascript_transitive_chunk(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["index.html"]["dynamicImports"] = ["_forged.txt"]
        manifest["_forged.txt"] = {
            "file": "assets/forged.txt",
            "imports": [],
            "dynamicImports": [],
        }
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (self.dist / "assets" / "forged.txt").write_text(
            "const invalid = ;", encoding="utf-8"
        )

        completed = subprocess.run(
            ["node", "ui/scripts/verify-build.mjs", str(self.dist)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("javascript", completed.stderr.lower())

    def test_vertical_rejects_a_noop_verify_build_guard(self) -> None:
        fake_root = self.temp_root / "noop-build-guard"
        router = fake_root / "ui" / "src" / "router.ts"
        router.parent.mkdir(parents=True)
        router.write_text(
            Path("ui/src/router.ts").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        verify_script = fake_root / "ui" / "scripts" / "verify-build.mjs"
        verify_script.parent.mkdir(parents=True)
        verify_script.write_text(
            "// console-dev.json vite_use_fixture fixture:// /fixtures/ "
            "ui/fixtures data/fixtures fixture_fallback\n",
            encoding="utf-8",
        )

        result = verify_vertical(
            overview=overview(pipeline()),
            production_bundle=self.dist,
            rendered_helm=VALID_RENDERED_HELM,
            root=fake_root,
        )

        self.assertIn("production_bundle_has_no_demo", result.failures)

    def test_bundle_proof_explains_how_to_install_missing_ui_dependencies(self) -> None:
        fake_root = self.temp_root / "missing-ui-dependencies"
        source = fake_root / "ui" / "src"
        source.mkdir(parents=True)
        (fake_root / "ui" / "index.html").write_text(
            '<script type="module" src="/src/main.tsx"></script>', encoding="utf-8"
        )
        (source / "main.tsx").write_text("export {};", encoding="utf-8")

        verified, detail = _vite_bundle_proof(self.dist, root=fake_root)

        self.assertFalse(verified)
        self.assertIn("npm ci --prefix ui", detail)

    def test_local_runbook_has_exactly_three_terminals_and_truthful_limits(self) -> None:
        runbook = Path("docs/product/quadringent-local-console.md").read_text(
            encoding="utf-8"
        )

        self.assertEqual(runbook.count("## Terminal "), 2)
        self.assertIn(
            "scripts/emit_console_snapshot_dev.py --out /tmp/quadringent-console.json",
            runbook,
        )
        self.assertIn(
            "--source historical:local-proof:file:///tmp/quadringent-console.json",
            runbook,
        )
        self.assertIn("--ui-dist ui/dist", runbook)
        self.assertIn("livraison Snowflake reste non prouvée", runbook)
        self.assertIn("aucune mutation cloud", runbook.lower())
        self.assertIn("http://127.0.0.1:8844/", runbook)

        vite_config = Path("ui/vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("port: 5180", vite_config)
        self.assertIn("strictPort: true", vite_config)
        self.assertIn("manifest: true", vite_config)

    def test_cli_help_resolves_the_package_instead_of_the_sibling_script(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "src"

        completed = subprocess.run(
            [sys.executable, "scripts/verify_quadringent_vertical.py", "--help"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--ui-dist", completed.stdout)
        self.assertIn("Node.js 24", completed.stdout)
        self.assertIn("npm ci --prefix ui", completed.stdout)
        self.assertIn("Helm", completed.stdout)

    def test_cli_imports_the_worker_with_only_src_on_pythonpath(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "src"

        completed = subprocess.run(
            [
                sys.executable,
                "scripts/verify_quadringent_vertical.py",
                "--ui-dist",
                str(self.dist),
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS worker_runtime_imports", completed.stdout)

    def test_importing_verifier_does_not_mutate_the_shared_module_path(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = "scripts:src"

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; before=list(sys.path); import verify_quadringent_vertical; "
                "assert sys.path == before, (before, sys.path)",
            ],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
