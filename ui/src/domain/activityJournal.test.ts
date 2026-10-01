import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Overview, Pipeline } from './controlPlane.ts';
import {
  activityJournalMeta,
  activitySessionEntries,
  mergeActivityEntries,
  snapshotActivityEvents,
  type ActivityEntry,
  type ActivityLog,
} from './activityJournal.ts';

const observedAt = '2026-09-13T09:00:00Z';
const earlierAt = '2026-09-13T08:58:00Z';
const laterAt = '2026-09-13T09:00:30Z';

function pipelineFixture(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id: 'alpha',
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: 'État borné au snapshot',
    observedAt,
    stages: (['source', 'capture', 'raw', 'load', 'destination'] as const).map((id) => ({
      id,
      status: 'healthy' as const,
      observedAt,
      headline: `${id} observé`,
      detail: 'Signal reçu.',
    })),
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: null,
    ...overrides,
  };
}

function overviewAt(revision: number, generatedAt = observedAt): Pick<Overview, 'revision' | 'generatedAt'> {
  return { revision, generatedAt };
}

function entry(overrides: Partial<ActivityEntry> = {}): ActivityEntry {
  return {
    at: observedAt,
    type: 'Relevé',
    source: 'alpha',
    verdict: 'constaté',
    label: 'Relevé lu',
    detail: null,
    origin: 'mesuré',
    tone: 'muted',
    ...overrides,
  };
}

/* ------------------------------------------------------------------ */
/* Événements du relevé courant — origine « mesuré »                    */
/* ------------------------------------------------------------------ */

test('un relevé sain produit un jalon Relevé mesuré, jamais une transition', () => {
  const events = snapshotActivityEvents(pipelineFixture(), false);
  assert.equal(events.length, 1);
  const [row] = events;
  assert.equal(row!.type, 'Relevé');
  assert.equal(row!.origin, 'mesuré');
  assert.equal(row!.verdict, 'constaté');
  assert.equal(row!.label, 'Relevé lu — État borné au snapshot');
  assert.equal(row!.at, observedAt);
});

test('un relevé figé qualifie chaque jalon — « constaté · figé », jamais live', () => {
  const events = snapshotActivityEvents(pipelineFixture(), true);
  assert.equal(events.length, 1);
  assert.equal(events[0]!.verdict, 'constaté · figé');
});

test('l incident déclaré devient une entrée mesurée datée par l étape fautive', () => {
  const pipeline = pipelineFixture({
    status: 'incident',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' },
  });
  const events = snapshotActivityEvents(pipeline, false);
  const incident = events.find((item) => item.type === 'Incident');
  assert.ok(incident);
  assert.equal(incident.origin, 'mesuré');
  assert.equal(incident.verdict, 'en cours');
  assert.equal(incident.tone, 'attention');
  assert.equal(incident.label, 'Capture arrêtée en sécurité');
  assert.match(incident.detail ?? '', /arrêt fail-closed.*relancer la capture/s);
  assert.equal(incident.at, observedAt);
});

test('un relevé figé ne transforme pas l incident en fait courant', () => {
  const pipeline = pipelineFixture({
    status: 'incident',
    incident: { code: 'capture_auth_blocked', type: 'capture_auth_blocked' },
  });
  const incident = snapshotActivityEvents(pipeline, true).find((item) => item.type === 'Incident');
  assert.equal(incident!.verdict, 'constaté · figé');
});

test('l arrêt planifié est observé, jamais présenté comme un incident', () => {
  const events = snapshotActivityEvents(pipelineFixture({ status: 'planned_stop' }), false);
  const stop = events.find((item) => item.type === 'Arrêt planifié');
  assert.ok(stop);
  assert.equal(stop.verdict, 'observé');
  assert.equal(stop.label, 'Lecture mise en pause — arrêt demandé');
  assert.equal(stop.detail, 'rien n’indique un incident');
});

test('la reprise reste bornée : une lecture complète reste requise', () => {
  const live = snapshotActivityEvents(pipelineFixture({ status: 'recovering' }), false);
  assert.equal(live.find((item) => item.type === 'Reprise')!.verdict, 'reprise en cours');
  const held = snapshotActivityEvents(pipelineFixture({ status: 'recovering' }), true);
  assert.equal(held.find((item) => item.type === 'Reprise')!.verdict, 'reprise constatée · figé');
  assert.match(held.find((item) => item.type === 'Reprise')!.detail ?? '', /lecture complète/);
});

test('les jalons d étape datés ou jamais observés sont des mesures, pas des transitions', () => {
  const pipeline = pipelineFixture({
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'capture'
        ? { ...stage, status: 'degraded' as const, observedAt: earlierAt, headline: 'Lecture incomplète' }
        : stage.id === 'destination'
          ? { ...stage, observedAt: null }
          : stage,
    ),
  });
  const events = snapshotActivityEvents(pipeline, false);
  const milestone = events.find((item) => item.label === 'Capture — Lecture incomplète');
  assert.ok(milestone);
  assert.equal(milestone.type, 'Étape');
  assert.equal(milestone.at, earlierAt);
  assert.equal(milestone.origin, 'mesuré');
  const never = events.find((item) => item.label === 'Destination — jamais observée');
  assert.ok(never);
  assert.equal(never.at, observedAt);
});

/* ------------------------------------------------------------------ */
/* Transitions déduites entre révisions — origine « déduit »            */
/* ------------------------------------------------------------------ */

test('le premier relevé de session ouvre le journal en « déduit »', () => {
  const log = new Map<string, ActivityLog>();
  const entries = activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  assert.equal(entries.length, 1);
  assert.equal(entries[0]!.type, 'Session');
  assert.equal(entries[0]!.origin, 'déduit');
  assert.equal(entries[0]!.verdict, 'reçu');
  assert.match(entries[0]!.label, /Premier relevé de la session — Garantie vérifiée/);
  assert.equal(log.get('alpha')!.received, 1);
});

test('la même révision servie deux fois ne duplique ni ne compte deux relevés', () => {
  const log = new Map<string, ActivityLog>();
  const first = activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  const second = activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  assert.equal(second, first);
  assert.equal(log.get('alpha')!.received, 1);
});

test('un relevé identique incrémente le compteur sans ajouter de ligne', () => {
  const log = new Map<string, ActivityLog>();
  activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  const entries = activitySessionEntries(log, pipelineFixture(), overviewAt(42, laterAt));
  assert.equal(entries.length, 1);
  assert.equal(log.get('alpha')!.received, 2);
});

test('incident apparu : type, cause servie, et pas de doublon « statut »', () => {
  const log = new Map<string, ActivityLog>();
  activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  const entries = activitySessionEntries(
    log,
    pipelineFixture({
      status: 'incident',
      incident: { code: 'x', type: 'capture_connection_failure' },
    }),
    overviewAt(42, laterAt),
  );
  const appeared = entries.find((item) => item.verdict === 'apparu');
  assert.ok(appeared);
  assert.equal(appeared.type, 'Incident');
  assert.equal(appeared.origin, 'déduit');
  assert.equal(appeared.tone, 'attention');
  assert.match(appeared.label, /Incident apparu — Connexion AS400 interrompue/);
  assert.match(appeared.detail ?? '', /ne joint plus la source/);
  // Le changement de statut qui accompagne l'incident n'est pas doublé.
  assert.equal(entries.filter((item) => item.type === 'Statut').length, 0);
});

test('incident résolu est déduit entre deux relevés, pas affirmé par un seul', () => {
  const log = new Map<string, ActivityLog>();
  activitySessionEntries(
    log,
    pipelineFixture({ status: 'incident', incident: { code: 'x', type: 'capture_stopped' } }),
    overviewAt(41),
  );
  const entries = activitySessionEntries(log, pipelineFixture(), overviewAt(42, laterAt));
  const resolved = entries.find((item) => item.type === 'Incident');
  assert.ok(resolved);
  assert.equal(resolved.verdict, 'résolu');
  assert.match(resolved.label, /Incident résolu — plus signalé dans le relevé/);
  // Le retour de statut qui accompagne la résolution est couvert par
  // l'entrée incident — pas doublé en ligne « Statut ».
  assert.equal(entries.filter((item) => item.type === 'Statut').length, 0);
});

test('un changement de statut sans incident est une transition déduite', () => {
  const log = new Map<string, ActivityLog>();
  activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  const entries = activitySessionEntries(
    log,
    pipelineFixture({ status: 'degraded' }),
    overviewAt(42, laterAt),
  );
  const status = entries.find((item) => item.type === 'Statut');
  assert.ok(status);
  assert.equal(status.label, 'Statut : Garantie vérifiée → Couverture partielle');
  assert.equal(status.at, laterAt);
});

test('la fraîcheur du relevé est une transition bornée, jamais un fait courant', () => {
  const log = new Map<string, ActivityLog>();
  activitySessionEntries(log, pipelineFixture(), overviewAt(41));
  const entries = activitySessionEntries(
    log,
    pipelineFixture({ quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' } }),
    overviewAt(42, laterAt),
  );
  const reading = entries.find((item) => item.type === 'Relevé');
  assert.ok(reading);
  assert.equal(reading.origin, 'déduit');
  assert.equal(reading.label, 'Relevé devenu trop ancien');
});

test('la phase d exécution et l étape Lecture ont leurs propres transitions', () => {
  const runtime = (phase: 'LIVE' | 'PAUSED') => ({
    formatVersion: 'v1' as const,
    fleetId: 'f',
    environment: 'dev',
    pipelineId: 'alpha',
    phase,
    checkpoint: null,
    capabilities: {},
    tableStates: [],
  });
  const log = new Map<string, ActivityLog>();
  activitySessionEntries(
    log,
    pipelineFixture({ fleetRuntime: runtime('LIVE') }),
    overviewAt(41),
  );
  const entries = activitySessionEntries(
    log,
    pipelineFixture({
      fleetRuntime: runtime('PAUSED'),
      stages: pipelineFixture().stages.map((stage) =>
        stage.id === 'capture' ? { ...stage, status: 'planned_stop' as const } : stage,
      ),
    }),
    overviewAt(42, laterAt),
  );
  const phase = entries.find((item) => item.type === 'Phase');
  assert.ok(phase);
  assert.equal(phase.label, 'Phase d’exécution : En temps réel → Suspendue');
  const stage = entries.find((item) => item.type === 'Étape');
  assert.ok(stage);
  assert.match(stage.label, /Étape Lecture : Garantie vérifiée → Arrêt planifié/);
});

/* ------------------------------------------------------------------ */
/* Fusion — tri, rang, déduplication, plafond                           */
/* ------------------------------------------------------------------ */

test('la fusion trie décroissant et fait passer l incident avant le reste à instant égal', () => {
  const rows = mergeActivityEntries([
    {
      pipeline: pipelineFixture(),
      session: [entry({ type: 'Statut', label: 'Statut : A → B', origin: 'déduit', verdict: 'déduit' })],
      events: [
        entry({ type: 'Incident', label: 'Capture arrêtée', tone: 'attention' }),
        entry({ type: 'Relevé', label: 'Relevé lu', at: earlierAt }),
      ],
    },
  ]);
  assert.deepEqual(
    rows.map((row) => row.label),
    ['Capture arrêtée', 'Statut : A → B', 'Relevé lu'],
  );
});

test('la fusion déduplique les entrées identiques sans effacer le distinct', () => {
  const duplicate = entry({ type: 'Reprise', label: 'Rétablissement en cours' });
  const rows = mergeActivityEntries([
    { pipeline: pipelineFixture(), session: [duplicate], events: [duplicate] },
    { pipeline: pipelineFixture({ id: 'beta' }), session: [entry({ source: 'beta', label: 'Rétablissement en cours', type: 'Reprise' })], events: [] },
  ]);
  assert.equal(rows.filter((row) => row.source === 'alpha').length, 1);
  assert.equal(rows.filter((row) => row.source === 'beta').length, 1);
});

test('la fusion plafonne le journal sans jamais inventer d entrée', () => {
  const session = Array.from({ length: 20 }, (_, index) =>
    entry({ at: `2026-09-13T09:0${index % 10}:00Z`, label: `Transition ${index}`, origin: 'déduit', type: 'Statut' }),
  );
  const rows = mergeActivityEntries([{ pipeline: pipelineFixture(), session, events: [] }], 5);
  assert.equal(rows.length, 5);
  for (let index = 1; index < rows.length; index += 1) {
    assert.ok(Date.parse(rows[index - 1]!.at) >= Date.parse(rows[index]!.at));
  }
});

/* ------------------------------------------------------------------ */
/* Méta du journal                                                       */
/* ------------------------------------------------------------------ */

test('la méta cite révision, âge et relevés reçus — au singulier comme au pluriel', () => {
  assert.equal(
    activityJournalMeta(298, 'il y a 2 s', 1),
    'révision 298 · générée il y a 2 s · 1 relevé reçu',
  );
  assert.match(activityJournalMeta(298, 'il y a 2 s', 214), /214 relevés reçus$/);
});
