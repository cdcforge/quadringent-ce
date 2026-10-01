"""Bounded, read-only S3/GCS adapter for an isolated qualification run.

The product object stores own the object read. This adapter adds the list and
creation timestamp calls needed by the qualification protocol. Every call is
restricted to the run's raw prefix, including calls made by a faulty caller.
Cloud clients use the runner's existing AWS/GCP identity; no static key is
accepted here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from quadringent.gcs_backend import GcsObjectStore
from quadringent.object_store import S3ObjectStore

from .config import StorageConfig


def _canonical_key(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("invalid qualification object key")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("invalid qualification object key")
    return value


def _aware_timestamp(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("object creation timestamp is absent or timezone-naive")
    return value


def _s3_missing(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    details = response.get("Error", {})
    code = str(details.get("Code", "")) if isinstance(details, dict) else ""
    return code in {"404", "NoSuchKey", "NotFound"}


class CloudQualificationStorage:
    """Read-only ``StorageBackend`` over S3 or GCS for one run prefix."""

    def __init__(
        self,
        config: StorageConfig,
        *,
        client: Any | None = None,
        object_store: Any | None = None,
        max_objects: int = 5000,
        max_object_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if config.backend not in {"s3", "gcs"}:
            raise ValueError("qualification storage backend must be s3 or gcs")
        if not config.bucket or not config.bucket.strip():
            raise ValueError("qualification bucket is required")
        self._root = _canonical_key(config.raw_prefix)
        if max_objects < 1 or max_object_bytes < 1:
            raise ValueError("qualification storage budgets must be positive")
        self.max_objects = max_objects
        self.max_object_bytes = max_object_bytes
        self._backend = config.backend
        self._bucket = config.bucket
        if object_store is None:
            object_store = (
                S3ObjectStore(config.bucket, client=client)
                if config.backend == "s3" else GcsObjectStore(config.bucket, client=client)
            )
        self._store = object_store
        self._client = client or object_store.client

    def _scoped(self, key: str) -> str:
        value = _canonical_key(key)
        if not value.startswith(self._root + "/"):
            raise ValueError("qualification object key escaped run prefix")
        return value

    def list_objects(self, prefix: str) -> tuple[str, ...]:
        value = _canonical_key(prefix)
        if value != self._root and not value.startswith(self._root + "/"):
            raise ValueError("qualification listing escaped run prefix")
        query_prefix = value + "/"
        if self._backend == "s3":
            return self._list_s3(query_prefix)
        return self._list_gcs(query_prefix)

    def _list_s3(self, prefix: str) -> tuple[str, ...]:
        keys: list[str] = []
        token: str | None = None
        seen: set[str] = set()
        while True:
            args: dict[str, object] = {
                "Bucket": self._bucket,
                "Prefix": prefix,
                "MaxKeys": min(1000, self.max_objects - len(keys) + 1),
            }
            if token is not None:
                args["ContinuationToken"] = token
            page = self._client.list_objects_v2(**args)
            for item in page.get("Contents", ()):
                key = item["Key"]
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise ValueError("qualification listing escaped requested prefix")
                self._scoped(key)
                keys.append(key)
                if len(keys) > self.max_objects:
                    raise ValueError("qualification listing exceeds object budget")
            if not page.get("IsTruncated", False):
                return tuple(keys)
            token = page.get("NextContinuationToken")
            if not isinstance(token, str) or not token or token in seen:
                raise ValueError("invalid qualification listing pagination")
            seen.add(token)
            if len(seen) > self.max_objects:
                raise ValueError("qualification listing exceeds page budget")

    def _list_gcs(self, prefix: str) -> tuple[str, ...]:
        keys: list[str] = []
        for blob in self._client.list_blobs(
            self._bucket, prefix=prefix, max_results=self.max_objects + 1,
        ):
            key = blob.name
            if not isinstance(key, str) or not key.startswith(prefix):
                raise ValueError("qualification listing escaped requested prefix")
            self._scoped(key)
            keys.append(key)
            if len(keys) > self.max_objects:
                raise ValueError("qualification listing exceeds object budget")
        return tuple(keys)

    def read_lines(self, key: str) -> tuple[str, ...]:
        payload = self._store.get_bounded(self._scoped(key), self.max_object_bytes)
        return tuple(payload.decode("utf-8").splitlines())

    def read_bytes(self, key: str, max_bytes: int) -> bytes:
        if type(max_bytes) is not int or not 0 < max_bytes <= self.max_object_bytes:
            raise ValueError("qualification object read budget is invalid")
        return self._store.get_bounded(self._scoped(key), max_bytes)

    def object_created_at(self, key: str) -> datetime:
        full_key = self._scoped(key)
        if self._backend == "s3":
            try:
                response = self._client.head_object(Bucket=self._bucket, Key=full_key)
            except Exception as error:
                if _s3_missing(error):
                    raise FileNotFoundError(full_key) from error
                raise
            return _aware_timestamp(response.get("LastModified"))
        blob = self._client.bucket(self._bucket).get_blob(full_key)
        if blob is None:
            raise FileNotFoundError(full_key)
        return _aware_timestamp(blob.time_created)
