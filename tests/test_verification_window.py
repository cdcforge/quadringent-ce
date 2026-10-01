import site_fixture
from datetime import datetime, timezone, timedelta
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from quadringent.contract import ChangeEvent, JournalPosition
from quadringent.console_snapshot import ConsoleSnapshotBuilder, FluxIdentity
from quadringent.raw import RawBatchWriter
from quadringent.site_config import current as current_site
from quadringent.verification_window import collect_verification_window

SITE = site_fixture.build_test_site()

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
PREFIX = f"{current_site().stream_prefix}/runs/test-run/"


class Store:
    def __init__(self):
        site = current_site()
        event = ChangeEvent('ibmi', site.journal_name, site.source_schema, site.proof_table, 'c',
            JournalPosition('TRNJRN4000', 42), NOW.isoformat(), 'v1', None, {'test': 1})
        with TemporaryDirectory() as directory:
            RawBatchWriter(directory).write_batch([event], high_watermark=event.position)
            self.objects = {PREFIX + p.name: p.read_bytes() for p in Path(directory).iterdir()}
        self.objects[PREFIX + 'reservation.json'] = json.dumps({
            'format_version': 'quadringent-run-reservation-v1', 'run_id': 'test-run'}).encode()
        self.capture = {'format_version':'as400-console-v1', 'generated_at':NOW.isoformat(),
            'run':{'state':'STOPPED_BUDGET', 'started_at':NOW.isoformat()},
            'counters':{'errors':{'value':0}, 'events_published':{'value':1},
                        'windows_published':{'value':1}}}
        self.calls = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        value = json.dumps(self.capture).encode() if kwargs['Key'].endswith('console-snapshot.json') else self.objects[kwargs['Key']]
        return {'Body':BytesIO(value), 'ContentLength':len(value)}

    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        return self

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        yield {'Contents':[{'Key':key} for key in self.objects]}


class VerificationWindowTests(unittest.TestCase):
    def test_archive_inspection_preserves_original_time_without_weakening_live_gate(self):
        from quadringent.verification_window import collect_archived_verification_window
        store = Store()
        later = NOW + timedelta(days=2)
        with self.assertRaisesRegex(ValueError, 'stale'):
            collect_verification_window(store, run_id='test-run', now=later, site=SITE)
        archive = collect_archived_verification_window(store, run_id='test-run', now=later, site=SITE)
        self.assertEqual(archive.capture['generated_at'], NOW.isoformat())
        self.assertEqual(archive.event_count, 1)
        self.assertEqual(archive.object_keys, tuple(k for k in sorted(store.objects) if k.endswith('.jsonl')))

    def test_archive_inspection_accepts_a_reservation_from_before_the_rename(self):
        """Un run réservé par CDC Forge reste la même autorité après renommage."""
        from quadringent.verification_window import collect_archived_verification_window
        store = Store()
        store.objects[PREFIX + 'reservation.json'] = json.dumps({
            'format_version': 'cdcforge-run-reservation-v1', 'run_id': 'test-run'}).encode()
        archive = collect_archived_verification_window(
            store, run_id='test-run', now=NOW + timedelta(days=1), site=SITE)
        self.assertEqual(archive.event_count, 1)

    def test_archive_inspection_still_rejects_unsafe_or_corrupt_runs(self):
        from quadringent.verification_window import collect_archived_verification_window
        for change in ('running', 'future', 'errors', 'count', 'corrupt', 'reservation'):
            with self.subTest(change=change):
                store = Store()
                if change == 'running': store.capture['run']['state'] = 'RUNNING'
                if change == 'future': store.capture['generated_at'] = (NOW + timedelta(days=3)).isoformat()
                if change == 'errors': store.capture['counters']['errors']['value'] = 1
                if change == 'count': store.capture['counters']['events_published']['value'] = 2
                if change == 'corrupt':
                    payload = next(k for k in store.objects if k.endswith('.jsonl'))
                    store.objects[payload] += b'corruption'
                if change == 'reservation': store.objects[PREFIX + 'reservation.json'] = b'{}'
                with self.assertRaises(ValueError):
                    collect_archived_verification_window(store, run_id='test-run', now=NOW + timedelta(days=2), site=SITE)

    def test_real_capture_snapshot_is_accepted_without_counter_translation(self):
        store = Store()
        builder = ConsoleSnapshotBuilder(
            identity=FluxIdentity(id='test-run', label='SALE', journal='DEMOJRN',
                journal_library='DEMOLIB', objects=('SALE',),
                reader_path='RetrieveJournal', target='Snowflake DEV', job='test-run'),
            started_at_iso=NOW.isoformat(), clock=lambda: 0.0,
            _started_monotonic=0.0)
        builder.observe(None, {'errors': 0, 'events_published': 1,
                               'batches_published': 1})
        builder.mark_stopped('STOPPED_BUDGET', 'budget')
        with patch('quadringent.console_snapshot._now_iso', return_value=NOW.isoformat()):
            store.capture = builder.document()
        result = collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)
        self.assertEqual(result.event_count, 1)
        self.assertEqual(len(result.object_keys), 1)

    def test_absent_reservation_is_not_accepted_as_a_legacy_run(self):
        store = Store()
        del store.objects[PREFIX + 'reservation.json']
        with self.assertRaises(KeyError):
            collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_reservation_must_belong_to_the_requested_run(self):
        for value in (None, {}, {'format_version':'quadringent-run-reservation-v1', 'run_id':'other'}):
            with self.subTest(value=value):
                store = Store()
                store.objects[PREFIX + 'reservation.json'] = json.dumps(value).encode()
                with self.assertRaises(ValueError):
                    collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_listing_page_budget_is_enforced(self):
        store = Store()
        store.paginate = lambda **kwargs: iter([{}] * 11)
        with self.assertRaisesRegex(ValueError, 'page budget'):
            collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_window_byte_budget_is_enforced(self):
        from unittest.mock import patch
        store = Store()
        size = len(json.dumps(store.capture).encode()) + sum(map(len,store.objects.values()))
        with patch('quadringent.verification_window.MAX_WINDOW_BYTES',size-1):
            with self.assertRaises(ValueError):
                collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_malformed_observation_timestamps_fail_closed(self):
        for field in ('generated_at','started_at'):
            with self.subTest(field=field):
                store = Store()
                target = store.capture if field=='generated_at' else store.capture['run']
                target[field] = 'not-a-timestamp'
                with self.assertRaises(ValueError):
                    collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_snapshot_race_and_oversized_reads_are_rejected(self):
        for mode in ('race', 'oversized'):
            with self.subTest(mode=mode):
                store = Store()
                original = store.get_object
                reads = 0
                def read(**kwargs):
                    nonlocal reads
                    if kwargs['Key'].endswith('console-snapshot.json'):
                        reads += 1
                        if mode == 'race' and reads == 2:
                            store.capture['run']['state'] = 'RUNNING'
                    result = original(**kwargs)
                    if mode == 'oversized': result['ContentLength'] = 10**12
                    return result
                store.get_object = read
                with self.assertRaises(ValueError):
                    collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_duplicate_identity_between_valid_batches_is_rejected(self):
        store = Store()
        event = ChangeEvent(*SITE.event_scope(), 'c',
            JournalPosition(f'{SITE.journal_name}4000', 42), NOW.isoformat(), 'v1', None, {'test':1})
        with TemporaryDirectory() as directory:
            RawBatchWriter(directory).write_batch([event], high_watermark=JournalPosition(f'{SITE.journal_name}4000',43))
            store.objects.update({PREFIX+p.name:p.read_bytes() for p in Path(directory).iterdir()})
        store.capture['counters']['events_published']['value']=2
        store.capture['counters']['windows_published']['value']=2
        with self.assertRaisesRegex(ValueError, 'Duplicate event'):
            collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_completed_run_yields_integrity_checked_keys_and_identity_digest(self):
        store = Store()
        result = collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)
        self.assertEqual(result.event_count, 1)
        self.assertEqual(len(result.event_ids_sha256), 64)
        self.assertEqual(result.object_keys, tuple(k for k in sorted(store.objects) if k.endswith('.jsonl')))
        self.assertTrue(all(c['Bucket']==SITE.raw_bucket for c in store.calls))

    def test_unsafe_run_id_rejected_before_io(self):
        for run in ('', '../bad', 'foo/bar', 'UPPER', 'a'*81):
            with self.subTest(run=run):
                store = Store()
                with self.assertRaises(ValueError):
                    collect_verification_window(store, run_id=run, now=NOW, site=SITE)
                self.assertEqual(store.calls, [])

    def test_terminal_fresh_error_free_known_counts_required(self):
        for change in ('running','old','future','errors','unknown','count'):
            with self.subTest(change=change):
                store = Store()
                if change=='running': store.capture['run']['state']='RUNNING'
                if change=='old': store.capture['generated_at']='2026-09-07T00:00:00+00:00'
                if change=='future': store.capture['generated_at']='2026-09-09T00:00:00+00:00'
                if change=='errors': store.capture['counters']['errors']['value']=1
                if change=='unknown': store.capture['counters']['errors']['value']=None
                if change=='count': store.capture['counters']['events_published']['value']=2
                with self.assertRaises(ValueError):
                    collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)

    def test_orphan_corruption_and_foreign_object_fail_closed(self):
        for change in ('orphan','corrupt','foreign','extra'):
            with self.subTest(change=change):
                store=Store()
                payload=next(k for k in store.objects if k.endswith('.jsonl'))
                if change=='orphan': del store.objects[payload.replace('.jsonl','.manifest.json')]
                if change=='corrupt': store.objects[payload]+=b'corrupt'
                if change=='foreign': store.objects['other/run/batch.jsonl']=b'{}'
                if change=='extra': store.objects[PREFIX+'unexpected.txt']=b'{}'
                with self.assertRaises(ValueError):
                    collect_verification_window(store, run_id='test-run', now=NOW, site=SITE)
