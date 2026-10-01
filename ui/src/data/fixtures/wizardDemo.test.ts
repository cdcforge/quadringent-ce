import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client } from '../controlPlaneV2Client.ts';
import { createWizardDemoFetch, DEMO_SOURCE_ID, DEMO_DESTINATION_ID } from './wizardDemo.ts';

test('the demo fetch lets the whole wizard flow run against the real client, with no network', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: createWizardDemoFetch() });

  const created = await client.createSource({ displayName: 'IBM i — demo', host: 'as400.demo.local', account: 'QDTDEMO', password: 'x' });
  assert.equal(created.id, DEMO_SOURCE_ID);

  const sources = await client.listSources();
  assert.equal(sources.length, 1);
  assert.equal(sources[0]!.id, DEMO_SOURCE_ID);

  const test1 = await client.testSource(DEMO_SOURCE_ID);
  // Sans sonde câblée, la démo reproduit la forme « non disponible » du
  // serveur réel (`SourcesService.test` sans `probe`) — jamais un faux vert.
  assert.equal(test1.kind, 'unavailable');

  const destination = await client.createDestination({ accountIdentifier: 'demo-xy12345' });
  assert.equal(destination.id, DEMO_DESTINATION_ID);
  assert.match(destination.sqlScript, /CREATE ROLE/);
  assert.ok(destination.privateKeyPem);

  const tables = await client.listTables(DEMO_SOURCE_ID);
  assert.ok(tables.length >= 3);
  const noKeyTable = tables.find((table) => table.readiness === 'no_key');
  assert.ok(noKeyTable, 'la démo doit fournir au moins une table prête au journal mais sans clé');

  const patched = await client.patchTableKey(noKeyTable!.id, { keyStrategy: 'rrn', acknowledgeRrn: true });
  assert.equal(patched.keyStrategy, 'rrn');
  assert.equal(patched.readiness, 'ready');

  await client.startTablePipeline(noKeyTable!.id, { dryRun: false });

  const user = await client.activateAdmin('tok_demo', 'mot-de-passe-suffisant');
  assert.equal(user.activated, true);
});
