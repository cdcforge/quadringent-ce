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

from quadringent.slo import evaluate_slo
from quadringent.slo_alerts import reconcile_alerts
from test_slo import NOW, _policy, _proof, _telemetry


SITE = site_fixture.build_test_site()

def _report(*, status: str = "pass", seconds: int = 0) -> dict[str, object]:
    telemetry = _telemetry()
    telemetry['collected_at'] = (NOW + timedelta(seconds=seconds)).isoformat()
    if status == "breach":
        telemetry["snowpipe_pending_files"] = 11
    elif status == "unobserved":
        telemetry.pop("snowpipe_pending_files")
    elif status != "pass":
        raise AssertionError(f"unsupported test status: {status}")
    return evaluate_slo(
        _proof(),
        telemetry,
        _policy(),
        now=NOW + timedelta(seconds=seconds),
    site=SITE)


class SloAlertLifecycleTests(unittest.TestCase):
    def test_nominal_report_creates_no_alert_or_false_notification(self) -> None:
        result = reconcile_alerts(_report(), site=SITE)

        self.assertEqual(result["schema_version"], "quadringent-alert-batch-v1")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["state"]["alerts"], [])
        self.assertEqual(result["source_report"]["status"], "pass")

    def test_new_breach_opens_one_stable_deduplicated_alert(self) -> None:
        opened = reconcile_alerts(_report(status="breach"), site=SITE)
        event = opened["events"][0]
        record = opened["state"]["alerts"][0]

        self.assertEqual(event["kind"], "firing")
        self.assertEqual(event["transition"], "opened")
        self.assertEqual(event["check_id"], "snowpipe_queue")
        self.assertEqual(event["severity"], "critical")
        self.assertTrue(event["fingerprint"].startswith("sha256:"))
        self.assertEqual(event["fingerprint"], record["fingerprint"])
        self.assertEqual(record["lifecycle_state"], "firing")
        self.assertEqual(record["occurrence_count"], 1)
        self.assertEqual(record["evaluation_count"], 1)

        repeated = reconcile_alerts(
            _report(status="breach", seconds=30), opened["state"]
        , site=SITE)
        repeated_record = repeated["state"]["alerts"][0]

        self.assertEqual(repeated["events"], [])
        self.assertEqual(repeated_record["fingerprint"], record["fingerprint"])
        self.assertEqual(repeated_record["firing_since"], record["firing_since"])
        self.assertEqual(repeated_record["occurrence_count"], 1)
        self.assertEqual(repeated_record["evaluation_count"], 2)

    def test_unobserved_after_breach_stays_firing_and_emits_changed_not_resolved(self) -> None:
        opened = reconcile_alerts(_report(status="breach"), site=SITE)
        changed = reconcile_alerts(
            _report(status="unobserved", seconds=30), opened["state"]
        , site=SITE)

        self.assertEqual(len(changed["events"]), 1)
        self.assertEqual(changed["events"][0]["kind"], "firing")
        self.assertEqual(changed["events"][0]["transition"], "changed")
        self.assertEqual(changed["events"][0]["severity"], "warning")
        self.assertEqual(changed["state"]["alerts"][0]["lifecycle_state"], "firing")
        self.assertIsNone(changed["state"]["alerts"][0]["resolved_at"])

    def test_explicit_pass_resolves_once_then_reopen_is_a_new_occurrence(self) -> None:
        opened = reconcile_alerts(_report(status="breach"), site=SITE)
        resolved = reconcile_alerts(_report(status="pass", seconds=30), opened["state"], site=SITE)

        self.assertEqual(resolved["events"][0]["kind"], "resolved")
        self.assertEqual(resolved["events"][0]["transition"], "resolved")
        self.assertEqual(resolved["state"]["alerts"][0]["lifecycle_state"], "resolved")

        still_resolved = reconcile_alerts(
            _report(status="pass", seconds=60), resolved["state"]
        , site=SITE)
        self.assertEqual(still_resolved["events"], [])

        reopened = reconcile_alerts(
            _report(status="breach", seconds=90), still_resolved["state"]
        , site=SITE)
        record = reopened["state"]["alerts"][0]
        self.assertEqual(reopened["events"][0]["transition"], "reopened")
        self.assertEqual(record["occurrence_count"], 2)
        self.assertEqual(record["evaluation_count"], 3)

    def test_exact_replay_is_idempotent_but_same_timestamp_conflict_is_rejected(self) -> None:
        report = _report(status="breach")
        opened = reconcile_alerts(report, site=SITE)
        replayed = reconcile_alerts(report, opened["state"], site=SITE)

        self.assertEqual(replayed["events"], [])
        self.assertEqual(replayed["state"], opened["state"])

        conflicting = deepcopy(report)
        check = next(
            item for item in conflicting["checks"] if item["id"] == "snowpipe_queue"
        )
        check["observed"] = 12
        with self.assertRaisesRegex(ValueError, "conflicting report"):
            reconcile_alerts(conflicting, opened["state"], site=SITE)

    def test_semantically_identical_reordered_report_is_an_idempotent_replay(self) -> None:
        report = _report(status="breach")
        second = next(item for item in report["checks"] if item["id"] == "s3_requests")
        second["status"] = "breach"
        second["observed"] = 500_001
        second["reason"] = "threshold_exceeded"
        report["alerts"].append(
            {
                "check_id": "s3_requests",
                "stage": "cost",
                "status": "breach",
                "severity": "critical",
                "reason": "threshold_exceeded",
            }
        )
        opened = reconcile_alerts(report, site=SITE)
        reordered = deepcopy(report)
        reordered["checks"].reverse()
        reordered["alerts"].reverse()

        replayed = reconcile_alerts(reordered, opened["state"], site=SITE)

        self.assertEqual(replayed["events"], [])
        self.assertEqual(replayed["state"], opened["state"])

    def test_stale_report_and_inconsistent_alert_projection_fail_closed(self) -> None:
        current = reconcile_alerts(_report(status="breach", seconds=30), site=SITE)

        with self.assertRaisesRegex(ValueError, "older than alert state"):
            reconcile_alerts(_report(status="breach"), current["state"], site=SITE)

        inconsistent = _report(status="breach", seconds=60)
        inconsistent["alerts"] = []
        with self.assertRaisesRegex(ValueError, "alerts do not match"):
            reconcile_alerts(inconsistent, current["state"], site=SITE)

    def test_previous_unknown_active_check_is_never_resolved_by_omission(self) -> None:
        opened = reconcile_alerts(_report(status="breach"), site=SITE)
        without_snowpipe = _report(status="pass", seconds=30)
        without_snowpipe["checks"] = [
            check
            for check in without_snowpipe["checks"]
            if check["id"] != "snowpipe_queue"
        ]

        result = reconcile_alerts(without_snowpipe, opened["state"], site=SITE)
        records = {item["check_id"]: item for item in result["state"]["alerts"]}

        self.assertEqual(records["snowpipe_queue"]["lifecycle_state"], "firing")
        self.assertEqual(result["events"], [])

    def test_tampered_previous_state_is_rejected_before_any_transition(self) -> None:
        opened = reconcile_alerts(_report(status="breach"), site=SITE)
        tampered = deepcopy(opened["state"])
        tampered["alerts"][0]["fingerprint"] = "sha256:tampered"

        with self.assertRaisesRegex(ValueError, "fingerprint"):
            reconcile_alerts(_report(status="pass", seconds=30), tampered, site=SITE)

        tampered_counts = deepcopy(opened["state"])
        tampered_counts["alerts"][0]["occurrence_count"] = 999
        tampered_counts["alerts"][0]["evaluation_count"] = 999
        with self.assertRaisesRegex(ValueError, "integrity digest"):
            reconcile_alerts(_report(status="pass", seconds=30), tampered_counts, site=SITE)

    def test_non_finite_report_value_and_future_state_record_fail_closed(self) -> None:
        report = _report(status="breach")
        check = next(item for item in report["checks"] if item["id"] == "snowpipe_queue")
        check["observed"] = float("nan")
        with self.assertRaisesRegex(ValueError, "JSON-compatible"):
            reconcile_alerts(report, site=SITE)

        opened = reconcile_alerts(_report(status="breach"), site=SITE)
        future_record = deepcopy(opened["state"])
        future_record["alerts"][0]["last_observed_at"] = (
            NOW + timedelta(seconds=60)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "newer than state"):
            reconcile_alerts(_report(status="pass", seconds=30), future_record, site=SITE)


def _cli_env(root, extra=""):
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{root / 'src'}:{root / 'scripts'}{extra}"
    return env


class SloAlertCliTests(unittest.TestCase):
    def test_cli_atomically_persists_state_and_drives_open_then_resolve(self) -> None:
        root = Path(__file__).parents[1]
        script = root / "scripts/quadringent_slo_alerts.py"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            report_path = base / "report.json"
            state_path = base / "alerts.json"
            report_path.write_text(
                json.dumps(_report(status="breach")), encoding="utf-8"
            )

            opened = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--report",
                    str(report_path),
                    "--state",
                    str(state_path),
                ],
                cwd=root,
                env=_cli_env(root, f":{root / 'tests'}"),
                capture_output=True,
                text=True,
                check=False,
            )
            opened_document = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertEqual(opened.returncode, 1, opened.stderr)
            self.assertEqual(json.loads(opened.stdout)["event_count"], 1)
            self.assertEqual(opened_document["events"][0]["transition"], "opened")

            report_path.write_text(
                json.dumps(_report(status="pass", seconds=30)), encoding="utf-8"
            )
            resolved = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--report",
                    str(report_path),
                    "--state",
                    str(state_path),
                ],
                cwd=root,
                env=_cli_env(root, f":{root / 'tests'}"),
                capture_output=True,
                text=True,
                check=False,
            )
            resolved_document = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(resolved_document["events"][0]["transition"], "resolved")

    def test_invalid_input_does_not_overwrite_last_known_state_or_leak_details(self) -> None:
        root = Path(__file__).parents[1]
        script = root / "scripts/quadringent_slo_alerts.py"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            report_path = base / "report.json"
            state_path = base / "alerts.json"
            report_path.write_text('{"secret":"must-not-leak"}', encoding="utf-8")
            state_path.write_text('{"last":"known-good"}\n', encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--report",
                    str(report_path),
                    "--state",
                    str(state_path),
                ],
                cwd=root,
                env=_cli_env(root),
                capture_output=True,
                text=True,
                check=False,
            )
            persisted = state_path.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 3)
        self.assertNotIn("must-not-leak", completed.stderr)
        self.assertEqual(persisted, '{"last":"known-good"}\n')


if __name__ == "__main__":
    unittest.main()
