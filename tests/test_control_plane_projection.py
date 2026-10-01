from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import unittest

from quadringent_control_plane.model import SourceDescriptor
from quadringent_control_plane.projection import (
    ProjectionError,
    build_overview,
    project_console_document,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
LIVE_SOURCE = SourceDescriptor("dev-cntr", "live", "dev", "file:///snapshot.json")
SIM_SOURCE = SourceDescriptor("sim-cntr", "simulation", "dev", "fixture://snapshot.json")


def fresh_running_document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": (NOW - timedelta(minutes=1)).isoformat(),
        "flux": {"id": "pays", "label": "CNTR"},
        "run": {"state": "RUNNING", "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {"events_published": {"value": 120}, "errors": {"value": 0}},
    }


def stale_document() -> dict[str, object]:
    document = fresh_running_document()
    document["generated_at"] = (NOW - timedelta(hours=2)).isoformat()
    return document


def fresh_destination_proof_document() -> dict[str, object]:
    document = fresh_running_document()
    observed_at = (NOW - timedelta(seconds=20)).isoformat()
    document["destination_proof"] = {
        "schema_version": "destination-proof-v1",
        "observed_at": observed_at,
        "source_checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
        "target": {
            "kind": "snowflake",
            "destination_id": "snowflake-dev-rd",
            "environment": "dev",
        },
        "activation": {
            "state": "active",
            "observed_at": observed_at,
            "checks": {
                "configuration": "valid",
                "credential": "available",
                "connectivity": "reachable",
                "authorization": "allowed",
                "contract": "compatible",
            },
            "blocker_code": None,
        },
        "load": {
            "state": "succeeded",
            "observed_at": observed_at,
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "batch_count": 4,
            "event_count": 120,
            "failed_event_count": 0,
            "incident_code": None,
        },
        "destination": {
            "state": "applied",
            "observed_at": observed_at,
            "apply_checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "failed_mutation_count": 0,
            "incident_code": None,
        },
        "reconciliation": {
            "state": "matched",
            "observed_at": observed_at,
            "window": {
                "from_exclusive": {"receiver": "DEMOJRN3776", "sequence": 0},
                "to_inclusive": {"receiver": "DEMOJRN3776", "sequence": 41},
            },
            "captured_event_count": 120,
            "loaded_event_count": 120,
            "ledger_event_count": 120,
            "distinct_event_count": 120,
            "duplicate_event_count": 0,
            "missing_event_count": 0,
            "unexpected_event_count": 0,
            "failed_mutation_count": 0,
        },
    }
    return document


class ControlPlaneProjectionTests(unittest.TestCase):
    def test_capture_observation_timestamp_cannot_be_replaced_by_publication(self) -> None:
        document = fresh_destination_proof_document()
        capture_at = (NOW - timedelta(hours=1)).isoformat()
        document["capture_observed_at"] = capture_at
        projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(projection.observed_at, capture_at)
        self.assertEqual(projection.stages[0].observed_at, capture_at)
        self.assertEqual(projection.quality["freshness"], "stale")
        self.assertEqual(projection.status, "unknown")

    def test_invalid_explicit_capture_timestamp_never_falls_back_to_publication(self) -> None:
        for timestamp in (None, "not-a-time", "2026-08-28T12:00:00"):
            with self.subTest(timestamp=timestamp):
                document = fresh_destination_proof_document()
                document["capture_observed_at"] = timestamp
                with self.assertRaises(ProjectionError):
                    project_console_document(document, LIVE_SOURCE, now=NOW)

    def test_verified_destination_counts_replace_capture_unknowns(self) -> None:
        document = fresh_destination_proof_document()
        document["counters"]["events_in_target"] = {"value": None}
        document["counters"]["duplicates_in_target"] = {"value": None}
        projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(projection.counters["events_in_target"], 120)
        self.assertEqual(projection.counters["duplicates_in_target"], 0)

    def test_unverified_destination_does_not_fill_capture_unknowns(self) -> None:
        for defect in ("stale", "checkpoint", "mismatch"):
            with self.subTest(defect=defect):
                document = fresh_destination_proof_document()
                document["counters"]["events_in_target"] = {"value": None}
                document["counters"]["duplicates_in_target"] = {"value": None}
                proof = document["destination_proof"]
                if defect == "stale":
                    proof["reconciliation"]["observed_at"] = (NOW - timedelta(hours=1)).isoformat()
                elif defect == "checkpoint":
                    proof["source_checkpoint"]["sequence"] = 40
                else:
                    proof["reconciliation"]["state"] = "mismatch"
                projection = project_console_document(document, LIVE_SOURCE, now=NOW)
                self.assertIsNone(projection.counters["events_in_target"])
                self.assertIsNone(projection.counters["duplicates_in_target"])

    def test_complete_fresh_live_destination_proof_is_the_only_healthy_path(self) -> None:
        projection = project_console_document(
            fresh_destination_proof_document(), LIVE_SOURCE, now=NOW
        )

        self.assertEqual(projection.status, "healthy")
        self.assertEqual(projection.quality["coverage"], "complete")
        self.assertEqual(
            [stage.status for stage in projection.stages],
            ["healthy", "healthy", "healthy", "healthy", "healthy"],
        )
        self.assertEqual(
            projection.summary, "Livraison Snowflake vérifiée de bout en bout"
        )

    def test_complete_simulation_or_historical_proof_remains_degraded(self) -> None:
        for source in (
            SIM_SOURCE,
            SourceDescriptor(
                "historical-cntr",
                "historical",
                "dev",
                "https://approved.example/snapshot",
            ),
        ):
            with self.subTest(evidence_kind=source.evidence_kind):
                projection = project_console_document(
                    fresh_destination_proof_document(), source, now=NOW
                )

                self.assertEqual(projection.status, "degraded")
                self.assertEqual(projection.quality["coverage"], "complete")
                self.assertEqual(projection.stages[3].status, "healthy")
                self.assertEqual(projection.stages[4].status, "healthy")

    def test_stale_destination_observations_make_the_pipeline_unknown(self) -> None:
        document = fresh_destination_proof_document()
        stale = (NOW - timedelta(minutes=10)).isoformat()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        proof["observed_at"] = stale
        for name in ("activation", "load", "destination", "reconciliation"):
            nested = proof[name]
            assert isinstance(nested, dict)
            nested["observed_at"] = stale

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.stages[3].status, "unknown")
        self.assertEqual(projection.stages[4].status, "unknown")
        self.assertEqual(projection.quality["coverage"], "complete")

    def test_destination_source_checkpoint_mismatch_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        proof["source_checkpoint"] = {"receiver": "DEMOJRN3776", "sequence": 40}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(projection.stages[3].status, "incident")
        self.assertEqual(projection.stages[4].status, "incident")
        self.assertEqual(
            projection.incident,
            {
                "code": "destination_source_checkpoint_mismatch",
                "type": "destination",
            },
        )

    def test_unusable_source_checkpoint_keeps_downstream_unknown_not_incident(self) -> None:
        document = fresh_destination_proof_document()
        document["position"] = {
            "checkpoint": None,
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
        }

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.stages[0].status, "unknown")
        self.assertEqual(projection.stages[3].status, "unknown")
        self.assertEqual(projection.stages[4].status, "unknown")
        self.assertIsNone(projection.incident)

    def test_load_checkpoint_behind_source_is_degraded_not_healthy(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        load = proof["load"]
        destination = proof["destination"]
        reconciliation = proof["reconciliation"]
        assert isinstance(load, dict)
        assert isinstance(destination, dict)
        assert isinstance(reconciliation, dict)
        load["checkpoint"] = {"receiver": "DEMOJRN3776", "sequence": 40}
        destination["apply_checkpoint"] = {
            "receiver": "DEMOJRN3776",
            "sequence": 40,
        }
        window = reconciliation["window"]
        assert isinstance(window, dict)
        window["to_inclusive"] = {"receiver": "DEMOJRN3776", "sequence": 40}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "degraded")
        self.assertEqual(projection.stages[3].status, "degraded")
        self.assertEqual(projection.stages[4].status, "degraded")
        self.assertEqual(projection.quality["coverage"], "partial")

    def test_apply_checkpoint_ahead_of_load_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        destination = proof["destination"]
        assert isinstance(destination, dict)
        destination["apply_checkpoint"] = {
            "receiver": "DEMOJRN3776",
            "sequence": 42,
        }

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(projection.stages[4].status, "incident")
        self.assertEqual(
            projection.incident,
            {"code": "destination_apply_ahead_of_load", "type": "destination"},
        )

    def test_reconciliation_mismatch_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        reconciliation = proof["reconciliation"]
        assert isinstance(reconciliation, dict)
        reconciliation.update(
            {
                "state": "mismatch",
                "loaded_event_count": 119,
                "ledger_event_count": 119,
                "distinct_event_count": 119,
                "missing_event_count": 1,
            }
        )

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(projection.stages[3].status, "incident")
        self.assertEqual(projection.stages[4].status, "incident")
        self.assertEqual(
            projection.incident,
            {
                "code": "destination_reconciliation_mismatch",
                "type": "destination",
            },
        )

    def test_active_activation_with_a_negative_check_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        activation = proof["activation"]
        assert isinstance(activation, dict)
        checks = activation["checks"]
        assert isinstance(checks, dict)
        checks["credential"] = "missing"

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {
                "code": "destination_activation_inconsistent",
                "type": "destination",
            },
        )

    def test_destination_environment_mismatch_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        target = proof["target"]
        assert isinstance(target, dict)
        target["environment"] = "prod"

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {"code": "destination_environment_mismatch", "type": "destination"},
        )

    def test_unknown_destination_property_is_rejected_without_reflection(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        proof["secret_token"] = "never-reflect-this-value"

        with self.assertRaises(ProjectionError) as captured:
            project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(captured.exception.code, "invalid_destination_proof")
        self.assertNotIn("never-reflect", captured.exception.safe_message)

    def test_catching_up_lag_prevents_healthy_even_with_complete_proof(self) -> None:
        document = fresh_destination_proof_document()
        document["lag"] = {
            "current": {"value": 4},
            "verdict": {"value": "CATCHING_UP"},
        }

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "degraded")
        self.assertEqual(projection.stages[3].status, "healthy")
        self.assertEqual(projection.stages[4].status, "healthy")

    def test_non_comparable_load_receiver_makes_downstream_unknown(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        load = proof["load"]
        assert isinstance(load, dict)
        load["checkpoint"] = {"receiver": "DEMOJRN3777", "sequence": 1}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.stages[3].status, "unknown")
        self.assertEqual(projection.stages[4].status, "unknown")
        self.assertEqual(projection.quality["coverage"], "partial")

    def test_load_checkpoint_ahead_of_source_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        load = proof["load"]
        assert isinstance(load, dict)
        load["checkpoint"] = {"receiver": "DEMOJRN3776", "sequence": 42}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {"code": "destination_load_ahead_of_source", "type": "destination"},
        )

    def test_blocked_activation_projects_only_the_allowlisted_code(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        activation = proof["activation"]
        assert isinstance(activation, dict)
        checks = activation["checks"]
        assert isinstance(checks, dict)
        activation["state"] = "blocked"
        activation["blocker_code"] = "destination_authorization_denied"
        checks["authorization"] = "denied"

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {"code": "destination_authorization_denied", "type": "destination"},
        )

    def test_not_configured_destination_is_unknown_not_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        activation = proof["activation"]
        assert isinstance(activation, dict)
        checks = activation["checks"]
        assert isinstance(checks, dict)
        activation["state"] = "not_configured"
        checks["configuration"] = "missing"

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertIsNone(projection.incident)
        self.assertEqual(projection.stages[3].status, "unknown")
        self.assertEqual(projection.stages[4].status, "unknown")

    def test_future_destination_clock_is_unknown_not_healthy(self) -> None:
        document = fresh_destination_proof_document()
        future = (NOW + timedelta(minutes=6)).isoformat()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        proof["observed_at"] = future
        for name in ("activation", "load", "destination", "reconciliation"):
            nested = proof[name]
            assert isinstance(nested, dict)
            nested["observed_at"] = future

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.stages[3].status, "unknown")
        self.assertEqual(projection.stages[4].status, "unknown")

    def test_destination_timestamp_without_timezone_is_rejected_safely(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        proof["observed_at"] = "2026-08-28T12:00:00"

        with self.assertRaises(ProjectionError) as captured:
            project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(captured.exception.code, "invalid_destination_proof")
        self.assertEqual(captured.exception.safe_message, "Preuve de destination invalide")

    def test_destination_counts_reject_boolean_negative_and_overflow(self) -> None:
        for invalid in (True, -1, 9_223_372_036_854_775_808):
            with self.subTest(invalid=invalid):
                document = fresh_destination_proof_document()
                proof = document["destination_proof"]
                assert isinstance(proof, dict)
                load = proof["load"]
                assert isinstance(load, dict)
                load["event_count"] = invalid

                with self.assertRaises(ProjectionError) as captured:
                    project_console_document(document, LIVE_SOURCE, now=NOW)

                self.assertEqual(captured.exception.code, "invalid_destination_proof")

    def test_matched_reconciliation_with_cross_receiver_window_is_an_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        reconciliation = proof["reconciliation"]
        assert isinstance(reconciliation, dict)
        window = reconciliation["window"]
        assert isinstance(window, dict)
        window["from_exclusive"] = {"receiver": "DEMOJRN3775", "sequence": 99}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {
                "code": "destination_reconciliation_inconsistent",
                "type": "destination",
            },
        )

    def test_succeeded_load_without_event_count_is_rejected(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        load = proof["load"]
        assert isinstance(load, dict)
        del load["event_count"]

        with self.assertRaises(ProjectionError) as captured:
            project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(captured.exception.code, "invalid_destination_proof")

    def test_failed_load_projects_a_safe_public_incident(self) -> None:
        document = fresh_destination_proof_document()
        proof = document["destination_proof"]
        assert isinstance(proof, dict)
        load = proof["load"]
        assert isinstance(load, dict)
        load["state"] = "failed"
        load["failed_event_count"] = 3
        load["incident_code"] = "destination_load_timeout"

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {"code": "destination_load_timeout", "type": "destination"},
        )
        self.assertNotIn("checkpoint", str(projection.incident))

    def test_only_allowlisted_public_counters_are_projected(self) -> None:
        document = fresh_running_document()
        document["counters"] = {
            "polls": {"value": 1},
            "events_published": {"value": 2},
            "payload_bytes_published": {"value": 3},
            "errors": {"value": 4},
            "empty_scans": {"value": 5},
            "idle_polls": {"value": 6},
            "windows_published": {"value": 7},
            "receiver_rotations": {"value": 8},
            "run_duration_s": {"value": 9.5},
            "cpu_ms_per_event": {"value": 10.5},
            "mean_mcpu": {"value": 11.5},
            "events_in_target": {"value": 12},
            "duplicates_in_target": {"value": 13},
            "password": {"value": 14},
            "raw_payload": {"value": 15},
            "secret_token": {"value": 16},
            "future_numeric_metric": {"value": 17},
        }

        counters = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()["counters"]

        self.assertEqual(
            set(counters),
            {
                "polls",
                "events_published",
                "payload_bytes_published",
                "errors",
                "empty_scans",
                "idle_polls",
                "windows_published",
                "receiver_rotations",
                "run_duration_s",
                "cpu_ms_per_event",
                "mean_mcpu",
                "events_in_target",
                "duplicates_in_target",
            },
        )
        self.assertNotIn("password", counters)
        self.assertNotIn("raw_payload", counters)
        self.assertNotIn("secret_token", counters)
        self.assertNotIn("future_numeric_metric", counters)

    def test_unknown_counter_values_are_not_parsed_or_rejected(self) -> None:
        document = fresh_running_document()
        document["counters"]["password"] = {"value": math.nan}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertNotIn("password", projection.counters)

    def test_fresh_capture_without_destination_is_degraded(self) -> None:
        projection = project_console_document(fresh_running_document(), LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "degraded")
        self.assertEqual(
            projection.quality,
            {"coverage": "partial", "freshness": "fresh", "evidence_kind": "live"},
        )
        self.assertEqual(
            projection.summary, "Capture saine, livraison Snowflake non prouvée"
        )
        self.assertEqual(projection.id, "dev-cntr")

    def test_stale_document_is_unknown_not_healthy(self) -> None:
        projection = project_console_document(stale_document(), LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.quality["freshness"], "stale")

    def test_future_generated_at_is_clock_untrusted_and_unknown(self) -> None:
        document = fresh_running_document()
        document["generated_at"] = (NOW + timedelta(minutes=6)).isoformat()

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.quality["freshness"], "clock_untrusted")
        self.assertEqual(projection.status, "unknown")

    def test_simulation_never_projects_healthy(self) -> None:
        projection = project_console_document(fresh_running_document(), SIM_SOURCE, now=NOW)

        self.assertNotEqual(projection.status, "healthy")
        self.assertEqual(projection.quality["evidence_kind"], "simulation")

    def test_historical_provenance_is_preserved(self) -> None:
        projection = project_console_document(
            fresh_running_document(),
            SourceDescriptor("soak-24h", "historical", "dev", "https://approved.example/snapshot"),
            now=NOW,
        )

        self.assertEqual(projection.quality["evidence_kind"], "historical")
        self.assertNotEqual(projection.status, "healthy")

    def test_missing_checkpoint_fails_closed_as_unknown(self) -> None:
        document = fresh_running_document()
        document["position"] = {"checkpoint": None, "source_tail": {"sequence": 42}}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.stages[0].status, "unknown")

    def test_stopped_fail_closed_projects_an_incident_without_error_content(self) -> None:
        document = fresh_running_document()
        document["run"] = {
            "state": "STOPPED_FAIL_CLOSED",
            "last_error": {
                "type": "snapshot-secret=not-for-output",
                "head": "password=not-for-output",
            },
        }

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")
        self.assertEqual(
            projection.incident,
            {"code": "capture_stopped_fail_closed", "type": "capture_stopped"},
        )
        self.assertNotIn("password", str(projection.to_dict()))
        self.assertNotIn("snapshot-secret", str(projection.to_dict()))

    def test_allowlisted_capture_errors_project_stable_public_types(self) -> None:
        for raw_type, public_type in (
            ("JdbcFailure", "capture_connection_failure"),
            ("SqlWindowTimeout", "capture_timeout"),
            ("ReceiverPlanningError", "capture_position_review"),
        ):
            document = fresh_running_document()
            document["run"] = {
                "state": "STOPPED_FAIL_CLOSED",
                "last_error": {"type": raw_type},
            }

            projection = project_console_document(document, LIVE_SOURCE, now=NOW)

            self.assertEqual(projection.to_dict()["incident"], {
                "code": "capture_stopped_fail_closed",
                "type": public_type,
            })

    def test_fresh_stopped_budget_projects_a_planned_stop(self) -> None:
        document = fresh_running_document()
        document["run"] = {"state": "STOPPED_BUDGET", "last_error": None}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "planned_stop")
        self.assertEqual(projection.summary, "Capture arrêtée comme prévu")

    def test_closed_capture_chain_is_not_pipeline_healthy(self) -> None:
        document = fresh_running_document()
        document['run'] = {'state': 'STOPPED_PROOF_CHAIN', 'last_error': None}
        projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(projection.status, 'planned_stop')

    def test_divergent_lag_projects_an_incident(self) -> None:
        document = fresh_running_document()
        document["lag"] = {"current": {"value": 30}, "verdict": {"value": "DIVERGING"}}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.status, "incident")

    def test_product_fixture_bounded_lag_keeps_zero_as_a_known_measurement(self) -> None:
        document = json.loads(Path("ui/fixtures/console-dev.json").read_text())
        generated_at = datetime.fromisoformat(document["generated_at"])

        projection = project_console_document(
            document,
            SIM_SOURCE,
            now=generated_at + timedelta(minutes=1),
        )

        self.assertEqual(projection.lag_sequences, 0)
        self.assertEqual(projection.status, "degraded")
        self.assertEqual(
            projection.summary,
            "Simulation : livraison Snowflake non prouvée",
        )

    def test_real_lag_series_is_projected_without_interpolation(self) -> None:
        document = fresh_running_document()
        document["lag"] = {
            "current": {"value": 8},
            "verdict": {"value": "STABLE"},
            "series": {
                "resolution_s": 5.0,
                "capacity": 240,
                "sample_count": 10,
                "unknown_sample_count": 2,
                "buckets": [
                    {
                        "start_s": 0.0,
                        "end_s": 4.0,
                        "min": 8,
                        "max": 12,
                        "last": 9,
                        "samples": 5,
                        "unknown_samples": 0,
                    },
                    {
                        "start_s": 5.0,
                        "end_s": 9.0,
                        "min": 7,
                        "max": 10,
                        "last": 8,
                        "samples": 5,
                        "unknown_samples": 2,
                    },
                ],
            },
        }

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertEqual(
            payload["lag_series"],
            {
                "resolution_s": 5.0,
                "sample_count": 10,
                "unknown_sample_count": 2,
                "buckets": [
                    {
                        "start_s": 0.0,
                        "end_s": 4.0,
                        "min": 8,
                        "max": 12,
                        "last": 9,
                        "samples": 5,
                        "unknown_samples": 0,
                        "coverage": "complete",
                        "kind": "observed",
                    },
                    {
                        "start_s": 5.0,
                        "end_s": 9.0,
                        "min": 7,
                        "max": 10,
                        "last": None,
                        "samples": 5,
                        "unknown_samples": 2,
                        "coverage": "gap",
                        "kind": "observed",
                    },
                ],
            },
        )

    def test_invalid_lag_series_sample_counts_fail_closed(self) -> None:
        document = fresh_running_document()
        document["lag"] = {
            "current": {"value": 1},
            "verdict": {"value": "STABLE"},
            "series": {
                "resolution_s": 5.0,
                "sample_count": 1,
                "unknown_sample_count": 2,
                "buckets": [
                    {
                        "start_s": 0.0,
                        "end_s": 1.0,
                        "min": None,
                        "max": None,
                        "last": None,
                        "samples": 1,
                        "unknown_samples": 2,
                    },
                ],
            },
        }

        with self.assertRaises(ProjectionError) as captured:
            project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(captured.exception.code, "invalid_lag_series")

    def test_temporal_discontinuity_becomes_an_explicit_metadata_gap(self) -> None:
        document = fresh_running_document()
        document["lag"] = {
            "current": {"value": 4},
            "verdict": {"value": "BOUNDED"},
            "series": {
                "resolution_s": 5.0,
                "sample_count": 10,
                "unknown_sample_count": 0,
                "buckets": [
                    {
                        "start_s": 0.0,
                        "end_s": 4.0,
                        "min": 8,
                        "max": 12,
                        "last": 9,
                        "samples": 5,
                        "unknown_samples": 0,
                    },
                    {
                        "start_s": 20.0,
                        "end_s": 24.0,
                        "min": 4,
                        "max": 6,
                        "last": 4,
                        "samples": 5,
                        "unknown_samples": 0,
                    },
                ],
            },
        }

        buckets = project_console_document(
            document, LIVE_SOURCE, now=NOW
        ).to_dict()["lag_series"]["buckets"]

        self.assertEqual(len(buckets), 3)
        self.assertEqual(
            buckets[1],
            {
                "start_s": 4.0,
                "end_s": 20.0,
                "min": None,
                "max": None,
                "last": None,
                "samples": 0,
                "unknown_samples": 0,
                "coverage": "gap",
                "kind": "temporal_gap",
            },
        )

    def test_merged_bucket_spillover_gap_starts_at_real_coverage_end(self) -> None:
        # Cas INT réel : un seau fusionné déborde sa cellule nominale
        # (end_s 338.22 > start_s 10 + résolution 320). Le trou déclaré doit
        # commencer à la fin réelle de la couverture, pas à la borne de
        # cellule — sinon il chevauche le seau précédent et la série est
        # invalide côté client.
        document = fresh_running_document()
        document["lag"] = {
            "current": {"value": 1},
            "verdict": {"value": "STABLE"},
            "series": {
                "resolution_s": 320.0,
                "capacity": 240,
                "sample_count": 360,
                "unknown_sample_count": 0,
                "buckets": [
                    {
                        "start_s": 10.0,
                        "end_s": 338.22,
                        "min": 1,
                        "max": 3,
                        "last": 1,
                        "samples": 178,
                        "unknown_samples": 0,
                    },
                    {
                        "start_s": 340.0,
                        "end_s": 659.41,
                        "min": 1,
                        "max": 2,
                        "last": 1,
                        "samples": 182,
                        "unknown_samples": 0,
                    },
                ],
            },
        }

        buckets = project_console_document(
            document, LIVE_SOURCE, now=NOW
        ).to_dict()["lag_series"]["buckets"]

        self.assertEqual(len(buckets), 3)
        self.assertEqual(buckets[0]["end_s"], 338.22)
        self.assertEqual(
            buckets[1],
            {
                "start_s": 338.22,
                "end_s": 340.0,
                "min": None,
                "max": None,
                "last": None,
                "samples": 0,
                "unknown_samples": 0,
                "coverage": "gap",
                "kind": "temporal_gap",
            },
        )
        # Aucun chevauchement : la série pave l'intervalle sans trou muet.
        for left, right in zip(buckets, buckets[1:]):
            self.assertLessEqual(left["end_s"], right["start_s"])

    def test_receiver_window_does_not_turn_an_unknown_lag_into_zero(self) -> None:
        document = fresh_running_document()
        document["position"] = {
            "checkpoint": {"receiver": "DEMOJRN3775", "sequence": 9},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 3},
            "receiver_first_sequence": 1,
            "receiver_last_sequence": 3,
        }
        document["lag"] = {"current": {"value": None, "unknown": "receivers disjoints"}, "verdict": {"value": None}}

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertIsNone(projection.lag_sequences)
        self.assertEqual(projection.status, "unknown")
        self.assertEqual(projection.stages[0].status, "unknown")
        self.assertEqual(projection.stages[1].status, "healthy")

    def test_boolean_or_reversed_sequences_make_source_unknown(self) -> None:
        document = fresh_running_document()
        document["position"] = {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": True},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 3},
        }

        boolean_projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(boolean_projection.status, "unknown")
        self.assertEqual(boolean_projection.stages[0].status, "unknown")

        document["position"] = {
            "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 10},
            "source_tail": {"receiver": "DEMOJRN3776", "sequence": 3},
        }
        reversed_projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(reversed_projection.status, "unknown")
        self.assertEqual(reversed_projection.stages[0].status, "unknown")

    def test_unknown_or_contradictory_lag_is_unknown_not_incident(self) -> None:
        document = fresh_running_document()
        document["lag"] = {"current": {"value": None}, "verdict": {"value": "STABLE"}}

        unknown_projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(unknown_projection.status, "unknown")
        self.assertEqual(unknown_projection.stages[0].status, "healthy")

        document["lag"] = {"current": {"value": 0}, "verdict": {"value": "DIVERGING"}}
        contradictory_projection = project_console_document(document, LIVE_SOURCE, now=NOW)
        self.assertEqual(contradictory_projection.status, "unknown")
        self.assertEqual(contradictory_projection.stages[0].status, "healthy")

    def test_runtime_descriptors_revision_and_numbers_are_validated_safely(self) -> None:
        for invalid_source in (
            ("", "live", "dev", "file:///snapshot.json"),
            ("dev-cntr", "preview", "dev", "file:///snapshot.json"),
            ("dev-cntr", "live", "", "file:///snapshot.json"),
            ("dev-cntr", "live", "dev", ""),
            ("dev-cntr", "live", "dev", {"origin-secret": "never-output"}),
        ):
            with self.assertRaises(ProjectionError) as captured:
                SourceDescriptor(*invalid_source)  # type: ignore[arg-type]
            self.assertNotIn("preview", captured.exception.safe_message)
            self.assertNotIn("origin-secret", captured.exception.safe_message)

        with self.assertRaises(ProjectionError) as origin_error:
            SourceDescriptor(
                "dev-cntr", "live", "dev", {"origin-secret": "never-output"}
            )  # type: ignore[arg-type]
        self.assertNotIn("origin-secret", str(origin_error.exception))

        pipeline = project_console_document(fresh_running_document(), LIVE_SOURCE, now=NOW)
        for revision in (True, -1, 1.2, "1"):
            with self.assertRaises(ProjectionError) as captured:
                build_overview((pipeline,), revision=revision, generated_at=NOW)  # type: ignore[arg-type]
            self.assertEqual(captured.exception.code, "invalid_revision")

        for value in (math.nan, math.inf, True, "120"):
            document = fresh_running_document()
            document["counters"] = {"events_published": {"value": value}}
            with self.assertRaises(ProjectionError) as captured:
                project_console_document(document, LIVE_SOURCE, now=NOW)
            self.assertEqual(captured.exception.code, "invalid_number")

    def test_clock_untrusted_summary_is_not_stale_copy(self) -> None:
        document = fresh_running_document()
        document["generated_at"] = (NOW + timedelta(minutes=6)).isoformat()

        projection = project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(projection.summary, "Horloge de capture non fiable")

    def test_incompatible_or_invalid_timestamp_is_a_safe_projection_error(self) -> None:
        document = fresh_running_document()
        document["generated_at"] = "raw-payload=never-disclose"

        with self.assertRaises(ProjectionError) as captured:
            project_console_document(document, LIVE_SOURCE, now=NOW)

        self.assertEqual(captured.exception.code, "invalid_generated_at")
        self.assertNotIn("raw-payload", captured.exception.safe_message)

        with self.assertRaises(ProjectionError) as incompatible:
            project_console_document({"format_version": "other-v9"}, LIVE_SOURCE, now=NOW)

        self.assertEqual(incompatible.exception.code, "incompatible_format")
        self.assertNotIn("other-v9", incompatible.exception.safe_message)

    def test_overview_rejects_duplicate_pipeline_ids(self) -> None:
        pipeline = project_console_document(fresh_running_document(), LIVE_SOURCE, now=NOW)

        with self.assertRaises(ProjectionError) as captured:
            build_overview((pipeline, deepcopy(pipeline)), revision=2, generated_at=NOW)

        self.assertEqual(captured.exception.code, "duplicate_pipeline_id")

    def test_pass_through_projects_position_flux_run_and_lag_verdict(self) -> None:
        document = fresh_running_document()
        document["flux"] = {
            "id": "as400/sales/sale",
            "label": "SALE — lecture par RetrieveJournal",
            "journal": "DEMOJRN",
            "journal_library": "DEMOLIB",
            "objects": ["SALES.SALE"],
            "reader_path": "RetrieveJournal",
            "target": "raw S3 — aucune cible déclarée",
            "job": "as400-capture",
        }
        document["position"]["receiver_first_sequence"] = 78975323
        document["position"]["receiver_last_sequence"] = 83994095
        document["run"] = {
            "state": "RUNNING",
            "started_at": (NOW - timedelta(minutes=3)).isoformat(),
            "elapsed_s": 180.5,
            "stopped_because": None,
            "last_error": None,
            "source_pause": None,
        }
        document["lag"]["verdict"] = {"value": "BOUNDED"}

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertEqual(
            payload["position"],
            {
                "checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
                "source_tail": {"receiver": "DEMOJRN3776", "sequence": 42},
                "receiver_first_sequence": 78975323,
                "receiver_last_sequence": 83994095,
            },
        )
        self.assertEqual(
            payload["flux"],
            {
                "id": "as400/sales/sale",
                "label": "SALE — lecture par RetrieveJournal",
                "journal": "DEMOJRN",
                "journal_library": "DEMOLIB",
                "objects": ["SALES.SALE"],
                "reader_path": "RetrieveJournal",
                "target": "raw S3 — aucune cible déclarée",
                "job": "as400-capture",
            },
        )
        self.assertEqual(
            payload["run"],
            {
                "state": "RUNNING",
                "started_at": (NOW - timedelta(minutes=3)).isoformat(),
                "elapsed_s": 180.5,
                "stopped_because": None,
                "diagnostic": None,
                "source_pause": None,
            },
        )
        self.assertEqual(payload["lag_verdict"], "BOUNDED")
        self.assertIsNone(payload["lag_verdict_reason"])
        self.assertEqual(
            [stage["id"] for stage in payload["stages"]],
            ["source", "capture", "raw", "load", "destination"],
        )

    def test_pass_through_preserves_unobserved_nulls_without_inventing(self) -> None:
        document = fresh_running_document()
        document["position"] = {"checkpoint": None, "source_tail": None}
        document["lag"]["verdict"] = {
            "value": None,
            "unknown": "moins de deux échantillons connus",
        }

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertEqual(
            payload["position"],
            {
                "checkpoint": None,
                "source_tail": None,
                "receiver_first_sequence": None,
                "receiver_last_sequence": None,
            },
        )
        self.assertIsNone(payload["lag_verdict"])
        self.assertEqual(
            payload["lag_verdict_reason"], "moins de deux échantillons connus"
        )

    def test_pass_through_null_flux_members_and_malformed_objects(self) -> None:
        document = fresh_running_document()
        document["flux"] = {"id": "pays", "objects": ["SALES.SALE", {"raw": 1}]}

        flux = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()["flux"]

        self.assertEqual(flux["id"], "pays")
        self.assertIsNone(flux["objects"])
        for name in ("label", "journal", "journal_library", "reader_path", "target", "job"):
            self.assertIsNone(flux[name], name)

        document["flux"]["objects"] = "SALES.SALE"
        flux = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()["flux"]
        self.assertIsNone(flux["objects"])

    def test_stopped_run_projects_because_and_allowlisted_error_head(self) -> None:
        document = fresh_running_document()
        error_at = (NOW - timedelta(seconds=5)).isoformat()
        document["run"] = {
            "state": "STOPPED_FAIL_CLOSED",
            "stopped_because": "CaptureCircuitOpenError",
            "last_error": {
                "type": "ReceiverPlanningError",
                "head": "receiver sequence gap requires operator review",
                "at": error_at,
            },
        }

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertEqual(payload["run"]["state"], "STOPPED_FAIL_CLOSED")
        self.assertEqual(payload["run"]["stopped_because"], "CaptureCircuitOpenError")
        self.assertEqual(
            payload["run"]["diagnostic"],
            {
                "type": "ReceiverPlanningError",
                "head": "receiver sequence gap requires operator review",
                "at": error_at,
            },
        )

    def test_pass_through_never_reflects_secret_like_run_content(self) -> None:
        document = fresh_running_document()
        document["run"] = {
            "state": "password=never-output",
            "stopped_because": "token=never-output",
            "last_error": {
                "type": "NotAllowlistedError",
                "head": "password=never-output",
                "at": "not-a-time",
            },
        }

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertIsNone(payload["run"]["state"])
        self.assertIsNone(payload["run"]["stopped_because"])
        self.assertEqual(
            payload["run"]["diagnostic"], {"type": None, "head": None, "at": None}
        )
        self.assertNotIn("never-output", json.dumps(payload))

        document["run"]["source_pause"] = {"retry_after": "x", "reason_code": "secret=never-output"}
        paused = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()
        self.assertEqual(
            paused["run"]["source_pause"], {"retry_after": None, "reason_code": None}
        )
        self.assertNotIn("never-output", json.dumps(paused))

    def test_attached_destination_block_is_projected_verbatim(self) -> None:
        document = fresh_running_document()
        observed = (NOW - timedelta(seconds=30)).isoformat()
        document["destination"] = {
            "kind": "snowflake",
            "database": "DB_RD",
            "schema": "CDC",
            "stage": "RAW_STG",
            "raw_table": "SALE_RAW",
            "canonical_table": "SALE_LEDGER",
            "run_tag": "RUN_2026_09_21",
            "observed_at": observed,
            "load_checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "apply_checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
            "source_events": 120,
            "raw_rows": 120,
            "canonical_rows": 120,
            "duplicates": 0,
        }

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertEqual(
            payload["destination"],
            {
                "kind": "snowflake",
                "database": "DB_RD",
                "schema": "CDC",
                "stage": "RAW_STG",
                "raw_table": "SALE_RAW",
                "canonical_table": "SALE_LEDGER",
                "run_tag": "RUN_2026_09_21",
                "observed_at": observed,
                "load_checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
                "apply_checkpoint": {"receiver": "DEMOJRN3776", "sequence": 41},
                "source_events": 120,
                "raw_rows": 120,
                "canonical_rows": 120,
                "duplicates": 0,
            },
        )
        self.assertIsNone(payload["destination_reason"])

    def test_missing_destination_block_is_null_with_reason(self) -> None:
        payload = project_console_document(
            fresh_running_document(), LIVE_SOURCE, now=NOW
        ).to_dict()

        self.assertIsNone(payload["destination"])
        self.assertEqual(payload["destination_reason"], "destination_not_attached")

    def test_malformed_destination_block_is_null_with_reason_without_reflection(self) -> None:
        document = fresh_running_document()
        document["destination"] = "raw-payload=never-reflect"

        payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()

        self.assertIsNone(payload["destination"])
        self.assertEqual(payload["destination_reason"], "destination_invalid")
        self.assertNotIn("never-reflect", json.dumps(payload))

    def test_invalid_destination_members_fall_back_to_null_individually(self) -> None:
        document = fresh_running_document()
        document["destination"] = {
            "kind": "snowflake",
            "run_tag": "bad tag!",
            "observed_at": "not-a-time",
            "load_checkpoint": {"receiver": "DEMOJRN3776", "sequence": True},
            "raw_rows": -4,
            "duplicates": 9_223_372_036_854_775_808,
        }

        destination = project_console_document(
            document, LIVE_SOURCE, now=NOW
        ).to_dict()["destination"]

        self.assertEqual(destination["kind"], "snowflake")
        self.assertIsNone(destination["run_tag"])
        self.assertIsNone(destination["observed_at"])
        self.assertIsNone(destination["load_checkpoint"])
        self.assertIsNone(destination["apply_checkpoint"])
        self.assertIsNone(destination["raw_rows"])
        self.assertIsNone(destination["duplicates"])

    def test_new_top_level_fields_are_always_emitted(self) -> None:
        for document in (fresh_running_document(), fresh_destination_proof_document()):
            payload = project_console_document(document, LIVE_SOURCE, now=NOW).to_dict()
            for name in (
                "position",
                "flux",
                "run",
                "lag_verdict",
                "lag_verdict_reason",
                "destination",
                "destination_reason",
            ):
                self.assertIn(name, payload, name)


if __name__ == "__main__":
    unittest.main()
