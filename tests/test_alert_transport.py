from __future__ import annotations

import site_fixture

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from quadringent.alert_transport import (
    MAX_EVENTS,
    assess_observability_freshness,
    deadman_payload,
    deliver,
    notification_payload,
)
from quadringent.slo import evaluate_slo
from quadringent.slo_alerts import reconcile_alerts
from test_slo import NOW, _policy, _proof, _telemetry


SITE = site_fixture.build_test_site()

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "quadringent_slo_alerts.py"


def _report(*, status: str = "breach", seconds: int = 0) -> dict[str, object]:
    telemetry = _telemetry()
    telemetry["collected_at"] = (NOW + timedelta(seconds=seconds)).isoformat()
    if status == "breach":
        telemetry["snowpipe_pending_files"] = 11
    elif status == "pass":
        pass
    else:
        raise AssertionError(f"unsupported test status: {status}")
    return evaluate_slo(_proof(), telemetry, _policy(), now=NOW + timedelta(seconds=seconds), site=SITE)


def _batch(*, status: str = "breach") -> dict[str, object]:
    return reconcile_alerts(_report(status=status), site=SITE)


class _Recorder(BaseHTTPRequestHandler):
    posts: list[tuple[str, bytes]] = []
    status = 200

    def do_POST(self) -> None:  # noqa: N802 - nommage BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", "0"))
        _Recorder.posts.append((self.path, self.rfile.read(length)))
        self.send_response(_Recorder.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def _serve() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/hook"


class NotificationPayloadTests(unittest.TestCase):
    def test_payload_is_bounded_projection_without_extra_fields(self) -> None:
        batch = _batch()
        payload = notification_payload(batch, site=SITE)

        self.assertEqual(payload["schema_version"], "quadringent-alert-notification-v1")
        self.assertEqual(payload["environment"], SITE.environment)
        self.assertEqual(payload["pipeline_id"], SITE.pipeline_id)
        self.assertEqual(payload["source_report_digest"], batch["source_report"]["digest"])
        self.assertEqual(payload["state_digest"], batch["state"]["state_digest"])
        self.assertEqual(payload["event_count"], 1)
        event = payload["events"][0]
        self.assertEqual(event["check_id"], "snowpipe_queue")
        self.assertEqual(event["transition"], "opened")
        self.assertTrue(event["event_id"].startswith("sha256:"))
        self.assertEqual(payload["active_alert_count"], 1)
        encoded = json.dumps(payload)
        for leaked in ("ISERIES", "password", "secret"):
            self.assertNotIn(leaked, encoded)

    def test_events_are_truncated_and_flagged_never_dropped_silently(self) -> None:
        batch = _batch()
        batch["events"] = batch["events"] * (MAX_EVENTS + 5)
        payload = notification_payload(batch, site=SITE)

        self.assertEqual(len(payload["events"]), MAX_EVENTS)
        self.assertTrue(payload["events_truncated"])
        self.assertEqual(payload["event_count"], MAX_EVENTS + 5)

    def test_wrong_schema_or_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema"):
            notification_payload({"schema_version": "other"}, site=SITE)
        batch = _batch()
        batch["environment"] = "prod"
        with self.assertRaisesRegex(ValueError, "declared pipeline"):
            notification_payload(batch, site=SITE)


class DeliveryTests(unittest.TestCase):
    def test_webhook_post_delivers_json_body(self) -> None:
        _Recorder.posts = []
        _Recorder.status = 200
        server, url = _serve()
        try:
            results = deliver(notification_payload(_batch(), site=SITE), webhook_url=url, site=SITE)
        finally:
            server.shutdown()
        self.assertEqual(results, [{"destination": "webhook", "status": "delivered"}])
        path, body = _Recorder.posts[0]
        self.assertEqual(path, "/hook")
        self.assertEqual(json.loads(body)["schema_version"], "quadringent-alert-notification-v1")

    def test_delivery_failures_are_reported_never_raised(self) -> None:
        _Recorder.posts = []
        _Recorder.status = 500
        server, url = _serve()
        try:
            results = deliver(notification_payload(_batch(), site=SITE), webhook_url=url, site=SITE)
        finally:
            server.shutdown()
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["destination"], "webhook")
        self.assertIn("error_type", results[0])

        closed = deliver(
            notification_payload(_batch(), site=SITE),
            webhook_url="http://127.0.0.1:1/unreachable",
            timeout_seconds=0.5,
            site=SITE,
        )
        self.assertEqual(closed[0]["status"], "failed")

    def test_sns_publish_uses_injected_client(self) -> None:
        class FakeSns:
            def __init__(self) -> None:
                self.calls = []

            def publish(self, **kwargs: object) -> None:
                self.calls.append(kwargs)

        client = FakeSns()
        results = deliver(
            notification_payload(_batch(), site=SITE),
            sns_topic=f"arn:aws:sns:{SITE.aws_region}:{SITE.aws_account_id}:quadringent-{SITE.environment}-alerts",
            sns_client=client,
            site=SITE,
        )
        self.assertEqual(results, [{"destination": "sns", "status": "delivered"}])
        self.assertEqual(len(client.calls), 1)
        message = json.loads(client.calls[0]["Message"])
        self.assertEqual(message["schema_version"], "quadringent-alert-notification-v1")

    def test_malformed_destinations_fail_closed_before_network(self) -> None:
        results = deliver(notification_payload(_batch(), site=SITE), webhook_url="ftp://example", site=SITE)
        self.assertEqual(results[0]["status"], "failed")
        self.assertEqual(results[0]["error_type"], "ValueError")
        results = deliver(
            notification_payload(_batch(), site=SITE), sns_topic="not-an-arn", sns_client=object(), site=SITE
        )
        self.assertEqual(results[0]["status"], "failed")


class DeadmanTests(unittest.TestCase):
    def _snapshot(self, *, updated_at: str) -> dict[str, object]:
        return {
            "format_version": "as400-console-v1",
            "observability": {
                "schema_version": "quadringent-observability-v1",
                "slo_report": {"observed_at": updated_at},
                "alert_state": {"updated_at": updated_at},
            },
        }

    def test_fresh_snapshot_passes(self) -> None:
        now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
        assessment = assess_observability_freshness(
            self._snapshot(updated_at="2026-09-17T11:30:00+00:00"),
            now=now,
            max_age_seconds=3600,
        )
        self.assertEqual(assessment["status"], "fresh")
        self.assertEqual(assessment["age_seconds"], 1800)

    def test_stale_and_missing_snapshots_are_distinct(self) -> None:
        now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
        stale = assess_observability_freshness(
            self._snapshot(updated_at="2026-09-17T10:00:00+00:00"),
            now=now,
            max_age_seconds=3600,
        )
        self.assertEqual(stale["status"], "stale")
        missing = assess_observability_freshness(None, now=now, max_age_seconds=3600)
        self.assertEqual(missing["status"], "missing")
        self.assertIsNone(missing["observed_at"])
        missing_block = assess_observability_freshness(
            {"format_version": "as400-console-v1"}, now=now, max_age_seconds=3600
        )
        self.assertEqual(missing_block["status"], "missing")

    def test_deadman_payload_is_stable_and_bounded(self) -> None:
        now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
        assessment = assess_observability_freshness(
            None, now=now, max_age_seconds=3600
        )
        payload = deadman_payload(assessment, detected_at=now.isoformat(), site=SITE)
        self.assertEqual(payload["check_id"], "observability_freshness")
        self.assertEqual(payload["status"], "missing")
        self.assertEqual(payload["severity"], "warning")
        first = deadman_payload(assessment, detected_at=now.isoformat(), site=SITE)
        second = deadman_payload(assessment, detected_at=now.isoformat(), site=SITE)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        with self.assertRaises(ValueError):
            deadman_payload({"status": "fresh"}, detected_at=now.isoformat(), site=SITE)


class CliTransportTests(unittest.TestCase):
    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        import os

        merged = dict(os.environ)
        merged["PYTHONPATH"] = f"{ROOT / 'src'}:{ROOT / 'scripts'}:{ROOT / 'tests'}"
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            env=merged,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_transitions_are_posted_and_delivery_failure_stays_fail_open(self) -> None:
        _Recorder.posts = []
        _Recorder.status = 200
        server, url = _serve()
        try:
            with tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "report.json"
                state_path = Path(directory) / "alerts.json"
                report_path.write_text(json.dumps(_report()), encoding="utf-8")
                completed = self._run(
                    "--report", str(report_path), "--state", str(state_path),
                    env={"QUADRINGENT_ALERT_WEBHOOK_URL": url},
                )
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(len(_Recorder.posts), 1)
            posted = json.loads(_Recorder.posts[0][1])
            self.assertEqual(posted["events"][0]["transition"], "opened")
        finally:
            server.shutdown()

        # Destination en échec : l'état persiste, le code de sortie est inchangé.
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            state_path = Path(directory) / "alerts.json"
            report_path.write_text(json.dumps(_report()), encoding="utf-8")
            completed = self._run(
                "--report", str(report_path), "--state", str(state_path),
                env={"QUADRINGENT_ALERT_WEBHOOK_URL": "http://127.0.0.1:1/down"},
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("alert_delivery_failed", completed.stderr)
            self.assertTrue(json.loads(state_path.read_text())["events"])

    def test_deadman_flags_stale_snapshot_and_notifies(self) -> None:
        _Recorder.posts = []
        _Recorder.status = 200
        server, url = _serve()
        try:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                report_path = base / "report.json"
                state_path = base / "alerts.json"
                snapshot_path = base / "snapshot.json"
                report_path.write_text(json.dumps(_report(status="pass")), encoding="utf-8")
                snapshot_path.write_text(
                    json.dumps(
                        {
                            "observability": {
                                "alert_state": {"updated_at": "2020-01-01T00:00:00+00:00"},
                                "slo_report": {"observed_at": "2020-01-01T00:00:00+00:00"},
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                completed = self._run(
                    "--report", str(report_path), "--state", str(state_path),
                    "--observability-snapshot", str(snapshot_path),
                    env={"QUADRINGENT_ALERT_WEBHOOK_URL": url},
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                summary = json.loads(completed.stdout)
                self.assertEqual(summary["deadman"]["status"], "stale")
                posted = json.loads(_Recorder.posts[0][1])
                self.assertEqual(posted["schema_version"], "quadringent-deadman-v1")
                self.assertEqual(posted["check_id"], "observability_freshness")
        finally:
            server.shutdown()

    def test_deadman_disabled_without_snapshot_and_missing_file_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            report_path = base / "report.json"
            state_path = base / "alerts.json"
            report_path.write_text(json.dumps(_report(status="pass")), encoding="utf-8")
            completed = self._run("--report", str(report_path), "--state", str(state_path))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["deadman"]["status"], "disabled")

            completed = self._run(
                "--report", str(report_path), "--state", str(state_path),
                "--observability-snapshot", str(base / "absent.json"),
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(json.loads(completed.stdout)["deadman"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
