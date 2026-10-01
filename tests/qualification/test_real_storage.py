"""Cloud object reads used by a real qualification run stay in its own prefix."""

from datetime import datetime, timezone

import pytest

from quadringent_qualification.config import StorageConfig
from quadringent_qualification.real_storage import CloudQualificationStorage


ROOT = "qualification/run-1"
CREATED = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, payload: bytes = b'{"n":1}\n{"n":2}\n') -> None:
        self.payload = payload
        self.reads: list[tuple[str, int]] = []

    def get_bounded(self, key: str, max_bytes: int) -> bytes:
        self.reads.append((key, max_bytes))
        if len(self.payload) > max_bytes:
            raise ValueError("object exceeds read budget")
        return self.payload


class FakeS3:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, str]] = []

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        if "ContinuationToken" not in kwargs:
            return {"Contents": [{"Key": ROOT + "/a.jsonl"}],
                    "IsTruncated": True, "NextContinuationToken": "next"}
        return {"Contents": [{"Key": ROOT + "/b.jsonl"}], "IsTruncated": False}

    def head_object(self, **kwargs: str) -> dict[str, datetime]:
        self.head_calls.append(kwargs)
        return {"LastModified": CREATED}


def test_s3_reads_only_own_run_prefix_and_paginates() -> None:
    client = FakeS3()
    store = FakeStore()
    adapter = CloudQualificationStorage(
        StorageConfig("s3", "qual-bucket", ROOT), client=client, object_store=store,
    )

    assert adapter.list_objects(ROOT) == (ROOT + "/a.jsonl", ROOT + "/b.jsonl")
    assert client.list_calls[0]["Prefix"] == ROOT + "/"
    assert client.list_calls[1]["ContinuationToken"] == "next"
    assert adapter.read_lines(ROOT + "/a.jsonl") == ('{"n":1}', '{"n":2}')
    assert store.reads == [(ROOT + "/a.jsonl", adapter.max_object_bytes)]
    assert adapter.read_bytes(ROOT + "/a.jsonl", 100) == store.payload
    assert store.reads[-1] == (ROOT + "/a.jsonl", 100)
    assert adapter.object_created_at(ROOT + "/a.jsonl") == CREATED
    assert client.head_calls == [{"Bucket": "qual-bucket", "Key": ROOT + "/a.jsonl"}]

    for escaped in ("qualification/run-2/a.jsonl", ROOT + "-other/a.jsonl", "../secret", "/tmp/key"):
        with pytest.raises(ValueError):
            adapter.read_lines(escaped)
        with pytest.raises(ValueError):
            adapter.read_bytes(escaped, 100)
        with pytest.raises(ValueError):
            adapter.object_created_at(escaped)
    with pytest.raises(ValueError):
        adapter.read_bytes(ROOT + "/a.jsonl", adapter.max_object_bytes + 1)


def test_s3_rejects_listing_that_escapes_prefix_or_exceeds_budget() -> None:
    class EscapingS3(FakeS3):
        def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
            return {"Contents": [{"Key": "qualification/run-2/private"}], "IsTruncated": False}

    adapter = CloudQualificationStorage(
        StorageConfig("s3", "qual-bucket", ROOT), client=EscapingS3(), object_store=FakeStore(),
    )
    with pytest.raises(ValueError, match="escaped"):
        adapter.list_objects(ROOT)

    adapter = CloudQualificationStorage(
        StorageConfig("s3", "qual-bucket", ROOT), client=FakeS3(),
        object_store=FakeStore(), max_objects=1,
    )
    with pytest.raises(ValueError, match="budget"):
        adapter.list_objects(ROOT)


class FakeBlob:
    def __init__(self, name: str, created: datetime | None = CREATED) -> None:
        self.name = name
        self.time_created = created


class FakeGcsBucket:
    def get_blob(self, key: str) -> FakeBlob | None:
        return FakeBlob(key) if key == ROOT + "/a.jsonl" else None


class FakeGcs:
    def __init__(self) -> None:
        self.list_calls: list[tuple[str, str, int]] = []

    def list_blobs(self, bucket: str, *, prefix: str, max_results: int):
        self.list_calls.append((bucket, prefix, max_results))
        return iter((FakeBlob(ROOT + "/a.jsonl"), FakeBlob(ROOT + "/b.jsonl")))

    def bucket(self, name: str) -> FakeGcsBucket:
        assert name == "qual-bucket"
        return FakeGcsBucket()


def test_gcs_reads_only_own_run_prefix_and_requires_creation_time() -> None:
    client = FakeGcs()
    adapter = CloudQualificationStorage(
        StorageConfig("gcs", "qual-bucket", ROOT), client=client, object_store=FakeStore(),
    )
    assert adapter.list_objects(ROOT) == (ROOT + "/a.jsonl", ROOT + "/b.jsonl")
    assert client.list_calls == [("qual-bucket", ROOT + "/", adapter.max_objects + 1)]
    assert adapter.object_created_at(ROOT + "/a.jsonl") == CREATED
    with pytest.raises(FileNotFoundError):
        adapter.object_created_at(ROOT + "/missing.jsonl")


def test_cloud_adapter_rejects_unbounded_or_ambiguous_prefix() -> None:
    for backend, client, prefix in (
        ("s3", FakeS3(), ""),
        ("s3", FakeS3(), "."),
        ("gcs", FakeGcs(), "../shared"),
    ):
        with pytest.raises(ValueError):
            CloudQualificationStorage(StorageConfig(backend, "qual-bucket", prefix),
                                      client=client, object_store=FakeStore())


def test_cloud_adapter_rejects_missing_or_unmeasured_creation_time() -> None:
    class MissingS3(FakeS3):
        def head_object(self, **kwargs: str) -> dict[str, datetime]:
            class Missing(Exception):
                response = {"Error": {"Code": "404"}}

            raise Missing("object absent")

    s3 = CloudQualificationStorage(
        StorageConfig("s3", "qual-bucket", ROOT), client=MissingS3(), object_store=FakeStore(),
    )
    with pytest.raises(FileNotFoundError):
        s3.object_created_at(ROOT + "/missing.jsonl")

    class UndatedBucket(FakeGcsBucket):
        def get_blob(self, key: str) -> FakeBlob | None:
            return FakeBlob(key, created=None)

    class UndatedGcs(FakeGcs):
        def bucket(self, name: str) -> FakeGcsBucket:
            return UndatedBucket()

    gcs = CloudQualificationStorage(
        StorageConfig("gcs", "qual-bucket", ROOT), client=UndatedGcs(), object_store=FakeStore(),
    )
    with pytest.raises(ValueError, match="timestamp"):
        gcs.object_created_at(ROOT + "/a.jsonl")
