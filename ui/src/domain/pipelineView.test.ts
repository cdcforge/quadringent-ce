import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import type { Overview, Pipeline } from './controlPlane.ts';
import { evidenceBannerCopy, filterPipelines, flowEvidenceClass, flowMotionClass, flowPathAccessibleLabel, isSimulatedOrHistorical, pipelineIssue, prioritizePipelines, summarizeWorkspace, workspaceFocus } from './pipelineView.ts';
import { statusCopy } from '../router.ts';

function pipeline(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id: 'dev-cntr',
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'simulation' },
    summary: 'Capture observée, destination non observée',
    observedAt: '2026-08-28T09:59:00.470000+00:00',
    stages: [
      { id: 'source', status: 'healthy', observedAt: '2026-08-28T09:59:00.470000+00:00', headline: 'Source', detail: 'OK' },
      { id: 'capture', status: 'healthy', observedAt: '2026-08-28T09:59:00.470000+00:00', headline: 'Capture', detail: 'OK' },
      { id: 'raw', status: 'healthy', observedAt: '2026-08-28T09:59:00.470000+00:00', headline: 'Raw', detail: 'OK' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'Load', detail: 'Non observé' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'Destination', detail: 'Non observée' },
    ],
    lagSequences: 3,
    lagSeconds: null,
    counters: {},
    incident: null,
    ...overrides,
  };
}

function overview(pipelines: readonly Pipeline[]): Overview {
  const environments = [...new Set(pipelines.map((item) => item.environment))].sort();
  const sourceKinds = [...new Map(pipelines.map((item) => [`${item.environment}/${item.quality.evidenceKind}`, {
    id: `source-${item.environment}-${item.quality.evidenceKind}`,
    environment: item.environment,
    evidenceKind: item.quality.evidenceKind,
    status: 'available' as const,
    error: null,
  }])).values()];
  return {
    revision: 7,
    generatedAt: '2026-08-28T10:00:00.470000+00:00',
    scope: environments.length === 0
      ? { kind: 'unavailable', environments: [] }
      : environments.length === 1
        ? { kind: 'single', environments }
        : { kind: 'mixed', environments },
    pipelines,
    sources: sourceKinds,
  };
}

test('incident outranks stale and degraded pipelines', () => {
  const degraded = pipeline({ id: 'degraded' });
  const stale = pipeline({ id: 'stale', status: 'unknown', quality: { coverage: 'partial', freshness: 'stale', evidenceKind: 'live' } });
  const incident = pipeline({ id: 'incident', status: 'incident', incident: { code: 'capture_stopped_fail_closed', type: 'capture_timeout' } });

  assert.deepEqual(prioritizePipelines([degraded, incident, stale]).map((item) => item.id), ['incident', 'stale', 'degraded']);
});

test('prioritization covers the six operator levels in order', () => {
  const levels = [
    pipeline({ id: 'healthy', status: 'healthy', quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' } }),
    pipeline({ id: 'recovering', status: 'recovering' }),
    pipeline({ id: 'planned', status: 'planned_stop' }),
    pipeline({ id: 'degraded', status: 'degraded' }),
    pipeline({ id: 'stale', status: 'unknown', quality: { coverage: 'partial', freshness: 'stale', evidenceKind: 'live' } }),
    pipeline({ id: 'incident', status: 'incident' }),
  ];
  assert.deepEqual(prioritizePipelines(levels).map((item) => item.id), ['incident', 'stale', 'degraded', 'planned', 'recovering', 'healthy']);
});

test('workspace copy names missing destination coverage', () => {
  assert.equal(
    summarizeWorkspace(overview([pipeline()])).headline,
    '1 destination n’est pas vérifiée',
  );
});

test('workspace focus points to the first operator action without inventing one', () => {
  const unknown = pipeline({ id: 'unknown-next', status: 'unknown' });
  const incident = pipeline({ id: 'incident-first', status: 'incident' });
  const focus = workspaceFocus(overview([unknown, incident]));

  assert.equal(focus.pipeline?.id, 'incident-first');
  assert.equal(focus.actionLabel, 'Examiner incident-first');
  assert.equal(focus.actionHref, '#/pipeline/incident-first');

  const confirmedEmpty = workspaceFocus(overview([]));
  assert.equal(confirmedEmpty.pipeline, null);
  assert.equal(confirmedEmpty.actionHref, null);

  const unavailable = workspaceFocus({
    ...overview([]),
    sources: [{ id: 'offline', environment: 'dev', evidenceKind: 'live', status: 'unavailable', error: 'offline' }],
  });
  assert.equal(unavailable.pipeline, null);
  assert.equal(unavailable.actionHref, null);
});

test('filters keep the selected DEV environment visible when it is the only environment', () => {
  const result = filterPipelines([pipeline()], { name: '', status: 'all', environment: 'dev' });

  assert.equal(result.length, 1);
  assert.equal(result[0]?.environment, 'dev');
});

test('name and status filters are applied together without changing priority', () => {
  const incident = pipeline({ id: 'dev-orders', status: 'incident', incident: { code: 'capture_stopped_fail_closed', type: 'capture_timeout' } });
  const result = filterPipelines([pipeline(), incident], { name: 'orders', status: 'incident', environment: 'all' });

  assert.deepEqual(result.map((item) => item.id), ['dev-orders']);
});

test('a filter with no match returns a truthful empty result without changing inventory density', () => {
  const inventory = Array.from({ length: 8 }, (_, index) => pipeline({ id: `dev-pipeline-${index + 1}` }));
  const result = filterPipelines(inventory, { name: 'pipeline-absent', status: 'all', environment: 'all' });

  assert.deepEqual(result, []);
  assert.equal(inventory.length, 8);
});

test('an active incident names its summary before an unobserved destination', () => {
  const incident = pipeline({ status: 'incident', summary: 'Lag divergent sur le receiver', incident: null });

  assert.equal(pipelineIssue(incident), 'Lag divergent sur le receiver');
});

test('an incident outside the incident verdict uses a local safe label, never its raw code', () => {
  const incident = pipeline({
    status: 'degraded',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_timeout' },
  });

  assert.equal(pipelineIssue(incident), 'Délai de lecture dépassé');
  assert.doesNotMatch(pipelineIssue(incident), /capture_stopped_fail_closed/);
});

test('unavailable source with no pipelines stays unknown instead of inventing zero attention', () => {
  const result = summarizeWorkspace({ revision: 8, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [], sources: [{ id: 'dev-cntr', environment: 'dev', evidenceKind: 'live', status: 'unavailable', error: 'offline' }] });
  assert.match(result.headline, /non confirmé/i);
  assert.doesNotMatch(result.headline, /0|aucune attention/i);
});

test('available pipeline keeps its verdict when another source is unavailable', () => {
  const result = summarizeWorkspace({ revision: 8, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [pipeline({ quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' }, stages: pipeline().stages.map((stage) => ({ ...stage, status: 'healthy', observedAt: '2026-08-28T10:00:00+00:00' })) })], sources: [
    { id: 'dev-cntr', environment: 'dev', evidenceKind: 'live', status: 'available', error: null },
    { id: 'dev-orders', environment: 'dev', evidenceKind: 'live', status: 'unavailable', error: 'offline' },
  ] });
  assert.match(result.detail, /Couverture partielle : 1 source indisponible/);
  assert.doesNotMatch(result.headline, /workspace inconnu/i);
});

test('all unavailable sources with a cached pipeline report partial cache coverage', () => {
  const result = summarizeWorkspace({ revision: 8, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [pipeline()], sources: [{ id: 'dev-cntr', environment: 'dev', evidenceKind: 'live', status: 'unavailable', error: 'offline' }] });
  assert.match(result.detail, /Couverture partielle : 1 source indisponible/);
  assert.doesNotMatch(result.detail, /Sans source disponible ni pipeline observé/);
});

test('all unavailable sources without pipelines remains unknown', () => {
  const result = summarizeWorkspace({ revision: 8, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [], sources: [{ id: 'dev-cntr', environment: 'dev', evidenceKind: 'live', status: 'unavailable', error: 'offline' }] });
  assert.match(result.headline, /non confirmé/i);
});

test('simulation or historical source is detected even without a pipeline', () => {
  assert.equal(isSimulatedOrHistorical({ revision: 8, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [], sources: [{ id: 'dev-cntr', environment: 'dev', evidenceKind: 'simulation', status: 'available', error: null }] }), true);
});

test('shared overview and pipelines provenance banner covers every evidence mode', () => {
  const source = (evidenceKind: 'live' | 'simulation' | 'historical') => ({ id: evidenceKind, environment: 'dev', evidenceKind, status: 'available' as const, error: null });
  const banner = (sources: readonly ReturnType<typeof source>[]) => evidenceBannerCopy({ revision: 8, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [], sources });
  assert.equal(banner([source('live')]).label, 'Relevé récent');
  assert.equal(banner([source('simulation')]).label, 'Démonstration');
  assert.equal(banner([source('historical')]).label, 'Relevé historique');
  assert.equal(banner([source('simulation'), source('historical')]).label, 'Données de natures différentes');
});

test('an empty workspace never defaults its provenance to live', () => {
  const empty = overview([]);
  const evidence = evidenceBannerCopy(empty);
  const verdict = summarizeWorkspace(empty);

  assert.equal(evidence.label, 'État des données non confirmé');
  assert.equal(verdict.evidenceMode, 'unestablished');
  assert.match(`${evidence.detail} ${verdict.headline}`, /non confirm/i);
  assert.doesNotMatch(`${evidence.label} ${verdict.headline}`, /relevé récent|aucune attention requise/i);
});

test('a pipeline without source descriptors cannot make workspace provenance live', () => {
  const live = pipeline({
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    stages: pipeline().stages.map((stage) => ({ ...stage, status: 'healthy', observedAt: '2026-08-28T10:00:00+00:00' })),
  });
  const evidence = evidenceBannerCopy({ ...overview([live]), sources: [] });

  assert.equal(evidence.label, 'État des données non confirmé');
  assert.doesNotMatch(evidence.label, /récent|actuel/i);
});

test('workspace summary distinguishes pure simulation from pure historical evidence', () => {
  const observedStages = pipeline().stages.map((stage) => ({ ...stage, status: 'healthy' as const, observedAt: '2026-08-28T10:00:00+00:00' }));
  const simulation = pipeline({ status: 'healthy', quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'simulation' }, stages: observedStages });
  const historical = pipeline({ status: 'healthy', quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'historical' }, stages: observedStages });

  const simulationHeadline = summarizeWorkspace(overview([simulation])).headline;
  const historicalHeadline = summarizeWorkspace(overview([historical])).headline;

  assert.match(simulationHeadline, /Démonstration/);
  assert.doesNotMatch(simulationHeadline, /relevé passé/i);
  assert.match(historicalHeadline, /relevé passé/i);
  assert.doesNotMatch(historicalHeadline, /Démonstration/);
});

test('workspace summary and evidence copy identify mixed evidence without reducing it to one kind', () => {
  const source = (evidenceKind: 'live' | 'simulation' | 'historical') => ({ id: evidenceKind, environment: 'dev', evidenceKind, status: 'available' as const, error: null });
  for (const sources of [
    [source('simulation'), source('historical')],
    [source('simulation'), source('live')],
    [source('historical'), source('live')],
  ]) {
    const mixedOverview: Overview = { revision: 9, generatedAt: '2026-08-28T10:00:00+00:00', pipelines: [], sources };
    const verdict = summarizeWorkspace(mixedOverview);
    const evidence = evidenceBannerCopy(mixedOverview);

    assert.match(verdict.headline, /natures différentes/i);
    assert.doesNotMatch(verdict.headline, /^Démonstration disponible/);
    assert.doesNotMatch(verdict.headline, /^Relevé passé disponible/);
    assert.equal(evidence.label, 'Données de natures différentes');
  }
});

test('simulated flow paths carry an explicit non-proven class', () => {
  assert.equal(flowEvidenceClass(pipeline()), 'flow-path flow-path--simulation');
});

test('flow motion requires live fresh proof and a verified destination', () => {
  const fullyObserved = pipeline().stages.map((stage) => ({
    ...stage,
    status: 'healthy' as const,
    observedAt: '2026-08-28T10:00:00+00:00',
  }));
  const liveFresh = pipeline({
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    stages: fullyObserved,
  });
  const liveStale = pipeline({
    quality: { coverage: 'partial', freshness: 'stale', evidenceKind: 'live' },
    stages: fullyObserved,
  });

  assert.equal(flowMotionClass(liveFresh), 'flow-path--moving');
  assert.equal(flowMotionClass(pipeline()), 'flow-path--still');
  assert.equal(flowMotionClass(liveStale), 'flow-path--still');
});

test('flow path labels keep live proof separate from simulation and historical proof', () => {
  const live = pipeline({ quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' }, stages: pipeline().stages.map((stage) => ({ ...stage, status: 'healthy', observedAt: '2026-08-28T10:00:00+00:00' })) });
  const simulation = pipeline({ quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'simulation' } });
  const historical = pipeline({ quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'historical' } });

  assert.match(flowPathAccessibleLabel(live), /Source : Garantie vérifiée/);
  for (const label of [flowPathAccessibleLabel(simulation), flowPathAccessibleLabel(historical)]) {
    assert.doesNotMatch(label, /Garantie vérifiée/);
    assert.match(label, /état réel non confirmé|pas un état actuel/);
    for (const stage of ['Source', 'Lecture', 'Zone de réception', 'Copie initiale', 'Destination']) assert.match(label, new RegExp(stage));
  }
});

test('pipeline inventory uses one responsive list DOM without a table, runway or full-width row separators', () => {
  const css = readFileSync(new URL('../styles/pipelines.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /pipeline-table|pipeline-mobile|runway/);
  assert.doesNotMatch(css, /\.pipeline-entry\s*\{[^}]*border-(?:top|bottom):/);
  assert.match(css, /\.pipeline-entry\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*0\.9fr\) minmax\(0,\s*1\.1fr\) minmax\(0,\s*0\.9fr\) minmax\(0,\s*1\.3fr\)/);
  assert.match(css, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.pipeline-entry\s*\{[^}]*grid-template-columns:\s*1fr/);
  assert.match(css, /overflow-wrap:\s*anywhere/);
  assert.match(css, /word-break:\s*normal/);
  assert.match(css, /\.pipeline-inventory--large \.pipeline-group__heading\s*\{[^}]*position:\s*sticky/);
});

test('operator labels never expose status codes', () => {
  assert.equal(statusCopy('planned_stop').label, 'Arrêt planifié');
  assert.equal(statusCopy('unknown').label, 'État non établi');
});

test('small instrument text keeps WCAG AA contrast on ivory and graphite surfaces', () => {
  const tokens = readFileSync(new URL('../styles/product-tokens.css', import.meta.url), 'utf8');
  const token = (name: string) => tokens.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, 'i'))?.[1] ?? '';

  assert.ok(contrastRatio(token('text-subtle'), token('surface-work')) >= 4.5);
  assert.ok(contrastRatio(token('text-rail-muted'), token('surface-rail')) >= 4.5);
});

test('brand, navigation and transport state never borrow the green proof signal', () => {
  const shell = readFileSync(new URL('../styles/product-shell.css', import.meta.url), 'utf8');
  const brand = readFileSync(new URL('../styles/brand.css', import.meta.url), 'utf8');

  assert.doesNotMatch(brand, /signal-proven/);
  for (const selector of ['product-nav a.is-current', 'connection--live', 'product-mobile__status-chip--live', 'evidence-context__pulse--live']) {
    const block = shell.match(new RegExp(`${selector.replaceAll('.', '\\.') }[^\\{]*\\{([^}]*)\\}`))?.[1] ?? '';
    assert.doesNotMatch(block, /signal-proven/, selector);
  }
});

test('interactive controls provide restrained hover and press feedback with reduced motion support', () => {
  const shell = readFileSync(new URL('../styles/product-shell.css', import.meta.url), 'utf8');
  const responsive = readFileSync(new URL('../styles/responsive.css', import.meta.url), 'utf8');

  assert.match(shell, /:where\([^)]*(?:a|button)[^)]*\)\s*\{[^}]*transition:/);
  assert.match(shell, /:where\([^)]*(?:a|button)[^)]*\):active\s*\{[^}]*transform:\s*translateY\(1px\)/);
  assert.match(responsive, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?transition-duration:\s*0\.01ms\s*!important/);
});

test('mobile pipeline inventory prioritizes the proof focus and preserves bottom-navigation clearance', () => {
  const css = readFileSync(new URL('../styles/pipelines.css', import.meta.url), 'utf8');
  const commercial = readFileSync(new URL('../styles/commercial.css', import.meta.url), 'utf8');

  assert.match(css, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.pipelines-view\s*\{[^}]*padding-bottom:\s*calc\(88px \+ env\(safe-area-inset-bottom\)\)/);
  assert.match(commercial, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.pipeline-solo__focus,[\s\S]*?order:\s*-1/);
  assert.match(commercial, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.pipeline-solo__identity,[\s\S]*?\.pipeline-solo__destination/);
  assert.doesNotMatch(css, /\.pipelines-context p span\s*\{[^}]*display:\s*none/);
});

test('the final commercial layer owns the light shell and one restrained proof focus', () => {
  const tokens = readFileSync(new URL('../styles/product-tokens.css', import.meta.url), 'utf8');
  const main = readFileSync(new URL('../main.tsx', import.meta.url), 'utf8');
  const commercial = readFileSync(new URL('../styles/commercial.css', import.meta.url), 'utf8');
  const responsive = readFileSync(new URL('../styles/responsive.css', import.meta.url), 'utf8');

  assert.match(tokens, /--font-sans:\s*"?IBM Plex Sans/i);
  assert.match(main, /import '\.\/styles\/responsive\.css';\s*import '\.\/styles\/commercial\.css';/);
  assert.match(commercial, /--surface-canvas:\s*#f2f2ef/i);
  assert.match(commercial, /\.product-topbar\s*\{[^}]*min-height:\s*72px/);
  assert.match(commercial, /\.overview-view \.proof-object,[\s\S]*?padding:\s*0[\s\S]*?background:\s*transparent[\s\S]*?box-shadow:\s*none/);
  assert.match(commercial, /\.overview-view \.proof-focus\s*\{[^}]*background:\s*transparent[^}]*border-radius:\s*0/);
  assert.match(commercial, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.overview-view \.proof-object,[\s\S]*?grid-template-columns:\s*1fr/);
  assert.match(commercial, /@media\s*\(max-width:\s*350px\)[\s\S]*?\.product-workspace\s*\{[^}]*width:\s*calc\(100% - 22px\)/);
  assert.match(responsive, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.product-topbar\s*\{[^}]*min-height:\s*calc\(56px \+ env\(safe-area-inset-top\)\)/);
  assert.match(responsive, /@media\s*\(max-width:\s*640px\)[\s\S]*?\.product-nav--mobile\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0/);
});

test('the commercial mobile layer keeps frequent targets reachable and composes onboarding instead of shrinking it', () => {
  const commercial = readFileSync(new URL('../styles/commercial.css', import.meta.url), 'utf8');
  const mobile = commercial.slice(commercial.indexOf('@media (max-width: 760px)'));

  assert.match(mobile, /\.product-nav--mobile a,\s*\.product-more > summary\s*\{[^}]*min-height:\s*68px/);
  assert.match(mobile, /\.proof-focus__footer \.page-action,[\s\S]*?\.usage-pipeline__detail-link\s*\{[^}]*min-height:\s*44px/);
  assert.match(mobile, /\.pipeline-detail-view--live \.detail-tabs a,[\s\S]*?\.pipeline-detail-view \.metric__evidence summary,[\s\S]*?\.usage-metric__proof summary\s*\{[^}]*min-height:\s*44px/);
  assert.match(mobile, /\.page-action,[\s\S]*?\.onboarding-index__item button\s*\{[^}]*min-height:\s*44px/);
  assert.match(mobile, /\.onboarding-index\s*\{[^}]*flex-wrap:\s*nowrap/);
  assert.match(commercial, /\.detail-facts \.fact-spool__band--gesture \.fact-spool__value\s*\{[^}]*text-wrap:\s*pretty/);
  assert.match(commercial, /\.incidents-answer,\s*\.usage-answer\s*\{[^}]*padding:\s*16px 0 12px/);
  assert.match(commercial, /\.incidents-view,\s*\.usage-view\s*\{[^}]*gap:\s*0/);
  assert.match(commercial, /@media\s*\(min-width:\s*901px\)[\s\S]*?\.incidents-view \.incidents-answer h1,[\s\S]*?font-size:\s*22px/);
  assert.match(commercial, /@media\s*\(min-width:\s*1280px\)[\s\S]*?\.product-context__label\s*\{[^}]*font-size:\s*11px/);
  assert.match(commercial, /\.product-context__label \{[^}]*color:\s*var\(--text-muted\)/);
  assert.ok(contrastRatio('#656861', '#f2f2ef') >= 4.5);
});

function contrastRatio(first: string, second: string): number {
  const luminance = (hex: string) => {
    const channels = hex.match(/[0-9a-f]{2}/gi)?.map((value) => Number.parseInt(value, 16) / 255) ?? [];
    const [red = 0, green = 0, blue = 0] = channels.map((value) => value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const a = luminance(first);
  const b = luminance(second);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
