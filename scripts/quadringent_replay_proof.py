#!/usr/bin/env python3
"""Rejoue une table et vérifie que chaque clé respecte la règle produit.

Sert à rejouer la preuve d'une table déclarée — image initiale plus
modifications — sans conserver une table de 4,4 Go. Le script crée la table
métier, exécute le plan (validation, MERGE), mesure la conformité de **chaque**
clé, puis supprime la table sauf si `--keep` est passé.

La conformité mesurée est : *l'événement de journal de plus grande séquence
gagne ; l'image initiale ne sert que si aucun événement de journal ne touche la
clé.*
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = sys.executable


def _sql(statement: str) -> list:
    programme = (
        "exec(open('/tmp/sf_query.py').read().split('if __name__')[0]);"
        "import json,sys;c,r=run(sys.argv[1]);print(json.dumps(r, default=str))"
    )
    out = subprocess.run([RUNNER, "-c", programme, statement],
                         capture_output=True, text=True, timeout=1800)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1][:200])
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument(
        "--catalogue",
        default=os.environ.get("QUADRINGENT_FLEET_CATALOGUE", ""),
        help="catalogue de flotte JSON (obligatoire, ex. via QUADRINGENT_FLEET_CATALOGUE)",
    )
    parser.add_argument("--keep", action="store_true",
                        help="conserver la table métier après mesure")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    table = args.table.strip().upper()

    sys.path.insert(0, str(ROOT / "src"))
    from quadringent.site_config import current as current_site
    from quadringent.snowflake_business import (
        SnowflakeBusinessMergePlan,
    )
    from quadringent_control_plane.fleet_plan import (
        FleetError,
        parse_fleet_catalog,
        table_merge_key,
    )

    site = current_site()
    if not args.catalogue.strip():
        print("ERROR: --catalogue or QUADRINGENT_FLEET_CATALOGUE is required", file=sys.stderr)
        return 2
    try:
        catalogue = parse_fleet_catalog(json.loads(Path(args.catalogue).read_text()))
        cles = table_merge_key(catalogue, table)
    except FleetError as error:
        print(json.dumps({"status": "REFUSED", "reason": str(error)}))
        return 2

    plan = SnowflakeBusinessMergePlan(
        scope=site.snowflake_scope,
        canonical_table=site.snowflake_canonical_for(table),
        target_table=f"QUADRINGENT_{table}_ROLLUP_PROOF",
        source_library=site.source_schema, source_table=table,
        key_columns=cles,
    )

    document: dict[str, object] = {"table": table, "key_columns": list(cles)}
    for index, statement in enumerate(plan.statements_for(), start=1):
        if statement.strip().upper().startswith("SELECT"):
            rows = _sql(statement)
            document["invalid_event_count"] = rows[0][0]
            if rows[0][0] != 0:
                print(json.dumps({**document, "status": "REFUSED_INVALID"}, sort_keys=True))
                return 1
            continue
        _sql(statement)
        document[f"statement_{index}"] = "ok"

    empreinte = (
        "SHA2(TO_JSON(ARRAY_CONSTRUCT("
        + ", ".join(f"PAYLOAD:after:{c}" for c in cles)
        + ")), 256)"
    )
    rows = _sql(f"""
WITH evenements AS (
  SELECT {empreinte} AS FP, EVENT_ID, JOURNAL_SEQUENCE,
         IFF(JOURNAL_RECEIVER LIKE 'SNAPSHOT:%', 1, 0) AS EST_SNAPSHOT
  FROM "{site.destination_database}"."{site.destination_schema}"."{site.snowflake_canonical_for(table)}"
  WHERE PAYLOAD:operation::string IN ('c','u','u_after')
), attendus AS (
  SELECT FP, EVENT_ID,
         ROW_NUMBER() OVER (PARTITION BY FP
                            ORDER BY EST_SNAPSHOT ASC, JOURNAL_SEQUENCE DESC, EVENT_ID DESC) AS RANG
  FROM evenements
)
SELECT COUNT(*), SUM(IFF(t.LAST_EVENT_ID = a.EVENT_ID, 1, 0))
FROM attendus a
JOIN "{site.destination_database}"."{site.destination_schema}"."QUADRINGENT_{table}_ROLLUP_PROOF" t
  ON t.BUSINESS_KEY_FINGERPRINT = a.FP
WHERE a.RANG = 1
""")
    document["keys_evaluated"] = rows[0][0]
    document["keys_conform"] = rows[0][1]
    document["divergent"] = rows[0][0] - (rows[0][1] or 0)
    document["status"] = "CONFORME" if document["divergent"] == 0 else "DIVERGENT"

    if not args.keep:
        _sql(f'DROP TABLE IF EXISTS "{site.destination_database}"."{site.destination_schema}"."QUADRINGENT_{table}_ROLLUP_PROOF"')
        document["table_dropped"] = True

    payload = json.dumps(document, sort_keys=True)
    if args.report:
        Path(args.report).write_text(payload + "\n")
    print(payload)
    return 0 if document["status"] == "CONFORME" else 1


if __name__ == "__main__":
    raise SystemExit(main())
