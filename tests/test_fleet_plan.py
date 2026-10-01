from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from quadringent_control_plane.fleet import (
    DESTINATION_NAMESPACE,
    ENVIRONMENT,
    MANIFEST,
    FleetError,
)
from quadringent_control_plane.fleet_plan import (
    CATALOG_FORMAT_VERSION,
    PLAN_FORMAT_VERSION,
    SOURCE_SCHEMA,
    build_fleet_plan,
    deserialize_fleet_plan,
    parse_fleet_catalog,
    serialize_fleet_plan,
    table_merge_key,
)

OBSERVED_CATALOG_PATH = Path("/tmp/quadringent-fleet-catalog-20260913.json")
OBSERVED_CATALOG_BYTES = 105207
OBSERVED_ROW_COUNT = 218150587
OBSERVED_DATA_SIZE = 46347608064
KEYED = {
    "ADDRS1": ("B2CADR",),
    "CAL001": ("CADATE", "CATYL", "CACDL", "CAPHASE", "CAEXPL"),
    "CUSTOM1": ("CH1PER", "CH1CH", "CH1CE", "CH1PT", "CH1NCPT", "CH1DA"),
    "ORDER": ("COCH", "COCE", "COCS", "COCOL", "COPT", "CODISC"),
    "SALE": ("SDOM", "SCOD", "SSEQ", "SORD", "STYP", "SDISC", "SDATE"),
}
# Seule SALE est journalisée *BOTH avec une clé : c'est la seule identité
# métier du catalogue. Toutes les tables *AFTER basculent sur l'identité
# physique (RRN) : un delete *AFTER n'a pas d'image, il ne cite que la
# position de la ligne.
KEYED_TABLES = ("SALE",)
RRN_TABLES = tuple(name for name in (
    "ADDRS1", "CAL001", "COST1", "CUSTOM1", "ORDER", "EXPENS", "DATE01",
    "SALE", "PLACE01", "PLACES", "CNTR", "PRODUCT", "HOLIDAYS",
) if name != "SALE")
BLOCKED = ()
TABLE_VOLUMES = {
    "PRODUCT": (32, 100),
    "CNTR": (210, 200),
    "PLACES": (1180, 300),
    "DATE01": (2896, 400),
    "PLACE01": (6722, 500),
    "COST1": (62834, 600),
    "HOLIDAYS": (67037, 700),
    "EXPENS": (111223, 800),
    "ORDER": (4422050, 900),
    "CAL001": (6272019, 1000),
    "CUSTOM1": (56796625, 1100),
    "SALE": (68758364, 1200),
    "ADDRS1": (81649395, 1300),
}
SMALL_FIRST = tuple(name for name, _volume in sorted(TABLE_VOLUMES.items(), key=lambda item: (item[1][0], item[1][1], item[0])))
LANES_CONCURRENCY_4 = (
    ("PRODUCT", "PLACE01", "ORDER", "ADDRS1"),
    ("CNTR", "COST1", "CAL001"),
    ("PLACES", "HOLIDAYS", "CUSTOM1"),
    ("DATE01", "EXPENS", "SALE"),
)


def _column(name: str, ordinal: int, *, nullable: bool = False) -> dict[str, object]:
    return {
        "name": name,
        "type": "CHAR",
        "length": 10,
        "numeric_precision": None,
        "numeric_scale": None,
        "ccsid": None,
        "nullable": nullable,
        "ordinal": ordinal,
    }


def _index(name: str, columns: tuple[str, ...], *, unique: bool = True, sparse: bool = False) -> dict[str, object]:
    return {
        "schema": SOURCE_SCHEMA,
        "name": name,
        "unique": unique,
        "sparse": sparse,
        "select_omit": None,
        "columns": [{"name": column, "ordinal": index + 1, "ordering": "A"} for index, column in enumerate(columns)],
    }


def _constraint(name: str, columns: tuple[str, ...]) -> dict[str, object]:
    return {
        "schema": SOURCE_SCHEMA,
        "name": name,
        "type": "UNIQUE",
        "columns": list(columns),
    }


def _identity_payload(name: str) -> tuple[list[str], list[dict[str, object]], list[dict[str, object]]]:
    if name == "ADDRS1":
        return list(KEYED[name]), [], [_index("ADRB2BL1", KEYED[name])]
    if name == "CAL001":
        return list(KEYED[name]), [_constraint("Q_ALPHAFP_CAL001_CADATE_00001", KEYED[name])], [_index("CALENDL1", KEYED[name])]
    if name == "CUSTOM1":
        return list(KEYED[name]), [], [
            _index("CMPTH01L1", KEYED[name]),
            _index("CMPTH01L3", ("CH1NCPT", "CH1PER", "CH1DA", "CH1CH", "CH1CE", "CH1PT")),
        ]
    if name == "ORDER":
        return [*KEYED[name], "COCNDT"], [], [
            _index("COLISL0", KEYED[name]),
            _index("COLISL34", ("COCNDT", "COCH"), unique=False, sparse=True),
        ]
    if name == "SALE":
        return [*KEYED[name], "EVTYEV"], [], [
            _index("EVNTL8", ("EVTYEV",), unique=False),
            _index("SALE_UNIQUE", KEYED[name]),
        ]
    return ["COL1"], [], []


def _table_payload(
    name: str,
    *,
    journal_library: str,
    journal_name: str,
) -> dict[str, object]:
    row_count, data_size = TABLE_VOLUMES[name]
    column_names, constraints, indexes = _identity_payload(name)
    return {
        "name": name,
        "row_count": row_count,
        "data_size": data_size,
        "member_count": 1,
        "journal_library": journal_library,
        "journal_name": journal_name,
        "journal_images": "*BOTH" if name == "SALE" else "*AFTER",
        "columns": [
            _column(column_name, ordinal, nullable=name == "CAL001")
            for ordinal, column_name in enumerate(column_names, start=1)
        ],
        "constraints": constraints,
        "indexes": indexes,
    }


def catalog_payload(
    *,
    journal_library: str = "DEMOLIB",
    journal_name: str = "DEMOJRN",
    attached_name: str = "DEMOJRN0100",
    attached_tail: int = 99,
    continuity: str = "uncertain",
) -> dict[str, object]:
    return {
        "format_version": CATALOG_FORMAT_VERSION,
        "observed_at": "2026-09-13T12:00:00Z",
        "environment": ENVIRONMENT,
        "source_schema": SOURCE_SCHEMA,
        "journals": [
            {
                "library": journal_library,
                "name": journal_name,
                "continuity": continuity,
                "receivers": [
                    {
                        "library": journal_library,
                        "name": "DEMOJRN0099",
                        "status": "ONLINE",
                        "first_sequence": "1",
                        "last_sequence": "50",
                        "attach_timestamp": "2026-09-13T00:00:00.000000",
                        "detach_timestamp": "2026-09-13T01:00:00.000000",
                        "previous_library": None,
                        "previous_name": None,
                    },
                    {
                        "library": journal_library,
                        "name": attached_name,
                        "status": "ATTACHED",
                        "first_sequence": "1",
                        "last_sequence": str(attached_tail),
                        "attach_timestamp": "2026-09-13T01:00:00.000000",
                        "detach_timestamp": None,
                        "previous_library": None,
                        "previous_name": None,
                    },
                ],
            }
        ],
        "tables": [_table_payload(name, journal_library=journal_library, journal_name=journal_name) for name in MANIFEST],
    }


def parse_catalog(**kwargs):
    return parse_fleet_catalog(catalog_payload(**kwargs))


def build_plan(payload: dict[str, object] | None = None, **kwargs):
    catalog = parse_fleet_catalog(payload if payload is not None else catalog_payload())
    return build_fleet_plan(catalog, **kwargs)


def observed_totals(payload: dict[str, object]) -> tuple[int, int]:
    tables = payload["tables"]
    return sum(table["row_count"] for table in tables), sum(table["data_size"] for table in tables)


def assert_json_safe(test: unittest.TestCase, value: object) -> None:
    if value is None or type(value) in (str, int, float, bool):
        return
    if type(value) is list:
        for item in value:
            assert_json_safe(test, item)
        return
    if type(value) is dict:
        for key, item in value.items():
            test.assertIs(type(key), str)
            lowered = key.lower()
            test.assertFalse(any(token in lowered for token in ("host", "user", "password", "secret", "token", "credit")))
            assert_json_safe(test, item)
        return
    test.fail(f"type JSON non autorisé: {type(value)!r}")


def assert_safe_error(test: unittest.TestCase, error: FleetError) -> None:
    message = error.safe_message.lower()
    test.assertNotIn("host", message)
    test.assertNotIn("user", message)
    test.assertNotIn("password", message)
    test.assertNotIn("@", message)


class SyntheticCatalogContractTests(unittest.TestCase):
    def test_builder_is_metadata_only_and_covers_the_manifest(self) -> None:
        payload = catalog_payload()
        self.assertEqual(payload["format_version"], CATALOG_FORMAT_VERSION)
        self.assertEqual(tuple(table["name"] for table in payload["tables"]), MANIFEST)
        encoded = json.dumps(payload)
        self.assertNotIn("INSERT", encoded)
        self.assertNotIn("sample_rows", encoded)
        for table in payload["tables"]:
            self.assertNotIn("rows", table)
            self.assertNotIn("sample", table)
            self.assertNotIn("data", table)

    def test_parser_derives_totals_from_the_thirteen_tables(self) -> None:
        payload = catalog_payload()
        catalog = parse_fleet_catalog(payload)
        rows, size = observed_totals(payload)
        self.assertEqual(catalog.observed_row_count, rows)
        self.assertEqual(catalog.observed_data_size, size)
        mutated = copy.deepcopy(payload)
        mutated["tables"][0]["row_count"] += 1
        updated = parse_fleet_catalog(mutated)
        self.assertEqual(updated.observed_row_count, rows + 1)


class IdentityAndContinuityTests(unittest.TestCase):
    def test_one_keyed_table_twelve_rrn_and_zero_blocked(self) -> None:
        plan = build_plan()
        keyed = [table for table in plan.tables if table.identity_status == "keyed"]
        rrn = [table for table in plan.tables if table.identity_status == "rrn"]
        blocked = [table for table in plan.tables if table.identity_status == "blocked"]
        self.assertEqual(tuple(table.name for table in keyed), KEYED_TABLES)
        self.assertEqual(tuple(table.name for table in rrn), RRN_TABLES)
        self.assertEqual(blocked, [])
        sale = plan.tables[MANIFEST.index("SALE")]
        self.assertEqual(sale.candidate_key, KEYED["SALE"])
        self.assertEqual(sale.identity_source, "unique_index")
        for name in RRN_TABLES:
            table = plan.tables[MANIFEST.index(name)]
            self.assertEqual(table.candidate_key, ("_rrn",))
            self.assertEqual(table.identity_source, "journal_rrn")

    def test_after_journal_uses_rrn_identity_even_with_a_unique_index(self) -> None:
        # ADDRS1 possede un index unique, mais un delete *AFTER ne cite que la
        # position physique : la cle metier ne peut pas adresser un delete.
        table = build_plan().tables[MANIFEST.index("ADDRS1")]
        self.assertEqual(table.identity_status, "rrn")
        self.assertEqual(table.candidate_key, ("_rrn",))
        self.assertEqual(table.identity_source, "journal_rrn")

    def test_both_without_unique_key_falls_back_to_rrn(self) -> None:
        payload = catalog_payload()
        payload["tables"][MANIFEST.index("CNTR")]["journal_images"] = "*BOTH"
        table = build_plan(payload).tables[MANIFEST.index("CNTR")]
        self.assertEqual(table.identity_status, "rrn")
        self.assertEqual(table.candidate_key, ("_rrn",))
        self.assertEqual(table.identity_source, "journal_rrn")

    def test_before_journal_stays_blocked(self) -> None:
        # *BEFORE n'a que l'ancien etat : un update n'est pas reconstructible,
        # quel que soit le mode d'identite.
        payload = catalog_payload()
        payload["tables"][MANIFEST.index("CNTR")]["journal_images"] = "*BEFORE"
        plan = build_plan(payload)
        table = plan.tables[MANIFEST.index("CNTR")]
        self.assertEqual(table.identity_status, "blocked")
        self.assertIsNone(table.candidate_key)
        self.assertIsNone(table.identity_source)
        self.assertIn("unsupported_journal_images", table.blocked_reasons)
        self.assertFalse(table.live_possible)
        self.assertFalse(table.certification_possible)

    def test_table_merge_key_returns_the_declared_plan_key(self) -> None:
        # Les scripts de rejeu doivent fusionner sur la cle du plan : metier
        # sous *BOTH, position physique sous *AFTER ou sans cle metier.
        catalog = parse_catalog()
        self.assertEqual(table_merge_key(catalog, "SALE"), KEYED["SALE"])
        self.assertEqual(table_merge_key(catalog, "ADDRS1"), ("_rrn",))
        self.assertEqual(table_merge_key(catalog, "PLACE01"), ("_rrn",))

    def test_table_merge_key_fails_closed_for_unknown_or_blocked_tables(self) -> None:
        catalog = parse_catalog()
        with self.assertRaises(FleetError):
            table_merge_key(catalog, "INCONNUE")
        payload = catalog_payload()
        payload["tables"][MANIFEST.index("CNTR")]["journal_images"] = "*BEFORE"
        with self.assertRaises(FleetError):
            table_merge_key(parse_fleet_catalog(payload), "CNTR")

    def test_cal001_prefers_unique_constraint_over_index(self) -> None:
        payload = catalog_payload()
        payload["tables"][MANIFEST.index("CAL001")]["journal_images"] = "*BOTH"
        table = build_plan(payload).tables[MANIFEST.index("CAL001")]
        self.assertEqual(table.identity_status, "keyed")
        self.assertEqual(table.identity_source, "unique_constraint")
        self.assertEqual(table.candidate_key, KEYED["CAL001"])

    def test_custom1_picks_the_first_unique_index_by_name(self) -> None:
        payload = catalog_payload()
        payload["tables"][MANIFEST.index("CUSTOM1")]["journal_images"] = "*BOTH"
        table = build_plan(payload).tables[MANIFEST.index("CUSTOM1")]
        self.assertEqual(table.identity_status, "keyed")
        self.assertEqual(table.identity_source, "unique_index")
        self.assertEqual(table.candidate_key, KEYED["CUSTOM1"])

    def test_uncertain_continuity_blocks_live_and_certified_for_every_table(self) -> None:
        plan = build_plan()
        self.assertEqual(plan.continuity, "uncertain")
        self.assertTrue(plan.live_blocked)
        self.assertTrue(plan.certification_blocked)
        self.assertTrue(plan.cutover_required_before_history)
        for table in plan.tables:
            self.assertFalse(table.live_possible)
            self.assertFalse(table.certification_possible)
            self.assertIn("uncertain_continuity", table.blocked_reasons)

    def test_journal_images_are_preserved(self) -> None:
        plan = build_plan()
        self.assertEqual(plan.tables[MANIFEST.index("SALE")].journal_images, "*BOTH")
        for name in MANIFEST:
            if name == "SALE":
                continue
            self.assertEqual(plan.tables[MANIFEST.index(name)].journal_images, "*AFTER")


class JournalAndCutoverTests(unittest.TestCase):
    def test_single_multi_object_journal_group_never_thirteen_readers(self) -> None:
        plan = build_plan(catalog_payload(journal_library="JRNLIBX", journal_name="APPJRN"))
        self.assertEqual(len(plan.journal_groups), 1)
        group = plan.journal_groups[0]
        self.assertEqual(group.library, "JRNLIBX")
        self.assertEqual(group.name, "APPJRN")
        self.assertEqual(group.reader_kind, "multi_object")
        self.assertEqual(group.reader_count, 1)
        self.assertEqual(group.table_names, MANIFEST)
        payload = serialize_fleet_plan(plan)
        self.assertEqual(len(payload["journal_groups"]), 1)
        self.assertEqual(payload["journal_groups"][0]["reader_count"], 1)

    def test_shared_cutover_is_derived_from_unique_attached_tail(self) -> None:
        payload = catalog_payload(attached_name="DEMOJRN7777", attached_tail=321)
        plan = build_plan(payload)
        self.assertEqual(plan.cutover_checkpoint.receiver, "DEMOJRN7777")
        self.assertEqual(plan.cutover_checkpoint.sequence, 321)
        self.assertIs(plan.cutover_required_before_history, True)
        mutated = serialize_fleet_plan(plan)
        mutated["cutover_required_before_history"] = False
        with self.assertRaises(FleetError) as raised:
            deserialize_fleet_plan(mutated)
        self.assertEqual(raised.exception.code, "missing_start_checkpoint")
        assert_safe_error(self, raised.exception)

    def test_cutover_requires_exactly_one_attached_receiver(self) -> None:
        missing = catalog_payload()
        missing["journals"][0]["receivers"][-1]["status"] = "ONLINE"
        with self.assertRaises(FleetError) as absent:
            parse_fleet_catalog(missing)
        self.assertEqual(absent.exception.code, "invalid_checkpoint")
        duplicated = catalog_payload()
        extra = copy.deepcopy(duplicated["journals"][0]["receivers"][-1])
        extra["name"] = "DEMOJRN0101"
        duplicated["journals"][0]["receivers"].append(extra)
        with self.assertRaises(FleetError) as many:
            parse_fleet_catalog(duplicated)
        self.assertEqual(many.exception.code, "invalid_checkpoint")
        assert_safe_error(self, absent.exception)
        assert_safe_error(self, many.exception)


class LaneConcurrencyAndBudgetTests(unittest.TestCase):
    def test_concurrency_four_uses_small_tables_first_then_large(self) -> None:
        plan = build_plan(max_concurrency=4)
        self.assertEqual(plan.max_concurrency, 4)
        self.assertEqual(len(plan.historical_lanes), 4)
        self.assertEqual(tuple(lane.tables for lane in plan.historical_lanes), LANES_CONCURRENCY_4)
        assigned = [name for lane in plan.historical_lanes for name in lane.tables]
        self.assertEqual(sorted(assigned, key=SMALL_FIRST.index), list(SMALL_FIRST))
        self.assertEqual(len(assigned), 13)

    def test_concurrency_one_keeps_deterministic_small_first_order(self) -> None:
        payload = catalog_payload()
        rows, size = observed_totals(payload)
        plan = build_plan(payload, max_concurrency=1)
        self.assertEqual(len(plan.historical_lanes), 1)
        self.assertEqual(plan.historical_lanes[0].tables, SMALL_FIRST)
        self.assertEqual(plan.historical_lanes[0].row_count, rows)
        self.assertEqual(plan.historical_lanes[0].data_size, size)

    def test_concurrency_bounds_are_fail_closed(self) -> None:
        catalog = parse_catalog()
        with self.assertRaises(FleetError) as zero:
            build_fleet_plan(catalog, max_concurrency=0)
        self.assertEqual(zero.exception.code, "invalid_concurrency")
        with self.assertRaises(FleetError):
            build_fleet_plan(catalog, max_concurrency=5)
        with self.assertRaises(FleetError):
            build_fleet_plan(catalog, max_concurrency=True)  # type: ignore[arg-type]
        assert_safe_error(self, zero.exception)

    def test_byte_budget_uses_observed_data_size_not_invented_cost(self) -> None:
        payload = catalog_payload()
        produit_size = TABLE_VOLUMES["PRODUCT"][1]
        plan = build_plan(payload, max_concurrency=4, historical_byte_budget=produit_size)
        self.assertEqual(len(plan.historical_lanes), 1)
        self.assertEqual(plan.historical_lanes[0].tables, ("PRODUCT",))
        self.assertEqual(plan.tables[MANIFEST.index("PRODUCT")].historical_lane, 1)
        self.assertIsNone(plan.tables[MANIFEST.index("CNTR")].historical_lane)
        self.assertIn("byte_budget_excluded", plan.tables[MANIFEST.index("CNTR")].blocked_reasons)
        self.assertLessEqual(
            sum(table.data_size for table in plan.tables if table.historical_lane is not None),
            produit_size,
        )
        serialized = serialize_fleet_plan(plan)
        self.assertNotIn("credit", json.dumps(serialized).lower())
        empty = build_plan(payload, historical_byte_budget=produit_size - 1)
        self.assertEqual(empty.historical_lanes, ())
        self.assertTrue(all(table.historical_lane is None for table in empty.tables))


class DeterminismAndSerializationTests(unittest.TestCase):
    def test_plan_is_deterministic_and_json_roundtrip_closed(self) -> None:
        payload = catalog_payload()
        rows, size = observed_totals(payload)
        first = serialize_fleet_plan(build_plan(payload, max_concurrency=4))
        second = serialize_fleet_plan(build_plan(payload, max_concurrency=4))
        self.assertEqual(first, second)
        assert_json_safe(self, first)
        self.assertEqual(first["format_version"], PLAN_FORMAT_VERSION)
        self.assertEqual(first["environment"], ENVIRONMENT)
        self.assertEqual(first["source_schema"], SOURCE_SCHEMA)
        self.assertEqual(first["destination_namespace"], DESTINATION_NAMESPACE)
        self.assertEqual(first["observed_row_count"], rows)
        self.assertEqual(first["observed_data_size"], size)
        self.assertIsNone(first["historical_byte_budget"])
        restored = deserialize_fleet_plan(json.loads(json.dumps(first)))
        self.assertEqual(serialize_fleet_plan(restored), first)
        extra = json.loads(json.dumps(first))
        extra["unexpected"] = True
        with self.assertRaises(FleetError):
            deserialize_fleet_plan(extra)

    def test_observed_lane_volumes_match_catalog_sizes(self) -> None:
        payload = catalog_payload()
        sizes = {table["name"]: (table["row_count"], table["data_size"]) for table in payload["tables"]}
        plan = build_plan(payload, max_concurrency=4)
        for lane in plan.historical_lanes:
            self.assertEqual(lane.row_count, sum(sizes[name][0] for name in lane.tables))
            self.assertEqual(lane.data_size, sum(sizes[name][1] for name in lane.tables))


class FailClosedAlterationTests(unittest.TestCase):
    def test_unknown_fields_bool_as_int_and_duplicates_are_rejected(self) -> None:
        payload = catalog_payload()
        unknown = copy.deepcopy(payload)
        unknown["extra"] = 1
        with self.assertRaises(FleetError) as extra:
            parse_fleet_catalog(unknown)
        self.assertEqual(extra.exception.code, "invalid_catalog")

        bool_as_int = copy.deepcopy(payload)
        bool_as_int["tables"][0]["row_count"] = True
        with self.assertRaises(FleetError) as counted:
            parse_fleet_catalog(bool_as_int)
        self.assertEqual(counted.exception.code, "invalid_catalog")

        unique_as_int = copy.deepcopy(payload)
        unique_as_int["tables"][0]["indexes"][0]["unique"] = 1
        with self.assertRaises(FleetError) as uniqueness:
            parse_fleet_catalog(unique_as_int)
        self.assertEqual(uniqueness.exception.code, "invalid_identity")

        duplicated = copy.deepcopy(payload)
        duplicated["tables"] = list(duplicated["tables"]) + [copy.deepcopy(duplicated["tables"][0])]
        with self.assertRaises(FleetError) as duplicate:
            parse_fleet_catalog(duplicated)
        self.assertEqual(duplicate.exception.code, "duplicate_table")

        table_unknown = copy.deepcopy(payload)
        table_unknown["tables"][0]["host"] = "example.invalid"
        with self.assertRaises(FleetError):
            parse_fleet_catalog(table_unknown)

        for error in (extra.exception, counted.exception, uniqueness.exception, duplicate.exception):
            assert_safe_error(self, error)

    def test_environment_source_and_journal_splits_fail_closed(self) -> None:
        payload = catalog_payload()
        prod = copy.deepcopy(payload)
        prod["environment"] = "PROD"
        with self.assertRaises(FleetError) as env:
            parse_fleet_catalog(prod)
        self.assertEqual(env.exception.code, "invalid_environment")

        source = copy.deepcopy(payload)
        source["source_schema"] = "OTHER"
        with self.assertRaises(FleetError) as schema:
            parse_fleet_catalog(source)
        self.assertEqual(schema.exception.code, "invalid_source")

        split = copy.deepcopy(payload)
        split["tables"][-1]["journal_name"] = "OTHERJRN"
        with self.assertRaises(FleetError) as journal:
            parse_fleet_catalog(split)
        self.assertEqual(journal.exception.code, "invalid_journal")

        for error in (env.exception, schema.exception, journal.exception):
            assert_safe_error(self, error)


@unittest.skipUnless(
    OBSERVED_CATALOG_PATH.is_file(),
    "catalogue observé /tmp/quadringent-fleet-catalog-20260913.json absent",
)
class ObservedCatalogProofTests(unittest.TestCase):
    def test_observed_catalog_totals_are_derived_not_required_by_parser(self) -> None:
        self.assertEqual(OBSERVED_CATALOG_PATH.stat().st_size, OBSERVED_CATALOG_BYTES)
        payload = json.loads(OBSERVED_CATALOG_PATH.read_text(encoding="utf-8"))
        catalog = parse_fleet_catalog(payload)
        self.assertEqual(catalog.observed_row_count, OBSERVED_ROW_COUNT)
        self.assertEqual(catalog.observed_data_size, OBSERVED_DATA_SIZE)
        plan = build_fleet_plan(catalog)
        attached = [receiver for receiver in catalog.journals[0].receivers if receiver.status == "ATTACHED"]
        self.assertEqual(len(attached), 1)
        self.assertEqual(plan.cutover_checkpoint.receiver, attached[0].name)
        self.assertEqual(plan.cutover_checkpoint.sequence, attached[0].last_sequence)
        self.assertEqual(plan.observed_row_count, OBSERVED_ROW_COUNT)
        self.assertEqual(plan.observed_data_size, OBSERVED_DATA_SIZE)
