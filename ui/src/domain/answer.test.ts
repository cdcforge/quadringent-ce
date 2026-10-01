import assert from 'node:assert/strict';
import { test } from 'node:test';
import { advanceAnswer } from './answer.ts';
import { reading, unknown, type Flux } from './types.ts';

const SRC = 'test';
const AT = '2026-08-27T12:00:00+02:00';

function flux(over: Partial<Flux> = {}): Flux {
  const base: Flux = {
    id: 'f',
    label: 'f',
    journal: 'TRNJRN',
    journalLibrary: 'LEDGER',
    objects: ['SALE'],
    readerPath: 'RetrieveJournal',
    target: 't',
    job: 'j',
    runState: reading('RUNNING', SRC, AT),
    runStartedAt: reading(AT, SRC, AT),
    position: {
      checkpoint: reading({ receiver: 'R', sequence: 1 }, SRC, AT),
      sourceTail: reading({ receiver: 'R', sequence: 2 }, SRC, AT),
      receiverFirstSequence: reading(0, SRC, AT),
      receiverLastSequence: reading(2, SRC, AT),
    },
    lag: {
      current: reading(1, SRC, AT),
      verdict: reading('BOUNDED', SRC, AT),
      floorFirstThird: reading(1, SRC, AT),
      floorLastThird: reading(1, SRC, AT),
      max: reading(1, SRC, AT),
      series: { source: SRC, observedAt: AT, points: [], sampleCount: 0, complete: true },
    },
    counters: {
      polls: reading(1, SRC, AT),
      errors: reading(0, SRC, AT),
      windowsPublished: reading(1, SRC, AT),
      eventsPublished: reading(1, SRC, AT),
      eventsInTarget: reading(1, SRC, AT),
      duplicatesInTarget: reading(0, SRC, AT),
      receiverRotations: reading(0, SRC, AT),
      meanMilliCpu: reading(30, SRC, AT),
      cpuMsPerEvent: reading(0.5463, SRC, AT),
      runDurationS: reading(1, SRC, AT),
    },
    timeline: [],
    caveats: [],
  };
  return { ...base, ...over };
}

test('au tail et borné → oui', () => {
  const answer = advanceAnswer(flux());
  assert.equal(answer.verdict, 'oui');
  assert.equal(answer.because, 'au tail');
  assert.equal(answer.next, undefined);
});

// Le cas qui donne son sens à l'écran : un gros retard qui décroît est sain.
test('400 000 de retard mais plancher qui descend → oui', () => {
  const answer = advanceAnswer(
    flux({
      lag: {
        current: reading(400000, SRC, AT),
        verdict: reading('CATCHING_UP', SRC, AT),
        floorFirstThird: reading(900000, SRC, AT),
        floorLastThird: reading(120000, SRC, AT),
        max: reading(900000, SRC, AT),
        series: { source: SRC, observedAt: AT, points: [], sampleCount: 0, complete: true },
      },
    }),
  );
  assert.equal(answer.verdict, 'oui');
  assert.equal(answer.because, 'le retard se résorbe');
});

// Et le symétrique : un petit retard qui monte est un incident.
test('500 de retard mais plancher qui monte → non, avec la suite à faire', () => {
  const answer = advanceAnswer(
    flux({
      lag: {
        current: reading(500, SRC, AT),
        verdict: reading('DIVERGING', SRC, AT),
        floorFirstThird: reading(1, SRC, AT),
        floorLastThird: reading(480, SRC, AT),
        max: reading(500, SRC, AT),
        series: { source: SRC, observedAt: AT, points: [], sampleCount: 0, complete: true },
      },
    }),
  );
  assert.equal(answer.verdict, 'non');
  assert.equal(answer.because, 'le plancher monte');
  assert.ok(answer.next);
});

// Un arrêt fail-closed n'est pas « live ». C'est l'erreur qu'on refuse de faire.
test('arrêté fail-closed → non, quel que soit le retard', () => {
  const answer = advanceAnswer(
    flux({ runState: reading('STOPPED_FAIL_CLOSED', SRC, AT) }),
  );
  assert.equal(answer.verdict, 'non');
  assert.equal(answer.because, 'arrêté fail-closed');
  assert.match(answer.next!, /checkpoint est intact/);
});

/* Le cas que `lag_trend` classe BOUNDED alors qu'il se résorbe : une rampe
 * linéaire depuis 30,3 M. Le verdict ne le dit pas, le plancher si. */
test('BOUNDED mais plancher effondré → oui, « le plancher descend »', () => {
  const answer = advanceAnswer(
    flux({
      lag: {
        current: reading(2400000, SRC, AT),
        verdict: reading('BOUNDED', SRC, AT),
        floorFirstThird: reading(20910619, SRC, AT),
        floorLastThird: reading(1, SRC, AT),
        max: reading(30320398, SRC, AT),
        series: { source: SRC, observedAt: AT, points: [], sampleCount: 0, complete: true },
      },
    }),
  );
  assert.equal(answer.verdict, 'oui');
  assert.equal(answer.because, 'le plancher descend');
});

test('retard non calculable → indéterminé, jamais « oui »', () => {
  const answer = advanceAnswer(
    flux({
      lag: {
        current: unknown('le curseur et le tail sont sur des receivers disjoints', SRC, AT),
        verdict: reading('BOUNDED', SRC, AT),
        floorFirstThird: reading(1, SRC, AT),
        floorLastThird: reading(1, SRC, AT),
        max: reading(1, SRC, AT),
        series: { source: SRC, observedAt: AT, points: [], sampleCount: 0, complete: true },
      },
    }),
  );
  assert.equal(answer.verdict, 'indéterminé');
  assert.equal(answer.because, 'retard non calculable');
});

test('état d’exécution inconnu → indéterminé', () => {
  const answer = advanceAnswer(flux({ runState: unknown('pas de relevé', SRC, AT) }));
  assert.equal(answer.verdict, 'indéterminé');
});
