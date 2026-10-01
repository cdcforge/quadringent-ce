import assert from 'node:assert/strict';
import { test } from 'node:test';

import type {
  DestinationProjection,
  FluxIdentity,
  JournalCheckpoint,
  JournalPosition,
  Pipeline,
  RunProjection,
} from './controlPlane.ts';
import {
  NOT_INSTRUMENTED,
  checkpointGlyph,
  clockUtc,
  destinationApplyStamp,
  destinationDetailSections,
  destinationReasonLabel,
  destinationRows,
  destinationStamp,
  fluxIdentityRows,
  fluxObjectCoverage,
  positionGlyph,
  positionRows,
  projectedLagVerdict,
  runDetailRows,
  runStateLabel,
  stageChain,
} from './sourceDetail.ts';

const observedAt = '2026-09-13T09:00:00Z';
const now = new Date('2026-09-13T09:00:30Z');
const THIN = ' ';

function pipelineFixture(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id: 'dev-cntr',
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: 'État borné au relevé',
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

function positionFixture(overrides: Partial<JournalPosition> = {}): JournalPosition {
  return {
    checkpoint: { receiver: 'TRNJRN3776', sequence: 41 },
    sourceTail: { receiver: 'TRNJRN3776', sequence: 1337 },
    receiverFirstSequence: 40,
    receiverLastSequence: 1337,
    ...overrides,
  };
}

function fluxFixture(overrides: Partial<FluxIdentity> = {}): FluxIdentity {
  return {
    id: 'dev-cntr',
    label: 'CNTR',
    journal: 'QJRN',
    journalLibrary: 'ACME_LIB',
    objects: ['CNTR', 'ACME.CLIENTS'],
    readerPath: 'journal',
    target: 'snowflake',
    job: 'CDCREADER1',
    ...overrides,
  };
}

function runFixture(overrides: Partial<RunProjection> = {}): RunProjection {
  return {
    state: 'RUNNING',
    startedAt: '2026-09-13T08:00:00Z',
    elapsedSeconds: 3600,
    stoppedBecause: null,
    diagnostic: { type: 'budget', head: 'quota atteint', at: '2026-09-13T08:30:00Z' },
    sourcePause: { retryAfter: '2026-09-13T10:00:00Z', reasonCode: 'journal_wrap' },
    ...overrides,
  };
}

function destinationFixture(overrides: Partial<DestinationProjection> = {}): DestinationProjection {
  return {
    kind: 'snowflake',
    database: 'ACME_RAW',
    schema: 'DEV',
    stage: 'QS_STAGE',
    rawTable: 'CNTR_RAW',
    canonicalTable: 'CNTR',
    runTag: 'run-41',
    observedAt: '2026-09-13T09:42:13Z',
    loadCheckpoint: { receiver: 'snowpipe', sequence: 900 },
    applyCheckpoint: { receiver: 'snowpipe', sequence: 901 },
    sourceEvents: 120,
    rawRows: 120,
    canonicalRows: 118,
    duplicates: 2,
    ...overrides,
  };
}

/* ------------------------------------------------------------------ */
/* Horodatage et glyphes — HH:MM:SS UTC, jamais d'heure locale          */
/* ------------------------------------------------------------------ */

test('clockUtc renders the UTC wall clock, never a local time', () => {
  assert.equal(clockUtc('2026-09-13T09:42:13Z'), '09:42:13');
  assert.equal(clockUtc('2026-09-13T23:59:59+00:00'), '23:59:59');
});

test('checkpointGlyph joins receiver and sequence in canonical form', () => {
  assert.equal(checkpointGlyph({ receiver: 'TRNJRN3776', sequence: 41 }), 'TRNJRN3776:41');
  assert.equal(checkpointGlyph({ receiver: 'TRNJRN3776', sequence: 1337 }), `TRNJRN3776:1${THIN}337`);
});

/* ------------------------------------------------------------------ */
/* Glyphe position — absent / partiel / mesuré, trois cas distincts     */
/* ------------------------------------------------------------------ */

test('position glyph is uninstrumented when the extension is absent', () => {
  const glyph = positionGlyph(pipelineFixture());
  assert.equal(glyph.instrumented, false);
  assert.equal(glyph.value, NOT_INSTRUMENTED);
  assert.match(glyph.reason ?? '', /non mesurée/);
  assert.equal(glyph.window, null);
});

test('position glyph uses the runtime checkpoint without inventing a tail', () => {
  const checkpoint: JournalCheckpoint = { receiver: 'TRNJRN3776', sequence: 41 };
  const glyph = positionGlyph(pipelineFixture({
    fleetRuntime: { checkpoint } as Pipeline['fleetRuntime'],
  }));
  assert.equal(glyph.instrumented, false);
  assert.equal(glyph.value, 'TRNJRN3776:41 → fin inconnue');
  assert.match(glyph.reason ?? '', /fin du journal non mesurée/);
});

test('position glyph renders the served checkpoint→tail form with the receiver window', () => {
  const glyph = positionGlyph(pipelineFixture({ position: positionFixture() }));
  assert.equal(glyph.instrumented, true);
  assert.equal(glyph.value, `TRNJRN3776:41 → TRNJRN3776:1${THIN}337`);
  assert.equal(glyph.window, `séquences 40 → 1${THIN}337`);
  assert.equal(glyph.reason, null);
});

test('position glyph names a missing bound instead of completing it', () => {
  const glyph = positionGlyph(pipelineFixture({
    position: positionFixture({ sourceTail: null, receiverFirstSequence: null, receiverLastSequence: null }),
  }));
  assert.equal(glyph.instrumented, true);
  assert.equal(glyph.value, 'TRNJRN3776:41 → fin inconnue');
  assert.equal(glyph.window, null);
  assert.equal(glyph.reason, 'borne manquante dans le relevé');
});

test('position glyph stays honest when no bound is published', () => {
  const glyph = positionGlyph(pipelineFixture({
    position: positionFixture({ checkpoint: null, sourceTail: null }),
  }));
  assert.equal(glyph.instrumented, true);
  assert.equal(glyph.value, 'Inconnu — bornes non publiées');
});

test('position rows distinguish absent extension, runtime fallback and served bounds', () => {
  const absent = positionRows(pipelineFixture());
  assert.equal(absent.length, 3);
  assert.equal(absent[0]?.[0], 'Checkpoint de lecture');
  assert.equal(absent[0]?.[1], 'Inconnu — exécution non projetée');
  assert.match(absent[1]?.[1] ?? '', /Non mesurée — mesure non publiée par le service/);

  const runtime = positionRows(pipelineFixture({
    fleetRuntime: { checkpoint: { receiver: 'TRNJRN3776', sequence: 41 } } as Pipeline['fleetRuntime'],
  }));
  assert.match(runtime[0]?.[1] ?? '', /TRNJRN3776\s·\s41 \(runtime\)/);

  const served = positionRows(pipelineFixture({ position: positionFixture() }));
  assert.equal(served.length, 4);
  assert.equal(served[1]?.[0], 'Fin du journal');
  assert.match(served[1]?.[1] ?? '', /TRNJRN3776\s·\s1\s?337/);
  assert.equal(served[2]?.[1], '40');
});

/* ------------------------------------------------------------------ */
/* Tampon apply — la seule preuve verte de livraison                    */
/* ------------------------------------------------------------------ */

test('apply stamp is uninstrumented when the extension is absent', () => {
  const stamp = destinationApplyStamp(pipelineFixture());
  assert.equal(stamp.measured, false);
  assert.equal(stamp.label, 'date non servie');
  assert.equal(stamp.reason, 'mesure non publiée par le service');
  assert.equal(stamp.at, null);
});

test('apply stamp cites the declared reason when the block is detached', () => {
  const stamp = destinationApplyStamp(pipelineFixture({
    destination: null,
    destinationReason: 'destination_not_attached',
  }));
  assert.equal(stamp.measured, false);
  assert.equal(stamp.label, 'date non servie');
  assert.equal(stamp.reason, 'destination non rattachée au relevé');
});

test('apply stamp never turns a served block without observation into an arrival', () => {
  const stamp = destinationApplyStamp(pipelineFixture({
    destination: destinationFixture({ observedAt: null }),
  }));
  assert.equal(stamp.measured, false);
  assert.equal(stamp.label, 'date non observée');
  assert.match(stamp.reason ?? '', /sans date d’application/);
});

test('apply stamp renders the measured apply clock — the only green proof', () => {
  const stamp = destinationApplyStamp(pipelineFixture({ destination: destinationFixture() }));
  assert.equal(stamp.measured, true);
  assert.equal(stamp.label, 'livré 09:42:13');
  assert.equal(stamp.at, '2026-09-13T09:42:13Z');
});

test('destination stamp is green only on a measured apply within current evidence', () => {
  const pipeline = pipelineFixture({
    destination: destinationFixture(),
    fleetPlan: { destinationNamespace: 'ACME_RAW.DEV' } as Pipeline['fleetPlan'],
  });
  const current = destinationStamp(pipeline, true);
  assert.equal(current.value, 'livré 09:42:13');
  assert.equal(current.tone, 'positive');
  assert.match(current.hint ?? '', /ACME_RAW\.DEV/);
  assert.match(current.hint ?? '', /observation destination/);

  const retained = destinationStamp(pipeline, false);
  assert.equal(retained.tone, 'muted');
});

test('destination stamp cites the window counter without calling it an arrival', () => {
  const pipeline = pipelineFixture({
    counters: { events_in_target: 120 },
    fleetPlan: { destinationNamespace: 'ACME_RAW.DEV' } as Pipeline['fleetPlan'],
  });
  const stamp = destinationStamp(pipeline, true);
  assert.equal(stamp.value, `120 événements constatés`);
  assert.match(stamp.hint ?? '', /date non servie/);
  assert.match(stamp.hint ?? '', /pas une réconciliation/);
  assert.equal(stamp.tone, 'active');
});

test('destination stamp stays uninstrumented without apply, counter or namespace', () => {
  const stamp = destinationStamp(pipelineFixture(), true);
  assert.equal(stamp.value, 'Destination · non datée');
  assert.equal(stamp.hint, 'mesure non publiée par le service');
  assert.equal(stamp.tone, 'muted');
});

test('destination reason label maps known reasons and passes unknowns through', () => {
  assert.equal(destinationReasonLabel(undefined), 'raison non déclarée');
  assert.equal(destinationReasonLabel(null), 'raison non déclarée');
  assert.equal(destinationReasonLabel('destination_not_attached'), 'destination non rattachée au relevé');
  assert.equal(destinationReasonLabel('reason_inconnue'), 'reason_inconnue');
});

/* ------------------------------------------------------------------ */
/* Chaîne des cinq étapes — apply uniquement sur destination            */
/* ------------------------------------------------------------------ */

test('stage chain carries five steps and apply lives only on destination', () => {
  const chain = stageChain(pipelineFixture({ destination: destinationFixture() }), now, true);
  assert.equal(chain.length, 5);
  for (const step of chain) {
    if (step.id === 'destination') {
      assert.equal(step.apply, 'livré 09:42:13');
      assert.equal(step.tone, 'positive');
    } else {
      assert.equal(step.apply, null);
      assert.equal(step.tone, 'active');
    }
  }
  const retained = stageChain(pipelineFixture({ destination: destinationFixture() }), now, false);
  assert.equal(retained[4]?.tone, 'muted');
  assert.equal(retained[0]?.tone, 'muted');
  // Relevé figé : une étape saine est observée, pas disponible à l'instant présent.
  for (const step of retained) {
    assert.equal(step.statusLabel, 'Observée', `étape ${step.id}`);
  }
});

test('stage chain marks incidents, degradations and never-observed stages honestly', () => {
  const pipeline = pipelineFixture({
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'ok' },
      { id: 'capture', status: 'incident', observedAt, headline: 'Capture interrompue', detail: 'timeout' },
      { id: 'raw', status: 'degraded', observedAt, headline: 'Raw partiel', detail: 'trous' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'Chargement non observé', detail: 'absent' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'Destination non observée', detail: 'absent' },
    ],
  });
  const chain = stageChain(pipeline, now, true);
  assert.equal(chain[1]?.tone, 'attention');
  assert.equal(chain[2]?.tone, 'attention');
  assert.equal(chain[3]?.tone, 'muted');
  assert.equal(chain[3]?.ageLabel, 'jamais observée');
  assert.equal(chain[3]?.statusLabel, 'Non disponible');
  assert.equal(chain[4]?.apply, null);
});

/* ------------------------------------------------------------------ */
/* Verdict lag projeté — déclaré par le worker, jamais déduit           */
/* ------------------------------------------------------------------ */

test('lag verdict is uninstrumented when neither verdict nor reason is served', () => {
  const verdict = projectedLagVerdict(pipelineFixture());
  assert.equal(verdict.instrumented, false);
  assert.equal(verdict.label, NOT_INSTRUMENTED);
  assert.equal(verdict.reason, 'mesure non publiée par le service');
});

test('lag verdict passes the declared verdict through the public allowlist', () => {
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'STABLE' })).label, 'Stable');
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'BOUNDED' })).label, 'Borné');
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'CATCHING_UP' })).label, 'Rattrapage');
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'DIVERGING' })).label, 'Divergence');
  // verdict hors allowlist : pass-through brut, jamais reformulé
  assert.equal(projectedLagVerdict(pipelineFixture({ lagVerdict: 'WILD' })).label, 'WILD');
});

test('lag verdict without a verdict cites the declared reason', () => {
  const declared = projectedLagVerdict(pipelineFixture({
    lagVerdict: null,
    lagVerdictReason: 'verdict_not_declared',
  }));
  assert.equal(declared.instrumented, true);
  assert.equal(declared.label, 'Indéterminé');
  assert.equal(declared.reason, 'verdict non déclaré par le worker');

  const raw = projectedLagVerdict(pipelineFixture({ lagVerdict: null, lagVerdictReason: 'weird' }));
  assert.equal(raw.reason, 'weird');

  const silent = projectedLagVerdict(pipelineFixture({ lagVerdict: null }));
  assert.equal(silent.reason, 'raison non déclarée');
});

/* ------------------------------------------------------------------ */
/* Exécution projetée — états mappés, jamais d'état inventé             */
/* ------------------------------------------------------------------ */

test('run state labels map the contract and pass unknowns through', () => {
  assert.equal(runStateLabel(null), 'Inconnu — état non publié');
  assert.equal(runStateLabel('RUNNING'), 'En cours');
  assert.equal(runStateLabel('PAUSED_SOURCE'), 'Pause source');
  assert.equal(runStateLabel('STOPPED_BUDGET'), 'Arrêt budget');
  assert.equal(runStateLabel('STOPPED_PROOF_CHAIN'), 'Arrêt — fenêtres clôturées');
  assert.equal(runStateLabel('STOPPED_FAIL_CLOSED'), 'Arrêt en sécurité');
  assert.equal(runStateLabel('STOPPED_AUTH_BLOCKED'), 'Authentification bloquée');
  assert.equal(runStateLabel('FUTURE_STATE'), 'FUTURE_STATE');
});

test('run detail rows are uninstrumented when the block is absent', () => {
  const rows = runDetailRows(pipelineFixture());
  assert.equal(rows.length, 6);
  assert.match(rows[0]?.[1] ?? '', /Non mesuré — mesure non publiée par le service/);
  assert.ok(rows.slice(1).every(([, value]) => /^Non mesuré/.test(value)));
});

test('run detail rows project measured members and cite absent ones', () => {
  const rows = runDetailRows(pipelineFixture({ run: runFixture() }));
  const map = new Map(rows);
  assert.equal(map.get('État du run'), 'En cours');
  assert.match(map.get('Démarrage') ?? '', /13\/09\/2026/);
  assert.equal(map.get('Durée mesurée'), `1${THIN}h`);
  assert.equal(map.get('Cause d’arrêt'), 'Aucune déclarée');
  assert.match(map.get('Dernier diagnostic') ?? '', /type budget/);
  assert.match(map.get('Dernier diagnostic') ?? '', /quota atteint/);
  assert.match(map.get('Pause source') ?? '', /code journal_wrap/);

  const minimal = runDetailRows(pipelineFixture({
    run: runFixture({ startedAt: null, elapsedSeconds: null, diagnostic: null, sourcePause: null }),
  }));
  const minimalMap = new Map(minimal);
  assert.equal(minimalMap.get('Démarrage'), 'Inconnu — non mesuré');
  assert.equal(minimalMap.get('Durée mesurée'), 'Inconnue — non mesurée');
  assert.equal(minimalMap.get('Dernier diagnostic'), 'Aucun diagnostic publié');
  assert.equal(minimalMap.get('Pause source'), 'Aucune pause source déclarée');
});

/* ------------------------------------------------------------------ */
/* Identité du flux et couverture des tables                            */
/* ------------------------------------------------------------------ */

test('flux identity rows are uninstrumented when the block is absent', () => {
  const rows = fluxIdentityRows(pipelineFixture());
  assert.equal(rows.length, 6);
  assert.match(rows[0]?.[1] ?? '', /Non mesuré — mesure non publiée par le service/);
});

test('flux identity rows join served members and cite unpublished ones', () => {
  const rows = fluxIdentityRows(pipelineFixture({ flux: fluxFixture() }));
  const map = new Map(rows);
  assert.equal(map.get('Flux'), 'CNTR · dev-cntr');
  assert.equal(map.get('Journal'), 'ACME_LIB/QJRN');
  assert.equal(map.get('Objets déclarés'), 'CNTR, ACME.CLIENTS');
  assert.equal(map.get('Chemin de lecture'), 'journal');
  assert.equal(map.get('Cible'), 'snowflake');
  assert.equal(map.get('Job'), 'CDCREADER1');

  const sparse = fluxIdentityRows(pipelineFixture({
    flux: fluxFixture({ journalLibrary: null, objects: null, job: null }),
  }));
  const sparseMap = new Map(sparse);
  assert.equal(sparseMap.get('Journal'), 'QJRN');
  assert.equal(sparseMap.get('Objets déclarés'), 'Inconnus — non publiés');
  assert.equal(sparseMap.get('Job'), 'Inconnu — non publié');

  const empty = fluxIdentityRows(pipelineFixture({ flux: fluxFixture({ objects: [] }) }));
  assert.equal(new Map(empty).get('Objets déclarés'), 'Aucun objet déclaré');
});

test('flux object coverage distinguishes absent, unpublished, covered and outside tables', () => {
  assert.match(fluxObjectCoverage(undefined, 'CNTR'), /Non mesurée — identité de la liaison absente/);
  assert.match(fluxObjectCoverage(null, 'CNTR'), /Non mesurée — identité de la liaison absente/);
  assert.match(fluxObjectCoverage(fluxFixture({ objects: null }), 'CNTR'), /contenu de la liaison non publié/);

  const flux = fluxFixture();
  assert.equal(fluxObjectCoverage(flux, 'CNTR'), 'Déclarée dans la liaison');
  assert.equal(fluxObjectCoverage(flux, 'cntr'), 'Déclarée dans la liaison');
  assert.equal(fluxObjectCoverage(flux, 'CLIENTS'), 'Déclarée dans la liaison'); // ACME.CLIENTS
  assert.equal(fluxObjectCoverage(flux, 'DEVIS'), 'Hors périmètre déclaré');
});

/* ------------------------------------------------------------------ */
/* Destination — registre L3, comptes cités comme mesurés               */
/* ------------------------------------------------------------------ */

test('destination rows are uninstrumented when the extension is absent', () => {
  const rows = destinationRows(pipelineFixture());
  const map = new Map(rows);
  assert.match(map.get('Bloc destination') ?? '', /Non mesurée — mesure non publiée par le service/);
  assert.ok(rows.slice(1).every(([, value]) => /^Non mesuré/.test(value)));
});

test('destination rows cite the detach reason when the block is null', () => {
  const rows = destinationRows(pipelineFixture({
    destination: null,
    destinationReason: 'destination_not_attached',
  }));
  const map = new Map(rows);
  assert.match(map.get('Bloc destination') ?? '', /Non rattachée — destination non rattachée au relevé/);
});

test('destination rows project measured members and cite unmeasured ones', () => {
  const rows = destinationRows(pipelineFixture({ destination: destinationFixture() }));
  const map = new Map(rows);
  assert.match(map.get('Observation destination') ?? '', /13\/09\/2026/);
  assert.equal(map.get('Nature'), 'snowflake');
  assert.equal(map.get('Base'), 'ACME_RAW');
  assert.equal(map.get('Schéma'), 'DEV');
  assert.equal(map.get('Run tag'), 'run-41');
  assert.match(map.get('Checkpoint de chargement') ?? '', /snowpipe\s·\s900/);
  assert.match(map.get('Checkpoint d’application') ?? '', /snowpipe\s·\s901/);
  assert.equal(map.get('Événements source'), '120');
  assert.equal(map.get('Lignes raw'), '120');
  assert.equal(map.get('Lignes canoniques'), '118');
  assert.equal(map.get('Doublons'), '2');

  const sparse = destinationRows(pipelineFixture({
    destination: destinationFixture({
      observedAt: null,
      applyCheckpoint: null,
      canonicalRows: null,
      duplicates: null,
      runTag: null,
    }),
  }));
  const sparseMap = new Map(sparse);
  assert.equal(sparseMap.get('Observation destination'), 'Inconnue — non mesurée');
  assert.equal(sparseMap.get('Checkpoint d’application'), 'Inconnu — non mesuré');
  assert.equal(sparseMap.get('Lignes canoniques'), 'Inconnu — non mesuré');
  assert.equal(sparseMap.get('Run tag'), 'Inconnu — non publié');
});

test('destination detail sections expose the apply stamp alongside the register', () => {
  const sections = destinationDetailSections(pipelineFixture({ destination: destinationFixture() }));
  assert.equal(sections.applyStamp.measured, true);
  assert.equal(sections.applyStamp.label, 'livré 09:42:13');
  assert.ok(sections.rows.length > 0);
});
