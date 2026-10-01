from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence

from .site_config import SnowflakeScope


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_STAGE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_OBJECT_KEYS = 1000


def assert_declared_destination(
    scope: SnowflakeScope, database: str, schema: str, *identifiers: str
) -> None:
    """Refuse any value escaping the declared Snowflake destination scope.

    ``scope`` carries the site-declared database/schema pair and the forbidden
    identifier fragments (incumbent markers). Nothing is pinned to a literal
    installation here — the check compares against the declared value only.
    """

    if not isinstance(scope, SnowflakeScope):
        raise ValueError("a declared Snowflake destination scope is required")
    if database != scope.database:
        raise ValueError(
            "destination database must equal the declared site database"
        )
    if schema != scope.schema:
        raise ValueError("destination schema must equal the declared site schema")
    for value in (database, schema, *identifiers):
        if any(
            fragment.upper() in value.upper()
            for fragment in scope.forbidden_fragments
        ):
            raise ValueError(
                "destination objects must not carry a forbidden fragment"
            )


@dataclass(frozen=True)
class SnowflakeRawLoadPlan:
    """SQL-only plan for the first Snowflake landing layer.

    It intentionally loads immutable JSON events into a VARIANT raw table. A
    later, separately tested MERGE can materialize a business table once the
    IBM i before/after contract is approved. No connection or credential is
    created by this class.
    """

    scope: SnowflakeScope
    table: str
    stage: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        for name, value in (
            ("table", self.table),
            ("stage", self.stage),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"invalid Snowflake identifier: {name}")
        assert_declared_destination(
            self.scope, self.database, self.schema, self.table, self.stage
        )

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    def statements_for(self, object_key: str) -> tuple[str, str]:
        path = _stage_path(object_key)
        table = self.qualified_table
        stage = "@" + self.qualified_stage
        create_table = f"""CREATE TABLE IF NOT EXISTS {table} (
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL,
    INGESTED_AT TIMESTAMP_LTZ NOT NULL
)"""
        copy_into = f"""COPY INTO {table} (PAYLOAD, SOURCE_FILE, INGESTED_AT)
FROM (
    SELECT $1, METADATA$FILENAME, CURRENT_TIMESTAMP()
    FROM {stage}/{path}
)
FILE_FORMAT = (TYPE = JSON STRIP_OUTER_ARRAY = FALSE)
ON_ERROR = 'ABORT_STATEMENT'"""
        return create_table, copy_into

    def statements_for_all_jsonl(self) -> tuple[str, str]:
        """Load every JSONL payload below the already-bounded stage root."""

        create_table, _ = self.statements_for("placeholder.jsonl")
        copy_into = f"""COPY INTO {self.qualified_table} (PAYLOAD, SOURCE_FILE, INGESTED_AT)
FROM (
    SELECT $1, METADATA$FILENAME, CURRENT_TIMESTAMP()
    FROM @{self.qualified_stage}
)
PATTERN = '.*[.]jsonl$'
FILE_FORMAT = (TYPE = JSON STRIP_OUTER_ARRAY = FALSE)
ON_ERROR = 'ABORT_STATEMENT'"""
        return create_table, copy_into

    def statements_for_object_keys(self, object_keys: Sequence[str]) -> tuple[str, str]:
        """Load an explicit JSONL list already bounded to the stage root."""

        paths = _object_key_list(object_keys)
        create_table, _ = self.statements_for(paths[0])
        files_sql = ", ".join(_sql_string(path) for path in paths)
        copy_into = f"""COPY INTO {self.qualified_table} (PAYLOAD, SOURCE_FILE, INGESTED_AT)
FROM (
    SELECT $1, METADATA$FILENAME, CURRENT_TIMESTAMP()
    FROM @{self.qualified_stage}
)
FILES = ({files_sql})
FILE_FORMAT = (TYPE = JSON STRIP_OUTER_ARRAY = FALSE)
ON_ERROR = 'ABORT_STATEMENT'"""
        return create_table, copy_into

    def execute(self, cursor: Any, object_key: str) -> None:
        for statement in self.statements_for(object_key):
            cursor.execute(statement)

    @property
    def qualified_table(self) -> str:
        return _qualified(self.database, self.schema, self.table)

    @property
    def qualified_stage(self) -> str:
        return _qualified(self.database, self.schema, self.stage)


@dataclass(frozen=True)
class SnowflakeCanonicalLoadPlan:
    """SQL-only plan for an idempotent canonical event ledger.

    The canonical table stores one technical row per journal event. It does
    not pretend to materialize a business table: a later consumer can apply
    the ``c/u/d`` images after the IBM i key contract has been approved. The
    ``MERGE`` is keyed by the deterministic ``event_id`` carried in the raw
    envelope, so reloading one object is a no-op; its source keeps one row
    per ``event_id`` so a replayed batch loaded in the same pass (the same
    event in two files) cannot insert twice. The technical operation
    roles ``u_before`` and ``u_after`` are retained in the canonical ledger;
    business materialization handles them in a later guarded merge.
    """

    scope: SnowflakeScope
    raw_table: str
    canonical_table: str
    stage: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        for name, value in (
            ("raw_table", self.raw_table),
            ("canonical_table", self.canonical_table),
            ("stage", self.stage),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"invalid Snowflake identifier: {name}")
        assert_declared_destination(
            self.scope,
            self.database,
            self.schema,
            self.raw_table,
            self.canonical_table,
            self.stage,
        )

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    @property
    def qualified_canonical_table(self) -> str:
        return _qualified(self.database, self.schema, self.canonical_table)

    @property
    def qualified_table(self) -> str:
        return _qualified(self.database, self.schema, self.raw_table)

    @property
    def qualified_stage(self) -> str:
        return _qualified(self.database, self.schema, self.stage)

    def statements_for(self, object_key: str) -> tuple[str, str, str, str]:
        path = _stage_path(object_key)
        raw_plan = SnowflakeRawLoadPlan(
            scope=self.scope,
            table=self.raw_table,
            stage=self.stage,
        )
        create_raw, copy_into = raw_plan.statements_for(path)
        canonical = _qualified(self.database, self.schema, self.canonical_table)
        create_canonical = f"""CREATE TABLE IF NOT EXISTS {canonical} (
    EVENT_ID VARCHAR NOT NULL,
    JOURNAL_RECEIVER VARCHAR NOT NULL,
    JOURNAL_SEQUENCE NUMBER(38, 0) NOT NULL,
    OPERATION VARCHAR NOT NULL,
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL,
    INGESTED_AT TIMESTAMP_LTZ NOT NULL
)"""
        merge = f"""MERGE INTO {canonical} AS target
USING (
    SELECT
        PAYLOAD:event_id::VARCHAR AS EVENT_ID,
        PAYLOAD:journal_receiver::VARCHAR AS JOURNAL_RECEIVER,
        PAYLOAD:journal_sequence::NUMBER(38, 0) AS JOURNAL_SEQUENCE,
        PAYLOAD:operation::VARCHAR AS OPERATION,
        PAYLOAD AS PAYLOAD,
        SOURCE_FILE,
        INGESTED_AT
    FROM {raw_plan.qualified_table}
    WHERE SOURCE_FILE = '{path}'
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY PAYLOAD:event_id::VARCHAR
        ORDER BY INGESTED_AT, SOURCE_FILE
    ) = 1
) AS source
ON target.EVENT_ID = source.EVENT_ID
WHEN NOT MATCHED THEN INSERT (
    EVENT_ID, JOURNAL_RECEIVER, JOURNAL_SEQUENCE, OPERATION,
    PAYLOAD, SOURCE_FILE, INGESTED_AT
)
VALUES (
    source.EVENT_ID, source.JOURNAL_RECEIVER, source.JOURNAL_SEQUENCE,
    source.OPERATION, source.PAYLOAD, source.SOURCE_FILE, source.INGESTED_AT
)"""
        return create_raw, copy_into, create_canonical, merge

    def statements_for_all_jsonl(self) -> tuple[str, str, str, str]:
        """Load and merge the complete JSONL capture, excluding JSON metadata."""

        raw_plan = SnowflakeRawLoadPlan(
            scope=self.scope,
            table=self.raw_table,
            stage=self.stage,
        )
        create_raw, copy_into = raw_plan.statements_for_all_jsonl()
        canonical = self.qualified_canonical_table
        create_canonical = f"""CREATE TABLE IF NOT EXISTS {canonical} (
    EVENT_ID VARCHAR NOT NULL,
    JOURNAL_RECEIVER VARCHAR NOT NULL,
    JOURNAL_SEQUENCE NUMBER(38, 0) NOT NULL,
    OPERATION VARCHAR NOT NULL,
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL,
    INGESTED_AT TIMESTAMP_LTZ NOT NULL
)"""
        merge = f"""MERGE INTO {canonical} AS target
USING (
    SELECT
        PAYLOAD:event_id::VARCHAR AS EVENT_ID,
        PAYLOAD:journal_receiver::VARCHAR AS JOURNAL_RECEIVER,
        PAYLOAD:journal_sequence::NUMBER(38, 0) AS JOURNAL_SEQUENCE,
        PAYLOAD:operation::VARCHAR AS OPERATION,
        PAYLOAD AS PAYLOAD,
        SOURCE_FILE,
        INGESTED_AT
    FROM {raw_plan.qualified_table}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY PAYLOAD:event_id::VARCHAR
        ORDER BY INGESTED_AT, SOURCE_FILE
    ) = 1
) AS source
ON target.EVENT_ID = source.EVENT_ID
WHEN NOT MATCHED THEN INSERT (
    EVENT_ID, JOURNAL_RECEIVER, JOURNAL_SEQUENCE, OPERATION,
    PAYLOAD, SOURCE_FILE, INGESTED_AT
)
VALUES (
    source.EVENT_ID, source.JOURNAL_RECEIVER, source.JOURNAL_SEQUENCE,
    source.OPERATION, source.PAYLOAD, source.SOURCE_FILE, source.INGESTED_AT
)"""
        return create_raw, copy_into, create_canonical, merge

    def statements_for_object_keys(
        self, object_keys: Sequence[str]
    ) -> tuple[str, str, str, str]:
        """Load and merge an explicit JSONL list, excluding JSON metadata."""

        raw_plan = SnowflakeRawLoadPlan(
            scope=self.scope,
            table=self.raw_table,
            stage=self.stage,
        )
        create_raw, copy_into = raw_plan.statements_for_object_keys(object_keys)
        _, _, create_canonical, merge = self.statements_for_all_jsonl()
        return create_raw, copy_into, create_canonical, merge

    def execute(self, cursor: Any, object_key: str) -> None:
        for statement in self.statements_for(object_key):
            cursor.execute(statement)


@dataclass(frozen=True)
class SnowflakeAutonomousLoadPlan:
    """Snowflake-managed continuous load for the dedicated DEV SALE lane.

    The capture runtime only writes immutable JSONL to S3. Snowpipe loads new
    files into a stable raw ledger and a zero-refresh view exposes one technical
    row per deterministic event id. No Snowflake credential is injected into
    the IBM i capture pod.
    """

    scope: SnowflakeScope
    stage: str
    raw_table: str
    canonical_table: str
    pipe: str
    warehouse: str
    manage_warehouse: bool = True

    def __post_init__(self) -> None:
        if type(self.manage_warehouse) is not bool:
            raise ValueError("manage_warehouse must be a boolean")
        if not isinstance(self.scope, SnowflakeScope):
            raise ValueError("a declared Snowflake destination scope is required")
        for name, value in (
            ("stage", self.stage),
            ("raw_table", self.raw_table),
            ("canonical_table", self.canonical_table),
            ("pipe", self.pipe),
            ("warehouse", self.warehouse),
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"invalid Snowflake identifier: {name}")
        assert_declared_destination(
            self.scope,
            self.database,
            self.schema,
            self.stage,
            self.raw_table,
            self.canonical_table,
            self.pipe,
            self.warehouse,
        )

    @property
    def database(self) -> str:
        return self.scope.database

    @property
    def schema(self) -> str:
        return self.scope.schema

    def statements(self) -> tuple[str, ...]:
        raw = _qualified(self.database, self.schema, self.raw_table)
        stage = _qualified(self.database, self.schema, self.stage)
        pipe = _qualified(self.database, self.schema, self.pipe)
        canonical = _qualified(self.database, self.schema, self.canonical_table)
        warehouse = f'"{self.warehouse}"'
        create_warehouse = f"""CREATE WAREHOUSE IF NOT EXISTS {warehouse}
WITH
    WAREHOUSE_SIZE = 'XSMALL'
    WAREHOUSE_TYPE = 'STANDARD'
    MIN_CLUSTER_COUNT = 1
    MAX_CLUSTER_COUNT = 1
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
COMMENT = 'Quadringent isolated DEV canonical verification'"""
        create_raw = f"""CREATE TABLE IF NOT EXISTS {raw} (
    PAYLOAD VARIANT NOT NULL,
    SOURCE_FILE VARCHAR NOT NULL,
    SOURCE_ROW_NUMBER NUMBER(38, 0) NOT NULL,
    INGESTED_AT TIMESTAMP_LTZ NOT NULL
)"""
        create_pipe = f"""CREATE OR ALTER PIPE {pipe}
AUTO_INGEST = TRUE
AS
COPY INTO {raw} (PAYLOAD, SOURCE_FILE, SOURCE_ROW_NUMBER, INGESTED_AT)
FROM (
    SELECT
        $1,
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER,
        METADATA$START_SCAN_TIME
    FROM @{stage}
)
PATTERN = '.*[.]jsonl$'
FILE_FORMAT = (TYPE = JSON STRIP_OUTER_ARRAY = FALSE)"""
        create_canonical = (
            f"CREATE OR REPLACE VIEW {canonical} AS\n"
            + self._canonical_query(raw)
        )
        resume_pipe = f"ALTER PIPE {pipe} SET PIPE_EXECUTION_PAUSED = FALSE"
        suspend_warehouse = f"ALTER WAREHOUSE IF EXISTS {warehouse} SUSPEND"
        if not self.manage_warehouse:
            return create_raw, create_pipe, create_canonical, resume_pipe
        return (
            create_warehouse,
            create_raw,
            create_pipe,
            create_canonical,
            resume_pipe,
            suspend_warehouse,
        )

    def pause_statements(self) -> tuple[str, ...]:
        pipe = _qualified(self.database, self.schema, self.pipe)
        warehouse = f'"{self.warehouse}"'
        if not self.manage_warehouse:
            return (f"ALTER PIPE IF EXISTS {pipe} SET PIPE_EXECUTION_PAUSED = TRUE",)
        return (
            f"ALTER PIPE IF EXISTS {pipe} SET PIPE_EXECUTION_PAUSED = TRUE",
            f"ALTER WAREHOUSE IF EXISTS {warehouse} SUSPEND",
        )

    def drop_legacy_canonical_statement(self) -> str:
        canonical = _qualified(self.database, self.schema, self.canonical_table)
        return f"DROP DYNAMIC TABLE {canonical}"

    def drop_canonical_view_statement(self) -> str:
        canonical = _qualified(self.database, self.schema, self.canonical_table)
        return f"DROP VIEW IF EXISTS {canonical}"

    def restore_legacy_canonical_statements(self) -> tuple[str, str]:
        """Recreate the previous refresh model only as migration rollback."""

        raw = _qualified(self.database, self.schema, self.raw_table)
        canonical = _qualified(self.database, self.schema, self.canonical_table)
        warehouse = f'"{self.warehouse}"'
        create = f"""CREATE OR ALTER DYNAMIC TABLE {canonical}
TARGET_LAG = '1 minute'
WAREHOUSE = {warehouse}
REFRESH_MODE = INCREMENTAL
INITIALIZE = ON_SCHEDULE
AS
{self._canonical_query(raw)}"""
        return create, f"ALTER DYNAMIC TABLE {canonical} SUSPEND"

    @staticmethod
    def _canonical_query(raw: str) -> str:
        return f"""SELECT
    PAYLOAD:event_id::VARCHAR AS EVENT_ID,
    PAYLOAD:journal_receiver::VARCHAR AS JOURNAL_RECEIVER,
    PAYLOAD:journal_sequence::NUMBER(38, 0) AS JOURNAL_SEQUENCE,
    PAYLOAD:operation::VARCHAR AS OPERATION,
    PAYLOAD AS PAYLOAD,
    SOURCE_FILE,
    SOURCE_ROW_NUMBER,
    INGESTED_AT
FROM {raw}
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY EVENT_ID
    ORDER BY JOURNAL_SEQUENCE DESC, INGESTED_AT DESC, SOURCE_FILE DESC
) = 1"""


def _qualified(*parts: str) -> str:
    return ".".join(f'"{part}"' for part in parts)


def _stage_path(object_key: str) -> str:
    if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
        raise ValueError("stage object key must stay below the configured prefix")
    parts = object_key.split("/")
    if any(not _STAGE_PATH_PART.fullmatch(part) for part in parts):
        raise ValueError("stage object key contains an unsafe path component")
    return object_key


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _object_key_list(object_keys: Sequence[str]) -> tuple[str, ...]:
    if not object_keys:
        raise ValueError("stage object key list must not be empty")
    if len(object_keys) > _MAX_OBJECT_KEYS:
        raise ValueError("stage object key list exceeds the COPY FILES limit")
    paths = tuple(_stage_path(key) for key in object_keys)
    if len(set(paths)) != len(paths):
        raise ValueError("stage object key list must not contain duplicates")
    return paths
