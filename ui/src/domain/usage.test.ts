import assert from 'node:assert/strict';
import test from 'node:test';

import { buildMeasuredUsage, usageView } from './usage.ts';
import type { Pipeline } from './controlPlane.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';

test('an estimate cannot render as a measured value', () => {
  const view = usageView({
    kind: 'estimate',
    label: 'Coût mensuel projeté',
    value: 4.4,
    currency: 'USD',
    methodology: 'Hypothèse : volume constant sur 30 jours.',
    assumptions: ['Prix public sans remise négociée'],
    asOf: '2026-08-28T00:00:00Z',
    region: 'us-east-1',
  });

  assert.equal(view.kind, 'estimate');
  assert.equal(view.badge, 'Estimation');
  assert.match(view.methodology, /hypothèse/i);
  assert.equal(view.asOf, '2026-08-28T00:00:00Z');
  assert.equal(view.region, 'us-east-1');
  assert.notEqual(view.badge, 'Mesuré');
});

test('an estimate requires methodology assumptions date and region', () => {
  assert.throws(
    () => usageView({
      kind: 'estimate',
      label: 'Coût',
      value: 4.4,
      currency: 'USD',
      methodology: '',
      assumptions: [],
      asOf: '',
      region: '',
    }),
    /méthodologie/i,
  );
});

test('measured counters keep unit source observation and provenance', () => {
  const [pipelineUsage] = buildMeasuredUsage([
    pipelineFixture('alpha', {
      events_published: 1_793,
      payload_bytes_published: 622_091,
      events_in_target: null,
    }),
  ]);

  assert.equal(pipelineUsage?.pipelineId, 'alpha');
  assert.deepEqual(
    pipelineUsage?.metrics.map((metric) => ({
      key: metric.key,
      badge: metric.badge,
      unit: metric.unit,
      source: metric.source,
      observedAt: metric.observedAt,
      provenance: metric.provenance,
    })),
    [
      {
        key: 'events_published',
        badge: 'Mesuré',
        unit: 'événements',
        source: 'control plane / alpha / counters.events_published',
        observedAt,
        provenance: 'simulation',
      },
      {
        key: 'payload_bytes_published',
        badge: 'Mesuré',
        unit: 'octets',
        source: 'control plane / alpha / counters.payload_bytes_published',
        observedAt,
        provenance: 'simulation',
      },
    ],
  );
});

test('heterogeneous and cross-pipeline counters are never added together', () => {
  const usage = buildMeasuredUsage([
    pipelineFixture('alpha', { events_published: 10, payload_bytes_published: 100 }),
    pipelineFixture('beta', { events_published: 20, payload_bytes_published: 200 }),
  ]);

  assert.equal(usage.length, 2);
  assert.deepEqual(usage.map((item) => item.pipelineId), ['alpha', 'beta']);
  assert.deepEqual(usage[0]?.metrics.map((metric) => metric.value), [10, 100]);
  assert.deepEqual(usage[1]?.metrics.map((metric) => metric.value), [20, 200]);
  assert.equal(usage.flatMap((item) => item.metrics).some((metric) => metric.value === 330), false);
});

test('null and semantically unknown counters do not become measured usage', () => {
  const [usage] = buildMeasuredUsage([
    pipelineFixture('alpha', {
      events_in_target: null,
      undocumented_business_rows: 42,
    }),
  ]);

  assert.deepEqual(usage?.metrics, []);
});

function pipelineFixture(id: string, counters: Readonly<Record<string, number | null>>): Pipeline {
  return {
    id,
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'simulation' },
    summary: 'Livraison Snowflake non prouvée',
    observedAt,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Signal présent' },
      { id: 'capture', status: 'healthy', observedAt, headline: 'Capture observée', detail: 'Signal présent' },
      { id: 'raw', status: 'healthy', observedAt, headline: 'Raw observé', detail: 'Signal présent' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'Load inconnu', detail: 'Non observé' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'Destination inconnue', detail: 'Non observée' },
    ],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters,
    incident: null,
  };
}
