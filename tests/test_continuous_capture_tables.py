from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import as400_continuous_capture as capture


ENV = {
    "AS400_RAW_BUCKET": "example-corp-000000000000-int-example-corp-raw",
    "ISERIES_HOST": "192.0.2.10",
    "ISERIES_USER": "CDCUSER",
    "AS400_RAW_PREFIX": "as400/sales/sale/runs/r1",
    "AS400_CHECKPOINT_TABLE": "example-corp-int-example-corp-checkpoints",
    "AS400_STREAM_KEY": "as400/sales/sale",
    "ISERIES_SCHEMA": "SALES",
    "ISERIES_TABLE": "SALE",
    "AS400_JOURNAL_NAME": "DEMOJRN",
    "AS400_JAVA_CLASSPATH": "test-only",
}


class ContinuousCaptureTablesTests(unittest.TestCase):
    def test_worker_receives_the_explicit_table_list(self) -> None:
        with patch.dict(os.environ, {**ENV, "ISERIES_TABLES": "SALE,CNTR"}, clear=True), patch(
            "sys.argv", ["capture"]
        ), patch.object(capture, "PersistentJavaWorker") as worker, patch.object(
            capture, "DynamoDbCheckpointStore"
        ), patch.object(capture, "DynamoDbSourceGate"), patch.object(
            capture, "S3ObjectStore"
        ) as stores:
            stores.return_value.get_bounded.side_effect = FileNotFoundError("window-chain.json")
            worker.side_effect = RuntimeError("source boundary reached")
            with self.assertRaisesRegex(RuntimeError, "source boundary reached"):
                capture.main()
        self.assertEqual(worker.call_args.kwargs["table"], "SALE")
        self.assertEqual(worker.call_args.kwargs["tables"], "SALE,CNTR")

    def test_absent_table_list_keeps_mono_table_worker_contract(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), patch("sys.argv", ["capture"]), patch.object(
            capture, "PersistentJavaWorker"
        ) as worker, patch.object(capture, "DynamoDbCheckpointStore"), patch.object(
            capture, "DynamoDbSourceGate"
        ), patch.object(
            capture, "S3ObjectStore"
        ) as stores:
            stores.return_value.get_bounded.side_effect = FileNotFoundError("window-chain.json")
            worker.side_effect = RuntimeError("source boundary reached")
            with self.assertRaisesRegex(RuntimeError, "source boundary reached"):
                capture.main()
        self.assertEqual(worker.call_args.kwargs["table"], "SALE")
        self.assertIsNone(worker.call_args.kwargs["tables"])

    def test_flux_identity_still_lists_objects_without_jvm_secrets(self) -> None:
        with patch.dict(os.environ, {**ENV, "ISERIES_TABLES": "SALE,CNTR"}, clear=True):
            identity = capture._flux_identity()
        self.assertEqual(identity.objects, ("SALES.SALE", "SALES.CNTR"))


if __name__ == "__main__":
    unittest.main()
