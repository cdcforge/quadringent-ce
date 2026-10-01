"""Un warehouse partagé reste sous le contrôle de son propriétaire."""

from dataclasses import replace

import pytest
import site_fixture
from test_slo_telemetry import (
    NOW,
    WINDOW_END,
    WINDOW_START,
    CloudWatchClient,
    S3Client,
    SnowflakeCursor,
)
from test_snowflake_autonomous import ProvisioningCursor

from quadringent.fleet_destination import FleetDestinationPlan
from quadringent.site_config import SiteConfigurationError, from_environment
from quadringent.slo_telemetry import collect_slo_telemetry
from quadringent.snowflake_autonomous import (
    autonomous_plan,
    pause_autonomous_destination,
    provision_autonomous_destination,
)


def shared_site():
    return from_environment(
        {
            **site_fixture.TEST_SITE_ENV,
            "QUADRINGENT_SNOWFLAKE_WAREHOUSE": "shared_ingestion_wh",
        }
    )


def test_shared_warehouse_keeps_the_existing_data_object_names():
    site = shared_site()
    original = site_fixture.build_test_site()
    assert site.warehouse_name == "SHARED_INGESTION_WH"
    assert not site.manages_warehouse
    assert original.manages_warehouse
    assert site.proof_canonical_table == original.proof_canonical_table
    assert site.verifier_role_name == original.verifier_role_name


@pytest.mark.parametrize("name", ["BAD-WH", "db.wh", "WH;DROP", "x" * 64, " "])
def test_shared_warehouse_rejects_invalid_identifiers(name):
    with pytest.raises(SiteConfigurationError):
        from_environment(
            {
                **site_fixture.TEST_SITE_ENV,
                "QUADRINGENT_SNOWFLAKE_WAREHOUSE": name,
            }
        )


def test_shared_warehouse_is_neither_created_nor_suspended():
    site = shared_site()
    plan = autonomous_plan(site)
    assert plan.warehouse == "SHARED_INGESTION_WH"
    assert all("WAREHOUSE" not in sql for sql in plan.statements())
    assert all("WAREHOUSE" not in sql for sql in plan.pause_statements())
    fleet = FleetDestinationPlan(tables=site.fleet_tables, site=site)
    assert fleet.suspend_warehouse_statement() is None
    assert all("WAREHOUSE" not in sql for sql in fleet.pause_statements())


@pytest.mark.parametrize("legacy", [False, True])
def test_shared_warehouse_preserves_provisioning_and_legacy_migration_order(legacy):
    site = shared_site()
    cursor = ProvisioningCursor(legacy_dynamic_table=legacy)
    result = provision_autonomous_destination(cursor, autonomous_plan(site), site=site)
    assert result["status"] == "READY_FOR_S3_NOTIFICATION"
    assert not any("WAREHOUSE" in sql for sql in cursor.executed)
    if legacy:
        drop = next(
            i for i, sql in enumerate(cursor.executed) if sql.startswith("DROP DYNAMIC")
        )
        create = next(
            i
            for i, sql in enumerate(cursor.executed)
            if sql.startswith("CREATE OR REPLACE VIEW")
        )
        assert drop < create


def test_shared_warehouse_rollback_does_not_suspend_other_consumers():
    site = shared_site()
    cursor = ProvisioningCursor(fail_prefix="CREATE OR REPLACE VIEW")
    with pytest.raises(RuntimeError):
        provision_autonomous_destination(cursor, autonomous_plan(site), site=site)
    assert any("PIPE_EXECUTION_PAUSED = TRUE" in sql for sql in cursor.executed)
    assert not any("WAREHOUSE" in sql for sql in cursor.executed)


@pytest.mark.parametrize(
    "action", [provision_autonomous_destination, pause_autonomous_destination]
)
def test_a_plan_cannot_claim_ownership_of_the_shared_warehouse(action):
    site = shared_site()
    cursor = ProvisioningCursor()
    with pytest.raises(ValueError):
        action(cursor, replace(autonomous_plan(site), manage_warehouse=True), site=site)
    assert cursor.executed == []


def test_shared_warehouse_total_is_not_reported_as_product_cost():
    class Cursor(SnowflakeCursor):
        def __init__(self):
            super().__init__()
            self.queries = []

        def execute(self, sql, params=None):
            self.queries.append(sql)
            super().execute(sql, params)

    cursor = Cursor()
    telemetry = collect_slo_telemetry(
        S3Client(),
        CloudWatchClient(),
        cursor,
        site=shared_site(),
        now=NOW,
        window_started_at=WINDOW_START,
        window_ended_at=WINDOW_END,
    )
    assert "snowflake_credits_24h" not in telemetry
    assert "snowflake_credits_window" not in telemetry
    assert not any("METERING" in sql for sql in cursor.queries)
    assert {
        "source": "snowflake_credits",
        "error_type": "SharedWarehouseAttributionRequired",
    } in telemetry["collection_errors"]
    assert telemetry["delivery_latency_p95_seconds"] == 12.5
