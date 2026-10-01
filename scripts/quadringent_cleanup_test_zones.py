#!/usr/bin/env python3
"""Retire les zones de test laissées dans S3 par une campagne de mesure.

Une campagne crée des préfixes `*-runs/` pour isoler ses essais. Ils n'ont pas
de consommateur : ni stage, ni tuyau, ni table Snowflake. Les laisser coûte du
stockage et brouille l'inventaire du client.

Le script est fail-closed :

- il refuse tout préfixe qui n'est pas explicitement dans sa liste ;
- il refuse de toucher un préfixe consommé par un stage Snowflake, sauf
  `--allow-consumed` pour la zone sonde, qui a un tuyau volontaire ;
- il ne supprime qu'après une confirmation exacte et un décompte préalable ;
- en mode `--dry-run` (défaut), il ne supprime rien et rend les volumes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

def _site():
    from quadringent.site_config import current  # noqa: PLC0415

    return current()


# Zones de campagne, nommées par la mesure qui les a créées.
TEST_ZONES = (
    "cutover-runs", "cutover2-runs", "cutover3-runs", "cutover4-runs",
    "cutover5-runs", "rotation-runs", "nodest-runs", "netcut-runs",
    "netcut2-runs", "alert-runs", "alert2-runs",
)
def _confirmation(site) -> str:
    return f"SUPPRIMER_ZONES_DE_TEST_{site.fleet_environment}"
SONDE = "latencyprobe"


def _client():
    import boto3  # noqa: PLC0415

    return boto3.client("s3", region_name=_site().aws_region)


def _inventory(client, prefix: str) -> tuple[int, int]:
    paginator = client.get_paginator("list_objects_v2")
    objects = 0
    total = 0
    for page in paginator.paginate(Bucket=_site().raw_bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            objects += 1
            total += int(item.get("Size") or 0)
    return objects, total


def _consumed_zones() -> set[str]:
    """Zones référencées par un stage Snowflake.

    Une zone consommée n'est jamais supprimée sans autorisation explicite :
    supprimer ses objets rendrait le stage vide et le tuyau silencieusement
    inutile. La lecture passe par le connecteur direct du poste, qui ne dépend
    pas de la session AWS.
    """

    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    programme = (
        "exec(open('/tmp/sf_query.py').read().split('if __name__')[0]);"
        f"import json;c,r=run('SHOW STAGES IN SCHEMA {_site().destination_namespace}');"
        "cols=[k[0] for k in c];"
        "print(json.dumps([dict(zip(cols,row)).get('url','') for row in r]))"
    )
    result = subprocess.run([sys.executable, "-c", programme],
                            capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError("la lecture des stages Snowflake a échoué")
    urls = json.loads(result.stdout.strip().splitlines()[-1])
    zones: set[str] = set()
    for url in urls:
        tail = str(url).removeprefix(f"s3://{_site().raw_bucket}/{_site().raw_prefix_root}/").strip("/")
        if tail:
            zones.add(tail.split("/")[0])
    return zones


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allow-consumed", action="store_true",
                        help="autoriser la zone sonde, qui a un tuyau volontaire")
    parser.add_argument("--report", default="")
    args = parser.parse_args()

    try:
        client = _client()
        client.list_objects_v2(Bucket=_site().raw_bucket, Prefix=f"{_site().raw_prefix_root}/", MaxKeys=1)
    except Exception as error:  # noqa: BLE001 - l'absence d'identifiants est un état attendu
        print(json.dumps({
            "status": "NO_CREDENTIALS",
            "message": "session AWS indisponible : lancer aws sso login avec le profil du site",
            "error_type": type(error).__name__,
        }, sort_keys=True))
        return 1
    consumed = _consumed_zones()

    zones = list(TEST_ZONES)
    if args.allow_consumed:
        zones.append(SONDE)

    report: list[dict[str, object]] = []
    for zone in zones:
        prefix = f"{_site().raw_prefix_root}/{zone}/"
        objects, total = _inventory(client, prefix)
        is_consumed = zone in consumed
        entry = {
            "zone": zone,
            "prefix": prefix,
            "objects": objects,
            "bytes": total,
            "consumed_by_snowflake": is_consumed,
        }
        if objects == 0:
            entry["action"] = "nothing_to_do"
            report.append(entry)
            continue
        if is_consumed and not (args.allow_consumed and zone == SONDE):
            entry["action"] = "refused_consumed"
            report.append(entry)
            continue
        if not args.execute:
            entry["action"] = "would_delete"
            report.append(entry)
            continue
        if args.confirm != _confirmation(_site()):
            entry["action"] = "refused_without_confirmation"
            report.append(entry)
            continue
        deleted = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_site().raw_bucket, Prefix=prefix):
            keys = [{"Key": str(item["Key"])} for item in page.get("Contents", [])]
            if keys:
                client.delete_objects(Bucket=_site().raw_bucket, Delete={"Objects": keys})
                deleted += len(keys)
        remaining, _ = _inventory(client, prefix)
        entry["action"] = "deleted" if remaining == 0 else "partial"
        entry["deleted_objects"] = deleted
        entry["remaining_objects"] = remaining
        report.append(entry)

    total_bytes = sum(int(item["bytes"]) for item in report)
    document = {
        "status": "EXECUTED" if args.execute else "DRY_RUN",
        "environment": _site().environment,
        "zones": len(report),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1073741824, 2),
        "confirmation": _confirmation(_site()),
        "report": report,
    }
    payload = json.dumps(document, sort_keys=True)
    if args.report:
        Path(args.report).write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
