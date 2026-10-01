"""Snowflake adapter for one isolated qualification run.

The product's SQL loader consumes receipted raw batches and materializes the
history/mirror. This adapter only supplies its declared scope and credentials;
it does not implement a second replay path. Readback reconstructs before and
after images from the actual flattened history rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import UUID

from quadringent.object_store import read_published_batch
from quadringent.storage_backend import StorageBackend as ProductStorageBackend
from quadringent.storage_layout import journal_prefix, snapshot_prefix
from scripts.quadringent_destination_loader import (
    LoaderTable, _connect_snowflake, _snowflake_role_to_warehouse, build_plan, run_once,
)
from quadringent_control_plane.v2.executor.evidence import EvidenceReader, evidence_key

from .adapters import RawReplayEvidence
from .config import RunConfig


_SAFE_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,127}\Z")
_MAX_SECRET_BYTES = 128 * 1024
_MAX_EVENTS = 5000
_MAX_RECEIPTS = 5000
_MAX_RECEIPT_BYTES = 65_536


@dataclass(frozen=True, repr=False)
class _Credential:
    account: str
    user: str
    role: str
    private_key_pem: str = field(repr=False)


def _load_credential(path_text: str) -> _Credential:
    path = Path(path_text)
    if not path.is_absolute() or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Snowflake credential must be an absolute private file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_size > _MAX_SECRET_BYTES):
                raise ValueError("Snowflake credential must be a private owned file")
            payload = json.loads(handle.read(_MAX_SECRET_BYTES + 1))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Snowflake credential is unreadable or invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("Snowflake credential is invalid")
    account, user, role, key = (
        payload.get("account"), payload.get("user"),
        payload.get("role"), payload.get("private_key_pem"),
    )
    if (not isinstance(account, str) or _SAFE_ACCOUNT.fullmatch(account) is None
            or not isinstance(user, str) or _SAFE_IDENTIFIER.fullmatch(user) is None
            or not isinstance(role, str) or _SAFE_IDENTIFIER.fullmatch(role) is None
            or not role.startswith("QDT_ROLE_")
            or not isinstance(key, str) or not key or len(key) > _MAX_SECRET_BYTES):
        raise ValueError("Snowflake credential is invalid")
    return _Credential(account, user, role, key)


def qualification_table_id(config: RunConfig) -> str:
    if _SAFE_RUN_ID.fullmatch(config.run_id) is None:
        raise ValueError("qualification run_id is invalid")
    try:
        if str(UUID(config.run_id)) != config.run_id:
            raise ValueError
    except ValueError:
        raise ValueError("real qualification run_id must be a canonical UUID") from None
    return f"qual-{config.run_id}"


def qualification_schema_suffix(run_id: str) -> str:
    return "_" + hashlib.sha256(run_id.encode("ascii")).hexdigest()[:12].upper()


def _product_storage(config: RunConfig) -> ProductStorageBackend:
    if config.storage.backend not in {"s3", "gcs"}:
        raise ValueError("qualification storage backend must be s3 or gcs")
    location = config.storage.checkpoint_location
    if not isinstance(location, str) or not location.strip():
        raise ValueError("qualification checkpoint location is required")
    env = {
        "QUADRINGENT_STORAGE_BACKEND": "aws" if config.storage.backend == "s3" else "gcs",
        "AS400_RAW_BUCKET": config.storage.bucket,
        "AS400_CHECKPOINT_TABLE" if config.storage.backend == "s3" else "AS400_CHECKPOINT_BUCKET": location,
    }
    return ProductStorageBackend.from_environment(env)


class SnowflakeQualificationWarehouse:
    """``WarehouseLoader`` using the product loader and actual history rows."""

    def __init__(
        self,
        config: RunConfig,
        *,
        storage_backend: Any | None = None,
        connection_factory: Callable[..., Any] = _connect_snowflake,
        loader: Callable[..., Any] = run_once,
    ) -> None:
        if config.warehouse.loader != "snowflake":
            raise ValueError("qualification warehouse loader must be snowflake")
        if not isinstance(config.storage.checkpoint_location, str) or not config.storage.checkpoint_location.strip():
            raise ValueError("qualification checkpoint location is required")
        prefix = config.storage.raw_prefix
        if (not isinstance(prefix, str) or not prefix or "\\" in prefix
                or any(ord(char) < 32 for char in prefix)
                or PurePosixPath(prefix).is_absolute() or ".." in PurePosixPath(prefix).parts
                or str(PurePosixPath(prefix)) != prefix or prefix.split("/")[-1] != config.run_id):
            raise ValueError("qualification raw prefix must end with run_id")
        library, table_name = config.table.qualified_name.split(".", 1)
        table_id = qualification_table_id(config)
        if not config.warehouse.schema_name.upper().endswith(qualification_schema_suffix(config.run_id)):
            raise ValueError("qualification destination schema must be isolated by run_id")
        self.table = LoaderTable(
            table_id=table_id,
            schema_name=library,
            table_name=table_name,
            key_columns=(config.table.primary_key,),
            columns=tuple({
                "name": column.name,
                "kind": column.kind,
                "length": column.length,
                "precision": column.precision,
                "scale": column.scale,
                "timestamp_precision": column.timestamp_precision,
            } for column in config.table.columns),
            evidence_key=evidence_key(config.storage.raw_prefix, table_id, config.run_id),
        )
        self.plan = build_plan(
            self.table, database=config.warehouse.database, schema=config.warehouse.schema_name,
        )
        self._config = config
        self._credential = _load_credential(config.warehouse.account_secret_file)
        self._warehouse = _snowflake_role_to_warehouse(self._credential.role)
        self._storage = storage_backend if storage_backend is not None else _product_storage(config)
        self._connect = connection_factory
        self._loader = loader

    @contextmanager
    def _cursor(self, *, timeout_seconds: int | None = None) -> Iterator[Any]:
        try:
            connection = self._connect(
                account=self._credential.account, user=self._credential.user,
                role=self._credential.role, private_key_pem=self._credential.private_key_pem,
                warehouse=self._warehouse,
                **({"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}),
            )
        except Exception:
            raise RuntimeError("Snowflake qualification connection failed") from None
        try:
            cursor = connection.cursor()
            try:
                yield cursor
            finally:
                cursor.close()
        finally:
            connection.close()

    def load(self, *, raw_prefix: str) -> None:
        if raw_prefix != self._config.storage.raw_prefix:
            raise ValueError("qualification run prefix differs from configured prefix")
        with self._cursor() as cursor:
            self._loader(
                storage=self._storage, tables=(self.table,), raw_prefix_root=raw_prefix,
                database=self.plan.database, schema=self.plan.schema,
                streaming_client_factory=None, cursor=cursor,
                history_mode="sql", measure_lag=False,
            )

    def _require_schema(self, schema: str) -> None:
        if schema != self.plan.schema:
            raise ValueError("qualification schema differs from declared destination")

    def fetch_raw_counts(self, *, schema: str) -> tuple[int, int]:
        """Count validated raw rows and distinct IDs from the run's published batches."""
        evidence = self.fetch_raw_evidence(schema=schema)
        return evidence.raw_rows, evidence.raw_distinct_events

    def fetch_raw_evidence(self, *, schema: str) -> RawReplayEvidence:
        """Compare le contenu canonique complet derrière chaque identité brute.

        Un lot peut être référencé par plusieurs reçus : chacune de ces
        relectures compte, même si le chargeur l'a déjà dédupliquée.
        """
        self._require_schema(schema)
        root = self._config.storage.raw_prefix
        snapshot_store = self._storage.object_store(snapshot_prefix(root, self.table.table_name))
        evidence = EvidenceReader(self._storage.object_store("")).read(self.table.evidence_key)
        if evidence is None or evidence.table_id != self.table.table_id or evidence.run_id != self._config.run_id:
            raise ValueError("qualification snapshot evidence is absent or mismatched")
        first_content: dict[str, str] = {}
        identical_ids: set[str] = set()
        divergent_ids: set[str] = set()
        raw_rows = identical = divergent = 0

        def observe(event: Any) -> None:
            nonlocal raw_rows, identical, divergent
            raw_rows += 1
            if raw_rows > _MAX_EVENTS:
                raise ValueError("qualification raw rows exceed budget")
            content = json.dumps(event.to_record(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            previous = first_content.get(event.event_id)
            if previous is None:
                first_content[event.event_id] = content
            elif previous == content:
                identical += 1
                identical_ids.add(event.event_id)
            else:
                divergent += 1
                divergent_ids.add(event.event_id)

        for ref in evidence.snapshot_batches:
            batch = read_published_batch(snapshot_store, ref.payload_key, ref.manifest_key)
            if any(
                event.library.upper() != self.table.schema_name.upper()
                or event.table.upper() != self.table.table_name.upper()
                or event.position.receiver != f"SNAPSHOT:{self._config.run_id}"
                for event in batch.events
            ):
                raise ValueError("qualification snapshot batch escapes declared run and table")
            for event in batch.events:
                observe(event)
        if raw_rows != evidence.rows_copied:
            raise ValueError("qualification snapshot evidence row count differs from raw")

        # The single-table reader writes receipts below the table's journal
        # prefix. Fleet readers write combined receipts at the run root. A
        # qualification run is one table, but both layouts can be inspected
        # without assuming which mode its capture adapter selected.
        for prefix, fleet in ((journal_prefix(root, self.table.table_name), False), (root, True)):
            store = self._storage.object_store(prefix)
            for key in store.list_receipt_keys(_MAX_RECEIPTS):
                receipt = json.loads(store.get_bounded(key, _MAX_RECEIPT_BYTES))
                raw = receipt.get("raw")
                if raw is None:
                    continue
                if (not isinstance(raw, dict) or not isinstance(raw.get("payload_key"), str)
                        or not isinstance(raw.get("manifest_key"), str)):
                    raise ValueError("qualification journal receipt has invalid raw reference")
                batch = read_published_batch(store, raw["payload_key"], raw["manifest_key"])
                if not fleet and any(
                    event.library.upper() != self.table.schema_name.upper()
                    or event.table.upper() != self.table.table_name.upper()
                    for event in batch.events
                ):
                    raise ValueError("qualification journal batch escapes declared table")
                for event in batch.events:
                    if not fleet or (
                        event.library.upper() == self.table.schema_name.upper()
                        and event.table.upper() == self.table.table_name.upper()
                    ):
                        observe(event)
        return RawReplayEvidence(raw_rows, len(first_content), identical, divergent,
                                 tuple(sorted(identical_ids)), tuple(sorted(divergent_ids)))

    def fetch_mirror_value(self, *, schema: str, row_key: int, column: str,
                           timeout_seconds: int) -> Any | None:
        self._require_schema(schema)
        if (type(row_key) is not int or column not in self._config.table.column_names
                or type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 10):
            raise ValueError("qualification mirror probe selector or timeout is invalid")
        with self._cursor(timeout_seconds=timeout_seconds) as cursor:
            cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}")
            cursor.execute(
                f"SELECT {column} FROM {self.plan.qualified_mirror_table} "
                f"WHERE {self._config.table.primary_key} = %s LIMIT 2", (row_key,),
            )
            rows = cursor.fetchall()
        if len(rows) > 1 or any(len(row) != 1 for row in rows):
            raise ValueError("qualification mirror probe is ambiguous")
        return rows[0][0] if rows else None

    def fetch_mirror_rows(self, *, schema: str) -> Sequence[Mapping[str, Any]]:
        self._require_schema(schema)
        columns = ", ".join(self._config.table.column_names)
        with self._cursor() as cursor:
            cursor.execute(
                f"SELECT {columns} FROM {self.plan.qualified_mirror_table} "
                f"ORDER BY {self._config.table.primary_key} LIMIT {_MAX_EVENTS + 1}"
            )
            rows = cursor.fetchall()
        if len(rows) > _MAX_EVENTS:
            raise ValueError("Snowflake qualification mirror exceeds row budget")
        if any(len(row) != len(self._config.table.columns) for row in rows):
            raise ValueError("Snowflake qualification mirror row has wrong shape")
        return [dict(zip(self._config.table.column_names, row, strict=True)) for row in rows]

    def fetch_events(self, *, schema: str) -> Sequence[Mapping[str, Any]]:
        self._require_schema(schema)
        columns = ", ".join(self._config.table.column_names)
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT EVENT_ID, OPERATION, JOURNAL_RECEIVER, JOURNAL_SEQUENCE, "
                f"{columns} FROM {self.plan.qualified_history_table} "
                f"ORDER BY INGESTED_AT, EVENT_ID LIMIT {_MAX_EVENTS + 1}"
            )
            rows = cursor.fetchall()
        if len(rows) > _MAX_EVENTS:
            raise ValueError("Snowflake qualification history exceeds row budget")
        events: list[Mapping[str, Any]] = []
        for row in rows:
            if len(row) != 4 + len(self._config.table.columns):
                raise ValueError("Snowflake qualification history row has wrong shape")
            event_id, operation, receiver, sequence, *values = row
            if (not isinstance(event_id, str) or not event_id
                    or operation not in {"c", "u_before", "u_after", "d"}
                    or not isinstance(receiver, str) or not receiver
                    or type(sequence) is not int or sequence < 0):
                raise ValueError("Snowflake qualification history row is invalid")
            record = dict(zip(self._config.table.column_names, values, strict=True))
            image = "before" if operation in {"u_before", "d"} else "after"
            events.append({
                "event_id": event_id,
                "receiver": receiver, "sequence": sequence, "operation": operation,
                "payload": {image: record}, "is_snapshot": receiver == f"SNAPSHOT:{self._config.run_id}",
            })
        return events
