import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Overview, Pipeline, Source } from './controlPlane.ts';
import {
  attentionItems,
  deriveThroughput,
  destinationWatermark,
  heartbeat,
  lagVitals,
  observationRows,
  pipelineSectionView,
  plateauVerdict,
  resumeReadiness,
  resumptionView,
  retainedReason,
  runStateWord,
  sectionAlert,
  sourceAvailabilityFor,
  tableRows,
  tableTotals,
  transportFor,
  type CounterSample,
} from './liveBoard.ts';

const observedAt = '2026-09-13T09:00:00Z';
const THIN = ' ';

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

function sourceFixture(overrides: Partial<Source> = {}): Source {
  return {
    id: 'dev-source',
    environment: 'dev',
    evidenceKind: 'live',
    status: 'available',
    error: null,
    ...overrides,
  };
}

function overviewFixture(
  pipelines: readonly Pipeline[],
  sources: readonly Source[] = [sourceFixture()],
): Overview {
  return {
    revision: 3,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources,
  };
}

function readyState(overview: Overview): ControlPlaneState {
  return {
    status: 'ready',
    connection: 'live',
    overview,
    pipelines: overview.pipelines,
    lastSuccessAt: new Date(observedAt),
  };
}

function lagPoint(lag: number | null, coverage: 'complete' | 'gap' = 'complete') {
  return {
    startSeconds: 0,
    endSeconds: 5,
    low: lag,
    high: lag,
    lag,
    samples: lag === null ? 0 : 1,
    unknownSamples: lag === null ? 1 : 0,
    coverage,
    kind: 'observed' as const,
  };
}

/* ------------------------------------------------------------------ */
/* Débit dérivé                                                        */
/* ------------------------------------------------------------------ */

test('un seul relevé ne produit aucun débit', () => {
  const view = deriveThroughput(undefined, { events: 1793, at: observedAt });
  assert.equal(view.kind, 'single');
  assert.match(view.value, /Non calculable — un seul relevé/);
  assert.equal(view.window, null);
});

test('compteur absent → inconnu, jamais zéro', () => {
  const view = deriveThroughput(
    { events: 100, at: '2026-09-13T08:59:30Z' },
    { events: null, at: observedAt },
  );
  assert.equal(view.kind, 'unknown');
  assert.match(view.value, /Inconnu — compteur non publié/);
});

test('delta nul → 0 événement avec fenêtre affichée', () => {
  const view = deriveThroughput(
    { events: 1793, at: '2026-09-13T08:59:30Z' },
    { events: 1793, at: observedAt },
  );
  assert.equal(view.kind, 'idle');
  assert.equal(view.value, '0 événement');
  assert.equal(view.window, `fenêtre 30${THIN}s`);
});

test('débit mesuré = Δ events_published / Δ temps', () => {
  const view = deriveThroughput(
    { events: 1_000, at: '2026-09-13T08:59:30Z' },
    { events: 1_300, at: observedAt },
  );
  assert.equal(view.kind, 'measured');
  assert.equal(view.value, '10 év/s');
  assert.equal(view.window, `fenêtre 30${THIN}s`);
  assert.match(view.note ?? '', /Δ \+300/);
});

test('régression du compteur = redémarrage observé avec fenêtre', () => {
  const view = deriveThroughput(
    { events: 500_000, at: '2026-09-13T08:59:30Z' },
    { events: 12, at: observedAt },
  );
  assert.equal(view.kind, 'restarted');
  assert.match(view.value, /Redémarrage observé/);
  assert.equal(view.window, `fenêtre 30${THIN}s`);
  assert.match(view.note ?? '', /repris à 12/);
});

test('deux relevés au même instant ne divisent pas par zéro', () => {
  const view = deriveThroughput(
    { events: 100, at: observedAt },
    { events: 200, at: observedAt },
  );
  assert.equal(view.kind, 'unknown');
  assert.match(view.value, /même instant/);
});

test('compteur absent du relevé précédent borne le calcul', () => {
  const view = deriveThroughput(
    { events: null, at: '2026-09-13T08:59:30Z' },
    { events: 50, at: observedAt },
  );
  assert.equal(view.kind, 'unknown');
  assert.equal(view.window, `fenêtre 30${THIN}s`);
});

/* ------------------------------------------------------------------ */
/* État de run en un mot                                               */
/* ------------------------------------------------------------------ */

test('le run word garde le dernier état mesuré quand le relevé est retenu', () => {
  const word = runStateWord(pipelineFixture(), { retained: 'relevé conservé', current: false });
  // Le gel ne rend pas la vérité inconnue : le dernier mot mesuré reste
  // servi, désaturé — la raison de rétention vit dans le badge de portée
  // et l'âge dans le heartbeat.
  assert.equal(word.label, 'Capture active');
  assert.equal(word.tone, 'muted');
});

test('un relevé retenu sans exécution projetée reste Inconnu — raison nommée', () => {
  const word = runStateWord(
    pipelineFixture({
      stages: (['source', 'capture', 'raw', 'load', 'destination'] as const).map((id) => ({
        id,
        status: 'unknown' as const,
        observedAt: null,
        headline: 'non observée',
        detail: 'absente',
      })),
    }),
    { retained: 'relevé conservé', current: false },
  );
  assert.equal(word.label, 'Inconnu — exécution non projetée');
  assert.equal(word.tone, 'muted');
});

test('incident capture_stopped → arrêtée en sécurité, ton bleu-gris pas rouge', () => {
  const word = runStateWord(
    pipelineFixture({ incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' } }),
    { retained: null, current: true },
  );
  assert.equal(word.label, 'Arrêtée en sécurité');
  assert.equal(word.tone, 'muted');
});

test('phase LIVE mesurée → Capture active en positif', () => {
  const word = runStateWord(
    pipelineFixture({
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'LIVE', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: true },
  );
  assert.equal(word.label, 'Capture active');
  assert.equal(word.tone, 'positive');
});

test('même phase LIVE en démonstration reste bornée', () => {
  const word = runStateWord(
    pipelineFixture({
      quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'simulation' },
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'LIVE', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: false },
  );
  assert.equal(word.label, 'Capture active · démonstration');
  assert.equal(word.tone, 'muted');
});

test('planned_stop → En pause ; PAUSED runtime → En pause ; BLOCKED → Bloquée', () => {
  const paused = runStateWord(pipelineFixture({ status: 'planned_stop' }), {
    retained: null,
    current: true,
  });
  assert.equal(paused.label, 'En pause');

  const runtimePaused = runStateWord(
    pipelineFixture({
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'PAUSED', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: true },
  );
  assert.equal(runtimePaused.label, 'En pause');

  const blocked = runStateWord(
    pipelineFixture({
      fleetRuntime: {
        formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
        phase: 'BLOCKED', checkpoint: null, capabilities: {}, tableStates: [],
      },
    }),
    { retained: null, current: true },
  );
  assert.equal(blocked.label, 'Bloquée');
  assert.equal(blocked.tone, 'attention');
});

test('sans runtime, l étape capture suffit à dire Capture active', () => {
  const word = runStateWord(pipelineFixture(), { retained: null, current: true });
  assert.equal(word.label, 'Capture active');
  assert.equal(word.tone, 'positive');
});

test('sans runtime ni capture observée → Inconnu, jamais de succès', () => {
  const pipeline = pipelineFixture({
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'capture' ? { ...stage, status: 'unknown' as const } : stage,
    ),
  });
  const word = runStateWord(pipeline, { retained: null, current: true });
  assert.match(word.label, /^Inconnu — /);
});

/* ------------------------------------------------------------------ */
/* Rétention et disponibilité                                          */
/* ------------------------------------------------------------------ */

test('retainedReason ordonne gelé > conservé > fraîcheur > source', () => {
  const pipeline = pipelineFixture({ quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' } });
  const availability = sourceAvailabilityFor(pipeline, [sourceFixture()]);
  assert.equal(retainedReason(pipeline, 'frozen', availability), 'actualisation gelée');
  assert.equal(retainedReason(pipeline, 'retained', availability), 'relevé conservé');
  assert.equal(retainedReason(pipeline, 'live', availability), 'relevé trop ancien');
});

test('source indisponible ou partielle fige la lecture', () => {
  const pipeline = pipelineFixture();
  const down = sourceAvailabilityFor(pipeline, [sourceFixture({ status: 'unavailable' })]);
  assert.equal(down.kind, 'unavailable');
  assert.equal(retainedReason(pipeline, 'live', down), 'source indisponible');

  const partial = sourceAvailabilityFor(pipeline, [
    sourceFixture({ id: 'a' }),
    sourceFixture({ id: 'b', status: 'unavailable' }),
  ]);
  assert.equal(partial.kind, 'partial');
  assert.match(partial.label, /Partielle · 1\/2/);
  assert.equal(retainedReason(pipeline, 'live', partial), 'sources partiellement indisponibles');
});

test('aucune source déclarée pour l environnement → inconnue', () => {
  const availability = sourceAvailabilityFor(pipelineFixture(), []);
  assert.equal(availability.kind, 'unmapped');
  assert.match(availability.label, /aucune source déclarée/);
});

test('transportFor distingue live, relevé conservé et gel', () => {
  const state = readyState(overviewFixture([pipelineFixture()]));
  assert.equal(transportFor(state, false), 'live');
  assert.equal(transportFor(state, true), 'frozen');
  assert.equal(
    transportFor({ ...state, status: 'degraded', message: 'offline' }, false),
    'retained',
  );
  assert.equal(transportFor({ ...state, connection: 'reconnecting' }, false), 'retained');
});

/* ------------------------------------------------------------------ */
/* Grille des tables                                                   */
/* ------------------------------------------------------------------ */

function planTable(
  name: string,
  overrides: Record<string, unknown> = {},
): NonNullable<Pipeline['fleetPlan']>['tables'][number] {
  return {
    name,
    rowCount: 1000,
    dataSize: 10,
    journalImages: '*BOTH',
    identityStatus: 'keyed',
    identitySource: null,
    candidateKey: null,
    historicalAdmitted: true,
    historicalLane: null,
    blockedReasons: [],
    copiedRows: null,
    copiedBytes: null,
    historyProgress: null,
    ...overrides,
  };
}

function planPipeline(): Pipeline {
  return pipelineFixture({
    fleetPlan: {
      environment: 'TEST',
      sourceSchema: 'LEDGER',
      destinationNamespace: 'ACME_RAW.IBMI_TEST',
      observedAt,
      freshness: 'fresh',
      continuity: 'uncertain',
      liveBlocked: false,
      certificationBlocked: false,
      historyAdmitted: true,
      cutoverCheckpoint: { receiver: 'TRNJRN3776', sequence: 1 },
      cutoverRequiredBeforeHistory: true,
      journal: { library: 'LEDGER', name: 'TRNJRN', readerKind: 'multi_object', readerCount: 1 },
      identity: { keyedCount: 2, rrnCount: 0, blockedCount: 1, keyed: ['SALE', 'CAL001'], rrn: [], blocked: ['ADDRS1'] },
      observedTotals: { tableCount: 3, rowCount: 3000, dataSize: 30 },
      historical: { maxConcurrency: 2, byteBudget: null, admittedCount: 2, excludedCount: 1 },
      cost: { status: 'unknown', observed: null, unknownBecause: 'not measured' },
      tables: [
        planTable('ADDRS1', { identityStatus: 'blocked', blockedReasons: ['identity_unproven'] }),
        planTable('CAL001'),
        planTable('SALE'),
      ],
    },
    fleetRuntime: {
      formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
      phase: 'LIVE',
      checkpoint: { receiver: 'TRNJRN3776', sequence: 345_089_832 },
      capabilities: {},
      tableStates: [
        { name: 'ADDRS1', phase: 'BLOCKED', copiedRows: null, totalRows: null },
        { name: 'CAL001', phase: 'CERTIFIED', copiedRows: 1000, totalRows: 1000 },
        { name: 'SALE', phase: 'LIVE', copiedRows: 800, totalRows: 1000 },
      ],
    },
  });
}

test('les lignes fusionnent runtime et plan : problèmes en tête puis phase décroissante', () => {
  const rows = tableRows(planPipeline(), true);
  assert.equal(rows.length, 3);
  assert.equal(rows[0]!.name, 'ADDRS1');
  assert.equal(rows[0]!.problem, 'blocked');
  assert.match(rows[0]!.verdict.label, /Bloquée — identité non prouvée/);
  assert.equal(rows[1]!.name, 'CAL001');
  assert.equal(rows[1]!.verdict.label, 'Certifiée');
  assert.equal(rows[1]!.verdict.tone, 'positive');
  assert.equal(rows[2]!.name, 'SALE');
  assert.equal(rows[2]!.verdict.label, 'En temps réel');
  assert.equal(rows[2]!.progress, 0.8);
});

test('journal *BEFORE est un problème, pas une note de bas de page', () => {
  const pipeline = planPipeline();
  pipeline.fleetPlan!.tables[1]!.journalImages;
  const mutated = {
    ...pipeline,
    fleetPlan: {
      ...pipeline.fleetPlan!,
      tables: pipeline.fleetPlan!.tables.map((table) =>
        table.name === 'SALE' ? { ...table, journalImages: '*BEFORE' as const } : table,
      ),
    },
  };
  const rows = tableRows(mutated, true);
  assert.equal(rows[0]!.name, 'ADDRS1');
  assert.equal(rows[1]!.name, 'SALE');
  assert.equal(rows[1]!.problem, 'journal');
  assert.equal(rows[1]!.verdict.label, 'Images non exploitables');
});

test('hors mesure live, le vert disparaît des verdicts de table', () => {
  const rows = tableRows(planPipeline(), false);
  const certified = rows.find((row) => row.name === 'CAL001')!;
  assert.equal(certified.verdict.label, 'Certifiée');
  assert.equal(certified.verdict.tone, 'muted');
});

test('compteurs null → Inconnu, jamais zéro affiché ; le total catalogue reste distinct', () => {
  const rows = tableRows(planPipeline(), true);
  const blocked = rows[0]!;
  assert.equal(blocked.copiedRows, null);
  assert.equal(blocked.totalRows, 1000);
  assert.equal(blocked.progress, null);
});

test('phase absente du runtime mais table admise → inconnue honnête', () => {
  const pipeline = planPipeline();
  const mutated = {
    ...pipeline,
    fleetRuntime: {
      ...pipeline.fleetRuntime!,
      tableStates: pipeline.fleetRuntime!.tableStates.filter((t) => t.name !== 'SALE'),
    },
  };
  const rows = tableRows(mutated, true);
  const orphan = rows.find((row) => row.name === 'SALE')!;
  assert.equal(orphan.phase, null);
  assert.equal(orphan.verdict.label, 'Inconnu — phase non mesurée');
});

/* ------------------------------------------------------------------ */
/* Retard                                                              */
/* ------------------------------------------------------------------ */

test('lag null → inconnu ; trous de lag_series comptés honnêtement', () => {
  const pipeline = pipelineFixture({
    lagSeries: [
      lagPoint(4), lagPoint(3), lagPoint(null, 'gap'),
      { ...lagPoint(null), kind: 'temporal_gap' as const },
      lagPoint(2), lagPoint(2),
    ],
  });
  const view = lagVitals(pipeline);
  assert.match(view.value, /Inconnu — non mesuré/);
  assert.equal(view.gaps, '2 intervalles sans mesure');
});

test('lag mesuré garde le verdict canonique', () => {
  const pipeline = pipelineFixture({
    lagSequences: 2,
    lagSeries: [lagPoint(5), lagPoint(3), lagPoint(2), lagPoint(1)],
  });
  const view = lagVitals(pipeline);
  assert.match(view.value, /2 séquences de retard/);
  assert.equal(view.verdict, 'Rattrapage');
});

/* ------------------------------------------------------------------ */
/* Vue de section + verdict global + file d attention                  */
/* ------------------------------------------------------------------ */

test('la section expose checkpoint, débit et badges sans invention', () => {
  const pipeline = planPipeline();
  pipeline.counters = { events_published: 345_000 };
  const view = pipelineSectionView(pipeline, {
    sources: [sourceFixture()],
    transport: 'live',
    previous: { events: 340_000, at: '2026-09-13T08:59:30Z' } satisfies CounterSample,
  });
  assert.equal(view.retained, null);
  assert.equal(view.current, true);
  assert.equal(view.runWord.label, 'Capture active');
  assert.deepEqual(view.checkpoint, { receiver: 'TRNJRN3776', sequence: 345_089_832 });
  assert.match(view.throughput.value, /év\/s/);
  assert.equal(view.throughput.window, `fenêtre 30${THIN}s`);
  assert.equal(view.rows.length, 3);
});

test('checkpoint absent → raison honnête', () => {
  const view = pipelineSectionView(pipelineFixture(), {
    sources: [sourceFixture()],
    transport: 'live',
    previous: undefined,
  });
  assert.equal(view.checkpoint, null);
  assert.equal(view.checkpointMissingReason, 'exécution non projetée');
});

test('verdict global : ça avance / ça coince / on ne sait pas', () => {
  const healthy = pipelineFixture({
    fleetRuntime: {
      formatVersion: 'v1', fleetId: 'f', environment: 'dev', pipelineId: 'alpha',
      phase: 'LIVE', checkpoint: null, capabilities: {}, tableStates: [],
    },
  });
  const advancing = plateauVerdict(readyState(overviewFixture([healthy])), 'live');
  assert.equal(advancing.label, 'Ça avance');

  const incident = pipelineFixture({
    status: 'incident',
    incident: { code: 'destination_load_failed', type: 'destination' },
  });
  const stuck = plateauVerdict(readyState(overviewFixture([incident])), 'live');
  assert.equal(stuck.label, 'Ça coince');
  assert.match(stuck.detail, /alpha/);

  const unknown = plateauVerdict(readyState(overviewFixture([pipelineFixture()])), 'retained');
  assert.equal(unknown.label, 'On ne sait pas');

  const loading = plateauVerdict(
    { status: 'loading', connection: 'connecting' },
    'retained',
  );
  assert.equal(loading.label, 'On ne sait pas');
  assert.match(loading.detail, /premier relevé/);
});

/* ------------------------------------------------------------------ */
/* Heartbeat, bandeau, watermark, totaux, journal                        */
/* ------------------------------------------------------------------ */

test('heartbeat mesure le canal et l âge du relevé, jamais « en direct »', () => {
  const now = new Date('2026-09-13T09:00:30Z');
  const beat = heartbeat(pipelineFixture(), 'live', now);
  assert.match(beat.measure, /^relevé il y a 30\ss$/);
  assert.equal(beat.channel, 'canal de mesure ouvert');
  assert.equal(beat.stale, false);
  assert.equal(beat.tone, 'active');

  const stale = heartbeat(
    pipelineFixture({ quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' } }),
    'live', now,
  );
  assert.match(stale.measure, /^relevés figés depuis il y a 30\ss$/);
  assert.equal(stale.channel, 'canal de mesure ouvert');
  assert.equal(stale.tone, 'attention');

  // Un relevé frais déclaré mais vieux de plus de 120 s se lit figé — honnête
  const old = heartbeat(pipelineFixture(), 'live', new Date('2026-09-13T09:05:00Z'));
  assert.match(old.measure, /relevés figés depuis/);

  assert.equal(heartbeat(pipelineFixture(), 'retained', now).channel, 'canal de mesure interrompu');
  assert.match(heartbeat(pipelineFixture(), 'frozen', now).channel, /suspendu/);
});

test('heartbeat : la révision servie prouve la boucle de lecture', () => {
  const now = new Date('2026-09-13T09:00:30Z');
  const generated = { revision: 41, generatedAt: '2026-09-13T09:00:22Z', received: 12 };
  const beat = heartbeat(pipelineFixture(), 'live', now, generated);
  // « révision 41 · générée il y a 8 s · 12 relevés reçus » — mesuré, la
  // boucle est démontrée par le compteur vivant, pas par le jargon « canal »
  assert.match(beat.channel, /^révision 41 · générée il y a 8\ss · 12 relevés reçus$/);
  // Canal fermé : la révision reste affichée (dernier relevé lu), l'interruption le dit
  const closed = heartbeat(pipelineFixture(), 'retained', now, generated);
  assert.match(closed.channel, /révision 41 · générée il y a 8\ss · canal de mesure interrompu/);
  // Affichage suspendu par l'opérateur : les relevés reçus restent visibles, la suspension est dite
  const frozen = heartbeat(pipelineFixture(), 'frozen', now, generated);
  assert.match(frozen.channel, /12 relevés reçus · affichage suspendu/);
  // Un seul relevé reçu : accord au singulier
  const first = heartbeat(pipelineFixture(), 'live', now, { ...generated, received: 1 });
  assert.match(first.channel, /1 relevé reçu$/);
});

test('sectionAlert traduit l incident en français avec remède guidance', () => {
  const alert = sectionAlert(
    pipelineFixture({ incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' } }),
    { transport: 'live', retained: null },
  );
  assert.equal(alert.severity, 'incident');
  assert.equal(alert.locus, 'Lecture');
  assert.equal(alert.headline, 'Authentification AS400 bloquée');
  assert.match(alert.cause, /la source refuse la connexion/);
  assert.match(alert.remedy, /Réparer le compte AS400 puis relancer/);
  assert.equal(alert.observedAt, observedAt);
  // L'incident destination nomme la destination, pas la lecture
  const destination = sectionAlert(
    pipelineFixture({ incident: { code: 'destination_reconciliation_mismatch', type: 'destination' } }),
    { transport: 'live', retained: null },
  );
  assert.equal(destination.locus, 'Destination');
});

test('sectionAlert : un seul bandeau — incident d abord, sinon relevé dégradé', () => {
  assert.equal(
    sectionAlert(pipelineFixture(), { transport: 'live', retained: null }),
    null,
  );
  // L affichage suspendu par l opérateur n est pas une alerte
  assert.equal(
    sectionAlert(pipelineFixture(), { transport: 'frozen', retained: 'actualisation gelée' }),
    null,
  );
  const stale = sectionAlert(
    pipelineFixture(),
    { transport: 'live', retained: 'relevé trop ancien' },
  );
  assert.equal(stale.severity, 'degrade');
  assert.equal(stale.locus, null);
  assert.match(stale.cause, /relevé trop ancien/);
  const cached = sectionAlert(
    pipelineFixture(),
    { transport: 'retained', retained: 'relevé conservé' },
  );
  assert.match(cached.cause, /canal de mesure est interrompu/);
});

test('resumptionView : reprise sans faux espoir, flux mesuré, dernier débit ou non mesuré', () => {
  const pipeline = planPipeline();
  pipeline.counters = { events_published: 345_000, events_in_target: 342_580 };
  const view = resumptionView(pipeline, { value: '10 év/s', at: '2026-09-12T12:00:00Z' });
  assert.match(view.retry, /aucune nouvelle tentative observée — intervention requise/);
  assert.match(view.flow, /journal TRNJRN3776\s·\s345\s089\s832 → 345\s000 publiés → 342\s580 à la destination/);
  assert.match(view.lastRate, /10 év\/s/);

  const bare = resumptionView(pipelineFixture(), null);
  assert.match(bare.flow, /journal non mesuré → publiés non mesurés → destination non mesurée/);
  assert.match(bare.lastRate, /non mesuré/);
});

test('la vue de section expose la boucle de reprise seulement sur incident', () => {
  const incident = pipelineFixture({ incident: { code: 'x', type: 'capture_auth_blocked' } });
  const view = pipelineSectionView(incident, {
    sources: [sourceFixture()],
    transport: 'live',
    previous: undefined,
  });
  assert.notEqual(view.resume, null);
  const healthy = pipelineSectionView(pipelineFixture(), {
    sources: [sourceFixture()],
    transport: 'live',
    previous: undefined,
  });
  assert.equal(healthy.resume, null);
});

test('destinationWatermark : compteur constaté, namespace seul, ou non instrumenté', () => {
  const pipeline = planPipeline();
  pipeline.counters = { events_in_target: 342_580 };
  const watermark = destinationWatermark(pipeline);
  assert.match(watermark.value, /342\s580 événements constatés/);
  assert.match(watermark.hint, /ACME_RAW\.IBMI_TEST · cumul constaté — pas une réconciliation/);

  pipeline.counters = {};
  const nsOnly = destinationWatermark(pipeline);
  assert.equal(nsOnly.value, 'ACME_RAW.IBMI_TEST');
  assert.equal(nsOnly.hint, 'compteur de livraison non servi');

  const bare = destinationWatermark(pipelineFixture());
  assert.equal(bare.value, 'Destination · non datée');
});

test('tableTotals : compte catalogue, copiées mesurées et problèmes, sans invention', () => {
  const rows = tableRows(planPipeline(), true);
  const totals = tableTotals(planPipeline(), rows);
  assert.equal(totals.tableCount, 3);
  assert.equal(totals.cataloguedRows, 3000);
  assert.equal(totals.copiedRows, 1800);
  assert.equal(totals.problems, 1);

  const empty = tableTotals(pipelineFixture(), []);
  assert.equal(empty.tableCount, 0);
  assert.equal(empty.cataloguedRows, null);
  assert.equal(empty.copiedRows, null);
});

test('observationRows : relevé, catalogue et étapes déduits — rien d inventé', () => {
  const rows = observationRows(planPipeline());
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!.kind, 'releve');
  assert.match(rows[0]!.label, /Relevé lu — État borné au snapshot/);
  assert.equal(rows[1]!.kind, 'catalogue');
  assert.match(rows[1]!.label, /Catalogue observé — 3\s000 lignes sur 3 tables · continuité incertaine/);

  const degraded = pipelineFixture({
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'destination' ? { ...stage, status: 'unknown' as const, observedAt: null } : stage,
    ),
  });
  const labels = observationRows(degraded).map((row) => row.label);
  assert.ok(labels.some((label) => /Destination — jamais observée/.test(label)));
});

test('la cellule État fusionnée ne répète le verdict que s il diverge', () => {
  const rows = tableRows(planPipeline(), true);
  const blocked = rows.find((row) => row.name === 'ADDRS1')!;
  assert.equal(blocked.state.primary, 'Bloquée — identité non prouvée');
  assert.equal(blocked.state.secondary, null);
  const certified = rows.find((row) => row.name === 'CAL001')!;
  assert.equal(certified.state.primary, 'Certifiée');
  assert.equal(certified.state.secondary, null);
});

test('volumeShare : la barre encode le volume relatif de la plus grande table', () => {
  const pipeline = planPipeline();
  const mutated = {
    ...pipeline,
    fleetRuntime: {
      ...pipeline.fleetRuntime!,
      tableStates: [
        { name: 'ADDRS1', phase: 'BLOCKED' as const, copiedRows: null, totalRows: 2000 },
        { name: 'CAL001', phase: 'CERTIFIED' as const, copiedRows: 500, totalRows: 1000 },
        { name: 'SALE', phase: 'LIVE' as const, copiedRows: 500, totalRows: 500 },
      ],
    },
  };
  const rows = tableRows(mutated, true);
  assert.equal(rows.find((row) => row.name === 'ADDRS1')!.volumeShare, 1);
  assert.equal(rows.find((row) => row.name === 'CAL001')!.volumeShare, 0.5);
  assert.equal(rows.find((row) => row.name === 'SALE')!.volumeShare, 0.25);
});

test('la file d attention épingle les ruptures, jamais les pipelines sains', () => {
  const healthy = pipelineFixture({ id: 'sain' });
  const broken = pipelineFixture({
    id: 'en-rupture',
    stages: pipelineFixture().stages.map((stage) =>
      stage.id === 'destination' ? { ...stage, status: 'unknown' as const, observedAt: null } : stage,
    ),
  });
  const items = attentionItems(readyState(overviewFixture([healthy, broken])));
  assert.equal(items.length, 1);
  assert.equal(items[0]!.pipeline.id, 'en-rupture');
  assert.match(items[0]!.cause, /./);
});

/* ------------------------------------------------------------------ */
/* « Prête à reprendre » — cause résolue prouvée, capture parquée       */
/* ------------------------------------------------------------------ */

function authBlockedPipeline(overrides: Partial<Pipeline> = {}): Pipeline {
  const base = planPipeline();
  return {
    ...base,
    status: 'incident',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' },
    fleetPlan: { ...base.fleetPlan!, continuity: 'proven' as const, freshness: 'fresh' as const },
    stages: base.stages.map((stage) =>
      stage.id === 'capture'
        ? { ...stage, status: 'incident' as const, headline: 'Capture arrêtée en sécurité' }
        : stage,
    ),
    ...overrides,
  };
}

test('reprise prête : incident récupérable + plan frais + continuité prouvée', () => {
  assert.equal(resumeReadiness(authBlockedPipeline()), 'ready');
});

test('reprise refusée : relevé de plan trop ancien ne prouve rien', () => {
  const pipeline = authBlockedPipeline();
  const stale = {
    ...pipeline,
    fleetPlan: { ...pipeline.fleetPlan!, freshness: 'stale' as const },
  };
  assert.equal(resumeReadiness(stale), null);
});

test('reprise refusée : continuité non prouvée ne qualifie pas', () => {
  const pipeline = authBlockedPipeline();
  const uncertain = {
    ...pipeline,
    fleetPlan: { ...pipeline.fleetPlan!, continuity: 'uncertain' as const },
  };
  assert.equal(resumeReadiness(uncertain), null);
});

test('reprise refusée : une capture qui tourne ne se qualifie pas', () => {
  const pipeline = authBlockedPipeline({ status: 'healthy' });
  assert.equal(resumeReadiness(pipeline), null);
});

test('reprise refusée : un incident destination n est pas récupérable par la sonde', () => {
  const pipeline = authBlockedPipeline({
    incident: { code: 'destination_apply_failed', type: 'destination' },
  });
  assert.equal(resumeReadiness(pipeline), null);
});

test('reprise prête : le drapeau serveur cause_resolved suffit sans plan', () => {
  const pipeline = authBlockedPipeline({
    fleetPlan: null,
    incident: {
      code: 'capture_stopped_fail_closed',
      type: 'capture_auth_blocked',
      causeResolved: true,
      causeResolvedObservedAt: '2026-09-21T14:07:49Z',
    },
  });
  assert.equal(resumeReadiness(pipeline), 'ready');
});

test('reprise prête : le bloc resume servi prime sur la dérivation', () => {
  const pipeline = authBlockedPipeline({
    fleetPlan: null,
    status: 'awaiting_resume',
    resume: {
      state: 'ready',
      authObservedAt: '2026-09-21T14:07:49Z',
      checkpoint: { receiver: 'DEMOJRN4114', sequence: 345_089_832 },
      tail: { receiver: 'DEMOJRN4158', sequence: 120_332_519 },
      backlogSequences: 77_777,
      backlogReceivers: 44,
    },
  });
  assert.equal(resumeReadiness(pipeline), 'ready');
});

test('alerte prête : cause résolue en titre, jamais incident actif', () => {
  const alert = sectionAlert(authBlockedPipeline(), { transport: 'live', retained: null });
  assert.equal(alert?.ready, true);
  assert.equal(alert?.headline, 'Cause résolue');
  assert.equal(alert?.tone, 'attention');
  assert.match(alert?.cause ?? '', /accepte de nouveau la connexion/);
});

test('alerte incident actif : inchangée quand la cause n est pas résolue', () => {
  const pipeline = authBlockedPipeline({
    fleetPlan: { ...authBlockedPipeline().fleetPlan!, freshness: 'stale' as const },
  });
  const alert = sectionAlert(pipeline, { transport: 'live', retained: null });
  assert.equal(alert?.ready, false);
});

test('le mot de run prête à reprendre ne devient jamais un incident rouge', () => {
  const word = runStateWord(authBlockedPipeline(), { retained: null, current: false });
  assert.equal(word.label, 'Prête à reprendre');
  assert.equal(word.tone, 'attention');
});

test('vue reprise prête : point d arrêt mesuré, pas de ligne de débit vide', () => {
  const view = resumptionView(authBlockedPipeline(), null);
  assert.equal(view.retry, null);
  assert.match(view.flow, /Repartir de TRNJRN3776/);
  assert.equal(view.lastRate, null);
});

test('vue reprise prête : backlog mesuré servi tel quel', () => {
  const pipeline = authBlockedPipeline({
    resume: {
      state: 'ready',
      authObservedAt: '2026-09-21T14:07:49Z',
      checkpoint: { receiver: 'DEMOJRN4114', sequence: 345_089_832 },
      tail: { receiver: 'DEMOJRN4158', sequence: 120_332_519 },
      backlogSequences: 77_777,
      backlogReceivers: 44,
    },
  });
  const view = resumptionView(pipeline, null);
  assert.match(view.flow, new RegExp(`77${THIN}777 séquences à relire`));
});

test('plateau : une source prête à reprendre dit À reprendre, pas Ça coince', () => {
  const state = readyState(overviewFixture([authBlockedPipeline()]));
  const verdict = plateauVerdict(state, 'live');
  assert.equal(verdict.label, 'À reprendre');
  assert.match(verdict.detail, /cause résolue/);
});
