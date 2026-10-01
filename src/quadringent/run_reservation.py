"""Fail-closed reservation of an isolated site capture prefix before source I/O."""
import json
import re
from typing import Any

from .site_config import SiteConfig


RUN_RESERVATION_FORMAT = 'quadringent-run-reservation-v1'
# Marqueurs écrits avant le renommage produit : acceptés en lecture seule —
# une réservation suspendue puis reprise de part et d'autre du renommage reste
# la même autorité. Toute nouvelle écriture porte RUN_RESERVATION_FORMAT.
LEGACY_RUN_RESERVATION_FORMATS = frozenset({'cdcforge-run-reservation-v1'})


def is_reservation_marker(marker: object, *, run_id: str) -> bool:
    """Vrai si ``marker`` est la réservation exacte de ``run_id``."""

    if not isinstance(marker, dict):
        return False
    return (
        marker.get('format_version') in
        ({RUN_RESERVATION_FORMAT} | LEGACY_RUN_RESERVATION_FORMATS)
        and marker.get('run_id') == run_id
    )


def reserve_run(
    client: Any,
    *,
    bucket: str,
    prefix: str,
    run_id: str,
    site: SiteConfig,
) -> None:
    if not isinstance(site, SiteConfig):
        raise ValueError("the declared site configuration is required")
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?', run_id):
        raise ValueError('Invalid run ID')
    allowed = {
        f'{site.raw_prefix_root}/{table.lower()}/runs/{run_id}'
        for table in site.reservable_tables
    }
    # Le lanceur de flotte réserve un espace neutre `<racine>/fleet/runs/<run>` :
    # les tables vivent sous `<racine>/<table>/journal` et la réservation
    # n'emprunte le préfixe d'aucune table.
    allowed.add(f'{site.raw_prefix_root}/fleet/runs/{run_id}')
    if bucket != site.raw_bucket or prefix not in allowed:
        raise ValueError('Run reservation requires an isolated declared prefix')
    listing = client.list_objects_v2(Bucket=bucket, Prefix=prefix + '/', MaxKeys=1)
    if listing.get('KeyCount') != 0 or listing.get('Contents') or listing.get('IsTruncated'):
        # Un Job suspendu puis repris relance un pod avec le même run_id : sa
        # propre réservation doit le laisser passer, jamais un préfixe étranger.
        marker_key = prefix + '/reservation.json'
        found = client.list_objects_v2(Bucket=bucket, Prefix=marker_key, MaxKeys=1)
        keys = [item.get('Key') for item in found.get('Contents') or []]
        if keys == [marker_key]:
            marker = json.loads(
                client.get_object(Bucket=bucket, Key=marker_key)['Body'].read()
            )
            if is_reservation_marker(marker, run_id=run_id):
                return
        raise ValueError('Run prefix is not empty')
    # Never overwrite or delete this marker. A failed/uncertain create requires
    # a fresh run ID, not an automatic retry that could reuse another capture.
    client.put_object(
        Bucket=bucket, Key=prefix + '/reservation.json', IfNoneMatch='*',
        ContentType='application/json',
        Body=json.dumps({'format_version': RUN_RESERVATION_FORMAT,
                         'run_id': run_id}, sort_keys=True).encode(),
    )
