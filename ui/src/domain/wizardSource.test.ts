import assert from 'node:assert/strict';
import test from 'node:test';
import { validateSourceField, validateSnowflakeAccount, isSourceFormComplete, prefillExistingSource, canTestSource, shouldCreateSource } from './wizardSource.ts';

test('an asynchronously found source restores only empty identity fields and never a password', () => {
  const existing = { host: 'ibmi.example.test', ibmiUser: 'QDCUSER' };
  assert.deepEqual(
    prefillExistingSource({ host: '', account: '', password: '' }, existing),
    { host: 'ibmi.example.test', account: 'QDCUSER', password: '' },
  );
  assert.deepEqual(
    prefillExistingSource({ host: 'other.example.test', account: 'EDITOR', password: 'en-cours-de-saisie' }, existing),
    { host: 'other.example.test', account: 'EDITOR', password: 'en-cours-de-saisie' },
  );
});

test('an empty host is reported as required, not as a format error', () => {
  const result = validateSourceField('host', '');
  assert.equal(result.valid, false);
  assert.equal(result.message, 'L’adresse est obligatoire.');
});

test('a host with a scheme or path is rejected with a corrective message', () => {
  const result = validateSourceField('host', 'https://as400.local/');
  assert.equal(result.valid, false);
  assert.match(result.message ?? '', /adresse/i);
});

test('a plain hostname or IP is a valid host', () => {
  assert.equal(validateSourceField('host', 'ibmi.example.test').valid, true);
  assert.equal(validateSourceField('host', '192.0.2.12').valid, true);
});

test('an account name must respect the IBM i user profile shape', () => {
  assert.equal(validateSourceField('account', 'QSECOFR').valid, true);
  assert.equal(validateSourceField('account', 'trop-long-pour-un-profil-ibmi-standard').valid, false);
  assert.equal(validateSourceField('account', '').valid, false);
});

test('a password is only checked for presence — never for a shape we cannot know', () => {
  assert.equal(validateSourceField('password', '').valid, false);
  assert.equal(validateSourceField('password', 'x').valid, true);
});

test('the form is complete only once every field individually validates', () => {
  assert.equal(isSourceFormComplete({ host: 'as400.local', account: 'QSECOFR', password: 'secret' }), true);
  assert.equal(isSourceFormComplete({ host: '', account: 'QSECOFR', password: 'secret' }), false);
});

test('a saved source can be retested without reentering its secret', () => {
  const source = { host: 'as400.local', account: 'QSECOFR' };
  const values = { ...source, password: '' };
  assert.equal(canTestSource(values, source), true);
  assert.equal(shouldCreateSource(values, source), false);
});

test('new credentials create a new source and require a password', () => {
  const source = { host: 'as400.local', account: 'QSECOFR' };
  assert.equal(canTestSource({ ...source, password: 'new-secret' }, source), true);
  assert.equal(shouldCreateSource({ ...source, password: 'new-secret' }, source), true);
  assert.equal(canTestSource({ ...source, host: 'other.local', password: '' }, source), false);
  assert.equal(canTestSource({ ...source, password: '' }, null), false);
});

test('a Snowflake account identifier rejects a full URL — only the identifier is asked for', () => {
  assert.equal(validateSnowflakeAccount('abcd-xy12345').valid, true);
  assert.equal(validateSnowflakeAccount('https://abcd-xy12345.snowflakecomputing.com').valid, false);
  assert.equal(validateSnowflakeAccount('').valid, false);
});
