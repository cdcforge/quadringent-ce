#!/usr/bin/env python3
"""Vérifie la continuité d'un run de flotte, à partir de ses propres artefacts.

Trois propriétés sont contrôlées, séparément parce qu'elles ne se remplacent
pas :

- **chaîne des reçus** : chaque reçu porte `previous`, et son `start` doit
  suivre exactement l'`end` du précédent, même receiver. Un trou est un
  intervalle manquant ;
- **contiguïté des séquences** : la séquence de départ d'une fenêtre vaut la
  séquence de fin de la précédente plus un ;
- **unicité des événements** : aucun identifiant d'événement ne doit apparaître
  dans deux fenêtres. Un doublon serait une double application au niveau brut.

Le vérificateur ne lit aucune donnée métier : il ne rend que des comptes, des
bornes de séquence et la liste des ruptures constatées.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _read_documents(paths: list[Path]) -> list[dict]:
    documents: list[dict] = []
    for path in paths:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                documents.append(json.loads(line))
    return documents


def check(receipts: list[dict], events: list[dict]) -> dict[str, object]:
    ordered = sorted(receipts, key=lambda item: _start_sequence(item))
    gaps: list[dict[str, object]] = []
    for previous, current in zip(ordered, ordered[1:]):
        expected = _end_sequence(previous) + 1
        actual = _start_sequence(current)
        if actual != expected or _receiver(previous) != _receiver(current):
            gaps.append(
                {
                    "after_receiver": _receiver(previous),
                    "after_sequence": _end_sequence(previous),
                    "next_receiver": _receiver(current),
                    "next_sequence": actual,
                    "expected_sequence": expected,
                }
            )
    unlinked = [
        index
        for index, receipt in enumerate(ordered)
        if index > 0 and not receipt.get("previous")
    ]
    identifiers = [str(event.get("event_id", "")) for event in events]
    present = [item for item in identifiers if item]
    duplicates = len(present) - len(set(present))
    counts_match = (
        sum(int(receipt.get("event_count") or 0) for receipt in ordered)
        == len(present)
    )
    return {
        "receipts": len(ordered),
        "chained_from": _start_sequence(ordered[0]) if ordered else None,
        "chained_to": _end_sequence(ordered[-1]) if ordered else None,
        "continuity_gaps": len(gaps),
        "gap_samples": gaps[:5],
        "receipts_without_previous": len(unlinked),
        "events": len(present),
        "unique_events": len(set(present)),
        "duplicate_events": duplicates,
        "receipt_counts_match_events": counts_match,
        "verdict": (
            "CONTINUOUS"
            if ordered and not gaps and duplicates == 0 and counts_match
            else "BROKEN_OR_INCOMPLETE"
        ),
    }


def _start_sequence(receipt: dict) -> int:
    return int((receipt.get("start") or {}).get("sequence") or 0)


def _end_sequence(receipt: dict) -> int:
    return int((receipt.get("end") or {}).get("sequence") or 0)


def _receiver(receipt: dict) -> str:
    return str((receipt.get("end") or {}).get("receiver") or "")


def _s3_objects(prefix: str, suffix: str) -> tuple[list[dict], list[bytes]]:
    import boto3  # noqa: PLC0415

    from quadringent.site_config import current as current_site  # noqa: PLC0415

    client = boto3.client("s3", region_name=current_site().aws_region)
    bucket, _, key = prefix.removeprefix("s3://").partition("/")
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=key):
        for item in page.get("Contents", []):
            if str(item["Key"]).endswith(suffix):
                keys.append(str(item["Key"]))
    documents: list[dict] = []
    payloads: list[bytes] = []
    for item in keys:
        body = client.get_object(Bucket=bucket, Key=item)["Body"].read()
        if suffix == ".json":
            documents.append(json.loads(body))
        else:
            payloads.append(body)
    return documents, payloads


def _events_from_payloads(payloads: list[bytes]) -> list[dict]:
    events: list[dict] = []
    for payload in payloads:
        for line in payload.decode("utf-8", "strict").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-prefix", required=True,
                        help="préfixe S3 du run, ex : s3://bucket/<racine>/<zone>/fleet/runs/<run>")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    prefix = args.run_prefix.rstrip("/")
    try:
        receipts, _ = _s3_objects(f"{prefix}/receipts/", ".json")
        _, payloads = _s3_objects(f"{prefix}/", ".jsonl")
        events = _events_from_payloads(payloads)
    except Exception as error:  # noqa: BLE001 - diagnostic borné
        print(json.dumps({"status": "ERROR", "error_type": type(error).__name__},
                         sort_keys=True))
        return 1
    document = {"run_prefix": prefix, "status": "READ", **check(receipts, events)}
    payload = json.dumps(document, sort_keys=True)
    if args.report:
        Path(args.report).write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
