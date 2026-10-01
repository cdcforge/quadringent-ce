from __future__ import annotations

import site_fixture

import unittest
import os
from pathlib import Path
import subprocess
import sys
import json

from quadringent.fault_matrix import run_fault_matrix


SITE = site_fixture.build_test_site()

class FaultMatrixTests(unittest.TestCase):
    def test_la_commande_documentee_ne_depend_pas_de_pythonpath(self) -> None:
        root = Path(__file__).resolve().parents[1]
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        result = subprocess.run([sys.executable, str(root / "scripts/raw_checkpoint_fault_matrix.py")],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")

    def test_all_raw_checkpoint_failure_boundaries_recover_idempotently(self) -> None:
        result = run_fault_matrix(site=SITE)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            [item["stage"] for item in result["stages"]],
            ["payload", "manifest", "checkpoint"],
        )
        for item in result["stages"]:
            self.assertTrue(item["failure_observed"])
            self.assertTrue(item["recovered"])
            self.assertEqual(item["replayed_event_count"], 1)
            self.assertIsNone(item["checkpoint_after_fault"])

    def test_output_contains_only_safe_counters_and_positions(self) -> None:
        result = run_fault_matrix(site=SITE)

        for item in result["stages"]:
            self.assertNotIn("payload", item)
            self.assertNotIn("manifest", item)
            self.assertNotIn("event_value", item)
            self.assertEqual(set(item), {
                "stage",
                "failure_observed",
                "raw_objects_after_fault",
                "checkpoint_after_fault",
                "recovered",
                "replayed_event_count",
                "retry_payload_created",
                "retry_manifest_created",
                "checkpoint_after_recovery",
            })


if __name__ == "__main__":
    unittest.main()
