"""Authentification déléguée au proxy : en-têtes de confiance, rôles, refus."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest

import pytest

import site_fixture
from quadringent_control_plane.auth import (
    AuthConfig,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    ROLE_ADMIN,
    authenticate,
    authorize,
    required_role,
)
from quadringent_control_plane.repository import ProjectionRepository, parse_source_spec
from quadringent_control_plane.server import serve


SITE = site_fixture.build_test_site()
NOW = datetime.now(timezone.utc)

CONFIG = AuthConfig.build(
    user_header="X-Forwarded-User",
    groups_header="X-Forwarded-Groups",
    operator_groups="quadringent-operators",
    admin_groups="quadringent-admins",
)


def document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(seconds=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {"checkpoint": {"receiver": "R", "sequence": 1}, "source_tail": {"receiver": "R", "sequence": 2}},
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 1}},
    }


class TestAuthConfig:
    def test_disabled_without_user_header(self) -> None:
        assert AuthConfig.build() is None
        assert AuthConfig.build(operator_groups="ops") is None

    def test_requires_at_least_one_group(self) -> None:
        with pytest.raises(ValueError, match="operator-groups|admin-groups"):
            AuthConfig.build(user_header="X-User", groups_header="X-Groups")

    @pytest.mark.parametrize("header", ["bad header", "x_y", "é"])
    def test_rejects_invalid_header_names(self, header: str) -> None:
        with pytest.raises(ValueError, match="en-tête"):
            AuthConfig.build(user_header=header, groups_header="X-G", operator_groups="ops")


class TestAuthenticate:
    def test_missing_user_header_is_anonymous(self) -> None:
        assert authenticate({}, CONFIG) is None
        assert authenticate({"x-forwarded-user": "  "}, CONFIG) is None

    def test_control_characters_rejected(self) -> None:
        assert authenticate({"X-Forwarded-User": "a\nb"}, CONFIG) is None

    def test_user_without_groups_is_viewer(self) -> None:
        identity = authenticate({"X-Forwarded-User": "lea@site.fr"}, CONFIG)
        assert identity is not None
        assert identity.subject == "lea@site.fr"
        assert identity.role == ROLE_VIEWER

    def test_operator_group(self) -> None:
        identity = authenticate(
            {"X-Forwarded-User": "sam", "X-Forwarded-Groups": "data, quadringent-operators"},
            CONFIG,
        )
        assert identity is not None and identity.role == ROLE_OPERATOR

    def test_admin_group_wins(self) -> None:
        identity = authenticate(
            {"X-Forwarded-User": "root", "X-Forwarded-Groups": "quadringent-operators,quadringent-admins"},
            CONFIG,
        )
        assert identity is not None and identity.role == ROLE_ADMIN

    def test_header_lookup_is_case_insensitive(self) -> None:
        identity = authenticate({"x-forwarded-user": "sam", "X-FORWARDED-GROUPS": "quadringent-operators"}, CONFIG)
        assert identity is not None and identity.role == ROLE_OPERATOR


class TestAuthorize:
    def test_reads_are_viewer_level(self) -> None:
        assert required_role("GET") == ROLE_VIEWER
        assert required_role("HEAD") == ROLE_VIEWER

    def test_writes_are_operator_level(self) -> None:
        assert required_role("POST") == ROLE_OPERATOR

    def test_viewer_cannot_write(self) -> None:
        identity = authenticate({"X-Forwarded-User": "lea"}, CONFIG)
        assert identity is not None
        assert authorize(identity, "GET") is None
        status, code = authorize(identity, "POST")
        assert status.value == 403
        assert code == "insufficient_role"

    def test_operator_can_write(self) -> None:
        identity = authenticate(
            {"X-Forwarded-User": "sam", "X-Forwarded-Groups": "quadringent-operators"}, CONFIG
        )
        assert identity is not None
        assert authorize(identity, "POST") is None


class AuthenticatedServerTests(unittest.TestCase):
    """Le serveur refuse sans identité et borne les écritures aux opérateurs."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{self.path}")])
        self.repository.refresh()
        self.server = serve(self.repository, port=0, auth=CONFIG)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _request(self, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b""):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(method, path, body=body or None, headers=headers or {})
        return connection, connection.getresponse()

    def test_get_without_identity_is_unauthorized(self) -> None:
        connection, response = self._request("GET", "/v1/overview")
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 401)
        self.assertEqual(body["error"]["code"], "authentication_required")

    def test_get_with_identity_succeeds(self) -> None:
        connection, response = self._request("GET", "/v1/overview", {"X-Forwarded-User": "lea@site.fr"})
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("pipelines", body)

    def test_post_without_identity_is_unauthorized(self) -> None:
        connection, response = self._request("POST", "/v1/onboarding/evaluate")
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)

    def test_post_viewer_is_forbidden(self) -> None:
        connection, response = self._request(
            "POST",
            "/v1/onboarding/evaluate",
            {"X-Forwarded-User": "lea", "Content-Type": "application/json"},
            b"{}",
        )
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(body["error"]["code"], "insufficient_role")

    def test_post_operator_reaches_the_route(self) -> None:
        connection, response = self._request(
            "POST",
            "/v1/onboarding/evaluate",
            {"X-Forwarded-User": "sam", "X-Forwarded-Groups": "quadringent-operators", "Content-Type": "application/json"},
            b"{}",
        )
        response.read()
        connection.close()
        # La route répond — le refus d'auth n'est plus en cause (400/415/422 métier possibles, jamais 401/403).
        self.assertNotIn(response.status, (401, 403))

    def test_healthz_is_exempt_from_authentication(self) -> None:
        connection, response = self._request("GET", "/healthz")
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_rejected_methods_still_require_identity(self) -> None:
        # L'auth s'évalue avant la méthode : aucun sondage anonyme du surface.
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            connection, response = self._request(method, "/v1/overview")
            response.read()
            connection.close()
            self.assertEqual(response.status, 401, method)
        # Méthodes classe-écriture : un viewer authentifié reste insuffisant.
        for method in ("PUT", "DELETE", "PATCH"):
            connection, response = self._request(
                method, "/v1/overview", {"X-Forwarded-User": "lea@site.fr"}
            )
            response.read()
            connection.close()
            self.assertEqual(response.status, 403, method)
        # Un opérateur atteint le refus de méthode, pas celui de rôle.
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            connection, response = self._request(
                method,
                "/v1/overview",
                {"X-Forwarded-User": "sam", "X-Forwarded-Groups": "quadringent-operators"},
            )
            response.read()
            connection.close()
            self.assertEqual(response.status, 405, method)
        connection, response = self._request("OPTIONS", "/v1/overview", {"X-Forwarded-User": "lea"})
        response.read()
        connection.close()
        self.assertEqual(response.status, 405)


class ProxySecretServerTests(unittest.TestCase):
    """Le secret partagé prouve le transit par le proxy — sans lui, aucun rôle."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "console.json"
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        self.repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{self.path}")])
        self.repository.refresh()
        config = AuthConfig.build(
            user_header="X-Forwarded-User",
            groups_header="X-Forwarded-Groups",
            operator_groups="quadringent-operators",
            proxy_secret="shared-site-secret",
        )
        self.server = serve(self.repository, port=0, auth=config)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def _request(self, path: str, headers: dict[str, str]):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", path, headers=headers)
        return connection, connection.getresponse()

    def test_identity_without_proxy_secret_is_rejected(self) -> None:
        connection, response = self._request("/v1/overview", {"X-Forwarded-User": "lea@site.fr"})
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)

    def test_wrong_proxy_secret_is_rejected(self) -> None:
        connection, response = self._request(
            "/v1/overview",
            {"X-Forwarded-User": "lea@site.fr", "X-Quadringent-Proxy-Secret": "forged"},
        )
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)

    def test_valid_proxy_secret_authenticates(self) -> None:
        connection, response = self._request(
            "/v1/overview",
            {"X-Forwarded-User": "lea@site.fr", "X-Quadringent-Proxy-Secret": "shared-site-secret"},
        )
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("pipelines", body)

    def test_duplicated_identity_header_is_rejected(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.putrequest("GET", "/v1/overview")
        connection.putheader("X-Forwarded-User", "lea@site.fr")
        connection.putheader("X-Forwarded-User", "mallory@site.fr")
        connection.putheader("X-Quadringent-Proxy-Secret", "shared-site-secret")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)


class LoopbackGuardWithAuthTests(unittest.TestCase):
    def test_non_loopback_bind_allowed_only_with_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.json"
            path.write_text(json.dumps(document()), encoding="utf-8")
            repository = ProjectionRepository([parse_source_spec(f"simulation:demo:file://{path}")])
            with self.assertRaises(ValueError):
                serve(repository, host="0.0.0.0", port=0)
            server = serve(repository, host="0.0.0.0", port=0, auth=CONFIG)
            server.server_close()
