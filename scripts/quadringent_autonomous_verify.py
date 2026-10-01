#!/usr/bin/env python3
"""Reconcile a bounded Quadringent capture with the declared site autonomous destination."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIRECTORY = str(Path(__file__).resolve().parent)
if __name__ == "__main__" and _SCRIPT_DIRECTORY in sys.path:
    sys.path.remove(_SCRIPT_DIRECTORY)
for extra in ("src",):
    path = str(ROOT / extra)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

from quadringent.console_snapshot import FileSnapshotSink
from quadringent.observability_snapshot import collect_and_attach_observability, read_previous_observability
from quadringent.site_config import SiteConfig, current as current_site
from quadringent.slo import SloPolicy
from quadringent.snowflake_autonomous import (
    DestinationLoadPending,
    autonomous_plan,
    publish_autonomous_proof,
    verify_autonomous_destination,
)
from quadringent_control_plane.model import SourceDescriptor
from quadringent_control_plane.projection import project_console_document
from quadringent.verification_window import await_capture_closed, collect_verification_window


def _site() -> SiteConfig:
    """Configuration du site déclaré — requise, jamais un défaut implicite."""

    return current_site()


def _publication_client(sdk: Any, profile_name: str | None) -> Any:
    """Resolve credentials without copying secrets and reject other accounts."""

    site = _site()
    options = {"region_name": site.aws_region}
    if profile_name is not None:
        options["profile_name"] = profile_name
    session = sdk.Session(**options)
    identity = session.client("sts").get_caller_identity()
    if identity.get("Account") != site.aws_account_id:
        raise ValueError("Proof publication requires the declared site's AWS account")
    return session.client("s3")


def _connect_snowflake(
    connector: Any, connection_name: str | None, *, oidc_token_file: str | None = None
) -> Any:
    """Charge every canonical verification to the declared site's warehouse."""

    site = _site()
    options: dict[str, Any]
    if oidc_token_file is not None:
        if connection_name is not None or not Path(oidc_token_file).is_absolute():
            raise ValueError("OIDC requires an absolute token path and no local profile")
        options = {
            "account": site.snowflake_account,
            "authenticator": "WORKLOAD_IDENTITY",
            "workload_identity_provider": "OIDC",
            "token_file_path": oidc_token_file,
            "role": site.verifier_role_name,
            "database": site.destination_database,
            "schema": site.destination_schema,
        }
    else:
        if not connection_name:
            raise ValueError("An explicit Snowflake authentication mode is required")
        options = {"connection_name": connection_name}
    return connector.connect(
        **options,
        warehouse=autonomous_plan(site).warehouse,
        login_timeout=20,
        network_timeout=60,
        session_parameters={"QUERY_TAG": "QUADRINGENT_AUTONOMY_VERIFY_READONLY"},
    )


def _validate_cockpit_document(
    document: dict[str, object], *, observed_at: datetime
) -> None:
    """Require the exact document accepted by the live site projection."""

    site = _site()
    source = SourceDescriptor(
        site.pipeline_id,
        "live",
        site.environment,
        site.autonomous_proof_s3_uri,
    )
    project_console_document(document, source, now=observed_at)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-snapshot")
    parser.add_argument("--object-keys-file")
    parser.add_argument("--run-id", help="Acquire a closed site run from its exclusive S3 prefix")
    parser.add_argument("--run-tag", required=True)
    parser.add_argument('--wait-seconds', type=int, default=0,
                        help='Retry partial file loads for at most 120 seconds; isolated runs only')
    parser.add_argument('--await-capture-seconds', type=int, default=0,
                        help='Observe the isolated capture until its planned stop, up to 3660 seconds')
    parser.add_argument("--proof-output", required=True)
    parser.add_argument("--slo-policy", help="Explicit site SLO policy JSON; attach measurements before publication")
    parser.add_argument("--previous-alert-state", help="Previous alert state JSON for lifecycle continuity")
    site = _site()
    snowflake_authentication = parser.add_mutually_exclusive_group()
    snowflake_authentication.add_argument("--connection-name", default=site.snowflake_connection)
    snowflake_authentication.add_argument(
        "--snowflake-oidc-token-file",
        help="Use the dedicated site workload identity with a projected OIDC token file",
    )
    parser.add_argument("--proof-s3-uri", default="")
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--aws-profile", default=site.aws_profile)
    authentication.add_argument(
        "--aws-default-credentials", action="store_true",
        help="Use the SDK credential chain without forcing a local SSO profile",
    )
    parser.add_argument("--publish-confirm", default="")
    args = parser.parse_args()
    if args.previous_alert_state and not args.slo_policy:
        parser.error("--previous-alert-state requires --slo-policy")
    if args.previous_alert_state and args.proof_s3_uri:
        parser.error("published proofs must recover alert history from the declared proof key")
    if not 0 <= args.wait_seconds <= 120 or (args.wait_seconds and args.run_id is None):
        parser.error('--wait-seconds requires --run-id and a value between 0 and 120')
    if not 0 <= args.await_capture_seconds <= 3660 or (args.await_capture_seconds and args.run_id is None):
        parser.error('--await-capture-seconds requires --run-id and a value between 0 and 3660')
    if args.run_id is not None:
        if args.capture_snapshot is not None or args.object_keys_file is not None:
            parser.error("--run-id cannot be combined with local capture inputs")
    elif args.capture_snapshot is None or args.object_keys_file is None:
        parser.error("provide --run-id or both --capture-snapshot and --object-keys-file")
    if args.proof_s3_uri and args.publish_confirm != site.publish_confirmation_token:
        print(
            "ERROR: exact site proof publication confirmation is required",
            file=sys.stderr,
        )
        return 2

    try:
        policy = None
        previous_alert_state = None
        publication_client = None
        previous_etag = None
        if args.slo_policy:
            policy = SloPolicy.from_mapping(json.loads(Path(args.slo_policy).read_text(encoding="utf-8")))
            if args.previous_alert_state:
                previous_alert_state = json.loads(Path(args.previous_alert_state).read_text(encoding="utf-8"))
        if args.proof_s3_uri:
            import boto3

            if args.proof_s3_uri != site.autonomous_proof_s3_uri:
                raise ValueError("proof publication escaped the declared site key")
            publication_client = _publication_client(boto3, None if args.aws_default_credentials else args.aws_profile)
            previous_alert_state, previous_etag = read_previous_observability(
                publication_client, site=site
            )
            if previous_alert_state is not None and policy is None:
                raise ValueError("publication cannot discard existing SLO alert history")
        expected_identity_digest = None
        if args.run_id is not None:
            import boto3

            client = _publication_client(boto3, None if args.aws_default_credentials else args.aws_profile)
            if args.await_capture_seconds:
                await_capture_closed(client, run_id=args.run_id, timeout_seconds=args.await_capture_seconds, site=site)
            window = collect_verification_window(
                client,
                run_id=args.run_id, now=datetime.now(UTC), site=site,
            )
            capture, object_keys = window.capture, window.object_keys
            expected_identity_digest = window.event_ids_sha256
        else:
            capture = json.loads(Path(args.capture_snapshot).read_text(encoding="utf-8"))
            object_keys = tuple(
                line.strip()
                for line in Path(args.object_keys_file).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            )
        if not isinstance(capture, dict):
            raise ValueError("capture snapshot must be a JSON object")
        import snowflake.connector

        connection = _connect_snowflake(
            snowflake.connector,
            None if args.snowflake_oidc_token_file is not None else args.connection_name,
            oidc_token_file=args.snowflake_oidc_token_file,
        )
        try:
            combined, observed_at = _verify_with_wait(
                connection, capture, object_keys, args.run_tag.upper(),
                expected_identity_digest, args.wait_seconds, site=site,
            )
            if args.run_id is not None:
                # The acquired archive is independent of the durable checkpoint stream.
                combined['verification_archive'] = {'run_id': args.run_id}
            if policy is not None:
                import boto3

                profile = None if args.aws_default_credentials else args.aws_profile
                storage = _publication_client(boto3, profile)
                session = boto3.Session(profile_name=profile, region_name=site.aws_region)
                cursor = connection.cursor()
                try:
                    combined = collect_and_attach_observability(
                        combined, storage, session.client("cloudwatch"), cursor,
                        object_keys=object_keys, policy=policy, now=datetime.now(UTC),
                        site=site,
                        previous_alert_state=previous_alert_state,
                    )
                finally:
                    cursor.close()
        finally:
            connection.close()
        _validate_cockpit_document(combined, observed_at=observed_at)
        payload = (
            json.dumps(combined, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        FileSnapshotSink(Path(args.proof_output)).write(payload)
        if args.proof_s3_uri:
            publish_autonomous_proof(
                publication_client, args.proof_s3_uri, payload,
                expected=site.autonomous_proof_s3_uri,
                expected_etag=previous_etag, create_only=previous_etag is None,
            )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "errno", None),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "phase": "autonomous_verify",
                "proof_published": bool(args.proof_s3_uri),
                "slo_status": combined["observability"]["slo_report"]["status"] if policy is not None else "not_collected",
            },
            sort_keys=True,
        )
    )
    return 0


def _verify_with_wait(connection, capture, object_keys, run_tag, digest, wait_seconds, *, site=None):
    site = site or _site()
    deadline = time.monotonic() + wait_seconds
    first = True
    while True:
        if wait_seconds and not first and time.monotonic() >= deadline:
            raise TimeoutError('Snowpipe convergence budget exhausted')
        first = False
        observed_at = datetime.now(UTC)
        if wait_seconds:
            generated = datetime.fromisoformat(capture['generated_at'])
            if generated.tzinfo is None or not 0 <= (observed_at - generated).total_seconds() <= 300:
                raise ValueError('Capture observation expired while waiting for Snowpipe')
        cursor = connection.cursor()
        try:
            combined = verify_autonomous_destination(cursor, capture, autonomous_plan(site),
                object_keys=object_keys, run_tag=run_tag, observed_at=observed_at,
                site=site, expected_event_ids_sha256=digest)
        except DestinationLoadPending:
            if not wait_seconds:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Snowpipe convergence budget exhausted') from None
        else:
            if wait_seconds and time.monotonic() >= deadline:
                raise TimeoutError('Snowpipe verification exceeded its budget')
            if wait_seconds and not 0 <= (datetime.now(UTC) - generated).total_seconds() <= 300:
                raise ValueError('Capture observation expired during Snowpipe verification')
            return combined, observed_at
        finally:
            cursor.close()
        time.sleep(min(5, remaining))


if __name__ == "__main__":
    raise SystemExit(main())
