from __future__ import annotations

import site_fixture

import unittest

from quadringent.snowflake_destination import (
    ColumnDefinition,
    IbmiColumnType,
    TableDestinationPlan,
    UnsupportedColumnTypeError,
    snowflake_type_for,
)


SITE = site_fixture.build_test_site()
SCOPE = SITE.snowflake_scope


class SnowflakeTypeForTests(unittest.TestCase):
    def test_char_and_varchar_map_to_varchar(self) -> None:
        self.assertEqual(snowflake_type_for(IbmiColumnType("char", length=10)), "VARCHAR(10)")
        self.assertEqual(snowflake_type_for(IbmiColumnType("varchar", length=255)), "VARCHAR(255)")

    def test_graphic_maps_to_varchar_in_characters(self) -> None:
        self.assertEqual(snowflake_type_for(IbmiColumnType("graphic", length=20)), "VARCHAR(20)")
        self.assertEqual(snowflake_type_for(IbmiColumnType("vargraphic", length=20)), "VARCHAR(20)")

    def test_decimal_numeric_map_to_number_with_precision_and_scale(self) -> None:
        self.assertEqual(
            snowflake_type_for(IbmiColumnType("decimal", precision=9, scale=2)), "NUMBER(9, 2)"
        )
        self.assertEqual(
            snowflake_type_for(IbmiColumnType("numeric", precision=15, scale=4)), "NUMBER(15, 4)"
        )

    def test_integer_family_maps_to_fixed_number_widths(self) -> None:
        self.assertEqual(snowflake_type_for(IbmiColumnType("smallint")), "NUMBER(5, 0)")
        self.assertEqual(snowflake_type_for(IbmiColumnType("integer")), "NUMBER(10, 0)")
        self.assertEqual(snowflake_type_for(IbmiColumnType("bigint")), "NUMBER(19, 0)")

    def test_date_time_timestamp(self) -> None:
        self.assertEqual(snowflake_type_for(IbmiColumnType("date")), "DATE")
        self.assertEqual(snowflake_type_for(IbmiColumnType("time")), "TIME")
        self.assertEqual(
            snowflake_type_for(IbmiColumnType("timestamp", timestamp_precision=6)),
            "TIMESTAMP_NTZ(6)",
        )
        self.assertEqual(
            snowflake_type_for(IbmiColumnType("timestamp", timestamp_precision=0)),
            "TIMESTAMP_NTZ(0)",
        )

    def test_binary_and_blob_map_to_binary_within_snowflake_limit(self) -> None:
        self.assertEqual(snowflake_type_for(IbmiColumnType("binary", length=16)), "BINARY(16)")
        self.assertEqual(snowflake_type_for(IbmiColumnType("varbinary", length=16)), "BINARY(16)")
        self.assertEqual(snowflake_type_for(IbmiColumnType("blob", length=1024)), "BINARY(1024)")

    def test_clob_maps_to_varchar_within_snowflake_limit(self) -> None:
        self.assertEqual(snowflake_type_for(IbmiColumnType("clob", length=5000)), "VARCHAR(5000)")

    def test_blob_beyond_snowflake_binary_limit_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedColumnTypeError):
            snowflake_type_for(IbmiColumnType("blob", length=8_388_609))

    def test_clob_beyond_snowflake_varchar_limit_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedColumnTypeError):
            snowflake_type_for(IbmiColumnType("clob", length=16_777_217))

    def test_decimal_precision_beyond_snowflake_number_limit_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedColumnTypeError):
            snowflake_type_for(IbmiColumnType("decimal", precision=39, scale=0))

    def test_unknown_kind_is_rejected_at_construction(self) -> None:
        with self.assertRaises(UnsupportedColumnTypeError):
            IbmiColumnType("rowid")

    def test_binary_ccsid_char_column_is_rejected_explicitly(self) -> None:
        with self.assertRaises(UnsupportedColumnTypeError):
            IbmiColumnType("char", length=10, ccsid=65535)

    def test_char_without_length_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IbmiColumnType("char")

    def test_decimal_without_scale_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            IbmiColumnType("decimal", precision=9)


class ColumnDefinitionTests(unittest.TestCase):
    def test_reserved_technical_column_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ColumnDefinition(name="EVENT_ID", type=IbmiColumnType("integer"))

    def test_invalid_identifier_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ColumnDefinition(name="1BAD", type=IbmiColumnType("integer"))


def _columns() -> tuple[ColumnDefinition, ...]:
    return (
        ColumnDefinition(name="ORDER_ID", type=IbmiColumnType("integer"), nullable=False),
        ColumnDefinition(name="LABEL", type=IbmiColumnType("varchar", length=60)),
        ColumnDefinition(name="AMOUNT", type=IbmiColumnType("decimal", precision=9, scale=2)),
        ColumnDefinition(name="EVENT_DATE", type=IbmiColumnType("date")),
        ColumnDefinition(name="UPDATED_AT", type=IbmiColumnType("timestamp", timestamp_precision=6)),
    )


class TableDestinationPlanTests(unittest.TestCase):
    def test_history_ddl_carries_technical_and_business_columns(self) -> None:
        plan = TableDestinationPlan(
            scope=SCOPE,
            history_table="SALE_HISTORY",
            mirror_table="SALE_MIRROR",
            columns=_columns(),
            key_columns=("ORDER_ID",),
        )
        ddl = plan.history_ddl()

        self.assertIn('CREATE TABLE IF NOT EXISTS "ACME_RAW"."IBMI_TEST"."SALE_HISTORY"', ddl)
        self.assertIn("EVENT_ID VARCHAR NOT NULL", ddl)
        self.assertIn("OPERATION VARCHAR NOT NULL", ddl)
        self.assertIn("JOURNAL_SEQUENCE NUMBER(38, 0) NOT NULL", ddl)
        self.assertIn("COMMIT_TIMESTAMP TIMESTAMP_NTZ(6) NOT NULL", ddl)
        self.assertIn("INGESTED_AT TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()", ddl)
        self.assertIn("ORDER_ID NUMBER(10, 0) NOT NULL", ddl)
        self.assertIn("AMOUNT NUMBER(9, 2)", ddl)
        self.assertIn("EVENT_DATE DATE", ddl)
        self.assertIn("UPDATED_AT TIMESTAMP_NTZ(6)", ddl)
        # Pas de colonne miroir dans l'historique (append-only, pas de MIRROR_UPDATED_AT)
        self.assertNotIn("MIRROR_UPDATED_AT", ddl)

    def test_mirror_ddl_carries_primary_key_and_no_operation_column(self) -> None:
        plan = TableDestinationPlan(
            scope=SCOPE,
            history_table="SALE_HISTORY",
            mirror_table="SALE_MIRROR",
            columns=_columns(),
            key_columns=("ORDER_ID",),
        )
        ddl = plan.mirror_ddl()

        self.assertIn('CREATE TABLE IF NOT EXISTS "ACME_RAW"."IBMI_TEST"."SALE_MIRROR"', ddl)
        self.assertIn("MIRROR_UPDATED_AT TIMESTAMP_LTZ NOT NULL", ddl)
        self.assertIn("PRIMARY KEY (ORDER_ID)", ddl)
        self.assertNotIn("\nOPERATION ", ddl)

    def test_duplicate_column_names_are_rejected(self) -> None:
        columns = _columns() + (ColumnDefinition(name="ORDER_ID", type=IbmiColumnType("integer")),)
        with self.assertRaises(ValueError):
            TableDestinationPlan(
                scope=SCOPE,
                history_table="SALE_HISTORY",
                mirror_table="SALE_MIRROR",
                columns=columns,
                key_columns=("ORDER_ID",),
            )

    def test_unknown_key_column_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TableDestinationPlan(
                scope=SCOPE,
                history_table="SALE_HISTORY",
                mirror_table="SALE_MIRROR",
                columns=_columns(),
                key_columns=("NOT_A_COLUMN",),
            )

    def test_forbidden_fragment_scope_is_rejected(self) -> None:
        from quadringent.site_config import SnowflakeScope

        forbidden_scope = SnowflakeScope(
            database="PROD_RAW",
            schema=SITE.destination_schema,
            forbidden_fragments=("PROD",),
        )
        with self.assertRaises(ValueError):
            TableDestinationPlan(
                scope=forbidden_scope,
                history_table="SALE_HISTORY",
                mirror_table="SALE_MIRROR",
                columns=_columns(),
                key_columns=("ORDER_ID",),
            )


if __name__ == "__main__":
    unittest.main()


class DefaultTableNameTests(unittest.TestCase):
    def test_mirror_scope_cannot_escape_database_or_forbidden_fragments(self):
        from quadringent.site_config import SnowflakeScope
        scope = SnowflakeScope(database="CLIENT_DB", schema="RAW", forbidden_fragments=("FORBIDDEN",))
        for mirror_scope in (
            SnowflakeScope(database="OTHER_DB", schema="CURATED"),
            SnowflakeScope(database="CLIENT_DB", schema="FORBIDDEN_SCHEMA"),
        ):
            with self.assertRaises(ValueError):
                TableDestinationPlan(scope=scope, mirror_scope=mirror_scope, history_table="SALE_HISTORY",
                                     mirror_table="SALE_MIRROR", columns=_columns(), key_columns=("ORDER_ID",))

    def test_history_and_mirror_names_are_derived_and_uppercased(self) -> None:
        from quadringent.snowflake_destination import default_history_table_name, default_mirror_table_name

        self.assertEqual(default_history_table_name("sale"), "SALE_HISTORY")
        self.assertEqual(default_mirror_table_name("sale"), "SALE_MIRROR")

    def test_empty_table_name_is_rejected(self) -> None:
        from quadringent.snowflake_destination import default_history_table_name

        with self.assertRaises(ValueError):
            default_history_table_name("   ")

    def test_overlong_derived_name_is_rejected(self) -> None:
        from quadringent.snowflake_destination import default_mirror_table_name

        with self.assertRaises(ValueError):
            default_mirror_table_name("X" * 60)
