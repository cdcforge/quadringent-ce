import site_fixture

from dataclasses import replace
from datetime import datetime
from unittest.mock import patch
import unittest
import http.client
import json
from threading import Thread

from quadringent_control_plane.repository import ProjectionRepository, bind_window_proofs, parse_source_spec
from test_window_delivery_projection import fresh_running_document, evidence, NOW
from quadringent_control_plane.server import serve

SITE = site_fixture.build_test_site()


class WindowProofRepositoryTests(unittest.TestCase):
    def setUp(self):
        clock=self.enterContext(patch('quadringent_control_plane.repository.datetime',wraps=datetime))
        clock.now.return_value=NOW

    def test_s3_sidecar_identity_must_match_its_object_path(self):
        source=parse_source_spec('live:dev-sale:file:///capture.json',environment=SITE.environment)
        sources=bind_window_proofs([source],[f'dev-sale=s3://{SITE.raw_bucket}/{SITE.stream_prefix}/runs/r1/windows/w1/destination.json'])
        for run,expected in (('r1','matched'),('other','invalid')):
            with self.subTest(run=run):
                proof=evidence()
                proof['archive_run_id']=run
                with patch('quadringent_control_plane.repository._read_document',side_effect=lambda origin:proof if origin.startswith('s3:') else fresh_running_document()):
                    result=ProjectionRepository(sources).refresh().pipelines[0]
                self.assertEqual(result.window_delivery['state'],expected)

    def test_capture_read_failure_marks_retained_window_stale(self):
        with patch('quadringent_control_plane.repository._read_document',side_effect=lambda origin:evidence() if origin.endswith('window.json') else fresh_running_document()):
            repository=ProjectionRepository(self.sources())
            repository.refresh()
        with patch('quadringent_control_plane.repository._read_document',side_effect=OSError('unavailable')):
            pipeline=repository.refresh().pipelines[0]
        self.assertEqual(pipeline.window_delivery['quality']['freshness'],'stale')
        self.assertEqual(pipeline.status,'unknown')

    def sources(self):
        source=parse_source_spec('live:dev-sale:file:///capture.json',environment=SITE.environment)
        return bind_window_proofs([source],['dev-sale=file:///window.json'])

    def test_sidecar_loss_and_recovery_notify_sse_and_invalidate_rest_etag(self):
        available = True
        def read(origin):
            if origin.endswith('window.json'):
                if not available:
                    raise OSError('unavailable')
                return evidence()
            return fresh_running_document()
        with patch('quadringent_control_plane.repository._read_document',side_effect=read):
            repository=ProjectionRepository(self.sources())
            initial=repository.refresh()
            server=serve(repository,port=0)
            thread=Thread(target=server.serve_forever,daemon=True)
            thread.start()
            stream=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            try:
                def overview(etag=None):
                    client=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
                    try:
                        client.request('GET','/v1/overview',headers={'If-None-Match':etag} if etag else {})
                        response=client.getresponse()
                        return response.status,response.getheader('ETag'),json.loads(response.read())
                    finally:
                        client.close()
                def frame(response):
                    lines=[]
                    for _ in range(8):
                        line=response.fp.readline()
                        if line in (b'\n',b''):
                            break
                        lines.append(line)
                    return b''.join(lines)
                _,etag,baseline=overview()
                stream.request('GET','/v1/events')
                response=stream.getresponse()
                self.assertEqual(response.status,200)
                self.assertIn(b'event: stream.cursor',frame(response))
                for available,expected in ((False,'unavailable'),(True,'matched')):
                    with self.subTest(state=expected):
                        snapshot=repository.refresh()
                        self.assertGreater(snapshot.revision,initial.revision)
                        notification=frame(response)
                        self.assertIn(b'event: projection.updated',notification)
                        self.assertIn(f'id: {snapshot.revision}'.encode(),notification)
                        status,new_etag,body=overview(etag)
                        self.assertEqual(status,200)
                        self.assertNotEqual(new_etag,etag)
                        self.assertEqual(body['revision'],snapshot.revision)
                        pipeline=body['pipelines'][0]
                        self.assertEqual(pipeline['window_delivery']['state'],expected)
                        self.assertEqual(pipeline['counters'],baseline['pipelines'][0]['counters'])
                        self.assertEqual(pipeline['status'],baseline['pipelines'][0]['status'])
                        etag=new_etag
            finally:
                stream.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_invalid_then_recovered_sidecar_advances_revision_without_capture_loss(self):
        proof={}
        def read(origin):
            return proof if origin.endswith('window.json') else fresh_running_document()
        with patch('quadringent_control_plane.repository._read_document',side_effect=read):
            repository=ProjectionRepository(self.sources())
            first=repository.refresh()
            self.assertEqual(first.pipelines[0].window_delivery['state'],'invalid')
            proof=evidence()
            second=repository.refresh()
        self.assertGreater(second.revision,first.revision)
        self.assertEqual(second.sources[0].status,'available')
        self.assertEqual(second.pipelines[0].window_delivery['state'],'matched')
        self.assertEqual(first.pipelines[0].counters,second.pipelines[0].counters)

    def test_wrong_stream_is_not_joined(self):
        proof=evidence()
        proof['window']['intent']['stream_id']='other'
        with patch('quadringent_control_plane.repository._read_document',side_effect=lambda origin:proof if origin.endswith('window.json') else fresh_running_document()):
            snapshot=ProjectionRepository(self.sources()).refresh()
        self.assertEqual(snapshot.sources[0].status,'available')
        self.assertEqual(snapshot.pipelines[0].window_delivery['state'],'invalid')

    def test_sidecar_preserves_capture_and_cannot_upgrade_file_to_live(self):
        document=fresh_running_document()
        def read(origin):
            return evidence() if origin.endswith('window.json') else document
        with patch('quadringent_control_plane.repository._read_document',side_effect=read):
            repository=ProjectionRepository(self.sources())
            result=repository.refresh().pipelines[0].to_dict()
        self.assertEqual(result['window_delivery']['state'],'matched')
        self.assertEqual(result['window_delivery']['quality']['evidence_kind'],'simulation')
        self.assertNotIn('window_destination_proof',document)

    def test_missing_sidecar_does_not_remove_capture(self):
        def read(origin):
            if origin.endswith('window.json'):raise OSError('unavailable')
            return fresh_running_document()
        with patch('quadringent_control_plane.repository._read_document',side_effect=read):
            result=ProjectionRepository(self.sources()).refresh()
        self.assertEqual(result.sources[0].status,'available')
        self.assertEqual(result.pipelines[0].window_delivery['state'],'unavailable')

    def test_bindings_reject_unknown_duplicate_nondev_and_foreign_origins(self):
        source=parse_source_spec('live:dev-sale:file:///capture.json',environment=SITE.environment)
        for specs in (['other=file:///window.json'],['dev-sale=file:///w','dev-sale=file:///x'],
                      ['dev-sale=https://example.com/window'],['dev-sale=s3://foreign/windows/w1/destination.json']):
            with self.subTest(specs=specs),self.assertRaises(ValueError):
                bind_window_proofs([source],specs)
        with self.assertRaises(ValueError):
            bind_window_proofs([replace(source,descriptor=replace(source.descriptor,environment='prod'))],['dev-sale=file:///w'])
