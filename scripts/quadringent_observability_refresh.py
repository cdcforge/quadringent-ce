#!/usr/bin/env python3
"""Refresh only the declared site observability; never start capture or refresh delivery dates."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from quadringent.console_snapshot import FileSnapshotSink
from quadringent.observability_snapshot import collect_and_attach_observability, read_previous_observability
from quadringent.slo import SloPolicy
from quadringent.site_config import current as current_site
from quadringent.snowflake_autonomous import autonomous_proof_s3_target
from quadringent_autonomous_verify import _connect_snowflake, _publication_client, _validate_cockpit_document
from quadringent_slo_collect import _proof_archive_run_id, _reconciled_archive

# Jeton de confirmation dérivé du site déclaré — jamais un littéral d'installation.


def prepare_observability(storage, cloudwatch, cursor, policy, *, now):
    site = current_site()
    previous, etag = read_previous_observability(storage, site=site)
    if etag is None:
        raise ValueError('a delivery proof must exist before observability refresh')
    bucket, key = autonomous_proof_s3_target(
        site.autonomous_proof_s3_uri, expected=site.autonomous_proof_s3_uri
    )
    response = storage.get_object(Bucket=bucket, Key=key, IfMatch=etag)
    body = response['Body']
    try:
        size = response.get('ContentLength')
        if type(size) is not int or not 0 < size <= 1048576:
            raise ValueError('proof size is invalid')
        payload = body.read(1048577)
        if len(payload) != size:
            raise ValueError('proof length changed')
        proof = json.loads(payload)
    finally:
        body.close()
    _validate_cockpit_document(proof, observed_at=now)
    archive = _reconciled_archive(proof, storage, _proof_archive_run_id(proof), now)
    combined = collect_and_attach_observability(proof, storage, cloudwatch, cursor,
        object_keys=archive.object_keys, policy=policy, now=now, site=site,
        previous_alert_state=previous)
    if {k:v for k,v in combined.items() if k != 'observability'} != {k:v for k,v in proof.items() if k != 'observability'}:
        raise ValueError('refresh changed delivery evidence')
    _validate_cockpit_document(combined, observed_at=now)
    output = (json.dumps(combined, sort_keys=True) + '\n').encode()
    if len(output) > 1048576:
        raise ValueError('combined proof exceeds the cockpit read budget')
    return combined, etag


def publish_prepared_observability(storage, document, etag):
    if not isinstance(etag, str) or not etag.strip():
        raise ValueError('publication requires a known previous version')
    site = current_site()
    bucket, key = autonomous_proof_s3_target(
        site.autonomous_proof_s3_uri, expected=site.autonomous_proof_s3_uri
    )
    return storage.put_object(Bucket=bucket, Key=key,
        Body=(json.dumps(document, sort_keys=True)+'\n').encode(),
        ContentType='application/json', CacheControl='no-store', IfMatch=etag)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--publish-confirm', default='')
    auth = parser.add_mutually_exclusive_group()
    site = current_site()
    auth.add_argument('--aws-profile', default=site.aws_profile)
    auth.add_argument('--aws-default-credentials', action='store_true')
    snow = parser.add_mutually_exclusive_group()
    snow.add_argument('--connection-name', default=site.snowflake_connection)
    snow.add_argument('--snowflake-oidc-token-file')
    args = parser.parse_args(argv)
    if args.publish_confirm not in ('', site.observability_refresh_token):
        parser.error('exact observability publication confirmation required')
    publication_status = 'not_attempted'
    try:
        import boto3
        import snowflake.connector
        policy = SloPolicy.from_mapping(json.loads(Path(args.policy).read_text()))
        profile = None if args.aws_default_credentials else args.aws_profile
        storage = _publication_client(boto3, profile)
        session = boto3.Session(profile_name=profile, region_name=site.aws_region)
        connection = _connect_snowflake(snowflake.connector,
            None if args.snowflake_oidc_token_file else args.connection_name,
            oidc_token_file=args.snowflake_oidc_token_file)
        try:
            cursor = connection.cursor()
            try:
                result, etag = prepare_observability(storage, session.client('cloudwatch'), cursor,
                    policy, now=datetime.now(UTC))
            finally:
                cursor.close()
        finally:
            connection.close()
        FileSnapshotSink(Path(args.out)).write((json.dumps(result, sort_keys=True)+'\n').encode())
        receipt = None
        if args.publish_confirm:
            publication_status = 'unknown'
            receipt = publish_prepared_observability(storage, result, etag)
            publication_status = 'confirmed'
    except Exception as error:
        print(json.dumps({'status':'error', 'error_type':type(error).__name__,
            'publication_status':publication_status}), file=sys.stderr)
        return 3
    print(json.dumps({'status':'refreshed', 'published':bool(args.publish_confirm),
        'publication_status':publication_status,
        'publication_etag':receipt.get('ETag') if isinstance(receipt, dict) else None,
        'publication_version_id':receipt.get('VersionId') if isinstance(receipt, dict) else None,
        'slo_status':result['observability']['slo_report']['status'], 'capture_started':False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
