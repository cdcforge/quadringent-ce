import type { CreateDestinationInput, DestinationRecord } from '../data/controlPlaneV2Client.ts';
import { WIZARD_COPY } from './operator.ts';
import type { FieldValidation } from './wizardSource.ts';

export function validateSnowflakeScope(value: string, optional: boolean): FieldValidation {
  const cleaned = value.trim();
  if ((optional && !cleaned) || /^[A-Za-z_][A-Za-z0-9_$]{0,62}$/.test(cleaned)) {
    return { valid: true, message: null };
  }
  return { valid: false, message: WIZARD_COPY.snowflake.invalidScope };
}

export function destinationInput(account: string, database: string, schema: string): CreateDestinationInput {
  if (!validateSnowflakeScope(database, false).valid || !validateSnowflakeScope(schema, true).valid) {
    throw new Error(WIZARD_COPY.snowflake.invalidScope);
  }
  return {
    accountIdentifier: account.trim(), destinationDatabase: database.trim().toUpperCase(),
    destinationSchema: schema.trim() ? schema.trim().toUpperCase() : null,
  };
}

/** La relecture d'état ne remet ni le script ni la clé de création. */
export function retainDestinationSetup(created: DestinationRecord, readback: DestinationRecord): DestinationRecord {
  if (created.id !== readback.id) throw new Error('La relecture a renvoyé une autre destination.');
  return { ...readback, sqlScript: created.sqlScript, privateKeyPem: created.privateKeyPem };
}
