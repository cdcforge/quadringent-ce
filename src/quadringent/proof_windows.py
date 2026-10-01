"""Immutable receipt windows; raw integrity and destination reconciliation are separate gates."""
from datetime import datetime, timedelta
import hashlib
import json
import re

from .contract import JournalPosition
from .object_store import receipt_index_key

MAX_RECEIPTS = 4096
MAX_DOCUMENT_BYTES = 1024 * 1024


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def _prefix(window_id):
    if not isinstance(window_id, str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', window_id) is None:
        raise ValueError('invalid proof window ID')
    return 'windows/' + window_id + '/'


def _position(value):
    if not isinstance(value, dict) or set(value) != {'receiver', 'sequence'}:
        raise ValueError('invalid window position')
    if not isinstance(value['receiver'], str) or not value['receiver'] or type(value['sequence']) is not int or value['sequence'] < 0:
        raise ValueError('invalid window position')
    return value


def _checkpoint(value):
    if value is None:
        raise ValueError('continuous windows require an established checkpoint')
    return _position({'receiver': value.receiver, 'sequence': value.sequence})


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('window timestamp must be a string')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('window timestamps require timezone')
    return parsed


def _read(store, key, limit=MAX_DOCUMENT_BYTES):
    payload = store.get_bounded(key, limit)
    if not 0 < len(payload) <= limit:
        raise ValueError('window metadata exceeds budget')
    return payload, json.loads(payload)


def _stream_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', value) is None
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('invalid window stream ID')
    return value


def begin_window(store, checkpoint, *, window_id, started_at, duration_seconds, closure_grace_seconds=60, stream_id=None):
    prefix = _prefix(window_id)
    if type(duration_seconds) is not int or not 600 <= duration_seconds <= 3600:
        raise ValueError('window duration must be 600 to 3600 seconds')
    _timestamp(started_at.isoformat())
    if type(closure_grace_seconds) is not int or not 0 <= closure_grace_seconds <= 120:
        raise ValueError('invalid closure grace')
    intent = {'format_version': 'quadringent-window-intent-v1', 'window_id': window_id,
              'previous': _checkpoint(checkpoint.load()), 'started_at': started_at.isoformat(),
              'duration_seconds': duration_seconds, 'closure_grace_seconds': closure_grace_seconds}
    if stream_id is not None:
        intent.update(format_version='quadringent-window-intent-v2', stream_id=_stream_id(stream_id))
    store.put_once(prefix+'intent.json', _encode(intent))
    return intent


def prepare_window(store, checkpoint, *, window_id, stream_id, now, duration_seconds, closure_grace_seconds=60):
    """Create once or recover an existing bound window before source access."""
    _stream_id(stream_id)
    _timestamp(now.isoformat())
    _checkpoint(checkpoint.load())
    try:
        intent=_read_intent(store,window_id)
    except FileNotFoundError:
        intent=begin_window(store,checkpoint,window_id=window_id,stream_id=stream_id,
                            started_at=now,duration_seconds=duration_seconds,
                            closure_grace_seconds=closure_grace_seconds)
    if (intent.get('stream_id')!=stream_id or intent['duration_seconds']!=duration_seconds
            or intent['closure_grace_seconds']!=closure_grace_seconds):
        raise ValueError('window restart configuration differs')
    close_eligible_window(store,checkpoint,window_id=window_id,now=now)
    return intent


def prepare_window_chain(store, checkpoint, *, initial_window_id, window_count):
    """Bind the budget before the first scan; never retrofit an advanced run."""
    # Store is scoped to the reserved run, not to a chosen window: changing
    # the initial window must not permit a second budget in the same run.
    key = 'window-chain.json'
    try:
        _, existing = _read(store, key)
    except FileNotFoundError:
        exists = False
    else:
        exists = True
    if window_count is None:
        if exists:
            raise ValueError('cannot omit the durable window budget')
        return None
    if type(window_count) is not int or not 1 <= window_count <= 128:
        raise ValueError('invalid window chain budget')
    intent = _read_intent(store, initial_window_id)
    if intent['format_version'] != 'quadringent-window-intent-v2':
        raise ValueError('window chain requires a stream-bound initial intent')
    expected = {'format_version': 'quadringent-window-chain-v1',
                'initial_window_id': initial_window_id, 'window_count': window_count,
                'stream_id': intent['stream_id'],
                'initial_intent_sha256': hashlib.sha256(_encode(intent)).hexdigest()}
    if exists:
        if _encode(existing) != _encode(expected):
            raise ValueError('window chain restart configuration differs')
    else:
        if _checkpoint(checkpoint.load()) != intent['previous']:
            raise ValueError('cannot declare a window budget after capture advanced')
        store.put_once(key, _encode(expected))
    return expected


def read_window_chain(store, *, now):
    """Read a contiguous durable capture prefix, never create or certify delivery.

    Only missing closure/link/next intent means pending. Missing dependencies
    inside a declared closure are errors, not permission to skip that window.
    """
    observed = _timestamp(now.isoformat())
    _, contract = _read(store, 'window-chain.json')
    fields = {'format_version', 'initial_window_id', 'window_count', 'stream_id',
              'initial_intent_sha256'}
    if (not isinstance(contract, dict) or set(contract) != fields
            or contract['format_version'] != 'quadringent-window-chain-v1'
            or type(contract['window_count']) is not int
            or not 1 <= contract['window_count'] <= 128):
        raise ValueError('invalid durable window chain')
    identity = contract['initial_window_id']
    intent = _read_intent(store, identity)
    if (intent['format_version'] != 'quadringent-window-intent-v2'
            or intent['stream_id'] != contract['stream_id']
            or hashlib.sha256(_encode(intent)).hexdigest() != contract['initial_intent_sha256']):
        raise ValueError('window chain root differs')
    closed_ids = []
    def result(complete=False):
        return {'initial_window_id': contract['initial_window_id'],
                'window_count': contract['window_count'], 'stream_id': contract['stream_id'],
                'closed_window_ids': closed_ids,
                'pending_window_id': None if complete else identity,
                'capture_complete': complete}
    for index in range(contract['window_count']):
        if _timestamp(intent['started_at']) > observed:
            raise ValueError('window chain intent lies in the future')
        try:
            store.get_bounded(_prefix(identity) + 'closed.json', MAX_DOCUMENT_BYTES)
        except FileNotFoundError:
            return result()
        closed = read_closed_window(store, window_id=identity)
        _position(closed['end'])
        if (closed['format_version'] != 'quadringent-closed-window-v2'
                or _timestamp(closed['sealed_at']) > observed
                or _encode(closed['intent']) != _encode(intent)):
            raise ValueError('invalid chain closure')
        closed_ids.append(identity)
        link_key = _prefix(identity) + 'successor.json'
        if index + 1 == contract['window_count']:
            try:
                store.get_bounded(link_key, MAX_DOCUMENT_BYTES)
            except FileNotFoundError:
                return result(complete=True)
            raise ValueError('successor exceeds durable window budget')
        digest = hashlib.sha256(_encode(closed)).hexdigest()
        successor_id = 'w-' + digest
        expected_intent = {'format_version': 'quadringent-window-intent-v2',
                           'window_id': successor_id, 'previous': closed['end'],
                           'started_at': closed['closed_at'], 'stream_id': intent['stream_id'],
                           'duration_seconds': intent['duration_seconds'],
                           'closure_grace_seconds': intent['closure_grace_seconds']}
        expected_link = {'format_version': 'quadringent-window-successor-v1',
                         'predecessor_id': identity, 'predecessor_sha256': digest,
                         'successor_intent': expected_intent}
        identity = successor_id
        try:
            _, link = _read(store, link_key)
        except FileNotFoundError:
            return result()
        if _encode(link) != _encode(expected_link):
            raise ValueError('window chain successor link differs')
        try:
            intent = _read_intent(store, identity)
        except FileNotFoundError:
            return result()
        if _encode(intent) != _encode(expected_intent):
            raise ValueError('window chain successor intent differs')
    raise AssertionError('bounded chain traversal must terminate')


def prepare_successor_window(store, checkpoint, *, predecessor_id, now):
    """Prepare one deterministic successor under exclusive writer ownership.

    The link is durable before the intent and before any successor scan.
    This does not certify checkpoint continuity after an existing intent.
    """
    observed = _timestamp(now.isoformat())
    current = _checkpoint(checkpoint.load())
    predecessor = read_closed_window(store, window_id=predecessor_id)
    _position(predecessor['end'])
    if (predecessor['format_version'] != 'quadringent-closed-window-v2'
            or predecessor['intent']['format_version'] != 'quadringent-window-intent-v2'):
        raise ValueError('successor requires a timed stream-bound predecessor')
    if _timestamp(predecessor['sealed_at']) > observed:
        raise ValueError('predecessor lies in the future')
    digest = hashlib.sha256(_encode(predecessor)).hexdigest()
    successor_id = 'w-' + digest
    previous_intent = predecessor['intent']
    intent = {'format_version': 'quadringent-window-intent-v2', 'window_id': successor_id,
              'previous': predecessor['end'], 'started_at': predecessor['closed_at'],
              'stream_id': previous_intent['stream_id'],
              'duration_seconds': previous_intent['duration_seconds'],
              'closure_grace_seconds': previous_intent['closure_grace_seconds']}
    link = {'format_version': 'quadringent-window-successor-v1', 'predecessor_id': predecessor_id,
            'predecessor_sha256': digest, 'successor_intent': intent}
    link_key = _prefix(predecessor_id) + 'successor.json'
    try:
        _, existing_link = _read(store, link_key)
    except FileNotFoundError:
        link_exists = False
    else:
        link_exists = True
        if _encode(existing_link) != _encode(link):
            raise ValueError('successor link differs')
    try:
        existing_intent = _read_intent(store, successor_id)
    except FileNotFoundError:
        existing_intent = None
    if existing_intent is not None:
        if not link_exists or _encode(existing_intent) != _encode(intent):
            raise ValueError('successor intent lacks its exact preceding link')
        try:
            store.get_bounded(_prefix(successor_id) + 'closed.json', MAX_DOCUMENT_BYTES)
        except FileNotFoundError:
            pass
        else:
            closed = read_closed_window(store, window_id=successor_id)
            if closed['format_version'] != 'quadringent-closed-window-v2' or _timestamp(closed['sealed_at']) > observed:
                raise ValueError('invalid existing successor closure')
            return intent
    deadline = _timestamp(intent['started_at']) + timedelta(
        seconds=intent['duration_seconds'] + intent['closure_grace_seconds'])
    if observed > deadline:
        raise ValueError('successor window expired; refusing to redate it')
    if existing_intent is None:
        if current != predecessor['end']:
            raise ValueError('checkpoint advanced before successor intent')
        store.put_once(link_key, _encode(link))
        # Use the validated predecessor, not another mutable checkpoint read.
        store.put_once(_prefix(successor_id) + 'intent.json', _encode(intent))
    return intent


def _read_intent(store, window_id):
    _, intent = _read(store, _prefix(window_id)+'intent.json')
    if not isinstance(intent, dict):
        raise ValueError('invalid window intent')
    fields = {'format_version','window_id','previous','started_at','duration_seconds','closure_grace_seconds'}
    if intent.get('format_version') == 'quadringent-window-intent-v2':
        fields.add('stream_id')
        _stream_id(intent.get('stream_id'))
    if set(intent) != fields:
        raise ValueError('invalid window intent')
    if intent['format_version'] not in ('quadringent-window-intent-v1','quadringent-window-intent-v2') or intent['window_id'] != window_id:
        raise ValueError('window intent identity differs')
    duration = intent['duration_seconds']
    if type(duration) is not int or not 600 <= duration <= 3600:
        raise ValueError('invalid declared window duration')
    grace = intent['closure_grace_seconds']
    if type(grace) is not int or not 0 <= grace <= 120:
        raise ValueError('invalid closure grace')
    _timestamp(intent['started_at'])
    _position(intent['previous'])
    return intent


def _build(store, *, window_id, receipt_keys, closed_at, expected_hashes=None, sealed_at=None):
    intent = _read_intent(store, window_id)
    duration, grace = intent['duration_seconds'], intent['closure_grace_seconds']
    elapsed = (_timestamp(closed_at)-_timestamp(intent['started_at'])).total_seconds()
    if not duration <= elapsed <= duration + grace:
        raise ValueError('window closed outside declared duration and grace')
    if not isinstance(receipt_keys, (tuple,list)) or not 1 <= len(receipt_keys) <= MAX_RECEIPTS:
        raise ValueError('window receipt budget invalid')
    cursor = _position(intent['previous'])
    visited = {cursor['receiver']}
    refs, seen, count = [], set(), 0
    scan_times = []
    for key in receipt_keys:
        if not isinstance(key,str) or re.fullmatch(r'receipts/scan-[a-f0-9]{64}\.json',key) is None or key in seen:
            raise ValueError('duplicate or foreign receipt')
        seen.add(key)
        payload, receipt = _read(store,key,16384)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_hashes is not None and expected_hashes.get(key) != digest:
            raise ValueError('receipt changed after closure')
        if not isinstance(receipt,dict):
            raise ValueError('invalid scan receipt')
        fields = {'format_version','previous','start','end','event_count','raw'}
        if receipt.get('format_version') == 'quadringent-scan-receipt-v2':
            fields.add('scan_completed_at')
            _timestamp(receipt.get('scan_completed_at'))
        if sealed_at is not None:
            scan_times.append(_timestamp(receipt.get('scan_completed_at')))
        if set(receipt) != fields:
            raise ValueError('invalid scan receipt')
        if receipt['format_version'] not in ('quadringent-scan-receipt-v1','quadringent-scan-receipt-v2') or _position(receipt['previous']) != cursor:
            raise ValueError('scan receipt chain is discontinuous')
        start,end = _position(receipt['start']),_position(receipt['end'])
        identity = json.dumps(start,sort_keys=True,separators=(',',':')).encode()
        if key != 'receipts/scan-'+hashlib.sha256(identity).hexdigest()+'.json':
            raise ValueError('receipt key differs from scanned start')
        if start['receiver'] != end['receiver'] or start['sequence'] > end['sequence']:
            raise ValueError('invalid scanned interval')
        if start['receiver'] == cursor['receiver']:
            if start['sequence'] != cursor['sequence']+1:
                raise ValueError('scan interval skips or overlaps predecessor')
        elif start['receiver'] in visited:
            raise ValueError('receiver transition loops back')
        visited.add(start['receiver'])
        n = receipt['event_count']
        raw = receipt['raw']
        if type(n) is not int or n < 0 or (raw is None and n != 0):
            raise ValueError('invalid receipt event count')
        if raw is not None:
            if not isinstance(raw,dict) or set(raw) != {'payload_key','manifest_key','payload_sha256','manifest_sha256'}:
                raise ValueError('invalid raw references')
            match = re.fullmatch(r'batch-([a-f0-9]{32})\.jsonl',str(raw['payload_key']))
            if match is None or raw['manifest_key'] != 'batch-'+match[1]+'.manifest.json':
                raise ValueError('invalid raw object keys')
            if any(re.fullmatch(r'[a-f0-9]{64}',str(raw[k])) is None for k in ('payload_sha256','manifest_sha256')):
                raise ValueError('invalid raw digest')
        refs.append({'key':key,'sha256':digest})
        count += n
        cursor = end
    result = {'format_version':'quadringent-closed-window-v1','window_id':window_id,
            'intent':intent,'closed_at':closed_at,'end':cursor,'receipts':refs,'event_count':count,
            'delivery_latency_state':'unobserved'}
    if sealed_at is not None:
        start_time = _timestamp(intent['started_at'])
        deadline = start_time + timedelta(seconds=duration)
        closed_time, sealed_time = _timestamp(closed_at), _timestamp(sealed_at)
        if not closed_time <= sealed_time <= deadline + timedelta(seconds=grace):
            raise ValueError('qualified closure decision outside time budget')
        if scan_times[-1] != closed_time or any(t < start_time or t > closed_time for t in scan_times):
            raise ValueError('scan time outside qualified window')
        if scan_times != sorted(scan_times) or any(t >= deadline for t in scan_times[:-1]):
            raise ValueError('scan order or first eligible boundary violated')
        result.update(format_version='quadringent-closed-window-v2', sealed_at=sealed_at)
    return result


def seal_window(store, checkpoint, *, window_id, receipt_keys, closed_at, expected_receipt_hashes=None, sealed_at=None):
    closed = _build(store,window_id=window_id,receipt_keys=receipt_keys,closed_at=closed_at.isoformat(),expected_hashes=expected_receipt_hashes,
                    sealed_at=None if sealed_at is None else sealed_at.isoformat())
    if _checkpoint(checkpoint.load()) != closed['end']:
        raise ValueError('closed range differs from committed checkpoint')
    payload = _encode(closed)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ValueError('closed window exceeds metadata budget')
    store.put_once(_prefix(window_id)+'closed.json',payload)
    return closed


def read_closed_window(store, *, window_id):
    """Recover immutable receipt metadata, not a raw/Snowflake reconciliation."""
    _, closed = _read(store,_prefix(window_id)+'closed.json')
    if not isinstance(closed,dict) or not isinstance(closed.get('receipts'),list):
        raise ValueError('invalid closed window')
    refs=closed['receipts']
    if not 1 <= len(refs) <= MAX_RECEIPTS or any(not isinstance(r,dict) or set(r) != {'key','sha256'} for r in refs):
        raise ValueError('invalid closed receipt references')
    rebuilt=_build(store,window_id=window_id,receipt_keys=[r['key'] for r in refs],
                   closed_at=closed.get('closed_at'),expected_hashes={r['key']:r['sha256'] for r in refs},
                   sealed_at=closed.get('sealed_at'))
    if rebuilt != closed:
        raise ValueError('closed window metadata differs from receipts')
    return closed


def recover_and_seal_window(store, checkpoint, *, window_id, closed_at, sealed_at=None):
    """Recover under the single-writer fence, before allowing another scan.

    A published but uncommitted tail receipt is not part of the closed range.
    An existing closure wins even if the current checkpoint has advanced.
    This does not certify when a pre-crash poll actually completed.
    """
    try:
        store.get_bounded(_prefix(window_id)+'closed.json', MAX_DOCUMENT_BYTES)
    except FileNotFoundError:
        pass
    else:
        return read_closed_window(store, window_id=window_id)
    intent = _read_intent(store, window_id)
    beginning = _position(intent['previous'])
    cursor = _checkpoint(checkpoint.load())
    keys = []
    hashes = {}
    visited = set()
    while cursor != beginning:
        position = (cursor['receiver'], cursor['sequence'])
        if position in visited:
            raise ValueError('recovery receipt chain loops')
        visited.add(position)
        if len(keys) >= MAX_RECEIPTS:
            raise ValueError('window recovery receipt budget exceeded')
        try:
            _, index = _read(store, receipt_index_key(JournalPosition(*position)), 4096)
        except FileNotFoundError as error:
            raise ValueError('committed receipt index missing') from error
        if not isinstance(index, dict) or set(index) != {'format_version','receipt_key','receipt_sha256'} or index['format_version'] != 'quadringent-scan-index-v1':
            raise ValueError('invalid scan index')
        key = index['receipt_key']
        if not isinstance(key,str) or re.fullmatch(r'receipts/scan-[a-f0-9]{64}\.json',key) is None:
            raise ValueError('foreign indexed receipt')
        payload, receipt = _read(store,key,16384)
        if hashlib.sha256(payload).hexdigest() != index['receipt_sha256']:
            raise ValueError('indexed receipt digest differs')
        if _position(receipt['end']) != cursor:
            raise ValueError('indexed receipt ends at different position')
        keys.append(key)
        hashes[key] = index['receipt_sha256']
        cursor = _position(receipt['previous'])
    # Full schema, hashes, chain, duration and final CAS position are checked here.
    return seal_window(store, checkpoint, window_id=window_id,
                       receipt_keys=list(reversed(keys)), closed_at=closed_at,
                       expected_receipt_hashes=hashes, sealed_at=sealed_at)


def close_eligible_window(store, checkpoint, *, window_id, now):
    """Called under the worker fence before and after each scan; never starts I/O source."""
    try:
        store.get_bounded(_prefix(window_id)+'closed.json', MAX_DOCUMENT_BYTES)
    except FileNotFoundError:
        pass
    else:
        closed = read_closed_window(store, window_id=window_id)
        if closed['format_version'] != 'quadringent-closed-window-v2':
            raise ValueError('metadata-only closure is not a timed window')
        if _timestamp(closed['sealed_at']) > _timestamp(now.isoformat()):
            raise ValueError('closed window lies in the future')
        return closed
    intent = _read_intent(store, window_id)
    start = _timestamp(intent['started_at'])
    duration, grace = intent['duration_seconds'], intent['closure_grace_seconds']
    if type(duration) is not int or not 600 <= duration <= 3600 or type(grace) is not int or not 0 <= grace <= 120:
        raise ValueError('invalid declared time budget')
    now = _timestamp(now.isoformat())
    deadline = start + timedelta(seconds=duration)
    if not start <= now <= deadline + timedelta(seconds=grace):
        raise ValueError('window observation outside time budget')
    current = _checkpoint(checkpoint.load())
    if current == _position(intent['previous']):
        return None
    _, index = _read(store, receipt_index_key(JournalPosition(current['receiver'],current['sequence'])),4096)
    if not isinstance(index,dict) or set(index) != {'format_version','receipt_key','receipt_sha256'} or index['format_version'] != 'quadringent-scan-index-v1':
        raise ValueError('invalid scan index')
    key = index.get('receipt_key')
    if not isinstance(key,str) or re.fullmatch(r'receipts/scan-[a-f0-9]{64}\.json',key) is None:
        raise ValueError('invalid indexed receipt')
    payload, receipt = _read(store,key,16384)
    if hashlib.sha256(payload).hexdigest() != index.get('receipt_sha256') or _position(receipt['end']) != current:
        raise ValueError('indexed receipt changed')
    scan_time = _timestamp(receipt.get('scan_completed_at'))
    if not start <= scan_time <= now:
        raise ValueError('scan completion outside observed interval')
    if scan_time < deadline:
        return None
    return recover_and_seal_window(store,checkpoint,window_id=window_id,
                                   closed_at=scan_time,sealed_at=now)
