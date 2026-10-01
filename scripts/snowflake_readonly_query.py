#!/usr/bin/env python3
"""Read-only Snowflake reader for isochrone comparison.

Refuses any statement that is not a SELECT/SHOW/DESCRIBE, so it cannot
create, copy, merge or drop anything. Credentials come from AWS Secrets
Manager and are never printed. Row payloads are never printed either: the
caller receives only the columns it asks for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from typing import Any, Sequence

ALLOWED = re.compile(r"^\s*(SELECT|WITH|SHOW|DESC|DESCRIBE)\b", re.IGNORECASE)
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|COPY|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|PUT|REMOVE|CALL|EXECUTE)\b",
    re.IGNORECASE,
)
def _site():
    from quadringent.site_config import current  # noqa: PLC0415

    return current()


def _secret_id() -> str:
    """Secret AWS Secrets Manager du site : jamais de valeur par défaut."""

    value = os.environ.get("QUADRINGENT_SNOWFLAKE_SECRET_ID", "").strip()
    if not value:
        raise SystemExit("ERROR: QUADRINGENT_SNOWFLAKE_SECRET_ID is required")
    return value


def assert_read_only(sql: str) -> None:
    if not ALLOWED.match(sql):
        raise ValueError("only SELECT/WITH/SHOW/DESCRIBE statements are allowed")
    if FORBIDDEN.search(sql):
        raise ValueError("statement contains a write keyword")
    if ";" in sql.strip().rstrip(";"):
        raise ValueError("multiple statements are not allowed")


def _load_credentials(profile: str, region: str) -> tuple[str, str]:
    result = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", _secret_id(),
         "--region", region, "--query", "SecretString", "--output", "text"],
        env={**os.environ, "AWS_PROFILE": profile},
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    return payload["username"], payload["private_key_pem"]


def connect(*, profile: str | None = None, region: str | None = None,
            role: str, warehouse: str,
            query_tag: str | None = None):
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    site = _site()
    profile = profile or site.aws_profile or ""
    region = region or site.aws_region
    query_tag = query_tag or f"{site.destination_schema}_ISOCHRONE_READONLY"
    username, private_key_pem = _load_credentials(profile, region)
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("ascii"), password=None
    ).private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return snowflake.connector.connect(
        account=site.snowflake_account,
        user=username,
        authenticator="SNOWFLAKE_JWT",
        private_key=private_key,
        role=role,
        warehouse=warehouse,
        login_timeout=20,
        network_timeout=60,
        session_parameters={"QUERY_TAG": query_tag},
    )


def run_query(connection, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    assert_read_only(sql)
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params or ())
        return cursor.fetchall()
    finally:
        cursor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--max-rows", type=int, default=50)
    args = parser.parse_args()

    try:
        assert_read_only(args.sql)
    except ValueError as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}))
        return 2

    connection = connect(role=args.role, warehouse=args.warehouse)
    try:
        rows = run_query(connection, args.sql)
    except Exception as error:
        print(json.dumps({"status": "ERROR", "error_type": type(error).__name__,
                          "error_code": getattr(error, "errno", None)}))
        return 1
    finally:
        connection.close()

    print(json.dumps({"status": "OK", "row_count": len(rows),
                      "rows": [list(map(_safe, r)) for r in rows[: args.max_rows]]},
                     indent=2, default=str))
    return 0


def _safe(value: Any) -> Any:
    return value if isinstance(value, (int, float, str, bool, type(None))) else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
