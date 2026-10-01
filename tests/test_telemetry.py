"""Télémétrie : opt-in strict, payload minimal, envoi borné et silencieux."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from quadringent_control_plane import telemetry


def _config(enabled: bool = True, **kwargs) -> telemetry.TelemetryConfig:
    state_dir = kwargs.pop("state_dir", Path(tempfile.mkdtemp()))
    return telemetry.TelemetryConfig(enabled=enabled, state_dir=state_dir, **kwargs)


class TelemetryConfigTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        config = telemetry.TelemetryConfig.from_env({})
        self.assertFalse(config.enabled)

    def test_enabled_only_on_explicit_value(self) -> None:
        for value in ("1", "true", "on", "yes", "ON"):
            self.assertTrue(telemetry.TelemetryConfig.from_env(
                {"QUADRINGENT_TELEMETRY": value}).enabled)
        for value in ("0", "false", "off", "", "enabled"):
            self.assertFalse(telemetry.TelemetryConfig.from_env(
                {"QUADRINGENT_TELEMETRY": value}).enabled)

    def test_non_https_endpoint_falls_back(self) -> None:
        config = telemetry.TelemetryConfig.from_env(
            {"QUADRINGENT_TELEMETRY_URL": "http://telemetry.local/ping"})
        self.assertTrue(config.endpoint.startswith("https://"))

    def test_https_endpoint_is_kept(self) -> None:
        config = telemetry.TelemetryConfig.from_env(
            {"QUADRINGENT_TELEMETRY_URL": "https://collector.example/v1"})
        self.assertEqual(config.endpoint, "https://collector.example/v1")


class PayloadTests(unittest.TestCase):
    def test_payload_is_minimal_and_anonymous(self) -> None:
        config = _config()
        payload = telemetry.build_payload(
            config, version="0.2.0", table_count=13, now=1234.0)
        self.assertEqual(set(payload), {"v", "install", "version", "tier", "tables", "ts"})
        self.assertEqual(payload["tier"], "community")
        self.assertEqual(payload["tables"], 13)
        self.assertEqual(payload["ts"], 1234)
        self.assertNotIn("host", json.dumps(payload).lower())
        self.assertEqual(len(payload["install"]), 64)  # sha256 hex

    def test_install_id_is_stable_and_stored_0600(self) -> None:
        state_dir = Path(tempfile.mkdtemp())
        config = _config(state_dir=state_dir)
        first = telemetry.build_payload(config, version="x", table_count=1)
        second = telemetry.build_payload(config, version="x", table_count=1)
        self.assertEqual(first["install"], second["install"])
        stored = state_dir / "telemetry-install-id"
        self.assertTrue(stored.exists())
        self.assertEqual(oct(stored.stat().st_mode & 0o777), "0o600")
        # L'identifiant stocké ne part jamais en clair : le payload porte son hachage.
        self.assertNotEqual(first["install"], stored.read_text().strip())


class SendTests(unittest.TestCase):
    def test_send_posts_payload_to_endpoint(self) -> None:
        config = _config(endpoint="https://telemetry.example/v1/ping")
        seen = {}

        class _Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            seen["body"] = json.loads(req.data.decode())
            return _Response()

        with mock.patch.object(telemetry._request, "urlopen", fake_urlopen):
            self.assertTrue(telemetry.send_once(config, {"v": 1, "install": "x"}))
        self.assertEqual(seen["url"], "https://telemetry.example/v1/ping")
        self.assertEqual(seen["timeout"], 5.0)
        self.assertEqual(seen["body"]["install"], "x")

    def test_send_failure_is_silent(self) -> None:
        config = _config()
        with mock.patch.object(telemetry._request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(telemetry.send_once(config, {"v": 1}))


class DaemonTests(unittest.TestCase):
    def test_maybe_start_without_optin_starts_nothing(self) -> None:
        self.assertIsNone(telemetry.maybe_start(
            _config(enabled=False), version="x", table_count=1))

    def test_maybe_start_with_optin_runs_daemon(self) -> None:
        daemon = telemetry.maybe_start(
            _config(enabled=True), version="x", table_count=1)
        self.assertIsNotNone(daemon)
        self.assertTrue(daemon.daemon)
        daemon.stop()
        daemon.join(timeout=2)
        self.assertFalse(daemon.is_alive())


if __name__ == "__main__":
    unittest.main()
