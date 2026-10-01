/**
 * Identité du site déclaré — fournie par GET /v1/onboarding/defaults.
 *
 * Aucune valeur d'installation n'est codée dans l'interface : les parseurs
 * comparent chaque document de flotte à l'identité publiée par le service.
 * Sans identité installée, tout document est refusé — le produit n'a aucun
 * site implicite.
 */

export interface SiteIdentity {
  readonly siteId: string;
  readonly fleetId: string;
  /** Environnement publié dans les documents de flotte (ex. « DEV »). */
  readonly environment: string;
  /** Environnement runtime déclaré (ex. « dev »). */
  readonly runtimeEnvironment: string;
  readonly destinationDatabase: string;
  readonly destinationSchema: string;
  readonly destinationNamespace: string;
  readonly sourceSchema: string;
  readonly journalName: string;
  readonly manifest: readonly string[];
  readonly proofTable: string;
  readonly runtimePipelineId: string;
  readonly ibmiHost: string;
  readonly ibmiUser: string;
  readonly tlsCaFile: string;
  readonly secretRefName: string;
  readonly secretRefKey: string;
  readonly snowflakeStage: string;
}

export class SiteIdentityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SiteIdentityError';
  }
}

const SITE_ID_PATTERN = /^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$/;
const FLEET_TOKEN_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const IBM_I_IDENTIFIER = /^[A-Z][A-Z0-9_]{0,29}$/;
const SNOWFLAKE_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_$]{0,62}$/;
const TLS_CA_FILE = /^\/[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$/;
const IBMI_HOST = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/;
const K8S_NAME = /^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$/;
const SECRET_KEY = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const BOUNDED_TEXT = /^[^\s][^\s]{0,252}$/;
const MAX_MANIFEST = 64;

let installed: SiteIdentity | null = null;

export function installSiteIdentity(identity: SiteIdentity): void {
  installed = identity;
}

/** Retire l'identité installée — réservé aux tests. */
export function resetSiteIdentity(): void {
  installed = null;
}

export function installedSiteIdentity(): SiteIdentity | null {
  return installed;
}

/**
 * Identité installée ou refus explicite. Les parseurs ne doivent jamais
 * supposer une valeur : l'absence d'identité est une erreur, pas un défaut.
 */
export function siteIdentity(): SiteIdentity {
  if (!installed) throw new SiteIdentityError('Identité de site non publiée par le service');
  return installed;
}

/** Valide le bloc ``site`` publié par le service ; refuse tout champ absent. */
export function parseSiteIdentity(value: unknown, field = 'site'): SiteIdentity {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new SiteIdentityError(`${field} invalide`);
  }
  const object = Object.fromEntries(Object.entries(value as Record<string, unknown>));
  const text = (key: string, pattern: RegExp): string => {
    const raw = object[key];
    if (typeof raw !== 'string' || pattern.test(raw) !== true) {
      throw new SiteIdentityError(`${field}.${key} invalide`);
    }
    return raw;
  };
  const optionalText = (key: string, pattern: RegExp = BOUNDED_TEXT): string => {
    const raw = object[key];
    if (raw === undefined || raw === null || raw === '') return '';
    if (typeof raw !== 'string' || !pattern.test(raw)) {
      throw new SiteIdentityError(`${field}.${key} invalide`);
    }
    return raw;
  };
  const tables = object.tables;
  if (!Array.isArray(tables) || tables.length === 0 || tables.length > MAX_MANIFEST) {
    throw new SiteIdentityError(`${field}.tables invalide`);
  }
  const manifest: string[] = [];
  for (const item of tables) {
    if (typeof item !== 'string' || !IBM_I_IDENTIFIER.test(item) || manifest.includes(item)) {
      throw new SiteIdentityError(`${field}.tables invalide`);
    }
    manifest.push(item);
  }
  const siteId = text('site_id', SITE_ID_PATTERN);
  const environment = text('environment', FLEET_TOKEN_PATTERN);
  const runtimeEnvironment = text('runtime_environment', SITE_ID_PATTERN);
  const fleetId = text('fleet_id', FLEET_TOKEN_PATTERN);
  if (fleetId !== `${siteId}-${runtimeEnvironment}`) {
    throw new SiteIdentityError(`${field}.fleet_id invalide`);
  }
  const destinationDatabase = text('destination_database', SNOWFLAKE_IDENTIFIER);
  const destinationSchema = text('destination_schema', SNOWFLAKE_IDENTIFIER);
  const destinationNamespace = text('destination_namespace', /^[A-Za-z_][A-Za-z0-9_$]{0,62}\.[A-Za-z_][A-Za-z0-9_$]{0,62}$/);
  if (destinationNamespace !== `${destinationDatabase}.${destinationSchema}`) {
    throw new SiteIdentityError(`${field}.destination_namespace invalide`);
  }
  const proofTable = text('proof_table', IBM_I_IDENTIFIER);
  if (!manifest.includes(proofTable)) {
    throw new SiteIdentityError(`${field}.proof_table invalide`);
  }
  const runtimePipelineId = text('runtime_pipeline_id', SITE_ID_PATTERN);
  if (runtimePipelineId !== siteId) {
    throw new SiteIdentityError(`${field}.runtime_pipeline_id invalide`);
  }
  return {
    siteId,
    fleetId,
    environment,
    runtimeEnvironment,
    destinationDatabase,
    destinationSchema,
    destinationNamespace,
    sourceSchema: text('source_schema', IBM_I_IDENTIFIER),
    journalName: text('journal_name', IBM_I_IDENTIFIER),
    manifest,
    proofTable,
    runtimePipelineId,
    ibmiHost: text('ibmi_host', IBMI_HOST),
    ibmiUser: text('ibmi_user', IBM_I_IDENTIFIER),
    tlsCaFile: text('tls_ca_file', TLS_CA_FILE),
    secretRefName: optionalText('secret_ref_name', K8S_NAME),
    secretRefKey: optionalText('secret_ref_key', SECRET_KEY),
    snowflakeStage: text('snowflake_stage', SNOWFLAKE_IDENTIFIER),
  };
}
