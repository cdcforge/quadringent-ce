"""Reconcile a timed raw window without manufacturing a capture snapshot."""
from datetime import datetime
import re

from .closed_window_raw import collect_closed_window_raw
from .object_store import FileObjectStore, S3ObjectStore
from .site_config import SiteConfig
from .snowflake_autonomous import autonomous_plan, reconcile_autonomous_files
from .verification_window import run_archive_prefix


def verify_closed_window_destination(cursor, store, *, run_id, window_id, observed_at, site):
    if not isinstance(site, SiteConfig):
        raise ValueError('the declared site configuration is required')
    if not isinstance(run_id,str) or re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',run_id) is None:
        raise ValueError('invalid archive run identity')
    prefix=run_archive_prefix(site, run_id).rstrip('/')
    if isinstance(store,S3ObjectStore):
        if store.bucket != site.raw_bucket or store.prefix != prefix:
            raise ValueError('window store escaped the exact declared run')
        backend='s3'
    elif isinstance(store,FileObjectStore):
        backend='local'
    else:
        raise ValueError('unsupported window storage provenance')
    if not isinstance(observed_at,datetime) or observed_at.utcoffset() is None:
        raise ValueError('destination observation requires timezone')
    acquired=collect_closed_window_raw(store,window_id=window_id,site=site)
    window=acquired.window
    if window['format_version'] != 'quadringent-closed-window-v2':
        raise ValueError('destination window requires timed closure')
    if datetime.fromisoformat(window['sealed_at']) > observed_at:
        raise ValueError('window closure is in the future')
    destination={'state':'not_tested','reason':'no_events','event_count':0,
                 'observed_at':observed_at.isoformat()}
    if acquired.event_count:
        metrics=reconcile_autonomous_files(cursor,autonomous_plan(site),
            object_keys=tuple(prefix+'/'+key for key in acquired.object_keys),
            expected_rows=acquired.event_count,
            site=site,
            expected_event_ids_sha256=acquired.event_ids_sha256)
        destination={'state':'matched','event_count':acquired.event_count,
                     'event_ids_sha256':acquired.event_ids_sha256,
                     'observed_at':observed_at.isoformat(),'metrics':metrics}
    return {'format_version':'quadringent-window-destination-v1',
            'archive_run_id':run_id,'window_id':window_id,'window':window,
            'storage_backend':backend,'process_state':'not_observed',
            'destination':destination}
