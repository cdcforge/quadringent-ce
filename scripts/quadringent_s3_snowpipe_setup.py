#!/usr/bin/env python3
"""Wire the declared site proof-lane S3 prefix to a Snowpipe SQS channel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.s3_snowpipe_notification import (
    with_snowpipe_notification,
    without_snowpipe_notification,
)
from quadringent.site_config import current as current_site


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notification-channel", default="")
    site = current_site()
    parser.add_argument("--profile", default=site.aws_profile)
    parser.add_argument(
        "--aws-default-credentials",
        action="store_true",
        help="utiliser l identite ambiante (variables d environnement ou role "
        "attache) au lieu d un profil nomme : un client n a pas a posseder le "
        "profil de l exploitant",
    )
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    rollback_confirmation = f"ROLLBACK_{site.confirmation_token('SNOWPIPE_S3')}"
    confirmation = site.confirmation_token('SNOWPIPE_S3')
    if args.rollback and args.execute and args.confirm != rollback_confirmation:
        print("ERROR: exact site rollback confirmation is required", file=sys.stderr)
        return 2
    if not args.rollback and args.execute and args.confirm != confirmation:
        print("ERROR: exact site confirmation is required", file=sys.stderr)
        return 2
    if not args.rollback and not args.notification_channel:
        parser.error("--notification-channel is required unless --rollback is used")
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "environment": site.environment,
                    "bucket": site.raw_bucket,
                    "prefix": site.stream_prefix + "/",
                    "operation": "rollback" if args.rollback else "install",
                    "inspection": "NOT_EXECUTED",
                    "confirmation": (
                        rollback_confirmation if args.rollback else confirmation
                    ),
                },
                sort_keys=True,
            )
        )
        return 0

    try:
        import boto3

        profile = None if args.aws_default_credentials else args.profile
        options = {"region_name": site.aws_region}
        if profile is not None:
            options["profile_name"] = profile
        client = boto3.Session(**options).client("s3")
        current = client.get_bucket_notification_configuration(Bucket=site.raw_bucket)
        updated = (
            without_snowpipe_notification(current, site=site)
            if args.rollback
            else with_snowpipe_notification(current, args.notification_channel, site=site)
        )
        changed = _without_metadata(current) != updated
        if changed:
            client.put_bucket_notification_configuration(
                Bucket=site.raw_bucket,
                NotificationConfiguration=updated,
            )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "response", {})
                    .get("Error", {})
                    .get("Code"),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "APPLIED",
                "environment": site.environment,
                "bucket": site.raw_bucket,
                "prefix": site.stream_prefix + "/",
                "changed": changed,
                "operation": "rollback" if args.rollback else "install",
                "confirmation": rollback_confirmation if args.rollback else confirmation,
            },
            sort_keys=True,
        )
    )
    return 0


def _without_metadata(value: dict[str, object]) -> dict[str, object]:
    result = dict(value)
    result.pop("ResponseMetadata", None)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
