from __future__ import annotations

import site_fixture

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from quadringent.observability_snapshot import attach_observability_snapshot
from quadringent.slo import evaluate_slo
from quadringent.slo_alerts import reconcile_alerts
from quadringent_control_plane.model import ProjectionError, SourceDescriptor
from quadringent_control_plane.projection import project_console_document
from test_slo import NOW, _policy, _proof, _telemetry


SITE = site_fixture.build_test_site()

LIVE_SOURCE = SourceDescriptor(
    f"{SITE.environment}-sale", "live", SITE.environment, "file:///quadringent-autonomous-latest.json"
)


def _console_proof() -> dict[str, object]:
    proof = deepcopy(_proof())
    proof.update(
        {
            "format_version": "as400-console-v1",
            "flux": {"id": SITE.stream_prefix, "label": "SALE"},
        }
    )
    proof["lag"]["verdict"] = {"value": "BOUNDED"}
    proof["counters"].update(
        {
            "events_published": {"value": 10},
            "windows_published": {"value": 2},
        }
    )
    destination = proof["destination_proof"]
    destination.update(
        {
            "schema_version": "destination-proof-v1",
            "target": {
                "kind": "snowflake",
                "destination_id": SITE.destination_id,
                "environment": SITE.environment,
            },
            "activation": {
                "state": "active",
                "observed_at": destination["observed_at"],
                "checks": {
                    "configuration": "valid",
                    "credential": "available",
                    "connectivity": "reachable",
                    "authorization": "allowed",
                    "contract": "compatible",
                },
                "blocker_code": None,
            },
        }
    )
    destination["load"].update({"batch_count": 2, "incident_code": None})
    destination["destination"]["incident_code"] = None
    destination["reconciliation"]["window"] = {
        "from_exclusive": None,
        "to_inclusive": deepcopy(destination["source_checkpoint"]),
    }
    return proof


def _report(
    *,
    status: str = "pass",
    now=NOW,
    proof: dict[str, object] | None = None,
) -> dict[str, object]:
    telemetry = _telemetry()
    telemetry['collected_at'] = now.isoformat()
    if status == "breach":
        telemetry["snowpipe_pending_files"] = 11
    elif status == "unobserved":
        telemetry.pop("snowpipe_pending_files")
    elif status != "pass":
        raise AssertionError(status)
    return evaluate_slo(proof or _console_proof(), telemetry, _policy(), now=now, site=SITE)


class ObservabilitySnapshotTests(unittest.TestCase):
    def test_verifier_cli_writes_observability_in_the_output_document(self) -> None:
        import quadringent_autonomous_verify as cli
        from contextlib import redirect_stdout
        from io import StringIO, BytesIO
        from unittest.mock import Mock, patch
        from test_slo_telemetry import S3Client, CloudWatchClient, SnowflakeCursor

        prior_report = _report(status='breach', now=NOW - timedelta(seconds=30))
        prior = attach_observability_snapshot(_console_proof(), prior_report, reconcile_alerts(prior_report, site=SITE), site=SITE)
        class Storage(S3Client):
            published = None
            def get_object(self, **kwargs):
                payload = json.dumps(prior).encode()
                return {'Body': BytesIO(payload), 'ContentLength': len(payload), 'ETag': '"prior"'}
            def put_object(self, **kwargs):
                self.published = kwargs
        storage = Storage()

        class Cursor(SnowflakeCursor):
            def execute(self, sql, params=None):
                if "APPROX_PERCENTILE" in sql:
                    self._row = (12.5, 21.0, 1.0, 10, 0)
                else:
                    super().execute(sql, params)

            def close(self):
                pass

        connection = Mock()
        connection.cursor.return_value = Cursor()
        sdk = Mock()
        sdk.Session.return_value.client.return_value = CloudWatchClient()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "capture.json").write_text(json.dumps(_console_proof()))
            (base / "keys.txt").write_text(f"{SITE.stream_prefix}/runs/test/batch-one.jsonl\n")
            (base / "policy.json").write_text(json.dumps(_policy().to_mapping()))
            argv = ["verify", "--capture-snapshot", str(base / "capture.json"),
                    "--object-keys-file", str(base / "keys.txt"), "--run-tag", "TEST",
                    "--proof-output", str(base / "result.json"), "--slo-policy", str(base / "policy.json"),
                    "--proof-s3-uri", SITE.autonomous_proof_s3_uri, "--publish-confirm", SITE.publish_confirmation_token]
            with patch.object(sys, "argv", argv), patch.dict(sys.modules, {"boto3": sdk, "snowflake": Mock(), "snowflake.connector": Mock()}), \
                 patch.object(cli, "_connect_snowflake", return_value=connection), \
                 patch.object(cli, "_verify_with_wait", return_value=(_console_proof(), NOW)), \
                 patch.object(cli, "_publication_client", return_value=storage), \
                 patch.object(cli, "datetime") as clock, redirect_stdout(StringIO()):
                clock.now.return_value = NOW
                self.assertEqual(cli.main(), 0)
            result = json.loads((base / "result.json").read_text())
            self.assertEqual(result["observability"]["slo_report"]["status"], "pass")
            self.assertEqual(project_console_document(result, LIVE_SOURCE, now=NOW).observability.status, "pass")
            cost = next(c for c in project_console_document(result, LIVE_SOURCE, now=NOW).observability.checks if c.id == 'snowflake_credits')
            self.assertTrue(cost.reason.startswith('metering_window_'))
            _, _, start, end, rows = cost.reason.split('_')
            self.assertEqual(int(end) - int(start), 86400)
            self.assertEqual(int(rows), 2)
            self.assertEqual(storage.published.get('IfMatch'), '"prior"')
            restored = json.loads(storage.published['Body'])['observability']['alert_state']
            alert = next(a for a in restored['alerts'] if a['check_id'] == 'snowpipe_queue')
            self.assertEqual(alert['lifecycle_state'], 'resolved')
            self.assertEqual(alert['first_fired_at'], prior_report['observed_at'])

    def test_autonomous_collection_reaches_cockpit_with_partial_permissions(self) -> None:
        from quadringent.observability_snapshot import collect_and_attach_observability
        from test_slo_telemetry import S3Client, SnowflakeCursor

        class DeniedCloudWatch:
            def get_metric_statistics(self, **kwargs):
                raise PermissionError("private provider diagnostic")

        class Cursor(SnowflakeCursor):
            def execute(self, sql, params=None):
                if "APPROX_PERCENTILE" in sql:
                    self._row = (12.5, 21.0, 1.0, 10, 0)
                else:
                    super().execute(sql, params)

        proof = _console_proof()
        combined = collect_and_attach_observability(
            proof, S3Client(), DeniedCloudWatch(), Cursor(),
            object_keys=[f"{SITE.stream_prefix}/runs/test/batch-one.jsonl"],
            policy=_policy(), now=NOW,
        site=SITE)
        projection = project_console_document(combined, LIVE_SOURCE, now=NOW)
        self.assertEqual(projection.observability.status, "unobserved")
        checks = combined["observability"]["slo_report"]["checks"]
        self.assertEqual(next(c for c in checks if c["id"] == "s3_requests")["status"], "unobserved")
        self.assertEqual(next(c for c in checks if c["id"] == "delivery_latency_p95")["status"], "pass")
        self.assertNotIn("private provider diagnostic", json.dumps(combined))
        self.assertNotIn("observability", proof)

    def test_nominal_snapshot_is_immutable_bound_to_dev_sale_and_projectable(self) -> None:
        proof = _console_proof()
        report = _report(proof=proof)
        alerts = reconcile_alerts(report, site=SITE)

        combined = attach_observability_snapshot(proof, report, alerts, site=SITE)
        projection = project_console_document(combined, LIVE_SOURCE, now=NOW)

        self.assertNotIn("observability", proof)
        self.assertEqual(combined["observability"]["pipeline_id"], SITE.pipeline_id)
        self.assertEqual(projection.observability.status, "pass")
        self.assertEqual(
            projection.observability.quality,
            {
                "coverage": "complete",
                "freshness": "fresh",
                "evidence_kind": "live",
            },
        )
        self.assertEqual(len(projection.observability.checks), 13)
        self.assertEqual(projection.observability.alerts, ())
        self.assertEqual(
            projection.to_dict()["observability"]["status"], "pass"
        )

    def test_schemas_from_before_the_rename_remain_readable_and_projectable(self) -> None:
        """Les enveloppes persistées par CDC Forge restent lisibles —
        l'historique d'alertes n'est jamais réinitialisé pour un renommage."""
        from quadringent.slo_alerts import _digest

        proof = _console_proof()
        legacy_report = dict(_report(proof=proof), schema_version="cdcforge-slo-v1")
        batch = reconcile_alerts(legacy_report, site=SITE)
        state = dict(batch["state"])
        state.pop("state_digest")
        state["schema_version"] = "cdcforge-alert-state-v1"
        state["state_digest"] = _digest(state)

        document = attach_observability_snapshot(proof, legacy_report, state, site=SITE)
        document["observability"]["schema_version"] = "cdcforge-observability-v1"
        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.observability.status, "pass")
        self.assertEqual(
            document["observability"]["alert_state"]["schema_version"],
            "cdcforge-alert-state-v1",
        )
        self.assertEqual(
            document["observability"]["slo_report"]["schema_version"],
            "cdcforge-slo-v1",
        )

    def test_missing_observability_is_explicitly_unavailable_never_green(self) -> None:
        projection = project_console_document(_console_proof(), LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.observability.status, "unavailable")
        self.assertEqual(projection.observability.quality["coverage"], "none")
        self.assertEqual(projection.observability.quality["freshness"], "unavailable")
        self.assertEqual(projection.observability.reason, "observability_not_attached")

    def test_breach_and_resolution_keep_the_alert_lifecycle_without_changing_delivery(self) -> None:
        proof = _console_proof()
        breached_report = _report(status="breach", proof=proof)
        opened = reconcile_alerts(breached_report, site=SITE)
        breached = project_console_document(
            attach_observability_snapshot(proof, breached_report, opened, site=SITE),
            LIVE_SOURCE,
            now=NOW,
        )

        self.assertEqual(breached.status, "healthy")
        self.assertEqual(breached.observability.status, "breach")
        self.assertEqual(len(breached.observability.alerts), 1)
        self.assertEqual(breached.observability.alerts[0].check_id, "snowpipe_queue")
        self.assertEqual(breached.observability.alerts[0].lifecycle_state, "firing")

        later = NOW + timedelta(seconds=30)
        resolved_report = _report(status="pass", now=later, proof=proof)
        resolved_state = reconcile_alerts(resolved_report, opened["state"], site=SITE)
        resolved = project_console_document(
            attach_observability_snapshot(proof, resolved_report, resolved_state, site=SITE),
            LIVE_SOURCE,
            now=later,
        )

        self.assertEqual(resolved.observability.status, "pass")
        self.assertEqual(resolved.observability.alerts[0].lifecycle_state, "resolved")
        self.assertEqual(resolved.observability.alerts[0].occurrence_count, 1)

    def test_stale_or_simulated_observability_preserves_status_but_bounds_quality(self) -> None:
        proof = _console_proof()
        report = _report(proof=proof)
        combined = attach_observability_snapshot(
            proof, report, reconcile_alerts(report, site=SITE)
        , site=SITE)

        stale = project_console_document(
            combined, LIVE_SOURCE, now=NOW + timedelta(minutes=10)
        )
        simulated = project_console_document(
            combined,
            SourceDescriptor(SITE.pipeline_id, "simulation", SITE.environment, "file:///fixture.json"),
            now=NOW,
        )

        self.assertEqual(stale.observability.status, "pass")
        self.assertEqual(stale.observability.quality["freshness"], "stale")
        self.assertEqual(simulated.observability.quality["evidence_kind"], "simulation")

    def test_active_alert_for_a_retired_check_forces_partial_coverage(self) -> None:
        proof = _console_proof()
        retired_report = _report(proof=proof)
        retired_report["status"] = "breach"
        retired_report["checks"].append(
            {
                "id": "retired_metric",
                "stage": "destination",
                "status": "breach",
                "observed": 2,
                "threshold": 1,
                "unit": "events",
                "reason": "threshold_exceeded",
            }
        )
        retired_report["alerts"].append(
            {
                "check_id": "retired_metric",
                "stage": "destination",
                "status": "breach",
                "severity": "critical",
                "reason": "threshold_exceeded",
            }
        )
        opened = reconcile_alerts(retired_report, site=SITE)
        current_report = _report(now=NOW + timedelta(seconds=30), proof=proof)
        preserved = reconcile_alerts(current_report, opened["state"], site=SITE)

        projection = project_console_document(
            attach_observability_snapshot(proof, current_report, preserved, site=SITE),
            LIVE_SOURCE,
            now=NOW + timedelta(seconds=30),
        )

        self.assertEqual(projection.observability.status, "pass")
        self.assertEqual(projection.observability.alerts[0].check_id, "retired_metric")
        self.assertEqual(projection.observability.alerts[0].lifecycle_state, "firing")
        self.assertEqual(projection.observability.quality["coverage"], "partial")

    def test_mismatched_or_tampered_observability_fails_closed(self) -> None:
        proof = _console_proof()
        report = _report(proof=proof)
        alerts = reconcile_alerts(report, site=SITE)

        tampered = deepcopy(alerts)
        tampered["state"]["alerts"] = [{"unsafe": "state"}]
        with self.assertRaisesRegex(ValueError, "alert state"):
            attach_observability_snapshot(proof, report, tampered, site=SITE)

        combined = attach_observability_snapshot(proof, report, alerts, site=SITE)
        with self.assertRaises(ProjectionError) as captured:
            project_console_document(
                combined,
                SourceDescriptor("another-pipeline", "live", SITE.environment, "file:///x.json"),
                now=NOW,
            )
        self.assertEqual(captured.exception.code, "invalid_observability")

    def test_cli_writes_atomically_and_preserves_last_good_output_on_failure(self) -> None:
        root = Path(__file__).parents[1]
        script = root / "scripts/quadringent_observability_snapshot.py"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proof_path = base / "proof.json"
            report_path = base / "report.json"
            alerts_path = base / "alerts.json"
            output_path = base / "combined.json"
            proof = _console_proof()
            report = _report(proof=proof)
            alerts = reconcile_alerts(report, site=SITE)
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            report_path.write_text(json.dumps(report), encoding="utf-8")
            alerts_path.write_text(json.dumps(alerts), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--proof",
                    str(proof_path),
                    "--report",
                    str(report_path),
                    "--alerts",
                    str(alerts_path),
                    "--out",
                    str(output_path),
                ],
                cwd=root,
                env={**__import__("os").environ, "PYTHONPATH": f"{root / 'src'}:{root / 'scripts'}"},
                capture_output=True,
                text=True,
                check=False,
            )
            good = output_path.read_text(encoding="utf-8")
            report_path.write_text('{"secret":"must-not-leak"}', encoding="utf-8")
            failed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--proof",
                    str(proof_path),
                    "--report",
                    str(report_path),
                    "--alerts",
                    str(alerts_path),
                    "--out",
                    str(output_path),
                ],
                cwd=root,
                env={**__import__("os").environ, "PYTHONPATH": f"{root / 'src'}:{root / 'scripts'}"},
                capture_output=True,
                text=True,
                check=False,
            )
            after_failure = output_path.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(good)["observability"]["schema_version"], "quadringent-observability-v1")
        self.assertEqual(failed.returncode, 3)
        self.assertNotIn("must-not-leak", failed.stderr)
        self.assertEqual(after_failure, good)


if __name__ == "__main__":
    unittest.main()
