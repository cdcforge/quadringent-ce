"""Relais v1 -> v2 (``/v2/*`` et ``/mcp``) : statut, en-têtes, corps, SSE, 502.

Le control plane v1 (serveur stdlib, port 8844) et le control plane v2
(FastAPI/uvicorn, port 8845) partagent le même Pod mais écoutent tous deux
en loopback : l'opérateur n'ouvre qu'un tunnel vers le port v1. Sans relais,
l'UI (basePath ``/v2``) et le lien d'activation ne fonctionnent pas à
travers ce tunnel unique — voir ``docs/api-v2.md``.

Ce module couvre le relais lui-même avec un amont v2 fictif ; l'intégration
avec la vraie application v2 (FastAPI réelle) est couverte par
``tests/test_control_plane_v2_proxy_integration.py``.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import socket
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Event, Thread
import unittest

import site_fixture
from quadringent_control_plane.auth import AuthConfig
from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
from quadringent_control_plane.server import MAX_PROXY_BODY_BYTES, serve


SITE = site_fixture.build_test_site()


def document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {"checkpoint": {"receiver": "R", "sequence": 1}, "source_tail": {"receiver": "R", "sequence": 2}},
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 1}},
    }


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


class _RecordingUpstreamHandler(BaseHTTPRequestHandler):
    """Amont v2 fictif : rejoue un corps/en-têtes connus, journalise la requête."""

    protocol_version = "HTTP/1.1"
    received: list[dict[str, object]] = []

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        type(self).received.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": raw,
            }
        )
        if self.path == "/v2/echo":
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Mcp-Session-Id", "session-abc")
            self.send_header("Set-Cookie", "quadringent_session=xyz; HttpOnly; Path=/")
            self.send_header("Set-Cookie", "other=1; Path=/")
            self.send_header("Connection", "keep-alive")
            body = json.dumps({"echo": raw.decode("utf-8") if raw else None}).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/mcp":
            # Comportement réel de Starlette : redirection absolue vers l'amont.
            self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/mcp/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class ControlPlaneV2ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "console.json"
        path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{path}")])
        self.repository.refresh()

        _RecordingUpstreamHandler.received = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingUpstreamHandler)
        self.upstream_thread = Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()

        self.server = serve(self.repository, port=0, v2_upstream_port=self.upstream.server_port)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=2)
        self.directory.cleanup()

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)

    def test_post_is_relayed_with_status_body_and_selected_headers(self) -> None:
        connection = self._connection()
        connection.request(
            "POST",
            "/v2/echo",
            body=b'{"hello":"world"}',
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "abc-1",
                "Authorization": "Bearer token-1",
                "Cookie": "quadringent_session=in",
                "X-Not-Whitelisted": "should-not-cross",
            },
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()

        self.assertEqual(response.status, HTTPStatus.CREATED)
        self.assertEqual(json.loads(body), {"echo": '{"hello":"world"}'})
        self.assertEqual(response.getheader("Mcp-Session-Id"), "session-abc")
        cookies = response.msg.get_all("Set-Cookie") or []
        self.assertIn("quadringent_session=xyz; HttpOnly; Path=/", cookies)
        self.assertIn("other=1; Path=/", cookies)
        # Hop-by-hop : jamais relayé, même si l'amont applicatif le pose.
        self.assertIsNone(response.getheader("Keep-Alive"))

        [request] = _RecordingUpstreamHandler.received
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["headers"].get("Idempotency-Key"), "abc-1")
        self.assertEqual(request["headers"].get("Authorization"), "Bearer token-1")
        self.assertEqual(request["headers"].get("Cookie"), "quadringent_session=in")
        self.assertNotIn("X-Not-Whitelisted", request["headers"])
        # L'amont ne doit jamais recevoir le Host forgé par le client v1.
        self.assertNotIn(f"127.0.0.1:{self.server.server_port}", request["headers"].get("Host", ""))

    def test_get_mcp_path_is_relayed_too(self) -> None:
        connection = self._connection()
        connection.request("GET", "/mcp/tools")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, HTTPStatus.NOT_FOUND)
        [request] = _RecordingUpstreamHandler.received
        self.assertEqual(request["path"], "/mcp/tools")

    def test_upstream_redirect_never_leaks_the_internal_port(self) -> None:
        """Constaté sur GKE : ``POST /mcp`` → 307 vers ``http://127.0.0.1:8845/mcp/``,
        injoignable derrière le tunnel. Le relais rend la redirection relative."""
        connection = self._connection()
        connection.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, HTTPStatus.TEMPORARY_REDIRECT)
        self.assertEqual(response.getheader("Location"), "/mcp/")

    def test_v1_auth_header_gate_is_bypassed_for_v2_paths_but_still_applies_to_v1_routes(self) -> None:
        # v2 porte sa propre authentification (jetons, sessions) : le garde
        # v1 par en-têtes de proxy ne doit jamais s'appliquer à /v2 ou
        # /mcp. On le prouve en activant l'auth v1 et en montrant que /v2
        # passe sans identité alors que /v1/overview la refuse toujours.
        auth = AuthConfig.build(
            user_header="x-auth-user",
            groups_header="x-auth-groups",
            operator_groups="operators",
        )
        server = serve(
            self.repository,
            port=0,
            auth=auth,
            v2_upstream_port=self.upstream.server_port,
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("DELETE", "/v2/echo")
            proxied = connection.getresponse()
            proxied.read()
            connection.close()
            self.assertEqual(proxied.status, HTTPStatus.CREATED)

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/v1/overview")
            guarded = connection.getresponse()
            guarded.read()
            connection.close()
            self.assertEqual(guarded.status, HTTPStatus.UNAUTHORIZED)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_body_over_bound_is_refused_without_reaching_upstream(self) -> None:
        connection = self._connection()
        # Le proxy tranche sur Content-Length avant de lire le corps et ferme
        # alors la socket. Envoyer tout le corps introduit une course où le
        # client reçoit un reset pendant son propre sendall, sans tester 413.
        connection.putrequest("POST", "/v2/echo")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_PROXY_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(body["error"]["code"], "payload_too_large")
        self.assertEqual(_RecordingUpstreamHandler.received, [])

    def test_unreachable_upstream_returns_bare_502(self) -> None:
        closed_port = _free_port()
        server = serve(self.repository, port=0, v2_upstream_port=closed_port)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/v2/echo")
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, HTTPStatus.BAD_GATEWAY)
            self.assertEqual(body, {"error": {"code": "upstream_unavailable"}})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_without_upstream_port_behaviour_is_unchanged(self) -> None:
        server = serve(self.repository, port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/v2/anything")
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, HTTPStatus.NOT_FOUND)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class _ChunkedSSEUpstream:
    """Amont v2 fictif minimal : un seul flux SSE chunké, écrit en deux temps."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self.port = self._socket.getsockname()[1]
        self.first_sent = Event()
        self.release_second = Event()
        self._thread = Thread(target=self._serve_once, daemon=True)
        self._thread.start()

    def _serve_once(self) -> None:
        connection, _ = self._socket.accept()
        try:
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream; charset=utf-8\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
            )
            self._write_chunk(connection, b"data: first\n\n")
            self.first_sent.set()
            self.release_second.wait(5)
            self._write_chunk(connection, b"data: second\n\n")
            connection.sendall(b"0\r\n\r\n")
        finally:
            connection.close()

    @staticmethod
    def _write_chunk(connection: socket.socket, data: bytes) -> None:
        connection.sendall(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")

    def close(self) -> None:
        self._socket.close()
        self._thread.join(timeout=2)


class ControlPlaneV2ProxySSETests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        path = Path(self.directory.name) / "console.json"
        path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{path}")])
        self.repository.refresh()

        self.upstream = _ChunkedSSEUpstream()
        self.server = serve(self.repository, port=0, v2_upstream_port=self.upstream.port)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.upstream.close()
        self.directory.cleanup()

    def test_sse_first_event_is_flushed_before_the_second_is_even_sent(self) -> None:
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        try:
            client.sendall(
                b"GET /v2/events HTTP/1.1\r\n"
                b"Host: ignored\r\n"
                b"Accept: text/event-stream\r\n"
                b"\r\n"
            )
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                part = client.recv(4096)
                self.assertTrue(part, "connexion fermée avant la fin des en-têtes")
                buffer += part
            head, _, rest = buffer.partition(b"\r\n\r\n")
            head_text = head.decode("latin-1")
            self.assertIn("200", head_text.splitlines()[0])
            self.assertIn("text/event-stream", head_text)
            # Ni Content-Length ni Transfer-Encoding : la fin est signalée
            # par la fermeture de connexion (voir _proxy_to_v2).
            self.assertNotIn("Content-Length", head_text)
            self.assertNotIn("Transfer-Encoding", head_text)

            while b"data: first\n\n" not in rest:
                part = client.recv(4096)
                self.assertTrue(part, "premier évènement jamais reçu")
                rest += part

            # Le premier évènement est arrivé alors que l'amont n'a
            # volontairement pas encore envoyé le second : preuve que le
            # relais ne bufferise pas la réponse entière avant de la vider.
            self.assertFalse(self.upstream.release_second.is_set())
            self.upstream.release_second.set()

            while b"data: second\n\n" not in rest:
                part = client.recv(4096)
                if not part:
                    break
                rest += part
            self.assertIn(b"data: second\n\n", rest)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
