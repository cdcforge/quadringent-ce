#!/usr/bin/env python3
"""Read a site capture chain in a killable child process. No source or SQL access."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from quadringent.object_store import S3ObjectStore
from quadringent.proof_windows import read_window_chain
from quadringent.site_config import current as current_site
from quadringent.verification_window import run_archive_prefix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    aws = parser.add_mutually_exclusive_group()
    site = current_site()
    aws.add_argument('--aws-profile', default=site.aws_profile)
    aws.add_argument('--aws-default-credentials', action='store_true')
    args = parser.parse_args(argv)
    try:
        if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', args.run_id) is None:
            raise ValueError('invalid run identity')
        import boto3
        from quadringent_autonomous_verify import _publication_client
        client = _publication_client(boto3, None if args.aws_default_credentials else args.aws_profile)
        store = S3ObjectStore(site.raw_bucket, run_archive_prefix(site, args.run_id), client=client)
        try:
            store.get_bounded('window-chain.json', 1024*1024)
        except FileNotFoundError:
            print(json.dumps({'status': 'pending', 'run_id': args.run_id}))
            return 2
        chain = read_window_chain(store, now=datetime.now(timezone.utc))
        print(json.dumps({'status': 'observed', 'run_id': args.run_id, 'chain': chain}))
        return 0
    except Exception as error:
        print(json.dumps({'status': 'error', 'error_type': type(error).__name__}), file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
