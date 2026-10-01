import assert from 'node:assert/strict';
import test from 'node:test';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type {
  Coverage,
  EvidenceKind,
  Freshness,
  Overview,
  Pipeline,
  Stage,
  StageId,
} from './controlPlane.ts';
import { resolvePipelineProofFocus, resolveProofFocus } from './proofFocus.ts';
import * as proofFocusModule from './proofFocus.ts';

const observedAt = '2026-08-29T20:06:00Z';

test('proof timestamp remains the pipeline observation when the cockpit snapshot is newer', () => {
  const state = stateWith(pipelineFixture());
  state.overview.generatedAt = '2026-08-29T21:00:00Z';
  const focus = resolveProofFocus(state);
  assert.equal(focus.timestamp.value, '2026-08-29T20:06:00Z');
});

test('an unavailable source with a retained snapshot breaks at Source and never claims a current proof', () => {
  const pipeline = pipelineFixture();
  const state = stateWith(pipeline, {
    status: 'degraded',
    connection: 'offline',
    sourceStatus: 'unavailable',
  });

  const focus = resolveProofFocus(state);

  assert.equal(focus.firstBreak?.stage, 'source');
  assert.equal(focus.firstBreak?.cause, 'La source est indisponible. Le dernier relevé reste consultable.');
  assert.deepEqual(focus.upstream.stages, []);
  assert.equal(focus.downstream?.stage, 'destination');
  assert.equal(focus.proofScope.kind, 'cached');
  assert.doesNotMatch(`${focus.verdict.headline} ${focus.proofScope.label}`, /preuve live|santé établie/i);
});

test('partially unavailable sources reject a current completion and keep the ambiguity explicit', () => {
  const focus = resolveProofFocus(stateWith(pipelineFixture(), { sourceStatus: 'partial' }));

  assert.equal(focus.firstBreak?.stage, 'source');
  assert.match(focus.firstBreak?.cause ?? '', /partiellement indisponibles/i);
  assert.equal(focus.proofScope.kind, 'partial');
  assert.equal(focus.proofScope.label, 'Sources partiellement indisponibles');
  assert.doesNotMatch(`${focus.verdict.headline} ${focus.proofScope.label}`, /livraison prouvée|preuve live/i);
});

test('an unmapped pipeline fails closed when no source descriptor exists', () => {
  const focus = resolvePipelineProofFocus(pipelineFixture(), {
    generatedAt: observedAt,
    sources: [],
    cached: false,
  });

  assert.equal(focus.firstBreak?.stage, 'source');
  assert.equal(focus.firstBreak?.reason, 'unavailable');
  assert.equal(focus.proofScope.kind, 'unavailable');
  assert.match(`${focus.firstBreak?.cause} ${focus.proofScope.label}`, /source.*(?:n’est rattachée|n’est pas rattachée|non rattachée|non établie)/i);
  assert.doesNotMatch(`${focus.verdict.headline} ${focus.proofScope.label}`, /livraison prouvée|preuve live/i);
});

test('source and pipeline provenance mismatch becomes a mixed Source break', () => {
  const pipeline = pipelineFixture({ evidenceKind: 'live' });
  const historicalMismatch = resolvePipelineProofFocus(pipeline, {
    generatedAt: observedAt,
    sources: [{ id: 'dev-history', environment: 'dev', evidenceKind: 'historical', status: 'available', error: null }],
    cached: false,
  });
  const mixedMismatch = resolvePipelineProofFocus(pipeline, {
    generatedAt: observedAt,
    sources: [
      { id: 'dev-live', environment: 'dev', evidenceKind: 'live', status: 'available', error: null },
      { id: 'dev-sim', environment: 'dev', evidenceKind: 'simulation', status: 'available', error: null },
    ],
    cached: false,
  });

  for (const focus of [historicalMismatch, mixedMismatch]) {
    assert.equal(focus.firstBreak?.stage, 'source');
    assert.equal(focus.proofScope.kind, 'mixed');
    assert.match(`${focus.firstBreak?.cause} ${focus.proofScope.detail}`, /nature des sources.*(?:incohérente|mixte)/i);
    assert.doesNotMatch(`${focus.verdict.headline} ${focus.proofScope.label}`, /livraison prouvée|preuve live/i);
  }
});

test('a Capture incident stays the first break and keeps Source as compact upstream proof', () => {
  const pipeline = pipelineFixture({
    status: 'incident',
    stages: stageSet({ capture: { status: 'incident', headline: 'Capture arrêtée', detail: 'La capture est arrêtée en sécurité.' } }),
  });

  const focus = resolveProofFocus(stateWith(pipeline));

  assert.equal(focus.firstBreak?.stage, 'capture');
  assert.equal(focus.focus.label, 'Lecture');
  assert.equal(focus.focus.cause, 'La capture est arrêtée en sécurité.');
  assert.deepEqual(focus.upstream.stages, ['source']);
  assert.equal(focus.cta.label, 'Inspecter la lecture');
  assert.equal(focus.cta.kind, 'link');
  if (focus.cta.kind === 'link') {
    assert.equal(focus.cta.href, '#/pipeline/dev-orders-to-snowflake/overview?inspect=capture');
  }
});

test('a structured incident without a matching stage fails closed at its public capture or destination boundary', () => {
  const capture = pipelineFixture({
    status: 'incident',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_timeout' },
  });
  const destination = pipelineFixture({
    status: 'incident',
    incident: { code: 'destination_reconciliation_mismatch', type: 'destination' },
  });

  const captureFocus = resolveProofFocus(stateWith(capture));
  const destinationFocus = resolveProofFocus(stateWith(destination));

  assert.equal(captureFocus.firstBreak?.stage, 'capture');
  assert.equal(captureFocus.firstBreak?.reason, 'incident');
  assert.match(captureFocus.focus.cause, /incident de lecture/i);
  assert.equal(destinationFocus.firstBreak?.stage, 'destination');
  assert.equal(destinationFocus.firstBreak?.reason, 'incident');
  assert.match(destinationFocus.focus.cause, /incident de destination/i);
  assert.doesNotMatch(`${captureFocus.verdict.headline} ${destinationFocus.verdict.headline}`, /livraison prouvée|livraison établie/i);
});

test('recovering or aggregate incident status without a stage break never becomes a completed delivery', () => {
  const recovering = resolveProofFocus(stateWith(pipelineFixture({ status: 'recovering' })));
  const divergentAggregate = resolveProofFocus(stateWith(pipelineFixture({
    status: 'incident',
    summary: 'Retard divergent signalé par le worker',
    lagSeries: lagPoints(Array.from({ length: 30 }, (_, index) => 1 + Math.round((1_377_824 * index) / 29))),
  })));

  assert.equal(recovering.firstBreak?.stage, 'destination');
  assert.equal(recovering.firstBreak?.reason, 'degraded');
  assert.match(recovering.focus.cause, /reprise/i);
  assert.equal(divergentAggregate.firstBreak?.stage, 'destination');
  assert.equal(divergentAggregate.firstBreak?.reason, 'incident');
  assert.match(divergentAggregate.focus.cause, /incident global/i);
  assert.doesNotMatch(`${recovering.verdict.headline} ${divergentAggregate.verdict.headline}`, /livraison prouvée|livraison établie/i);
});

test('late freshness and incomplete coverage reject completion without inventing a failing stage', () => {
  const late = resolveProofFocus(stateWith(pipelineFixture({ freshness: 'late' })));
  const partial = resolveProofFocus(stateWith(pipelineFixture({ coverage: 'partial' })));

  assert.equal(late.firstBreak?.stage, 'destination');
  assert.equal(late.proofScope.kind, 'stale');
  assert.match(late.proofScope.label, /tardif/i);
  assert.equal(partial.firstBreak?.stage, 'destination');
  assert.equal(partial.proofScope.kind, 'partial');
  assert.match(partial.focus.cause, /incomplet/i);
  assert.doesNotMatch(`${late.verdict.headline} ${partial.verdict.headline}`, /livraison prouvée|livraison établie/i);
});

test('stale freshness invalidates the chain from Source even when every stage is marked healthy', () => {
  const pipeline = pipelineFixture({ freshness: 'stale' });

  const focus = resolveProofFocus(stateWith(pipeline));

  assert.equal(focus.firstBreak?.stage, 'source');
  assert.equal(focus.firstBreak?.reason, 'stale');
  assert.equal(focus.focus.cause, 'Le relevé le plus récent est trop ancien.');
  assert.equal(focus.proofScope.kind, 'stale');
  assert.doesNotMatch(focus.verdict.headline, /livraison prouvée|santé/i);
});

test('a fully observed simulation completes only inside the simulated scope', () => {
  const pipeline = pipelineFixture({ evidenceKind: 'simulation' });

  const focus = resolveProofFocus(stateWith(pipeline, { evidenceKind: 'simulation' }));

  assert.equal(focus.firstBreak, null);
  assert.equal(focus.focus.kind, 'completion');
  assert.equal(focus.focus.stage, 'destination');
  assert.deepEqual(focus.upstream.stages, ['source', 'capture', 'raw', 'load']);
  assert.equal(focus.downstream, null);
  assert.equal(focus.proofScope.kind, 'simulation');
  assert.match(focus.verdict.headline, /démonstration/i);
  assert.doesNotMatch(`${focus.verdict.headline} ${focus.verdict.detail}`, /preuve live|santé réelle établie|healthy/i);
});

test('an unobserved load is selected before an unobserved destination', () => {
  const pipeline = pipelineFixture({
    status: 'degraded',
    stages: stageSet({
      load: { status: 'unknown', observedAt: null, headline: 'Chargement inconnu', detail: 'Aucun reçu.' },
      destination: { status: 'unknown', observedAt: null, headline: 'Destination inconnue', detail: 'Aucune réconciliation.' },
    }),
  });

  const focus = resolveProofFocus(stateWith(pipeline));

  assert.equal(focus.firstBreak?.stage, 'load');
  assert.deepEqual(focus.upstream.stages, ['source', 'capture', 'raw']);
  assert.equal(focus.focus.evidence, 'Attendu · Copie initiale');
  assert.equal(focus.downstream?.status, 'not-established');
  assert.equal(focus.cta.label, 'Inspecter la copie initiale');
});

test('an observed load followed by an unobserved destination breaks at Destination', () => {
  const pipeline = pipelineFixture({
    status: 'degraded',
    stages: stageSet({
      destination: { status: 'unknown', observedAt: null, headline: 'Destination inconnue', detail: 'Aucune réconciliation.' },
    }),
  });

  const focus = resolveProofFocus(stateWith(pipeline));

  assert.equal(focus.firstBreak?.stage, 'destination');
  assert.deepEqual(focus.upstream.stages, ['source', 'capture', 'raw', 'load']);
  assert.equal(focus.downstream, null);
  assert.equal(focus.focus.evidence, 'Attendu · Arrivée confirmée');
});

test('offline with a cache preserves the cached break and dates it instead of recalculating a current state', () => {
  const pipeline = pipelineFixture({
    status: 'degraded',
    stages: stageSet({ load: { status: 'unknown', observedAt: null, headline: 'Load inconnu', detail: 'Aucun reçu.' } }),
  });
  const state = stateWith(pipeline, { status: 'degraded', connection: 'offline' });

  const focus = resolveProofFocus(state);

  assert.equal(focus.firstBreak?.stage, 'load');
  assert.equal(focus.proofScope.kind, 'cached');
  assert.equal(focus.timestamp.value, observedAt);
  assert.match(focus.timestamp.label, /dernier relevé conservé/i);
  assert.match(focus.verdict.headline, /^Dernière livraison non confirmée/);
});

test('a reconnecting SSE with retained data is presented as cached, not current', () => {
  const pipeline = pipelineFixture({
    status: 'degraded',
    stages: stageSet({ load: { status: 'unknown', observedAt: null, headline: 'Load inconnu', detail: 'Aucun reçu.' } }),
  });
  const state = stateWith(pipeline, { connection: 'reconnecting' });

  const focus = resolveProofFocus(state);

  assert.equal(focus.proofScope.kind, 'cached');
  assert.match(focus.verdict.headline, /^Dernière livraison non confirmée/);
  assert.doesNotMatch(focus.proofScope.label, /preuve live/i);
});

test('offline without a cache invents neither a pipeline nor a first break', () => {
  const state: ControlPlaneState = {
    status: 'failed',
    connection: 'offline',
    message: 'Control plane indisponible',
  };

  const focus = resolveProofFocus(state);

  assert.equal(focus.pipelineId, null);
  assert.equal(focus.firstBreak, null);
  assert.equal(focus.focus.kind, 'unavailable');
  assert.deepEqual(focus.upstream.stages, []);
  assert.equal(focus.downstream?.status, 'not-established');
  assert.equal(focus.proofScope.kind, 'unavailable');
  assert.equal(focus.cta.kind, 'refresh');
  assert.doesNotMatch(`${focus.verdict.headline} ${focus.verdict.detail}`, /healthy|livraison prouvée|établie/i);
});

test('a downstream historical observation never repairs an earlier Capture break', () => {
  const pipeline = pipelineFixture({
    status: 'incident',
    evidenceKind: 'historical',
    stages: stageSet({ capture: { status: 'incident', headline: 'Capture arrêtée', detail: 'La capture a échoué.' } }),
  });

  const focus = resolveProofFocus(stateWith(pipeline, { evidenceKind: 'historical' }));

  assert.equal(focus.firstBreak?.stage, 'capture');
  assert.equal(focus.proofScope.kind, 'historical');
  assert.equal(focus.downstream?.status, 'retained-observation');
  assert.match(focus.downstream?.detail ?? '', /ne confirme pas la livraison/i);
  assert.doesNotMatch(focus.verdict.headline, /livraison prouvée|établie/i);
});

test('the workspace resolver delegates to the reusable pipeline resolver without changing its result', () => {
  const pipeline = pipelineFixture({
    status: 'degraded',
    stages: stageSet({ load: { status: 'unknown', observedAt: null, headline: 'Load inconnu', detail: 'Aucun reçu.' } }),
  });
  const state = stateWith(pipeline);
  const pipelineResolver = (proofFocusModule as typeof proofFocusModule & {
    resolvePipelineProofFocus?: (
      pipeline: Pipeline,
      context: {
        readonly generatedAt: string;
        readonly sources: Overview['sources'];
        readonly cached: boolean;
      },
    ) => ReturnType<typeof resolveProofFocus>;
  }).resolvePipelineProofFocus;

  assert.equal(typeof pipelineResolver, 'function');
  const direct = pipelineResolver?.(pipeline, {
    generatedAt: observedAt,
    sources: state.status === 'ready' ? state.overview.sources : [],
    cached: false,
  });

  assert.deepEqual(direct, resolveProofFocus(state));
  assert.equal(direct?.firstBreak?.stage, 'load');
});

interface PipelineOverrides {
  readonly status?: Pipeline['status'];
  readonly freshness?: Freshness;
  readonly coverage?: Coverage;
  readonly evidenceKind?: EvidenceKind;
  readonly stages?: readonly Stage[];
  readonly incident?: Pipeline['incident'];
  readonly summary?: string;
  readonly lagSeries?: Pipeline['lagSeries'];
}

function pipelineFixture(overrides: PipelineOverrides = {}): Pipeline {
  return {
    id: 'dev-orders-to-snowflake',
    environment: 'dev',
    status: overrides.status ?? 'healthy',
    quality: {
      coverage: overrides.coverage ?? 'complete',
      freshness: overrides.freshness ?? 'fresh',
      evidenceKind: overrides.evidenceKind ?? 'live',
    },
    summary: overrides.summary ?? 'Projection serveur',
    observedAt,
    stages: overrides.stages ?? stageSet(),
    lagSequences: null,
    lagSeconds: null,
    lagSeries: overrides.lagSeries ?? [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: overrides.incident ?? null,
  };
}

function lagPoints(values: readonly number[]): Pipeline['lagSeries'] {
  return values.map((lag, index) => ({
    startSeconds: index * 5,
    endSeconds: index * 5 + 4,
    low: lag,
    high: lag,
    lag,
    samples: 5,
    unknownSamples: 0,
    coverage: 'complete',
    kind: 'observed',
  }));
}

function stageSet(overrides: Partial<Record<StageId, Partial<Stage>>> = {}): readonly Stage[] {
  const labels: Readonly<Record<StageId, string>> = {
    source: 'Source',
    capture: 'Capture',
    raw: 'Raw durable',
    load: 'Chargement',
    destination: 'Destination',
  };
  return (['source', 'capture', 'raw', 'load', 'destination'] as const).map((id) => ({
    id,
    status: 'healthy' as const,
    observedAt,
    headline: labels[id],
    detail: 'Observation reçue.',
    ...overrides[id],
  }));
}

function stateWith(
  pipeline: Pipeline,
  options: {
    readonly status?: 'ready' | 'degraded';
    readonly connection?: 'live' | 'reconnecting' | 'offline';
    readonly sourceStatus?: 'available' | 'unavailable' | 'partial';
    readonly evidenceKind?: EvidenceKind;
  } = {},
): ControlPlaneState {
  const overview: Overview = {
    revision: 12,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines: [pipeline],
    sources: options.sourceStatus === 'partial'
      ? [
          { id: 'dev-ibmi-a', environment: 'dev', evidenceKind: options.evidenceKind ?? pipeline.quality.evidenceKind, status: 'available', error: null },
          { id: 'dev-ibmi-b', environment: 'dev', evidenceKind: options.evidenceKind ?? pipeline.quality.evidenceKind, status: 'unavailable', error: 'offline' },
        ]
      : [{
          id: 'dev-ibmi',
          environment: 'dev',
          evidenceKind: options.evidenceKind ?? pipeline.quality.evidenceKind,
          status: options.sourceStatus ?? 'available',
          error: options.sourceStatus === 'unavailable' ? 'offline' : null,
        }],
  };
  if (options.status === 'degraded') {
    return {
      status: 'degraded',
      connection: options.connection ?? 'offline',
      overview,
      pipelines: overview.pipelines,
      lastSuccessAt: new Date(observedAt),
      message: 'Dernier snapshot conservé',
    };
  }
  return {
    status: 'ready',
    connection: options.connection ?? 'live',
    overview,
    pipelines: overview.pipelines,
    lastSuccessAt: new Date(observedAt),
  };
}
