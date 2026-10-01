/**
 * Site fictif partagé par les tests frontend — miroir de
 * tests/site_fixture.py côté backend. Aucune valeur n'appartient à une
 * installation réelle : le site « acme » vérifie que l'interface résout son
 * périmètre depuis l'identité publiée par le service, jamais depuis des
 * défauts du code. Le manifeste reprend les noms de tables des jeux de
 * données capturés — données d'entrée, pas identité de déploiement.
 */

import { installSiteIdentity, type SiteIdentity } from './siteIdentity.ts';

export const TEST_SITE: SiteIdentity = {
  siteId: 'acme',
  fleetId: 'acme-test',
  environment: 'TEST',
  runtimeEnvironment: 'test',
  destinationDatabase: 'ACME_RAW',
  destinationSchema: 'IBMI_TEST',
  destinationNamespace: 'ACME_RAW.IBMI_TEST',
  sourceSchema: 'LEDGER',
  journalName: 'TRNJRN',
  manifest: [
    'ADDRS1',
    'CAL001',
    'COST1',
    'CUSTOM1',
    'ORDER',
    'EXPENS',
    'DATE01',
    'SALE',
    'PLACE01',
    'PLACES',
    'CNTR',
    'PRODUCT',
    'HOLIDAYS',
  ],
  proofTable: 'SALE',
  runtimePipelineId: 'acme',
  ibmiHost: 'ibmi.acme.invalid',
  ibmiUser: 'CDCAPP',
  tlsCaFile: '/app/certs/ibmi-test-ca.pem',
  secretRefName: 'acme-test-ibmi',
  secretRefKey: 'ISERIES_PASSWORD',
  snowflakeStage: 'IBMI_TEST_SALE_EXTERNAL_STAGE',
};

/** Installe le site fictif comme identité courante pour la durée du test. */
export function installTestSite(): SiteIdentity {
  installSiteIdentity(TEST_SITE);
  return TEST_SITE;
}

/** Forme publiée par le service (bloc ``site`` de /v1/onboarding/defaults). */
export const TEST_SITE_WIRE = {
  site_id: TEST_SITE.siteId,
  fleet_id: TEST_SITE.fleetId,
  environment: TEST_SITE.environment,
  runtime_environment: TEST_SITE.runtimeEnvironment,
  destination_database: TEST_SITE.destinationDatabase,
  destination_schema: TEST_SITE.destinationSchema,
  destination_namespace: TEST_SITE.destinationNamespace,
  source_schema: TEST_SITE.sourceSchema,
  journal_name: TEST_SITE.journalName,
  tables: [...TEST_SITE.manifest],
  proof_table: TEST_SITE.proofTable,
  runtime_pipeline_id: TEST_SITE.runtimePipelineId,
  ibmi_host: TEST_SITE.ibmiHost,
  ibmi_user: TEST_SITE.ibmiUser,
  tls_ca_file: TEST_SITE.tlsCaFile,
  secret_ref_name: TEST_SITE.secretRefName,
  secret_ref_key: TEST_SITE.secretRefKey,
  snowflake_stage: TEST_SITE.snowflakeStage,
} as const;
