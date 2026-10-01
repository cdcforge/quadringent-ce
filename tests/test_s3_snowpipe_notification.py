from __future__ import annotations

import site_fixture

import copy
from pathlib import Path
import subprocess
import sys
import unittest


SITE = site_fixture.build_test_site()

QUEUE_ARN = f"arn:aws:sqs:{SITE.aws_region}:{SITE.aws_account_id}:sf-snowpipe"
ROOT = Path(__file__).parents[1]


class S3SnowpipeNotificationTests(unittest.TestCase):
    def test_adds_a_bounded_jsonl_notification_and_preserves_existing_entries(self) -> None:
        from quadringent.s3_snowpipe_notification import with_snowpipe_notification

        existing = {
            "TopicConfigurations": [
                {
                    "Id": "unrelated-topic",
                    "TopicArn": "arn:aws:sns:eu-west-3:000000000001:other",
                    "Events": ["s3:ObjectRemoved:*"],
                }
            ]
        }
        original = copy.deepcopy(existing)

        updated = with_snowpipe_notification(existing, QUEUE_ARN, site=SITE)

        self.assertEqual(existing, original)
        self.assertEqual(updated["TopicConfigurations"], original["TopicConfigurations"])
        queue = updated["QueueConfigurations"][0]
        self.assertEqual(queue["QueueArn"], QUEUE_ARN)
        self.assertEqual(queue["Events"], ["s3:ObjectCreated:*"])
        self.assertEqual(
            queue["Filter"]["Key"]["FilterRules"],
            [
                {"Name": "prefix", "Value": f"{SITE.stream_prefix}/"},
                {"Name": "suffix", "Value": ".jsonl"},
            ],
        )

    def test_is_idempotent_for_the_exact_existing_notification(self) -> None:
        from quadringent.s3_snowpipe_notification import with_snowpipe_notification

        first = with_snowpipe_notification({}, QUEUE_ARN, site=SITE)
        second = with_snowpipe_notification(first, QUEUE_ARN, site=SITE)

        self.assertEqual(second, first)
        self.assertEqual(len(second["QueueConfigurations"]), 1)

    def test_refuses_to_replace_a_different_queue_with_the_same_id(self) -> None:
        from quadringent.s3_snowpipe_notification import with_snowpipe_notification

        existing = with_snowpipe_notification({}, QUEUE_ARN, site=SITE)
        with self.assertRaises(ValueError):
            with_snowpipe_notification(
                existing,
                f"arn:aws:sqs:{SITE.aws_region}:{SITE.aws_account_id}:different-queue",
            site=SITE)

    def test_refuses_an_invalid_or_non_sqs_channel(self) -> None:
        from quadringent.s3_snowpipe_notification import with_snowpipe_notification

        for channel in (
            "",
            "https://example.test",
            "arn:aws:sns:eu-west-3:123:topic",
            "arn:aws:sqs:eu-west-3:000000000001:sf-snowpipe",
            "arn:aws-cn:sqs:eu-west-3:000000000001:sf-snowpipe",
        ):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                with_snowpipe_notification({}, channel, site=SITE)

    def test_refuses_an_overlapping_existing_object_created_filter(self) -> None:
        from quadringent.s3_snowpipe_notification import with_snowpipe_notification

        existing = {
            "LambdaFunctionConfigurations": [
                {
                    "Id": "broad-json-loader",
                    "LambdaFunctionArn": "arn:aws:lambda:eu-west-3:000000000001:function:loader",
                    "Events": ["s3:ObjectCreated:*"] ,
                    "Filter": {
                        "Key": {
                            "FilterRules": [
                                {"Name": "prefix", "Value": SITE.stream_prefix.rsplit("/", 1)[0] + "/"},
                                {"Name": "suffix", "Value": ".jsonl"},
                            ]
                        }
                    },
                }
            ]
        }

        with self.assertRaises(ValueError):
            with_snowpipe_notification(existing, QUEUE_ARN, site=SITE)

    def test_setup_cli_requires_exact_confirmation_before_aws_mutation(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_s3_snowpipe_setup.py",
                "--notification-channel",
                QUEUE_ARN,
                "--execute",
                "--confirm",
                "wrong",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("exact site confirmation is required", result.stderr)

    def test_setup_cli_is_a_hermetic_dry_run_by_default(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_s3_snowpipe_setup.py",
                "--notification-channel",
                QUEUE_ARN,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        document = __import__("json").loads(result.stdout)
        self.assertEqual(document["status"], "DRY_RUN")
        self.assertEqual(document["inspection"], "NOT_EXECUTED")

    def test_rollback_removes_only_the_product_notification(self) -> None:
        from quadringent.s3_snowpipe_notification import (
            with_snowpipe_notification,
            without_snowpipe_notification,
        )

        existing = with_snowpipe_notification(
            {
                "QueueConfigurations": [
                    {
                        "Id": "unrelated",
                        "QueueArn": "arn:aws:sqs:eu-west-3:000000000001:unrelated",
                        "Events": ["s3:ObjectRemoved:*"] ,
                    }
                ]
            },
            QUEUE_ARN,
        site=SITE)

        rolled_back = without_snowpipe_notification(existing, site=SITE)

        self.assertEqual(
            [entry["Id"] for entry in rolled_back["QueueConfigurations"]],
            ["unrelated"],
        )

    def test_rollback_is_idempotent_when_notification_is_absent(self) -> None:
        from quadringent.s3_snowpipe_notification import without_snowpipe_notification

        existing = {"EventBridgeConfiguration": {}}
        self.assertEqual(without_snowpipe_notification(existing, site=SITE), existing)

    def test_rollback_refuses_a_malformed_existing_queue_entry(self) -> None:
        from quadringent.s3_snowpipe_notification import without_snowpipe_notification

        with self.assertRaises(ValueError):
            without_snowpipe_notification({"QueueConfigurations": ["invalid"]}, site=SITE)

    def test_rollback_cli_requires_its_own_exact_confirmation(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/quadringent_s3_snowpipe_setup.py",
                "--rollback",
                "--execute",
                "--confirm",
                "wrong",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("exact site rollback confirmation is required", result.stderr)

    def test_refuses_cleanly_on_a_gcs_backend_site(self) -> None:
        """Les notifications S3->SQS->Snowpipe sont un mécanisme AWS
        uniquement : un site storage_backend=gcs ne doit ni construire un ARN
        vide de sens ni planter — un refus explicite et clair."""

        from quadringent.s3_snowpipe_notification import (
            lane_notification_id,
            with_snowpipe_notification,
            with_snowpipe_notifications,
            without_snowpipe_notification,
            without_snowpipe_notifications,
        )

        gcs_site = site_fixture.build_test_site(
            storage_backend="gcs", aws_account_id="", aws_region="", checkpoint_table=""
        )

        with self.assertRaises(ValueError) as ctx:
            with_snowpipe_notification({}, QUEUE_ARN, site=gcs_site)
        self.assertIn("storage_backend=aws", str(ctx.exception))

        with self.assertRaises(ValueError):
            without_snowpipe_notification({}, site=gcs_site)
        with self.assertRaises(ValueError):
            with_snowpipe_notifications({}, {"SALE": QUEUE_ARN}, site=gcs_site)
        with self.assertRaises(ValueError):
            without_snowpipe_notifications({}, ["SALE"], site=gcs_site)
        with self.assertRaises(ValueError):
            lane_notification_id("SALE", site=gcs_site)


if __name__ == "__main__":
    unittest.main()
