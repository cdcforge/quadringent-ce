-- Quadringent R&D — DEV/INT uniquement.
-- Conserver le préfixe CNTR existant et ajouter SALE sans recréer
-- l'intégration, afin de préserver la relation de confiance AWS.

USE ROLE ACCOUNTADMIN;

ALTER STORAGE INTEGRATION DEV_AS400_RD_S3_INT SET
  STORAGE_ALLOWED_LOCATIONS = (
    's3://example-corp-000000000000-int-example-corp-raw/as400/sales/cntr/',
    's3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/'
  );

CREATE STAGE IF NOT EXISTS DEV_RAW.AS400_RD.AS400_RD_SALE_EXTERNAL_STAGE
  URL = 's3://example-corp-000000000000-int-example-corp-raw/as400/sales/sale/'
  STORAGE_INTEGRATION = DEV_AS400_RD_S3_INT
  FILE_FORMAT = (TYPE = JSON);
