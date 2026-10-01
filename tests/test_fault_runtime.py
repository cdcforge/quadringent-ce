from __future__ import annotations

import site_fixture

import os
import io
import json
import runpy
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.fault_runtime import (
    FRONTIERS,
    inspect_frontier,
    recover,
    run_fault_runtime,
)
from quadringent.object_store import FileObjectStore
from quadringent.raw import RawBatchWriter
from test_raw import event


SITE = site_fixture.build_test_site()

POC_ROOT = Path(__file__).resolve().parents[1]


class FaultRuntimeTests(unittest.TestCase):
    def test_fault_scope_requires_explicit_isolated_dev_targets(self) -> None:
        from quadringent import fault_runtime
        scope = {
            "AS400_FAULT_CONFIRM": SITE.fault_confirmation_token,
            "AS400_RAW_BUCKET": SITE.raw_bucket,
            "AS400_CHECKPOINT_TABLE": SITE.checkpoint_table,
            "AS400_FAULT_RUN_ID": "probe-0909",
            "AS400_RAW_PREFIX": f"{SITE.stream_prefix}/faults/probe-0909",
            "AS400_STREAM_KEY": f"{SITE.stream_prefix}/faults/probe-0909",
            "ISERIES_SCHEMA": SITE.source_schema, "ISERIES_TABLE": SITE.proof_table,
            "AWS_DEFAULT_REGION": SITE.aws_region,
        }
        with patch.dict(os.environ, scope, clear=True):
            fault_runtime.validate_fault_scope(site=SITE)
        for key, bad in (("AS400_FAULT_CONFIRM", ""), ("AS400_RAW_BUCKET", "prod"),
                         ("AS400_CHECKPOINT_TABLE", "shared"), ("AS400_RAW_PREFIX", SITE.stream_prefix),
                         ("AS400_STREAM_KEY", "shared"), ("AS400_FAULT_RUN_ID", "../other"),
                         ("ISERIES_SCHEMA", "FORBIDDEN"), ("ISERIES_TABLE", "CNTR"),
                         ("AWS_DEFAULT_REGION", "eu-west-1"), ("AWS_REGION", "eu-west-1")):
            with self.subTest(key=key), patch.dict(os.environ, {**scope, key: bad}, clear=True):
                with self.assertRaises(ValueError):
                    fault_runtime.validate_fault_scope(site=SITE)
        for frontier in FRONTIERS:
            with patch.dict(os.environ, {**scope,
                    "AS400_RAW_PREFIX": scope["AS400_RAW_PREFIX"] + "/" + frontier.replace("_", "-"),
                    "AS400_STREAM_KEY": scope["AS400_STREAM_KEY"] + "/" + frontier}, clear=True):
                fault_runtime.validate_fault_scope(child=True, site=SITE)
                with self.assertRaises(ValueError):
                    fault_runtime.validate_fault_scope(site=SITE)

    def test_live_fault_scope_rejected_before_decode_or_store_creation(self) -> None:
        script = runpy.run_path(str(POC_ROOT / "scripts/raw_checkpoint_fault_runtime.py"))
        def unexpected_io(*args, **kwargs):
            raise AssertionError("external I/O reached before scope rejection")
        with patch.dict(os.environ, {"AS400_RAW_BUCKET": "production"}, clear=True), \
             patch.dict(script["run_live"].__globals__, {"_decode_window": unexpected_io}):
            with self.assertRaises(ValueError):
                script["run_live"](site=SITE)
        from quadringent import fault_runtime
        with patch.dict(os.environ, {"AS400_RAW_BUCKET": "production"}, clear=True), \
             patch.object(fault_runtime, "S3ObjectStore", unexpected_io):
            with self.assertRaises(ValueError):
                fault_runtime.store_from_env(site=SITE)

    def test_fault_entrypoints_fail_closed_on_nonpassing_reports(self) -> None:
        from quadringent import fault_runtime

        script_main = runpy.run_path(str(POC_ROOT / "scripts/raw_checkpoint_fault_runtime.py"))["main"]
        for status, expected in (("PASS", 0), ("FAIL", 1), ("unobserved", 1), (None, 1)):
            for target, live in ((fault_runtime.main, False), (script_main, False), (script_main, True)):
                with self.subTest(status=status, entrypoint=target.__module__, live=live):
                    report = {"status": status, "cases": []}
                    output = io.StringIO()
                    with patch.dict(os.environ, {**site_fixture.TEST_SITE_ENV,
                                                 "AS400_RAW_BUCKET": "test-only" if live else ""}, clear=True), \
                         patch.object(sys, "argv", ["fault-test"]), \
                         patch.dict(target.__globals__, {"run_fault_runtime": lambda **_: report, "run_live": lambda **_: report}), \
                         redirect_stdout(output):
                        self.assertEqual(target(), expected)
                    self.assertEqual(json.loads(output.getvalue()), report)

    def test_inprocess_frontiers_recover_without_loss_or_checkpoint_lead(self) -> None:
        result = run_fault_runtime(site=SITE)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual([item["frontier"] for item in result["cases"]], list(FRONTIERS))
        for item in result["cases"]:
            self.assertTrue(item["failure_observed"])
            self.assertTrue(item["recovered"])
            self.assertEqual(item["loss"], 0)
            self.assertEqual(item["extra"], 0)
            self.assertEqual(item["collisions"], 0)
            self.assertFalse(item["checkpoint_ahead_of_raw_after_fault"])
            self.assertFalse(item["checkpoint_ahead_of_raw_after_recovery"])
            self.assertIsNone(item["checkpoint_after_fault"])
            self.assertEqual(item["replayed_event_count"], 1)

        by_frontier = {item["frontier"]: item for item in result["cases"]}
        self.assertEqual(by_frontier["after_read"]["raw_objects_after_fault"], 0)
        self.assertFalse(by_frontier["after_read"]["payload_after_fault"])
        self.assertTrue(by_frontier["after_read"]["retry_payload_created"])
        self.assertEqual(by_frontier["after_payload"]["raw_objects_after_fault"], 1)
        self.assertTrue(by_frontier["after_payload"]["payload_after_fault"])
        self.assertFalse(by_frontier["after_payload"]["manifest_after_fault"])
        self.assertFalse(by_frontier["after_payload"]["retry_payload_created"])
        self.assertTrue(by_frontier["after_payload"]["retry_manifest_created"])
        self.assertEqual(by_frontier["after_manifest"]["raw_objects_after_fault"], 2)
        self.assertTrue(by_frontier["after_manifest"]["manifest_after_fault"])
        self.assertFalse(by_frontier["after_manifest"]["retry_payload_created"])
        self.assertFalse(by_frontier["after_manifest"]["retry_manifest_created"])

    def test_output_contains_only_safe_counters_and_positions(self) -> None:
        result = run_fault_runtime(site=SITE)
        allowed = {
            "frontier",
            "failure_observed",
            "payload_after_fault",
            "manifest_after_fault",
            "checkpoint_after_fault",
            "checkpoint_ahead_of_raw_after_fault",
            "raw_objects_after_fault",
            "recovered",
            "loss",
            "extra",
            "collisions",
            "replayed_event_count",
            "retry_payload_created",
            "retry_manifest_created",
            "checkpoint_after_recovery",
            "checkpoint_ahead_of_raw_after_recovery",
            "raw_objects_after_recovery",
        }
        for item in result["cases"]:
            self.assertEqual(set(item), allowed)
            self.assertNotIn("payload", item)
            self.assertNotIn("event_value", item)

    def test_sigkill_child_at_each_frontier_then_recovers(self) -> None:
        watermark = JournalPosition("DEMOJRN3677", 101)
        events = [event(100), event(101)]
        pythonpath = os.pathsep.join(
            part for part in (str(POC_ROOT / "src"), str(POC_ROOT), os.environ.get("PYTHONPATH", "")) if part
        )
        for frontier in FRONTIERS:
            with self.subTest(frontier=frontier):
                with tempfile.TemporaryDirectory(prefix="as400-fault-kill-") as directory:
                    root = Path(directory)
                    staged = RawBatchWriter(root / "stage").write_batch(
                        events,
                        high_watermark=watermark,
                    )
                    payload_key = f"batch-{staged.batch_id}.jsonl"
                    manifest_key = f"batch-{staged.batch_id}.manifest.json"
                    payload_path = root / "stage" / payload_key
                    manifest_path = root / "stage" / manifest_key
                    env = os.environ.copy()
                    # This test must stay on FileObjectStore even on a capture host.
                    env.pop("AS400_RAW_BUCKET", None)
                    env.update(
                        {
                            "PYTHONPATH": pythonpath,
                            "PYTHONDONTWRITEBYTECODE": "1",
                            "AS400_FAULT_CHILD": "1",
                            "AS400_CRASH_AFTER": frontier,
                            "AS400_CRASH_MODE": "kill",
                            "AS400_FAULT_ROOT": str(root),
                            "AS400_FAULT_PAYLOAD": str(payload_path),
                            "AS400_FAULT_MANIFEST": str(manifest_path),
                        }
                    )
                    completed = subprocess.run(
                        [sys.executable, "-m", "quadringent.fault_runtime", "--child"],
                        cwd=str(POC_ROOT),
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=15,
                    )
                    self.assertEqual(completed.returncode, -9, completed.stderr[-500:])
                    self.assertIn('"fault_crash"', completed.stdout)

                    store = FileObjectStore(root / "objects")
                    checkpoint = JsonCheckpointStore(root / "checkpoint.json")
                    after_fault = inspect_frontier(
                        store,
                        checkpoint,
                        payload_key=payload_key,
                        manifest_key=manifest_key,
                    )
                    self.assertIsNone(after_fault["checkpoint"])
                    self.assertFalse(after_fault["checkpoint_ahead_of_raw"])
                    if frontier == "after_read":
                        self.assertFalse(after_fault["payload_present"])
                        self.assertFalse(after_fault["manifest_present"])
                    elif frontier == "after_payload":
                        self.assertTrue(after_fault["payload_present"])
                        self.assertFalse(after_fault["manifest_present"])
                    else:
                        self.assertTrue(after_fault["payload_present"])
                        self.assertTrue(after_fault["manifest_present"])

                    recovery, collisions = recover(
                        store,
                        checkpoint,
                        payload=payload_path.read_bytes(),
                        manifest=manifest_path.read_bytes(),
                    )
                    after_recovery = inspect_frontier(
                        store,
                        checkpoint,
                        payload_key=payload_key,
                        manifest_key=manifest_key,
                    )
                    self.assertEqual(collisions, 0)
                    self.assertEqual(recovery["loss"], 0)
                    self.assertEqual(recovery["extra"], 0)
                    self.assertEqual(recovery["replayed_event_count"], 2)
                    self.assertEqual(
                        after_recovery["checkpoint"],
                        {"receiver": "DEMOJRN3677", "sequence": 101},
                    )
                    self.assertFalse(after_recovery["checkpoint_ahead_of_raw"])


if __name__ == "__main__":
    unittest.main()
