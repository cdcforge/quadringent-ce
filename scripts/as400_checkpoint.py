#!/usr/bin/env python3
"""Console opérateur du curseur durable de capture (checkpoint journal).

Le checkpoint CAS (``quadringent.checkpoint.DynamoDbCheckpointStore``) porte la
position de reprise d'un flux : receiver + séquence. Quand la rétention du
journal purge le receiver porteur — événement IBM i classique après maintenance
ou rotations — la reprise est impossible et le lecteur refuse en fail-closed.
Ce script est le seul point de ré-ancrage explicite :

* ``status`` — affiche le checkpoint durable du flux ;
* ``anchor --receiver R --sequence N --actor <nom> --reason "<motif>"`` —
  ré-ancre le curseur sur une position vivante après décision opérateur
  (par exemple le curseur certifié d'une voie déjà livrée). La transition est
  CAS : elle exige le prédécesseur exact et consigne acteur, instant et motif.

Un ré-ancrage déclare que tout événement antérieur à la position cible est déjà
livré ou volontairement écarté : il ne doit jamais masquer un trou de capture.
Aucun secret n'est lu ni affiché.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.checkpoint import DynamoDbCheckpointStore  # noqa: E402
from quadringent.contract import JournalPosition  # noqa: E402
from quadringent.site_config import SiteConfigurationError, current as current_site  # noqa: E402

_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")
_RECEIVER = re.compile(r"[A-Za-z0-9$#@][A-Za-z0-9$#@._-]{0,63}")


def _store(args) -> DynamoDbCheckpointStore:
    site = current_site()
    table = (args.table or site.checkpoint_table).strip()
    stream = (args.stream or os.environ.get("AS400_STREAM_KEY", "")).strip()
    if not stream:
        raise SiteConfigurationError("AS400_STREAM_KEY")
    return DynamoDbCheckpointStore(
        table,
        stream,
        client=_dynamodb_client(site.aws_region),
    )


def _dynamodb_client(region: str):
    import boto3

    return boto3.client("dynamodb", region_name=region)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _position_payload(position: JournalPosition | None) -> dict[str, object]:
    if position is None:
        return {"present": False}
    return {
        "present": True,
        "receiver": position.receiver,
        "sequence": position.sequence,
    }


def _anchor(store: DynamoDbCheckpointStore, args) -> dict[str, object]:
    previous = store.load()
    target = JournalPosition(receiver=args.receiver.strip(), sequence=args.sequence)
    if previous is None:
        # Pas de prédécesseur : le CAS exige que l'item n'existe pas encore.
        store.compare_and_set(None, target)
    else:
        store.transition(previous, target)
    stamp = _now().isoformat()
    # La transition CAS a déjà posé la cible : la condition verrouille l'audit
    # sur exactement cette position, sinon l'écriture est refusée.
    store.client.update_item(
        TableName=store.table_name,
        Key={"stream_id": {"S": store.stream_key}},
        UpdateExpression=(
            "SET anchor_actor = :actor, anchor_at = :at, anchor_reason = :reason"
        ),
        ConditionExpression="#receiver = :receiver AND #sequence = :sequence",
        ExpressionAttributeNames={"#receiver": "receiver", "#sequence": "sequence"},
        ExpressionAttributeValues={
            ":actor": {"S": args.actor.strip()},
            ":at": {"S": stamp},
            ":reason": {"S": args.reason.strip()},
            ":receiver": {"S": target.receiver},
            ":sequence": {"N": str(target.sequence)},
        },
    )
    return {
        "action": "anchored",
        "actor": args.actor.strip(),
        "at": stamp,
        "reason": args.reason.strip(),
        "previous": _position_payload(previous),
        "position": _position_payload(target),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", default=None, help="défaut : stream key du site déclaré")
    parser.add_argument("--table", default=None, help="défaut : table de checkpoints du site")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("status", help="affiche le checkpoint durable du flux")
    anchor = command.add_parser("anchor", help="ré-ancre le curseur (CAS audité)")
    anchor.add_argument("--receiver", required=True, help="receiver cible vivant")
    anchor.add_argument("--sequence", type=int, required=True, help="séquence cible (>= 0)")
    anchor.add_argument("--actor", required=True, help="identité de l'opérateur, consignée")
    anchor.add_argument("--reason", required=True, help="motif du ré-ancrage, consigné")
    args = parser.parse_args()

    try:
        store = _store(args)
    except SiteConfigurationError:
        print(json.dumps({"event": "checkpoint_error", "error": "site_configuration"}, sort_keys=True))
        return 2

    if args.command == "status":
        print(json.dumps({"event": "checkpoint", "stream": store.stream_key,
                          "state": _position_payload(store.load())}, sort_keys=True))
        return 0

    # anchor
    actor = args.actor.strip()
    reason = args.reason.strip()
    if _ACTOR.fullmatch(actor) is None:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_actor"}, sort_keys=True))
        return 2
    if _RECEIVER.fullmatch(args.receiver.strip()) is None:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_receiver"}, sort_keys=True))
        return 2
    if args.sequence < 0:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_sequence"}, sort_keys=True))
        return 2
    if not reason or len(reason) > 200:
        print(json.dumps({"event": "checkpoint_error", "error": "invalid_reason"}, sort_keys=True))
        return 2
    try:
        result = _anchor(store, args)
    except ValueError as error:
        print(json.dumps({"event": "checkpoint_error", "error": str(error)}, sort_keys=True))
        return 2
    except RuntimeError:
        print(json.dumps({"event": "checkpoint_error", "error": "cas_conflict"}, sort_keys=True))
        return 2
    print(json.dumps({"event": "checkpoint", "stream": store.stream_key, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
