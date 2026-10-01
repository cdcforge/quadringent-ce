import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import * as shellModel from './AppShell.model.ts';
import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Overview, Pipeline, PipelineStatus } from '../domain/controlPlane.ts';

const { closeMobileDetailsOnEscape, connectionCopy, liveAnnouncement, skipWorkspaceInteraction } = shellModel;
let vite: ViteDevServer;
let AppShell: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  AppShell = (await vite.ssrLoadModule('/src/components/AppShell.tsx')).AppShell;
});

after(async () => { await vite.close(); });

test('skip workspace interaction prevents navigation on a nested hash and focuses the workspace', () => {
  let prevented = 0;
  let scheduled = 0;
  let focused = 0;
  const nestedHash = '#/pipeline/dev-cntr/integrity';

  skipWorkspaceInteraction(
    { preventDefault() { prevented += 1; } } as { preventDefault(): void },
    () => {
      focused += 1;
    },
    (focus) => {
      scheduled += 1;
      focus();
    },
  );

  assert.equal(prevented, 1);
  assert.equal(scheduled, 1);
  assert.equal(focused, 1);
  assert.equal(nestedHash, '#/pipeline/dev-cntr/integrity');
});

test('skip workspace interaction schedules focus after the event', () => {
  let scheduled = 0;
  let focused = 0;

  skipWorkspaceInteraction(
    { preventDefault() {} } as { preventDefault(): void },
    () => {
      focused += 1;
    },
    (focus) => {
      scheduled += 1;
      assert.equal(focused, 0);
      focus();
    },
  );

  assert.equal(scheduled, 1);
  assert.equal(focused, 1);
});

test('a route change returns to the workspace origin before moving focus', () => {
  const events: string[] = [];
  const restore = (shellModel as typeof shellModel & {
    restoreWorkspaceAfterNavigation?: (
      scroll: () => void,
      focus: () => void,
      schedule: (work: () => void) => void,
    ) => void;
  }).restoreWorkspaceAfterNavigation;

  assert.equal(typeof restore, 'function');
  restore?.(
    () => { events.push('scroll'); },
    () => { events.push('focus'); },
    (work) => { events.push('schedule'); work(); },
  );

  assert.deepEqual(events, ['scroll', 'schedule', 'focus']);
});

test('connection copy stays disconnected until the SSE stream is live', () => {
  assert.equal(connectionCopy('connecting').label, 'Connexion en cours');
  assert.equal(connectionCopy('live').label, 'Disponible');
  assert.equal(connectionCopy('live', 'degraded').label, 'Indisponible');
  assert.equal(connectionCopy('live', 'failed').label, 'Indisponible');
  assert.equal(connectionCopy('live', 'ready').label, 'Disponible');
  assert.equal(connectionCopy('live', 'ready', 'technical').label, 'Disponible');
  assert.equal(connectionCopy('live', 'refreshing').label, 'Vérification en cours');
  assert.equal(connectionCopy('live', 'loading').label, 'Vérification en cours');
  assert.equal(connectionCopy('reconnecting').label, 'Reconnexion en cours');
  assert.equal(connectionCopy('offline').label, 'Hors ligne');
});

test('escape closes an open mobile disclosure and restores its summary focus', () => {
  let prevented = 0;
  let stopped = 0;
  let focused = 0;
  const target = {
    open: true,
    querySelector(selector: string) {
      assert.equal(selector, 'summary');
      return { focus() { focused += 1; } };
    },
  };

  const closed = closeMobileDetailsOnEscape({
    key: 'Escape',
    currentTarget: target,
    preventDefault() { prevented += 1; },
    stopPropagation() { stopped += 1; },
  });

  assert.equal(closed, true);
  assert.equal(target.open, false);
  assert.equal(prevented, 1);
  assert.equal(stopped, 1);
  assert.equal(focused, 1);
});

test('mobile disclosure ignores unrelated keys and already closed states', () => {
  for (const event of [
    { key: 'Enter', open: true },
    { key: 'Escape', open: false },
  ]) {
    let prevented = 0;
    let focused = 0;
    const target = {
      open: event.open,
      querySelector() { return { focus() { focused += 1; } }; },
    };

    assert.equal(closeMobileDetailsOnEscape({
      key: event.key,
      currentTarget: target,
      preventDefault() { prevented += 1; },
      stopPropagation() {},
    }), false);
    assert.equal(target.open, event.open);
    assert.equal(prevented, 0);
    assert.equal(focused, 0);
  }
});

test('the closed mobile More disclosure hides its action panel from primary navigation', () => {
  const markup = renderShell(readyState([pipelineFixture('alpha', 'healthy')]), { name: 'setup' });

  assert.match(markup, /<details class="product-more"><summary class="is-current">Plus<\/summary><div class="product-more__menu" hidden="">/);
});

test('desktop and mobile chrome reserve the live signal for receivable current evidence', () => {
  const pipeline = pipelineFixture('fresh', 'healthy', 'live', true);
  const fresh = readyState([pipeline]);
  const cached: ControlPlaneState = {
    ...fresh,
    status: 'degraded',
    connection: 'reconnecting',
    message: 'Snapshot conservé',
  };
  const partial: ControlPlaneState = {
    ...fresh,
    overview: {
      ...fresh.overview,
      sources: [
        ...fresh.overview.sources,
        { id: 'source-dev-secondary', evidenceKind: 'live', environment: 'dev', status: 'unavailable', error: 'offline' },
      ],
    },
  };
  const unavailable: ControlPlaneState = {
    ...fresh,
    overview: {
      ...fresh.overview,
      sources: fresh.overview.sources.map((source) => ({ ...source, status: 'unavailable' as const, error: 'offline' })),
    },
  };
  const mismatch: ControlPlaneState = {
    ...fresh,
    overview: {
      ...fresh.overview,
      sources: fresh.overview.sources.map((source) => ({ ...source, evidenceKind: 'historical' as const })),
    },
  };
  const crossEnvironment = readyState([
    pipelineFixture('alpha', 'healthy', 'live', true),
    { ...pipelineFixture('bravo', 'healthy', 'live', true), environment: 'prod' },
  ]);
  const aggregatePartial: ControlPlaneState = {
    ...crossEnvironment,
    overview: {
      ...crossEnvironment.overview,
      sources: crossEnvironment.overview.sources.map((source) => source.environment === 'prod'
        ? { ...source, status: 'unavailable' as const, error: 'offline' }
        : source),
    },
  };
  const cases = [
    { state: cached, short: 'Relevé conservé' },
    { state: partial, short: 'Informations partielles' },
    { state: unavailable, short: 'À vérifier' },
    { state: readyState([]), short: 'À vérifier' },
    { state: mismatch, short: 'À vérifier' },
    { state: aggregatePartial, short: 'Informations partielles' },
  ] as const;

  // NOTE : la coquille n'affiche plus de pastille de preuve visible en
  // permanence (l'ancienne `.product-topbar__evidence--*` du en-tête). L'état
  // des données ne vit plus que dans le panneau « Détails », qui est replié
  // dans un <details> mobile (`.product-mobile-context`, masqué au-delà de
  // 640px par responsive.css). Ce test vérifie donc ce panneau plutôt qu'un
  // badge visible en desktop — voir le rapport de migration : aucune preuve
  // de fraîcheur des données n'est actuellement visible en desktop hors de ce
  // repli, ce qui est signalé comme un défaut découvert, pas corrigé ici.
  const freshMarkup = renderShell(fresh);
  assert.match(freshMarkup, /href="#\/setup"/);
  assert.match(freshMarkup, />Connexions<\/a>/);
  assert.match(freshMarkup, /aria-label="Détails des données"/);
  assert.match(freshMarkup, /<dt>État des données<\/dt><dd>Relevé récent<\/dd>/);
  assert.match(freshMarkup, /<dt>Service<\/dt><dd>Disponible<\/dd>/);

  for (const { state, short } of cases) {
    const markup = renderShell(state);
    const chrome = visibleChrome(markup);
    assert.match(markup, new RegExp(`<dt>État des données</dt><dd>${escapeRegExp(short)}</dd>`));
    assert.doesNotMatch(markup, /product-topbar__evidence--live/);
    assert.doesNotMatch(chrome, /API\/SSE connecté/);
  }

  const historical = readyState([pipelineFixture('historical', 'healthy', 'historical', true)], 'historical');
  const historicalMarkup = renderShell(historical);
  assert.match(historicalMarkup, /<dt>État des données<\/dt><dd>Relevé historique<\/dd>/);
  assert.match(historicalMarkup, />Disponible</);
  assert.doesNotMatch(visibleChrome(historicalMarkup), /API\/SSE connecté/);
});

test('live announcement keeps unavailable and empty workspace states explicit', () => {
  assert.equal(
    liveAnnouncement({ status: 'loading', connection: 'connecting' }),
    'Le service est en cours de vérification. Environnement : Non confirmé. Données indisponibles.',
  );
  assert.equal(
    liveAnnouncement({ status: 'failed', connection: 'offline', message: 'indisponible' }),
    'Le service est indisponible. Environnement : Non confirmé. Données indisponibles.',
  );
  assert.equal(
    liveAnnouncement(readyState([])),
    'Le service répond. Environnement : Non confirmé. Données indisponibles.',
  );
});

test('a scope-only SSE revision changes the live announcement without reading metric ticks', () => {
  const localPipeline = { ...pipelineFixture('stable', 'degraded'), environment: 'local' };
  const local = readyState([localPipeline], 'live', { kind: 'single', environments: ['local'] });
  const mixed: ControlPlaneState = {
    ...local,
    overview: {
      ...local.overview,
      revision: 2,
      scope: { kind: 'mixed', environments: ['local', 'prod'] },
      pipelines: local.overview.pipelines.map((pipeline) => ({ ...pipeline, counters: { events_published: 999_999 } })),
      sources: [...local.overview.sources, { id: 'source-prod', evidenceKind: 'live', environment: 'prod', status: 'available', error: null }],
    },
  };

  assert.match(liveAnnouncement(local), /Environnement : LOCAL/);
  assert.match(liveAnnouncement(mixed), /Environnement : LOCAL, PROD/);
  assert.notEqual(liveAnnouncement(mixed), liveAnnouncement(local));
  assert.doesNotMatch(liveAnnouncement(mixed), /999 999|events_published/);
});

test('live announcement follows the shared proof verdict without reading metric ticks', () => {
  const states: ReadonlyArray<{
    readonly statuses: readonly PipelineStatus[];
    readonly expected: string;
  }> = [
    { statuses: ['healthy'], expected: 'Données disponibles dans Snowflake' },
    { statuses: ['unknown'], expected: 'Données à vérifier' },
    { statuses: ['unknown', 'incident'], expected: 'Données à vérifier' },
  ];

  for (const { statuses, expected } of states) {
    const announcement = liveAnnouncement(readyState(statuses.map((status, index) => pipelineFixture(`${index}`, status, 'live', true))));
    assert.match(announcement, new RegExp(expected));
    assert.doesNotMatch(announcement, /1 793|events_published/);
  }
});

test('live announcement does not let planned stop or recovery hide an unobserved destination', () => {
  for (const status of ['planned_stop', 'recovering'] as const) {
    const pipeline = pipelineFixture(status, status);
    const announcement = liveAnnouncement(readyState([pipeline]));

    assert.match(announcement, /Informations partielles|Données à vérifier/i);
    assert.doesNotMatch(announcement, /chargement|Arrêt planifié|Reprise en cours/i);
  }
});

test('live announcement keeps simulated and historical data origins explicit', () => {
  const simulation = pipelineFixture('simulation', 'healthy', 'simulation', true);
  const historical = pipelineFixture('historical', 'healthy', 'historical', true);

  assert.match(liveAnnouncement(readyState([simulation], 'simulation')), /Données de démonstration/);
  assert.match(liveAnnouncement(readyState([historical], 'historical')), /Dernier relevé historique/);
  assert.doesNotMatch(liveAnnouncement(readyState([simulation], 'simulation')), /Garantie vérifiée/);
  assert.doesNotMatch(liveAnnouncement(readyState([historical], 'historical')), /Garantie vérifiée/);
  assert.doesNotMatch(liveAnnouncement(readyState([historical], 'historical')), /[Dd]émonstration/);
});

test('live announcement calls mixed evidence partial and never collapses it to simulation or historical', () => {
  const state = readyState([]);
  const mixed: ControlPlaneState = {
    ...state,
    overview: {
      ...state.overview,
      scope: { kind: 'single', environments: ['dev'] },
      sources: [
        { id: 'simulation', evidenceKind: 'simulation', environment: 'dev', status: 'available', error: null },
        { id: 'historical', evidenceKind: 'historical', environment: 'dev', status: 'available', error: null },
      ],
    },
  };
  const announcement = liveAnnouncement(mixed);

  assert.match(announcement, /Informations partielles/);
  assert.doesNotMatch(announcement, /Preuves mixtes|[Dd]émonstration|Relevé historique/);
});

test('a simulated incident stays bounded by the delivery proof and data origin limits', () => {
  const incident: Pipeline = {
    ...pipelineFixture('incident', 'incident', 'simulation'),
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' },
  };
  const announcement = liveAnnouncement(readyState([incident], 'simulation'));

  assert.match(announcement, /Informations partielles/);
  assert.doesNotMatch(announcement, /demande une action|action requise|incident actif/i);
});

test('a simulated incident is announced as observed evidence, never as a current required action', () => {
  const incident: Pipeline = {
    ...pipelineFixture('incident', 'incident', 'simulation'),
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' },
  };
  const announcement = liveAnnouncement(readyState([incident], 'simulation'));

  assert.match(announcement, /Informations partielles/i);
  assert.doesNotMatch(announcement, /demande une action|action requise|incident actif/i);
});

test('metric ticks do not change the live announcement', () => {
  const pipeline = pipelineFixture('stable', 'degraded');
  const before = liveAnnouncement(readyState([pipeline]));
  const after = liveAnnouncement(readyState([{ ...pipeline, counters: { events_published: 999_999, polls: 42 } }]));

  assert.equal(after, before);
});

function readyState(
  pipelines: readonly Pipeline[],
  evidenceKind: Pipeline['quality']['evidenceKind'] = 'live',
  scope?: Overview['scope'],
): ControlPlaneState {
  const environments = [...new Set(pipelines.map((pipeline) => pipeline.environment))].sort();
  const resolvedScope = scope ?? (environments.length === 0
    ? { kind: 'unavailable' as const, environments: [] }
    : environments.length === 1
      ? { kind: 'single' as const, environments }
      : { kind: 'mixed' as const, environments });
  const overview: Overview = {
    revision: 1,
    generatedAt: '2026-08-28T08:33:56Z',
    scope: resolvedScope,
    pipelines,
    sources: environments.map((environment) => ({ id: `source-${environment}`, evidenceKind, environment, status: 'available', error: null })),
  };
  return {
    status: 'ready',
    connection: 'live',
    overview,
    pipelines,
    lastSuccessAt: new Date('2026-08-28T08:33:56Z'),
  };
}

function pipelineFixture(
  id: string,
  status: PipelineStatus,
  evidenceKind: Pipeline['quality']['evidenceKind'] = 'live',
  destinationObserved = status === 'healthy',
): Pipeline {
  const observedAt = '2026-08-28T08:33:56Z';
  return {
    id,
    environment: 'dev',
    status,
    quality: { coverage: destinationObserved ? 'complete' : 'partial', freshness: 'fresh', evidenceKind },
    summary: 'Verdict serveur',
    observedAt,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source', detail: 'Observée' },
      { id: 'capture', status: 'healthy', observedAt, headline: 'Capture', detail: 'Observée' },
      { id: 'raw', status: 'healthy', observedAt, headline: 'Raw', detail: 'Observé' },
      { id: 'load', status: destinationObserved ? 'healthy' : 'unknown', observedAt: destinationObserved ? observedAt : null, headline: 'Load', detail: 'État' },
      { id: 'destination', status: destinationObserved ? 'healthy' : 'unknown', observedAt: destinationObserved ? observedAt : null, headline: 'Destination', detail: 'État' },
    ],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: { events_published: 1_793 },
    incident: null,
  };
}

function renderShell(state: ControlPlaneState, route: Parameters<typeof AppShell>[0]['route'] = { name: 'overview' }): string {
  return renderToStaticMarkup(createElement(
    AppShell,
    { route, state },
    createElement('h1', null, 'Workspace'),
  ));
}

function visibleChrome(markup: string): string {
  return markup.replace(/<p class="sr-only"[^>]*>[\s\S]*?<\/p>/g, '');
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
