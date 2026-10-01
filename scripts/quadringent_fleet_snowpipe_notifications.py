#!/usr/bin/env python3
"""Wire one S3 notification per fleet table to its Snowpipe SQS channel.

Dry-run is the default. Execution reads the live bucket notification document,
preserves every existing entry, adds the missing per-table lanes and refuses
any foreign notification that would overlap a fleet journal prefix.

The lanes come from the Snowflake readback report of
`quadringent_fleet_destination_setup.py --verify-only`, so the prefix, the table
and the channel are the objects Snowflake actually returned, never a guess.
"""

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
    with_snowpipe_notifications,
    without_snowpipe_notifications,
)
from quadringent.site_config import current as current_site


def load_lanes(report: Path, site) -> dict[str, str]:
    """Construit la demande à partir de la relecture Snowflake, pas d'une liste."""

    document = json.loads(report.read_text())
    if document.get("status") != "VERIFIED":
        raise ValueError("destination report is not a verified readback")
    lanes: dict[str, str] = {}
    for lane in document.get("lanes", []):
        if lane.get("pre_existing"):
            # Une voie préexistante a déjà sa notification : ne pas la doubler.
            continue
        table = str(lane.get("table", ""))
        channel = str(lane.get("notification_channel", ""))
        prefix = str(lane.get("prefix", ""))
        if not table or not channel:
            raise ValueError("destination report is missing a lane channel")
        expected = site.journal_prefix_for(table)
        if prefix != expected:
            raise ValueError("destination report lane prefix is not canonical")
        lanes[table] = channel
    if not lanes:
        raise ValueError("destination report contains no new lane")
    return lanes


def _changed(before: dict, after: dict) -> bool:
    a = dict(before)
    a.pop("ResponseMetadata", None)
    return a != after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destinations-report", required=True)
    parser.add_argument(
        "--profile",
        default="",
        help="profil AWS nomme ; vide signifie : utiliser l identite ambiante "
        "(variables d environnement ou role attache), comme chez un client",
    )
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    site = current_site()
    rollback_confirmation = f"ROLLBACK_{site.confirmation_token('FLEET_SNOWPIPE_S3')}"
    confirmation = site.confirmation_token('FLEET_SNOWPIPE_S3')
    lanes = load_lanes(Path(args.destinations_report), site)
    if args.rollback and args.execute and args.confirm != rollback_confirmation:
        print("ERROR: exact site rollback confirmation is required", file=sys.stderr)
        return 2
    if not args.rollback and args.execute and args.confirm != confirmation:
        print("ERROR: exact site confirmation is required", file=sys.stderr)
        return 2
    if not args.execute:
        return _emit(
            {
                "status": "DRY_RUN",
                "environment": site.environment,
                "bucket": site.raw_bucket,
                "operation": "rollback" if args.rollback else "install",
                "lane_count": len(lanes),
                "tables": sorted(lanes),
                "inspection": "NOT_EXECUTED",
                "confirmation": (
                    rollback_confirmation if args.rollback else confirmation
                ),
            },
            args.report,
        )

    try:
        import boto3

        # Sans profil nomme, boto3 resout l'identite ambiante : variables
        # d'environnement, fichier partage ou role attache. Un client n'a pas
        # a posseder un profil SSO nomme pour exploiter ce produit.
        session = (
            boto3.Session(profile_name=args.profile, region_name=site.aws_region)
            if args.profile.strip()
            else boto3.Session(region_name=site.aws_region)
        )
        client = session.client("s3")
        current = client.get_bucket_notification_configuration(Bucket=site.raw_bucket)
        updated = (
            without_snowpipe_notifications(current, list(lanes), site=site)
            if args.rollback
            else with_snowpipe_notifications(current, lanes, site=site)
        )
        changed = _changed(current, updated)
        if changed:
            client.put_bucket_notification_configuration(
                Bucket=site.raw_bucket,
                NotificationConfiguration=updated,
            )
        readback = client.get_bucket_notification_configuration(Bucket=site.raw_bucket)
        installed = _installed_ids(readback)
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "response", {}).get("Error", {}).get("Code"),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    return _emit(
        {
            "status": "APPLIED" if changed else "UNCHANGED",
            "environment": site.environment,
            "bucket": site.raw_bucket,
            "operation": "rollback" if args.rollback else "install",
            "changed": changed,
            "lane_count": len(lanes),
            "installed_ids": installed,
            "verification": "READBACK",
            "confirmation": rollback_confirmation if args.rollback else confirmation,
        },
        args.report,
    )


def _installed_ids(document: dict) -> list[str]:
    return sorted(
        str(entry.get("Id", ""))
        for entry in document.get("QueueConfigurations", [])
        if isinstance(entry, dict)
    )


def _emit(document: dict, report: str) -> int:
    payload = json.dumps(document, sort_keys=True)
    if report:
        Path(report).write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
