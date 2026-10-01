import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import type { Pipeline } from './controlPlane.ts';
import { consumptionFor, hasMeasures, humanBytes } from './consumption.ts';

function plain(value: string): string {
  return value.replace(/[   ]/g, ' ');
}

/** Compteurs réels servis par le service pour la liaison `example-corp`. */
function pipeline(overrides: Record<string, unknown> = {}): Pipeline {
  return {
    id: 'example-corp',
    counters: {
      cpu_ms_per_event: 13.9054,
      duplicates_in_target: 0,
      empty_scans: 22433,
      errors: 6,
      events_in_target: 221885292,
      events_published: 1310463,
      idle_polls: 27227,
      mean_mcpu: 201.351,
      payload_bytes_published: 854278169,
      polls: 51626,
      receiver_rotations: 1,
      run_duration_s: 90501.073,
      windows_published: 1960,
    },
    destination: {
      kind: 'snowflake',
      database: 'DEV_RAW',
      schema: 'AS400_RD',
      canonicalRows: 221885292,
      rawRows: 221885292,
      duplicates: 0,
    },
    ...overrides,
  } as unknown as Pipeline;
}

test('les compteurs de boucle interne n’atteignent jamais l’écran', () => {
  const view = consumptionFor(pipeline());
  const labels = view.lines.map((line) => line.label.toLowerCase()).join(' ');
  // polls, idle_polls, empty_scans, receiver_rotations, windows_published :
  // traduits mot à mot, ils restaient des mesures d'ingénieur.
  for (const forbidden of ['lecture à vide', 'recherche', 'rotation', 'fenêtre', 'sondage', 'scan']) {
    assert.ok(!labels.includes(forbidden), `« ${forbidden} » ne doit pas être une ligne : ${labels}`);
  }
  assert.equal(view.lines.length, 5);
});

test('les millièmes de cœur deviennent une puissance lisible', () => {
  const view = consumptionFor(pipeline());
  const cpu = view.lines.find((line) => line.label === 'Puissance machine moyenne');
  // 201,351 mCPU se lit « 0,20 cœur », jamais « 201 ».
  assert.equal(cpu?.value, '0,20');
  assert.equal(cpu?.unit, 'cœur');
});

test('le volume transféré se lit en unités usuelles', () => {
  const view = consumptionFor(pipeline());
  const volume = view.lines.find((line) => line.label === 'Volume transféré');
  assert.equal(plain(volume!.value!), '815 Mio');
});

test('un compteur absent vaut « non mesuré », jamais zéro', () => {
  const view = consumptionFor(pipeline({ counters: {}, destination: null }));
  for (const line of view.lines) assert.equal(line.value, null);
  assert.equal(hasMeasures(view), false);
});

test('la limite du produit est dite, pas reléguée sous le tableau', () => {
  const view = consumptionFor(pipeline());
  assert.ok(view.unmeasured.length >= 1);
  assert.ok(view.unmeasured.some((item) => item.includes('crédits Snowflake')));
  assert.ok(view.unmeasured.some((item) => item.includes('Aucun prix')));
});

test('les octets se convertissent sans fausse précision', () => {
  assert.equal(plain(humanBytes(512)), '512 octets');
  assert.equal(plain(humanBytes(2048)), '2,0 Kio');
  assert.equal(plain(humanBytes(854278169)), '815 Mio');
});
