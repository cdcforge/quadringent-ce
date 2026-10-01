#!/usr/bin/env python3
"""Bounded verification/publication of an already closed site window; never starts capture."""
import argparse
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
_src = str(ROOT / 'src')
if _src not in sys.path:
    sys.path.insert(0, _src)

from quadringent.site_config import current as current_site


def supervise(*,run_id,window_id,proof_output,budget_seconds=300,attempt_seconds=60,
              interval_seconds=10,credential_args=(),run=subprocess.run,
              monotonic=time.monotonic,sleep=time.sleep):
    for identity in (run_id,window_id):
        if not isinstance(identity,str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',identity) is None:
            raise ValueError('invalid window identity')
    for value,lower,upper in ((budget_seconds,30,5400),(attempt_seconds,5,120),(interval_seconds,1,60)):
        if type(value) is not int or not lower<=value<=upper:
            raise ValueError('invalid supervision budget')
    output=Path(proof_output).absolute()
    if output.is_symlink():
        raise ValueError('proof path must not be a symlink')
    command=[sys.executable,str(Path(__file__).with_name('quadringent_window_verify.py')),
             '--run-id',run_id,'--window-id',window_id,'--proof-output',str(output),
             '--publish-window-proof','--await-window',*credential_args]
    deadline=monotonic()+budget_seconds
    attempts=0
    publication='not_attempted'
    def result(status):
        return {'status':status,'run_id':run_id,'window_id':window_id,
                'attempts':attempts,'publication_state':publication,'capture_started':False}
    max_attempts=budget_seconds//interval_seconds+2
    while monotonic()<deadline and attempts<max_attempts:
        attempts+=1
        try:
            current=command+(['--resume-publication'] if output.exists() else [])
            completed=run(current,capture_output=True,text=True,check=False,
                          timeout=min(attempt_seconds,max(0.001,deadline-monotonic())))
        except subprocess.TimeoutExpired:
            # The child may have committed its PUT before being killed/reaped.
            publication='unknown'
        except Exception:
            return result('supervisor_failed')
        else:
            try:
                report=json.loads(completed.stdout if completed.returncode in (0,2) else completed.stderr)
                if not isinstance(report,dict):raise ValueError('invalid child report')
            except (ValueError,TypeError):
                publication='unknown'
                return result('invalid_child_report')
            state=report.get('status')
            if completed.returncode in (0,2) and (report.get('run_id')!=run_id or report.get('window_id')!=window_id):
                publication='unknown'
                return result('invalid_child_report')
            if (completed.returncode in (0,2) and state in ('matched','not_tested')
                    and report.get('publication_state')=='confirmed' and report.get('proof_published') is True
                    and completed.returncode==(0 if state=='matched' else 2)):
                publication='confirmed'
                return result(state if monotonic()<=deadline else 'budget_exhausted')
            if completed.returncode==2 and state=='pending':
                pass
            elif completed.returncode==3 and state=='error' and report.get('publication_state')=='unknown':
                publication='unknown'
            else:
                if completed.returncode in (0,2):publication='unknown'
                return result('verification_failed')
        remaining=deadline-monotonic()
        if remaining>0:
            try:
                sleep(min(interval_seconds,remaining))
            except Exception:
                return result('supervisor_failed')
    return result('budget_exhausted')


def supervise_chain(*, run_id, proof_directory, load_chain, budget_seconds=300,
                    attempt_seconds=60, interval_seconds=10, credential_args=(),
                    run=subprocess.run, monotonic=time.monotonic, sleep=time.sleep):
    """Qualify each closed window in order under one global deadline."""
    if not isinstance(run_id, str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', run_id) is None:
        raise ValueError('invalid run identity')
    for value, lower, upper in ((budget_seconds,30,5400),(attempt_seconds,5,120),(interval_seconds,1,60)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError('invalid supervision budget')
    directory = Path(proof_directory).absolute()
    if directory.is_symlink(): raise ValueError('proof directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True)
    deadline = monotonic() + budget_seconds
    matched = []
    binding = None
    def result(status, window=None):
        return {'status': status, 'run_id': run_id, 'matched_window_ids': list(matched),
                'window_id': window, 'capture_started': False}
    for _ in range(budget_seconds // interval_seconds + 130):
        remaining = deadline - monotonic()
        if remaining <= 0: return result('budget_exhausted')
        try:
            chain = load_chain(min(attempt_seconds, remaining))
        except subprocess.TimeoutExpired:
            chain = None
        except Exception:
            return result('chain_read_failed')
        if monotonic() >= deadline: return result('budget_exhausted')
        if chain is None:
            sleep(min(interval_seconds, deadline-monotonic()))
            continue
        current_binding = (chain['initial_window_id'], chain['window_count'], chain['stream_id'])
        if binding is not None and binding != current_binding:
            return result('chain_changed')
        binding = current_binding
        closed = chain['closed_window_ids']
        if closed[:len(matched)] != matched:
            return result('chain_changed')
        if len(closed) > len(matched):
            identity = closed[len(matched)]
            remaining = math.floor(deadline-monotonic())
            if remaining < 30: return result('budget_exhausted', identity)
            verified = supervise(run_id=run_id, window_id=identity,
                                 proof_output=directory/(identity+'.json'),
                                 budget_seconds=remaining, attempt_seconds=attempt_seconds,
                                 interval_seconds=interval_seconds, credential_args=credential_args,
                                 run=run, monotonic=monotonic, sleep=sleep)
            if verified['status'] != 'matched':
                return {**result(verified['status'], identity),
                        'publication_state': verified['publication_state']}
            matched.append(identity)
            continue
        if chain['capture_complete'] and len(matched) == chain['window_count']:
            return result('matched')
        sleep(min(interval_seconds, deadline-monotonic()))
    return result('budget_exhausted')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--window-id')
    parser.add_argument('--proof-output')
    parser.add_argument('--chain-proof-directory', help='Verify the entire reserved chain in order')
    parser.add_argument('--budget-seconds',type=int,default=300)
    parser.add_argument('--attempt-seconds',type=int,default=60)
    parser.add_argument('--interval-seconds',type=int,default=10)
    site=current_site()
    aws=parser.add_mutually_exclusive_group()
    aws.add_argument('--aws-profile',default=site.aws_profile)
    aws.add_argument('--aws-default-credentials',action='store_true')
    snow=parser.add_mutually_exclusive_group()
    snow.add_argument('--connection-name',default=site.snowflake_connection)
    snow.add_argument('--snowflake-oidc-token-file')
    args=parser.parse_args(argv)
    if args.chain_proof_directory:
        if args.window_id or args.proof_output: parser.error('chain mode cannot select a single window')
    elif not args.window_id or not args.proof_output:
        parser.error('single-window mode requires window-id and proof-output')
    if not args.snowflake_oidc_token_file and not args.connection_name:
        parser.error('Snowflake authentication requires --connection-name, '
                     '--snowflake-oidc-token-file or QUADRINGENT_SNOWFLAKE_CONNECTION')
    credentials=(['--aws-default-credentials'] if args.aws_default_credentials or not args.aws_profile
                 else ['--aws-profile',args.aws_profile])
    credentials+=['--snowflake-oidc-token-file',args.snowflake_oidc_token_file] if args.snowflake_oidc_token_file else ['--connection-name',args.connection_name]
    try:
        if args.chain_proof_directory:
            def load_chain(timeout):
                command = [sys.executable, str(Path(__file__).with_name('quadringent_window_chain_read.py')),
                           '--run-id', args.run_id, *credentials[:1 if args.aws_default_credentials else 2]]
                child = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
                if child.returncode not in (0, 2): raise ValueError('chain reader failed')
                report = json.loads(child.stdout)
                if report.get('run_id') != args.run_id: raise ValueError('chain reader identity differs')
                if child.returncode == 2 and report.get('status') == 'pending': return None
                if child.returncode != 0 or report.get('status') != 'observed': raise ValueError('invalid chain reader status')
                return report['chain']
            result = supervise_chain(run_id=args.run_id, proof_directory=args.chain_proof_directory,
                                     load_chain=load_chain, budget_seconds=args.budget_seconds,
                                     attempt_seconds=args.attempt_seconds, interval_seconds=args.interval_seconds,
                                     credential_args=credentials)
        else:
            result=supervise(run_id=args.run_id,window_id=args.window_id,proof_output=args.proof_output,
                         budget_seconds=args.budget_seconds,attempt_seconds=args.attempt_seconds,
                         interval_seconds=args.interval_seconds,credential_args=credentials)
    except Exception as error:
        print(json.dumps({'status':'error','error_type':type(error).__name__,'capture_started':False}),file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0 if result['status']=='matched' else 2 if result['status'] in ('not_tested','budget_exhausted') else 3


if __name__=='__main__':
    raise SystemExit(main())
