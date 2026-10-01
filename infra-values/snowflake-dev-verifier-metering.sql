-- DEV only. Execute with an authorized view owner; never grant account-wide
-- metering access to QUADRINGENT_DEV_VERIFIER_ROLE. Intentionally no OR REPLACE.
-- Data is delayed (up to 6h including cloud services), not a total product bill.
CREATE SECURE VIEW DEV_RAW.AS400_RD.QUADRINGENT_DEV_WH_METERING
COMMENT = 'Forge DEV warehouse only; delayed hourly credits, not total billed cost; absent rows are unknown, not zero'
AS SELECT START_TIME, END_TIME, WAREHOUSE_NAME,
          CREDITS_USED_COMPUTE, CREDITS_USED_CLOUD_SERVICES, CREDITS_USED
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE WAREHOUSE_NAME = 'QUADRINGENT_DEV_WH';

GRANT SELECT ON VIEW DEV_RAW.AS400_RD.QUADRINGENT_DEV_WH_METERING
TO ROLE QUADRINGENT_DEV_VERIFIER_ROLE;

-- Rollback: revoke this SELECT, then drop only this newly created view after
-- confirming its definition and ownership still match this deployment.
