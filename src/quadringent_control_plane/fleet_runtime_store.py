"""Atomic UTF-8 JSON mapping store with process-local locking.

This module is intentionally standalone: stdlib only, no product imports,
no logging, no secrets, and no network or cloud clients.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class AtomicJsonStateStoreError(Exception):
    """Persisted state is corrupt or not a JSON mapping.

    Messages never include file contents or rejected values.
    """


def _reject_parse_constant(_literal: str) -> object:
    raise AtomicJsonStateStoreError("value is not a JSON type") from None


def _copy_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AtomicJsonStateStoreError("JSON object keys must be strings")
            copied[key] = _copy_json_value(item)
        return copied
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AtomicJsonStateStoreError("value is not a JSON type")
        return value
    raise AtomicJsonStateStoreError("value is not a JSON type")


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)


class AtomicJsonStateStore:
    """Load and save a JSON object atomically on a single local path."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._path.exists():
                return None
            try:
                raw = self._path.read_bytes()
            except OSError as exc:
                raise AtomicJsonStateStoreError("unable to read state") from exc
            try:
                text = raw.decode("utf-8")
                payload = json.loads(text, parse_constant=_reject_parse_constant)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AtomicJsonStateStoreError("corrupt JSON state") from None
            if not isinstance(payload, Mapping):
                raise AtomicJsonStateStoreError("state root is not a mapping")
            copied = _copy_json_value(payload)
            if not isinstance(copied, dict):
                raise AtomicJsonStateStoreError("state root is not a mapping")
            return copied

    def save(self, mapping: Mapping[str, Any]) -> None:
        if not isinstance(mapping, Mapping):
            raise AtomicJsonStateStoreError("state root is not a mapping")
        copied = _copy_json_value(mapping)
        if not isinstance(copied, dict):
            raise AtomicJsonStateStoreError("state root is not a mapping")
        try:
            serialized = json.dumps(copied, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise AtomicJsonStateStoreError("value is not a JSON type") from None
        directory = self._path.parent
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=str(directory),
            )
            tmp_path = Path(tmp_name)
            replaced = False
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, self._path)
                replaced = True
                _fsync_directory(directory)
            finally:
                if not replaced:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
