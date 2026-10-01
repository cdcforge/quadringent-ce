#!/usr/bin/env python3
"""Collect attributed SLO telemetry for the one Quadringent DEV pipeline."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC in sys.path:
    sys.path.remove(SRC)
sys.path.insert(0, SRC)

from quadringent.console_snapshot import FileSnapshotSink
from quadringent.site_config import current as current_site
from quadringent.slo_telemetry import collect_slo_telemetry
from quadringent.verification_window import collect_archived_verification_window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--now", default="")
    parser.add_argument("--run-id", help="Inspect the exact closed run and match its stored identity proof")
    args = parser.parse_args(argv)

    try:
        now = _timestamp(args.now) if args.now else datetime.now(UTC)
        proof = json.loads(Path(args.proof).read_text(encoding="utf-8"))
        if not isinstance(proof, dict):
            raise ValueError("proof must be a JSON object")
        window_started_at, window_ended_at = _proof_window(proof)

        import boto3
        import snowflake.connector

        site = current_site()
        session = boto3.Session(
            profile_name=site.aws_profile, region_name=site.aws_region
        )
        storage = session.client("s3")
        archive = _reconciled_archive(proof, storage, args.run_id, now) if args.run_id else None
        if not site.snowflake_connection:
            raise ValueError("QUADRINGENT_SNOWFLAKE_CONNECTION is required for this collector")
        connection = snowflake.connector.connect(
            connection_name=site.snowflake_connection,
            warehouse=site.warehouse_name,
            database=site.destination_database,
            schema=site.destination_schema,
            login_timeout=20,
            network_timeout=60,
            session_parameters={"QUERY_TAG": "QUADRINGENT_SLO_READONLY"},
        )
        cursor = connection.cursor()
        try:
            telemetry = collect_slo_telemetry(
                storage,
                session.client("cloudwatch"),
                cursor,
                now=now,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                object_keys=archive.object_keys if archive else None,
                expected_event_count=archive.event_count if archive else None,
                site=site,
            )
        finally:
            close_cursor = getattr(cursor, "close", None)
            if callable(close_cursor):
                close_cursor()
            connection.close()
        payload = (
            json.dumps(telemetry, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        FileSnapshotSink(Path(args.out)).write(payload)
    except Exception as error:
        print(
            json.dumps(
                {"status": "invalid", "error_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3

    errors = telemetry.get("collection_errors")
    status = "pass" if errors == [] else "unobserved"
    print(
        json.dumps(
            {
                "status": status,
                "phase": "slo_collect",
                "measurement_count": len(telemetry) - 2,
                "collection_error_count": len(errors) if isinstance(errors, list) else 1,
            },
            sort_keys=True,
        )
    )
    return 0 if status == "pass" else 2


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must be timezone-aware")
    return parsed


def _proof_archive_run_id(proof: dict[str, object]) -> str:
    """Resolve archive identity; legacy proofs used their checkpoint flux ID."""
    if 'verification_archive' in proof:
        archive = proof['verification_archive']
        run_id = archive.get('run_id') if isinstance(archive, dict) else None
        if not isinstance(run_id, str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', run_id) is None:
            raise ValueError('invalid explicit verification archive')
        return run_id
    flux = proof.get('flux')
    flux_id = flux.get('id') if isinstance(flux, dict) else None
    stream = re.escape(current_site().stream_prefix)
    match = re.fullmatch(
        stream + r'/runs/([a-z0-9][a-z0-9-]{0,79})', str(flux_id)
    )
    if match is None:
        raise ValueError('observability requires an isolated closed run')
    return match[1]


def _reconciled_archive(proof: dict[str, object], storage, run_id: str, now: datetime):
    identity = proof.get("stored_event_identity_proof")
    if _proof_archive_run_id(proof) != run_id:
        raise ValueError("requested archive differs from the proof run")
    if not isinstance(identity, dict) or identity.get("state") != "matched" or identity.get("basis") != "verified_s3_batches":
        raise ValueError("stored identity proof is missing")
    archive = collect_archived_verification_window(storage, run_id=run_id, now=now, site=current_site())
    if (type(identity.get("event_count")) is not int
            or identity["event_count"] != archive.event_count
            or identity.get("event_ids_sha256") != archive.event_ids_sha256):
        raise ValueError("archive identities differ from the reconciled proof")
    return archive


def _proof_window(proof: dict[str, object]) -> tuple[datetime, datetime]:
    run = proof.get("run")
    destination = proof.get("destination_proof")
    if not isinstance(run, dict) or not isinstance(destination, dict):
        raise ValueError("proof window is missing")
    started_at = run.get("started_at")
    ended_at = destination.get("observed_at")
    if not isinstance(started_at, str) or not isinstance(ended_at, str):
        raise ValueError("proof window timestamps are missing")
    return _timestamp(started_at), _timestamp(ended_at)


if __name__ == "__main__":
    raise SystemExit(main())
