import site_fixture
from datetime import datetime, timezone
import hashlib
import json
import unittest

from quadringent.snowflake_autonomous import default_autonomous_plan, verify_autonomous_destination

SITE = site_fixture.build_test_site()

IDS = [f'{i:064x}' for i in range(1, 4)]
DIGEST = hashlib.sha256('\n'.join(IDS).encode()).hexdigest()


class Cursor:
    def __init__(self, raw=None, canonical=None):
        self.raw = IDS if raw is None else raw
        self.canonical = IDS if canonical is None else canonical
        self.queries = []
        self.remaining = []

    def execute(self, sql):
        self.queries.append(sql)
        if 'SYSTEM$PIPE_STATUS' in sql:
            self.row = (json.dumps({'executionState':'RUNNING'}),)
        elif 'COUNT(DISTINCT SOURCE_FILE)' in sql:
            self.row = (3, 3, 1)
        elif sql.startswith('SELECT COUNT(*)'):
            self.row = (3,)
        else:
            self.remaining = [(i,) for i in (self.canonical if sql.startswith('SELECT EVENT_ID') else self.raw)]

    def fetchone(self):
        return self.row

    def fetchmany(self, size):
        assert size <= 1000
        batch, self.remaining = self.remaining[:2], self.remaining[2:]
        return batch


def verify(cursor, digest=DIGEST):
    return verify_autonomous_destination(cursor, {
        'format_version':'as400-console-v1',
        'position':{'checkpoint':{'receiver':'DEMOJRN4000','sequence':3}},
        'counters':{'events_published':{'value':3}},
    }, default_autonomous_plan(), object_keys=(f'{SITE.stream_prefix}/runs/test/batch-a.jsonl',),
        run_tag='TEST', observed_at=datetime(2026,9,8,tzinfo=timezone.utc),
        expected_event_ids_sha256=digest, site=SITE)


class IdentityReconciliationTests(unittest.TestCase):
    def test_identity_digest_matches_raw_and_canonical_in_multiple_pages(self):
        cursor=Cursor()
        result=verify(cursor)
        proof=result['stored_event_identity_proof']
        self.assertEqual(proof['state'],'matched')
        self.assertEqual(proof['event_ids_sha256'],DIGEST)
        self.assertEqual(proof['basis'],'verified_s3_batches')
        self.assertTrue(all(q.startswith('SELECT') for q in cursor.queries))

    def test_equal_counts_do_not_hide_replaced_event(self):
        for target in ('raw','canonical'):
            with self.subTest(target=target):
                cursor=Cursor(**{target:IDS[:2]+['f'*64]})
                with self.assertRaises(ValueError): verify(cursor)

    def test_duplicate_missing_extra_invalid_or_unordered_ids_fail(self):
        for identities in (IDS[:2],IDS+['f'*64],[IDS[0],IDS[0],IDS[2]],
                           [None,*IDS[:2]],['not-an-id',*IDS[:2]],list(reversed(IDS))):
            with self.subTest(identities=identities):
                with self.assertRaises(ValueError): verify(Cursor(canonical=identities))

    def test_invalid_expected_digest_rejected_before_sql(self):
        cursor=Cursor()
        with self.assertRaises(ValueError): verify(cursor,'bad')
        self.assertEqual(cursor.queries,[])
