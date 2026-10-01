"""Verify exact immutable proof-lane raw references, not destination delivery."""
from dataclasses import dataclass
import hashlib
import json

from .proof_windows import read_closed_window
from .raw import read_raw_batch
from .site_config import SiteConfig

MAX_RAW_BYTES = 256 * 1024 * 1024
MAX_OBJECT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ClosedRawWindow:
    window: dict
    object_keys: tuple[str, ...]
    event_count: int
    event_ids_sha256: str


def collect_closed_window_raw(store, *, window_id, site: SiteConfig, max_raw_bytes=MAX_RAW_BYTES):
    """Read only the closed read-set; never list a growing capture prefix.

    Metadata has separate bounded reads. The raw budget includes manifests
    and payloads. Every receipt is rehashed on use to prevent a TOCTOU swap.
    No source completeness, wall-clock authenticity or Snowflake claim.
    """
    if not isinstance(site, SiteConfig):
        raise ValueError('the declared site configuration is required')
    if type(max_raw_bytes) is not int or not 0 < max_raw_bytes <= MAX_RAW_BYTES:
        raise ValueError('invalid raw window budget')
    closed = read_closed_window(store, window_id=window_id)
    remaining = max_raw_bytes
    keys, identities = [], set()
    for ref in closed['receipts']:
        content = store.get_bounded(ref['key'], 16384)
        if hashlib.sha256(content).hexdigest() != ref['sha256']:
            raise ValueError('receipt changed during raw acquisition')
        receipt = json.loads(content)
        raw = receipt['raw']
        if raw is None:
            continue
        if raw['payload_key'] in keys:
            raise ValueError('duplicate raw object reference')
        objects = {}
        for kind in ('manifest', 'payload'):
            if remaining <= 0:
                raise ValueError('raw window byte budget exhausted')
            data = store.get_bounded(raw[kind+'_key'], min(MAX_OBJECT_BYTES, remaining))
            remaining -= len(data)
            if hashlib.sha256(data).hexdigest() != raw[kind+'_sha256']:
                raise ValueError('raw object differs from receipt digest')
            objects[kind] = data
        batch = read_raw_batch(objects['manifest'], objects['payload'])
        end = receipt['end']
        if (batch.manifest.high_watermark.receiver, batch.manifest.high_watermark.sequence) != (end['receiver'], end['sequence']):
            raise ValueError('raw watermark differs from receipt')
        if raw['payload_key'] != 'batch-'+batch.manifest.batch_id+'.jsonl':
            raise ValueError('raw batch identity differs from object key')
        if batch.manifest.event_count != receipt['event_count']:
            raise ValueError('raw population differs from receipt')
        for event in batch.events:
            if (event.source_system, event.journal, event.library, event.table) != site.event_scope():
                raise ValueError('raw window contains events outside the declared proof lane')
            if event.position.sequence < receipt['start']['sequence']:
                raise ValueError('raw event precedes receipt interval')
            if event.event_id in identities:
                raise ValueError('duplicate raw event identity')
            identities.add(event.event_id)
            if len(identities) > closed['event_count']:
                raise ValueError('raw population exceeds closed window')
        keys.append(raw['payload_key'])
    if len(identities) != closed['event_count']:
        raise ValueError('raw population does not reconcile with closed window')
    digest = hashlib.sha256('\n'.join(sorted(identities)).encode()).hexdigest()
    return ClosedRawWindow(closed, tuple(keys), len(identities), digest)
