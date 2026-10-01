import assert from 'node:assert/strict';
import { test } from 'node:test';
import { destinationInput, validateSnowflakeScope, retainDestinationSetup } from './wizardSnowflake.ts';
import type { DestinationRecord } from '../data/controlPlaneV2Client.ts';

test('le scope explicite est normalisé et envoyé par l’assistant', () => {
  assert.deepEqual(destinationInput(' test-account ', ' client_db ', ' site_a '), {
    accountIdentifier: 'test-account', destinationDatabase: 'CLIENT_DB', destinationSchema: 'SITE_A',
  });
});

test('le schéma vide choisit explicitement le contrat historique', () => {
  assert.deepEqual(destinationInput('test-account', 'QUADRINGENT', '  '), {
    accountIdentifier: 'test-account', destinationDatabase: 'QUADRINGENT', destinationSchema: null,
  });
});

test('un scope invalide empêche la génération du script', () => {
  for (const value of ['', '1BAD', 'A;DROP DATABASE X', 'A.B', 'a'.repeat(64)]) {
    assert.equal(validateSnowflakeScope(value, false).valid, false);
    assert.throws(() => destinationInput('test-account', value, ''), /invalide/);
  }
  assert.equal(validateSnowflakeScope('SCHEMA$1', true).valid, true);
  assert.equal(validateSnowflakeScope('', true).valid, true);
  assert.equal(validateSnowflakeScope('a'.repeat(63), false).valid, true);
  assert.throws(() => destinationInput('test-account', 'CLIENT_DB', 'A.B'), /invalide/);
});

test('la relecture sans script ni clé conserve le reçu initial, même après un échec', () => {
  const created: DestinationRecord = {
    id: 'dst_synthetic', accountIdentifier: 'synthetic-account', destinationDatabase: 'TEST_DB', destinationSchema: 'TEST_SCHEMA',
    verificationState: 'declared_not_verified', sqlScript: 'SCRIPT SYNTHETIQUE INITIAL', privateKeyPem: 'CLE SYNTHETIQUE INITIALE',
  };
  for (const verificationState of ['failed', 'verified'] as const) {
    const readback: DestinationRecord = { ...created, sqlScript: '', privateKeyPem: null, verificationState };
    const result = retainDestinationSetup(created, readback);
    assert.equal(result.verificationState, verificationState);
    assert.equal(result.sqlScript, created.sqlScript);
    assert.equal(result.privateKeyPem, created.privateKeyPem);
    assert.equal(readback.sqlScript, '');
  }
  assert.throws(() => retainDestinationSetup(created, { ...created, id: 'dst_autre' }), /autre destination/);
});
