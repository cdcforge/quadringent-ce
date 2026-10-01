/**
 * Le branchement du parcours de création sur la route du service.
 *
 * Les deux extrémités sont vérifiées séparément — la route par la suite
 * Python, le parcours par ses propres tests — mais rien ne garantissait le
 * contrat entre elles : le verbe, le chemin, et surtout le traitement des
 * trois réponses possibles.
 */

import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { ControlPlaneClient } from './controlPlaneClient.ts';

interface Call {
  readonly url: string;
  readonly init: RequestInit | undefined;
}

function clientReturning(status: number, body: unknown): { client: ControlPlaneClient; calls: Call[] } {
  const calls: Call[] = [];
  const client = new ControlPlaneClient({
    fetchFn: async (url, init) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  return { client, calls };
}

test('la création poste le corps du parcours sur la route des liaisons', async () => {
  const { client, calls } = clientReturning(201, { connection: { connection_id: 'abc' } });
  await client.createConnection({ ibmi_host: '192.0.2.10', display_name: 'AS400 recette' });

  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.url, '/v1/connections');
  assert.equal(calls[0]?.init?.method, 'POST');
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), {
    ibmi_host: '192.0.2.10',
    display_name: 'AS400 recette',
  });
});

test('un 201 rend la liaison créée', async () => {
  const { client } = clientReturning(201, { connection: { connection_id: 'abc' } });
  const outcome = await client.createConnection({});
  assert.equal(outcome.ok, true);
});

test('un refus revient comme un verdict, pas comme une panne', async () => {
  // C'est ce qui permet à l'écran de réafficher le refus dans ses propres
  // mots plutôt que d'annoncer une erreur technique.
  const verdict = { step: 'destination', status: 'error', errors: ['Le journal IBM i est obligatoire'] };
  const { client } = clientReturning(400, verdict);
  const outcome = await client.createConnection({});
  assert.equal(outcome.ok, false);
  assert.deepEqual(outcome.ok === false ? outcome.verdict : null, verdict);
});

test('une réponse hors contrat lève, elle ne se fait pas passer pour un succès', async () => {
  for (const status of [404, 500, 503]) {
    const { client } = clientReturning(status, {});
    await assert.rejects(() => client.createConnection({}), /Création impossible/);
  }
});
