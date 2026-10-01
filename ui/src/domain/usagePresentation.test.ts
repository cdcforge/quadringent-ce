import assert from 'node:assert/strict';
import test from 'node:test';

import type { Pipeline } from './controlPlane.ts';
import { buildUsagePresentation } from './usagePresentation.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';

test('selects one primary measure with the explicit publication priority', () => {
  const [view] = buildUsagePresentation([
    pipelineFixture({ payload_bytes_published: 622_091, events_published: 1_793, polls: 281 }),
  ]);

  assert.equal(view?.primary?.key, 'events_published');
  assert.equal(view?.primary?.value, 1_793);
  assert.equal(view?.primary?.unit, 'événements');
});

test('falls back to payload bytes then the first allowlisted measured counter', () => {
  const [payload] = buildUsagePresentation([pipelineFixture({ polls: 20, payload_bytes_published: 12_000 })]);
  const [activity] = buildUsagePresentation([pipelineFixture({ receiver_rotations: 0, polls: 20 })]);

  assert.equal(payload?.primary?.key, 'payload_bytes_published');
  assert.equal(activity?.primary?.key, 'polls');
});

test('groups only allowlisted counters and keeps receiver rotations as activity', () => {
  const [view] = buildUsagePresentation([pipelineFixture({
    events_published: 10,
    errors: 0,
    receiver_rotations: 2,
    undocumented_business_rows: 42,
  })]);

  assert.deepEqual(view?.groups.map((group) => group.id), ['progression', 'reliability', 'activity', 'destination']);
  assert.deepEqual(view?.groups[0]?.metrics.map((metric) => metric.key), ['events_published']);
  assert.deepEqual(view?.groups[1]?.metrics.map((metric) => metric.key), ['errors']);
  assert.deepEqual(view?.groups[2]?.metrics.map((metric) => metric.key), ['receiver_rotations']);
  assert.equal(view?.allMetrics.some((metric) => metric.key === 'undocumented_business_rows'), false);
});

test('preserves measured zero without turning it into health or an error', () => {
  const [view] = buildUsagePresentation([pipelineFixture({ errors: 0, receiver_rotations: 0 })]);

  assert.equal(view?.groups[1]?.metrics[0]?.value, 0);
  assert.equal(view?.groups[2]?.metrics[0]?.value, 0);
  assert.equal(view?.groups[2]?.metrics[0]?.label, 'Rotations du journal');
  assert.equal(JSON.stringify(view).includes('healthy'), false);
});

test('unknown and non-allowlisted values stay non-instrumented', () => {
  const [view] = buildUsagePresentation([pipelineFixture({
    events_published: null,
    errors: null,
    undocumented_business_rows: 42,
  })]);

  assert.equal(view?.primary, null);
  assert.deepEqual(view?.allMetrics, []);
  assert.equal(view?.groups.every((group) => group.metrics.length === 0), true);
});

test('every retained metric keeps its exact source, observation and provenance', () => {
  const [view] = buildUsagePresentation([pipelineFixture({ events_published: 10 })]);
  const metric = view?.primary;

  assert.equal(metric?.source, 'control plane / alpha / counters.events_published');
  assert.equal(metric?.observedAt, observedAt);
  assert.equal(metric?.provenance, 'simulation');
});

function pipelineFixture(counters: Readonly<Record<string, number | null>>): Pipeline {
  return {
    id: 'alpha',
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
