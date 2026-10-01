#!/usr/bin/env python3
"""Run a synthetic C/U/D business-merge acceptance test in Snowflake DEV.

The command is dry-run by default. Execute mode creates two uniquely named
DEV tables, loads only synthetic rows, runs the guarded business MERGE twice,
checks the expected final snapshot, and drops the two tables before exiting.
Credentials are read only from the process environment and are never printed.
"""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

from quadringent.site_config import current as current_site
from quadringent.snowflake_business_replay import (
    BusinessReplayConfig,
    build_synthetic_events,
    build_technical_image_events,
    evaluate_business_snapshot,
)


def _confirmation(site) -> str:
    return f"{site.destination_schema}_BUSINESS_CUD_{site.fleet_environment}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-tag",
        default=datetime.now(UTC).strftime("CUD%Y%m%d%H%M%S"),
        help="suffixe d'isolement en majuscules",
    )
    parser.add_argument(
        "--fixture",
        choices=("logical", "technical-images"),
        default="logical",
        help="synthetic fixture; technical-images preserves IBM i *BOTH roles",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)

    site = current_site()
    try:
        config = BusinessReplayConfig(
            run_tag=args.run_tag,
            scope=site.snowflake_scope,
            source_library=f"{site.destination_schema}_TEST",
            source_table=f"{site.proof_table}_SIM",
        )
    except ValueError as error:
        parser.error(str(error))

    events, expected_sequence = _fixture(args.fixture, config)

    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "database": config.database,
                    "schema": config.schema,
                    "canonical_table": config.canonical_table,
                    "target_table": config.target_table,
                    "source_library": config.source_library,
                    "source_table": config.source_table,
                    "key_columns": list(config.key_columns),
                    "fixture": args.fixture,
                    "synthetic_event_count": len(events),
                    "confirmation": _confirmation(site),
                    "cleanup": "always_after_execute",
                },
                sort_keys=True,
            )
        )
        return 0

    if args.confirm != _confirmation(site):
        print("ERROR: exact confirmation is required", file=sys.stderr)
        return 2

    try:
        connection = _connect(
            query_tag=f"{config.schema}_BUSINESS_CUD_{config.run_tag}"
        )
        try:
            metrics = _execute_replay(connection, config, events, expected_sequence)
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

    print(json.dumps(metrics, sort_keys=True))
    return 0 if metrics.get("status") == "PASS" else 1


def _execute_replay(
    connection: Any,
    config: BusinessReplayConfig,
    events: tuple[dict[str, Any], ...],
    expected_sequence: int,
) -> dict[str, object]:
    cursor = connection.cursor()
    target_expected = False
    canonical_created = False
    try:
        _assert_tables_absent(cursor, config)
        cursor.execute(_canonical_ddl(config))
        canonical_created = True
        _insert_synthetic_events(cursor, config, events)
        cursor.execute(f"SELECT COUNT(*) FROM {config.plan.qualified_canonical_table}")
        canonical_rows = int(cursor.fetchone()[0])

        target_expected = True
        config.plan.execute(cursor)
        first_snapshot = evaluate_business_snapshot(
            _snapshot(cursor, config), expected_sequence=expected_sequence
        )
        first_target_rows = _count(cursor, config.plan.qualified_target_table)

        config.plan.execute(cursor)
        second_snapshot = evaluate_business_snapshot(
            _snapshot(cursor, config), expected_sequence=expected_sequence
        )
        second_target_rows = _count(cursor, config.plan.qualified_target_table)
        if first_snapshot != second_snapshot or first_target_rows != second_target_rows:
            raise ValueError("business replay is not idempotent")
        return {
            "status": "PASS",
            "run_tag": config.run_tag,
            "canonical_rows": canonical_rows,
            "target_rows_first": first_target_rows,
            "target_rows_second": second_target_rows,
            "first_snapshot": first_snapshot,
            "second_snapshot": second_snapshot,
            "cleanup": "completed",
        }
    finally:
        if target_expected:
            cursor.execute(f"DROP TABLE IF EXISTS {config.plan.qualified_target_table}")
        if canonical_created:
            cursor.execute(f"DROP TABLE IF EXISTS {config.plan.qualified_canonical_table}")
        cursor.close()


def _assert_tables_absent(cursor: Any, config: BusinessReplayConfig) -> None:
    database = _identifier(config.database)
    cursor.execute(
        f"""SELECT TABLE_NAME
FROM {database}.INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = %s
  AND TABLE_NAME IN (%s, %s)""",
        (config.schema, config.canonical_table, config.target_table),
    )
    if cursor.fetchall():
        raise RuntimeError("run tag already exists in DEV schema")


def _canonical_ddl(config: BusinessReplayConfig) -> str:
    return f"""CREATE TABLE {config.plan.qualified_canonical_table} (
    EVENT_ID VARCHAR NOT NULL,
    JOURNAL_RECEIVER VARCHAR NOT NULL,
    JOURNAL_SEQUENCE NUMBER(38, 0) NOT NULL,
    OPERATION VARCHAR NOT NULL,
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL,
    INGESTED_AT TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
)"""


def _insert_synthetic_events(
    cursor: Any,
    config: BusinessReplayConfig,
    events: tuple[dict[str, Any], ...],
) -> None:
    statement = f"""INSERT INTO {config.plan.qualified_canonical_table} (
    EVENT_ID, JOURNAL_RECEIVER, JOURNAL_SEQUENCE, OPERATION, PAYLOAD, SOURCE_FILE
) SELECT %s, %s, %s, %s, PARSE_JSON(%s), %s"""
    for event in events:
        cursor.execute(
            statement,
            (
                event["event_id"],
                event["journal_receiver"],
                event["journal_sequence"],
                event["operation"],
                json.dumps(event, separators=(",", ":"), ensure_ascii=False),
                f"synthetic/{config.run_tag}/{event['event_id']}.jsonl",
            ),
        )


def _fixture(name: str, config: BusinessReplayConfig) -> tuple[tuple[dict[str, Any], ...], int]:
    if name == "logical":
        return (
            build_synthetic_events(
                source_library=config.source_library,
                source_table=config.source_table,
            ),
            120,
        )
    if name == "technical-images":
        return (
            build_technical_image_events(
                source_library=config.source_library,
                source_table=config.source_table,
            ),
            121,
        )
    raise ValueError(f"unknown fixture: {name}")


def _snapshot(cursor: Any, config: BusinessReplayConfig) -> list[tuple[Any, ...]]:
    cursor.execute(
        f"""SELECT
    BUSINESS_KEY:ID::VARCHAR,
    ROW_DATA:VALUE::VARCHAR,
    JOURNAL_SEQUENCE,
    LAST_OPERATION
FROM {config.plan.qualified_target_table}
ORDER BY 1"""
    )
    return list(cursor.fetchall())


def _count(cursor: Any, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cursor.fetchone()[0])


def _connect(*, query_tag: str) -> Any:
    try:
        import snowflake.connector
        from cryptography.hazmat.primitives import serialization
    except ImportError as error:
        raise RuntimeError("snowflake-connector-python and cryptography are required") from error

    params: dict[str, object] = {
        "account": _required_env("SNOWFLAKE_ACCOUNT"),
        "user": _required_env("SNOWFLAKE_USER"),
        "authenticator": "SNOWFLAKE_JWT",
        "private_key": _private_key(serialization),
        "warehouse": _required_env("SNOWFLAKE_WAREHOUSE"),
        "login_timeout": 20,
        "network_timeout": 60,
        "session_parameters": {"QUERY_TAG": query_tag},
    }
    role = os.environ.get("SNOWFLAKE_ROLE")
    if role:
        params["role"] = role
    return snowflake.connector.connect(**params)


def _private_key(serialization: Any) -> bytes:
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
        else base64.b64decode(encoded)
        if encoded
        else pem.encode("utf-8")
    )
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


def _identifier(value: str) -> str:
    if not value or not value.replace("_", "").replace("$", "").isalnum():
        raise ValueError("invalid database identifier")
    return f'"{value}"'


if __name__ == "__main__":
    raise SystemExit(main())
