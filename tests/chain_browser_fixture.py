"""Local-only browser fixture. Uses synthetic events and a fake SQL cursor."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'scripts')]

from quadringent.checkpoint import JsonCheckpointStore
from quadringent.contract import JournalPosition
from quadringent.object_store import FileObjectStore, RawFirstCaptureCoordinator
from quadringent.proof_windows import begin_window, prepare_window_chain, prepare_successor_window, close_eligible_window
from quadringent.raw import RawBatchWriter
from quadringent.window_destination import verify_closed_window_destination
from quadringent_control_plane.repository import ProjectionRepository, bind_window_proofs, parse_source_spec
from quadringent_control_plane.server import serve
from test_continuous import event
from test_window_destination import IdentityCursor, SITE
from test_window_delivery_projection import fresh_running_document, STREAM_ID


def main():
    with TemporaryDirectory(prefix='quadringent-chain-browser-') as directory:
        root = Path(directory)
        store = FileObjectStore(root/'r1')
        checkpoint = JsonCheckpointStore(root/'checkpoint.json')
        checkpoint.commit(JournalPosition('R1', 9))
        start = datetime.now(timezone.utc)-timedelta(seconds=1300)
        identity = 'w1'
        begin_window(store, checkpoint, window_id=identity, stream_id=STREAM_ID,
                     started_at=start, duration_seconds=600)
        prepare_window_chain(store, checkpoint, initial_window_id=identity, window_count=2)
        for index in range(2):
            previous = checkpoint.load()
            end = JournalPosition('R1', previous.sequence+10)
            source_event = replace(event('R1', previous.sequence+2), table='SALE')
            manifest = RawBatchWriter(root/'input').write_batch([source_event], high_watermark=end)
            stem = root/'input'/('batch-'+manifest.batch_id)
            at = start+timedelta(seconds=(index+1)*600)
            RawFirstCaptureCoordinator(store, checkpoint).capture_receipted_window(
                start=JournalPosition('R1', previous.sequence+1), end=end, previous=previous,
                payload=Path(str(stem)+'.jsonl').read_bytes(),
                manifest_content=Path(str(stem)+'.manifest.json').read_bytes(), scan_completed_at=at)
            close_eligible_window(store, checkpoint, window_id=identity, now=at)
            proof = verify_closed_window_destination(IdentityCursor(source_event.event_id), store,
                        run_id='r1', window_id=identity, observed_at=at+timedelta(seconds=1), site=SITE)
            store.put_once('windows/'+identity+'/destination.json', json.dumps(proof).encode())
            if index == 0:
                identity = prepare_successor_window(store, checkpoint, predecessor_id=identity, now=at)['window_id']
        last = root/'r1'/'windows'/identity/'destination.json'
        hidden = last.with_name('destination.hidden')
        last.rename(hidden)
        document = fresh_running_document()
        document['generated_at'] = datetime.now(timezone.utc).isoformat()
        document['run']['state'] = 'STOPPED_PROOF_CHAIN'
        capture = root/'capture.json'
        capture.write_text(json.dumps(document))
        source = parse_source_spec('simulation:dev-sale:'+capture.as_uri(), environment=SITE.environment)
        repository = ProjectionRepository(bind_window_proofs([source],
            ['dev-sale='+(root/'r1'/'window-chain.json').as_uri()]))
        repository.refresh()
        server = serve(repository, port=0, ui_dist=ROOT/'ui'/'dist')
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({'url': 'http://127.0.0.1:'+str(server.server_port), 'simulation': True}), flush=True)
        try:
            for command in sys.stdin:
                command = command.strip()
                if command == 'quit': break
                if command == 'restore': hidden.rename(last)
                elif command == 'hide': last.rename(hidden)
                else: raise ValueError('invalid fixture command')
                snapshot = repository.refresh()
                print(json.dumps({'revision': snapshot.revision}), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    main()
