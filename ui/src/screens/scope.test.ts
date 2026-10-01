import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Overview, Pipeline } from '../domain/controlPlane.ts';

const observedAt = '2026-08-28T10:00:00+00:00';
let vite: ViteDevServer;
let AppShell: ComponentType<any>;
let OverviewScreen: ComponentType<any>;
let PipelinesScreen: ComponentType<any>;
let IncidentsScreen: ComponentType<any>;
let UsageScreen: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  const [shell, overview, pipelines, incidents, usage] = await Promise.all([
    vite.ssrLoadModule('/src/components/AppShell.tsx'),
    vite.ssrLoadModule('/src/screens/Overview.tsx'),
    vite.ssrLoadModule('/src/screens/Pipelines.tsx'),
    vite.ssrLoadModule('/src/screens/Incidents.tsx'),
    vite.ssrLoadModule('/src/screens/Usage.tsx'),
  ]);
  AppShell = shell.AppShell;
  OverviewScreen = overview.Overview;
  PipelinesScreen = pipelines.Pipelines;
  IncidentsScreen = incidents.Incidents;
  UsageScreen = usage.Usage;
});

after(async () => { await vite.close(); });

test('la coquille affiche le bon environnement pour chaque périmètre, et lui seul le répète', () => {
  // Depuis la refonte, l'environnement n'est plus répété sur chaque écran
  // (cf. commentaire d'AppShell.tsx : « Un seul repère dans l'en-tête »).
  // Les écrans eux-mêmes ne doivent donc jamais afficher un environnement
  // codé en dur qui contredirait celui de la coquille.
  const cases = [
    { scope: { kind: 'single', environments: ['local'] }, expected: 'LOCAL' },
    { scope: { kind: 'mixed', environments: ['dev', 'prod'] }, expected: 'DEV, PROD' },
    { scope: { kind: 'unavailable', environments: [] }, expected: 'Non confirmé' },
  ] as const;

  for (const fixture of cases) {
    const overview = overviewFixture(fixture.scope, 'available');
    const state = readyState(overview);
    const shellMarkup = renderToStaticMarkup(createElement(AppShell, { route: { name: 'overview' }, state }, createElement('p', null, 'contenu')));
    assert.match(shellMarkup, new RegExp(`<strong>${fixture.expected}</strong>`));

    const screens = [
      renderToStaticMarkup(createElement(OverviewScreen, { state, overview, onRefresh() {} })),
      renderToStaticMarkup(createElement(PipelinesScreen, { state, overview, pipelines: overview.pipelines, onRefresh() {} })),
      renderToStaticMarkup(createElement(IncidentsScreen, { state, overview, pipelines: overview.pipelines, onRefresh() {} })),
      renderToStaticMarkup(createElement(UsageScreen, { state, overview, pipelines: overview.pipelines, onRefresh() {} })),
    ];
    for (const markup of screens) {
      assert.doesNotMatch(markup, /workspace DEV|scope DEV|inventaire DEV|>DEV</);
    }
  }
});

test('des sources indisponibles ne fabriquent jamais une confiance vide sur aucun écran opérateur', () => {
  // NOTE — défaut découvert : `SourceAvailabilityBanner` et
  // `sourceAvailabilityCopy()` (src/domain/scope.ts, src/components/
  // SourceAvailabilityBanner.tsx) ne sont plus importés par AUCUN écran.
  // Aucun écran ne signale donc plus explicitement « Sources indisponibles »
  // ou « Sources partiellement indisponibles » quand la connexion au control
  // plane reste live mais que des sources sont marquées indisponibles dans le
  // relevé. Ce test vérifie ce qui reste vrai : les écrans ne prétendent
  // jamais à un succès vide (0 liaison, 0 incident) quand des données sont
  // réellement retenues. Le bandeau perdu est signalé au rapport final,
  // pas corrigé silencieusement ici (règle 6 de la mission).
  const overview = overviewFixture({ kind: 'single', environments: ['local'] }, 'unavailable');
  const state: ControlPlaneState = {
    status: 'degraded',
    connection: 'reconnecting',
    overview,
    pipelines: overview.pipelines,
    lastSuccessAt: new Date(observedAt),
    message: 'Source locale indisponible',
  };

  const overviewMarkup = renderToStaticMarkup(createElement(OverviewScreen, { state, overview, onRefresh() {} }));
  const pipelinesMarkup = renderToStaticMarkup(createElement(PipelinesScreen, { state, overview, pipelines: overview.pipelines, onRefresh() {} }));
  const incidentsMarkup = renderToStaticMarkup(createElement(IncidentsScreen, { state, overview, pipelines: overview.pipelines, onRefresh() {} }));
  const usageMarkup = renderToStaticMarkup(createElement(UsageScreen, { state, overview, pipelines: overview.pipelines, onRefresh() {} }));

  // La liaison retenue reste affichée — jamais une liste vide rassurante.
  assert.match(overviewMarkup, /id="liaison-cached-pipeline"/);
  assert.doesNotMatch(overviewMarkup, /Aucune liaison pour l’instant/);
  assert.match(pipelinesMarkup, /id="tables-cached-pipeline"/);
  assert.doesNotMatch(incidentsMarkup, />0 signal|Aucun incident structuré courant/);
  assert.doesNotMatch(usageMarkup, />0 mesure|Aucune valeur mesurée disponible/);
});

test('une source indisponible ne transforme jamais une destination retenue en total confirmé', () => {
  const base = overviewFixture({ kind: 'single', environments: ['local'] }, 'unavailable');
  const retained: Pipeline = {
    ...base.pipelines[0]!,
    stages: base.pipelines[0]!.stages.map((stage) => (stage.id === 'load' || stage.id === 'destination'
      ? { ...stage, status: 'healthy' as const, observedAt }
      : stage)),
  };
  const overview: Overview = { ...base, pipelines: [retained] };
  const state = readyState(overview);
  const markup = renderToStaticMarkup(createElement(OverviewScreen, { state, overview, onRefresh() {} }));

  // La liaison reste servie avec ses faits mesurés — mais un compte non publié
  // (ici, aucune ligne/destination mesurée dans ce relevé minimal) reste
  // « Non mesuré », jamais 0 ni une confirmation fabriquée.
  assert.match(markup, /id="liaison-cached-pipeline"/);
  assert.doesNotMatch(markup, /<dd class="liaison__fact-absent" data-numeric="">0<\/dd>/);
  assert.doesNotMatch(markup, /<dt>Destinations non vérifiées<\/dt>/);
});

test('the transformed HTML fallback title stays environment-neutral', async () => {
  const source = await readFile(new URL('../../index.html', import.meta.url), 'utf8');
  const html = await vite.transformIndexHtml('/', source);

  assert.match(html, /<title>Quadringent — cockpit opérateur<\/title>/);
  assert.doesNotMatch(html, /<title>[^<]*DEV[^<]*<\/title>/);
});

function overviewFixture(
  scope: { readonly kind: 'single' | 'mixed' | 'unavailable'; readonly environments: readonly string[] },
  sourceStatus: 'available' | 'unavailable',
): Overview {
  const pipeline = pipelineFixture();
  return {
    revision: 4,
    generatedAt: observedAt,
    scope,
    pipelines: [pipeline],
    sources: [{ id: 'local-source', evidenceKind: 'live', environment: 'local', status: sourceStatus, error: sourceStatus === 'unavailable' ? 'offline' : null }],
  } as Overview;
}

function readyState(overview: Overview): ControlPlaneState {
  return { status: 'ready', connection: 'live', overview, pipelines: overview.pipelines, lastSuccessAt: new Date(observedAt) };
}

function pipelineFixture(): Pipeline {
  return {
    id: 'cached-pipeline',
    environment: 'local',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'stale', evidenceKind: 'live' },
    summary: 'Snapshot retenu, source indisponible',
    observedAt,
    stages: [
      { id: 'source', status: 'unknown', observedAt: null, headline: 'Source non observée', detail: 'Indisponible' },
      { id: 'capture', status: 'unknown', observedAt: null, headline: 'Capture non observée', detail: 'Indisponible' },
      { id: 'raw', status: 'unknown', observedAt: null, headline: 'Raw non observé', detail: 'Indisponible' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'Load non observé', detail: 'Indisponible' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'Destination non observée', detail: 'Indisponible' },
    ],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: null,
  };
}
