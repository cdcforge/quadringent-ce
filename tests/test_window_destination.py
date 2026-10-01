import site_fixture
import hashlib
import unittest
from tempfile import TemporaryDirectory
from datetime import timedelta
import test_closed_window_raw
from test_proof_windows import NOW

from quadringent.snowflake_autonomous import default_autonomous_plan
from test_snowflake_autonomous import VerificationCursor


SITE = site_fixture.build_test_site()

class IdentityCursor(VerificationCursor):
    def __init__(self, identity='a'*64):
        super().__init__(raw_rows=1,distinct_events=1,source_files=1,canonical_rows=1)
        self.identity=identity
        self.ids=[]

    def execute(self,sql):
        if sql.startswith('SELECT DISTINCT PAYLOAD:event_id') or sql.startswith('SELECT EVENT_ID'):
            self.executed.append(sql)
            self.ids=[(self.identity,)]
        else:
            super().execute(sql)

    def fetchmany(self,size):
        result,self.ids=self.ids,[]
        return result


class WindowDestinationTests(unittest.TestCase):
    def test_file_budget_is_rejected_before_queries(self):
        from quadringent.snowflake_autonomous import reconcile_autonomous_files
        cursor=IdentityCursor()
        with self.assertRaises(ValueError):
            reconcile_autonomous_files(cursor,default_autonomous_plan(),expected_rows=1001,
                object_keys=[f'{SITE.stream_prefix}/runs/r1/batch-{i}.jsonl' for i in range(1001)], site=SITE)
        self.assertEqual(cursor.executed,[])

    def test_untimed_future_and_foreign_store_are_rejected_before_query(self):
        from quadringent.window_destination import verify_closed_window_destination
        from quadringent.object_store import S3ObjectStore
        for timed,observed in ((False,NOW+timedelta(seconds=610)),(True,NOW)):
            with self.subTest(timed=timed),TemporaryDirectory() as directory:
                store,_,_,_=test_closed_window_raw.ClosedWindowRawTests().fixture(directory,timed=timed)
                cursor=IdentityCursor()
                with self.assertRaises(ValueError):verify_closed_window_destination(cursor,store,run_id='r1',window_id='w1',observed_at=observed,site=SITE)
                self.assertEqual(cursor.executed,[])
        cursor=IdentityCursor()
        for bucket,prefix in (('foreign','run'),(SITE.raw_bucket,'ibmi/ledger/sale/runs/other')):
            with self.subTest(bucket=bucket,prefix=prefix),self.assertRaises(ValueError):
                verify_closed_window_destination(cursor,S3ObjectStore(bucket,prefix,client=object()),run_id='r1',window_id='w1',observed_at=NOW,site=SITE)
        self.assertEqual(cursor.executed,[])

    def test_closed_raw_window_connects_to_destination_without_process_claim(self):
        from quadringent.window_destination import verify_closed_window_destination
        with TemporaryDirectory() as directory:
            store,_,event,_=test_closed_window_raw.ClosedWindowRawTests().fixture(directory,timed=True)
            proof=verify_closed_window_destination(IdentityCursor(event.event_id),store,
                run_id='r1',window_id='w1',observed_at=NOW+timedelta(seconds=610),site=SITE)
            self.assertEqual(proof['destination']['state'],'matched')
            self.assertEqual(proof['destination']['event_count'],1)
            self.assertEqual(proof['window']['end'],{'receiver':'R1','sequence':20})
            self.assertEqual(proof['process_state'],'not_observed')
            self.assertEqual(proof['storage_backend'],'local')

    def test_empty_window_does_not_claim_snowflake_delivery(self):
        from quadringent.window_destination import verify_closed_window_destination
        with TemporaryDirectory() as directory:
            store,_,_,_=test_closed_window_raw.ClosedWindowRawTests().fixture(directory,timed=True,empty=True)
            cursor=IdentityCursor()
            proof=verify_closed_window_destination(cursor,store,run_id='r1',window_id='w1',observed_at=NOW+timedelta(seconds=610),site=SITE)
            self.assertEqual(proof['destination']['state'],'not_tested')
            self.assertEqual(cursor.executed,[])

    def test_exact_population_reconciles_without_capture_snapshot(self):
        from quadringent.snowflake_autonomous import reconcile_autonomous_files
        cursor=IdentityCursor()
        result=reconcile_autonomous_files(cursor,default_autonomous_plan(),
            object_keys=[f'{SITE.stream_prefix}/runs/r1/batch-a.jsonl'],expected_rows=1,
            expected_event_ids_sha256=hashlib.sha256(('a'*64).encode()).hexdigest(),site=SITE)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['raw_rows_after_second'],1)
        self.assertTrue(all(sql.startswith('SELECT') for sql in cursor.executed))

    def test_count_and_identity_mismatch_cannot_pass(self):
        from quadringent.snowflake_autonomous import reconcile_autonomous_files
        for count,identity in ((2,'a'*64),(1,'b'*64)):
            with self.subTest(count=count,identity=identity),self.assertRaises(ValueError):
                reconcile_autonomous_files(IdentityCursor(identity),default_autonomous_plan(),
                    object_keys=[f'{SITE.stream_prefix}/runs/r1/batch-a.jsonl'],expected_rows=count,
                    expected_event_ids_sha256=hashlib.sha256(('a'*64).encode()).hexdigest(),site=SITE)
