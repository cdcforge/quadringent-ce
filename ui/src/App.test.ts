import assert from 'node:assert/strict';
import test from 'node:test';

import { buildCounterMetrics, buildPipelineLead, buildPipelineMissingCopy, buildPriorityMeta, buildStatusNarrative, buildWorkspaceSummary } from './productViewModels.ts';

test('loading and failed summaries never invent zero incidents or recoveries', () => {
  const loading = buildWorkspaceSummary({ status: 'loading', connection: 'connecting' }, []);
  const failed = buildWorkspaceSummary({ status: 'failed', connection: 'offline', message: 'offline' }, []);

  assert.equal(loading.incidents.value, 'Inconnu');
  assert.equal(loading.recoveries.value, 'Inconnu');
  assert.equal(failed.incidents.value, 'Inconnu');
  assert.equal(failed.recoveries.value, 'Inconnu');
  assert.equal(loading.empty.title, 'Données indisponibles');
  assert.equal(failed.empty.title, 'Données indisponibles');
  assert.ok(!loading.incidents.value.includes('0'));
  assert.ok(!failed.recoveries.value.includes('0'));
});

test('degraded narratives expose the cached age and safe message', () => {
  const narrative = buildStatusNarrative(
    {
      status: 'degraded',
      connection: 'reconnecting',
      pipelines: [],
      lastSuccessAt: new Date('2026-08-28T10:00:00Z'),
      message: 'Flux temps réel indisponible ; reconnexion en cours.',
    },
    new Date('2026-08-28T10:03:00Z'),
  );

  assert.match(narrative, /3 min/);
  assert.match(narrative, /reconnexion/i);
});

test('missing pipeline copies do not mention an absent id during loading or failure', () => {
  const loading = buildPipelineMissingCopy({ status: 'loading', connection: 'connecting' }, 'dev-cntr');
  const failed = buildPipelineMissingCopy({ status: 'failed', connection: 'offline', message: 'offline' }, 'dev-cntr');

  assert.equal(loading.title, 'Données indisponibles');
  assert.equal(failed.title, 'Données indisponibles');
  assert.doesNotMatch(loading.description, /absent/i);
  assert.doesNotMatch(failed.description, /absent/i);
});

test('a missing pipeline is confirmed only by a live ready snapshot with exhaustive sources', () => {
  const available = overviewWithSources(['available']);
  const unavailable = overviewWithSources(['unavailable']);
  const partial = overviewWithSources(['available', 'unavailable']);
  const unmapped = overviewWithSources([]);
  const confirmed = buildPipelineMissingCopy(readyState(available), 'dev-cntr');
  const uncertainStates = [
    readyState(unavailable),
    readyState(partial),
    readyState(unmapped),
    {
      status: 'degraded' as const,
      connection: 'reconnecting' as const,
      overview: available,
      pipelines: [],
      lastSuccessAt: new Date('2026-08-28T10:00:00Z'),
      message: 'Snapshot conservé',
    },
  ];

  assert.equal(confirmed.title, 'Pipeline non trouvé');
  assert.match(confirmed.description, /relevé confirmé ne contient pas ce pipeline/i);
  for (const state of uncertainStates) {
    const copy = buildPipelineMissingCopy(state, 'dev-cntr');
    assert.equal(copy.title, 'Présence du pipeline à confirmer');
    assert.match(copy.description, /ne permettent pas de confirmer/i);
    assert.doesNotMatch(`${copy.title} ${copy.description}`, /pipeline absent|ne contient pas ce pipeline/i);
  }
});

test('pipeline leads stay availability-first while loading or failed', () => {
  assert.equal(
    buildPipelineLead({ status: 'loading', connection: 'connecting' }, null),
    'Vérification en cours — la console ne peut pas encore confirmer ce pipeline.',
  );
  assert.equal(
    buildPipelineLead({ status: 'failed', connection: 'offline', message: 'offline' }, null),
    'Données indisponibles — la console ne peut pas encore confirmer ce pipeline.',
  );
  assert.equal(
    buildPipelineLead(readyState(overviewWithSources(['available'])), null),
    'Le relevé confirmé ne contient pas ce pipeline.',
  );
});

test('priority meta stays availability-first while loading or failed', () => {
  assert.equal(buildPriorityMeta({ status: 'loading', connection: 'connecting' }, 0), 'Vérification en cours');
  assert.equal(
    buildPriorityMeta({ status: 'failed', connection: 'offline', message: 'offline' }, 0),
    'Données indisponibles — aucun relevé',
  );
  assert.equal(
    buildPriorityMeta({ status: 'ready', connection: 'live', pipelines: [], lastSuccessAt: new Date('2026-08-28T10:00:00Z') }, 0),
    'Aucune reprise observée',
  );
});

test('destination counter labels disclose event-window scope instead of business table totals', () => {
  const metrics = buildCounterMetrics({ events_in_target: 1064, duplicates_in_target: 0 });
  assert.match(metrics[0]!.label, /événements/i);
  assert.match(metrics[0]!.label, /fenêtre/i);
  assert.match(metrics[1]!.label, /fenêtre/i);
  assert.equal(metrics[1]!.value, '0');
});

test('counter metrics keep every entry visible including null counters', () => {
  const metrics = buildCounterMetrics({
    a: 1,
    b: 2,
    c: 3,
    d: 4,
    e: null,
  });

  assert.equal(metrics.length, 5);
  assert.equal(metrics[4]?.label, 'e');
  assert.equal(metrics[4]?.value, 'Inconnu');
  assert.equal(metrics[4]?.unknownReason, 'Compteur à confirmer');
});

function overviewWithSources(statuses: readonly ('available' | 'unavailable')[]) {
  return {
    revision: 1,
    generatedAt: '2026-08-28T10:00:00Z',
    scope: { kind: statuses.length ? 'single' as const : 'unavailable' as const, environments: statuses.length ? ['dev'] : [] },
    pipelines: [],
    sources: statuses.map((status, index) => ({
      id: `dev-${index}`,
      environment: 'dev',
      evidenceKind: 'live' as const,
      status,
      error: status === 'available' ? null : 'offline',
    })),
  };
}

function readyState(overview: ReturnType<typeof overviewWithSources>) {
  return {
    status: 'ready' as const,
    connection: 'live' as const,
    overview,
    pipelines: [],
    lastSuccessAt: new Date('2026-08-28T10:00:00Z'),
  };
}
