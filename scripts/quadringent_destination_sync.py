#!/usr/bin/env python3
"""Synchronise la table de preuve du site depuis son stage dédié.

Le chargement reste découplé de la capture : ce processus ne lit pas IBM i et
n'avance pas le checkpoint journal. Dry-run par défaut.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

from quadringent.console_snapshot import FileSnapshotSink
from quadringent.destination_sync import (
    DestinationSyncError,
    sync_captured_destination,
)
from quadringent.site_config import current as current_site
from quadringent.snowflake_replay import SnowflakeReplayConfig


def _site():
    return current_site()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    site = _site()
    parser.add_argument("--object-key", default=f"{site.stream_prefix}/batch.jsonl")
    parser.add_argument("--all-jsonl", action="store_true")
    parser.add_argument(
        "--object-keys-file",
        default="",
        help="liste bornée de clés de la table de preuve (une par ligne), exclusive de --all-jsonl",
    )
    parser.add_argument("--run-tag", required=True)
    parser.add_argument("--capture-snapshot", required=True)
    parser.add_argument("--proof-output", required=True)
    parser.add_argument("--connection-name", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    if args.all_jsonl and args.object_keys_file:
        parser.error("--all-jsonl and --object-keys-file are mutually exclusive")
    object_keys = _read_object_keys(args.object_keys_file) if args.object_keys_file else None
    config = SnowflakeReplayConfig(
        scope=site.snowflake_scope,
        stage=site.proof_stage,
        raw_table=f"{site.destination_schema}_EXT_RAW_{args.run_tag.upper()}",
        canonical_table=f"{site.destination_schema}_EXT_CANONICAL_{args.run_tag.upper()}",
        object_key=args.object_key if object_keys is None else object_keys[0],
    )
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "stage": config.stage,
                    "object_key": None
                    if args.all_jsonl or object_keys is not None
                    else config.object_key,
                    "object_count": None if object_keys is None else len(object_keys),
                    "scope": (
                        "all_jsonl"
                        if args.all_jsonl
                        else ("object_keys" if object_keys is not None else "one_object")
                    ),
                    "confirmation": site.confirmation_token("EXTERNAL_REPLAY"),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.confirm != site.confirmation_token("EXTERNAL_REPLAY"):
        print("ERROR: exact site confirmation is required", file=sys.stderr)
        return 2

    capture = json.loads(Path(args.capture_snapshot).read_text(encoding="utf-8"))
    if not isinstance(capture, dict):
        print(json.dumps({"status": "ERROR", "code": "invalid_capture"}, sort_keys=True))
        return 1
    try:
        from snowflake_external_replay import _connect

        connection = _connect(
            query_tag=f"{site.destination_schema}_DEST_SYNC_{args.run_tag.upper()}",
            connection_name=args.connection_name or None,
        )
        try:
            combined = sync_captured_destination(
                connection.cursor(),
                capture,
                config,
                run_tag=args.run_tag.upper(),
                observed_at=datetime.now(UTC),
                site=site,
                all_jsonl=args.all_jsonl,
                object_keys=object_keys,
            )
        finally:
            connection.close()
    except DestinationSyncError as error:
        print(json.dumps({"status": "ERROR", "code": error.code}, sort_keys=True))
        return 1
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

    FileSnapshotSink(Path(args.proof_output)).write(
        json.dumps(combined, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    print(json.dumps({"status": "PASS", "phase": "destination_sync"}, sort_keys=True))
    return 0


def _read_object_keys(path: str) -> list[str]:
    keys = [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not keys:
        raise SystemExit("ERROR: --object-keys-file must list at least one proof object")
    return keys


if __name__ == "__main__":
    raise SystemExit(main())
