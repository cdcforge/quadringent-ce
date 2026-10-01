#!/usr/bin/env python3
"""Maintient la table metier d'une table : un cycle de rejeu incremental.

Une execution fait exactement ceci, dans cet ordre :

1. lire le filigrane courant (aucun si la table n'a jamais ete rejouee) ;
2. lire l'horodatage du dernier evenement ingere ;
3. si rien de neuf, s'arreter sans rien ecrire ;
4. sinon, controler le contrat de cle, puis rejouer seulement les cles touchees ;
5. **apres** un rejeu reussi, avancer le filigrane.

L'ordre porte la garantie : une panne entre le rejeu et l'avancement fait
simplement rejouer la meme fenetre, ce qui est sans effet puisque le rejeu est
idempotent. L'ordre inverse perdrait des evenements sans le signaler.

Le mode `--dry-run` (defaut) lit et rapporte sans rien ecrire.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.site_config import current as current_site
from quadringent.snowflake_business_incremental import (
    IncrementalReplayPlan,
)
from quadringent_control_plane.fleet_plan import (
    FleetError,
    parse_fleet_catalog,
    table_merge_key,
)

QUERY_TAG = "QUADRINGENT_BUSINESS_REPLAY"


def _keys_for(catalogue_path: str, table: str) -> tuple[str, ...]:
    """Cle declaree par le plan de flotte, jamais devinee."""

    try:
        catalogue = parse_fleet_catalog(json.loads(Path(catalogue_path).read_text()))
        return table_merge_key(catalogue, table)
    except FleetError as error:
        raise SystemExit(f"{table}: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument(
        "--catalogue",
        default=os.environ.get("QUADRINGENT_FLEET_CATALOGUE", ""),
        help="catalogue de flotte JSON (obligatoire, ex. via QUADRINGENT_FLEET_CATALOGUE)",
    )
    parser.add_argument("--connection-name", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    site = current_site()
    connection_name = args.connection_name.strip() or (site.snowflake_connection or "")
    if not connection_name:
        print("ERROR: --connection-name or QUADRINGENT_SNOWFLAKE_CONNECTION is required", file=sys.stderr)
        return 2
    if not args.catalogue.strip():
        print("ERROR: --catalogue or QUADRINGENT_FLEET_CATALOGUE is required", file=sys.stderr)
        return 2
    table = args.table.strip().upper()

    plan = IncrementalReplayPlan.for_table(
        table=table,
        key_columns=_keys_for(args.catalogue, table),
        scope=site.snowflake_scope,
        source_library=site.source_schema,
        destination_prefix=site.destination_prefix,
    )

    try:
        import snowflake.connector
    except ImportError:
        print("ERROR: snowflake-connector-python is required", file=sys.stderr)
        return 1

    def one(sql: str):
        connection = snowflake.connector.connect(
            connection_name=connection_name,
            login_timeout=20,
            network_timeout=600,
            session_parameters={"QUERY_TAG": QUERY_TAG},
        )
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            return columns, cursor.fetchall() if cursor.description else []
        finally:
            connection.close()

    def scalar(sql: str):
        _, rows = one(sql)
        return rows[0][0] if rows else None

    def run_suite(statements):
        connection = snowflake.connector.connect(
            connection_name=connection_name,
            login_timeout=20,
            network_timeout=1800,
            session_parameters={"QUERY_TAG": QUERY_TAG},
        )
        try:
            cursor = connection.cursor()
            for statement in statements:
                if statement.strip().upper().startswith("SELECT"):
                    cursor.execute(statement)
                    rows = cursor.fetchall()
                    if rows and rows[0][0] not in (0, None):
                        return "refused", int(rows[0][0])
                    continue
                cursor.execute(statement)
            return "applied", None
        finally:
            connection.close()

    document: dict[str, object] = {"table": table, "key_columns": list(plan.business.key_columns)}

    # Les objets existent-ils ? Sans table metier ni etat, la premiere execution
    # les cree ; on ne les cree pas en dry-run.
    try:
        watermark = scalar(plan.read_watermark_statement())
    except Exception:
        watermark = None
    try:
        latest = scalar(plan.latest_ingested_statement())
    except Exception as error:
        print(json.dumps({**document, "status": "NO_CANONICAL_VIEW",
                          "error_type": type(error).__name__}, sort_keys=True))
        return 1

    document["watermark"] = watermark
    document["latest_ingested"] = latest
    steps = plan.steps(watermark, latest)
    document["steps_planned"] = len(steps)

    if latest is None:
        document["status"] = "NOTHING_TO_REPLAY"
    elif watermark is not None and latest <= watermark:
        document["status"] = "UP_TO_DATE"
    else:
        document["status"] = "REPLAY_PLANNED" if not args.execute else "REPLAYED"
        if args.execute:
            outcome, invalid = run_suite([plan.create_state_statement()] + list(steps))
            if outcome == "refused":
                document["status"] = "REFUSED_INVALID_KEY_CONTRACT"
                document["invalid_event_count"] = invalid
            else:
                rows = scalar(plan.count_target_statement())
                document["target_rows"] = rows
                one(plan.advance_watermark_statement(latest, int(rows)))
                document["watermark_advanced_to"] = latest

    payload = json.dumps(document, sort_keys=True)
    if args.report:
        Path(args.report).write_text(payload + "\n")
    print(payload)
    return 0 if document["status"] != "REFUSED_INVALID_KEY_CONTRACT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
