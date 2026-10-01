#!/usr/bin/env python3
"""Verify one closed site window; optionally publish its immutable S3 sidecar."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from quadringent.object_store import FileObjectStore, S3ObjectStore
from quadringent.proof_windows import _stream_id
from quadringent.snowflake_autonomous import DestinationLoadPending
from quadringent.site_config import current as current_site
from quadringent.verification_window import run_archive_prefix
from quadringent.window_destination import verify_closed_window_destination
from quadringent_autonomous_verify import _connect_snowflake, _publication_client


def _encode(proof):
    return json.dumps(proof,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'


def _revalidated_payload(previous_bytes, fresh):
    """Keep the initial observation, but never reuse unverified business content."""
    previous=json.loads(previous_bytes)
    if _encode(previous)!=previous_bytes:
        raise ValueError('resume requires original canonical proof bytes')
    stamp=previous['destination']['observed_at']
    observed=datetime.fromisoformat(stamp)
    sealed=datetime.fromisoformat(fresh['window']['sealed_at'])
    current=datetime.fromisoformat(fresh['destination']['observed_at'])
    if observed.utcoffset() is None or not sealed<=observed<=current:
        raise ValueError('invalid initial destination observation')
    comparable={**fresh,'destination':{**fresh['destination'],'observed_at':stamp}}
    if _encode(comparable)!=previous_bytes:
        raise ValueError('window evidence changed since publication attempt')
    return previous_bytes


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--window-id',required=True)
    parser.add_argument('--proof-output',required=True,help='New local evidence file; existing files are never replaced')
    parser.add_argument('--publish-window-proof',action='store_true',help='Create the dedicated site window sidecar and verify exact readback; never overwrite capture')
    parser.add_argument('--resume-publication',action='store_true',help='Revalidate and reuse the original local proof bytes after an uncertain publication')
    parser.add_argument('--await-window',action='store_true',help='Return pending if the initial closed-window object does not exist yet')
    site=current_site()
    aws=parser.add_mutually_exclusive_group()
    aws.add_argument('--aws-profile',default=site.aws_profile)
    aws.add_argument('--aws-default-credentials',action='store_true')
    snow=parser.add_mutually_exclusive_group()
    snow.add_argument('--connection-name',default=site.snowflake_connection)
    snow.add_argument('--snowflake-oidc-token-file')
    args=parser.parse_args(argv)
    if args.resume_publication and not args.publish_window_proof:
        parser.error('--resume-publication requires --publish-window-proof')
    for value in (args.run_id,args.window_id):
        if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',value) is None:
            parser.error('run and window identities must be bounded lowercase identifiers')
    if args.snowflake_oidc_token_file and not Path(args.snowflake_oidc_token_file).is_absolute():
        parser.error('OIDC token path must be absolute')
    publication_state='not_attempted'
    try:
        output=Path(args.proof_output)
        previous_bytes=None
        if output.is_symlink():
            raise ValueError('evidence path must not be a symlink')
        if args.resume_publication:
            previous_bytes=FileObjectStore(output.parent).get_bounded(output.name,1024*1024)
        elif output.exists():
            raise FileExistsError('evidence file already exists')
        import boto3
        import snowflake.connector

        storage=_publication_client(boto3,None if args.aws_default_credentials else args.aws_profile)
        store=S3ObjectStore(site.raw_bucket,run_archive_prefix(site,args.run_id),client=storage)
        if args.await_window and not args.resume_publication:
            try:
                store.get_bounded(f'windows/{args.window_id}/closed.json',1024*1024)
            except FileNotFoundError:
                print(json.dumps({'status':'pending','reason':'window_not_closed','run_id':args.run_id,'window_id':args.window_id,'publication_state':'not_attempted'}))
                return 2
        connection=_connect_snowflake(snowflake.connector,
            None if args.snowflake_oidc_token_file else args.connection_name,
            oidc_token_file=args.snowflake_oidc_token_file)
        try:
            cursor=connection.cursor()
            try:
                proof=verify_closed_window_destination(cursor,store,run_id=args.run_id,
                    window_id=args.window_id,observed_at=datetime.now(timezone.utc),site=site)
            finally:
                cursor.close()
        finally:
            connection.close()
        state=proof['destination']['state']
        if state not in ('matched','not_tested'):
            raise ValueError('unrecognized destination verdict')
        payload=_revalidated_payload(previous_bytes,proof) if previous_bytes is not None else _encode(proof)
        if len(payload)>1024*1024:
            raise ValueError('window proof exceeds publication budget')
        if args.publish_window_proof:
            intent=proof['window']['intent']
            if (proof.get('storage_backend')!='s3' or proof.get('archive_run_id')!=args.run_id
                    or proof.get('window_id')!=args.window_id
                    or intent.get('format_version')!='quadringent-window-intent-v2'):
                raise ValueError('publication requires bound site window evidence')
            _stream_id(intent.get('stream_id'))
        if previous_bytes is None:
            FileObjectStore(output.parent).put_once(output.name,payload)
        if args.publish_window_proof:
            key=f'windows/{args.window_id}/destination.json'
            publication_state='unknown'
            store.put_once(key,payload)
            if store.get_bounded(key,1024*1024)!=payload:
                raise ValueError('publication readback differs')
            publication_state='confirmed'
    except DestinationLoadPending:
        print(json.dumps({'status':'pending','run_id':args.run_id,'window_id':args.window_id}))
        return 2
    except Exception as error:
        print(json.dumps({'status':'error','error_type':type(error).__name__,'publication_state':publication_state}),file=sys.stderr)
        return 3
    print(json.dumps({'status':state,'run_id':args.run_id,'window_id':args.window_id,'proof_published':publication_state=='confirmed','publication_state':publication_state}))
    return 0 if state=='matched' else 2


if __name__=='__main__':
    raise SystemExit(main())
