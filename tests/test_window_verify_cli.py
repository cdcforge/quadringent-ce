from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


class WindowVerifyCliTests(unittest.TestCase):
    def test_missing_raw_after_closure_is_an_error_not_pending(self):
        import quadringent_window_verify as cli
        connector=Mock()
        snowflake=ModuleType('snowflake')
        snowflake.connector=connector
        with TemporaryDirectory() as directory:
            stderr=StringIO()
            with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'S3ObjectStore',return_value=Mock()),patch.object(cli,'verify_closed_window_destination',side_effect=FileNotFoundError('private missing raw')),redirect_stdout(StringIO()),redirect_stderr(stderr):
                code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(Path(directory)/'proof.json'),'--await-window'])
            self.assertEqual(code,3)
            self.assertEqual(json.loads(stderr.getvalue())['publication_state'],'not_attempted')

    def test_waiting_for_closure_does_not_connect_to_snowflake(self):
        import quadringent_window_verify as cli
        connector=Mock()
        snowflake=ModuleType('snowflake')
        snowflake.connector=connector
        store=Mock()
        store.get_bounded.side_effect=FileNotFoundError('closed window absent')
        with TemporaryDirectory() as directory:
            output=Path(directory)/'proof.json'
            stdout=StringIO()
            with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'S3ObjectStore',return_value=store),patch.object(cli,'_connect_snowflake') as connect,redirect_stdout(stdout),redirect_stderr(StringIO()):
                code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(output),'--await-window'])
            self.assertEqual(code,2)
            self.assertEqual(json.loads(stdout.getvalue())['reason'],'window_not_closed')
            connect.assert_not_called()
            store.put_once.assert_not_called()
            self.assertFalse(output.exists())

    def test_resume_rejects_noncanonical_or_tampered_proof(self):
        import quadringent_window_verify as cli
        from copy import deepcopy
        from test_window_delivery_projection import evidence
        for mode in ('boolean_count','extra_field','pretty_json','naive_time','before_seal'):
            with self.subTest(mode=mode):
                fresh=evidence()
                previous=deepcopy(fresh)
                if mode=='boolean_count':previous['destination']['event_count']=True
                if mode=='extra_field':previous['untrusted']='extra'
                if mode=='naive_time':previous['destination']['observed_at']='2026-09-09T00:00:00'
                if mode=='before_seal':previous['destination']['observed_at']=previous['window']['intent']['started_at']
                payload=json.dumps(previous,indent=2).encode() if mode=='pretty_json' else cli._encode(previous)
                with self.assertRaises(ValueError):cli._revalidated_payload(payload,fresh)

    def test_resume_missing_or_symlink_file_never_connects(self):
        import quadringent_window_verify as cli
        for mode in ('missing','symlink','oversized'):
            with self.subTest(mode=mode),TemporaryDirectory() as directory:
                output=Path(directory)/'proof.json'
                if mode=='symlink':output.symlink_to(Path(directory)/'missing')
                if mode=='oversized':output.write_bytes(b'x'*(1024*1024+1))
                with patch.object(cli,'_publication_client') as connect,redirect_stderr(StringIO()):
                    code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(output),'--publish-window-proof','--resume-publication'])
                self.assertEqual(code,3)
                connect.assert_not_called()

    def test_resume_revalidates_but_preserves_original_bytes_and_observation(self):
        import quadringent_window_verify as cli
        from copy import deepcopy
        from datetime import datetime, timedelta
        from test_window_delivery_projection import evidence
        for mode in ('same','different_population','future_observation'):
            with self.subTest(mode=mode),TemporaryDirectory() as directory:
                previous=evidence()
                fresh=deepcopy(previous)
                stamp=datetime.fromisoformat(previous['destination']['observed_at'])
                fresh['destination']['observed_at']=(stamp+timedelta(seconds=30)).isoformat()
                if mode=='different_population':fresh['destination']['event_ids_sha256']='b'*64
                if mode=='future_observation':previous['destination']['observed_at']=(stamp+timedelta(hours=1)).isoformat()
                payload=json.dumps(previous,sort_keys=True,separators=(',',':')).encode()+b'\n'
                output=Path(directory)/'proof.json'
                output.write_bytes(payload)
                connector=Mock()
                snowflake=ModuleType('snowflake')
                snowflake.connector=connector
                store=Mock()
                store.get_bounded.return_value=payload
                with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'S3ObjectStore',return_value=store),patch.object(cli,'verify_closed_window_destination',return_value=fresh) as verify,redirect_stdout(StringIO()),redirect_stderr(StringIO()):
                    code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(output),'--publish-window-proof','--resume-publication'])
                self.assertEqual(code,0 if mode=='same' else 3)
                verify.assert_called_once()
                self.assertEqual(output.read_bytes(),payload)
                if mode=='same':store.put_once.assert_called_once_with('windows/w1/destination.json',payload)
                else:store.put_once.assert_not_called()

    def test_unbound_proof_and_collision_never_claim_publication(self):
        import quadringent_window_verify as cli
        from test_window_delivery_projection import evidence
        for mode in ('legacy','local','wrong_run','collision','lost_response','readback_error'):
            with self.subTest(mode=mode),TemporaryDirectory() as directory:
                connector=Mock()
                snowflake=ModuleType('snowflake')
                snowflake.connector=connector
                proof=evidence()
                if mode=='legacy':proof['window']['intent']['format_version']='quadringent-window-intent-v1'
                if mode=='local':proof['storage_backend']='local'
                if mode=='wrong_run':proof['archive_run_id']='other'
                store=Mock()
                applied={}
                def applied_then_lost(key,payload):
                    applied[key]=payload
                    raise OSError('private timeout')
                if mode=='collision':store.put_once.side_effect=ValueError('private collision')
                if mode=='lost_response':store.put_once.side_effect=applied_then_lost
                if mode=='readback_error':store.get_bounded.side_effect=OSError('private read error')
                stdout,stderr=StringIO(),StringIO()
                output=Path(directory)/'proof.json'
                with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'S3ObjectStore',return_value=store),patch.object(cli,'verify_closed_window_destination',return_value=proof),redirect_stdout(stdout),redirect_stderr(stderr):
                    code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(output),'--publish-window-proof'])
                self.assertEqual(code,3)
                self.assertEqual(stdout.getvalue(),'')
                self.assertNotIn('private',stderr.getvalue())
                self.assertEqual(json.loads(stderr.getvalue())['publication_state'],'not_attempted' if mode in ('legacy','local','wrong_run') else 'unknown')
                if mode in ('legacy','local','wrong_run'):
                    store.put_once.assert_not_called()
                    self.assertFalse(output.exists())
                else:self.assertEqual(json.loads(output.read_bytes()),proof)
                if mode=='lost_response':self.assertEqual(applied['windows/w1/destination.json'],output.read_bytes())

    def test_publication_is_explicit_and_readback_is_required(self):
        import quadringent_window_verify as cli
        from test_window_delivery_projection import evidence
        for publish,corrupt in ((False,False),(True,False),(True,True)):
            with self.subTest(publish=publish,corrupt=corrupt),TemporaryDirectory() as directory:
                connector=Mock()
                snowflake=ModuleType('snowflake')
                snowflake.connector=connector
                proof=evidence()
                payload=json.dumps(proof,sort_keys=True,separators=(',',':')).encode()+b'\n'
                store=Mock()
                store.get_bounded.return_value=b'wrong' if corrupt else payload
                stdout,stderr=StringIO(),StringIO()
                with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'S3ObjectStore',return_value=store),patch.object(cli,'verify_closed_window_destination',return_value=proof),redirect_stdout(stdout),redirect_stderr(stderr):
                    code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(Path(directory)/'proof.json')]+(['--publish-window-proof'] if publish else []))
                self.assertEqual(code,3 if corrupt else 0)
                if publish:
                    store.put_once.assert_called_once_with('windows/w1/destination.json',payload)
                    store.get_bounded.assert_called_once()
                else:store.put_once.assert_not_called()
                if not corrupt:self.assertEqual(json.loads(stdout.getvalue())['proof_published'],publish)

    def test_partial_load_is_pending_not_a_successful_proof(self):
        import quadringent_window_verify as cli
        from quadringent.snowflake_autonomous import DestinationLoadPending
        connector=Mock()
        snowflake=ModuleType('snowflake')
        snowflake.connector=connector
        with TemporaryDirectory() as directory:
            out=Path(directory)/'proof.json'
            stdout=StringIO()
            with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'verify_closed_window_destination',side_effect=DestinationLoadPending('private details')),redirect_stdout(stdout),redirect_stderr(StringIO()):
                code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(out)])
            self.assertEqual(code,2)
            self.assertEqual(json.loads(stdout.getvalue())['status'],'pending')
            self.assertFalse(out.exists())
            connector.connect.return_value.cursor.return_value.close.assert_called_once()
            connector.connect.return_value.close.assert_called_once()

    def test_help_and_invalid_identity_require_no_credentials(self):
        for args,code in ((['--help'],0),(['--run-id','../escape','--window-id','w1','--proof-output','/unused'],2)):
            result=subprocess.run([sys.executable,'scripts/quadringent_window_verify.py',*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,code,result.stderr)

    def test_output_status_resources_and_secret_safe_failure(self):
        import quadringent_window_verify as cli
        for state in ('matched','not_tested','failure'):
            with self.subTest(state=state),TemporaryDirectory() as directory:
                connector=Mock()
                snowflake=ModuleType('snowflake')
                snowflake.connector=connector
                result={'format_version':'quadringent-window-destination-v1','destination':{'state':state}}
                out=Path(directory)/'proof.json'
                stdout,stderr=StringIO(),StringIO()
                with patch.dict(sys.modules,{'boto3':Mock(),'snowflake':snowflake,'snowflake.connector':connector}),patch.object(cli,'_publication_client',return_value=Mock()),patch.object(cli,'verify_closed_window_destination',side_effect=RuntimeError('sensitive-connector-message') if state=='failure' else None,return_value=result),redirect_stdout(stdout),redirect_stderr(stderr):
                    code=cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(out)])
                self.assertEqual(code,3 if state=='failure' else 0 if state=='matched' else 2)
                connector.connect.return_value.cursor.return_value.close.assert_called_once()
                connector.connect.return_value.close.assert_called_once()
                if state=='failure':
                    self.assertFalse(out.exists())
                    self.assertNotIn('sensitive-connector-message',stderr.getvalue())
                else:
                    self.assertEqual(json.loads(out.read_bytes()),result)

    def test_existing_output_is_not_overwritten(self):
        import quadringent_window_verify as cli
        with TemporaryDirectory() as directory:
            from quadringent.object_store import FileObjectStore
            root=Path(directory)
            FileObjectStore(root).put_once('proof.json',b'original')
            with redirect_stderr(StringIO()):
                self.assertEqual(cli.main(['--run-id','r1','--window-id','w1','--proof-output',str(root/'proof.json')]),3)
            self.assertEqual((root/'proof.json').read_bytes(),b'original')
