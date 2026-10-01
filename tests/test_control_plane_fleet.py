from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

import site_fixture

from quadringent_control_plane.fleet import (
    DESTINATION_NAMESPACE,
    ENVIRONMENT,
    MANIFEST,
    create_fleet,
    serialize_fleet,
)
from quadringent_control_plane.fleet_sidecar import generate_fleet_ui_sidecar
from quadringent_control_plane.model import SourceDescriptor
from quadringent_control_plane.projection import ProjectionError, project_console_document
from quadringent_control_plane.repository import (
    ProjectionRepository,
    ProjectionSource,
    bind_fleet_proofs,
    bind_fleet_runs,
    parse_source_spec,
)
from quadringent_control_plane.server import serve
from quadringent_control_plane.audit import ActionAuditLog

try:
    from tests.test_fleet_plan import catalog_payload
except ImportError:
    from test_fleet_plan import catalog_payload


SITE = site_fixture.build_test_site()
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
LIVE_SOURCE = SourceDescriptor("test-cntr", "live", SITE.environment, "file:///snapshot.json")
HISTORICAL_SOURCE = SourceDescriptor(SITE.site_id, "historical", SITE.environment, "file:///snapshot.json")


def fresh_running_document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(minutes=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 120}, "errors": {"value": 0}},
    }
ROOT = Path(__file__).resolve().parents[1]
OWNED_PATHS = (
    ROOT / "src/quadringent_control_plane/model.py",
    ROOT / "src/quadringent_control_plane/projection.py",
    ROOT / "src/quadringent_control_plane/repository.py",
    ROOT / "src/quadringent_control_plane/server.py",
    ROOT / "src/quadringent_control_plane/actions.py",
    ROOT / "scripts/quadringent_control_plane.py",
    ROOT / "tests/test_control_plane_fleet.py",
    ROOT / "tests/test_control_plane_actions.py",
)


def fleet_payload(**overrides: object) -> dict[str, object]:
    payload = serialize_fleet(create_fleet(max_concurrency=2, credit_budget=10.0))
    payload.update(overrides)
    return payload


def capture_document() -> dict[str, object]:
    document = deepcopy(fresh_running_document())
    document["generated_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    return document


class ControlPlaneFleetProjectionTests(unittest.TestCase):
    def test_absent_or_none_fleet_does_not_invent_one(self) -> None:
        document = fresh_running_document()
        baseline = project_console_document(document, LIVE_SOURCE, NOW).to_dict()
        self.assertNotIn("fleet", baseline)
        document["fleet"] = None
        result = project_console_document(document, LIVE_SOURCE, NOW).to_dict()
        self.assertEqual(result, baseline)

    def test_roundtrip_keeps_thirteen_manifest_tables(self) -> None:
        document = fresh_running_document()
        payload = fleet_payload()
        document["fleet"] = payload
        result = project_console_document(document, LIVE_SOURCE, NOW).to_dict()
        fleet = result.pop("fleet")
        capture = project_console_document(fresh_running_document(), LIVE_SOURCE, NOW).to_dict()
        self.assertEqual(result, capture)
        self.assertEqual(fleet["fleet_id"], SITE.fleet_id)
        self.assertEqual(fleet["environment"], ENVIRONMENT)
        self.assertEqual(fleet["destination_namespace"], DESTINATION_NAMESPACE)
        names = [table["name"] for table in fleet["tables"]]
        self.assertEqual(names, list(MANIFEST))
        self.assertEqual(len(names), 13)
        self.assertEqual(fleet["summary"]["table_count"], 13)
        self.assertEqual(
            set(fleet["capabilities"]),
            {"refresh", "prepare", "start", "pause", "resume"},
        )
        for capability in fleet["capabilities"].values():
            self.assertEqual(capability, {"state": "available", "reason": None})
        serialized = json.dumps(fleet)
        json.loads(serialized)
        self.assertNotIn("file://", serialized)
        self.assertNotIn("/tmp/", serialized)

    def test_historical_source_is_not_live(self) -> None:
        document = fresh_running_document()
        document["fleet"] = fleet_payload()
        fleet = project_console_document(document, HISTORICAL_SOURCE, NOW).to_dict()["fleet"]
        self.assertEqual(fleet["fleet_id"], SITE.fleet_id)
        for capability in fleet["capabilities"].values():
            self.assertEqual(capability["state"], "unavailable")
            self.assertEqual(capability["reason"], "not_live")

    def test_altered_namespace_environment_and_manifest_fail_closed(self) -> None:
        cases = [
            {"environment": "PROD"},
            {"destination_namespace": "PROD_RAW.OTHER_SCHEMA"},
            {"tables": serialize_fleet(create_fleet(max_concurrency=2, credit_budget=10.0))["tables"][:-1]},
            {
                "tables": list(
                    reversed(serialize_fleet(create_fleet(max_concurrency=2, credit_budget=10.0))["tables"])
                )
            },
            {"secret": "never-output"},
        ]
        for override in cases:
            with self.subTest(override=list(override)):
                document = fresh_running_document()
                payload = fleet_payload()
                payload.update(override)
                document["fleet"] = payload
                with self.assertRaises(ProjectionError) as raised:
                    project_console_document(document, LIVE_SOURCE, NOW)
                self.assertEqual(raised.exception.code, "invalid_fleet")
                self.assertNotIn("never-output", raised.exception.safe_message)
                self.assertNotIn("PROD", raised.exception.safe_message)

    def test_owned_files_do_not_name_the_competitor(self) -> None:
        forbidden = "pop" + "sink"
        public_sources = tuple(path for path in OWNED_PATHS if path.suffix == ".py" and "tests/" not in str(path))
        for path in OWNED_PATHS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(forbidden, text.casefold(), path)
        for path in public_sources:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("business_circulation", text, path)
            self.assertNotIn("business_collect", text, path)

    def test_public_modules_do_not_import_benchmark_collector(self) -> None:
        public = (
            ROOT / "src/quadringent_control_plane/model.py",
            ROOT / "src/quadringent_control_plane/projection.py",
            ROOT / "src/quadringent_control_plane/repository.py",
            ROOT / "src/quadringent_control_plane/server.py",
            ROOT / "src/quadringent_control_plane/actions.py",
            ROOT / "scripts/quadringent_control_plane.py",
        )
        for path in public:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("business_collect", text, path)
        package = "\n".join(
            path.read_text(encoding="utf-8")
            for path in public
            if path.name != "quadringent_control_plane.py"
        )
        self.assertNotIn("from .dev_refresh import", package)
        self.assertNotIn("import business_collect", package)


class ControlPlaneFleetRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.capture = self.root / "console.json"
        self.sidecar = self.root / "fleet.json"
        self.capture.write_text(json.dumps(capture_document()), encoding="utf-8")
        self.sidecar.write_text(json.dumps(fleet_payload()), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _source(self, *, environment: str = SITE.environment, source_id: str = SITE.site_id) -> ProjectionSource:
        return parse_source_spec(f"live:{source_id}:file://{self.capture}", environment=environment)

    def _bound(self, *, environment: str = SITE.environment, source_id: str = SITE.site_id) -> list[ProjectionSource]:
        return bind_fleet_proofs(
            [self._source(environment=environment, source_id=source_id)],
            [f"{source_id}={self.sidecar.as_uri()}"],
        )

    def test_projection_source_keeps_optional_fleet_origin_default(self) -> None:
        source = ProjectionSource(
            SourceDescriptor("demo", "live", "local", "file:///tmp/console.json")
        )
        self.assertIsNone(source.fleet_origin)
        parsed = parse_source_spec("simulation:demo:file:///tmp/console.json")
        self.assertIsNone(parsed.fleet_origin)

    def test_bindings_reject_unknown_duplicate_nondev_and_network_origins(self) -> None:
        source = self._source()
        cases = [
            ["other=file:///tmp/fleet.json"],
            ["acme=file:///tmp/a.json", "acme=file:///tmp/b.json"],
            ["acme=https://example.invalid/fleet.json"],
            ["acme=http://127.0.0.1/fleet.json"],
            ["acme=s3://bucket/fleet.json"],
            ["acme=file://host/tmp/fleet.json"],
            ["acme=file:relative/fleet.json"],
            ["acme=file:/tmp/fleet.json"],
            ["acme=file:////tmp/fleet.json"],
            ["acme=file:///tmp/fleet.json?query=1"],
            ["acme=file:///tmp/fleet.json#frag"],
            ["example-corp"],
        ]
        for specs in cases:
            with self.subTest(specs=specs):
                with self.assertRaises(ValueError) as captured:
                    bind_fleet_proofs([source], specs)
                message = str(captured.exception)
                self.assertNotIn("secret", message)
                self.assertNotIn("example.invalid", message)
                self.assertNotIn("/tmp/fleet.json", message)
                self.assertNotIn("acme", message)

        for environment in ("prod", "PROD", "DEV", "local"):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError) as captured:
                    bind_fleet_proofs(
                        [self._source(environment=environment)],
                        [f"acme={self.sidecar.as_uri()}"],
                    )
                self.assertNotIn("acme", str(captured.exception))
                self.assertNotIn(str(self.sidecar), str(captured.exception))

    def test_sidecar_does_not_override_capture_fields(self) -> None:
        snapshot = ProjectionRepository(self._bound()).refresh()
        pipeline = snapshot.pipelines[0]
        self.assertEqual(pipeline.counters["events_published"], 120)
        fleet = pipeline.to_dict()["fleet"]
        self.assertEqual(fleet["fleet_id"], SITE.fleet_id)
        self.assertEqual([table["name"] for table in fleet["tables"]], list(MANIFEST))
        on_disk = json.loads(self.capture.read_text(encoding="utf-8"))
        self.assertNotIn("fleet", on_disk)
        self.assertNotIn("pop" + "sink", json.dumps(snapshot.to_dict()).casefold())

    def test_real_fleet_plan_sidecar_is_attached_without_overriding_capture(self) -> None:
        self.sidecar.write_text(
            json.dumps(generate_fleet_ui_sidecar(catalog_payload())),
            encoding="utf-8",
        )
        snapshot = ProjectionRepository(self._bound()).refresh()
        pipeline = snapshot.pipelines[0]
        self.assertEqual(pipeline.counters["events_published"], 120)
        self.assertIsNone(pipeline.fleet)
        plan = pipeline.to_dict()["fleet_plan"]
        self.assertEqual(plan["destination_namespace"], DESTINATION_NAMESPACE)
        self.assertEqual(plan["observed_totals"]["table_count"], 13)
        self.assertEqual(plan["observed_totals"]["row_count"], 218_150_587)
        self.assertEqual(plan["continuity"], "uncertain")
        self.assertEqual(plan["cost"]["status"], "unknown")
        self.assertIsNone(plan["cost"]["observed"])
        self.assertNotIn("credit_budget", json.dumps(plan))

    def test_real_fleet_plan_binding_rejects_mismatched_source_without_leaking(self) -> None:
        self.sidecar.write_text(
            json.dumps(generate_fleet_ui_sidecar(catalog_payload())),
            encoding="utf-8",
        )
        bound = bind_fleet_proofs(
            [self._source(source_id="secret-source")],
            [f"secret-source={self.sidecar.as_uri()}"],
        )
        snapshot = ProjectionRepository(bound).refresh()
        self.assertEqual(snapshot.sources[0].status, "unavailable")
        self.assertEqual(snapshot.pipelines, ())
        serialized = json.dumps(snapshot.to_dict())
        self.assertNotIn(str(self.sidecar), serialized)

    def test_real_fleet_plan_read_error_retains_stale_last_plan(self) -> None:
        self.sidecar.write_text(
            json.dumps(generate_fleet_ui_sidecar(catalog_payload())),
            encoding="utf-8",
        )
        repository = ProjectionRepository(self._bound())
        observed = repository.refresh()
        observed_plan = observed.pipelines[0].to_dict()["fleet_plan"]
        self.sidecar.unlink()
        unavailable = repository.refresh()
        retained = unavailable.pipelines[0]
        self.assertEqual(unavailable.sources[0].status, "unavailable")
        self.assertEqual(retained.quality["freshness"], "stale")
        self.assertEqual(retained.to_dict()["fleet_plan"], observed_plan)
        self.assertNotIn(str(self.sidecar), json.dumps(unavailable.to_dict()))

    def test_invalid_sidecar_fails_closed_without_leaking(self) -> None:
        invalid = fleet_payload()
        invalid["password"] = "never-output"
        self.sidecar.write_text(json.dumps(invalid), encoding="utf-8")
        snapshot = ProjectionRepository(self._bound()).refresh()
        serialized = json.dumps(snapshot.to_dict())
        self.assertEqual(snapshot.sources[0].status, "unavailable")
        self.assertEqual(snapshot.pipelines, ())
        self.assertNotIn("never-output", serialized)
        self.assertNotIn(str(self.sidecar), serialized)


    def test_later_sidecar_read_error_retains_stale_unavailable_fleet(self) -> None:
        repository = ProjectionRepository(self._bound())
        observed = repository.refresh()
        observed_fleet = observed.pipelines[0].to_dict()["fleet"]
        self.sidecar.unlink()
        unavailable = repository.refresh()
        self.assertEqual(unavailable.revision, observed.revision + 1)
        self.assertEqual(unavailable.sources[0].status, "unavailable")
        retained = unavailable.pipelines[0]
        self.assertEqual(retained.status, "unknown")
        self.assertEqual(retained.quality["freshness"], "stale")
        retained_fleet = retained.to_dict()["fleet"]
        self.assertEqual(retained_fleet["fleet_id"], SITE.fleet_id)
        self.assertEqual(retained_fleet["tables"], observed_fleet["tables"])
        self.assertEqual(retained_fleet["summary"], observed_fleet["summary"])
        for capability in retained_fleet["capabilities"].values():
            self.assertEqual(capability, {"state": "unavailable", "reason": "stale_proof"})
        self.assertNotIn(str(self.sidecar), json.dumps(unavailable.to_dict()))

    def test_absent_sidecar_binding_does_not_invent_fleet(self) -> None:
        source = self._source()
        snapshot = ProjectionRepository([source]).refresh()
        self.assertNotIn("fleet", snapshot.pipelines[0].to_dict())

    def test_fleet_run_binding_projects_the_domain_run(self) -> None:
        run_path = self.root / "fleet-run.json"
        run_path.write_text(
            json.dumps(
                {
                    "format_version": "quadringent-fleet-run-state-v1",
                    "generation": 3,
                    "fleet": fleet_payload(),
                }
            ),
            encoding="utf-8",
        )
        bound = bind_fleet_runs(
            [self._source()], [f"{SITE.site_id}={run_path.as_uri()}"]
        )
        snapshot = ProjectionRepository(bound).refresh()
        fleet = snapshot.pipelines[0].to_dict()["fleet"]
        self.assertEqual(fleet["fleet_id"], SITE.fleet_id)
        self.assertEqual(len(fleet["tables"]), 13)

    def test_fleet_run_binding_overrides_the_sidecar(self) -> None:
        run_path = self.root / "fleet-run.json"
        run = create_fleet(max_concurrency=2, credit_budget=10.0)
        run_path.write_text(
            json.dumps(
                {
                    "format_version": "quadringent-fleet-run-state-v1",
                    "generation": 1,
                    "fleet": serialize_fleet(run),
                }
            ),
            encoding="utf-8",
        )
        source = bind_fleet_proofs(
            [self._source()], [f"{SITE.site_id}={self.sidecar.as_uri()}"]
        )[0]
        bound = bind_fleet_runs([source], [f"{SITE.site_id}={run_path.as_uri()}"])
        snapshot = ProjectionRepository(bound).refresh()
        fleet = snapshot.pipelines[0].to_dict()["fleet"]
        self.assertEqual(fleet["fleet_id"], SITE.fleet_id)

    def test_fleet_run_envelope_must_carry_the_run(self) -> None:
        run_path = self.root / "fleet-run.json"
        for document in (
            {"format_version": "quadringent-fleet-run-state-v1"},
            {"format_version": "other-format", "fleet": fleet_payload()},
            {"format_version": "quadringent-fleet-run-state-v1", "fleet": "not-a-map"},
        ):
            with self.subTest(document=document):
                run_path.write_text(json.dumps(document), encoding="utf-8")
                bound = bind_fleet_runs(
                    [self._source()], [f"{SITE.site_id}={run_path.as_uri()}"]
                )
                snapshot = ProjectionRepository(bound).refresh()
                self.assertEqual(snapshot.sources[0].status, "unavailable")
                self.assertEqual(snapshot.pipelines, ())

    def test_fleet_run_bindings_reject_unknown_nondev_and_network_origins(self) -> None:
        source = self._source()
        cases = [
            ["other=file:///tmp/fleet-run.json"],
            ["acme=file:///tmp/a.json", "acme=file:///tmp/b.json"],
            ["acme=https://example.invalid/fleet-run.json"],
            ["acme=s3://bucket/fleet-run.json"],
            ["acme=file:relative/fleet-run.json"],
            ["example-corp"],
        ]
        for specs in cases:
            with self.subTest(specs=specs):
                with self.assertRaises(ValueError) as captured:
                    bind_fleet_runs([source], specs)
                message = str(captured.exception)
                self.assertNotIn("example.invalid", message)
                self.assertNotIn("/tmp/fleet-run.json", message)
                self.assertNotIn("acme", message)

        for environment in ("prod", "PROD", "DEV", "local"):
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    bind_fleet_runs(
                        [self._source(environment=environment)],
                        ["acme=file:///tmp/fleet-run.json"],
                    )

    def test_cli_exposes_repeatable_file_sidecar_without_network(self) -> None:
        script = Path(__file__).resolve().parents[1] / "src" / "quadringent_control_plane" / "cli.py"
        text = script.read_text(encoding="utf-8")
        self.assertIn("--fleet-proof", text)
        self.assertNotIn("business_proof", text)
        self.assertNotIn("business-proof", text)
        self.assertNotIn("snowflake", text.lower())
        help_rendered = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[0].parent,
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(help_rendered.returncode, 0)
        self.assertIn("--fleet-proof", help_rendered.stdout)
        rejected = subprocess.run(
            [
                sys.executable,
                str(script),
                "--source",
                f"live:secret-source:file://{self.capture}",
                "--environment",
                SITE.environment,
                "--fleet-proof",
                "secret-source=https://example.invalid/fleet.json",
            ],
            cwd=script.parents[0].parent,
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn("secret-source", rejected.stderr)
        self.assertNotIn("example.invalid", rejected.stderr)




class ControlPlaneResumeReconciliationTests(unittest.TestCase):
    """Incident auth + sonde catalogue fraîche = capture prête à reprendre."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.capture = self.root / "console.json"
        self.sidecar = self.root / "fleet-sidecar.json"
        self.catalog = self.root / "fleet-catalog.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _write_capture(self) -> None:
        document = capture_document()
        document["run"] = {
            "state": "STOPPED_FAIL_CLOSED",
            "stopped_because": "IbmiUserDisabledError",
            "last_error": {
                "type": "IbmiUserDisabledError",
                "head": "receiver catalog failed",
                "at": "2026-09-19T21:02:01+00:00",
            },
        }
        self.capture.write_text(json.dumps(document), encoding="utf-8")

    def _catalog_payload(self, *, continuity: str, observed_at: str) -> dict[str, object]:
        payload = catalog_payload(continuity=continuity)
        payload["observed_at"] = observed_at
        payload["journals"][0]["receivers"] = [
            {
                "library": "DEMOLIB",
                "name": "DEMOJRN3776",
                "status": "ONLINE",
                "first_sequence": "1",
                "last_sequence": "50",
                "attach_timestamp": "2026-09-13T00:00:00.000000",
                "detach_timestamp": "2026-09-13T01:00:00.000000",
                "previous_library": None,
                "previous_name": None,
            },
            {
                "library": "DEMOLIB",
                "name": "DEMOJRN3777",
                "status": "ONLINE",
                "first_sequence": "1",
                "last_sequence": "100",
                "attach_timestamp": "2026-09-13T01:00:00.000000",
                "detach_timestamp": "2026-09-13T02:00:00.000000",
                "previous_library": None,
                "previous_name": None,
            },
            {
                "library": "DEMOLIB",
                "name": "DEMOJRN3778",
                "status": "ATTACHED",
                "first_sequence": "1",
                "last_sequence": "200",
                "attach_timestamp": "2026-09-13T02:00:00.000000",
                "detach_timestamp": None,
                "previous_library": None,
                "previous_name": None,
            },
        ]
        return payload

    def _bind(self) -> list[ProjectionSource]:
        source = parse_source_spec(
            f"live:{SITE.site_id}:file://{self.capture}", environment=SITE.environment
        )
        return bind_fleet_proofs(
            [source], [f"{SITE.site_id}={self.sidecar.as_uri()}"]
        )

    def _refresh_pipeline(self, *, continuity: str = "proven", age_seconds: float = 30.0):
        observed_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        catalog = self._catalog_payload(continuity=continuity, observed_at=observed_at)
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        self.sidecar.write_text(
            json.dumps(generate_fleet_ui_sidecar(catalog)), encoding="utf-8"
        )
        self._write_capture()
        return ProjectionRepository(self._bind()).refresh().pipelines[0]

    def test_fresh_probe_turns_auth_block_into_awaiting_resume(self) -> None:
        pipeline = self._refresh_pipeline()
        self.assertEqual(pipeline.status, "awaiting_resume")
        self.assertEqual(pipeline.summary, "Prête à reprendre")
        stages = {stage.id: stage for stage in pipeline.stages}
        self.assertEqual(stages["capture"].status, "awaiting_resume")
        self.assertEqual(stages["source"].status, "healthy")
        self.assertEqual(pipeline.incident["type"], "capture_auth_blocked")
        self.assertIs(pipeline.incident["cause_resolved"], True)
        resume = pipeline.resume
        self.assertEqual(resume["state"], "ready")
        self.assertEqual(resume["checkpoint"], {"receiver": "DEMOJRN3776", "sequence": 41})
        # 9 entrées restantes sur DEMOJRN3776 + 100 + 200 sur les suivants.
        self.assertEqual(resume["backlog_sequences"], 309)
        self.assertEqual(resume["backlog_receivers"], 3)
        self.assertEqual(resume["tail"], {"receiver": "DEMOJRN3778", "sequence": 200})

    def test_stale_probe_keeps_the_incident(self) -> None:
        pipeline = self._refresh_pipeline(age_seconds=3600.0)
        self.assertEqual(pipeline.status, "incident")
        self.assertIsNone(pipeline.resume)
        stages = {stage.id: stage for stage in pipeline.stages}
        self.assertEqual(stages["capture"].status, "incident")
        self.assertNotIn("cause_resolved", pipeline.incident)

    def test_unproven_continuity_keeps_the_incident(self) -> None:
        pipeline = self._refresh_pipeline(continuity="uncertain")
        self.assertEqual(pipeline.status, "incident")
        self.assertIsNone(pipeline.resume)

    def test_running_capture_is_not_requalified(self) -> None:
        self.capture.write_text(json.dumps(capture_document()), encoding="utf-8")
        observed_at = datetime.now(timezone.utc).isoformat()
        catalog = self._catalog_payload(continuity="proven", observed_at=observed_at)
        self.sidecar.write_text(
            json.dumps(generate_fleet_ui_sidecar(catalog)), encoding="utf-8"
        )
        pipeline = ProjectionRepository(self._bind()).refresh().pipelines[0]
        self.assertIsNone(pipeline.resume)
        self.assertNotEqual(pipeline.status, "awaiting_resume")

class _SupportingFleetExecutor:
    def supports(self, request: object) -> bool:
        return getattr(request, "fleet_id", None) == SITE.fleet_id and getattr(
            request, "action", None
        ) in {"refresh", "prepare", "start", "pause", "resume"}


class ControlPlaneFleetRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.capture = self.root / "console.json"
        self.sidecar = self.root / "fleet.json"
        self.capture.write_text(json.dumps(capture_document()), encoding="utf-8")
        self.sidecar.write_text(json.dumps(fleet_payload()), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _bound(self) -> list[ProjectionSource]:
        source = parse_source_spec(f"live:{SITE.site_id}:file://{self.capture}", environment=SITE.environment)
        return bind_fleet_proofs([source], [f"acme={self.sidecar.as_uri()}"])

    def _serve(self, executor: object | None = None) -> tuple[ProjectionRepository, object]:
        repository = ProjectionRepository(self._bound())
        repository.refresh()
        server = serve(repository, port=0, action_executor=executor, audit_log=ActionAuditLog(Path(self.directory.name) / "actions.jsonl"))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(_stop)
        return repository, server

    def test_get_overview_marks_all_capabilities_when_executor_supports_fleet(self) -> None:
        _repository, server = self._serve(executor=_SupportingFleetExecutor())
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/v1/overview")
        overview = json.loads(connection.getresponse().read())
        connection.close()
        capabilities = overview["pipelines"][0]["fleet"]["capabilities"]
        self.assertEqual(
            set(capabilities),
            {"refresh", "prepare", "start", "pause", "resume"},
        )
        for capability in capabilities.values():
            self.assertEqual(capability, {"state": "available", "reason": None})
        self.assertNotIn("business_circulation", overview["pipelines"][0])

    def test_get_overview_keeps_stale_proof_unavailable(self) -> None:
        repository, server = self._serve(executor=_SupportingFleetExecutor())
        self.sidecar.unlink()
        repository.refresh()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/v1/overview")
        overview = json.loads(connection.getresponse().read())
        connection.close()
        fleet = overview["pipelines"][0]["fleet"]
        self.assertEqual(fleet["fleet_id"], SITE.fleet_id)
        for capability in fleet["capabilities"].values():
            self.assertEqual(capability, {"state": "unavailable", "reason": "stale_proof"})


if __name__ == "__main__":
    unittest.main()
