from __future__ import annotations

import site_fixture

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from quadringent.destination_proof import attach_snowflake_proof
from quadringent_control_plane.model import SourceDescriptor
from quadringent_control_plane.projection import project_console_document
from snowflake_external_replay import _write_destination_snapshot


SITE = site_fixture.build_test_site()

OBSERVED = datetime(2026, 8, 31, 8, 45, tzinfo=timezone.utc)


def capture_document() -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3848", "sequence": 76071470}
        },
        "counters": {"events_published": {"value": 7}},
    }


def projectable_capture_document(*, state: str = "RUNNING") -> dict[str, object]:
    return {
        "format_version": "as400-console-v1",
        "generated_at": "2026-08-31T08:40:00+00:00",
        "flux": {"id": "as400/sales/sale", "label": "SALE"},
        "run": {"state": state, "last_error": None},
        "position": {
            "checkpoint": {"receiver": "DEMOJRN3848", "sequence": 76071470},
            "source_tail": {"receiver": "DEMOJRN3848", "sequence": 76071471},
        },
        "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
        "counters": {
            "events_published": {"value": 7},
            "windows_published": {"value": 2},
            "errors": {"value": 0},
        },
    }


def replay_metrics(**overrides: object) -> dict[str, object]:
    metrics: dict[str, object] = {
        "status": "PASS",
        "database": SITE.snowflake_scope.database,
        "schema": SITE.snowflake_scope.schema,
        "stage": f"{SITE.destination_schema}_EXTERNAL_STAGE",
        "raw_table": f"{SITE.destination_schema}_EXT_RAW_E2E20260831",
        "canonical_table": f"{SITE.destination_schema}_EXT_CANONICAL_E2E20260831",
        "raw_rows_after_second": 7,
        "distinct_event_ids_after_second": 7,
        "canonical_rows_after_second": 7,
    }
    metrics.update(overrides)
    return metrics


class DestinationProofTests(unittest.TestCase):
    def test_replay_cli_supports_a_standard_snowflake_connection_name(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/snowflake_external_replay.py", "--help"],
            env={**os.environ, "PYTHONPATH": "src"},
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--connection-name", result.stdout)
        self.assertIn("--all-jsonl", result.stdout)

    def test_replay_helper_writes_the_combined_snapshot_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture_path = Path(directory) / "capture.json"
            output_path = Path(directory) / "combined.json"
            capture_path.write_text(json.dumps(capture_document()), encoding="utf-8")

            _write_destination_snapshot(
                capture_path=capture_path,
                output_path=output_path,
                metrics=replay_metrics(),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
            )

            combined = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(combined["destination"]["schema"], SITE.snowflake_scope.schema)
        self.assertEqual(combined["destination"]["canonical_rows"], 7)

    def test_attach_proof_is_immutable_bounded_and_reconciled(self) -> None:
        capture = capture_document()

        combined = attach_snowflake_proof(
            capture,
            replay_metrics(),
            run_tag="E2E20260831",
            observed_at=OBSERVED,
        site=SITE)

        self.assertNotIn("destination", capture)
        self.assertEqual(combined["destination"], {
            "kind": "snowflake",
            "database": SITE.snowflake_scope.database,
            "schema": SITE.snowflake_scope.schema,
            "stage": f"{SITE.destination_schema}_EXTERNAL_STAGE",
            "raw_table": f"{SITE.destination_schema}_EXT_RAW_E2E20260831",
            "canonical_table": f"{SITE.destination_schema}_EXT_CANONICAL_E2E20260831",
            "run_tag": "E2E20260831",
            "observed_at": "2026-08-31T08:45:00+00:00",
            "load_checkpoint": {
                "receiver": "DEMOJRN3848",
                "sequence": 76071470,
            },
            "apply_checkpoint": {
                "receiver": "DEMOJRN3848",
                "sequence": 76071470,
            },
            "source_events": 7,
            "raw_rows": 7,
            "canonical_rows": 7,
            "duplicates": 0,
        })

    def test_attach_proof_rejects_untrusted_or_unreconciled_metrics(self) -> None:
        invalid_metrics = (
            replay_metrics(schema="POPSINK_ALPHA"),
            replay_metrics(status="FAIL"),
            replay_metrics(raw_rows_after_second=8),
            replay_metrics(distinct_event_ids_after_second=6),
            replay_metrics(canonical_rows_after_second=6),
        )

        for metrics in invalid_metrics:
            with self.assertRaises(ValueError):
                attach_snowflake_proof(
                    capture_document(),
                    metrics,
                    run_tag="E2E20260831",
                    observed_at=OBSERVED,
                site=SITE)

    def test_attach_proof_rejects_missing_checkpoint_and_unsafe_run_tag(self) -> None:
        capture = capture_document()
        capture["position"] = {"checkpoint": None}

        with self.assertRaises(ValueError):
            attach_snowflake_proof(
                capture,
                replay_metrics(),
                run_tag="E2E20260831",
                observed_at=OBSERVED,
            site=SITE)
        with self.assertRaises(ValueError):
            attach_snowflake_proof(
                capture_document(),
                replay_metrics(),
                run_tag="unsafe/tag",
                observed_at=OBSERVED,
            site=SITE)

    def test_attach_proof_emits_destination_proof_v1(self) -> None:
        combined = attach_snowflake_proof(
            capture_document(),
            replay_metrics(),
            run_tag="E2E20260831",
            observed_at=OBSERVED,
        site=SITE)

        proof = combined["destination_proof"]
        self.assertEqual(combined["generated_at"], "2026-08-31T08:45:00+00:00")
        self.assertEqual(proof["schema_version"], "destination-proof-v1")
        self.assertEqual(proof["target"], {
            "kind": "snowflake",
            "destination_id": SITE.destination_id,
            "environment": SITE.environment,
        })
        self.assertEqual(proof["activation"]["state"], "active")
        self.assertIsNone(proof["activation"]["blocker_code"])
        self.assertEqual(proof["load"]["event_count"], 7)
        self.assertEqual(proof["load"]["batch_count"], 1)
        self.assertEqual(proof["reconciliation"]["state"], "matched")
        self.assertIsNone(proof["reconciliation"]["window"]["from_exclusive"])
        self.assertEqual(
            proof["reconciliation"]["window"]["to_inclusive"],
            {"receiver": "DEMOJRN3848", "sequence": 76071470},
        )

    def test_reverification_preserves_original_capture_age(self) -> None:
        first = attach_snowflake_proof(projectable_capture_document(), replay_metrics(),
                                      run_tag="FIRST", observed_at=OBSERVED, site=SITE)
        second = attach_snowflake_proof(first, replay_metrics(),
                                       run_tag="SECOND", observed_at=OBSERVED + timedelta(hours=1), site=SITE)
        self.assertEqual(second.get("capture_observed_at"), "2026-08-31T08:40:00+00:00")
        projection = project_console_document(second,
            SourceDescriptor("dev-sale", "live", SITE.environment, "file:///combined.json"),
            now=OBSERVED + timedelta(hours=1))
        self.assertEqual(projection.quality["freshness"], "stale")
        self.assertNotEqual(projection.status, "healthy")

    def test_attached_running_proof_projects_e2e_healthy(self) -> None:
        capture = projectable_capture_document(state="RUNNING")
        capture["generated_at"] = "2026-08-31T08:44:00+00:00"
        combined = attach_snowflake_proof(
            capture,
            replay_metrics(),
            run_tag="E2E20260831",
            observed_at=OBSERVED,
        site=SITE)

        projection = project_console_document(
            combined,
            SourceDescriptor("dev-sale", "live", SITE.environment, "file:///combined.json"),
            now=OBSERVED + timedelta(seconds=20),
        )

        self.assertEqual(
            [stage.id for stage in projection.stages],
            ["source", "capture", "raw", "load", "destination"],
        )
        self.assertEqual(
            [stage.status for stage in projection.stages],
            ["healthy", "healthy", "healthy", "healthy", "healthy"],
        )
        self.assertEqual(projection.status, "healthy")
        self.assertEqual(projection.quality["coverage"], "complete")
        self.assertEqual(projection.quality["freshness"], "fresh")
        self.assertEqual(projection.quality["evidence_kind"], "live")

    def test_attached_budget_stop_does_not_claim_live_healthy(self) -> None:
        capture = projectable_capture_document(state="STOPPED_BUDGET")
        capture["generated_at"] = "2026-08-31T08:44:00+00:00"
        combined = attach_snowflake_proof(
            capture,
            replay_metrics(),
            run_tag="E2E20260831",
            observed_at=OBSERVED,
        site=SITE)

        projection = project_console_document(
            combined,
            SourceDescriptor("dev-sale", "live", SITE.environment, "file:///combined.json"),
            now=OBSERVED + timedelta(seconds=20),
        )

        self.assertEqual(projection.stages[1].status, "planned_stop")
        self.assertEqual(projection.stages[3].status, "healthy")
        self.assertEqual(projection.stages[4].status, "healthy")
        self.assertEqual(projection.status, "planned_stop")
        self.assertNotEqual(projection.status, "healthy")


if __name__ == "__main__":
    unittest.main()
