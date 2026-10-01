import assert from 'node:assert/strict';
import { test } from 'node:test';
import { age, decimal, exact, formatDuration, sequences } from './format.ts';

const THIN = ' ';

test('les séquences gardent tous leurs chiffres', () => {
  assert.equal(sequences(30320398), `30${THIN}320${THIN}398`);
  assert.equal(sequences(1), '1');
  assert.equal(sequences(142580), `142${THIN}580`);
});

test('les décimales de mesure ne sont pas rabotées', () => {
  assert.equal(decimal(0.5463, 4), '0,5463');
  assert.equal(decimal(30, 0), '30');
});

test('une mesure s’affiche telle quelle, sans bourrage de décimales', () => {
  assert.equal(exact(0.5463), '0,5463');
  assert.equal(exact(30.69), '30,69');
  assert.equal(exact(30), '30');
});

test('les durées suivent un format unique : s sous la minute, min·s sous l’heure, h·min sous deux jours', () => {
  assert.equal(formatDuration(8), `8${THIN}s`);
  assert.equal(formatDuration(174), `2${THIN}min${THIN}54${THIN}s`);
  assert.equal(formatDuration(196), `3${THIN}min${THIN}16${THIN}s`);
  assert.equal(formatDuration(600), `10${THIN}min`);
  assert.equal(formatDuration(1247), `20${THIN}min${THIN}47${THIN}s`);
  assert.equal(formatDuration(3600), `1${THIN}h`);
  assert.equal(formatDuration(18540), `5${THIN}h${THIN}09`);
  assert.equal(formatDuration(90501), `25${THIN}h${THIN}08`);
  assert.equal(formatDuration(140_000), `38${THIN}h${THIN}53`);
  assert.equal(formatDuration(200_000), `2${THIN}j`);
});

test('une mesure vieille est marquée vieille', () => {
  const now = new Date('2026-08-28T10:00:00Z');
  assert.equal(age('2026-08-28T09:59:30Z', now).stale, false);
  assert.equal(age('2026-08-27T22:00:00Z', now).stale, true);
  assert.equal(age('2026-08-28T09:00:00Z', now).label, `il y a 1${THIN}h`);
});
