"""Relie des sondes DML à des lots bruts durables du run de qualification.

Le reçu et son index doivent pointer vers le même lot. Le manifeste, les
empreintes et les évènements sont vérifiés avec le décodeur brut du produit ;
un objet orphelin ou un simple compteur du lecteur ne devient pas une mesure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Any, Sequence

from quadringent.contract import JournalPosition
from quadringent.object_store import receipt_index_key
from quadringent.raw import read_raw_batch
from quadringent.storage_layout import journal_prefix

from .adapters import StorageBackend
from .schema import TableSchema, canonical_value


_RECEIPT_NAME = re.compile(r"receipts/scan-[a-f0-9]{64}\.json\Z")
_PAYLOAD_NAME = re.compile(r"batch-([a-f0-9]{32})\.jsonl\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_MAX_RECEIPTS = 1000
_MAX_RECEIPT_BYTES = 16_384
_MAX_INDEX_BYTES = 4096
_MAX_MANIFEST_BYTES = 65_536
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_RAW_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class PublishedProbe:
    event_id: str
    object_key: str
    created_at: datetime


def _document(storage: StorageBackend, key: str, limit: int) -> tuple[bytes, dict[str, Any]]:
    content = storage.read_bytes(key, limit)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("qualification publication document is invalid")
    return content, parsed


def _position(value: object) -> JournalPosition:
    if (not isinstance(value, dict) or set(value) != {"receiver", "sequence"}
            or not isinstance(value["receiver"], str)
            or type(value["sequence"]) is not int):
        raise ValueError("qualification receipt position is invalid")
    return JournalPosition(value["receiver"], value["sequence"])


def _raw_reference(receipt: dict[str, Any]) -> tuple[str, str, str, str] | None:
    raw = receipt.get("raw")
    if raw is None:
        if receipt.get("event_count") != 0:
            raise ValueError("qualification empty receipt has events")
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "payload_key", "manifest_key", "payload_sha256", "manifest_sha256",
    }:
        raise ValueError("qualification receipt raw reference is invalid")
    payload_name = raw["payload_key"]
    if not isinstance(payload_name, str):
        raise ValueError("qualification payload name is invalid")
    matched = _PAYLOAD_NAME.fullmatch(payload_name)
    if matched is None or raw["manifest_key"] != f"batch-{matched[1]}.manifest.json":
        raise ValueError("qualification raw object names are invalid")
    if any(not isinstance(raw[key], str) or _DIGEST.fullmatch(raw[key]) is None
           for key in ("payload_sha256", "manifest_sha256")):
        raise ValueError("qualification raw digests are invalid")
    return payload_name, raw["manifest_key"], raw["payload_sha256"], raw["manifest_sha256"]


def find_receipted_probes(
    storage: StorageBackend, *, raw_prefix: str, schema: TableSchema,
    row_key: int, markers: Sequence[str],
) -> dict[str, PublishedProbe]:
    """Trouve chaque mise à jour unique dans les reçus du journal de la table.

    Les clés retournées correspondent aux marqueurs demandés. Une clé absente
    signifie que sa publication n'est pas prouvée. Toute preuve ambiguë ou
    incohérente fait échouer la lecture au lieu de produire une latence.
    """
    if (not markers or len(set(markers)) != len(markers)
            or any(not isinstance(marker, str) or not marker for marker in markers)
            or type(row_key) is not int):
        raise ValueError("qualification probe selector is invalid")
    library, table = schema.qualified_name.split(".", 1)
    prefix = journal_prefix(raw_prefix, table)
    receipt_prefix = f"{prefix}/receipts"
    keys = tuple(storage.list_objects(receipt_prefix))
    if len(keys) > _MAX_RECEIPTS:
        raise ValueError("qualification receipt listing exceeds budget")
    expected = set(markers)
    found: dict[str, PublishedProbe] = {}
    raw_bytes_total = 0
    for full_receipt_key in keys:
        if not full_receipt_key.startswith(receipt_prefix + "/"):
            raise ValueError("qualification receipt escaped run prefix")
        receipt_name = full_receipt_key[len(prefix) + 1:]
        if _RECEIPT_NAME.fullmatch(receipt_name) is None:
            raise ValueError("qualification receipt name is invalid")
        receipt_content, receipt = _document(storage, full_receipt_key, _MAX_RECEIPT_BYTES)
        if receipt.get("format_version") not in {
            "quadringent-scan-receipt-v1", "quadringent-scan-receipt-v2",
        }:
            raise ValueError("qualification receipt format is invalid")
        start, end = _position(receipt.get("start")), _position(receipt.get("end"))
        if start.receiver != end.receiver or start.sequence > end.sequence:
            raise ValueError("qualification receipt interval is invalid")
        start_record = {"receiver": start.receiver, "sequence": start.sequence}
        expected_receipt_name = "receipts/scan-" + hashlib.sha256(
            json.dumps(start_record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() + ".json"
        if receipt_name != expected_receipt_name:
            raise ValueError("qualification receipt identity is invalid")
        event_count = receipt.get("event_count")
        if type(event_count) is not int or not 0 <= event_count <= 5000:
            raise ValueError("qualification receipt count is invalid")
        raw = _raw_reference(receipt)
        if raw is None:
            continue
        index_key = f"{prefix}/{receipt_index_key(end)}"
        _, index = _document(storage, index_key, _MAX_INDEX_BYTES)
        if (set(index) != {"format_version", "receipt_key", "receipt_sha256"}
                or index["format_version"] != "quadringent-scan-index-v1"
                or index["receipt_key"] != receipt_name
                or index["receipt_sha256"] != hashlib.sha256(receipt_content).hexdigest()):
            raise ValueError("qualification receipt index is invalid")

        payload_name, manifest_name, payload_digest, manifest_digest = raw
        full_payload_key = f"{prefix}/{payload_name}"
        manifest_content = storage.read_bytes(f"{prefix}/{manifest_name}", _MAX_MANIFEST_BYTES)
        payload = storage.read_bytes(full_payload_key, _MAX_PAYLOAD_BYTES)
        raw_bytes_total += len(manifest_content) + len(payload)
        if raw_bytes_total > _MAX_TOTAL_RAW_BYTES:
            raise ValueError("qualification raw read exceeds total budget")
        if (hashlib.sha256(payload).hexdigest() != payload_digest
                or hashlib.sha256(manifest_content).hexdigest() != manifest_digest):
            raise ValueError("qualification raw differs from receipt digest")
        batch = read_raw_batch(manifest_content, payload, payload_name=payload_name)
        if (payload_name != f"batch-{batch.manifest.batch_id}.jsonl"
                or batch.manifest.high_watermark != end
                or len(batch.events) != event_count):
            raise ValueError("qualification raw differs from receipt scope")
        for event in batch.events:
            if (event.library.upper() != library.upper() or event.table.upper() != table.upper()
                    or event.position.receiver != start.receiver
                    or not start.sequence <= event.position.sequence <= end.sequence):
                raise ValueError("qualification raw event escaped receipt scope")
            if event.operation not in {"u", "u_after"} or event.after is None:
                continue
            key = canonical_value(event.after.get(schema.primary_key), schema.column(schema.primary_key))
            marker = event.after.get("LABEL")
            if key != row_key or marker not in expected:
                continue
            if marker in found:
                raise ValueError("qualification probe has ambiguous publication")
            found[marker] = PublishedProbe(
                event_id=event.event_id, object_key=full_payload_key,
                created_at=storage.object_created_at(full_payload_key),
            )
    return found
