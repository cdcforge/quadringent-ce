import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
import unittest

from quadringent_window_supervise import supervise


class WindowSupervisorTests(unittest.TestCase):
    def test_hour_window_can_wait_more_than_128_pending_observations(self):
        clock=[0.0]
        calls=[]
        def run(command,**kwargs):
            calls.append(command)
            return self.result('pending',2) if clock[0]<3600 else self.result('matched',publication_state='confirmed',proof_published=True)
        with TemporaryDirectory() as directory:
            result=supervise(run_id='r1',window_id='w1',proof_output=Path(directory)/'proof.json',budget_seconds=4000,run=run,monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(result['status'],'matched')
        self.assertGreater(len(calls),128)

    def test_local_failure_after_timeout_preserves_unknown(self):
        clock=[0.0]
        calls=[]
        with TemporaryDirectory() as directory:
            output=Path(directory)/'proof.json'
            def run(command,**kwargs):
                calls.append(command)
                if len(calls)==1:
                    output.write_bytes(b'original')
                    raise TimeoutExpired(command,kwargs['timeout'])
                raise OSError('private spawn failure')
            result=supervise(run_id='r1',window_id='w1',proof_output=output,run=run,monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(result['status'],'supervisor_failed')
        self.assertEqual(result['publication_state'],'unknown')

    def test_definitive_error_stops_and_bad_success_cannot_pass(self):
        for report in (CompletedProcess([],3,'',json.dumps({'status':'error','publication_state':'not_attempted'})),
                       self.result('matched',proof_published=False,publication_state='unknown'),
                       self.result('matched',proof_published=True,publication_state='confirmed',run_id='other')):
            with self.subTest(report=report.stdout),TemporaryDirectory() as directory:
                calls=[]
                def run(command,**kwargs):
                    calls.append(command)
                    return report
                result=supervise(run_id='r1',window_id='w1',proof_output=Path(directory)/'proof.json',run=run)
                self.assertNotEqual(result['status'],'matched')
                self.assertEqual(len(calls),1)

    def test_publication_unknown_retries_and_empty_window_is_not_matched(self):
        clock=[0.0]
        calls=[]
        with TemporaryDirectory() as directory:
            output=Path(directory)/'proof.json'
            def run(command,**kwargs):
                calls.append(command)
                if len(calls)==1:
                    output.write_bytes(b'original')
                    return CompletedProcess([],3,'',json.dumps({'status':'error','publication_state':'unknown'}))
                return self.result('not_tested',2,publication_state='confirmed',proof_published=True)
            result=supervise(run_id='r1',window_id='w1',proof_output=output,run=run,monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(result['status'],'not_tested')
        self.assertIn('--resume-publication',calls[1])

    def result(self,status,code=0,**extra):
        value={'status':status,'run_id':'r1','window_id':'w1',**extra}
        return CompletedProcess([],code,json.dumps(value),'')

    def test_pending_then_confirmed(self):
        clock=[0.0]
        calls=[]
        def run(command,**kwargs):
            calls.append(command)
            return self.result('pending',2) if len(calls)==1 else self.result('matched',publication_state='confirmed',proof_published=True)
        with TemporaryDirectory() as directory:
            result=supervise(run_id='r1',window_id='w1',proof_output=Path(directory)/'proof.json',budget_seconds=30,attempt_seconds=10,interval_seconds=1,run=run,monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(result['status'],'matched')
        self.assertEqual(len(calls),2)
        self.assertIn('--publish-window-proof',calls[0])

    def test_timeout_with_saved_proof_automatically_resumes(self):
        clock=[0.0]
        calls=[]
        with TemporaryDirectory() as directory:
            output=Path(directory)/'proof.json'
            def run(command,**kwargs):
                calls.append(command)
                if len(calls)==1:
                    output.write_bytes(b'original')
                    clock[0]+=kwargs['timeout']
                    raise TimeoutExpired(command,kwargs['timeout'])
                return self.result('matched',publication_state='confirmed',proof_published=True)
            result=supervise(run_id='r1',window_id='w1',proof_output=output,budget_seconds=30,attempt_seconds=10,interval_seconds=1,run=run,monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(result['status'],'matched')
        self.assertIn('--resume-publication',calls[1])

    def test_pending_never_outlives_budget(self):
        clock=[0.0]
        def run(command,**kwargs):
            clock[0]+=kwargs['timeout']
            raise TimeoutExpired(command,kwargs['timeout'])
        with TemporaryDirectory() as directory:
            result=supervise(run_id='r1',window_id='w1',proof_output=Path(directory)/'proof.json',budget_seconds=30,attempt_seconds=10,interval_seconds=1,run=run,monotonic=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
        self.assertEqual(result['status'],'budget_exhausted')
        self.assertLessEqual(clock[0],30)
