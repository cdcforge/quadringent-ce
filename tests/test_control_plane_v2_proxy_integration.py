"""Intégration réelle du relais v1 -> v2 : vraie application v2 sous uvicorn.

Contrairement à ``tests/test_control_plane_v2_proxy.py`` (amont fictif),
ce module démarre la véritable application FastAPI v2 (SQLite + migrations,
même fixture que ``tests/test_v2_users_routes.py``) sur un port loopback
éphémère via uvicorn, câble le relais v1 dessus, puis exerce le parcours
d'installation minimal à travers le seul port v1 : premier admin puis
sonde de santé v2 — exactement ce qu'un opérateur ferait derrière un unique
tunnel ``kubectl port-forward ... 8844:8844``.
"""

from __future__ import annotations

from http import HTTPStatus
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

import uvicorn

import site_fixture
from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
from quadringent_control_plane.server import serve
from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.app import create_v2_app
from quadringent_control_plane.v2.crypto import SecretBox


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


class ControlPlaneV2ProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        directory = Path(self.directory.name)

        console_path = directory / "console.json"
        console_path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository(
            [parse_source_spec(f"simulation:demo:file://{console_path}")]
        )
        self.repository.refresh()

        dsn = f"sqlite:///{directory / 'v2-proxy-integration.sqlite3'}"
        v2_db.run_migrations(dsn)
        self.engine = v2_db.create_engine_for(dsn)
        with self.engine.begin() as connection:
            connection.execute(
                v2_schema.organizations.insert(), {"id": "default", "name": "Client unique"}
            )
        app = create_v2_app(
            engine=self.engine,
            secret_box=SecretBox(SecretBox.generate_key()),
            token_pepper=b"pepper-integration-test",
            session_cookie_secure=False,
        )
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.v2_server = uvicorn.Server(config)
        self.v2_thread = Thread(target=self.v2_server.run, daemon=True)
        self.v2_thread.start()
        self._wait_until(lambda: self.v2_server.started)
        v2_port = self.v2_server.servers[0].sockets[0].getsockname()[1]

        self.v1_server = serve(self.repository, port=0, v2_upstream_port=v2_port)
        self.v1_thread = Thread(target=self.v1_server.serve_forever, daemon=True)
        self.v1_thread.start()

    def tearDown(self) -> None:
        self.v1_server.shutdown()
        self.v1_server.server_close()
        self.v1_thread.join(timeout=2)
        self.v2_server.should_exit = True
        self.v2_thread.join(timeout=5)
        self.engine.dispose()
        self.directory.cleanup()

    @staticmethod
    def _wait_until(predicate, timeout: float = 5.0) -> None:
        from time import monotonic, sleep

        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if predicate():
                return
            sleep(0.02)
        raise AssertionError("condition jamais vraie avant l'échéance")

    def _connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.v1_server.server_port, timeout=10)

    def test_first_admin_setup_then_v2_healthz_through_the_v1_port_only(self) -> None:
        connection = self._connection()
        connection.request(
            "POST",
            "/v2/setup/first-admin",
            body=json.dumps({"email": "admin@example.com"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Idempotency-Key": "proxy-setup-1"},
        )
        created = connection.getresponse()
        created_body = json.loads(created.read())
        connection.close()

        self.assertEqual(created.status, HTTPStatus.CREATED)
        self.assertEqual(created_body["after"]["email"], "admin@example.com")
        self.assertTrue(created_body["after"]["activation_token"])

        connection = self._connection()
        connection.request("GET", "/v2/healthz")
        healthy = connection.getresponse()
        healthy_body = json.loads(healthy.read())
        connection.close()

        self.assertEqual(healthy.status, HTTPStatus.OK)
        self.assertEqual(healthy_body.get("status"), "ok")


if __name__ == "__main__":
    unittest.main()
