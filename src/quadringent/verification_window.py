"""Acquire one closed run from its exclusive declared prefix, never by upload time.

This validates stored batch integrity, not independent source completeness or
Snowflake delivery. Callers must reserve a new run ID before starting capture.
The bucket, run prefix and event scope always come from the declared site.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import time
from typing import Any

from .raw import read_raw_batch
from .run_reservation import is_reservation_marker
from .site_config import SiteConfig

MAX_BATCHES = 1000
MAX_OBJECT_BYTES = 32 * 1024 * 1024
MAX_WINDOW_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class VerificationWindow:
    capture: dict[str, Any]
    object_keys: tuple[str, ...]
    event_count: int
    event_ids_sha256: str


def _read(client: Any, bucket: str, key: str, limit: int) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response['Body']
    try:
        size = response.get('ContentLength')
        if type(size) is not int or not 0 < size <= limit:
            raise ValueError('Object size outside verification budget')
        content = body.read(limit + 1)
        if len(content) != size or len(content) > limit:
            raise ValueError('Object length changed or exceeded budget')
        return content
    finally:
        body.close()


def _count(capture: dict[str, Any], name: str) -> int:
    value = capture.get('counters', {}).get(name, {}).get('value')
    if type(value) is not int or value < 0:
        raise ValueError('Capture counter is unknown or invalid')
    return value


def run_archive_prefix(site: SiteConfig, run_id: str) -> str:
    """Préfixe ``<flux>/runs/<run>/`` du run isolé dans le site déclaré."""

    return _run_prefix(run_id, site=_site(site))


def _site(site: SiteConfig) -> SiteConfig:
    if not isinstance(site, SiteConfig):
        raise ValueError('the declared site configuration is required')
    return site


def _run_prefix(run_id: str, *, site: SiteConfig) -> str:
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', run_id):
        raise ValueError('Invalid isolated run ID')
    return site.stream_prefix + '/runs/' + run_id + '/'


def await_capture_closed(client: Any, *, run_id: str, timeout_seconds: int, site: SiteConfig) -> None:
    """Observe a canary/soak snapshot only; never start or stop the capture."""
    site = _site(site)
    prefix = _run_prefix(run_id, site=site)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3660:
        raise ValueError('Capture observation budget must be between 1 and 3660 seconds')
    deadline = time.monotonic() + timeout_seconds
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError('Capture observation budget exhausted')
        try:
            payload = _read(client, site.raw_bucket, prefix + 'console-snapshot.json', 2 * 1024 * 1024)
        except Exception as error:
            response = getattr(error, 'response', {})
            if not isinstance(response, dict) or response.get('Error', {}).get('Code') != 'NoSuchKey':
                raise
        else:
            capture = json.loads(payload)
            if not isinstance(capture, dict) or capture.get('format_version') != 'as400-console-v1':
                raise ValueError('Invalid capture observation')
            run = capture.get('run')
            state = run.get('state') if isinstance(run, dict) else None
            if state == 'STOPPED_BUDGET':
                if time.monotonic() >= deadline:
                    raise TimeoutError('Capture closed after observation budget')
                return
            if state != 'RUNNING':
                raise ValueError('Capture failed or its state is unknown')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Capture observation budget exhausted')
        time.sleep(min(5, remaining))


def collect_verification_window(client: Any, *, run_id: str, now: datetime, site: SiteConfig) -> VerificationWindow:
    """Acquire a recent canary; never accept an archived observation as live."""
    return _collect_window(client, run_id=run_id, now=now, require_recent=True, site=site)


def collect_archived_verification_window(client: Any, *, run_id: str, now: datetime, site: SiteConfig) -> VerificationWindow:
    """Inspect a closed archive for telemetry, not live capture certification.

    All integrity, scope, size and closure checks remain enforced. Original
    timestamps are retained. Callers must not use this to authorize a soak or
    claim a fresh source observation.
    """
    return _collect_window(client, run_id=run_id, now=now, require_recent=False, site=site)


def _collect_window(client: Any, *, run_id: str, now: datetime, require_recent: bool, site: SiteConfig) -> VerificationWindow:
    site = _site(site)
    bucket = site.raw_bucket
    prefix = _run_prefix(run_id, site=site)
    if now.tzinfo is None:
        raise ValueError('Verification time must include a timezone')
    reservation_key = prefix + 'reservation.json'
    reservation_bytes = _read(client, bucket, reservation_key, 4096)
    if not is_reservation_marker(json.loads(reservation_bytes), run_id=run_id):
        raise ValueError('Run reservation does not match requested run')
    snapshot_key = prefix + 'console-snapshot.json'
    snapshot_bytes = _read(client, bucket, snapshot_key, 2 * 1024 * 1024)
    capture = json.loads(snapshot_bytes)
    if not isinstance(capture, dict) or capture.get('format_version') != 'as400-console-v1':
        raise ValueError('Unsupported capture snapshot')
    if capture.get('run', {}).get('state') != 'STOPPED_BUDGET' or _count(capture, 'errors') != 0:
        raise ValueError('Capture must have stopped successfully by budget')
    generated = datetime.fromisoformat(capture['generated_at'])
    started = datetime.fromisoformat(capture['run']['started_at'])
    if generated.tzinfo is None or started.tzinfo is None or started > generated:
        raise ValueError('Invalid capture observation window')
    age_seconds = (now - generated).total_seconds()
    if age_seconds < 0 or (require_recent and age_seconds > 300):
        raise ValueError('Capture observation is stale or in the future')
    expected_batches = _count(capture, 'windows_published')
    expected_events = _count(capture, 'events_published')
    if not 0 < expected_batches <= MAX_BATCHES or expected_events == 0:
        raise ValueError('Capture has no bounded nonempty batch window')

    keys: set[str] = set()
    pages = client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix)
    for page_index, page in enumerate(pages):
        if page_index >= 10:
            raise ValueError('Object listing exceeded page budget')
        for item in page.get('Contents', []):
            key = item['Key']
            if key in (snapshot_key, reservation_key):
                continue
            if key in keys or not key.startswith(prefix):
                raise ValueError('Duplicate or foreign object in run listing')
            relative = key[len(prefix):]
            if not re.fullmatch(r'batch-[a-f0-9]{32}\.(jsonl|manifest\.json)', relative):
                raise ValueError('Unexpected object in run prefix')
            keys.add(key)
            if len(keys) > 2 * MAX_BATCHES:
                raise ValueError('Too many batch objects')
    payloads = tuple(sorted(key for key in keys if key.endswith('.jsonl')))
    pairs = {key.replace('.jsonl', '.manifest.json') for key in payloads}
    if len(payloads) != expected_batches or keys != set(payloads) | pairs:
        raise ValueError('Incomplete batch/manifest set')

    identities: set[str] = set()
    total_bytes = len(snapshot_bytes) + len(reservation_bytes)
    expected_scope = site.event_scope()
    for key in payloads:
        manifest = _read(client, bucket, key.replace('.jsonl', '.manifest.json'), MAX_OBJECT_BYTES)
        total_bytes += len(manifest)
        remaining = min(MAX_OBJECT_BYTES, MAX_WINDOW_BYTES - total_bytes)
        if remaining <= 0:
            raise ValueError('Run exceeds byte budget')
        payload = _read(client, bucket, key, remaining)
        total_bytes += len(payload)
        batch = read_raw_batch(manifest, payload)
        if key != prefix + 'batch-' + batch.manifest.batch_id + '.jsonl':
            raise ValueError('Batch identity does not match object key')
        for event in batch.events:
            if (event.source_system, event.journal, event.library, event.table) != expected_scope:
                raise ValueError('Batch contains events outside the declared proof lane')
            if event.event_id in identities:
                raise ValueError('Duplicate event identity across batches')
            identities.add(event.event_id)
            if len(identities) > expected_events:
                raise ValueError('Batch events exceed capture count')
    if len(identities) != expected_events:
        raise ValueError('Batch events do not reconcile with capture')
    if _read(client, bucket, snapshot_key, 2 * 1024 * 1024) != snapshot_bytes:
        raise ValueError('Capture snapshot changed during acquisition')
    if _read(client, bucket, reservation_key, 4096) != reservation_bytes:
        raise ValueError('Run reservation changed during acquisition')
    return VerificationWindow(capture, payloads, len(identities),
        hashlib.sha256('\n'.join(sorted(identities)).encode()).hexdigest())
