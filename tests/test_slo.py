from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import site_fixture

from quadringent.slo import SloPolicy, evaluate_slo


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
SITE = site_fixture.build_test_site()


def _at(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _policy() -> SloPolicy:
    return SloPolicy(
        capture_freshness_seconds=120,
        s3_freshness_seconds=180,
        destination_freshness_seconds=180,
        checkpoint_lag_sequences=1_000,
        delivery_latency_p95_seconds=60,
        delivery_latency_p99_seconds=120,
        s3_requests_24h=500_000,
        snowpipe_pending_files=0,
        snowflake_credits_24h=1.0,
        observability_freshness_seconds=240,
    )


def _proof() -> dict[str, object]:
    checkpoint = {"receiver": "DEMOJRN4000", "sequence": 100}
    return {
        "generated_at": _at(30),
        "run": {"state": "RUNNING", "last_error": None, "started_at": _at(60)},
        "position": {
            "checkpoint": checkpoint,
            "source_tail": {"receiver": "DEMOJRN4000", "sequence": 101},
        },
        "lag": {"current": {"value": 1}},
        "counters": {"errors": {"value": 0}},
        "destination_proof": {
            "observed_at": _at(20),
            "source_checkpoint": checkpoint,
            "load": {
                "state": "succeeded",
                "observed_at": _at(20),
                "checkpoint": checkpoint,
                "event_count": 10,
                "failed_event_count": 0,
            },
            "destination": {
                "state": "applied",
                "observed_at": _at(20),
                "apply_checkpoint": checkpoint,
                "failed_mutation_count": 0,
            },
            "reconciliation": {
                "state": "matched",
                "observed_at": _at(20),
                "captured_event_count": 10,
                "loaded_event_count": 10,
                "ledger_event_count": 10,
                "distinct_event_count": 10,
                "duplicate_event_count": 0,
                "missing_event_count": 0,
                "unexpected_event_count": 0,
                "failed_mutation_count": 0,
            },
        },
    }


def _telemetry() -> dict[str, object]:
    return {
        "collected_at": NOW.isoformat(),
        "s3_last_object_at": _at(25),
        "s3_requests_24h": 20,
        "snowpipe_pending_files": 0,
        "delivery_latency_p95_seconds": 12.5,
        "delivery_latency_p99_seconds": 21.0,
        "snowflake_credits_24h": 0.05,
        "snowflake_credits_window": {
            "from_inclusive": "2026-08-31T05:00:00+00:00",
            "to_exclusive": "2026-09-01T05:00:00+00:00",
            "scope": SITE.warehouse_name, "status": "delayed_metering", "reported_rows": 2,
        },
    }


class SloEvaluationTests(unittest.TestCase):
    def test_replaying_telemetry_does_not_refresh_the_report_or_change_its_verdict(self):
        initial = evaluate_slo(_proof(), _telemetry(), _policy(), now=NOW, site=SITE)
        replayed = evaluate_slo(_proof(), _telemetry(), _policy(), now=NOW + timedelta(days=1), site=SITE)
        # Le rapport décrit toujours l'instant de collecte : rejouer un fichier
        # ancien ne le rafraîchit pas. Mais le check dead-man compare cet
        # instant à l'horloge réelle et révèle que l'observabilité s'est tue.
        self.assertEqual(replayed['observed_at'], NOW.isoformat())
        initial_by_id = {check['id']: check for check in initial['checks']}
        replayed_by_id = {check['id']: check for check in replayed['checks']}
        for check_id, check in initial_by_id.items():
            if check_id == 'observability_freshness':
                continue
            self.assertEqual(replayed_by_id[check_id], check)
        self.assertEqual(replayed_by_id['observability_freshness']['status'], 'breach')
        self.assertEqual(replayed['status'], 'breach')

    def test_a_fail_closed_capture_is_a_breach(self):
        """Un arret fail-closed doit produire une violation, pas un silence.

        Mesure du 16/09 : un rattrapage reel a echoue apres cinq erreurs
        consecutives et publie `STOPPED_FAIL_CLOSED`. C'est le signal qu'un
        operateur doit voir : sans ce test, l'etat degrade pouvait rester
        classe comme inobservable au lieu d'etre une alerte.
        """

        proof = _proof()
        proof['run'] = {
            'state': 'STOPPED_FAIL_CLOSED',
            'last_error': {'at': _at(1), 'head': 'bounded IBM i reader failed'},
            'started_at': _at(60),
        }
        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        capture = next(
            check for check in report['checks'] if check['id'] == 'capture_state'
        )
        self.assertEqual(capture['status'], 'breach')
        self.assertEqual(capture['reason'], 'capture_fail_closed')
        self.assertEqual(report['status'], 'breach')

    def test_a_planned_stop_without_error_is_not_a_breach(self):
        """Un arret planifie n'est pas une panne : il ne doit pas alerter."""

        proof = _proof()
        proof['run'] = {
            'state': 'STOPPED_BUDGET',
            'last_error': None,
            'started_at': _at(60),
        }
        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        capture = next(
            check for check in report['checks'] if check['id'] == 'capture_state'
        )
        self.assertEqual(capture['status'], 'pass')
        self.assertEqual(capture['reason'], 'planned_stop')

    def test_an_unknown_run_state_is_never_passed(self):
        """Un etat inconnu reste inobservable : il ne passe jamais."""

        proof = _proof()
        proof['run'] = {'state': 'STOPPED_SOMETHING_ELSE', 'last_error': None, 'started_at': _at(60)}
        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        capture = next(
            check for check in report['checks'] if check['id'] == 'capture_state'
        )
        self.assertEqual(capture['status'], 'unobserved')
        self.assertNotEqual(report['status'], 'pass')

    def test_undated_external_measurements_cannot_pass(self):
        telemetry = _telemetry()
        del telemetry['collected_at']
        report = evaluate_slo(_proof(), telemetry, _policy(), now=NOW, site=SITE)
        unknown = {c['id'] for c in report['checks'] if c['status'] == 'unobserved'}
        self.assertEqual(unknown, {'s3_freshness', 's3_requests', 'snowpipe_queue',
                                   'delivery_latency_p95', 'delivery_latency_p99', 'snowflake_credits',
                                   'observability_freshness'})

    def test_newer_proof_cannot_be_certified_by_an_older_collection(self):
        proof = _proof()
        proof['generated_at'] = (NOW + timedelta(seconds=10)).isoformat()
        proof['destination_proof']['observed_at'] = proof['generated_at']
        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW + timedelta(days=1), site=SITE)
        for check_id in ('capture_freshness', 'canonical_freshness'):
            check = next(c for c in report['checks'] if c['id'] == check_id)
            self.assertEqual((check['status'], check['reason']), ('unobserved', 'clock_untrusted'))

    def test_invalid_or_future_collection_time_is_rejected(self):
        for value in ('invalid', '2026-09-01T12:00:00', (NOW + timedelta(seconds=1)).isoformat(), 42):
            with self.subTest(value=value):
                telemetry = _telemetry()
                telemetry['collected_at'] = value
                with self.assertRaises(ValueError):
                    evaluate_slo(_proof(), telemetry, _policy(), now=NOW, site=SITE)

    def test_legacy_cost_without_window_is_not_certified_as_delayed_metering(self):
        telemetry = _telemetry()
        telemetry.pop('snowflake_credits_window', None)
        cost = next(c for c in evaluate_slo(_proof(), telemetry, _policy(), now=NOW, site=SITE)['checks'] if c['id'] == 'snowflake_credits')
        self.assertEqual(cost['status'], 'unobserved')

    def test_destination_refresh_does_not_refresh_capture_slo(self) -> None:
        proof = _proof()
        proof["capture_observed_at"] = _at(600)
        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        check = next(item for item in report["checks"] if item["id"] == "capture_freshness")
        self.assertEqual(check["status"], "breach")
        proof["capture_observed_at"] = None
        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        check = next(item for item in report["checks"] if item["id"] == "capture_freshness")
        self.assertEqual(check["status"], "unobserved")

    def test_versioned_dev_policy_is_executable_on_nominal_and_breached_evidence(self) -> None:
        policy_path = Path(__file__).parents[1] / "infra-values/slo-policy-dev.json"
        policy = SloPolicy.from_mapping(json.loads(policy_path.read_text(encoding="utf-8")))

        nominal = evaluate_slo(_proof(), _telemetry(), policy, now=NOW, site=SITE)
        self.assertEqual(nominal["status"], "pass")

        breached_telemetry = _telemetry()
        breached_telemetry["s3_requests_24h"] = 10_000_000
        breached = evaluate_slo(_proof(), breached_telemetry, policy, now=NOW, site=SITE)
        self.assertEqual(breached["status"], "breach")
        self.assertIn(
            "s3_requests",
            {alert["check_id"] for alert in breached["alerts"]},
        )

    def test_complete_fresh_observation_passes_every_required_signal(self) -> None:
        report = evaluate_slo(_proof(), _telemetry(), _policy(), now=NOW, site=SITE)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["alerts"], [])
        self.assertEqual(
            {check["id"] for check in report["checks"]},
            {
                "capture_freshness",
                "capture_state",
                "capture_errors",
                "checkpoint_lag",
                "s3_freshness",
                "s3_requests",
                "snowpipe_queue",
                "canonical_freshness",
                "delivery_latency_p95",
                "delivery_latency_p99",
                "reconciliation",
                "snowflake_credits",
                "observability_freshness",
            },
        )
        self.assertTrue(all(check["status"] == "pass" for check in report["checks"]))

    def test_missing_external_metrics_are_unobserved_never_green(self) -> None:
        report = evaluate_slo(_proof(), {}, _policy(), now=NOW, site=SITE)

        self.assertEqual(report["status"], "unobserved")
        unobserved = {
            check["id"] for check in report["checks"] if check["status"] == "unobserved"
        }
        self.assertEqual(
            unobserved,
            {
                "s3_freshness",
                "s3_requests",
                "snowpipe_queue",
                "delivery_latency_p95",
                "delivery_latency_p99",
                "snowflake_credits",
                "observability_freshness",
            },
        )
        self.assertEqual(
            {alert["check_id"] for alert in report["alerts"]}, unobserved
        )

    def test_breaches_report_the_exact_stage_without_compensating_passes(self) -> None:
        proof = _proof()
        proof["generated_at"] = _at(121)
        proof["run"]["state"] = "STOPPED_FAIL_CLOSED"
        proof["counters"]["errors"]["value"] = 1
        proof["lag"]["current"]["value"] = 1_001
        reconciliation = proof["destination_proof"]["reconciliation"]
        reconciliation["missing_event_count"] = 1
        telemetry = _telemetry()
        telemetry.update(
            {
                "s3_requests_24h": 500_001,
                "snowpipe_pending_files": 1,
                "delivery_latency_p99_seconds": 121,
                "snowflake_credits_24h": 1.01,
            }
        )

        report = evaluate_slo(proof, telemetry, _policy(), now=NOW, site=SITE)

        self.assertEqual(report["status"], "breach")
        breached = {
            check["id"] for check in report["checks"] if check["status"] == "breach"
        }
        self.assertEqual(
            breached,
            {
                "capture_freshness",
                "capture_state",
                "capture_errors",
                "checkpoint_lag",
                "s3_requests",
                "snowpipe_queue",
                "delivery_latency_p99",
                "reconciliation",
                "snowflake_credits",
            },
        )
        self.assertEqual(
            {alert["check_id"] for alert in report["alerts"]}, breached
        )

    def test_receiver_mismatch_makes_checkpoint_lag_unobservable(self) -> None:
        proof = _proof()
        proof["position"]["source_tail"]["receiver"] = "DEMOJRN4001"

        report = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)

        checkpoint = next(
            check for check in report["checks"] if check["id"] == "checkpoint_lag"
        )
        self.assertEqual(checkpoint["status"], "unobserved")
        self.assertEqual(checkpoint["reason"], "receiver_chain_required")
        self.assertEqual(report["status"], "unobserved")

    def test_fresh_planned_budget_stop_is_accepted_but_unknown_state_is_not(self) -> None:
        proof = _proof()
        proof["run"] = {
            "state": "STOPPED_BUDGET",
            "last_error": None,
            "stopped_because": "budget reached",
            "started_at": _at(60),
        }

        planned = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        state = next(check for check in planned["checks"] if check["id"] == "capture_state")
        self.assertEqual(state["status"], "pass")

        proof["run"]["state"] = "UNKNOWN"
        unknown = evaluate_slo(proof, _telemetry(), _policy(), now=NOW, site=SITE)
        state = next(check for check in unknown["checks"] if check["id"] == "capture_state")
        self.assertEqual(state["status"], "unobserved")

    def test_invalid_negative_or_boolean_measurement_is_rejected(self) -> None:
        for field, value in (
            ("s3_requests_24h", -1),
            ("snowpipe_pending_files", True),
            ("delivery_latency_p95_seconds", -0.1),
            ("snowflake_credits_24h", "0.1"),
            ("delivery_latency_p99_seconds", float("nan")),
            ("snowflake_credits_24h", float("inf")),
        ):
            with self.subTest(field=field):
                telemetry = _telemetry()
                telemetry[field] = value
                with self.assertRaisesRegex(ValueError, field):
                    evaluate_slo(_proof(), telemetry, _policy(), now=NOW, site=SITE)


class SloCliTests(unittest.TestCase):
    def test_cli_exits_two_for_unobserved_and_never_writes_a_green_report(self) -> None:
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proof = base / "proof.json"
            telemetry = base / "telemetry.json"
            policy = base / "policy.json"
            proof.write_text(json.dumps(_proof()), encoding="utf-8")
            telemetry.write_text("{}", encoding="utf-8")
            policy.write_text(json.dumps(_policy().to_mapping()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/quadringent_slo_check.py"),
                    "--proof",
                    str(proof),
                    "--telemetry",
                    str(telemetry),
                    "--policy",
                    str(policy),
                    "--now",
                    NOW.isoformat(),
                ],
                cwd=root,
                env={
                    "PYTHONPATH": f"{root / 'src'}:{root / 'scripts'}:{root}",
                    **site_fixture.TEST_SITE_ENV,
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "unobserved")


if __name__ == "__main__":
    unittest.main()
