"""Le Job de flotte doit refuser une configuration incomplète avant toute I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT = "scripts/as400_continuous_capture.py"

PROOF_ENVIRONMENT = {
    "AS400_RAW_BUCKET": "example-corp-000000000000-int-example-corp-raw",
    "ISERIES_HOST": "192.0.2.10",
    "ISERIES_USER": "CDCUSER",
    "AS400_CHECKPOINT_TABLE": "example-corp-int-example-corp-checkpoints",
    "ISERIES_SCHEMA": "SALES",
    "ISERIES_TABLE": "SALE",
    "AS400_JOURNAL_NAME": "DEMOJRN",
    "AS400_RAW_PREFIX": "as400/sales/sale/runs/abcdefghijkl",
    "AS400_STREAM_KEY": "sales.sale",
}


class ContinuousFleetEntrypointTests(unittest.TestCase):
    def run_capture(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": "src", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if environment:
            env.update(environment)
        return subprocess.run(
            [sys.executable, SCRIPT, *arguments],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def failure(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertNotEqual(result.returncode, 0)
        # Une seule ligne de journal : aucun appel cloud n'a été tenté avant.
        self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
        return json.loads(result.stderr.strip())

    def test_fleet_root_without_tables_is_refused_before_io(self) -> None:
        result = self.run_capture(environment={"AS400_FLEET_TABLE_ROOT": "as400/sales"})

        payload = self.failure(result)
        self.assertEqual(payload["error_type"], "FleetConfigurationError")
        self.assertIn("supplied together", str(payload["message"]))

    def test_fleet_tables_without_root_is_refused_before_io(self) -> None:
        result = self.run_capture(environment={"AS400_FLEET_TABLES": "CNTR,SALE"})

        payload = self.failure(result)
        self.assertEqual(payload["error_type"], "FleetConfigurationError")

    def test_a_single_fleet_table_is_refused(self) -> None:
        result = self.run_capture(
            environment={
                "AS400_FLEET_TABLE_ROOT": "as400/sales",
                "AS400_FLEET_TABLES": "CNTR",
            }
        )

        payload = self.failure(result)
        self.assertIn("at least two tables", str(payload["message"]))

    def test_fleet_capture_refuses_a_single_table_proof_window(self) -> None:
        environment = dict(PROOF_ENVIRONMENT)
        environment.update(
            {
                "AS400_FLEET_TABLE_ROOT": "as400/sales",
                "AS400_FLEET_TABLES": "CNTR,SALE",
            }
        )
        result = self.run_capture("--proof-window-id", "w1", environment=environment)

        payload = self.failure(result)
        self.assertEqual(payload["error_type"], "FleetConfigurationError")
        self.assertIn("no single-table proof window", str(payload["message"]))

    def test_fleet_capture_refuses_a_table_outside_the_declared_fleet(self) -> None:
        result = self.run_capture(
            environment={
                "AS400_FLEET_TABLE_ROOT": "as400/sales",
                "AS400_FLEET_TABLES": "CNTR,SALE",
                "ISERIES_TABLE": "ORDER",
            }
        )

        payload = self.failure(result)
        self.assertIn("must belong to the fleet tables", str(payload["message"]))

    def test_an_unrelated_configuration_error_stays_message_free(self) -> None:
        """Une erreur qui ne vient pas du dépôt ne doit rien journaliser."""

        result = self.run_capture(environment={"AS400_READER_TIMEOUT_SECONDS": "1"})

        payload = self.failure(result)
        self.assertEqual(payload["error_type"], "ValueError")
        self.assertNotIn("message", payload)

    def test_the_entrypoint_still_exposes_the_single_stream_options(self) -> None:
        result = self.run_capture("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--max-consecutive-errors", result.stdout)
        self.assertIn("--proof-window-id", result.stdout)

    def test_the_fleet_module_is_declared_in_the_runtime_image(self) -> None:
        modules = Path("docker/runtime-modules.txt").read_text(encoding="utf-8").split()
        self.assertIn("src/quadringent/fleet_capture.py", modules)
        self.assertIn(
            "COPY src/quadringent/fleet_capture.py /app/quadringent/",
            Path("docker/Dockerfile").read_text(encoding="utf-8"),
        )
