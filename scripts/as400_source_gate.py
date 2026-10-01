#!/usr/bin/env python3
"""Console opérateur de la garde de connexions source IBM i.

La garde (``quadringent.source_gate``) borne les sign-ons : au plus trois
tentatives, blocage durable sur refus d'authentification, pause bornée sur
indisponibilité ou maintenance. Ce script est le seul point de réarmement :

* ``status`` — affiche l'état durable de la garde ;
* ``pause --seconds N`` — déclare une fenêtre de maintenance : aucun sign-on
  avant l'échéance, puis une sonde unique ;
* ``reset --actor <nom>`` — réarme après intervention (profil réactivé,
  mot de passe renouvelé) ; l'acteur et l'instant sont consignés dans le
  record pour audit.

Aucun secret n'est lu ni affiché : l'identité de la garde se limite à
l'hôte et au compte source déclarés par le site.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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

from quadringent.site_config import SiteConfigurationError, current as current_site  # noqa: E402
from quadringent.source_gate import DynamoDbSourceGate  # noqa: E402

_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}")


def _gate(args) -> DynamoDbSourceGate:
    site = current_site()
    host = (args.host or site.ibmi_host).strip()
    user = (args.user or site.ibmi_user).strip()
    table = (args.table or site.checkpoint_table).strip()
    return DynamoDbSourceGate(
        table,
        DynamoDbSourceGate.key_for(host, user),
        client=_dynamodb_client(site.aws_region),
    )


def _dynamodb_client(region: str):
    import boto3

    return boto3.client("dynamodb", region_name=region)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="défaut : hôte IBM i du site déclaré")
    parser.add_argument("--user", default=None, help="défaut : compte IBM i du site déclaré")
    parser.add_argument("--table", default=None, help="défaut : table de checkpoints du site")
    command = parser.add_subparsers(dest="command", required=True)
    command.add_parser("status", help="affiche l'état durable de la garde")
    pause = command.add_parser("pause", help="déclare une pause (fenêtre de maintenance)")
    pause.add_argument("--seconds", type=int, required=True,
                       help="durée de la pause en secondes (60 à 86400)")
    reset = command.add_parser("reset", help="réarme la garde après intervention source")
    reset.add_argument("--actor", required=True,
                       help="identité de l'opérateur, consignée dans le record")
    args = parser.parse_args()

    try:
        gate = _gate(args)
    except SiteConfigurationError as error:
        print(json.dumps({"event": "source_gate_error", "error": "site_configuration"}, sort_keys=True))
        return 2

    if args.command == "status":
        print(json.dumps({"event": "source_gate", "state": gate.state()}, sort_keys=True))
        return 0

    if args.command == "pause":
        if not 60 <= args.seconds <= 86_400:
            print(json.dumps({"event": "source_gate_error", "error": "pause_seconds_range"}, sort_keys=True))
            return 2
        until = _now() + timedelta(seconds=args.seconds)
        record = gate.pause_until(until=until, actor="operator", now=_now())
        print(json.dumps({"event": "source_gate", "action": "paused", "state": record}, sort_keys=True))
        return 0

    # reset
    actor = args.actor.strip()
    if _ACTOR.fullmatch(actor) is None:
        print(json.dumps({"event": "source_gate_error", "error": "invalid_actor"}, sort_keys=True))
        return 2
    record = gate.reset(actor=actor, now=_now())
    print(json.dumps({"event": "source_gate", "action": "reset", "state": record}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
