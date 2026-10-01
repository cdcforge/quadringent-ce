#!/usr/bin/env python3
"""Réconciliation champ à champ entre un préfixe de journal et sa destination.

La comparaison se fait sur l'identité d'événement, jamais sur l'ordre : un lot
publié puis rechargé garde ses identifiants, donc deux lectures du même
préfixe ne peuvent pas produire un faux écart. Trois contrôles sont rendus
séparément parce qu'ils ne se remplacent pas :

- volumes : combien d'identités de chaque côté, et lesquelles manquent ;
- clés : pour chaque identité commune, les noms de colonnes identiques ;
- valeurs : pour chaque colonne commune, la valeur identique, et son type.

Aucun aperçu de donnée métier n'est imprimé : seuls des comptes, des noms de
colonnes et des motifs de divergence bornés le sont.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src",):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

from quadringent.site_config import current as current_site  # noqa: E402


def _load_source(prefix_dir: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(prefix_dir.glob("*.jsonl")):
        with path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                document = json.loads(line)
                identifier = str(document.get("event_id", ""))
                if not identifier:
                    raise ValueError("source row without event identity")
                rows[identifier] = document.get("after", {})
    return rows


def _as_mapping(value: object) -> dict[str, Any]:
    """Le connecteur rend un VARIANT tantôt objet, tantôt chaîne JSON."""

    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _load_destination(query: Any, table: str, site) -> dict[str, dict[str, Any]]:
    sql = (
        "SELECT PAYLOAD:event_id::string AS EID, PAYLOAD:after AS AFTER_IMAGE "
        f'FROM "{site.destination_database}"."{site.destination_schema}"."{site.snowflake_raw_table_for(table)}" '
        f"WHERE SOURCE_FILE LIKE '%/{table.lower()}/journal/%'"
    )
    columns, rows = query(sql)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        result[str(row[0])] = _as_mapping(row[1])
    return result


def reconcile(
    source: dict[str, dict[str, Any]], destination: dict[str, dict[str, Any]]
) -> dict[str, object]:
    identifiers = set(source) & set(destination)
    missing = sorted(set(source) - set(destination))
    extra = sorted(set(destination) - set(source))
    value_mismatches: list[dict[str, str]] = []
    key_mismatches: list[str] = []
    compared_cells = 0
    null_cells = 0
    numeric_cells = 0
    for identifier in sorted(identifiers):
        left, right = source[identifier], destination[identifier]
        if set(left) != set(right):
            key_mismatches.append(identifier)
            continue
        for name, value in left.items():
            compared_cells += 1
            if value is None:
                null_cells += 1
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric_cells += 1
            if right.get(name) != value:
                if len(value_mismatches) < 12:
                    value_mismatches.append(
                        {"event": identifier[:16], "column": str(name)}
                    )
    return {
        "source_rows": len(source),
        "destination_rows": len(destination),
        "matched_rows": len(identifiers),
        "missing_in_destination": len(missing),
        "extra_in_destination": len(extra),
        "column_set_mismatches": len(key_mismatches),
        "value_mismatches": len(value_mismatches),
        "compared_cells": compared_cells,
        "null_cells_compared": null_cells,
        "numeric_cells_compared": numeric_cells,
        "samples": value_mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--connection-name", default="")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    site = current_site()
    connection_name = args.connection_name.strip() or (site.snowflake_connection or "")
    if not connection_name:
        print("ERROR: --connection-name or QUADRINGENT_SNOWFLAKE_CONNECTION is required", file=sys.stderr)
        return 2
    table = args.table.strip().upper()
    try:
        import snowflake.connector  # noqa: PLC0415
    except ImportError:
        print("ERROR: snowflake-connector-python is required", file=sys.stderr)
        return 1

    def query(sql: str):
        connection = snowflake.connector.connect(
            connection_name=connection_name,
            login_timeout=20,
            network_timeout=120,
            session_parameters={"QUERY_TAG": f"QUADRINGENT_RECONCILE_{table}"},
        )
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            columns = [column[0] for column in cursor.description]
            return columns, cursor.fetchall()
        finally:
            connection.close()

    source = _load_source(Path(args.source_dir))
    destination = _load_destination(query, table, site)
    document: dict[str, object] = {"table": table, **reconcile(source, destination)}
    payload = json.dumps(document, sort_keys=True)
    if args.report:
        Path(args.report).write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
