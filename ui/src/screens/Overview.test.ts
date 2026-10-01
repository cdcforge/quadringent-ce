import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { FleetCapability, Overview, Pipeline } from '../domain/controlPlane.ts';

const observedAt = '2026-09-13T09:00:00Z';
let vite: ViteDevServer;
let OverviewScreen: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  OverviewScreen = (await vite.ssrLoadModule('/src/screens/Overview.tsx')).Overview;
});

after(async () => { await vite.close(); });

test('l’accueil ouvre sur « Vos liaisons » et son résumé de plateau', () => {
  const markup = render([pipelineFixture('alpha')]);
  assert.match(markup, /<h1 class="board__title">Vos liaisons<\/h1>/);
  assert.match(markup, /Votre liaison est à jour/);
  assert.match(markup, /<button type="button" class="board__refresh"[^>]*>Actualiser<\/button>/);
});

test('loading et failed distinguent une lecture en cours d’un service qui n’a pas répondu', () => {
  const loading = renderToStaticMarkup(createElement(OverviewScreen, {
    state: { status: 'loading', connection: 'connecting' }, overview: null, onRefresh() {},
  }));
  assert.match(loading, /<h2>Lecture en cours<\/h2>/);
  assert.match(loading, /Quadringent interroge le service\. L’état s’affichera dès la première réponse\./);
  assert.match(loading, /Lecture en cours<\/p>/);
  assert.match(loading, />Actualiser<\/button>/);

  const failed = renderToStaticMarkup(createElement(OverviewScreen, {
    state: { status: 'failed', connection: 'offline', message: 'injoignable' }, overview: null, onRefresh() {},
  }));
  assert.match(failed, /<h2>État non lu<\/h2>/);
  assert.match(failed, /Le service n’a pas répondu\. Rien n’est affiché tant qu’un état n’est pas confirmé\./);
  assert.match(failed, /État non lu<\/p>/);
  assert.match(failed, />Réessayer<\/button>/);
  assert.doesNotMatch(loading, /Aucune liaison/);
  assert.doesNotMatch(failed, /Aucune liaison/);
});

test('un relevé confirmé sans liaison invite à créer une liaison, jamais une lecture en cours', () => {
  const overview = overviewFixture([]);
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines: [], lastSuccessAt: new Date(observedAt) };
  const markup = renderToStaticMarkup(createElement(OverviewScreen, { state, overview, onRefresh() {} }));

  assert.match(markup, /<h2>Aucune liaison pour l’instant<\/h2>/);
  assert.match(markup, /<a href="#\/setup">Créer une liaison<\/a>/);
  assert.doesNotMatch(markup, /Lecture en cours/);
});

test('plusieurs liaisons s’affichent, la plus dégradée passe devant', () => {
  const ok = pipelineFixture('alpha', { status: 'healthy' });
  const stopped = pipelineFixture('bravo', { status: 'incident', incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' } });
  const markup = render([ok, stopped]);

  assert.equal((markup.match(/<article class="liaison /g) ?? []).length, 2);
  assert.match(markup, /1 liaison demande votre attention/);
  assert.ok(markup.indexOf('id="liaison-bravo"') < markup.indexOf('id="liaison-alpha"'), 'la liaison en incident doit précéder la liaison à jour');
});

test('un bouton d’action n’apparaît que si le service publie une capacité disponible — jamais une consigne à côté', () => {
  const withCapability = pipelineFixture('alpha', {
    status: 'awaiting_resume',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked', causeResolved: true },
    fleetRuntime: fleetRuntimeFixture({ resume: { state: 'available', reason: null } }),
  });
  const withoutCapability = pipelineFixture('bravo', {
    status: 'awaiting_resume',
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked', causeResolved: true },
    fleetRuntime: fleetRuntimeFixture({ resume: { state: 'unavailable', reason: 'operator_access_unavailable' } }),
  });

  const withMarkup = render([withCapability]);
  assert.match(withMarkup, /<button[^>]*class="liaison__action"[^>]*>Relancer<\/button>/);
  assert.doesNotMatch(withMarkup, /class="liaison__guidance"/);

  const withoutMarkup = render([withoutCapability]);
  assert.doesNotMatch(withoutMarkup, /class="liaison__action"/);
  assert.match(withoutMarkup, /class="liaison__guidance">La relance ne se pilote pas depuis Quadringent\. À demander à votre exploitation\.<\/p>/);
});

test('une action de pilotage passe par une confirmation, jamais par un seul clic', () => {
  // Relancer une lecture ou lancer une première copie engage la source et la
  // destination. Le rendu initial ne montre donc jamais « Confirmer » : il
  // montre l'action, et sa conséquence en toutes lettres.
  const markup = render([
    pipelineFixture('alpha', {
      status: 'awaiting_resume',
      incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked', causeResolved: true },
      fleetRuntime: fleetRuntimeFixture({ resume: { state: 'available', reason: null } }),
    }),
  ]);
  assert.match(markup, /<button[^>]*class="liaison__action"[^>]*>Relancer<\/button>/);
  assert.match(markup, /class="liaison__effect">Reprend la lecture là où elle s’était arrêtée\.<\/span>/);
  assert.doesNotMatch(markup, /liaison__confirm/);
});

test('une démonstration est signalée avant d’être lue comme une mesure', () => {
  const simulated = render([
    pipelineFixture('alpha', {
      quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'simulation' },
    }),
  ]);
  assert.match(simulated, /class="liaison__caveat"[^>]*>Démonstration — ces chiffres ne viennent pas de votre système\.<\/p>/);
});

test('une lecture réelle et fraîche n’affiche aucun avertissement', () => {
  // Le relevé est daté à l'exécution : c'est le seul moyen d'être frais face à
  // l'horloge du poste qui rend la page.
  const fresh = new Date().toISOString();
  const markup = render([
    pipelineFixture('bravo', {
      observedAt: fresh,
      stages: [{ id: 'source', status: 'healthy', observedAt: fresh, headline: 'Connexion vérifiée', detail: '' }],
    } as Partial<Pipeline>),
  ]);
  assert.doesNotMatch(markup, /liaison__caveat/);
});

test('un relevé réel mais périmé le dit, sans se faire passer pour courant', () => {
  const markup = render([pipelineFixture('charlie')]);
  assert.match(markup, /class="liaison__caveat"[^>]*>Ce relevé n’est plus récent : l’état a pu changer depuis\.<\/p>/);
});

test('les faits absents disent « Non mesuré », jamais zéro, sur la carte d’accueil', () => {
  const markup = render([pipelineFixture('alpha', { destination: null, fleetRuntime: null })]);
  assert.match(markup, /<dd class="liaison__fact-absent" data-numeric="">Non mesuré<\/dd>/);
  assert.doesNotMatch(markup, /liaison__fact[^>]*>\s*<dd[^>]*data-numeric="">0</);
});

test('les styles de la carte de liaison restent sans décor interdit', () => {
  const css = readFileSync(new URL('../styles/liaison.css', import.meta.url), 'utf8');
  assert.match(css, /\.liaison\s*\{/);
  assert.doesNotMatch(css, /linear-gradient|radial-gradient|backdrop-filter|blur\(/);
});

function render(pipelines: readonly Pipeline[]): string {
  const overview = overviewFixture(pipelines);
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines, lastSuccessAt: new Date(observedAt) };
  return renderToStaticMarkup(createElement(OverviewScreen, { state, overview, onRefresh() {} }));
}

function overviewFixture(pipelines: readonly Pipeline[]): Overview {
  return {
    revision: 1,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources: pipelines.map((pipeline) => ({
      id: `source-${pipeline.id}`,
      environment: pipeline.environment,
      evidenceKind: 'live',
      status: 'available',
      error: null,
    })),
  };
}

function fleetRuntimeFixture(capabilities: Readonly<Record<string, FleetCapability>>): Pipeline['fleetRuntime'] {
  return {
    formatVersion: 'quadringent-fleet-runtime-v1',
    fleetId: 'acme-dev',
    environment: 'dev',
    pipelineId: 'acme',
    phase: 'CERTIFIED',
    checkpoint: null,
    capabilities,
    tableStates: [],
  };
}

function pipelineFixture(id: string, overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id,
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: 'Toutes les étapes déclarées observées',
    observedAt,
    stages: [
      { id: 'source', status: 'healthy', observedAt, headline: 'Source active', detail: 'Observation reçue.' },
      { id: 'capture', status: 'healthy', observedAt, headline: 'Capture active', detail: 'Observation reçue.' },
      { id: 'raw', status: 'healthy', observedAt, headline: 'Raw actif', detail: 'Observation reçue.' },
      { id: 'load', status: 'healthy', observedAt, headline: 'Load actif', detail: 'Observation reçue.' },
      { id: 'destination', status: 'healthy', observedAt, headline: 'Destination active', detail: 'Observation reçue.' },
    ],
    lagSequences: 0,
    lagSeconds: 0,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: null,
    ...overrides,
  };
}
