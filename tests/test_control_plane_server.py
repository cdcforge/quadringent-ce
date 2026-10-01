from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import http.client
import json
from pathlib import Path
import os
import subprocess
import sys
import socket
import struct
import tempfile
from threading import Event, Thread
from time import monotonic
import unittest

import site_fixture
from quadringent_control_plane.connections import (
    CONNECTIONS_STATE_FILE,
    LIFECYCLE_DECLARED_NOT_IN_SERVICE,
    ConnectionsStore,
)
from quadringent_control_plane.fleet_runtime_store import AtomicJsonStateStore
from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
from quadringent_control_plane.server import _etag, _write_sse, serve


SITE = site_fixture.build_test_site()
NOW = datetime.now(timezone.utc)


def document(*, counter: int = 120) -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {"checkpoint": {"receiver": "R", "sequence": 1}, "source_tail": {"receiver": "R", "sequence": 2}},
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": counter}},
    }


class ControlPlaneServerTests(unittest.TestCase):
    def test_etag_does_not_collide_when_a_restarted_process_reuses_revision(self) -> None:
        before = self.repository.snapshot()
        restarted = replace(before, generated_at=before.generated_at + timedelta(seconds=1))
        self.assertEqual(before.revision, restarted.revision)
        self.assertNotEqual(_etag(before), _etag(restarted))
        self.assertEqual(_etag(before), _etag(before))

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{self.path}")])
        self.repository.refresh()
        self.server = serve(self.repository, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _request(self, method: str, path: str, headers: dict[str, str] | None = None) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, headers=headers or {})
        return connection, connection.getresponse()

    def test_overview_json_is_private_and_etag_revalidates(self) -> None:
        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json; charset=utf-8")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(body["pipelines"][0]["quality"]["evidence_kind"], "simulation")
        self.assertNotEqual(body["pipelines"][0]["status"], "healthy")
        self.assertEqual(body["scope"], {"kind": "single", "environments": ["local"]})
        etag = response.getheader("ETag")
        connection.close()

        self.repository.refresh()
        connection, unchanged = self._request("GET", "/v1/overview")
        self.assertEqual(unchanged.getheader("ETag"), etag)
        self.assertEqual(json.loads(unchanged.read()), body)
        connection.close()

        connection, not_modified = self._request("GET", "/v1/overview", {"If-None-Match": etag})
        self.assertEqual(not_modified.status, 304)
        self.assertEqual(not_modified.read(), b"")
        connection.close()

    def test_routes_are_read_only_and_not_found_hides_requested_identifier(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "FROB"):
            connection, response = self._request(method, "/v1/overview")
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.getheader("Allow"), "GET, HEAD", method)
            connection.close()

        connection, response = self._request("GET", "/v1/pipelines/secret-id-never-returned")
        body = response.read().decode("utf-8")
        self.assertEqual(response.status, 404)
        self.assertNotIn("secret-id-never-returned", body)
        connection.close()

    def test_sse_emits_cursor_then_revision_update(self) -> None:
        connection, response = self._request("GET", "/v1/events")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        cursor_frame = b"".join(response.fp.readline() for _ in range(3))
        self.assertIn(b"event: stream.cursor", cursor_frame)

        self.path.write_text(json.dumps(document(counter=121)), encoding="utf-8")
        self.repository.refresh()
        update_frame = b"".join(response.fp.readline() for _ in range(4))
        self.assertIn(b"event: projection.updated", update_frame)
        self.assertIn(b'"revision":2', update_frame)
        connection.close()

    def test_sse_resets_when_last_event_id_is_older_than_retained_history(self) -> None:
        for counter in range(121, 187):
            self.path.write_text(json.dumps(document(counter=counter)), encoding="utf-8")
            self.repository.refresh()

        connection, response = self._request("GET", "/v1/events", {"Last-Event-ID": "1"})
        cursor_frame = b"".join(response.fp.readline() for _ in range(4))
        reset_frame = b"".join(response.fp.readline() for _ in range(4))

        self.assertIn(b"event: stream.cursor", cursor_frame)
        self.assertIn(b"event: projection.reset", reset_frame)
        connection.close()

    def test_sse_future_cursor_resets_and_receives_next_update_after_restart(self) -> None:
        current = self.repository.snapshot()
        event, snapshots = self.repository.events_after(current.revision + 100)
        self.assertEqual(event, "reset")
        self.assertEqual(snapshots, (current,))
        connection, response = self._request(
            "GET", "/v1/events", {"Last-Event-ID": str(current.revision + 100)}
        )
        try:
            cursor_frame = b"".join(response.fp.readline() for _ in range(4))
            reset_frame = b"".join(response.fp.readline() for _ in range(4))
            self.assertIn(b"event: stream.cursor", cursor_frame)
            self.assertIn(b"event: projection.reset", reset_frame)
            self.assertIn(f"id: {current.revision}".encode(), reset_frame)
            self.path.write_text(json.dumps(document(counter=121)), encoding="utf-8")
            updated = self.repository.refresh()
            update_frame = b"".join(response.fp.readline() for _ in range(4))
            self.assertIn(b"event: projection.updated", update_frame)
            self.assertIn(f"id: {updated.revision}".encode(), update_frame)
        finally:
            connection.close()

    def test_sse_replays_each_retained_revision_after_last_event_id(self) -> None:
        self.path.write_text(json.dumps(document(counter=121)), encoding="utf-8")
        second = self.repository.refresh()
        self.path.write_text(json.dumps(document(counter=122)), encoding="utf-8")
        third = self.repository.refresh()

        connection, response = self._request("GET", "/v1/events", {"Last-Event-ID": "1"})
        cursor_frame = b"".join(response.fp.readline() for _ in range(4))
        second_frame = b"".join(response.fp.readline() for _ in range(4))
        third_frame = b"".join(response.fp.readline() for _ in range(4))

        self.assertIn(b"event: stream.cursor", cursor_frame)
        self.assertIn(f"id: {second.revision}".encode(), second_frame)
        self.assertIn(f"id: {third.revision}".encode(), third_frame)
        connection.close()

    def test_cli_refuses_non_loopback_without_explicit_opt_in(self) -> None:
        rendered = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_control_plane.py",
                "--source",
                f"simulation:demo:file://{self.path}",
                "--host",
                "0.0.0.0",
            ],
            cwd=Path(__file__).parents[1],
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            timeout=3,
        )

        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("loopback", rendered.stderr)

    def test_cli_has_no_non_loopback_escape_hatch(self) -> None:
        rendered = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_control_plane.py",
                "--source",
                f"simulation:demo:file://{self.path}",
                "--host",
                "0.0.0.0",
                "--allow-non-loopback",
            ],
            cwd=Path(__file__).parents[1],
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            timeout=3,
        )

        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("unrecognized arguments: --allow-non-loopback", rendered.stderr)

    def test_cli_environment_is_reflected_by_the_served_overview(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        process = subprocess.Popen(
            [
                sys.executable,
                "scripts/quadringent_control_plane.py",
                "--source",
                f"live:dev-e2e:file://{self.path}",
                "--environment",
                "dev",
                "--port",
                str(port),
            ],
            cwd=Path(__file__).parents[1],
            env={**os.environ, "PYTHONPATH": "src"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            body = None
            # Ce test vérifie le contrat HTTP, pas un démarrage sous une seconde.
            deadline = monotonic() + 5
            while monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=0.1
                    )
                    connection.request("GET", "/v1/overview")
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    connection.close()
                    break
                except OSError:
                    Event().wait(0.025)
            stopped = process.poll()
            failure = process.stderr.read() if stopped is not None else ""
            self.assertIsNone(stopped, failure)
            self.assertIsNotNone(body)
            self.assertEqual(body["scope"], {"kind": "single", "environments": ["dev"]})
            self.assertEqual(body["pipelines"][0]["environment"], "dev")
        finally:
            process.terminate()
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_server_refuses_non_loopback_bind_even_without_cli(self) -> None:
        for host in ("0.0.0.0", "192.0.2.1"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                serve(self.repository, host=host, port=0)

    def test_rest_never_exposes_unknown_or_secret_like_numeric_counters(self) -> None:
        payload = document()
        payload["counters"].update(
            {
                "password": {"value": 1},
                "raw_payload": {"value": 2},
                "secret_token": {"value": 3},
                "unknown_numeric": {"value": 4},
            }
        )
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.repository.refresh()

        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()

        counters = body["pipelines"][0]["counters"]
        self.assertEqual(counters["events_published"], 120)
        self.assertNotIn("password", counters)
        self.assertNotIn("raw_payload", counters)
        self.assertNotIn("secret_token", counters)
        self.assertNotIn("unknown_numeric", counters)

    def test_rest_retains_last_incident_when_source_refresh_fails(self) -> None:
        payload = document()
        payload["run"] = {
            "state": "STOPPED_FAIL_CLOSED",
            "last_error": {"type": "JdbcFailure", "password": "never-output"},
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        incident = self.repository.refresh()
        self.path.unlink()
        unavailable = self.repository.refresh()

        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(unavailable.revision, incident.revision + 1)
        self.assertEqual(len(body["pipelines"]), 1)
        self.assertEqual(body["pipelines"][0]["status"], "unknown")
        self.assertEqual(body["pipelines"][0]["quality"]["freshness"], "stale")
        self.assertEqual(
            body["pipelines"][0]["incident"],
            {"code": "capture_stopped_fail_closed", "type": "capture_connection_failure"},
        )
        self.assertEqual(body["pipelines"][0]["counters"]["events_published"], 120)
        self.assertEqual(body["sources"][0]["status"], "unavailable")
        self.assertEqual(body["sources"][0]["error"], "source_refresh_failed")
        self.assertNotIn("password", json.dumps(body))

    def test_sse_write_disconnects_are_silent(self) -> None:
        class ClosedWriter:
            def write(self, _: bytes) -> None:
                raise BrokenPipeError

            def flush(self) -> None:
                raise ConnectionResetError

        self.assertFalse(_write_sse(ClosedWriter(), b"event: projection.updated\n\n"))

    def test_sse_disconnect_during_headers_does_not_reach_server_error_handler(self) -> None:
        entered_headers = Event()
        release_headers = Event()
        headers_finished = Event()
        errors: list[object] = []
        handler = self.server.RequestHandlerClass
        original_headers = handler._sse_headers

        def delayed_headers(request_handler: object) -> None:
            entered_headers.set()
            release_headers.wait(timeout=1)
            try:
                original_headers(request_handler)
            finally:
                headers_finished.set()

        handler._sse_headers = delayed_headers
        self.server.handle_error = lambda request, client_address: errors.append(client_address)  # type: ignore[method-assign]
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1)
        try:
            client.sendall(b"GET /v1/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertTrue(entered_headers.wait(timeout=1))
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            release_headers.set()
            self.assertTrue(headers_finished.wait(timeout=1))
            self.assertEqual(errors, [])
        finally:
            handler._sse_headers = original_headers
            release_headers.set()

    def test_overview_sends_security_headers(self) -> None:
        connection, response = self._request("GET", "/v1/overview")
        response.read()
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy") or "")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        connection.close()

    def test_onboarding_evaluate_is_pure_and_rejects_secrets(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        body = json.dumps(
            {
                "step": "source",
                "ibmi_host": "192.0.2.10",
                "ibmi_user": "CDCUSER",
                "tls": True,
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/v1/onboarding/evaluate",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["defaults"]["replica_count_at_rest"], 0)
        connection.close()

        secret_body = json.dumps({"step": "source", "password": "leak-me"}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "POST",
            "/v1/onboarding/evaluate",
            body=secret_body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(secret_body))},
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertNotIn("leak-me", raw)
        self.assertIn("secret", json.loads(raw)["blocked"][0].lower())
        connection.close()

    def test_onboarding_evaluate_reject_non_post_with_allow_post(self) -> None:
        connection, response = self._request("PUT", "/v1/onboarding/evaluate")
        self.assertEqual(response.status, 405)
        self.assertEqual(response.getheader("Allow"), "POST")
        connection.close()

    def test_onboarding_http_cannot_promote_client_claims_to_runtime_proof(self) -> None:
        from test_onboarding import spec

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("POST", "/v1/onboarding/evaluate",
            body=json.dumps(spec(step="activate", connectivity="ok", pilot="pass")),
            headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["proven"], [])
        self.assertEqual(payload["verification_scope"], "configuration_only")
        self.assertTrue(payload["declared"])
        connection.close()

    def test_onboarding_defaults_are_readable(self) -> None:
        connection, response = self._request("GET", "/v1/onboarding/defaults")
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["defaults"]["allow_plaintext"], False)
        self.assertEqual(payload["defaults"]["tls_ca_file"], SITE.tls_ca_file)
        connection.close()

    def test_onboarding_defaults_publish_the_declared_site_identity(self) -> None:
        connection, response = self._request("GET", "/v1/onboarding/defaults")
        payload = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        site = payload["site"]
        self.assertEqual(site["site_id"], SITE.site_id)
        self.assertEqual(site["fleet_id"], SITE.fleet_id)
        self.assertEqual(site["environment"], SITE.fleet_environment)
        self.assertEqual(site["runtime_environment"], SITE.environment)
        self.assertEqual(site["destination_namespace"], SITE.destination_namespace)
        self.assertEqual(site["tables"], list(SITE.fleet_tables))
        self.assertEqual(site["proof_table"], SITE.proof_table)
        self.assertEqual(site["runtime_pipeline_id"], SITE.site_id)
        self.assertEqual(site["ibmi_host"], SITE.ibmi_host)
        self.assertEqual(site["ibmi_user"], SITE.ibmi_user)
        self.assertEqual(site["snowflake_stage"], SITE.proof_stage)
        # Seules les métadonnées de la référence sont publiées — jamais la
        # valeur du secret ni aucun autre champ sensible.
        self.assertNotIn("password", payload["defaults"])
        serialized = json.dumps(payload).lower()
        for forbidden in ("iseries_password_value", "jdbc:", "password="):
            self.assertNotIn(forbidden, serialized)


class ControlPlaneUiServingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.path = root / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        self.ui_dist = root / "dist"
        assets = self.ui_dist / "assets"
        assets.mkdir(parents=True)
        (self.ui_dist / "index.html").write_text(
            "<!doctype html><title>Quadringent</title><script type='module' src='/assets/app.js'></script>",
            encoding="utf-8",
        )
        (assets / "app.js").write_text("window.__QUADRINGENT__=true;", encoding="utf-8")
        (self.ui_dist / "favicon.ico").write_bytes(b"ico")
        (self.ui_dist / "console-dev.json").write_text("{}", encoding="utf-8")
        self.repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{self.path}")])
        self.repository.refresh()
        self.server = serve(self.repository, port=0, ui_dist=self.ui_dist)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _get(self, path: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", path)
        return connection, connection.getresponse()

    def test_index_and_assets_are_served_same_origin(self) -> None:
        connection, response = self._get("/")
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type") or "")
        self.assertIn("Quadringent", html)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy") or "")
        connection.close()

        connection, response = self._get("/assets/app.js")
        script = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("javascript", response.getheader("Content-Type") or "")
        self.assertEqual(script, "window.__QUADRINGENT__=true;")
        connection.close()

        connection, response = self._get("/v1/overview")
        body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertNotEqual(body["pipelines"][0]["status"], "healthy")
        connection.close()

    def test_favicon_is_served_from_ui_dist(self) -> None:
        connection, response = self._get("/favicon.ico")
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn("icon", (response.getheader("Content-Type") or "").lower())
        self.assertEqual(body, b"ico")
        connection.close()

    def test_path_traversal_and_fixture_names_are_not_served(self) -> None:
        connection, response = self._get("/assets/../console-dev.json")
        self.assertEqual(response.status, 404)
        connection.close()

        connection, response = self._get("/console-dev.json")
        self.assertEqual(response.status, 404)
        self.assertNotIn("fixture", response.read().decode("utf-8").lower())
        connection.close()


def _connection_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ibmi_host": "ibmi.acme.invalid",
        "ibmi_user": "CDCAPP",
        "tls": True,
        "allow_plaintext": False,
        "secret_ref_name": "acme-test-ibmi",
        "secret_ref_key": "ISERIES_PASSWORD",
        "schema": "LEDGER",
        "table": "SALE",
        "journal_library": "DEMOLIB",
        "journal_name": "TRNJRN",
        "snowflake_database": "ACME_RAW",
        "snowflake_schema": "IBMI_TEST",
        "snowflake_stage": "IBMI_TEST_SALE_EXTERNAL_STAGE",
        "display_name": "Site Acme (test)",
    }
    payload.update(overrides)
    return payload


class ControlPlaneConnectionsTests(unittest.TestCase):
    """GET/POST /v1/connections — persistance des liaisons via le control plane."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository(
            [parse_source_spec(f"simulation:demo:file://{self.path}")]
        )
        self.repository.refresh()
        self.state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_directory.cleanup)
        self.connections_store = ConnectionsStore(
            AtomicJsonStateStore(Path(self.state_directory.name) / CONNECTIONS_STATE_FILE)
        )
        self.server = serve(self.repository, port=0, connections_store=self.connections_store)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _get(self, path: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", path)
        return connection, connection.getresponse()

    def _post(
        self, path: str, payload: object
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        body = json.dumps(payload).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        return connection, connection.getresponse()

    def test_list_is_empty_before_any_connection_is_created(self) -> None:
        connection, response = self._get("/v1/connections")
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(body, {"connections": []})

    def test_create_persists_a_connection_and_never_echoes_a_secret(self) -> None:
        connection, response = self._post("/v1/connections", _connection_payload())
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 201)
        record = body["connection"]
        self.assertEqual(record["site_id"], SITE.site_id)
        self.assertEqual(record["display_name"], "Site Acme (test)")
        self.assertEqual(record["ibmi_host"], "ibmi.acme.invalid")
        self.assertEqual(record["destination_database"], SITE.destination_database)
        self.assertEqual(record["destination_schema"], SITE.destination_schema)
        self.assertEqual(record["tables"], ["SALE"])
        self.assertEqual(record["secret_ref_name"], "acme-test-ibmi")
        self.assertEqual(record["secret_ref_key"], "ISERIES_PASSWORD")
        self.assertEqual(record["lifecycle_state"], LIFECYCLE_DECLARED_NOT_IN_SERVICE)
        self.assertNotIn("password", json.dumps(body))

        connection, listed = self._get("/v1/connections")
        listed_body = json.loads(listed.read())
        connection.close()
        self.assertEqual(listed.status, 200)
        self.assertEqual([item["connection_id"] for item in listed_body["connections"]], [record["connection_id"]])

    def test_un_stockage_indisponible_repond_503_sans_detail_interne(self) -> None:
        from unittest.mock import patch

        with patch.object(AtomicJsonStateStore, "save", side_effect=OSError("private-path")):
            connection, response = self._post("/v1/connections", _connection_payload())
            body = response.read().decode("utf-8")
            connection.close()
        self.assertEqual(response.status, 503)
        self.assertIn("connections_store_unavailable", body)
        self.assertNotIn("private-path", body)
        self.assertEqual(self.connections_store.list(), ())

    def test_create_rejects_a_password_field_with_the_onboarding_verdict(self) -> None:
        connection, response = self._post(
            "/v1/connections", _connection_payload(ibmi_password="hunter2")
        )
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertEqual(body["status"], "error")
        self.assertIn("secret", " ".join(body["blocked"]).lower())
        self.assertNotIn("hunter2", json.dumps(body))

    def test_create_rejects_a_missing_required_field_with_the_onboarding_verdict(self) -> None:
        payload = _connection_payload()
        del payload["ibmi_host"]
        connection, response = self._post("/v1/connections", payload)
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertIn("step", body)
        self.assertIn("L'hôte IBM i est obligatoire", body["errors"])

    def test_create_rejects_a_destination_outside_the_site_perimeter(self) -> None:
        connection, response = self._post(
            "/v1/connections", _connection_payload(snowflake_schema="SOMEWHERE_ELSE")
        )
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 400)
        self.assertTrue(body["blocked"])

    def test_connections_routes_are_not_found_without_a_wired_store(self) -> None:
        server = serve(self.repository, port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("GET", "/v1/connections")
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
