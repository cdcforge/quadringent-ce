from __future__ import annotations

import unittest

import site_fixture

from quadringent.site_config import SnowflakeScope
from quadringent.snowflake_business import SnowflakeBusinessMergePlan

SITE = site_fixture.build_test_site()

def make_plan() -> SnowflakeBusinessMergePlan:
    return SnowflakeBusinessMergePlan(
        scope=SITE.snowflake_scope, 
        canonical_table="CNTR_CDC_CANONICAL",
        target_table="CNTR_CURRENT",
        source_library=SITE.source_schema,
        source_table="CNTR",
        key_columns=("PYPA", "PYCODE"),
    )


class SnowflakeBusinessMergePlanTests(unittest.TestCase):
    def test_plan_refuses_popsink_as400_alpha_destination(self) -> None:
        forbidden = SnowflakeScope(
            database="DEV_RAW", schema="POPSINK_ALPHA",
            forbidden_fragments=("POPSINK",),
        )
        with self.assertRaises(ValueError):
            SnowflakeBusinessMergePlan(
                scope=forbidden,
                canonical_table="CNTR_CDC_CANONICAL",
                target_table="CNTR_CURRENT",
                source_library=SITE.source_schema,
                source_table="CNTR",
                key_columns=("PYPA", "PYCODE"),
            )
        sql = " ".join(make_plan().statements_for())
        self.assertIn('"ACME_RAW"."IBMI_TEST"', sql)
        self.assertNotIn("POPSINK", sql)

    def test_plan_rejects_empty_duplicate_and_unsafe_keys(self) -> None:
        with self.assertRaises(ValueError):
            SnowflakeBusinessMergePlan(
                scope=SITE.snowflake_scope, 
                canonical_table="CNTR_CDC_CANONICAL",
                target_table="CNTR_CURRENT",
                source_library=SITE.source_schema,
                source_table="CNTR",
                key_columns=(),
            )
        with self.assertRaises(ValueError):
            SnowflakeBusinessMergePlan(
                scope=SITE.snowflake_scope, 
                canonical_table="CNTR_CDC_CANONICAL",
                target_table="CNTR_CURRENT",
                source_library=SITE.source_schema,
                source_table="CNTR",
                key_columns=("PYPA", "PYPA"),
            )
        with self.assertRaises(ValueError):
            SnowflakeBusinessMergePlan(
                scope=SITE.snowflake_scope, 
                canonical_table="CNTR_CDC_CANONICAL",
                target_table="CNTR_CURRENT",
                source_library=SITE.source_schema,
                source_table="CNTR",
                key_columns=("PYPA;DROP",),
            )

    def test_sql_has_validation_and_deterministic_c_u_d_expansion(self) -> None:
        statements = make_plan().statements_for()

        self.assertEqual(len(statements), 4)
        self.assertIn("ERROR_ON_NONDETERMINISTIC_MERGE = TRUE", statements[0])
        self.assertIn("INVALID_EVENT_COUNT", statements[2])
        self.assertIn("PAYLOAD:before.PYPA IS NULL", statements[2])
        self.assertIn("PAYLOAD:after.PYCODE IS NULL", statements[2])
        self.assertIn("NOT IS_NULL_VALUE(PAYLOAD:after)", statements[2])
        self.assertIn("NOT IS_NULL_VALUE(PAYLOAD:before)", statements[2])
        self.assertIn("UNION ALL", statements[3])
        self.assertIn("BEFORE_FINGERPRINT <> AFTER_FINGERPRINT", statements[3])
        self.assertIn("OPERATION IN ('c', 'u', 'u_after')", statements[3])
        self.assertIn("OPERATION = 'u_before'", statements[3])
        self.assertIn("QUALIFY ROW_NUMBER() OVER", statements[3])
        self.assertIn("WHEN MATCHED AND source.APPLY_OPERATION = 'delete' THEN DELETE", statements[3])
        self.assertIn("WHEN NOT MATCHED AND source.APPLY_OPERATION = 'upsert' THEN INSERT", statements[3])
        self.assertNotIn("DROP", " ".join(statements).upper())

    def test_source_scope_is_escaped_and_key_is_ordered_as_an_array(self) -> None:
        plan = SnowflakeBusinessMergePlan(
            scope=SITE.snowflake_scope, 
            canonical_table="CDC_CANONICAL",
            target_table="CURRENT_ROWS",
            source_library=SITE.source_schema,
            source_table="CNTR",
            key_columns=("PYPA", "PYCODE"),
        )
        merge = plan.statements_for()[3]

        self.assertIn("SHA2(TO_JSON(ARRAY_CONSTRUCT(PAYLOAD:before.PYPA, PAYLOAD:before.PYCODE)), 256)", merge)
        self.assertIn("PAYLOAD:library::VARCHAR = 'LEDGER'", merge)
        self.assertIn("PAYLOAD:table::VARCHAR = 'CNTR'", merge)

    def test_validation_fails_closed_when_multiple_receivers_need_an_order(self) -> None:
        validation = make_plan().statements_for()[2]

        self.assertIn("COUNT(DISTINCT JOURNAL_RECEIVER)", validation)
        self.assertIn("> 1", validation)

    def test_rrn_plan_rejects_deletes_of_unknown_physical_positions(self) -> None:
        # Sous identite RRN (*AFTER, ou *BOTH sans cle), un delete cite une
        # position physique. Si elle n'a jamais ete inseree ou copiee dans le
        # perimetre, une reorganisation a reecrit les positions hors flux :
        # rejouer supprimerait a tort — la validation doit refuser le lot.
        rrn_plan = SnowflakeBusinessMergePlan(
            scope=SITE.snowflake_scope,
            canonical_table="PLACE01_CDC_CANONICAL",
            target_table="PLACE01_CURRENT",
            source_library=SITE.source_schema,
            source_table="PLACE01",
            key_columns=("_rrn",),
        )
        validation = rrn_plan.statements_for()[2]

        self.assertIn("OPERATION) = 'd' AND NOT EXISTS", validation)
        self.assertIn("(known.PAYLOAD:after._rrn)::NUMBER(38, 0)", validation)
        self.assertIn("(scope_events.PAYLOAD:before._rrn)::NUMBER(38, 0)", validation)
        # La position doit avoir ete vue AVANT le delete : un _rrn qui
        # n'apparait que plus tard est une reecriture (reorganisation).
        self.assertIn(
            "known.PAYLOAD:commit_timestamp::TIMESTAMP_LTZ\n"
            "                <= scope_events.PAYLOAD:commit_timestamp::TIMESTAMP_LTZ",
            validation,
        )
        merge = rrn_plan.statements_for()[3]
        self.assertIn("SHA2(TO_JSON(ARRAY_CONSTRUCT(PAYLOAD:after._rrn)), 256)", merge)
        self.assertIn("SHA2(TO_JSON(ARRAY_CONSTRUCT(PAYLOAD:before._rrn)), 256)", merge)

    def test_business_key_plan_does_not_carry_the_rrn_delete_guard(self) -> None:
        # Le garde n'a de sens que pour l'identite physique : une cle metier
        # cite ses propres valeurs, l'anti-join _rrn serait inapplicable.
        validation = make_plan().statements_for()[2]

        self.assertNotIn("before._rrn", validation)
        self.assertNotIn("NOT EXISTS", validation)

    def test_execute_does_not_merge_after_validation_failure(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.executed: list[str] = []

            def execute(self, statement: str) -> None:
                self.executed.append(statement)

            def fetchone(self) -> tuple[int]:
                return (1,)

        cursor = Cursor()
        with self.assertRaisesRegex(ValueError, "rejected 1 events"):
            make_plan().execute(cursor)

        self.assertEqual(len(cursor.executed), 3)
        self.assertNotIn("MERGE INTO", cursor.executed[-1])

    def test_execute_merges_only_after_zero_invalid_events(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.executed: list[str] = []

            def execute(self, statement: str) -> None:
                self.executed.append(statement)

            def fetchone(self) -> tuple[int]:
                return (0,)

        cursor = Cursor()
        make_plan().execute(cursor)

        self.assertEqual(len(cursor.executed), 4)
        self.assertIn("MERGE INTO", cursor.executed[-1])


if __name__ == "__main__":
    unittest.main()
