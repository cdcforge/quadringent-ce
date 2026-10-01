from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
from threading import Thread
import tempfile
import unittest

import site_fixture

from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
from quadringent_control_plane.server import serve
from quadringent_control_plane.audit import ActionAuditLog


SITE = site_fixture.build_test_site()
NOW = datetime.now(timezone.utc)
SECRET = "never-output-super-secret-token-value"
SECRET_URL = "https://snowflake.example/credential"
MANIFEST = (
    "ADDRS1",
    "CAL001",
    "COST1",
    "CUSTOM1",
    "ORDER",
    "EXPENS",
    "DATE01",
    "SALE",
    "PLACE01",
    "PLACES",
    "CNTR",
    "PRODUCT",
    "HOLIDAYS",
)
CATALOG_FLEET = {
    "fleet_id": SITE.fleet_id,
    "environment": SITE.environment,
    "capabilities": {
        "refresh": {"state": "available", "reason": None},
        "prepare": {"state": "available", "reason": None},
        "start": {"state": "available", "reason": None},
        "pause": {"state": "available", "reason": None},
        "resume": {"state": "available", "reason": None},
    },
}
CATALOG_PLAN = {
    "destination_namespace": SITE.destination_namespace,
    "observed_totals": {"table_count": 13},
}


def capture_document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 120}},
    }


def runtime_projection(
    *,
    phase: str = "NOT_PREPARED",
    checkpoint: dict[str, object] | None = None,
) -> dict[str, object]:
    if phase == "NOT_PREPARED":
        capabilities = {
            "prepare": {"state": "available", "reason": None},
            "start": {"state": "unavailable", "reason": "not_prepared"},
            "pause": {"state": "unavailable", "reason": "unsupported_action"},
            "resume": {"state": "unavailable", "reason": "unsupported_action"},
            "refresh": {"state": "unavailable", "reason": "unsupported_action"},
        }
    elif phase == "PREPARED":
        capabilities = {
            "prepare": {"state": "unavailable", "reason": "already_prepared"},
            "start": {"state": "available", "reason": None},
            "pause": {"state": "unavailable", "reason": "unsupported_action"},
            "resume": {"state": "unavailable", "reason": "unsupported_action"},
            "refresh": {"state": "unavailable", "reason": "unsupported_action"},
        }
    elif phase == "HISTORICAL":
        capabilities = {
            "prepare": {"state": "unavailable", "reason": "already_prepared"},
            "start": {"state": "unavailable", "reason": "already_started"},
            "pause": {"state": "unavailable", "reason": "unsupported_action"},
            "resume": {"state": "unavailable", "reason": "unsupported_action"},
            "refresh": {"state": "unavailable", "reason": "unsupported_action"},
        }
    elif phase == "BLOCKED":
        capabilities = {
            "prepare": {"state": "unavailable", "reason": "needs_recovery"},
            "start": {"state": "unavailable", "reason": "needs_recovery"},
            "pause": {"state": "unavailable", "reason": "unsupported_action"},
            "resume": {"state": "unavailable", "reason": "unsupported_action"},
            "refresh": {"state": "unavailable", "reason": "unsupported_action"},
        }
    else:
        capabilities = {
            "prepare": {"state": "unavailable", "reason": "invalid_runtime_state"},
            "start": {"state": "unavailable", "reason": "invalid_runtime_state"},
            "pause": {"state": "unavailable", "reason": "unsupported_action"},
            "resume": {"state": "unavailable", "reason": "unsupported_action"},
            "refresh": {"state": "unavailable", "reason": "unsupported_action"},
        }
    return {
        "format_version": "quadringent-fleet-runtime-v1",
        "fleet_id": SITE.fleet_id,
        "environment": SITE.environment,
        "pipeline_id": SITE.site_id,
        "phase": phase,
        "checkpoint": checkpoint,
        "capabilities": capabilities,
        "table_states": [
            {"name": name, "phase": phase, "copied_rows": None, "total_rows": None}
            for name in MANIFEST
        ],
    }


class _PipelineCatalog:
    def __init__(self, pipeline: object, *, fleet: object | None = None, fleet_plan: object | None = None) -> None:
        self._pipeline = pipeline
        self.fleet = fleet
        self.fleet_plan = fleet_plan
        self.id = pipeline.id
        self.environment = pipeline.environment

    def __getattr__(self, name: str) -> object:
        return getattr(self._pipeline, name)

    def to_dict(self) -> dict[str, object]:
        payload = self._pipeline.to_dict()
        if self.fleet is not None:
            payload["fleet"] = self.fleet
        if self.fleet_plan is not None:
            payload["fleet_plan"] = self.fleet_plan
        return payload


class _CatalogRepository:
    def __init__(
        self,
        inner: ProjectionRepository,
        *,
        fleet: object | None = None,
        fleet_plan: object | None = None,
    ) -> None:
        self._inner = inner
        self._fleet = fleet
        self._fleet_plan = fleet_plan

    def snapshot(self) -> object:
        snap = self._inner.snapshot()
        if self._fleet is None and self._fleet_plan is None:
            return snap
        return replace(
            snap,
            pipelines=tuple(
                _PipelineCatalog(item, fleet=self._fleet, fleet_plan=self._fleet_plan)
                for item in snap.pipelines
            ),
        )


class RuntimeExecutor:
    def __init__(self, projection: object, *, declare_support: bool = False) -> None:
        self.projection = projection
        self._declare_support = declare_support

    def project(self) -> object:
        if isinstance(self.projection, BaseException):
            raise self.projection
        if callable(self.projection):
            return self.projection()
        return deepcopy(self.projection)

    def supports(self, request: object) -> bool:
        return self._declare_support is True


class MutableRuntimeExecutor:
    def __init__(self) -> None:
        self.payload = runtime_projection(phase="NOT_PREPARED")

    def project(self) -> dict[str, object]:
        return deepcopy(self.payload)


class ControlPlaneFleetRuntimeHttpTests(unittest.TestCase):
    def _start(
        self,
        *,
        fleet: object | None = CATALOG_FLEET,
        fleet_plan: object | None = None,
        executor: object | None = None,
        source_id: str = SITE.site_id,
        environment: str = SITE.environment,
    ) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(capture_document()), encoding="utf-8")
        inner = ProjectionRepository(
            [parse_source_spec(f"live:{source_id}:file://{self.path}", environment=environment)]
        )
        inner.refresh()
        self.inner = inner
        self.repository = _CatalogRepository(inner, fleet=fleet, fleet_plan=fleet_plan)
        self.server = serve(self.repository, port=0, action_executor=executor, audit_log=ActionAuditLog(Path(self.directory.name) / "actions.jsonl"))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        server = getattr(self, "server", None)
        thread = getattr(self, "thread", None)
        directory = getattr(self, "directory", None)
        self.server = None
        self.thread = None
        self.directory = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        if directory is not None:
            directory.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, headers=headers or {})
        return connection, connection.getresponse()

    def _assert_redacted(self, *parts: object) -> None:
        encoded = " ".join(
            part if isinstance(part, str) else json.dumps(part, default=str) for part in parts
        )
        self.assertNotIn(SECRET, encoded)
        self.assertNotIn(SECRET_URL, encoded)
        self.assertNotIn("snowflake.example", encoded)

    def test_get_overview_list_and_detail_publish_exact_runtime(self) -> None:
        expected = runtime_projection(
            phase="PREPARED",
            checkpoint={"receiver": "DEMOJRN0100", "sequence": 250},
        )
        self._start(executor=RuntimeExecutor(expected, declare_support=True))

        connection, overview_response = self._request("GET", "/v1/overview")
        overview = json.loads(overview_response.read())
        connection.close()
        connection, list_response = self._request("GET", "/v1/pipelines")
        listing = json.loads(list_response.read())
        connection.close()
        connection, detail_response = self._request("GET", f"/v1/pipelines/{SITE.site_id}")
        detail = json.loads(detail_response.read())
        connection.close()

        self.assertEqual(overview_response.status, 200)
        self.assertEqual(list_response.status, 200)
        self.assertEqual(detail_response.status, 200)
        overview_pipeline = overview["pipelines"][0]
        list_pipeline = listing["pipelines"][0]
        detail_pipeline = detail["pipeline"]
        self.assertEqual(overview_pipeline["fleet_runtime"], expected)
        self.assertEqual(list_pipeline["fleet_runtime"], expected)
        self.assertEqual(detail_pipeline["fleet_runtime"], expected)
        self.assertEqual(overview_pipeline["fleet"]["capabilities"], expected["capabilities"])
        self.assertEqual(list_pipeline["fleet"]["capabilities"], expected["capabilities"])
        self.assertEqual(detail_pipeline["fleet"]["capabilities"], expected["capabilities"])
        self.assertEqual(overview_pipeline["fleet"]["fleet_id"], SITE.fleet_id)
        self.assertNotIn("fleet_plan", overview_pipeline)

    def test_head_matches_get_etag_on_all_runtime_routes(self) -> None:
        self._start(executor=RuntimeExecutor(runtime_projection(phase="UNKNOWN")))
        for path in ("/v1/overview", "/v1/pipelines", f"/v1/pipelines/{SITE.site_id}"):
            with self.subTest(path=path):
                connection, get_response = self._request("GET", path)
                body = get_response.read()
                get_etag = get_response.getheader("ETag")
                connection.close()
                connection, head_response = self._request("HEAD", path)
                head_body = head_response.read()
                head_etag = head_response.getheader("ETag")
                connection.close()
                self.assertEqual(get_response.status, 200)
                self.assertEqual(head_response.status, 200)
                self.assertEqual(head_etag, get_etag)
                self.assertTrue(get_etag)
                self.assertEqual(head_body, b"")
                self.assertIn(b"fleet_runtime", body)
                connection, cached = self._request("GET", path, {"If-None-Match": get_etag})
                self.assertEqual(cached.status, 304)
                self.assertEqual(cached.read(), b"")
                self.assertEqual(cached.getheader("ETag"), get_etag)
                connection.close()

    def test_etag_changes_when_runtime_changes_without_repository_revision(self) -> None:
        executor = MutableRuntimeExecutor()
        self._start(executor=executor)
        connection, first = self._request("GET", "/v1/overview")
        first_body = json.loads(first.read())
        first_etag = first.getheader("ETag")
        connection.close()
        revision = first_body["revision"]
        self.assertEqual(first_body["pipelines"][0]["fleet_runtime"]["phase"], "NOT_PREPARED")
        self.assertIsNone(first_body["pipelines"][0]["fleet_runtime"]["checkpoint"])

        executor.payload = runtime_projection(
            phase="PREPARED",
            checkpoint={"receiver": "DEMOJRN0100", "sequence": 250},
        )
        connection, second = self._request("GET", "/v1/overview")
        second_body = json.loads(second.read())
        second_etag = second.getheader("ETag")
        connection.close()
        self.assertEqual(second_body["revision"], revision)
        self.assertEqual(self.inner.snapshot().revision, revision)
        self.assertNotEqual(second_etag, first_etag)
        self.assertEqual(second_body["pipelines"][0]["fleet_runtime"]["phase"], "PREPARED")
        self.assertEqual(
            second_body["pipelines"][0]["fleet_runtime"]["checkpoint"],
            {"receiver": "DEMOJRN0100", "sequence": 250},
        )

        connection, head_response = self._request("HEAD", "/v1/overview")
        self.assertEqual(head_response.getheader("ETag"), second_etag)
        self.assertEqual(head_response.read(), b"")
        connection.close()

    def test_absent_fleet_and_plan_omits_runtime(self) -> None:
        self._start(fleet=None, fleet_plan=None, executor=RuntimeExecutor(runtime_projection()))
        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()
        pipeline = body["pipelines"][0]
        self.assertNotIn("fleet_runtime", pipeline)
        self.assertNotIn("fleet", pipeline)
        self.assertNotIn("fleet_plan", pipeline)

        connection, listing = self._request("GET", "/v1/pipelines")
        listed = json.loads(listing.read())
        connection.close()
        connection, detail = self._request("GET", f"/v1/pipelines/{SITE.site_id}")
        detailed = json.loads(detail.read())
        connection.close()
        self.assertNotIn("fleet_runtime", listed["pipelines"][0])
        self.assertNotIn("fleet_runtime", detailed["pipeline"])

    def _assert_fleet_plan_byte_for_byte(self, raw: bytes, pipeline: dict[str, object]) -> None:
        expected_plan = json.dumps(CATALOG_PLAN, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        rendered_plan = json.dumps(pipeline["fleet_plan"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(rendered_plan, expected_plan)
        self.assertIn(expected_plan, raw)
        self.assertEqual(pipeline["fleet_plan"], CATALOG_PLAN)
        self.assertNotIn("capabilities", pipeline["fleet_plan"])
        self.assertNotIn(b'"capabilities"', expected_plan)

    def test_fleet_plan_only_publishes_runtime_without_inventing_fleet(self) -> None:
        expected = runtime_projection(phase="BLOCKED")
        self._start(
            fleet=None,
            fleet_plan=deepcopy(CATALOG_PLAN),
            executor=RuntimeExecutor(expected, declare_support=True),
        )
        for path in ("/v1/overview", "/v1/pipelines", f"/v1/pipelines/{SITE.site_id}"):
            with self.subTest(path=path):
                connection, response = self._request("GET", path)
                raw = response.read()
                connection.close()
                body = json.loads(raw)
                pipeline = body["pipelines"][0] if "pipelines" in body else body["pipeline"]
                self.assertEqual(pipeline["fleet_runtime"], expected)
                self.assertNotIn("fleet", pipeline)
                self._assert_fleet_plan_byte_for_byte(raw, pipeline)

    def test_fleet_and_plan_keep_plan_byte_for_byte_when_runtime_is_published(self) -> None:
        expected = runtime_projection(phase="PREPARED", checkpoint={"receiver": "DEMOJRN0100", "sequence": 250})
        self._start(
            fleet=deepcopy(CATALOG_FLEET),
            fleet_plan=deepcopy(CATALOG_PLAN),
            executor=RuntimeExecutor(expected, declare_support=True),
        )
        connection, response = self._request("GET", "/v1/overview")
        raw = response.read()
        connection.close()
        pipeline = json.loads(raw)["pipelines"][0]
        self.assertEqual(pipeline["fleet_runtime"], expected)
        self.assertEqual(pipeline["fleet"]["capabilities"], expected["capabilities"])
        self._assert_fleet_plan_byte_for_byte(raw, pipeline)

    def test_malformed_projection_is_omitted_and_secret_redacted(self) -> None:
        malformed = runtime_projection()
        malformed["password"] = SECRET
        malformed["url"] = SECRET_URL
        self._start(executor=RuntimeExecutor(malformed))
        for path in ("/v1/overview", "/v1/pipelines", f"/v1/pipelines/{SITE.site_id}"):
            with self.subTest(path=path):
                connection, response = self._request("GET", path)
                raw = response.read()
                headers = dict(response.getheaders())
                connection.close()
                body = json.loads(raw)
                pipeline = body["pipelines"][0] if "pipelines" in body else body["pipeline"]
                self.assertNotIn("fleet_runtime", pipeline)
                self.assertIn("fleet", pipeline)
                self._assert_redacted(body, headers, raw.decode("utf-8"))

    def test_corrupt_tables_and_wrong_scope_are_omitted(self) -> None:
        truncated = runtime_projection()
        truncated["table_states"] = truncated["table_states"][:-1]
        self._start(executor=RuntimeExecutor(truncated))
        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()
        self.assertNotIn("fleet_runtime", body["pipelines"][0])
        self._stop()

        foreign = runtime_projection()
        foreign["pipeline_id"] = "other-source"
        self._start(executor=RuntimeExecutor(foreign))
        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()
        self.assertNotIn("fleet_runtime", body["pipelines"][0])
        self._stop()

        self._start(
            source_id="demo",
            executor=RuntimeExecutor(runtime_projection()),
        )
        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(body["pipelines"][0]["id"], "demo")
        self.assertNotIn("fleet_runtime", body["pipelines"][0])

    def test_project_exception_is_omitted_and_secret_redacted(self) -> None:
        error = RuntimeError(f"credential={SECRET} {SECRET_URL}")
        self._start(executor=RuntimeExecutor(error))
        for path in ("/v1/overview", "/v1/pipelines", f"/v1/pipelines/{SITE.site_id}"):
            with self.subTest(path=path):
                connection, response = self._request("GET", path)
                raw = response.read()
                headers = dict(response.getheaders())
                connection.close()
                self.assertEqual(response.status, 200)
                body = json.loads(raw)
                pipeline = body["pipelines"][0] if "pipelines" in body else body["pipeline"]
                self.assertNotIn("fleet_runtime", pipeline)
                self._assert_redacted(body, headers, raw.decode("utf-8"))

    def test_unknown_runtime_keeps_null_checkpoint(self) -> None:
        expected = runtime_projection(phase="UNKNOWN")
        self._start(executor=RuntimeExecutor(expected))
        connection, response = self._request("GET", f"/v1/pipelines/{SITE.site_id}")
        body = json.loads(response.read())
        connection.close()
        runtime = body["pipeline"]["fleet_runtime"]
        self.assertEqual(runtime["phase"], "UNKNOWN")
        self.assertIsNone(runtime["checkpoint"])
        self.assertNotEqual(runtime["checkpoint"], 0)
        self.assertNotEqual(runtime.get("checkpoint"), {"receiver": "DEMOJRN0100", "sequence": 0})

    def test_domain_phases_are_accepted_for_the_aggregate(self) -> None:
        for phase in ("CATCHING_UP", "LIVE", "RECONCILING", "CERTIFIED"):
            with self.subTest(phase=phase):
                expected = runtime_projection(phase=phase)
                self._start(executor=RuntimeExecutor(expected))
                connection, response = self._request(
                    "GET", f"/v1/pipelines/{SITE.site_id}"
                )
                body = json.loads(response.read())
                connection.close()
                runtime = body["pipeline"]["fleet_runtime"]
                self.assertEqual(runtime["phase"], phase)
                self.assertEqual(
                    [state["phase"] for state in runtime["table_states"]],
                    [phase] * len(MANIFEST),
                )
                self._stop()

    def test_mixed_table_phases_are_accepted(self) -> None:
        expected = runtime_projection(phase="HISTORICAL")
        expected["table_states"] = [
            {
                "name": name,
                "phase": "LIVE" if index < 4 else "READY",
                "copied_rows": 100 + index,
                "total_rows": 100 + index,
            }
            for index, name in enumerate(MANIFEST)
        ]
        self._start(executor=RuntimeExecutor(expected))
        connection, response = self._request("GET", f"/v1/pipelines/{SITE.site_id}")
        body = json.loads(response.read())
        connection.close()
        runtime = body["pipeline"]["fleet_runtime"]
        self.assertEqual(runtime["phase"], "HISTORICAL")
        self.assertEqual(runtime["table_states"][0]["phase"], "LIVE")
        self.assertEqual(runtime["table_states"][0]["copied_rows"], 100)
        self.assertEqual(runtime["table_states"][-1]["phase"], "READY")

    def test_unknown_table_phase_is_rejected(self) -> None:
        expected = runtime_projection(phase="HISTORICAL")
        expected["table_states"][0] = {
            "name": MANIFEST[0],
            "phase": "MELTING",
            "copied_rows": None,
            "total_rows": None,
        }
        self._start(executor=RuntimeExecutor(expected))
        connection, response = self._request("GET", f"/v1/pipelines/{SITE.site_id}")
        body = json.loads(response.read())
        connection.close()
        self.assertNotIn("fleet_runtime", body["pipeline"])


if __name__ == "__main__":
    unittest.main()
