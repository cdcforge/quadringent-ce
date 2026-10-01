import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { Overview, Pipeline } from './controlPlane.ts';

const observedAt = '2026-08-28T08:33:56.470000+00:00';
let vite: ViteDevServer;

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('../../', import.meta.url)),
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true, hmr: false },
  });
});

after(async () => {
  await vite.close();
});

test('incidents route has one main, one h1, labeled navigation and truthful history', async () => {
  const { AppShell, Incidents } = await operatorComponents();
  const pipelines = [
    pipelineFixture('structured', 'incident', { code: 'capture_stopped_fail_closed', type: 'capture_stopped' }),
    pipelineFixture('derived', 'incident', null),
  ] as const;
  const overview = overviewFixture(pipelines);
  const state = readyState(overview);
  const markup = renderToStaticMarkup(createElement(
    AppShell,
    { route: { name: 'incidents' }, state },
    createElement(Incidents, { state, overview, pipelines, onRefresh() {} }),
  ));

  assert.equal(count(markup, /<main\b/g), 1);
  assert.equal(count(markup, /<h1\b/g), 1);
  assert.match(markup, /aria-label="Destinations principales"/);
  assert.match(markup, /<h1 class="board__title">Journal<\/h1>/);
  assert.match(markup, /Ce qui demande votre attention/);
  assert.match(markup, /href="#\/pipeline\/structured"/);
  assert.match(markup, /href="#\/pipeline\/derived"/);
  // Un incident nommé par le service reste traduit ; un incident sans type
  // servi ne prétend pas à un motif qu'il ne connaît pas.
  assert.match(markup, /Capture arrêtée en sécurité/);
  assert.doesNotMatch(markup, /Incident actif|Voir la preuve courante|verdict courant/i);
  assert.doesNotMatch(markup, /mesuré|déduit|constaté · figé/i);
  assert.doesNotMatch(markup, /title=/);
  assertNoUnnamedButtons(markup);
});

test('usage route exposes measured evidence without inventing cost or estimate', async () => {
  const { AppShell, Usage } = await operatorComponents();
  const pipelines = [pipelineFixture('alpha', 'degraded', null)] as const;
  const overview = overviewFixture(pipelines);
  const state = readyState(overview);
  const markup = renderToStaticMarkup(createElement(
    AppShell,
    { route: { name: 'usage' }, state },
    createElement(Usage, { state, overview, pipelines, onRefresh() {} }),
  ));

  assert.equal(count(markup, /<main\b/g), 1);
  assert.equal(count(markup, /<h1\b/g), 1);
  assert.match(markup, /<h1 class="board__title">Consommation<\/h1>/);
  assert.match(markup, /<dt>Enregistrements lus à la source<\/dt><dd data-numeric="">1\s?793<\/dd>/);
  assert.match(markup, /Non mesuré/);
  assert.match(markup, /Aucun prix déclaré : les crédits restent affichés sans conversion en devise\./);
  // Aucune estimation, aucun crédit : le service ne publie ni l’un ni l’autre.
  assert.doesNotMatch(markup, /Estimation/);
  assert.doesNotMatch(markup, /crédits consommés|Pas encore disponible/);
  assert.doesNotMatch(markup, /title=/);
  assertNoUnnamedButtons(markup);
});

test('the shell exposes exactly one polite live region across desktop and mobile', async () => {
  const { AppShell } = await operatorComponents();
  const overview = overviewFixture([pipelineFixture('alpha', 'degraded', null)]);
  const state = readyState(overview);
  const markup = renderToStaticMarkup(createElement(
    AppShell,
    { route: { name: 'overview' }, state },
    createElement('h1', null, 'Overview'),
  ));

  assert.equal(count(markup, /aria-live="polite"/g), 1);
  assert.match(markup, /Le service répond/);
  assert.match(markup, /<dt>Service<\/dt><dd>Disponible<\/dd>/);
  assert.match(markup, /<dt>État des données<\/dt><dd>Informations partielles<\/dd>/);
  assert.doesNotMatch(markup, /demande une action|action requise|incident actif/i);
});

test('the shell gives operators one branded context and predictable destinations', async () => {
  const { AppShell } = await operatorComponents();
  const overview = overviewFixture([pipelineFixture('alpha', 'degraded', null)]);
  const state = readyState(overview);
  const markup = renderToStaticMarkup(createElement(
    AppShell,
    { route: { name: 'overview' }, state },
    createElement('h1', null, 'workspace'),
  ));

  assert.match(markup, /aria-label="Quadringent"/);
  assert.match(markup, />Liaisons</);
  assert.match(markup, />Tables</);
  assert.match(markup, />Journal</);
  assert.match(markup, />Consommation</);
  assert.match(markup, />Installation</);
  assert.equal(count(markup, /aria-live="polite"/g), 1);
});

test('the shell and overview expose one topbar and one compact data register', async () => {
  const [{ AppShell }, { Overview }] = await Promise.all([
    vite.ssrLoadModule('/src/components/AppShell.tsx'),
    vite.ssrLoadModule('/src/screens/Overview.tsx'),
  ]);
  const overview = overviewFixture([pipelineFixture('alpha', 'degraded', null)]);
  const state = readyState(overview);
  const markup = renderToStaticMarkup(createElement(
    AppShell,
    { route: { name: 'overview' }, state },
    createElement(Overview, { state, overview, onRefresh() {} }),
  ));

  assert.match(markup, /<header class="product-topbar"/);
  assert.doesNotMatch(markup, /<aside class="product-rail"/);
  assert.equal(count(markup, /class="board__refresh"/g), 1);
  assert.match(markup, /<h1[^>]*>Vos liaisons<\/h1>/);
  assert.match(markup, /class="board__list"/);
  assert.match(markup, /class="liaison /);
  assert.doesNotMatch(markup, /class="proof-focus|Chaîne de preuve|État en aval|class="tmat"/);
  // Aucun horodatage brut ne fuite dans le texte visible : la fraîcheur passe
  // par le registre en clair (`view.freshness`), jamais par l'ISO servi.
  assert.doesNotMatch(markup, />[^<]*2026-08-28T08:33:56/);
});

test('the shell distinguishes snapshot generation from API reception time', async () => {
  const { AppShell } = await operatorComponents();
  const overview = overviewFixture([pipelineFixture('alpha', 'degraded', null)]);
  const state: ControlPlaneState = {
    ...readyState(overview),
    lastSuccessAt: new Date('2026-08-28T11:04:05.000Z'),
  };
  const markup = renderToStaticMarkup(createElement(
    AppShell,
    { route: { name: 'overview' }, state },
    createElement('h1', null, 'workspace'),
  ));

  assert.match(markup, /<dt>Relevé du<\/dt><dd><time dateTime="2026-08-28T08:33:56\.470000\+00:00">/);
  assert.match(markup, /<dt>Reçu le<\/dt><dd><time dateTime="2026-08-28T11:04:05\.000Z">/);
});

test('operator empty states distinguish confirmed absence from unavailable proof', async () => {
  const { Incidents, Usage } = await operatorComponents();
  const empty = overviewFixture([]);
  const ready = readyState(empty);
  const failed: ControlPlaneState = { status: 'failed', connection: 'offline', message: 'offline' };

  const confirmed = [
    renderToStaticMarkup(createElement(Incidents, { state: ready, overview: empty, pipelines: [], onRefresh() {} })),
    renderToStaticMarkup(createElement(Usage, { state: ready, overview: empty, pipelines: [], onRefresh() {} })),
  ].join(' ');
  assert.match(confirmed, /Rien à signaler/);
  assert.match(confirmed, /Aucun événement n’a été enregistré sur vos liaisons\./);
  assert.match(confirmed, /<h2>Aucune liaison<\/h2>/);
  assert.match(confirmed, /La consommation apparaîtra dès qu’une liaison aura tourné\./);

  // NOTE : contrairement à Incidents (« Journal non lu » vs « Lecture en
  // cours »), l'écran Consommation ne distingue pas encore un échec confirmé
  // d'une lecture en cours — les deux affichent « Lecture en cours ». C'est un
  // défaut découvert pendant cette migration de tests, signalé au rapport
  // plutôt que corrigé silencieusement ici (règle 6 de la mission).
  const unavailable = [
    renderToStaticMarkup(createElement(Incidents, { state: failed, overview: null, pipelines: [], onRefresh() {} })),
    renderToStaticMarkup(createElement(Usage, { state: failed, overview: null, pipelines: [], onRefresh() {} })),
  ].join(' ');
  assert.match(unavailable, /<h2>Journal non lu<\/h2>/);
  assert.match(unavailable, /Le service n’a pas répondu\. Aucun événement n’est affiché\./);
  assert.match(unavailable, /<h2>Lecture en cours<\/h2>/);
  assert.doesNotMatch(unavailable, /Rien à signaler|Aucune liaison</);
  assert.doesNotMatch(unavailable, /credentials|Garantie vérifiée|Healthy/i);
});

function assertNoUnnamedButtons(markup: string): void {
  const buttons = [...markup.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)];
  assert.ok(buttons.length > 0);
  for (const [, attributes = '', content = ''] of buttons) {
    const text = content.replace(/<[^>]+>/g, '').trim();
    assert.ok(text || /aria-label="[^"]+"/.test(attributes), `bouton sans nom: ${attributes}`);
  }
}

async function operatorComponents(): Promise<{
  readonly AppShell: import('react').ComponentType<any>;
  readonly Incidents: import('react').ComponentType<any>;
  readonly Usage: import('react').ComponentType<any>;
}> {
  const [{ AppShell }, { Incidents }, { Usage }] = await Promise.all([
    vite.ssrLoadModule('/src/components/AppShell.tsx'),
    vite.ssrLoadModule('/src/screens/Incidents.tsx'),
    vite.ssrLoadModule('/src/screens/Usage.tsx'),
  ]);
  return { AppShell, Incidents, Usage };
}

function count(value: string, pattern: RegExp): number {
  return [...value.matchAll(pattern)].length;
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

function overviewFixture(pipelines: readonly Pipeline[]): Overview {
  return {
    revision: 3,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources: [{ id: 'fixture', evidenceKind: 'simulation', environment: 'dev', status: 'available', error: null }],
  };
}

function pipelineFixture(
  id: string,
  status: Pipeline['status'],
  incident: Pipeline['incident'],
): Pipeline {
  return {
    id,
    environment: 'dev',
    status,
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'simulation' },
    summary: 'Livraison Snowflake non prouvée',
    observedAt,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source observée', detail: 'Signal présent' },
      { id: 'capture', status: incident ? 'incident' : 'healthy', observedAt, headline: 'Capture observée', detail: 'Signal présent' },
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
    counters: { events_published: 1_793, payload_bytes_published: 622_091, events_in_target: null },
    incident,
  };
}
