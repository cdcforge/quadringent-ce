"""Tâche 4 — parsing du protocole ``discover``, classification de readiness
et génération des commandes CL correctives (offline, aucune connexion IBM i).
"""

from __future__ import annotations

import pytest

from quadringent.table_discovery import (
    IMAGES_INCOMPLETE,
    JOURNAL_MISMATCH,
    NOT_JOURNALED,
    NO_KEY,
    READY,
    DiscoverProtocolError,
    DiscoveredColumn,
    DiscoveredTable,
    classify_selection,
    classify_table,
    discovered_column_to_type_kwargs,
    ibmi_catalog_type_to_kind,
    parse_discover_output,
)


def _row(
    *,
    library="SALES",
    system_name="ORDHDR",
    sql_name="ORDER_HEADER",
    text="En-tête de commande",
    row_count="1200",
    size_bytes="65536",
    has_key="yes",
    key_columns="ORDER_ID,LINE_NO",
    journaled="yes",
    journal_library="SALES",
    journal_name="ORDJRN",
    images="*BOTH",
    omitted="no",
    columns="",
) -> str:
    return "\t".join(
        [
            "table",
            library,
            system_name,
            sql_name,
            text,
            row_count,
            size_bytes,
            has_key,
            key_columns,
            journaled,
            journal_library,
            journal_name,
            images,
            omitted,
            columns,
        ]
    )


def test_parse_discover_output_round_trips_a_ready_table() -> None:
    tables = parse_discover_output(_row())
    assert len(tables) == 1
    table = tables[0]
    assert table.qualified_name == "SALES/ORDHDR"
    assert table.journal_qualified_name == "SALES/ORDJRN"
    assert table.key_columns == ("ORDER_ID", "LINE_NO")
    assert table.row_count == 1200
    assert table.size_bytes == 65536
    assert table.has_key is True
    assert table.journaled is True


def test_parse_discover_output_handles_multiple_lines_and_blank_lines() -> None:
    output = "\n".join([_row(system_name="ORDHDR"), "", _row(system_name="ORDLINE", sql_name="ORDER_LINE")])
    tables = parse_discover_output(output)
    assert [t.system_name for t in tables] == ["ORDHDR", "ORDLINE"]


def test_parse_discover_output_empty_fields_become_none_or_empty_tuple() -> None:
    output = _row(
        text="",
        row_count="",
        size_bytes="",
        has_key="no",
        key_columns="",
        journaled="no",
        journal_library="",
        journal_name="",
        images="",
    )
    (table,) = parse_discover_output(output)
    assert table.text is None
    assert table.row_count is None
    assert table.size_bytes is None
    assert table.key_columns == ()
    assert table.journaled is False
    assert table.journal_library is None
    assert table.images is None


def test_parse_discover_output_rejects_wrong_field_count() -> None:
    with pytest.raises(DiscoverProtocolError):
        parse_discover_output("table\tSALES\tORDHDR")


def test_parse_discover_output_rejects_wrong_tag() -> None:
    with pytest.raises(DiscoverProtocolError):
        parse_discover_output(_row().replace("table\t", "row\t", 1))


def test_parse_discover_output_rejects_duplicate_tables() -> None:
    output = "\n".join([_row(), _row()])
    with pytest.raises(DiscoverProtocolError):
        parse_discover_output(output)


def test_parse_discover_output_rejects_invalid_yes_no_field() -> None:
    with pytest.raises(DiscoverProtocolError):
        parse_discover_output(_row(has_key="maybe"))


def _table(**overrides) -> DiscoveredTable:
    defaults = dict(
        library="SALES",
        system_name="ORDHDR",
        sql_name="ORDER_HEADER",
        text="En-tête",
        row_count=1200,
        size_bytes=65536,
        has_key=True,
        key_columns=("ORDER_ID",),
        journaled=True,
        journal_library="SALES",
        journal_name="ORDJRN",
        images="*BOTH",
        omitted=False,
    )
    defaults.update(overrides)
    return DiscoveredTable(**defaults)


def test_classify_table_ready() -> None:
    result = classify_table(_table())
    assert result.state == READY
    assert result.cl_commands == ()


def test_classify_table_not_journaled_with_existing_journal_in_library() -> None:
    table = _table(journaled=False, journal_library=None, journal_name=None, images=None)
    result = classify_table(table, has_journal_in_library=True)
    assert result.state == NOT_JOURNALED
    assert len(result.cl_commands) == 1
    assert result.cl_commands[0].command.startswith("STRJRNPF FILE(SALES/ORDHDR) JRN(SALES/QSQJRN)")
    assert "IMAGES(*BOTH)" in result.cl_commands[0].command
    assert "OMTJRNE(*OPNCLO)" in result.cl_commands[0].command


def test_classify_table_not_journaled_without_journal_creates_receiver_and_journal_first() -> None:
    table = _table(journaled=False, journal_library=None, journal_name=None, images=None)
    result = classify_table(table, has_journal_in_library=False)
    assert result.state == NOT_JOURNALED
    assert len(result.cl_commands) == 3
    assert result.cl_commands[0].command.startswith("CRTJRNRCV JRNRCV(SALES/QSQJRN0001)")
    assert result.cl_commands[1].command.startswith("CRTJRN JRN(SALES/QSQJRN) JRNRCV(SALES/QSQJRN0001)")
    assert result.cl_commands[2].command.startswith("STRJRNPF FILE(SALES/ORDHDR)")


def test_classify_table_images_incomplete_generates_chgjrnobj() -> None:
    table = _table(images="*AFTER")
    result = classify_table(table)
    assert result.state == IMAGES_INCOMPLETE
    assert len(result.cl_commands) == 1
    command = result.cl_commands[0].command
    assert command == "CHGJRNOBJ OBJ((SALES/ORDHDR *FILE)) ATR(*IMAGES) IMAGES(*BOTH)"


def test_classify_table_no_key_explains_rrn_fallback_consistently_with_onboarding() -> None:
    table = _table(has_key=False, key_columns=())
    result = classify_table(table)
    assert result.state == NO_KEY
    assert result.cl_commands == ()
    assert "position physique (RRN)" in result.explanation
    assert "RGZPFM, CLRPFM" in result.explanation
    assert "resynchronisation" in result.explanation


def test_classify_selection_detects_journal_mismatch() -> None:
    tables = (
        _table(system_name="ORDHDR", journal_library="SALES", journal_name="ORDJRN"),
        _table(system_name="ORDLINE", journal_library="SALES", journal_name="LINEJRN"),
    )
    selection = classify_selection(tables)
    assert selection.journal_mismatch is True
    assert selection.journal_mismatch_explanation is not None
    assert "SALES/LINEJRN" in selection.journal_mismatch_explanation
    assert "SALES/ORDJRN" in selection.journal_mismatch_explanation
    assert [r.state for r in selection.per_table] == [READY, READY]


def test_classify_selection_no_mismatch_when_same_journal() -> None:
    tables = (
        _table(system_name="ORDHDR", journal_library="SALES", journal_name="ORDJRN"),
        _table(system_name="ORDLINE", journal_library="SALES", journal_name="ORDJRN"),
    )
    selection = classify_selection(tables)
    assert selection.journal_mismatch is False
    assert selection.journal_mismatch_explanation is None


def test_classify_selection_ignores_unjournaled_tables_for_mismatch() -> None:
    tables = (
        _table(system_name="ORDHDR", journal_library="SALES", journal_name="ORDJRN"),
        _table(system_name="LEGACY", journaled=False, journal_library=None, journal_name=None, images=None),
    )
    selection = classify_selection(tables)
    assert selection.journal_mismatch is False


def test_journal_mismatch_constant_is_exposed_for_api_layer() -> None:
    # Regression : le nom de l'état doit rester stable pour le contrat API/DB.
    assert JOURNAL_MISMATCH == "journal_mismatch"


def test_char_padded_ibm_i_names_are_trimmed() -> None:
    """Constaté sur un IBM i réel : SYSTEM_TABLE_NAME est un CHAR(10) complété d'espaces."""
    (table,) = parse_discover_output(
        _row(library="SALES     ", system_name="ORD_TAIL  ", journal_library="SALES     ",
             journal_name="ORDJRN    ", key_columns="ORDER_ID  ,LINE_NO")
    )
    assert table.library == "SALES"
    assert table.system_name == "ORD_TAIL"
    assert table.journal_library == "SALES"
    assert table.journal_name == "ORDJRN"
    assert table.key_columns == ("ORDER_ID", "LINE_NO")
    assert classify_table(table).explanation.startswith("SALES/ORD_TAIL est ")


# --- Colonnes découvertes (objectif C, chantier 2026-09-24) ------------------


def test_parse_discover_output_parses_the_trailing_columns_field() -> None:
    (table,) = parse_discover_output(_row(columns="ORDER_ID,DECIMAL,7,0,no;LABEL,VARCHAR,60,,yes"))
    assert table.columns == (
        DiscoveredColumn(name="ORDER_ID", type="DECIMAL", length=7, scale=0, nullable=False),
        DiscoveredColumn(name="LABEL", type="VARCHAR", length=60, scale=None, nullable=True),
    )


def test_parse_discover_output_empty_columns_field_is_no_columns() -> None:
    (table,) = parse_discover_output(_row(columns=""))
    assert table.columns == ()


def test_parse_discover_output_rejects_a_malformed_columns_entry() -> None:
    with pytest.raises(DiscoverProtocolError):
        parse_discover_output(_row(columns="ORDER_ID,DECIMAL,7"))


def test_ibmi_catalog_type_to_kind_recognizes_documented_short_names() -> None:
    assert ibmi_catalog_type_to_kind("DECIMAL") == "decimal"
    assert ibmi_catalog_type_to_kind(" varchar ") == "varchar"
    assert ibmi_catalog_type_to_kind("CHARACTER") == "char"
    assert ibmi_catalog_type_to_kind("TIMESTAMP") == "timestamp"


def test_ibmi_catalog_type_to_kind_unknown_type_is_none_never_guessed() -> None:
    assert ibmi_catalog_type_to_kind("ROWID") is None
    assert ibmi_catalog_type_to_kind("XML") is None


def test_discovered_column_to_type_kwargs_routes_decimal_length_to_precision() -> None:
    column = DiscoveredColumn(name="AMOUNT", type="DECIMAL", length=9, scale=2, nullable=False)
    assert discovered_column_to_type_kwargs(column) == {"kind": "decimal", "precision": 9, "scale": 2}


def test_discovered_column_to_type_kwargs_routes_varchar_length_to_length() -> None:
    column = DiscoveredColumn(name="LABEL", type="VARCHAR", length=60, scale=None, nullable=True)
    assert discovered_column_to_type_kwargs(column) == {"kind": "varchar", "length": 60}


def test_discovered_column_to_type_kwargs_unknown_type_is_none() -> None:
    column = DiscoveredColumn(name="WEIRD", type="ROWID", length=None, scale=None, nullable=True)
    assert discovered_column_to_type_kwargs(column) is None


def test_real_ibm_i_catalog_line_maps_every_column_type() -> None:
    """Ligne ``discover`` réelle (IBM i 7.5, 24 septembre 2026) : SYSCOLUMNS
    abrège les types sur 8 caractères (``TIMESTMP``) ; aucune colonne ne doit
    rester sans type, sinon elle disparaîtrait de la destination."""
    from quadringent.table_discovery import discovered_column_to_type_kwargs, parse_discover_output

    line = (
        "table\tLIB1\tORDERS\tORDERS\t\t110\t176128\tyes\tORDER_ID\tyes\tLIB1\tJRN1\t*BOTH\tno\t"
        "ORDER_ID,INTEGER,4,0,no;LABEL,VARCHAR,40,,yes;CODE,CHAR,8,,yes;AMOUNT,DECIMAL,11,2,yes;"
        "EVENT_DATE,DATE,4,,yes;UPDATED_AT,TIMESTMP,10,,yes;NOTE,VARCHAR,80,,yes"
    )
    [table] = parse_discover_output(line + "\n")
    kinds = {column.name: (discovered_column_to_type_kwargs(column) or {}).get("kind") for column in table.columns}
    assert kinds == {"ORDER_ID": "integer", "LABEL": "varchar", "CODE": "char", "AMOUNT": "decimal",
                     "EVENT_DATE": "date", "UPDATED_AT": "timestamp", "NOTE": "varchar"}


def test_db2_for_i_eight_character_type_abbreviations() -> None:
    from quadringent.table_discovery import ibmi_catalog_type_to_kind

    assert [ibmi_catalog_type_to_kind(t) for t in ("TIMESTMP", "VARG", "VARBIN")] == ["timestamp", "vargraphic", "varbinary"]
