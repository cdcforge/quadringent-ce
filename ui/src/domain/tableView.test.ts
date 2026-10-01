import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import type { Pipeline } from './controlPlane.ts';
import { tableRows, tableStateFor, tablesSummary } from './tableView.ts';

function plain(value: string): string {
  return value.replace(/[   ]/g, ' ');
}

function pipelineWith(states: ReadonlyArray<Record<string, unknown>>): Pipeline {
  return {
    id: 'example-corp',
    fleetRuntime: { tableStates: states },
  } as unknown as Pipeline;
}

test('les quatre moments d’une copie en cours se disent d’une seule façon', () => {
  // Le service distingue HISTORICAL, CATCHING_UP, LIVE, RECONCILING : pour
  // l'exploitant, la copie travaille, et c'est tout ce qui change sa décision.
  for (const phase of ['HISTORICAL', 'CATCHING_UP', 'LIVE', 'RECONCILING'] as const) {
    assert.equal(tableStateFor(phase).state, 'encours');
    assert.equal(tableStateFor(phase).label, 'Copie en cours');
  }
});

test('seule une table certifiée est annoncée copiée', () => {
  assert.deepEqual(tableStateFor('CERTIFIED'), { state: 'copiee', label: 'Copiée' });
  assert.notEqual(tableStateFor('LIVE').state, 'copiee');
});

test('une table bloquée reste bloquée, jamais « en attente »', () => {
  assert.deepEqual(tableStateFor('BLOCKED'), { state: 'bloquee', label: 'Bloquée' });
});

test('les problèmes remontent en tête de liste', () => {
  const rows = tableRows(
    pipelineWith([
      { name: 'ZZZ', phase: 'CERTIFIED', copiedRows: 10, totalRows: 10 },
      { name: 'AAA', phase: 'CERTIFIED', copiedRows: 20, totalRows: 20 },
      { name: 'MMM', phase: 'BLOCKED', copiedRows: null, totalRows: null },
    ]),
  );
  assert.equal(rows[0]?.name, 'MMM');
  // À état égal, l'ordre reste alphabétique — la liste ne bouge pas d'un relevé à l'autre.
  assert.deepEqual(rows.slice(1).map((row) => row.name), ['AAA', 'ZZZ']);
});

test('l’avancement n’est calculé que pendant une copie', () => {
  const rows = tableRows(
    pipelineWith([
      { name: 'ENCOURS', phase: 'LIVE', copiedRows: 50, totalRows: 200 },
      { name: 'FINIE', phase: 'CERTIFIED', copiedRows: 200, totalRows: 200 },
    ]),
  );
  const encours = rows.find((row) => row.name === 'ENCOURS');
  const finie = rows.find((row) => row.name === 'FINIE');
  assert.equal(encours?.progress, 0.25);
  // Une barre pleine sur chaque ligne terminée ne dit rien de plus que l'état.
  assert.equal(finie?.progress, null);
});

test('un compte de lignes absent se dit, il ne devient pas zéro', () => {
  const rows = tableRows(pipelineWith([{ name: 'X', phase: 'BLOCKED', copiedRows: null, totalRows: null }]));
  assert.equal(rows[0]?.rows, null);
});

test('le résumé annonce les problèmes avant les réussites', () => {
  const rows = tableRows(
    pipelineWith([
      { name: 'A', phase: 'CERTIFIED', copiedRows: 1, totalRows: 1 },
      { name: 'B', phase: 'BLOCKED', copiedRows: null, totalRows: null },
    ]),
  );
  assert.equal(plain(tablesSummary(rows)), '1 table bloquée sur 2');
});

test('quand tout est copié, le résumé le dit sans fraction inutile', () => {
  const rows = tableRows(
    pipelineWith([
      { name: 'A', phase: 'CERTIFIED', copiedRows: 1, totalRows: 1 },
      { name: 'B', phase: 'CERTIFIED', copiedRows: 2, totalRows: 2 },
    ]),
  );
  assert.equal(plain(tablesSummary(rows)), '2 tables copiées');
});

test('une liaison sans table le dit plutôt que d’afficher zéro', () => {
  assert.equal(tablesSummary([]), 'Aucune table déclarée');
});

test('aucun réglage de capture n’atteint la ligne d’une table', () => {
  const rows = tableRows(pipelineWith([{ name: 'SALE', phase: 'CERTIFIED', copiedRows: 5, totalRows: 5 }]));
  const surfaced = JSON.stringify(rows).toLowerCase();
  // *AFTER, *BOTH et RRN décrivent ce que la capture sait reconstituer,
  // pas si les données sont arrivées.
  for (const forbidden of ['after', 'both', 'rrn', 'journal', 'identité']) {
    assert.ok(!surfaced.includes(forbidden), `« ${forbidden} » ne doit pas atteindre la ligne : ${surfaced}`);
  }
});
