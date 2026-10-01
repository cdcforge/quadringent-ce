"""Une mesure de fraîcheur exige un reçu, un index et un lot brut validés."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.object_store import receipt_index_key
from quadringent.raw import RawBatchWriter
from quadringent.storage_layout import journal_prefix
from quadringent_qualification.published_probes import find_receipted_probes

from .fakes import FakeStorageBackend
from .test_orchestrator import SCHEMA


ROOT = "qualification/run-1"
CREATED = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
MARKER = "Q-run-1-1"


def _store_with_proof(tmp_path, *, marker=MARKER, table="QUALIF_ORDERS"):
    position = JournalPosition("R1", 101)
    event = ChangeEvent(
        source_system="QUALIFICATION", journal="QUALJRN", library="QUALIF_LIB",
        table=table, operation="u_after", position=position,
        commit_timestamp="2026-09-29T12:00:00+00:00", schema_version="1",
        before=None, after={"ORDER_ID": "1", "LABEL": marker},
    )
    writer = RawBatchWriter(tmp_path)
    manifest = writer.write_batch([event], high_watermark=position)
    payload_name = f"batch-{manifest.batch_id}.jsonl"
    manifest_name = f"batch-{manifest.batch_id}.manifest.json"
    payload = (tmp_path / payload_name).read_bytes()
    manifest_content = (tmp_path / manifest_name).read_bytes()
    start = {"receiver": "R1", "sequence": 101}
    end = {"receiver": "R1", "sequence": 101}
    receipt = {
        "format_version": "quadringent-scan-receipt-v1",
        "previous": {"receiver": "R1", "sequence": 100},
        "start": start, "end": end, "event_count": 1,
        "raw": {
            "payload_key": payload_name, "manifest_key": manifest_name,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
        },
    }
    receipt_content = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt_name = "receipts/scan-" + hashlib.sha256(
        json.dumps(start, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest() + ".json"
    index_content = (json.dumps({
        "format_version": "quadringent-scan-index-v1",
        "receipt_key": receipt_name,
        "receipt_sha256": hashlib.sha256(receipt_content).hexdigest(),
    }, sort_keys=True, separators=(",", ":")) + "\n").encode()
    prefix = journal_prefix(ROOT, "QUALIF_ORDERS")
    store = FakeStorageBackend()
    store.blobs.update({
        f"{prefix}/{payload_name}": payload,
        f"{prefix}/{manifest_name}": manifest_content,
        f"{prefix}/{receipt_name}": receipt_content,
        f"{prefix}/{receipt_index_key(position)}": index_content,
    })
    store.created_at[f"{prefix}/{payload_name}"] = CREATED
    return store, event, f"{prefix}/{payload_name}", f"{prefix}/{receipt_name}"


def test_probe_uses_only_indexed_receipted_validated_raw(tmp_path):
    store, event, payload_key, _ = _store_with_proof(tmp_path)
    found = find_receipted_probes(store, raw_prefix=ROOT, schema=SCHEMA,
                                  row_key=1, markers=(MARKER,))
    assert found[MARKER].event_id == event.event_id
    assert found[MARKER].object_key == payload_key
    assert found[MARKER].created_at == CREATED


def test_orphan_raw_is_not_measurement(tmp_path):
    store, _, _, receipt_key = _store_with_proof(tmp_path)
    del store.blobs[receipt_key]
    assert find_receipted_probes(store, raw_prefix=ROOT, schema=SCHEMA,
                                 row_key=1, markers=(MARKER,)) == {}


def test_changed_payload_or_missing_index_cannot_prove_freshness(tmp_path):
    store, _, payload_key, _ = _store_with_proof(tmp_path)
    store.blobs[payload_key] += b"x"
    with pytest.raises(ValueError):
        find_receipted_probes(store, raw_prefix=ROOT, schema=SCHEMA,
                              row_key=1, markers=(MARKER,))

    store, _, _, _ = _store_with_proof(tmp_path)
    index_key = next(key for key in store.blobs if "/scan-index/" in key)
    del store.blobs[index_key]
    with pytest.raises((KeyError, FileNotFoundError, ValueError)):
        find_receipted_probes(store, raw_prefix=ROOT, schema=SCHEMA,
                              row_key=1, markers=(MARKER,))


def test_foreign_table_cannot_prove_freshness(tmp_path):
    store, _, _, _ = _store_with_proof(tmp_path, table="OTHER_TABLE")
    with pytest.raises(ValueError):
        find_receipted_probes(store, raw_prefix=ROOT, schema=SCHEMA,
                              row_key=1, markers=(MARKER,))
