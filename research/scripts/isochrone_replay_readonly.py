#!/usr/bin/env python3
"""Replay an isochrone comparison on already-captured data. Read-only.

Compares one canonical table against the declared Popsink reference table on
the same commit band, using the commit instant as the join key instead of the
journal sequence. Also reports the legacy sequence-key matrix so the two can
be contrasted, and profiles the sequence offset as a diagnostic.

No IBM i call, no Popsink API call, no write. Technical columns only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent_research.isochrone import compare_key_sets, technical_key  # noqa: E402
from quadringent_research.isochrone_temporal import (  # noqa: E402
    band_comparability,
    classify_matrix,
    commit_timestamp_to_epoch_ms,
    compare_event_multisets,
    key_from_popsink_record_temporal,
    key_from_rd_record_temporal,
)
from quadringent.site_config import current as current_site  # noqa: E402
from scripts.snowflake_readonly_query import connect, run_query  # noqa: E402

RD_SQL = """
SELECT t.PAYLOAD:event_id::string        AS event_id,
       t.JOURNAL_RECEIVER                AS receiver,
       t.JOURNAL_SEQUENCE                AS sequence,
       t.OPERATION                       AS operation,
       t.PAYLOAD:commit_timestamp::string AS commit_timestamp
  FROM {database}.{schema}.{table} t
"""

COVERAGE_SQL = """
SELECT MIN(__SOURCE_TS_MS), MAX(__SOURCE_TS_MS), COUNT(*)
  FROM {reference}
"""

POPSINK_SQL = """
SELECT __SOURCE_TS_MS AS ts_ms,
       __OP           AS op,
       __DELETED      AS deleted
  FROM {reference}
 WHERE __SOURCE_TS_MS BETWEEN %s AND %s
"""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _qualified(name: str) -> str:
    """Identifiant Snowflake pleinement qualifié ``DB.SCH.OBJ``, borné."""

    parts = name.split(".")
    if len(parts) != 3 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise SystemExit("ERROR: reference table must be a qualified DB.SCHEMA.TABLE identifier")
    return name


def _rd_records(connection, table: str, site) -> list[dict]:
    rows = run_query(
        connection,
        RD_SQL.format(
            database=site.destination_database,
            schema=site.destination_schema,
            table=table,
        ),
    )
    return [
        {
            "event_id": row[0],
            "journal_receiver": row[1],
            "journal_sequence": int(row[2]),
            "operation": row[3],
            "commit_timestamp": row[4],
        }
        for row in rows
    ]


def _band(records: list[dict], offset_hours: int) -> tuple[int, int]:
    stamps = [
        commit_timestamp_to_epoch_ms(r["commit_timestamp"], offset_hours=offset_hours)
        for r in records
    ]
    return min(stamps), max(stamps)


def _popsink_records(connection, reference: str, low: int, high: int) -> list[dict]:
    rows = run_query(connection, POPSINK_SQL.format(reference=reference), (low, high))
    return [{"ts_ms": int(row[0]), "op": row[1], "deleted": row[2]} for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rd-table", required=True,
                        help="canonical R&D table under the declared destination schema")
    parser.add_argument("--reference-table", required=True,
                        help="table de référence qualifiée DB.SCHEMA.TABLE (Popsink)")
    parser.add_argument("--role", required=True)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--offset-hours", type=int, default=None,
                        help="wall-clock offset of the IBM i commit timestamp; "
                             "probed over 0..2 when omitted")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if not args.rd_table.replace("_", "").isalnum():
        parser.error("--rd-table must be alphanumeric plus underscores")
    reference = _qualified(args.reference_table)
    site = current_site()

    connection = connect(
        role=args.role,
        warehouse=args.warehouse,
        query_tag=f"{site.destination_schema}_ISOCHRONE_REPLAY_{args.rd_table}",
    )
    try:
        coverage_row = run_query(connection, COVERAGE_SQL.format(reference=reference))[0]
        coverage_low = int(coverage_row[0]) if coverage_row[0] is not None else None
        coverage_high = int(coverage_row[1]) if coverage_row[1] is not None else None
        coverage_rows = int(coverage_row[2])
        rd = _rd_records(connection, args.rd_table, site)
        if not rd:
            print(json.dumps({"status": "EMPTY", "rd_table": args.rd_table}))
            return 1

        offsets = [args.offset_hours] if args.offset_hours is not None else [0, 1, 2]
        probes = []
        for offset in offsets:
            low, high = _band(rd, offset)
            popsink = _popsink_records(connection, reference, low, high)
            probes.append({"offset_hours": offset, "band_low_ms": low,
                           "band_high_ms": high, "popsink_rows": len(popsink),
                           "records": popsink})
        chosen = max(probes, key=lambda p: p["popsink_rows"])
        popsink = chosen["records"]
    finally:
        connection.close()

    offset = chosen["offset_hours"]
    rd_temporal = [key_from_rd_record_temporal(r, offset_hours=offset) for r in rd]
    popsink_temporal = [key_from_popsink_record_temporal(r) for r in popsink]
    temporal = compare_event_multisets(rd_temporal, popsink_temporal)

    rd_sequence = {
        technical_key(r["journal_receiver"], r["journal_sequence"], r["operation"])
        for r in rd
    }
    legacy_comparable = False
    sequence_matrix = None
    if popsink and "sequence" in popsink[0]:
        legacy_comparable = True
        sequence_matrix = compare_key_sets(
            rd_sequence,
            {technical_key(r["receiver"], r["sequence"], r["op"]) for r in popsink},
        ).to_record()

    comparability = band_comparability(
        band_low_ms=chosen["band_low_ms"],
        band_high_ms=chosen["band_high_ms"],
        coverage_low_ms=coverage_low,
        coverage_high_ms=coverage_high,
    )
    verdict = classify_matrix(
        overlap=temporal.overlap,
        extra=temporal.extra,
        missing=temporal.missing,
        comparable=comparability["comparable"],
    )

    report = {
        "status": "OK",
        "verdict": verdict,
        "comparability": comparability,
        "reference_coverage": {
            "low_ms": coverage_low,
            "high_ms": coverage_high,
            "rows": coverage_rows,
            "table": reference,
        },
        "rd_table": args.rd_table,
        "join_key_used": "commit_epoch_ms/operation",
        "offset_hours_selected": offset,
        "offset_probes": [
            {k: v for k, v in p.items() if k != "records"} for p in probes
        ],
        "band_low_ms": chosen["band_low_ms"],
        "band_high_ms": chosen["band_high_ms"],
        "temporal_matrix": temporal.to_record(),
        "legacy_sequence_matrix": sequence_matrix,
        "legacy_sequence_comparable": legacy_comparable,
        "legacy_note": "Popsink Snowflake exposes no journal sequence column, so the "
                       "legacy key cannot even be evaluated here; it was evaluated "
                       "against Kafka in the 2026-08-24 and img2 lots",
        "rd_operations": _counts(r["operation"] for r in rd),
        "popsink_operations": _counts(r["op"] for r in popsink),
        "popsink_deleted_flags": _counts(str(r.get("deleted")) for r in popsink),
        "read_only": True,
        "ibmi_calls": 0,
        "popsink_api_calls": 0,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    return 0 if verdict in {"PASS", "NOT_COMPARABLE"} else 1


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items()))


if __name__ == "__main__":
    raise SystemExit(main())
