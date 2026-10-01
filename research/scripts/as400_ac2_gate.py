#!/usr/bin/env python3
"""Offline AC2 gate: refuse POST stop unless the Job is the proven SQL pair."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quadringent_research.dual_sql_capture import ac2_claim, gate_job_legs
from quadringent_research.multi_object_capture import ac2_claim_from_groups, gate_multi_tables


def extract_leg_ids(yaml_text: str) -> list[str]:
    match = re.search(r"AS400_DUAL_LEG_IDS[^\n]*\n\s+value:\s+\"([^\"]+)\"", yaml_text)
    if not match:
        raise ValueError("AS400_DUAL_LEG_IDS missing from Job YAML")
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def extract_multi_tables(yaml_text: str) -> list[str]:
    match = re.search(r"AS400_MULTI_TABLES[^\n]*\n\s+value:\s+\"([^\"]+)\"", yaml_text)
    if not match:
        raise ValueError("AS400_MULTI_TABLES missing from Job YAML")
    return [item.strip().upper() for item in match.group(1).split(",") if item.strip()]


def _common_image_gate(yaml_text: str) -> str | None:
    """Refuse tout marqueur banni puis exige le digest d'image éprouvé du site.

    Les deux listes sont déclarées par le site via ``AS400_GATE_BANNED_MARKERS``
    (fragments séparés par des virgules) et ``AS400_GATE_IMAGE_DIGEST`` — aucune
    image n'est éprouvée par défaut dans le code.
    """

    banned = [
        marker.strip()
        for marker in os.environ.get("AS400_GATE_BANNED_MARKERS", "").split(",")
        if marker.strip()
    ]
    if any(marker in yaml_text for marker in banned):
        return "banned_marker"
    expected = os.environ.get("AS400_GATE_IMAGE_DIGEST", "").strip()
    if not expected or expected not in yaml_text:
        return "wrong_image"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml", required=True)
    parser.add_argument("--registry", required=True)
    args = parser.parse_args()
    yaml_text = Path(args.yaml).read_text(encoding="utf-8")
    image_fail = _common_image_gate(yaml_text)
    if image_fail:
        print(f"GATE_FAIL {image_fail}", file=sys.stderr)
        return 2
    if "as400_multi_object_capture.py" in yaml_text:
        if "MultiObjectDisplayJournal" not in yaml_text:
            print("GATE_FAIL missing_multi_java", file=sys.stderr)
            return 2
        if "as400_dual_sql_capture.py" in yaml_text:
            print("GATE_FAIL sequential_dual_not_allowed", file=sys.stderr)
            return 2
        tables = gate_multi_tables(extract_multi_tables(yaml_text))
        dry = ac2_claim_from_groups(
            {table: [object()] for table in tables},
            {table: {"decoded": 1, "elapsed_ms": 1} for table in tables},
        )
        if not dry:
            print("GATE_FAIL assertion_template", file=sys.stderr)
            return 2
        print(f"GATE_PASS tables={','.join(tables)} mode=one_retrieve_multi_object")
        return 0
    if "as400_dual_sql_capture.py" not in yaml_text:
        print("GATE_FAIL missing_dual_driver", file=sys.stderr)
        return 2
    legs = extract_leg_ids(yaml_text)
    selected = gate_job_legs(legs, args.registry)
    dry = ac2_claim(
        [
            {
                "table": item,
                "events_published": 1,
                "retrieve_summary": {"decoded": 1, "elapsed_ms": 1},
            }
            for item in selected
        ]
    )
    if not dry:
        print("GATE_FAIL assertion_template", file=sys.stderr)
        return 2
    print(f"GATE_PASS legs={','.join(selected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
