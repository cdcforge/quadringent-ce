"""Google Cloud Storage backend: raw objects, checkpoints and source gate.

GCS offers the two primitives the capture relies on: create-if-absent
(``if_generation_match=0``) and compare-and-set on an object generation.
Raw batches therefore keep their write-once, hash-checked semantics, and a
checkpoint or source-gate record is one small JSON object replaced only
from the exact generation that was read. ``google-cloud-storage`` is
imported lazily so offline tests and AWS installations do not need it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .contract import JournalPosition
from .object_store import _read_bounded, _validate_read_budget
from .source_gate import GATE_FORMAT_VERSION, SourceGatePolicy, _RevisionedSourceGate, _empty_record

CHECKPOINT_FORMAT_VERSION = "as400-checkpoint-v1"
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


def _client(client: Any | None) -> Any:
    if client is not None:
        return client
    from google.cloud import storage

    return storage.Client()


def _valid_bucket(bucket: str) -> str:
    if not isinstance(bucket, str) or _BUCKET.fullmatch(bucket) is None:
        raise ValueError("invalid GCS bucket")
    return bucket


def _code(error: Exception) -> int | None:
    code = getattr(error, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _is_not_found(error: Exception) -> bool:
    return _code(error) == 404


def _is_precondition_failed(error: Exception) -> bool:
    return _code(error) == 412


class GcsObjectStore:
    """GCS implementation of ``ObjectStore`` with hash-checked idempotent writes.

    The runtime identity needs ``storage.objects.get``, ``create`` and
    ``list`` on the bucket (e.g. roles/storage.objectCreator plus
    objectViewer), restricted by IAM condition to the configured prefix
    where the site requires it. No delete or overwrite permission is used.
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        self.bucket_name = _valid_bucket(bucket)
        self.prefix = prefix.strip("/")
        self.client = _client(client)
        self._bucket = self.client.bucket(self.bucket_name)

    def put_once(self, key: str, content: bytes) -> bool:
        full_key = self._key(key)
        digest = hashlib.sha256(content).hexdigest()
        existing = self._bucket.get_blob(full_key)
        if existing is not None:
            return self._same_existing(existing, full_key, content, digest)
        blob = self._bucket.blob(full_key)
        blob.metadata = {"sha256": digest}
        try:
            blob.upload_from_string(content, content_type="application/octet-stream",
                                    if_generation_match=0)
            return True
        except Exception as error:
            if not _is_precondition_failed(error):
                raise
        # A concurrent writer won: apply the same content check rather than
        # silently accepting a conflicting artifact.
        existing = self._bucket.get_blob(full_key)
        if existing is None:
            raise RuntimeError(f"object precondition failed but object is absent: {full_key}")
        return self._same_existing(existing, full_key, content, digest)

    def _same_existing(self, blob: Any, full_key: str, content: bytes, digest: str) -> bool:
        metadata = {str(k).lower(): str(v) for k, v in (blob.metadata or {}).items()}
        if metadata.get("sha256") == digest:
            return False
        if blob.download_as_bytes(if_generation_match=blob.generation) != content:
            raise ValueError(f"object collision with different content: {full_key}")
        return False

    def get(self, key: str) -> bytes:
        return self._bucket.blob(self._key(key)).download_as_bytes()

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        _validate_read_budget(max_bytes)
        blob = self._bucket.blob(self._key(key))
        try:
            payload = blob.download_as_bytes(start=0, end=max_bytes)
        except Exception as error:
            if _is_not_found(error):
                raise FileNotFoundError(key) from error
            raise
        return _read_bounded(_Buffer(payload), max_bytes)

    def list_receipt_keys(self, max_keys: int) -> tuple[str, ...]:
        _validate_read_budget(max_keys)
        prefix = self._key("receipts") + "/"
        keys: list[str] = []
        for blob in self.client.list_blobs(self.bucket_name, prefix=prefix, max_results=max_keys + 1):
            if not blob.name.startswith(prefix):
                raise ValueError("receipt listing escaped prefix")
            keys.append("receipts/" + blob.name[len(prefix):])
            if len(keys) > max_keys:
                raise ValueError("receipt listing exceeds budget")
        return tuple(keys)

    def _key(self, key: str) -> str:
        relative = Path(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("object key must not escape the configured prefix")
        relative_key = str(relative).replace("\\", "/")
        return f"{self.prefix}/{relative_key}" if self.prefix else relative_key


class _Buffer:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class _GenerationRecord:
    """One JSON object replaced only from the generation that was read."""

    def __init__(self, bucket: str, name: str, client: Any | None) -> None:
        self.client = _client(client)
        self._bucket = self.client.bucket(_valid_bucket(bucket))
        self.name = name

    def read(self) -> tuple[dict[str, Any] | None, int]:
        blob = self._bucket.get_blob(self.name)
        if blob is None:
            return None, 0
        try:
            payload = blob.download_as_bytes(if_generation_match=blob.generation)
        except Exception as error:
            if _is_precondition_failed(error) or _is_not_found(error):
                # Replaced or removed between metadata and content reads:
                # report a generation no writer can match, so CAS fails.
                return self._reread()
            raise
        record = json.loads(payload)
        if not isinstance(record, dict):
            raise ValueError("GCS record is not an object")
        return record, int(blob.generation)

    def _reread(self) -> tuple[dict[str, Any] | None, int]:
        blob = self._bucket.get_blob(self.name)
        if blob is None:
            return None, 0
        record = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
        return record, int(blob.generation)

    def write(self, record: dict[str, Any], expected_generation: int) -> bool:
        blob = self._bucket.blob(self.name)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        try:
            blob.upload_from_string(payload, content_type="application/json",
                                    if_generation_match=expected_generation)
            return True
        except Exception as error:
            if _is_precondition_failed(error):
                return False
            raise


def _record_name(root: str, key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("record key must not be empty")
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    readable = re.sub(r"[^a-z0-9-]+", "-", key.strip().lower()).strip("-")[:80] or "record"
    return f"{root}/{readable}-{digest}.json"


class GcsCheckpointStore:
    """``CheckpointStore`` backed by one GCS object per stream.

    Semantics match ``DynamoDbCheckpointStore``: commits never move
    backwards, a receiver change is an explicit transition, and every write
    is conditional on the exact predecessor observed by the caller.
    """

    def __init__(self, bucket: str, stream_key: str, *, client: Any | None = None,
                 root: str = "checkpoints") -> None:
        if not isinstance(stream_key, str) or not stream_key.strip():
            raise ValueError("checkpoint stream key must not be empty")
        self.stream_key = stream_key
        self._record = _GenerationRecord(bucket, _record_name(root.strip("/"), stream_key), client)

    @property
    def object_name(self) -> str:
        return self._record.name

    def _read(self) -> tuple[JournalPosition | None, int]:
        record, generation = self._record.read()
        if record is None:
            return None, 0
        try:
            if record.get("stream_id") != self.stream_key:
                raise KeyError("stream_id")
            return JournalPosition(receiver=str(record["receiver"]),
                                   sequence=int(record["sequence"])), generation
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid GCS checkpoint record") from error

    def load(self) -> JournalPosition | None:
        return self._read()[0]

    def commit(self, position: JournalPosition) -> None:
        previous = self.load()
        if previous is not None:
            if position.receiver != previous.receiver:
                raise ValueError("receiver rotation requires explicit ordering")
            if position < previous:
                raise ValueError("checkpoint moved backwards")
        self.compare_and_set(previous, position)

    def transition(self, previous: JournalPosition, position: JournalPosition) -> None:
        """CAS a receiver change against the exact predecessor position."""

        current = self.load()
        if current != previous:
            raise ValueError("checkpoint transition predecessor does not match")
        if position.receiver == previous.receiver:
            raise ValueError("receiver transition must change receiver")
        self.compare_and_set(previous, position)

    def compare_and_set(self, previous: JournalPosition | None, position: JournalPosition) -> None:
        if previous is not None and previous.receiver == position.receiver and position.sequence < previous.sequence:
            raise ValueError("checkpoint moved backwards")
        current, generation = self._read()
        if current != previous:
            raise RuntimeError("checkpoint compare-and-set conflict")
        record = {"format_version": CHECKPOINT_FORMAT_VERSION, "stream_id": self.stream_key,
                  "receiver": position.receiver, "sequence": position.sequence}
        if not self._record.write(record, generation):
            raise RuntimeError("checkpoint compare-and-set conflict")


class GcsSourceGate(_RevisionedSourceGate):
    """Source gate stored as one GCS object; the generation is the revision."""

    def __init__(self, bucket: str, gate_key: str, *, policy: SourceGatePolicy | None = None,
                 client: Any | None = None, root: str = "source-gates") -> None:
        if not isinstance(gate_key, str) or not gate_key.strip() or len(gate_key) > 256:
            raise ValueError("invalid source gate key")
        super().__init__(policy)
        self.gate_key = gate_key
        self._record = _GenerationRecord(bucket, _record_name(root.strip("/"), gate_key), client)

    def _read(self) -> dict[str, Any]:
        stored, generation = self._record.read()
        if stored is None:
            record = _empty_record(datetime.now(timezone.utc))
        else:
            if stored.get("format_version") != GATE_FORMAT_VERSION or stored.get("gate_key") != self.gate_key:
                raise ValueError("source gate record format unknown")
            record = {k: v for k, v in stored.items() if k != "gate_key"}
        record["revision"] = generation
        return record

    def _cas_write(self, record: dict[str, Any], expected_revision: int) -> bool:
        stored = {k: v for k, v in record.items() if k != "revision" and v is not None}
        stored["format_version"] = GATE_FORMAT_VERSION
        stored["gate_key"] = self.gate_key
        return self._record.write(stored, expected_revision)
