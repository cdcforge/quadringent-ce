"""Tests du générateur synthétique et des étapes DML (generator.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from quadringent_qualification.generator import (
    DML_STEP_NAMES,
    build_oracle,
    generated_row,
    require_default_columns,
    step_plan,
)
from quadringent_qualification.schema import Column, TableSchema


def make_schema() -> TableSchema:
    return TableSchema(
        qualified_name="QUALIF_LIB.QUALIF_ORDERS",
        primary_key="ORDER_ID",
        columns=(
            Column("ORDER_ID", "integer"),
            Column("LABEL", "varchar", length=40),
            Column("CODE", "char", length=8),
            Column("AMOUNT", "decimal", precision=11, scale=2),
            Column("EVENT_DATE", "date"),
            Column("UPDATED_AT", "timestamp", timestamp_precision=6),
            Column("NOTE", "varchar", length=80),
        ),
    )


# --- Déterminisme du générateur ---------------------------------------------

def test_generated_row_is_deterministic():
    assert generated_row(42) == generated_row(42)


def test_generated_row_covers_null_values():
    # i % 10 == 0 -> LABEL is None ; i=10 est le premier
    assert generated_row(10)["LABEL"] is None


def test_generated_row_covers_empty_string():
    # i % 10 == 1 -> LABEL == ""
    assert generated_row(1)["LABEL"] == ""


def test_generated_row_covers_accents_and_apostrophe():
    row = generated_row(2)  # i % 10 == 2
    assert "é" in row["LABEL"] and "'" in row["LABEL"]


def test_generated_row_covers_signed_negative_amount():
    negatives = [generated_row(i)["AMOUNT"] for i in range(1, 40) if generated_row(i)["AMOUNT"] is not None]
    assert any(a < 0 for a in negatives)
    assert any(a > 0 for a in negatives)


def test_generated_row_covers_null_amount():
    assert generated_row(9)["AMOUNT"] is None  # i % 9 == 0


def test_generated_row_covers_null_date():
    assert generated_row(11)["EVENT_DATE"] is None  # i % 11 == 0


def test_generated_row_covers_null_timestamp():
    assert generated_row(13)["UPDATED_AT"] is None  # i % 13 == 0


def test_generated_row_rejects_non_positive_index():
    with pytest.raises(ValueError):
        generated_row(0)


# --- Étapes DML ---------------------------------------------------------------

def test_require_default_columns_rejects_incompatible_schema():
    schema = TableSchema("L.T", (Column("ORDER_ID", "integer"),), primary_key="ORDER_ID")
    with pytest.raises(ValueError, match="colonnes manquantes"):
        require_default_columns(schema)


def test_step_plan_unknown_name_raises():
    with pytest.raises(ValueError, match="étape DML inconnue"):
        step_plan("bogus", make_schema())


def test_seed_produces_100_insert_statements():
    plan = step_plan("seed", make_schema())
    assert len(plan.statements) == 100
    assert all(s.startswith("INSERT INTO QUALIF_LIB.QUALIF_ORDERS") for s in plan.statements)


def test_seed_apply_populates_oracle_with_keys_1_to_100():
    plan = step_plan("seed", make_schema())
    oracle: dict[int, dict] = {}
    plan.apply(oracle)
    assert set(oracle) == set(range(1, 101))


def test_changes1_inserts_updates_and_deletes():
    schema = make_schema()
    oracle = {}
    step_plan("seed", schema).apply(oracle)
    step_plan("changes1", schema).apply(oracle)
    assert set(range(101, 111)) <= set(oracle)  # insertions
    assert set(range(91, 96)).isdisjoint(oracle)  # suppressions
    assert oracle[1]["LABEL"] == "Modifié n°1"  # modification


def test_changes3_updates_amount_to_signed_large_decimal():
    schema = make_schema()
    oracle = {}
    for step in ("seed", "changes1", "changes2", "changes3"):
        step_plan(step, schema).apply(oracle)
    assert oracle[20]["AMOUNT"] == Decimal("-99999.99")
    assert oracle[21]["NOTE"] is None


def test_build_oracle_full_scenario_has_110_keys_canonical_form():
    schema = make_schema()
    oracle = build_oracle(DML_STEP_NAMES, schema)
    assert len(oracle) == 110
    # forme canonique : AMOUNT est une chaîne à 2 décimales, pas un Decimal
    sample = next(v for v in oracle.values() if v["AMOUNT"] is not None)
    assert isinstance(sample["AMOUNT"], str)


def test_build_oracle_is_order_independent_of_dict_iteration():
    schema = make_schema()
    first = build_oracle(DML_STEP_NAMES, schema)
    second = build_oracle(list(DML_STEP_NAMES), schema)
    assert first == second
