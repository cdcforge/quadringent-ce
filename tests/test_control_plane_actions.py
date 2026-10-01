from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
from threading import Event, Thread
import tempfile
import unittest
from urllib.parse import quote

import site_fixture

from quadringent_control_plane.connections import CONNECTIONS_STATE_FILE, ConnectionsStore
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
from quadringent_control_plane.server import serve
from quadringent_control_plane.audit import ActionAuditLog


SITE = site_fixture.build_test_site()
NOW = datetime.now(timezone.utc)
AVAILABLE = {
    "fleet_id": SITE.fleet_id,
    "capabilities": {
        "refresh": {"state": "available"},
        "prepare": {"state": "available"},
        "start": {"state": "available"},
        "pause": {"state": "available"},
        "resume": {"state": "available"},
    },
}
SUCCESS_STAGES = {
    "intent": {"state": "recorded", "code": "intent_recorded", "message": "Intent recorded"},
    "execution": {"state": "completed", "code": "execution_completed", "message": "Execution completed"},
    "observed_effect": {"state": "succeeded", "code": "effect_observed", "message": "Effect observed"},
}


def document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "R", "sequence": 1},
            "source_tail": {"receiver": "R", "sequence": 2},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 120}},
    }


class _PipelineWithFleet:
    def __init__(self, pipeline: object, fleet: object) -> None:
        self._pipeline = pipeline
        self.fleet = fleet
        self.id = pipeline.id
        self.environment = pipeline.environment

    def __getattr__(self, name: str) -> object:
        return getattr(self._pipeline, name)

    def to_dict(self) -> dict[str, object]:
        payload = self._pipeline.to_dict()
        payload["fleet"] = self.fleet
        return payload


class _FleetRepository:
    def __init__(self, inner: ProjectionRepository, fleet: object | None) -> None:
        self._inner = inner
        self._fleet = fleet

    def snapshot(self) -> object:
        snap = self._inner.snapshot()
        if self._fleet is None:
            return snap
        return replace(
            snap,
            pipelines=tuple(
                _PipelineWithFleet(item, self._fleet) for item in snap.pipelines
            ),
        )


class FakeSuccessExecutor:
    def execute(self, request: object) -> dict[str, object]:
        return dict(SUCCESS_STAGES)


class SupportingSuccessExecutor(FakeSuccessExecutor):
    def supports(self, request: object) -> bool:
        return getattr(request, "action", None) == "refresh"


class BlockingExecutor:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    def execute(self, request: object) -> dict[str, object]:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        return dict(SUCCESS_STAGES)


class ThrowingExecutor:
    def execute(self, request: object) -> dict[str, object]:
        raise RuntimeError("snowflake.amazonaws.com credential=AKIASECRET")


class MalformedExecutor:
    def execute(self, request: object) -> dict[str, object]:
        return {
            "intent": {"state": "recorded", "code": "intent_recorded", "message": "Intent recorded"},
            "sql": "DROP TABLE SALE",
            "url": "https://snowflake.example/secret",
        }


class ControlPlaneActionTests(unittest.TestCase):
    def _start(
        self,
        *,
        environment: str = SITE.environment,
        fleet: object | None = AVAILABLE,
        executor: object | None = None,
        source_id: str = "demo",
        auth: object | None = None,
    ) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        inner = ProjectionRepository(
            [parse_source_spec(f"simulation:{source_id}:file://{self.path}", environment=environment)]
        )
        inner.refresh()
        self.repository = _FleetRepository(inner, fleet)
        self.audit = ActionAuditLog(Path(self.directory.name) / "actions.jsonl")
        self.server = serve(self.repository, port=0, action_executor=executor, audit_log=self.audit, auth=auth)
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

    def _post(
        self,
        action: str,
        payload: object,
        *,
        pipeline_id: str = "demo",
        timeout: float = 2,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        if isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload).encode("utf-8")
        encoded = quote(pipeline_id, safe="")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=timeout)
        connection.request(
            "POST",
            f"/v1/pipelines/{encoded}/actions/{action}",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        return connection, connection.getresponse()

    def _refresh_payload(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "fleet_id": SITE.fleet_id,
            "environment": "TEST",
            "confirmation": None,
        }
        payload.update(changes)
        return payload

    def test_happy_success_via_fake_executor(self) -> None:
        self._start(executor=FakeSuccessExecutor())
        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(body["action"], "refresh")
        self.assertEqual(body["fleet_id"], SITE.fleet_id)
        self.assertEqual(body["environment"], SITE.environment)
        self.assertEqual(body["state"], "succeeded")
        self.assertRegex(body["id"], r"^[A-Za-z0-9_-]{8,128}$")
        self.assertIn("+00:00", body["created_at"])
        self.assertEqual(body["stages"]["intent"]["state"], "recorded")
        self.assertEqual(body["stages"]["execution"]["state"], "completed")
        self.assertEqual(body["stages"]["observed_effect"]["state"], "succeeded")
        self.assertEqual(
            set(body),
            {"id", "action", "fleet_id", "environment", "created_at", "state", "stages"},
        )
        serialized = json.dumps(body)
        self.assertNotIn("SELECT", serialized)
        self.assertNotIn("://", serialized)

    def test_audit_refuse_avant_tout_effet(self):
        executor = RecordingExecutor()
        self._start(executor=executor)
        self.audit.path.unlink()
        self.audit.path.mkdir()
        connection, response = self._post("refresh", self._refresh_payload())
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.read())["error"]["code"], "audit_unavailable")
        connection.close()
        self.assertEqual(executor.calls, [])
        for route in ("/v1/overview", "/v1/pipelines/demo"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
            connection.request("GET", route)
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            pipeline = body["pipelines"][0] if "pipelines" in body else body["pipeline"]
            self.assertTrue(all(cap["state"] == "unavailable" for cap in pipeline["fleet"]["capabilities"].values()))

    def test_http_conserve_l_identite_authentifiee_et_le_recu(self):
        from quadringent_control_plane.auth import AuthConfig
        self._start(executor=FakeSuccessExecutor(), auth=AuthConfig.build(
            user_header="X-User", groups_header="X-Groups", operator_groups="ops"))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/v1/pipelines/demo/actions/refresh", json.dumps(self._refresh_payload()),
                           {"Content-Type": "application/json", "X-User": "operator@example.invalid", "X-Groups": "ops"})
        response = connection.getresponse()
        receipt = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        records = [json.loads(line) for line in self.audit.path.read_text().splitlines()]
        self.assertEqual(records[-2]["actor"], "operator@example.invalid")
        self.assertEqual(records[-1]["receipt_id"], receipt["id"])
        self.assertEqual(records[-1]["observed_effect"], "succeeded")

    def test_invalid_json_oversize_and_unknown_fields(self) -> None:
        self._start(executor=FakeSuccessExecutor())

        connection, response = self._post("refresh", b"{")
        raw = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(raw), {"error": {"code": "invalid_request"}})

        connection, response = self._post("refresh", [self._refresh_payload()])
        connection.close()
        self.assertEqual(response.status, 400)

        oversize = b"x" * (4 * 1024 + 1)
        connection, response = self._post("refresh", oversize)
        connection.close()
        self.assertEqual(response.status, 400)

        connection, response = self._post(
            "refresh",
            self._refresh_payload(extra="nope"),
        )
        connection.close()
        self.assertEqual(response.status, 400)

        connection, response = self._post("refresh", self._refresh_payload(fleet_id="CNTR"))
        connection.close()
        self.assertEqual(response.status, 400)

    def test_exact_confirmation(self) -> None:
        self._start(executor=FakeSuccessExecutor())

        connection, response = self._post("refresh", self._refresh_payload(confirmation="PREPARE ACME TEST"))
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "wrong_confirmation")
        connection.close()

        connection, response = self._post(
            "prepare",
            {"fleet_id": SITE.fleet_id, "environment": SITE.environment, "confirmation": None},
        )
        self.assertEqual(response.status, 403)
        connection.close()

        connection, response = self._post(
            "prepare",
            {"fleet_id": SITE.fleet_id, "environment": "TEST", "confirmation": "prepare acme test"},
        )
        self.assertEqual(response.status, 403)
        connection.close()

        connection, response = self._post(
            "start",
            {"fleet_id": SITE.fleet_id, "environment": "TEST", "confirmation": "START ACME TEST "},
        )
        self.assertEqual(response.status, 403)
        connection.close()

        connection, response = self._post(
            "prepare",
            {"fleet_id": SITE.fleet_id, "environment": "TEST", "confirmation": "PREPARE ACME TEST"},
        )
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["action"], "prepare")
        self.assertEqual(body["state"], "succeeded")

        connection, response = self._post(
            "pause",
            {"fleet_id": SITE.fleet_id, "environment": "TEST", "confirmation": "PAUSE ACME TEST"},
        )
        self.assertEqual(response.status, 200)
        connection.close()

    def test_prod_and_non_dev_are_refused(self) -> None:
        self._start(environment="prod", executor=FakeSuccessExecutor())
        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(body["error"]["code"], "wrong_environment")
        self.assertNotIn("demo", json.dumps(body))

        self._stop()
        self._start(executor=FakeSuccessExecutor())
        connection, response = self._post("refresh", self._refresh_payload(environment="prod"))
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["code"], "wrong_environment")
        connection.close()

    def test_missing_pipeline_is_hidden(self) -> None:
        self._start(executor=FakeSuccessExecutor())
        connection, response = self._post(
            "refresh",
            self._refresh_payload(),
            pipeline_id="secret-id-never-returned",
        )
        raw = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 404)
        self.assertNotIn("secret-id-never-returned", raw)
        self.assertEqual(json.loads(raw)["error"]["code"], "not_found")

    def test_unavailable_capability_is_conflict(self) -> None:
        blocked = {
            "fleet_id": SITE.fleet_id,
            "capabilities": {
                "refresh": {"state": "blocked"},
                "prepare": {"state": "available"},
                "start": {"state": "available"},
                "pause": {"state": "available"},
            },
        }
        self._start(fleet=blocked, executor=FakeSuccessExecutor())
        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["state"], "unavailable")
        self.assertEqual(body["stages"]["intent"]["code"], "capability_unavailable")

    def test_configured_executor_can_publish_and_execute_refresh_capability(self) -> None:
        fleet = {
            **AVAILABLE,
            "capabilities": {
                **AVAILABLE["capabilities"],
                "refresh": {"state": "unavailable", "reason": "historical blocker"},
            },
        }
        self._start(fleet=fleet, executor=SupportingSuccessExecutor())
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        connection.request("GET", "/v1/overview")
        response = connection.getresponse()
        overview = json.loads(response.read())
        connection.close()
        capability = overview["pipelines"][0]["fleet"]["capabilities"]["refresh"]
        self.assertEqual(capability, {"state": "available", "reason": None})

        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["state"], "succeeded")
        self.assertEqual(body["action"], "refresh")
        self.assertEqual(body["environment"], SITE.environment)

    def test_missing_executor_fails_closed(self) -> None:
        self._start(executor=None)
        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["state"], "unavailable")
        self.assertEqual(body["stages"]["intent"]["code"], "executor_unavailable")

        self._stop()
        self._start(executor=object())
        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["state"], "unavailable")
        self.assertEqual(body["stages"]["intent"]["code"], "executor_unavailable")
        self.assertEqual(body["stages"]["intent"]["state"], "rejected")
        self.assertEqual(body["stages"]["execution"]["state"], "not_started")
        self.assertEqual(body["stages"]["observed_effect"]["state"], "unknown")

        self._stop()
        self._start(fleet=None, executor=FakeSuccessExecutor())
        connection, response = self._post("refresh", self._refresh_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["stages"]["intent"]["code"], "capability_unavailable")

    def test_concurrent_same_pipeline_is_rejected(self) -> None:
        blocker = BlockingExecutor()
        self._start(executor=blocker)
        payload = self._refresh_payload()
        first: dict[str, object] = {}

        def run_first() -> None:
            connection, response = self._post("refresh", payload, timeout=3)
            first["status"] = response.status
            first["body"] = json.loads(response.read())
            connection.close()

        worker = Thread(target=run_first)
        worker.start()
        self.assertTrue(blocker.entered.wait(timeout=1))
        connection, response = self._post("refresh", payload)
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 409)
        self.assertEqual(body["state"], "conflict")
        self.assertEqual(body["stages"]["intent"]["code"], "action_in_progress")
        blocker.release.set()
        worker.join(timeout=3)
        self.assertEqual(first["status"], 200)
        self.assertEqual(blocker.calls, 1)

    def test_malformed_and_throwing_executor_are_redacted(self) -> None:
        self._start(executor=ThrowingExecutor())
        connection, response = self._post("refresh", self._refresh_payload())
        raw = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(raw), {"error": {"code": "internal_error"}})
        self.assertNotIn("snowflake", raw.lower())
        self.assertNotIn("AKIASECRET", raw)
        self.assertNotIn("RuntimeError", raw)

        self._stop()
        self._start(executor=MalformedExecutor())
        connection, response = self._post("refresh", self._refresh_payload())
        raw = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(raw), {"error": {"code": "internal_error"}})
        self.assertNotIn("DROP TABLE", raw)
        self.assertNotIn("https://", raw)
        self.assertNotIn("snowflake", raw.lower())

    def test_valid_action_route_allows_post_only(self) -> None:
        self._start(executor=FakeSuccessExecutor())
        for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
            connection.request(method, "/v1/pipelines/demo/actions/refresh")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.getheader("Allow"), "POST", method)
            self.assertEqual(response.getheader("Cache-Control"), "no-store", method)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"), method)
            connection.close()

    def test_onboarding_post_contract_is_unchanged(self) -> None:
        self._start(executor=FakeSuccessExecutor())
        body = json.dumps(
            {
                "step": "source",
                "ibmi_host": "ibmi.acme.invalid",
                "ibmi_user": "CDCUSER",
                "tls": True,
            }
        ).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "POST",
            "/v1/onboarding/evaluate",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")


class RecordingExecutor(FakeSuccessExecutor):
    def __init__(self) -> None:
        self.calls: list[object] = []

    def execute(self, request: object) -> dict[str, object]:
        self.calls.append(request)
        return dict(SUCCESS_STAGES)


class ControlPlanePostGuardTests(unittest.TestCase):
    """Une écriture non locale est refusée avant lecture du corps et sans exécuteur."""

    def _start(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        inner = ProjectionRepository(
            [parse_source_spec(f"simulation:demo:file://{self.path}", environment=SITE.environment)]
        )
        inner.refresh()
        self.executor = RecordingExecutor()
        self.connections_state_directory = tempfile.TemporaryDirectory()
        self.connections_store = ConnectionsStore(
            AtomicJsonStateStore(Path(self.connections_state_directory.name) / CONNECTIONS_STATE_FILE)
        )
        self.server = serve(
            _FleetRepository(inner, AVAILABLE),
            port=0,
            action_executor=self.executor,
            audit_log=ActionAuditLog(Path(self.directory.name) / "actions.jsonl"),
            connections_store=self.connections_store,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.addCleanup(self.connections_state_directory.cleanup)

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

    def _post(
        self,
        path: str,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, object]:
        body = json.dumps(payload).encode("utf-8")
        merged: dict[str, str] = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if headers:
            merged.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", path, body=body, headers=merged)
        response = connection.getresponse()
        try:
            parsed = json.loads(response.read())
        except ValueError:
            parsed = None
        return connection, response, parsed

    def _action_payload(self) -> dict[str, object]:
        return {"fleet_id": SITE.fleet_id, "environment": "TEST", "confirmation": None}

    def _assert_refused(self, response: http.client.HTTPResponse, parsed: object, status: int, code: str) -> None:
        self.assertEqual(response.status, status)
        self.assertEqual(parsed, {"error": {"code": code}})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(self.executor.calls, [])

    def test_distant_origin_is_refused_without_calling_executor(self) -> None:
        self._start()
        connection, response, parsed = self._post(
            "/v1/pipelines/demo/actions/refresh",
            self._action_payload(),
            headers={"Origin": "https://evil.example"},
        )
        connection.close()
        self._assert_refused(response, parsed, 403, "forbidden_origin")

    def test_cross_site_metadata_is_refused_without_calling_executor(self) -> None:
        self._start()
        connection, response, parsed = self._post(
            "/v1/pipelines/demo/actions/refresh",
            self._action_payload(),
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://127.0.0.1:5180"},
        )
        connection.close()
        self._assert_refused(response, parsed, 403, "forbidden_origin")

    def test_foreign_host_is_refused_before_body_is_read(self) -> None:
        self._start()
        connection, response, parsed = self._post(
            "/v1/pipelines/demo/actions/refresh",
            self._action_payload(),
            headers={"Host": "rebind.example", "Origin": "http://127.0.0.1:5180"},
        )
        connection.close()
        self._assert_refused(response, parsed, 403, "invalid_host")

    def test_non_json_body_is_refused_without_calling_executor(self) -> None:
        self._start()
        connection, response, parsed = self._post(
            "/v1/pipelines/demo/actions/refresh",
            self._action_payload(),
            headers={"Content-Type": "text/plain"},
        )
        connection.close()
        self._assert_refused(response, parsed, 415, "unsupported_media_type")

    def test_missing_content_type_is_refused(self) -> None:
        self._start()
        body = json.dumps(self._action_payload()).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "POST",
            "/v1/pipelines/demo/actions/refresh",
            body=body,
            headers={"Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        parsed = json.loads(response.read())
        connection.close()
        self._assert_refused(response, parsed, 415, "unsupported_media_type")

    def test_onboarding_evaluate_refuses_distant_origin(self) -> None:
        self._start()
        connection, response, parsed = self._post(
            "/v1/onboarding/evaluate",
            {"step": "source", "ibmi_host": "ibmi.acme.invalid", "ibmi_user": "CDCUSER", "tls": True},
            headers={"Origin": "https://evil.example"},
        )
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(parsed, {"error": {"code": "forbidden_origin"}})

    def test_connections_create_refuses_distant_origin_without_persisting(self) -> None:
        """Même garde que /v1/onboarding/evaluate : aucune écriture disque avant le contrôle."""
        self._start()
        connection, response, parsed = self._post(
            "/v1/connections",
            {"ibmi_host": "ibmi.acme.invalid", "ibmi_user": "CDCUSER", "tls": True},
            headers={"Origin": "https://evil.example"},
        )
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(parsed, {"error": {"code": "forbidden_origin"}})
        self.assertEqual(self.connections_store.list(), ())

    def test_connections_create_refuses_a_foreign_host_before_reading_the_body(self) -> None:
        self._start()
        connection, response, parsed = self._post(
            "/v1/connections",
            {"ibmi_host": "ibmi.acme.invalid", "ibmi_user": "CDCUSER", "tls": True},
            headers={"Host": "rebind.example", "Origin": "http://127.0.0.1:5180"},
        )
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(parsed, {"error": {"code": "invalid_host"}})
        self.assertEqual(self.connections_store.list(), ())

    def test_same_origin_action_still_reaches_executor(self) -> None:
        self._start()
        origin = f"http://127.0.0.1:{self.server.server_port}"
        connection, response, parsed = self._post(
            "/v1/pipelines/demo/actions/refresh",
            self._action_payload(),
            headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"},
        )
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(getattr(parsed, "get", lambda *_: None)("state"), "succeeded")

    def test_local_dev_proxy_origin_still_reaches_executor(self) -> None:
        self._start()
        connection, response, _parsed = self._post(
            "/v1/pipelines/demo/actions/refresh",
            self._action_payload(),
            headers={"Origin": "http://localhost:5180", "Sec-Fetch-Site": "same-origin"},
        )
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
