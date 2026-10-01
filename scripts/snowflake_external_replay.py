#!/usr/bin/env python3
"""Replay one raw S3 object through the declared site Snowflake external stage.

The command is dry-run by default. An explicit confirmation is required for
the table creation, COPY and MERGE statements. Authentication uses either a
standard Snowflake connection name or environment variables; credentials are
never printed.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sys

from quadringent.snowflake_replay import (
    SnowflakeReplayConfig,
    execute_external_replay,
    execute_external_stage_replay,
)
from quadringent.console_snapshot import FileSnapshotSink
from quadringent.destination_proof import attach_snowflake_proof
from quadringent.site_config import current as current_site


_RUN_TAG = re.compile(r"^[A-Za-z0-9_]{1,30}$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--object-key",
        default="batch-7cee092d0da6dc56df7e66e959f2dbce.jsonl",
        help="one object below the configured external stage prefix",
    )
    parser.add_argument(
        "--all-jsonl",
        action="store_true",
        help="replay every .jsonl payload below the bounded stage root",
    )
    parser.add_argument(
        "--run-tag",
        default=datetime.now(UTC).strftime("%Y%m%d%H%M%S"),
        help="safe suffix used for the two isolated site tables",
    )
    site = current_site()
    # La destination est celle du site déclaré : aucune option ne peut la déplacer.
    stage = f"{site.destination_schema}_EXTERNAL_STAGE"

    parser.add_argument(
        "--connection-name",
        default=os.environ.get("SNOWFLAKE_CONNECTION_NAME"),
        help="standard Snowflake connection from connections.toml",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--capture-snapshot",
        help="snapshot as400-console-v1 à compléter après réconciliation",
    )
    parser.add_argument(
        "--proof-output",
        help="snapshot combiné écrit atomiquement pour le control plane",
    )
    args = parser.parse_args()

    if not _RUN_TAG.fullmatch(args.run_tag):
        parser.error("--run-tag must contain only letters, digits and underscores")
    if bool(args.capture_snapshot) != bool(args.proof_output):
        parser.error("--capture-snapshot and --proof-output are required together")
    config = SnowflakeReplayConfig(
        scope=site.snowflake_scope,
        stage=stage,
        raw_table=f"{site.destination_schema}_EXT_RAW_{args.run_tag}",
        canonical_table=f"{site.destination_schema}_EXT_CANONICAL_{args.run_tag}",
        object_key=args.object_key,
    )

    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "database": config.database,
                    "schema": config.schema,
                    "stage": config.stage,
                    "object_key": None if args.all_jsonl else config.object_key,
                    "scope": "all_jsonl" if args.all_jsonl else "one_object",
                    "raw_table": config.raw_table,
                    "canonical_table": config.canonical_table,
                    "confirmation": site.confirmation_token("EXTERNAL_REPLAY"),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.confirm != site.confirmation_token("EXTERNAL_REPLAY"):
        print("ERROR: exact site confirmation is required", file=sys.stderr)
        return 2

    try:
        connection = _connect(
            query_tag=f"{site.destination_schema}_EXTERNAL_REPLAY_{args.run_tag}",
            connection_name=args.connection_name,
        )
        try:
            replay = (
                execute_external_stage_replay
                if args.all_jsonl
                else execute_external_replay
            )
            metrics = replay(connection.cursor(), config)
        finally:
            connection.close()
    except Exception as error:
        # Connector error messages can contain SQL text or endpoint details;
        # the error class/code is enough to route the failed gate safely.
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

    if args.capture_snapshot:
        try:
            _write_destination_snapshot(
                capture_path=Path(args.capture_snapshot),
                output_path=Path(args.proof_output),
                metrics=metrics,
                run_tag=args.run_tag.upper(),
                observed_at=datetime.now(UTC),
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "error_type": type(error).__name__,
                        "phase": "destination_proof",
                    },
                    sort_keys=True,
                )
            )
            return 1
    print(json.dumps(metrics, sort_keys=True))
    if args.capture_snapshot:
        print(
            json.dumps(
                {"status": "PASS", "phase": "destination_proof"},
                sort_keys=True,
            )
        )
    return 0 if metrics.get("status") == "PASS" else 1


def _write_destination_snapshot(
    *,
    capture_path: Path,
    output_path: Path,
    metrics: dict[str, object],
    run_tag: str,
    observed_at: datetime,
) -> None:
    if capture_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("capture snapshot is too large")
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    if not isinstance(capture, dict):
        raise ValueError("capture snapshot must be an object")
    combined = attach_snowflake_proof(
        capture,
        metrics,
        run_tag=run_tag,
        observed_at=observed_at,
        site=current_site(),
    )
    FileSnapshotSink(output_path).write(
        json.dumps(combined, sort_keys=True, ensure_ascii=False).encode("utf-8")
        + b"\n"
    )


def _connect(*, query_tag: str, connection_name: str | None = None):
    try:
        import snowflake.connector
    except ImportError as error:
        raise RuntimeError("snowflake-connector-python is required") from error

    if connection_name is not None:
        if not connection_name.strip():
            raise RuntimeError("Snowflake connection name cannot be blank")
        return snowflake.connector.connect(
            connection_name=connection_name,
            login_timeout=20,
            network_timeout=60,
            session_parameters={"QUERY_TAG": query_tag},
        )

    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as error:
        raise RuntimeError("cryptography is required for environment authentication") from error

    account = _required_env("SNOWFLAKE_ACCOUNT")
    user = _required_env("SNOWFLAKE_USER")
    private_key = _private_key(serialization)
    params: dict[str, object] = {
        "account": account,
        "user": user,
        "authenticator": "SNOWFLAKE_JWT",
        "private_key": private_key,
        "login_timeout": 20,
        "network_timeout": 60,
        "session_parameters": {"QUERY_TAG": query_tag},
    }
    role = os.environ.get("SNOWFLAKE_ROLE")
    # COPY INTO requires an active warehouse even when LIST and DDL can run
    # through Snowflake services without one. Make the dependency explicit so
    # a missing warehouse fails before a partial replay is attempted.
    warehouse = _required_env("SNOWFLAKE_WAREHOUSE")
    if role:
        params["role"] = role
    params["warehouse"] = warehouse
    return snowflake.connector.connect(**params)


def _private_key(serialization):
    key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    encoded = os.environ.get("SNOWFLAKE_PRIVATE_KEY_B64")
    if bool(key_path) == bool(encoded):
        raise RuntimeError(
            "set exactly one of SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_PRIVATE_KEY_B64"
        )
    material = Path(key_path).read_bytes() if key_path else base64.b64decode(encoded)
    try:
        key = serialization.load_pem_private_key(material, password=None)
    except ValueError:
        key = serialization.load_der_private_key(material, password=None)
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
