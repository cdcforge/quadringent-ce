"""Selection of the durable capture backend from the process environment.

``QUADRINGENT_STORAGE_BACKEND`` chooses where raw batches, checkpoints and
the source gate live:

* ``aws`` (default): S3 bucket ``AS400_RAW_BUCKET`` and DynamoDB table
  ``AS400_CHECKPOINT_TABLE`` — unchanged behaviour;
* ``gcs``: GCS bucket ``AS400_RAW_BUCKET`` for raw batches and GCS bucket
  ``AS400_CHECKPOINT_BUCKET`` for checkpoint and source-gate records.

The ordering guarantees (raw durable before checkpoint, compare-and-set,
explicit receiver transitions) are identical for both backends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

BACKENDS = ("aws", "gcs")


def backend_from_environment(environ: Mapping[str, str]) -> str:
    backend = (environ.get("QUADRINGENT_STORAGE_BACKEND") or "aws").strip().lower()
    if backend not in BACKENDS:
        raise ValueError("QUADRINGENT_STORAGE_BACKEND must be aws or gcs")
    return backend


def _required(environ: Mapping[str, str], name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class StorageBackend:
    kind: str
    raw_bucket: str
    state_location: str
    gcs_client: Any | None = None
    aws_client_factory: Callable[[str], Any] | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str], *, gcs_client: Any | None = None,
                         aws_client_factory: Callable[[str], Any] | None = None) -> "StorageBackend":
        kind = backend_from_environment(environ)
        raw_bucket = _required(environ, "AS400_RAW_BUCKET")
        if kind == "gcs":
            if gcs_client is None:
                from google.cloud import storage

                gcs_client = storage.Client()
            return cls(kind, raw_bucket, _required(environ, "AS400_CHECKPOINT_BUCKET"), gcs_client=gcs_client)
        return cls(kind, raw_bucket, _required(environ, "AS400_CHECKPOINT_TABLE"),
                   aws_client_factory=aws_client_factory)

    def _aws(self, service: str) -> Any | None:
        return self.aws_client_factory(service) if self.aws_client_factory is not None else None

    def object_store(self, prefix: str):
        if self.kind == "gcs":
            from .gcs_backend import GcsObjectStore

            return GcsObjectStore(self.raw_bucket, prefix, client=self.gcs_client)
        from .object_store import S3ObjectStore

        return S3ObjectStore(self.raw_bucket, prefix, client=self._aws("s3"))

    def checkpoint_store(self, stream_key: str):
        if self.kind == "gcs":
            from .gcs_backend import GcsCheckpointStore

            return GcsCheckpointStore(self.state_location, stream_key, client=self.gcs_client)
        from .checkpoint import DynamoDbCheckpointStore

        return DynamoDbCheckpointStore(self.state_location, stream_key, client=self._aws("dynamodb"))

    def source_gate(self, gate_key: str, *, policy: Any | None = None):
        if self.kind == "gcs":
            from .gcs_backend import GcsSourceGate

            return GcsSourceGate(self.state_location, gate_key, policy=policy, client=self.gcs_client)
        from .source_gate import DynamoDbSourceGate

        return DynamoDbSourceGate(self.state_location, gate_key, policy=policy, client=self._aws("dynamodb"))
