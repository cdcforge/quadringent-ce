#!/usr/bin/env python3
"""Provision the credential-free Snowpipe destination in the declared site.

Dry-run is the default. Execution creates only the dedicated site raw table,
Snowpipe, and zero-refresh canonical view. Wiring the returned SQS channel into
the dedicated S3 prefix is a separate, explicit infrastructure step.
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

from quadringent.site_config import current as current_site
from quadringent.snowflake_autonomous import (
    autonomous_plan,
    pause_autonomous_destination,
    provision_autonomous_destination,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    site = current_site()
    parser.add_argument("--connection-name", default=site.snowflake_connection)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    plan = autonomous_plan(site)
    rollback_confirmation = f"ROLLBACK_{site.confirmation_token('AUTONOMOUS_LOAD')}"
    confirmation = site.confirmation_token('AUTONOMOUS_LOAD')
    if args.rollback and args.execute and args.confirm != rollback_confirmation:
        print("ERROR: exact site rollback confirmation is required", file=sys.stderr)
        return 2
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "environment": site.environment,
                    "operation": "rollback" if args.rollback else "install",
                    "statement_count": len(
                        plan.pause_statements() if args.rollback else plan.statements()
                    ),
                    "confirmation": rollback_confirmation if args.rollback else confirmation,
                },
                sort_keys=True,
            )
        )
        return 0
    if not args.rollback and args.confirm != confirmation:
        print("ERROR: exact site confirmation is required", file=sys.stderr)
        return 2

    try:
        import snowflake.connector
    except ImportError:
        print("ERROR: snowflake-connector-python is required", file=sys.stderr)
        return 1
    if not args.connection_name:
        print(
            "ERROR: --connection-name or QUADRINGENT_SNOWFLAKE_CONNECTION is required",
            file=sys.stderr,
        )
        return 2
    try:
        connection = snowflake.connector.connect(
            connection_name=args.connection_name,
            login_timeout=20,
            network_timeout=60,
            session_parameters={"QUERY_TAG": f"QUADRINGENT_AUTONOMOUS_LOAD_SETUP_{site.fleet_environment}"},
        )
        try:
            result = (
                pause_autonomous_destination(connection.cursor(), plan, site=site)
                if args.rollback
                else provision_autonomous_destination(connection.cursor(), plan, site=site)
            )
        finally:
            connection.close()
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "errno", None),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
