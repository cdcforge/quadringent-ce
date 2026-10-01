"""Tests de la normalisation canonique et des littéraux SQL (schema.py)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from quadringent_qualification.schema import (
    CanonicalisationError,
    Column,
    TableSchema,
    canonical,
    canonical_value,
    delete_sql,
    insert_sql,
    parse_ibmi_date,
    parse_ibmi_timestamp,
    sql_literal,
    update_sql,
)


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


# --- TableSchema / Column validation -------------------------------------------

def test_column_char_requires_length():
    with pytest.raises(ValueError):
        Column("CODE", "char")


def test_column_decimal_requires_scale():
    with pytest.raises(ValueError):
        Column("AMOUNT", "decimal", precision=11)


def test_column_unknown_kind_rejected():
    with pytest.raises(ValueError):
        Column("X", "blob")


def test_table_schema_requires_primary_key_in_columns():
    with pytest.raises(ValueError):
        TableSchema("L.T", (Column("A", "integer"),), primary_key="B")


def test_table_schema_rejects_duplicate_columns():
    with pytest.raises(ValueError):
        TableSchema("L.T", (Column("A", "integer"), Column("A", "integer")), primary_key="A")


def test_table_and_column_identifiers_reject_sql_metacharacters():
    with pytest.raises(ValueError, match="table"):
        TableSchema("QUALIF_LIB.QUALIF_ORDERS;DELETE", (Column("ORDER_ID", "integer"),), "ORDER_ID")
    with pytest.raises(ValueError, match="colonne"):
        Column("LABEL); DROP TABLE X; --", "varchar", length=40)


# --- Dates : format IBM i ambigu explicitement refusé --------------------------

def test_parse_ibmi_date_accepts_iso():
    assert parse_ibmi_date("2026-09-23") == "2026-09-23"


def test_parse_ibmi_date_accepts_date_object():
    assert parse_ibmi_date(date(2026, 9, 23)) == "2026-09-23"


def test_parse_ibmi_date_rejects_two_digit_year():
    with pytest.raises(CanonicalisationError, match="année sur 2 chiffres"):
        parse_ibmi_date("26-09-23")


def test_parse_ibmi_date_rejects_garbage():
    with pytest.raises(CanonicalisationError):
        parse_ibmi_date("not-a-date")


# --- Timestamps : troncature/complétion des microsecondes ----------------------

def test_parse_ibmi_timestamp_from_datetime():
    value = datetime(2026, 9, 23, 8, 0, 1, 234567)
    assert parse_ibmi_timestamp(value) == "2026-09-23 08:00:01.234567"


def test_parse_ibmi_timestamp_ibmi_native_format():
    assert parse_ibmi_timestamp("2026-09-23-08.00.01.234567") == "2026-09-23 08:00:01.234567"


def test_parse_ibmi_timestamp_iso_with_t():
    assert parse_ibmi_timestamp("2026-09-23T08:00:01.234567") == "2026-09-23 08:00:01.234567"


def test_parse_ibmi_timestamp_truncates_long_fraction():
    assert parse_ibmi_timestamp("2026-09-23 08:00:01.2345678") == "2026-09-23 08:00:01.234567"


def test_parse_ibmi_timestamp_pads_short_fraction():
    assert parse_ibmi_timestamp("2026-09-23 08:00:01.2") == "2026-09-23 08:00:01.200000"


def test_parse_ibmi_timestamp_no_fraction_at_all():
    assert parse_ibmi_timestamp("2026-09-23 08:00:01") == "2026-09-23 08:00:01.000000"


# --- Décimaux : montants JSON en float ------------------------------------------

def test_canonical_value_decimal_from_json_float():
    column = Column("AMOUNT", "decimal", precision=11, scale=2)
    assert canonical_value(-99999.99, column) == "-99999.99"


def test_canonical_value_decimal_from_string():
    column = Column("AMOUNT", "decimal", precision=11, scale=2)
    assert canonical_value("12.5", column) == "12.50"


def test_canonical_value_decimal_none_stays_none():
    column = Column("AMOUNT", "decimal", precision=11, scale=2)
    assert canonical_value(None, column) is None


def test_canonical_value_decimal_invalid_raises():
    column = Column("AMOUNT", "decimal", precision=11, scale=2)
    with pytest.raises(CanonicalisationError):
        canonical_value("not-a-number", column)


# --- CHAR padding ---------------------------------------------------------------

def test_canonical_value_char_pads_with_spaces():
    column = Column("CODE", "char", length=8)
    assert canonical_value("AB", column) == "AB      "


def test_canonical_value_char_none_stays_none():
    column = Column("CODE", "char", length=8)
    assert canonical_value(None, column) is None


def test_canonical_value_varchar_preserves_trailing_spaces():
    column = Column("NOTE", "varchar", length=80)
    assert canonical_value("note 3  ", column) == "note 3  "


def test_canonical_value_integer_coerces_str():
    column = Column("ORDER_ID", "integer")
    assert canonical_value("42", column) == 42


# --- canonical() sur une ligne complète ------------------------------------------

def test_canonical_full_row():
    schema = make_schema()
    row = {
        "ORDER_ID": 1, "LABEL": "l'été", "CODE": "AB", "AMOUNT": Decimal("-1.50"),
        "EVENT_DATE": date(2026, 1, 2), "UPDATED_AT": datetime(2026, 1, 1, 8, 0, 1, 234567),
        "NOTE": None,
    }
    result = canonical(row, schema)
    assert result == {
        "ORDER_ID": 1, "LABEL": "l'été", "CODE": "AB      ", "AMOUNT": "-1.50",
        "EVENT_DATE": "2026-01-02", "UPDATED_AT": "2026-01-01 08:00:01.234567", "NOTE": None,
    }


# --- sql_literal : échappement des apostrophes et accents ---------------------

def test_sql_literal_none_is_null():
    assert sql_literal(None) == "NULL"


def test_sql_literal_escapes_apostrophes():
    assert sql_literal("l'été") == "'l''été'"


def test_sql_literal_accented_and_special_chars_pass_through_quoted():
    assert sql_literal("Größe ÄÖÜ äöü ß") == "'Größe ÄÖÜ äöü ß'"


def test_sql_literal_multiple_apostrophes():
    assert sql_literal("a'b'c") == "'a''b''c'"


def test_sql_literal_decimal_uses_column_scale():
    column = Column("AMOUNT", "decimal", precision=11, scale=2)
    assert sql_literal(Decimal("-0.01") * 5, column) == "-0.05"


def test_sql_literal_integer():
    assert sql_literal(42) == "42"


def test_sql_literal_date():
    assert sql_literal(date(2026, 9, 23)) == "DATE '2026-09-23'"


def test_sql_literal_datetime():
    assert sql_literal(datetime(2026, 9, 23, 8, 0, 1, 234567)) == "TIMESTAMP '2026-09-23 08:00:01.234567'"


# --- Génération SQL --------------------------------------------------------------

def test_insert_sql_contains_all_columns_in_order():
    schema = make_schema()
    row = {"ORDER_ID": 1, "LABEL": "x", "CODE": "AB", "AMOUNT": Decimal("1.00"),
           "EVENT_DATE": date(2026, 1, 1), "UPDATED_AT": datetime(2026, 1, 1), "NOTE": None}
    sql = insert_sql(schema, row)
    assert sql.startswith("INSERT INTO QUALIF_LIB.QUALIF_ORDERS (ORDER_ID, LABEL, CODE, AMOUNT, EVENT_DATE, UPDATED_AT, NOTE) VALUES (")
    assert "NULL" in sql


def test_update_sql_targets_primary_key():
    schema = make_schema()
    sql = update_sql(schema, 5, {"LABEL": "y"})
    assert sql == "UPDATE QUALIF_LIB.QUALIF_ORDERS SET LABEL = 'y' WHERE ORDER_ID = 5"


def test_delete_sql():
    schema = make_schema()
    assert delete_sql(schema, 5) == "DELETE FROM QUALIF_LIB.QUALIF_ORDERS WHERE ORDER_ID = 5"
