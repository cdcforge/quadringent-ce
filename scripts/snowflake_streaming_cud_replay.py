#!/usr/bin/env python3
"""Run a bounded Snowpipe Streaming C/U/D recovery proof in the declared scope.

The command is dry-run by default. Execute mode creates one unique table,
streams five synthetic rows in two batches through a named channel, closes and
reopens the channel using its committed offset, verifies the final table, and
removes the table and channel. Credentials stay in the process environment or
an ephemeral profile file and are never printed.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

from quadringent.site_config import current as current_site
from quadringent.snowflake_streaming_replay import (
    StreamingReplayConfig,
    build_stream_rows,
    evaluate_streaming_snapshot,
    offset_token,
)


SDK_VERSION = "1.7.0"


def _confirmation(config: StreamingReplayConfig) -> str:
    """Chaîne à taper pour exécuter : liée au schéma et au tag, jamais figée."""

    return f"{config.schema}_SNOWPIPE_STREAMING_{config.run_tag}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-tag",
        default=datetime.now(UTC).strftime("STR%Y%m%d%H%M%S"),
        help="suffixe d'isolement en majuscules",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    try:
        config = StreamingReplayConfig(
            run_tag=args.run_tag,
            scope=current_site().snowflake_scope,
        )
    except ValueError as error:
        parser.error(str(error))

    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "database": config.database,
                    "schema": config.schema,
                    "target_table": config.target_table,
                    "channel_name": config.channel_name,
                    "synthetic_event_count": len(_stream_rows(config)),
                    "sdk_version": SDK_VERSION,
                    "confirmation": _confirmation(config),
                    "cleanup": "table_and_channel_after_execute",
                },
                sort_keys=True,
            )
        )
        return 0

    if args.confirm != _confirmation(config):
        print("ERROR: exact confirmation is required", file=sys.stderr)
        return 2

    try:
        metrics = _execute_replay(config)
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

    print(json.dumps(metrics, sort_keys=True))
    return 0 if metrics.get("status") == "PASS" else 1


def _stream_rows(config: StreamingReplayConfig) -> tuple[dict[str, Any], ...]:
    return build_stream_rows(
        source_library=f"{config.schema}_TEST",
        source_table="STREAMING_SIM",
    )


def _execute_replay(config: StreamingReplayConfig) -> dict[str, Any]:
    try:
        import snowflake.connector
        from cryptography.hazmat.primitives import serialization
        from snowflake.ingest.streaming import StreamingIngestClient
    except ImportError as error:
        raise RuntimeError(
            "snowflake-connector-python, cryptography and snowpipe-streaming are required"
        ) from error

    connection = _connect(snowflake_connector=snowflake.connector, query_tag=config.channel_name)
    table_created = False
    clients: list[Any] = []
    channels: list[Any] = []
    try:
        cursor = connection.cursor()
        try:
            _assert_table_absent(cursor, config)
            cursor.execute(_table_ddl(config))
            table_created = True

            with tempfile.TemporaryDirectory(prefix="quadringent-snowpipe-profile-") as profile_dir:
                profile_path = _write_profile(Path(profile_dir), serialization)
                rows = _stream_rows(config)

                first_client = StreamingIngestClient.from_table(
                    client_name=f"{config.schema}_{config.run_tag}_ONE",
                    db_name=config.database,
                    schema_name=config.schema,
                    table_name=config.target_table,
                    profile_json=str(profile_path),
                )
                clients.append(first_client)
                first_channel, _ = first_client.open_channel(config.channel_name)
                channels.append(first_channel)

                first_started = time.perf_counter()
                first_channel.append_rows(
                    list(rows[:3]),
                    start_offset_token=offset_token(100),
                    end_offset_token=offset_token(120),
                )
                first_channel.wait_for_commit(
                    _at_least(offset_token(120)),
                    timeout_seconds=120,
                )
                first_elapsed_ms = (time.perf_counter() - first_started) * 1000
                first_offset = first_channel.get_latest_committed_offset_token()
                first_client.close(wait_for_flush=True, timeout_seconds=120)

                second_client = StreamingIngestClient.from_table(
                    client_name=f"{config.schema}_{config.run_tag}_TWO",
                    db_name=config.database,
                    schema_name=config.schema,
                    table_name=config.target_table,
                    profile_json=str(profile_path),
                )
                clients.append(second_client)
                second_channel, _ = second_client.open_channel(config.channel_name)
                channels.append(second_channel)
                reopened_offset = second_channel.get_latest_committed_offset_token()
                if reopened_offset != offset_token(120):
                    raise RuntimeError("streaming channel did not recover its committed offset")

                second_started = time.perf_counter()
                second_channel.append_rows(
                    list(rows[3:]),
                    start_offset_token=offset_token(130),
                    end_offset_token=offset_token(140),
                )
                second_channel.wait_for_commit(
                    _at_least(offset_token(140)),
                    timeout_seconds=120,
                )
                second_elapsed_ms = (time.perf_counter() - second_started) * 1000
                final_offset = second_channel.get_latest_committed_offset_token()

                # Drop the named channel only after the final commit. The table
                # remains the authoritative assertion target until cleanup.
                second_channel.close(drop=True, wait_for_flush=True, timeout_seconds=120)
                second_client.close(wait_for_flush=False)

            row_count, distinct_event_count, structured_payload_count = _snapshot(
                cursor,
                config,
            )
            snapshot = evaluate_streaming_snapshot(
                row_count=row_count,
                distinct_event_count=distinct_event_count,
                latest_offset=final_offset,
            )
            if structured_payload_count != row_count:
                raise ValueError("Snowpipe Streaming VARIANT payload was not structured")
            return {
                "status": snapshot["status"],
                "run_tag": config.run_tag,
                "sdk_version": SDK_VERSION,
                "first_batch_rows": 3,
                "second_batch_rows": 2,
                "first_offset": first_offset,
                "reopened_offset": reopened_offset,
                "final_offset": final_offset,
                "first_batch_ms": round(first_elapsed_ms, 3),
                "second_batch_ms": round(second_elapsed_ms, 3),
                "row_count": row_count,
                "distinct_event_count": distinct_event_count,
                "structured_payload_count": structured_payload_count,
                "cleanup": "completed",
            }
        finally:
            _close_resources(channels, clients)
            if table_created:
                cursor.execute(f"DROP TABLE IF EXISTS {config.qualified_target_table}")
            cursor.close()
    finally:
        connection.close()


def _connect(*, snowflake_connector: Any, query_tag: str) -> Any:
    params: dict[str, object] = {
        "account": _required_env("SNOWFLAKE_ACCOUNT"),
        "user": _required_env("SNOWFLAKE_USER"),
        "authenticator": "SNOWFLAKE_JWT",
        "private_key": _private_key_der(),
        "warehouse": _required_env("SNOWFLAKE_WAREHOUSE"),
        "login_timeout": 20,
        "network_timeout": 60,
        "session_parameters": {"QUERY_TAG": query_tag},
    }
    role = os.environ.get("SNOWFLAKE_ROLE")
    if role:
        params["role"] = role
    return snowflake_connector.connect(**params)


def _write_profile(root: Path, serialization: Any) -> Path:
    key = _private_key_object(serialization)
    key_path = root / "snowflake-key.p8"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    profile = {
        "user": _required_env("SNOWFLAKE_USER"),
        "account": _required_env("SNOWFLAKE_ACCOUNT"),
        "url": os.environ.get(
            "SNOWFLAKE_URL",
            f"https://{_required_env('SNOWFLAKE_ACCOUNT')}.snowflakecomputing.com:443",
        ),
        "private_key_file": str(key_path),
    }
    role = os.environ.get("SNOWFLAKE_ROLE")
    if role:
        profile["role"] = role
    profile_path = root / "profile.json"
    profile_path.write_text(json.dumps(profile, sort_keys=True), encoding="utf-8")
    profile_path.chmod(0o600)
    return profile_path


def _private_key_der() -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = _private_key_object(serialization)
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _private_key_object(serialization: Any) -> Any:
    key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    encoded = os.environ.get("SNOWFLAKE_PRIVATE_KEY_B64")
    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    configured = [bool(key_path), bool(encoded), bool(pem)]
    if sum(configured) != 1:
        raise RuntimeError(
            "set exactly one of SNOWFLAKE_PRIVATE_KEY_PATH, "
            "SNOWFLAKE_PRIVATE_KEY_B64 or SNOWFLAKE_PRIVATE_KEY"
        )
    material = (
        Path(key_path).read_bytes()
        if key_path
        else base64.b64decode(encoded, validate=True)
        if encoded
        else pem.encode("utf-8")
    )
    try:
        return serialization.load_pem_private_key(material, password=None)
    except ValueError:
        return serialization.load_der_private_key(material, password=None)


def _assert_table_absent(cursor: Any, config: StreamingReplayConfig) -> None:
    cursor.execute(
        f"""SELECT TABLE_NAME
FROM {_quoted_identifier(config.database)}.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s""",
        (config.schema, config.target_table),
    )
    if cursor.fetchall():
        raise RuntimeError("streaming run tag already exists in the declared schema")


def _table_ddl(config: StreamingReplayConfig) -> str:
    return f"""CREATE TABLE {config.qualified_target_table} (
    EVENT_ID VARCHAR NOT NULL,
    JOURNAL_RECEIVER VARCHAR NOT NULL,
    JOURNAL_SEQUENCE NUMBER(38, 0) NOT NULL,
    OPERATION VARCHAR NOT NULL,
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL
)"""


def _snapshot(cursor: Any, config: StreamingReplayConfig) -> tuple[int, int, int]:
    cursor.execute(
        f"""SELECT COUNT(*), COUNT(DISTINCT EVENT_ID),
SUM(IFF(TYPEOF(PAYLOAD) = 'OBJECT', 1, 0))
FROM {config.qualified_target_table}"""
    )
    row = cursor.fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def _at_least(expected: str):
    return lambda token: token is not None and token >= expected


def _close_resources(channels: list[Any], clients: list[Any]) -> None:
    for channel in reversed(channels):
        try:
            channel.close(drop=True, wait_for_flush=True, timeout_seconds=120)
        except Exception:
            pass
    for client in reversed(clients):
        try:
            client.close(wait_for_flush=False)
        except Exception:
            pass


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required")
    return value


def _quoted_identifier(value: str) -> str:
    if not value or not value.replace("_", "").replace("$", "").isalnum():
        raise ValueError("unsafe Snowflake identifier")
    return f'"{value}"'


if __name__ == "__main__":
    raise SystemExit(main())
