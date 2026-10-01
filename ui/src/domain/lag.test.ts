import assert from 'node:assert/strict';
import { test } from 'node:test';
import { lagTrend } from './lag.ts';

/* Parité avec `continuous.lag_trend` (src/quadringent/continuous.py).
 *
 *  Les valeurs attendues ne sont pas devinées : chaque cas a été exécuté contre
 *  l'implémentation Python et le résultat recopié ici. Reproduire :
 *
 *    PYTHONPATH=src python3 -c "from quadringent.continuous import lag_trend; \
 *      import json; print(json.dumps(lag_trend([...])))"
 */

test('soak24 — 17 échantillons à 1 → BOUNDED', () => {
  const trend = lagTrend(Array<number>(17).fill(1));
  assert.equal(trend.verdict, 'BOUNDED');
  assert.equal(trend.samples, 17);
  assert.equal(trend.floorFirstThird, 1);
  assert.equal(trend.floorLastThird, 1);
  assert.equal(trend.slopePerSample, 0);
  assert.equal(trend.mean, 1);
});

// Palier 6 du 2026-08-26 : montée monotone jusqu'à 1 377 824, arrêt fail-closed.
// C'est le cas que la règle de pente seule classait BOUNDED.
test('palier 6 — plancher qui monte → DIVERGING', () => {
  const climb = Array.from({ length: 30 }, (_, i) => 1 + Math.round((1377824 * i) / 29));
  const trend = lagTrend(climb);
  assert.equal(trend.verdict, 'DIVERGING');
  assert.equal(trend.diverging, true);
  assert.equal(trend.floorFirstThird, 1);
  assert.equal(trend.floorLastThird, 950224);
  assert.equal(trend.slopePerSample, 47511.17);
});

test('descente convexe vers le tail → CATCHING_UP', () => {
  const trend = lagTrend([400000, 120000, 30000, 5000, 400, 20, 1]);
  assert.equal(trend.verdict, 'CATCHING_UP');
  assert.equal(trend.floorFirstThird, 120000);
  assert.equal(trend.floorLastThird, 1);
  assert.equal(trend.slopePerSample, -52484.179);
});

/* Une descente linéaire depuis un très gros retard rend BOUNDED, pas
 * CATCHING_UP : le seuil de pente vaut 0,25 × la moyenne, et une rampe de M à 0
 * sur n points a une pente de -M/n contre un seuil de -M/8. CATCHING_UP ne se
 * déclenche donc qu'au-dessous de ~8 échantillons ou sur une descente convexe.
 * Le verdict seul ne suffit pas à dire « ça se résorbe » : c'est le plancher qui
 * le dit, et l'écran affiche les deux. */
test('rampe linéaire 30,3 M → 1 : BOUNDED, plancher effondré', () => {
  const descent = Array.from({ length: 30 }, (_, i) => Math.max(1, 30320398 - i * 1045531));
  const trend = lagTrend(descent);
  assert.equal(trend.verdict, 'BOUNDED');
  assert.equal(trend.floorFirstThird, 20910619);
  assert.equal(trend.floorLastThird, 1);
});

// Pic absorbé : le retard monte à 468 046 puis retombe au tail. Sain.
test('pic absorbé — oscillation avec retour au tail → BOUNDED', () => {
  const trend = lagTrend([1, 1, 61946, 1, 4520, 1, 17901, 1, 1, 1, 468046, 1, 1, 1, 1]);
  assert.equal(trend.verdict, 'BOUNDED');
  assert.equal(trend.max, 468046);
  assert.equal(trend.floorFirstThird, 1);
  assert.equal(trend.floorLastThird, 1);
});

test('un seul échantillon → INCONCLUSIVE, aucun zéro inventé', () => {
  const trend = lagTrend([42]);
  assert.equal(trend.verdict, 'INCONCLUSIVE');
  assert.equal(trend.samples, 1);
  assert.equal(trend.first, 42);
});

test('série vide → INCONCLUSIVE, bornes nulles et non zéro', () => {
  const trend = lagTrend([]);
  assert.equal(trend.verdict, 'INCONCLUSIVE');
  assert.equal(trend.min, null);
  assert.equal(trend.max, null);
  assert.equal(trend.first, null);
});
