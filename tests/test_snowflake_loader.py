from __future__ import annotations

import site_fixture

import unittest

from quadringent import snowflake_loader
from quadringent.site_config import SnowflakeScope
from quadringent.snowflake_loader import SnowflakeCanonicalLoadPlan, SnowflakeRawLoadPlan


SITE = site_fixture.build_test_site()
SCOPE = SITE.snowflake_scope

# Un scope « étranger » dont la base porte un fragment interdit : les plans
# doivent le refuser avant de produire la moindre instruction.
FORBIDDEN_DB_SCOPE = SnowflakeScope(
    database="PROD_RAW",
    schema=SITE.destination_schema,
    forbidden_fragments=("PROD",),
)
FORBIDDEN_SCHEMA_SCOPE = SnowflakeScope(
    database=SITE.destination_database,
    schema="POPSINK_ALPHA",
    forbidden_fragments=("POPSINK",),
)


class SnowflakeRawLoadPlanTests(unittest.TestCase):
    def test_plan_is_scoped_to_one_stage_object(self) -> None:
        plan = SnowflakeRawLoadPlan(
            scope=SCOPE,
            table="SALE_CDC_RAW",
            stage="IBMI_TEST_EXTERNAL_STAGE",
        )
        statements = plan.statements_for("batch-abc.jsonl")
        sql = " ".join(statements)

        self.assertEqual(len(statements), 2)
        self.assertIn('CREATE TABLE IF NOT EXISTS "ACME_RAW"."IBMI_TEST"."SALE_CDC_RAW"', statements[0])
        self.assertIn('@"ACME_RAW"."IBMI_TEST"."IBMI_TEST_EXTERNAL_STAGE"/batch-abc.jsonl', statements[1])
        self.assertNotIn("POPSINK", sql)
        self.assertNotIn("PASSWORD", sql.upper())

    def test_plan_can_load_only_jsonl_payloads_from_the_whole_stage(self) -> None:
        plan = SnowflakeRawLoadPlan(
            scope=SCOPE,
            table="SALE_CDC_RAW",
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
        )

        statements = plan.statements_for_all_jsonl()

        self.assertEqual(len(statements), 2)
        self.assertIn(
            '@"ACME_RAW"."IBMI_TEST"."IBMI_TEST_SALE_EXTERNAL_STAGE"',
            statements[1],
        )
        self.assertIn("PATTERN = '.*[.]jsonl$'", statements[1])
        self.assertNotIn("console-snapshot.json", statements[1])

    def test_identifiers_and_paths_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            SnowflakeRawLoadPlan(
                scope=SnowflakeScope(
                    database="ACME_RAW;DROP TABLE X", schema="IBMI_TEST"
                ),
                table="SALE_CDC_RAW",
                stage="RAW_STAGE",
            )
        plan = SnowflakeRawLoadPlan(
            scope=SCOPE, table="SALE_CDC_RAW", stage="IBMI_TEST_EXTERNAL_STAGE"
        )
        with self.assertRaises(ValueError):
            plan.statements_for("../other-table.jsonl")
        with self.assertRaises(ValueError):
            plan.statements_for("batch-name;DROP.jsonl")
        with self.assertRaises(ValueError):
            plan.statements_for("batch name.jsonl")

    def test_plan_refuses_popsink_alpha_destination(self) -> None:
        with self.assertRaises(ValueError):
            SnowflakeRawLoadPlan(
                scope=FORBIDDEN_SCHEMA_SCOPE,
                table="SALE_CDC_RAW",
                stage="POPSINK_ALPHA_RAW_STAGE",
            )
        with self.assertRaises(ValueError):
            SnowflakeCanonicalLoadPlan(
                scope=FORBIDDEN_SCHEMA_SCOPE,
                raw_table="SALE_CDC_RAW",
                canonical_table="SALE_CDC_CANONICAL",
                stage="POPSINK_ALPHA_RAW_STAGE",
            )
        with self.assertRaises(ValueError):
            SnowflakeRawLoadPlan(
                scope=FORBIDDEN_DB_SCOPE,
                table="SALE_CDC_RAW",
                stage="IBMI_TEST_EXTERNAL_STAGE",
            )

    def test_canonical_plan_merges_by_event_id(self) -> None:
        plan = SnowflakeCanonicalLoadPlan(
            scope=SCOPE,
            raw_table="SALE_CDC_RAW",
            canonical_table="SALE_CDC_CANONICAL",
            stage="IBMI_TEST_EXTERNAL_STAGE",
        )

        statements = plan.statements_for("batch-abc.jsonl")
        sql = " ".join(statements)

        self.assertEqual(len(statements), 4)
        self.assertIn('CREATE TABLE IF NOT EXISTS "ACME_RAW"."IBMI_TEST"."SALE_CDC_CANONICAL"', statements[2])
        self.assertIn("MERGE INTO", statements[3])
        self.assertIn("target.EVENT_ID = source.EVENT_ID", statements[3])
        self.assertIn("WHEN NOT MATCHED", statements[3])
        self.assertIn("SOURCE_FILE = 'batch-abc.jsonl'", statements[3])
        self.assertNotIn("POPSINK", sql)
        self.assertNotIn("PASSWORD", sql.upper())

    def test_canonical_plan_can_merge_the_complete_jsonl_capture(self) -> None:
        plan = SnowflakeCanonicalLoadPlan(
            scope=SCOPE,
            raw_table="SALE_CDC_RAW",
            canonical_table="SALE_CDC_CANONICAL",
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
        )

        statements = plan.statements_for_all_jsonl()

        self.assertEqual(len(statements), 4)
        self.assertIn("PATTERN = '.*[.]jsonl$'", statements[1])
        self.assertIn('FROM "ACME_RAW"."IBMI_TEST"."SALE_CDC_RAW"', statements[3])
        self.assertNotIn("WHERE SOURCE_FILE", statements[3])

    def test_plan_can_load_an_explicit_jsonl_file_list(self) -> None:
        plan = SnowflakeRawLoadPlan(
            scope=SCOPE,
            table="SALE_CDC_RAW",
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
        )

        statements = plan.statements_for_object_keys(
            ["batch-aaa.jsonl", "batch-bbb.jsonl"]
        )

        self.assertEqual(len(statements), 2)
        self.assertIn("FILES = ('batch-aaa.jsonl', 'batch-bbb.jsonl')", statements[1])
        self.assertNotIn("PATTERN = '.*[.]jsonl$'", statements[1])
        self.assertNotIn("POPSINK", statements[1])

    def test_object_key_list_is_validated_and_bounded(self) -> None:
        plan = SnowflakeRawLoadPlan(
            scope=SCOPE,
            table="SALE_CDC_RAW",
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
        )

        with self.assertRaises(ValueError):
            plan.statements_for_object_keys([])
        with self.assertRaises(ValueError):
            plan.statements_for_object_keys(["../outside.jsonl"])
        with self.assertRaises(ValueError):
            plan.statements_for_object_keys(["batch-aaa.jsonl", "batch-aaa.jsonl"])
        with self.assertRaises(ValueError):
            plan.statements_for_object_keys(["batch-aaa.jsonl"] * 1001)

    def test_canonical_plan_merges_an_explicit_jsonl_file_list(self) -> None:
        plan = SnowflakeCanonicalLoadPlan(
            scope=SCOPE,
            raw_table="SALE_CDC_RAW",
            canonical_table="SALE_CDC_CANONICAL",
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
        )

        statements = plan.statements_for_object_keys(
            ["batch-aaa.jsonl", "batch-bbb.jsonl"]
        )

        self.assertEqual(len(statements), 4)
        self.assertIn("FILES = ('batch-aaa.jsonl', 'batch-bbb.jsonl')", statements[1])
        self.assertNotIn("WHERE SOURCE_FILE", statements[3])
        self.assertNotIn("PATTERN = '.*[.]jsonl$'", statements[1])


class SnowflakeAutonomousLoadPlanTests(unittest.TestCase):
    def test_plan_loads_new_sale_files_without_operator_credentials(self) -> None:
        self.assertTrue(
            hasattr(snowflake_loader, "SnowflakeAutonomousLoadPlan"),
            "the autonomous Snowflake load plan is not implemented",
        )
        SnowflakeAutonomousLoadPlan = snowflake_loader.SnowflakeAutonomousLoadPlan
        plan = SnowflakeAutonomousLoadPlan(
            scope=SCOPE,
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
            raw_table="QUADRINGENT_SALE_RAW",
            canonical_table="QUADRINGENT_SALE_CANONICAL",
            pipe="QUADRINGENT_SALE_PIPE",
            warehouse=SITE.warehouse_name,
        )

        (
            create_warehouse,
            create_raw,
            create_pipe,
            create_canonical,
            resume_pipe,
            suspend_warehouse,
        ) = plan.statements()
        sql = "\n".join(plan.statements())

        self.assertIn("CREATE WAREHOUSE IF NOT EXISTS", create_warehouse)
        self.assertIn("WAREHOUSE_SIZE = 'XSMALL'", create_warehouse)
        self.assertIn("AUTO_SUSPEND = 60", create_warehouse)
        self.assertIn("MAX_CLUSTER_COUNT = 1", create_warehouse)
        self.assertIn("CREATE TABLE IF NOT EXISTS", create_raw)
        self.assertIn("CREATE OR ALTER PIPE", create_pipe)
        self.assertIn("AUTO_INGEST = TRUE", create_pipe)
        self.assertIn("PATTERN = '.*[.]jsonl$'", create_pipe)
        self.assertNotIn("ON_ERROR", create_pipe)
        self.assertNotIn("FORCE", create_pipe)
        self.assertIn("CREATE OR REPLACE VIEW", create_canonical)
        self.assertNotIn("DYNAMIC TABLE", create_canonical)
        self.assertNotIn("TARGET_LAG", create_canonical)
        self.assertIn("PARTITION BY EVENT_ID", create_canonical)
        self.assertIn("ORDER BY JOURNAL_SEQUENCE DESC", create_canonical)
        self.assertIn("QUALIFY ROW_NUMBER()", create_canonical)
        self.assertIn("SET PIPE_EXECUTION_PAUSED = FALSE", resume_pipe)
        self.assertEqual(
            suspend_warehouse,
            f'ALTER WAREHOUSE IF EXISTS "{SITE.warehouse_name}" SUSPEND',
        )
        self.assertNotIn("POPSINK", sql)
        self.assertNotIn("DEV_INGESTION_WH", sql)
        self.assertNotIn("PASSWORD", sql.upper())

        pause_pipe, suspend_warehouse = plan.pause_statements()
        self.assertIn("SET PIPE_EXECUTION_PAUSED = TRUE", pause_pipe)
        self.assertIn(
            f'ALTER WAREHOUSE IF EXISTS "{SITE.warehouse_name}" SUSPEND',
            suspend_warehouse,
        )

    def test_plan_can_restore_the_previous_dynamic_table_after_failed_migration(
        self,
    ) -> None:
        plan = snowflake_loader.SnowflakeAutonomousLoadPlan(
            scope=SCOPE,
            stage="IBMI_TEST_SALE_EXTERNAL_STAGE",
            raw_table="QUADRINGENT_SALE_RAW",
            canonical_table="QUADRINGENT_SALE_CANONICAL",
            pipe="QUADRINGENT_SALE_PIPE",
            warehouse=SITE.warehouse_name,
        )

        self.assertEqual(
            plan.drop_legacy_canonical_statement(),
            'DROP DYNAMIC TABLE "ACME_RAW"."IBMI_TEST"."QUADRINGENT_SALE_CANONICAL"',
        )
        self.assertEqual(
            plan.drop_canonical_view_statement(),
            'DROP VIEW IF EXISTS "ACME_RAW"."IBMI_TEST"."QUADRINGENT_SALE_CANONICAL"',
        )
        restore = plan.restore_legacy_canonical_statements()
        self.assertEqual(len(restore), 2)
        self.assertIn("CREATE OR ALTER DYNAMIC TABLE", restore[0])
        self.assertIn("TARGET_LAG = '1 minute'", restore[0])
        self.assertTrue(restore[1].endswith(" SUSPEND"))

    def test_plan_refuses_non_dev_or_unsafe_objects(self) -> None:
        self.assertTrue(
            hasattr(snowflake_loader, "SnowflakeAutonomousLoadPlan"),
            "the autonomous Snowflake load plan is not implemented",
        )
        SnowflakeAutonomousLoadPlan = snowflake_loader.SnowflakeAutonomousLoadPlan
        base = {
            "scope": SCOPE,
            "stage": "IBMI_TEST_SALE_EXTERNAL_STAGE",
            "raw_table": "QUADRINGENT_SALE_RAW",
            "canonical_table": "QUADRINGENT_SALE_CANONICAL",
            "pipe": "QUADRINGENT_SALE_PIPE",
            "warehouse": SITE.warehouse_name,
        }

        for override in (
            {"scope": FORBIDDEN_DB_SCOPE},
            {"scope": FORBIDDEN_SCHEMA_SCOPE},
            {"stage": "POPSINK_ALPHA_STAGE"},
            {"pipe": "PIPE; DROP TABLE X"},
            {"warehouse": "WH WITH SPACE"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                SnowflakeAutonomousLoadPlan(**(base | override))


if __name__ == "__main__":
    unittest.main()
