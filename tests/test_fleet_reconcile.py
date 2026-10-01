"""Réconciliation : comparaison par identité, et jamais par position."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "quadringent_fleet_reconcile", ROOT / "scripts" / "quadringent_fleet_reconcile.py"
)
reconcile_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(reconcile_module)


def test_identical_rows_have_no_gap() -> None:
    rows = {"a": {"X": 1, "Y": "v"}, "b": {"X": 2, "Y": "w"}}
    report = reconcile_module.reconcile(rows, rows)
    assert report["matched_rows"] == 2
    assert report["value_mismatches"] == 0
    assert report["compared_cells"] == 4


def test_a_different_value_is_reported() -> None:
    source = {"a": {"X": 1}}
    destination = {"a": {"X": 2}}
    report = reconcile_module.reconcile(source, destination)
    assert report["value_mismatches"] == 1
    assert report["samples"] == [{"event": "a", "column": "X"}]


def test_a_missing_or_extra_row_is_separated_from_a_value_gap() -> None:
    report = reconcile_module.reconcile({"a": {"X": 1}}, {"b": {"X": 1}})
    assert report["missing_in_destination"] == 1
    assert report["extra_in_destination"] == 1
    assert report["value_mismatches"] == 0


def test_a_column_set_difference_is_its_own_finding() -> None:
    report = reconcile_module.reconcile({"a": {"X": 1}}, {"a": {"X": 1, "Z": 2}})
    assert report["column_set_mismatches"] == 1
    assert report["value_mismatches"] == 0


def test_null_and_numeric_cells_are_counted() -> None:
    rows = {"a": {"X": None, "Y": 7, "Z": "text"}}
    report = reconcile_module.reconcile(rows, rows)
    assert report["null_cells_compared"] == 1
    assert report["numeric_cells_compared"] == 1


def test_a_variant_returned_as_text_is_still_an_object() -> None:
    """Le connecteur rend un VARIANT tantôt objet, tantôt chaîne JSON."""

    assert reconcile_module._as_mapping('{"X": 1}') == {"X": 1}
    assert reconcile_module._as_mapping({"X": 1}) == {"X": 1}
    assert reconcile_module._as_mapping(None) == {}


def test_source_lots_are_read_without_relying_on_order(tmp_path: Path) -> None:
    (tmp_path / "batch-a.jsonl").write_text(
        json.dumps({"event_id": "e2", "after": {"X": 2}}) + "\n"
    )
    (tmp_path / "batch-b.jsonl").write_text(
        json.dumps({"event_id": "e1", "after": {"X": 1}}) + "\n"
    )
    rows = reconcile_module._load_source(tmp_path)
    assert set(rows) == {"e1", "e2"}


def test_a_source_row_without_identity_is_refused(tmp_path: Path) -> None:
    (tmp_path / "batch-a.jsonl").write_text(json.dumps({"after": {"X": 1}}) + "\n")
    try:
        reconcile_module._load_source(tmp_path)
    except ValueError:
        return
    raise AssertionError("a source row without identity must be refused")
