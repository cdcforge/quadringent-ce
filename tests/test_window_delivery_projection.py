import site_fixture

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch
from dataclasses import asdict

from dataclasses import replace

from quadringent_control_plane.projection import project_console_document
from test_control_plane_projection import NOW, LIVE_SOURCE as _LIVE_SOURCE, fresh_running_document as base_document

SITE = site_fixture.build_test_site()
LIVE_SOURCE = replace(_LIVE_SOURCE, environment=SITE.environment)
STREAM_ID=f'{SITE.stream_prefix}/runs/qual0909a'

def fresh_running_document():
    from as400_continuous_capture import _flux_identity
    document=base_document()
    with patch.dict('os.environ',{'AS400_STREAM_KEY':STREAM_ID,'AS400_JOURNAL_NAME':SITE.journal_name,'ISERIES_SCHEMA':SITE.source_schema,'ISERIES_TABLE':SITE.proof_table,'ISERIES_TABLES':SITE.proof_table}):
        document['flux']=asdict(_flux_identity())
    document['flux']['objects']=list(document['flux']['objects'])
    return document


def evidence():
    stamp=(NOW-timedelta(seconds=20)).isoformat()
    return {'format_version':'quadringent-window-destination-v1','archive_run_id':'r1','window_id':'w1',
        'storage_backend':'s3','process_state':'not_observed',
        'window':{'format_version':'quadringent-closed-window-v2','window_id':'w1',
            'intent':{'format_version':'quadringent-window-intent-v2','window_id':'w1','stream_id':STREAM_ID,'started_at':(NOW-timedelta(seconds=620)).isoformat(),
                'duration_seconds':600,'closure_grace_seconds':60,'previous':{'receiver':'R1','sequence':9}},
            'closed_at':stamp,'sealed_at':stamp,'end':{'receiver':'R1','sequence':20},'event_count':1},
        'destination':{'state':'matched','event_count':1,'event_ids_sha256':'a'*64,'observed_at':stamp,
            'metrics':{'status':'PASS','database':SITE.snowflake_scope.database,'schema':SITE.snowflake_scope.schema,'stage':SITE.proof_stage,
                'raw_table':SITE.proof_raw_table,'canonical_table':SITE.proof_canonical_table,
                'raw_rows_after_second':1,'distinct_event_ids_after_second':1,'canonical_rows_after_second':1}}}


class WindowDeliveryProjectionTests(unittest.TestCase):
    def test_worker_qualifies_only_unqualified_objects_when_schema_is_known(self):
        from as400_continuous_capture import _flux_identity
        for schema,tables,expected in (
            (SITE.source_schema,f'{SITE.source_schema}.SALE',(f'{SITE.source_schema}.SALE',)),
            (SITE.source_schema,f'SALE, {SITE.source_schema}.CNTR',(f'{SITE.source_schema}.SALE',f'{SITE.source_schema}.CNTR')),
            ('','SALE',('SALE',)),
        ):
            with self.subTest(schema=schema,tables=tables),patch.dict('os.environ',{'ISERIES_SCHEMA':schema,'ISERIES_TABLE':'SALE','ISERIES_TABLES':tables}):
                self.assertEqual(_flux_identity().objects,expected)

    def test_recent_destination_observation_does_not_refresh_old_window(self):
        proof=evidence()
        now=NOW+timedelta(hours=1)
        proof['destination']['observed_at']=now.isoformat()
        document=fresh_running_document()
        document['window_destination_proof']=proof
        delivery=project_console_document(document,LIVE_SOURCE,now).to_dict()['window_delivery']
        self.assertEqual(delivery['quality']['freshness'],'stale')

    def test_empty_window_is_not_delivery_and_foreign_scope_is_invalid(self):
        proof=evidence()
        proof['window']['event_count']=0
        proof['destination']={'state':'not_tested','reason':'no_events','event_count':0,'observed_at':NOW.isoformat()}
        document=fresh_running_document()
        document['window_destination_proof']=proof
        self.assertEqual(project_console_document(document,LIVE_SOURCE,NOW).to_dict()['window_delivery']['state'],'not_tested')
        document['flux']['objects']=[f'{SITE.source_schema}.CNTR']
        self.assertEqual(project_console_document(document,LIVE_SOURCE,NOW).to_dict()['window_delivery']['state'],'invalid')

    def test_real_window_adapter_projects_without_upgrading_local_evidence(self):
        from tempfile import TemporaryDirectory
        from test_closed_window_raw import ClosedWindowRawTests, NOW as WINDOW_START
        from test_window_destination import IdentityCursor
        from quadringent.window_destination import verify_closed_window_destination
        with TemporaryDirectory() as directory:
            store,_,event,_=ClosedWindowRawTests().fixture(directory,timed=True,stream_id=STREAM_ID)
            observed=WINDOW_START+timedelta(seconds=610)
            proof=verify_closed_window_destination(IdentityCursor(event.event_id),store,run_id='r1',window_id='w1',observed_at=observed,site=SITE)
            document=fresh_running_document()
            document['window_destination_proof']=proof
            delivery=project_console_document(document,LIVE_SOURCE,max(NOW,observed)).to_dict()['window_delivery']
            self.assertEqual(delivery['state'],'matched')
            self.assertEqual(delivery['quality']['evidence_kind'],'simulation')

    def test_window_delivery_never_replaces_current_capture_verdict(self):
        document=fresh_running_document()
        baseline=project_console_document(document,LIVE_SOURCE,NOW).to_dict()
        document['window_destination_proof']=evidence()
        result=project_console_document(document,LIVE_SOURCE,NOW).to_dict()
        self.assertEqual(result['window_delivery']['state'],'matched')
        for key in ('status','counters','stages','quality','observed_at'):
            self.assertEqual(result[key],baseline[key])

    def test_local_stale_and_wrong_stream_cannot_become_fresh_live(self):
        for mode in ('local','stale','wrong_stream','counts','missing_stream','legacy_intent'):
            with self.subTest(mode=mode):
                proof=deepcopy(evidence())
                if mode=='local':proof['storage_backend']='local'
                if mode=='wrong_stream':proof['window']['intent']['stream_id']='other'
                if mode=='counts':proof['destination']['event_count']=2
                if mode=='missing_stream':del proof['window']['intent']['stream_id']
                if mode=='legacy_intent':proof['window']['intent']['format_version']='quadringent-window-intent-v1'
                document=fresh_running_document()
                document['window_destination_proof']=proof
                now=NOW+timedelta(hours=1) if mode=='stale' else NOW
                delivery=project_console_document(document,LIVE_SOURCE,now).to_dict()['window_delivery']
                if mode in ('wrong_stream','counts','missing_stream','legacy_intent'):self.assertEqual(delivery['state'],'invalid')
                elif mode=='local':self.assertEqual(delivery['quality']['evidence_kind'],'simulation')
                else:self.assertEqual(delivery['quality']['freshness'],'stale')
