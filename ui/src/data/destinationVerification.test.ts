import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client } from './controlPlaneV2Client.ts';

function verificationClient(probe: boolean | 'unknown', stored: string) {
  const requests: Array<{ path: string; init?: RequestInit }> = [];
  const client = new ControlPlaneV2Client({ fetchFn: async (path, init) => {
    requests.push({ path, init });
    const body = init?.method === 'POST'
      ? { before: null, after: { destination_id: 'dst_1', verified: probe,
        connection: { ok: probe, detail: probe === true ? null : 'Connexion non établie.' } },
        verify: { method: 'GET', path: '/v2/destinations/dst_1' }, dry_run: null }
      : { id: 'dst_1', snowflake_account: 'synthetic-account', verification_state: stored, setup_script: 'SQL SYNTHETIQUE' };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  } });
  return { client, requests };
}

test('la sonde native porte une clé d’idempotence et relit la destination avant de confirmer', async () => {
  const { client, requests } = verificationClient(true, 'verified');
  const result = await client.verifyDestination('dst_1');
  assert.equal(result.verified, true);
  assert.deepEqual(requests.map(({ path, init }) => [path, init?.method ?? 'GET']), [
    ['/v2/destinations/dst_1/verify', 'POST'], ['/v2/destinations/dst_1', 'GET'],
  ]);
  assert.equal(requests[0].init?.body, '{}');
  assert.ok(new Headers(requests[0].init?.headers).get('Idempotency-Key'));
  assert.equal(new Headers(requests[1].init?.headers).get('Idempotency-Key'), null);
});

test('un succès de sonde sans état persisté vérifié ne permet pas de continuer', async () => {
  const { client } = verificationClient(true, 'declared_not_verified');
  assert.equal((await client.verifyDestination('dst_1')).verified, false);
});

test('un état ancien vérifié ne transforme pas une sonde échouée ou inconnue en succès', async () => {
  for (const probe of [false, 'unknown'] as const) {
    const { client } = verificationClient(probe, 'verified');
    const result = await client.verifyDestination('dst_1');
    assert.equal(result.verified, false);
    assert.equal(result.detail, 'Connexion non établie.');
  }
});

test('une relecture d’une autre destination est refusée sans remplacer la cible', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: async (_path, init) => {
    const body = init?.method === 'POST'
      ? { before: null, after: { destination_id: 'dst_1', verified: true }, verify: null, dry_run: null }
      : { id: 'dst_autre', snowflake_account: 'autre-compte', verification_state: 'verified', setup_script: 'AUTRE SCRIPT' };
    return new Response(JSON.stringify(body), { status: 200 });
  } });
  await assert.rejects(client.verifyDestination('dst_1'), /autre destination/);
});

test('une nouvelle tentative porte une clé d’idempotence distincte', async () => {
  const { client, requests } = verificationClient(false, 'failed');
  await client.verifyDestination('dst_1');
  await client.verifyDestination('dst_1');
  const keys = requests.filter(({ init }) => init?.method === 'POST')
    .map(({ init }) => new Headers(init?.headers).get('Idempotency-Key'));
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1]);
});

test('une ancienne identité absente conserve le diagnostic de régénération fourni par le service', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: async (_path, init) => {
    const body = init?.method === 'POST'
      ? { before: null, after: { destination_id: 'dst_1', verified: 'unknown', detail: 'Régénérez la destination.' }, verify: null, dry_run: null }
      : { id: 'dst_1', verification_state: 'declared_not_verified' };
    return new Response(JSON.stringify(body), { status: 200 });
  } });
  const result = await client.verifyDestination('dst_1');
  assert.equal(result.verified, false);
  assert.equal(result.detail, 'Régénérez la destination.');
});
