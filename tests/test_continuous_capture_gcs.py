from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import as400_continuous_capture as capture
from quadringent.gcs_backend import GcsCheckpointStore, GcsObjectStore, GcsSourceGate
from test_gcs_backend import FakeGcsClient


ENV = {
    "QUADRINGENT_STORAGE_BACKEND": "gcs",
    "AS400_RAW_BUCKET": "sandbox-raw",
    "AS400_CHECKPOINT_BUCKET": "sandbox-state",
    "AS400_RAW_PREFIX": "qualification/run-1",
    "AS400_STREAM_KEY": "qualification/run-1",
    "ISERIES_HOST": "ibmi.example.test",
    "ISERIES_USER": "CDCUSER",
    "ISERIES_SCHEMA": "SALES",
    "ISERIES_TABLE": "SALE",
    "AS400_JOURNAL_NAME": "DEMOJRN",
    "AS400_JAVA_CLASSPATH": "test-only",
}


class ContinuousCaptureGcsTests(unittest.TestCase):
    def test_gcs_backend_builds_gcs_stores_and_no_aws_client(self) -> None:
        client = FakeGcsClient()
        with patch.dict(os.environ, ENV, clear=True), patch("sys.argv", ["capture"]), \
                patch.object(capture, "_gcs_client", return_value=client), \
                patch.object(capture, "PersistentJavaWorker") as worker, \
                patch.object(capture, "RawFirstCaptureCoordinator") as coordinator, \
                patch.object(capture, "DynamoDbCheckpointStore") as dynamo, \
                patch.object(capture, "S3ObjectStore") as s3:
            worker.side_effect = RuntimeError("source boundary reached")
            with self.assertRaisesRegex(RuntimeError, "source boundary reached"):
                capture.main()
        store, checkpoint = coordinator.call_args.args
        self.assertIsInstance(store, GcsObjectStore)
        self.assertEqual((store.bucket_name, store.prefix), ("sandbox-raw", "qualification/run-1"))
        self.assertIsInstance(checkpoint, GcsCheckpointStore)
        self.assertIsInstance(worker.call_args.kwargs["connect_gate"], GcsSourceGate)
        dynamo.assert_not_called()
        s3.assert_not_called()

    def test_gcs_backend_requires_checkpoint_bucket_before_source_io(self) -> None:
        environment = {k: v for k, v in ENV.items() if k != "AS400_CHECKPOINT_BUCKET"}
        with patch.dict(os.environ, environment, clear=True), patch("sys.argv", ["capture"]), \
                patch.object(capture, "_gcs_client", return_value=FakeGcsClient()), \
                patch.object(capture, "PersistentJavaWorker") as worker:
            with self.assertRaisesRegex(ValueError, "AS400_CHECKPOINT_BUCKET"):
                capture.main()
        worker.assert_not_called()

    def test_aws_only_features_are_refused_on_gcs(self) -> None:
        for extra, argv in (({}, ["capture", "--reserve-run-id", "r1"]),
                            ({"AS400_CONSOLE_SNAPSHOT_S3_KEY": "console.json"}, ["capture"])):
            with self.subTest(argv=argv), patch.dict(os.environ, {**ENV, **extra}, clear=True), \
                    patch("sys.argv", argv), patch.object(capture, "PersistentJavaWorker") as worker, \
                    patch.object(capture, "_reserve_run") as reserve:
                with self.assertRaisesRegex(ValueError, "aws backend"):
                    capture.main()
            worker.assert_not_called()
            reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
