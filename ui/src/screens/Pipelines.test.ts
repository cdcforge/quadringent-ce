import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { FleetRuntimeTableState, Overview, Pipeline } from '../domain/controlPlane.ts';

const observedAt = '2026-09-13T09:42:00+00:00';
let vite: ViteDevServer;
let PipelinesScreen: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  PipelinesScreen = (await vite.ssrLoadModule('/src/screens/Pipelines.tsx')).Pipelines;
});

after(async () => { await vite.close(); });

test('l’écran Tables ouvre sur l’en-tête produit et le bouton d’actualisation', () => {
  const markup = render([pipelineFixture('alpha', [tableState('SALE', 'CERTIFIED', 10, 10)])]);

  assert.match(markup, /<h1 class="board__title">Tables<\/h1>/);
  assert.match(markup, /Vos tables AS400 et l’avancement de leur copie\./);
  assert.match(markup, /<button type="button" class="board__refresh">Actualiser<\/button>/);
  assert.match(markup, /<h2 class="tables__title" id="tables-alpha"><a href="#\/pipeline\/alpha">alpha<\/a><\/h2>/);
});

test('loading et failed restent une lecture en cours, jamais une absence confirmée', () => {
  for (const state of [
    { status: 'loading', connection: 'connecting' } as const,
    { status: 'failed', connection: 'offline', message: 'offline' } as const,
  ]) {
    const markup = renderToStaticMarkup(createElement(PipelinesScreen, { state, overview: null, pipelines: [], onRefresh() {} }));
    assert.match(markup, /<h2>Lecture en cours<\/h2>/);
    assert.match(markup, /Quadringent interroge le service\./);
    assert.doesNotMatch(markup, /Aucune table/);
  }
});

test('un relevé confirmé sans liaison dit l’absence de tables, jamais une lecture en cours', () => {
  const markup = render([]);
  assert.match(markup, /<h2>Aucune table<\/h2>/);
  assert.match(markup, /Les tables apparaîtront dès qu’une liaison en déclarera\./);
  assert.doesNotMatch(markup, /Lecture en cours/);
});

test('plusieurs liaisons produisent plusieurs registres de tables', () => {
  const markup = render([
    pipelineFixture('alpha', [tableState('SALE', 'CERTIFIED', 10, 10)]),
    pipelineFixture('bravo', [tableState('CNTR', 'LIVE', 3, 10)]),
  ]);
  assert.equal((markup.match(/<section class="tables"/g) ?? []).length, 2);
  assert.match(markup, /id="tables-alpha"/);
  assert.match(markup, /id="tables-bravo"/);
});

test('les problèmes remontent en tête, à état égal l’ordre reste alphabétique', () => {
  const markup = render([pipelineFixture('alpha', [
    tableState('ZZZ', 'CERTIFIED', 10, 10),
    tableState('AAA', 'CERTIFIED', 10, 10),
    tableState('MMM', 'BLOCKED', null, null),
  ])]);
  const names = [...markup.matchAll(/class="tables__name">([^<]+)</g)].map((match) => match[1]);
  assert.deepEqual(names, ['MMM', 'AAA', 'ZZZ']);
  assert.match(markup, /class="tables__row tables__row--bloquee"/);
});

test('un compte de lignes absent se dit « Non mesuré », jamais zéro', () => {
  const markup = render([pipelineFixture('alpha', [tableState('MMM', 'BLOCKED', null, null)])]);
  assert.match(markup, /<span class="tables__rows tables__rows--absent" data-numeric="">Non mesuré<\/span>/);
  assert.doesNotMatch(markup, /tables__rows"[^>]*>0</);
});

test('une copie en cours affiche une barre d’avancement, une table terminée n’en affiche aucune', () => {
  const markup = render([pipelineFixture('alpha', [
    tableState('ENCOURS', 'LIVE', 50, 200),
    tableState('FINIE', 'CERTIFIED', 200, 200),
  ])]);
  assert.match(markup, /aria-label="Copie à 25 %"/);
  const encoursRow = markup.match(/<li class="tables__row tables__row--encours">[\s\S]*?<\/li>/)?.[0] ?? '';
  assert.match(encoursRow, /tables__progress-fill" style="width:25%"/);
  const finieRow = markup.match(/<li class="tables__row tables__row--copiee">[\s\S]*?<\/li>/)?.[0] ?? '';
  assert.doesNotMatch(finieRow, /tables__progress/);
});

test('la recherche n’apparaît qu’à partir de neuf tables et filtre la liste affichée', () => {
  const fewStates = Array.from({ length: 8 }, (_, index) => tableState(`T${index}`, 'CERTIFIED', 1, 1));
  const fewMarkup = render([pipelineFixture('alpha', fewStates)]);
  assert.doesNotMatch(fewMarkup, /tables__search/);

  const manyStates = Array.from({ length: 9 }, (_, index) => tableState(`T${index}`, 'CERTIFIED', 1, 1));
  const manyMarkup = render([pipelineFixture('alpha', manyStates)]);
  assert.match(manyMarkup, /class="tables__search"/);
  assert.match(manyMarkup, /aria-label="Chercher une table de alpha"/);
});

test('un pipeline sans table déclarée le dit honnêtement', () => {
  const markup = render([pipelineFixture('alpha', [])]);
  assert.match(markup, /<p class="tables__summary">Aucune table déclarée<\/p>/);
});

test('les styles du registre de tables existent et restent sans décor interdit', () => {
  const css = readFileSync(new URL('../styles/tables.css', import.meta.url), 'utf8');
  assert.match(css, /\.tables\s*\{/);
  assert.match(css, /\.tables__row\s*\{/);
  assert.match(css, /\.tables__progress\s*\{/);
  assert.doesNotMatch(css, /linear-gradient|radial-gradient|backdrop-filter|blur\(/);
});

function render(pipelines: readonly Pipeline[]): string {
  const overview = overviewFixture(pipelines);
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines, lastSuccessAt: new Date(observedAt) };
  return renderToStaticMarkup(createElement(PipelinesScreen, { state, overview, pipelines, onRefresh() {} }));
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

function tableState(name: string, phase: FleetRuntimeTableState['phase'], copiedRows: number | null, totalRows: number | null): FleetRuntimeTableState {
  return { name, phase, copiedRows, totalRows };
}

function pipelineFixture(id: string, tableStates: readonly FleetRuntimeTableState[]): Pipeline {
  return {
    id,
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    summary: 'État observé',
    observedAt,
    stages: [],
    lagSequences: null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: null,
    fleetRuntime: {
      formatVersion: 'quadringent-fleet-runtime-v1',
      fleetId: `${id}-dev`,
      environment: 'dev',
      pipelineId: id,
      phase: 'CERTIFIED',
      checkpoint: null,
      capabilities: {},
      tableStates,
    },
  };
}
