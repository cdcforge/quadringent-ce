"""Read-only chain qualification for the cockpit; never infer process health."""
import json

from quadringent.proof_windows import read_window_chain, read_closed_window, _encode
from .projection import _window_delivery


class BoundedChainStore:
    """Cache metadata within one refresh, with a bounded read and memory budget."""
    def __init__(self, read):
        self.read = read
        self.cache = {}
        self.size = 0

    def get_bounded(self, key, limit):
        if key not in self.cache:
            if len(self.cache) >= 1024:
                raise ValueError('chain metadata read budget exceeded')
            value = self.read(key, limit)
            if not isinstance(value, bytes) or len(value) > limit:
                raise ValueError('chain metadata document exceeds budget')
            self.size += len(value)
            if self.size > 32 * 1024 * 1024:
                raise ValueError('chain metadata byte budget exceeded')
            self.cache[key] = value
        value = self.cache[key]
        if len(value) > limit: raise ValueError('chain metadata document exceeds budget')
        return value


def project_window_chain(store, *, run_id, flux, source, now, storage_backend):
    chain = read_window_chain(store, now=now)
    progress = {'declared_windows': chain['window_count'], 'matched_windows': 0,
                'capture_complete': chain['capture_complete'], 'state': 'incomplete',
                'evidence_kind': 'simulation' if storage_backend == 'local' else source.evidence_kind}
    latest = None
    for identity in chain['closed_window_ids']:
        try:
            proof = json.loads(store.get_bounded('windows/'+identity+'/destination.json', 1024*1024))
        except FileNotFoundError:
            return {'state': 'unavailable', 'reason': 'chain_destination_missing', 'chain': progress}
        if (not isinstance(proof, dict) or proof.get('archive_run_id') != run_id
                or proof.get('window_id') != identity
                or (storage_backend == 's3' and proof.get('storage_backend') != 's3')
                or _encode(proof.get('window')) != _encode(read_closed_window(store, window_id=identity))):
            return {'state': 'invalid', 'reason': 'chain_destination_mismatch', 'chain': progress}
        proof = {**proof, 'storage_backend': storage_backend}
        delivery = _window_delivery(proof, flux, source, now)
        if delivery['state'] != 'matched':
            return {**delivery, 'chain': progress}
        progress['matched_windows'] += 1
        latest = delivery
    if not chain['capture_complete'] or latest is None:
        return {'state': 'unavailable', 'reason': 'chain_capture_incomplete', 'chain': progress}
    progress['state'] = 'matched'
    return {**latest, 'chain': progress}
