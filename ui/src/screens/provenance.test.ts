import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { Overview, Pipeline } from '../domain/controlPlane.ts';
import type { ControlPlaneState } from '../data/controlPlaneController.ts';

/**
 * Provenance des données — où elle est dite, et pourquoi.
 *
 * L'ancien badge de provenance s'affichait sur chaque carte, y compris pour
 * une lecture réelle (« En direct »), ce qui en faisait une décoration : un
 * marqueur présent partout ne signale plus rien.
 *
 * La règle retenue est asymétrique. Une lecture réelle et courante n'a rien à
 * annoncer. Une donnée qui n'en est pas une — démonstration, rejeu d'archive —
 * est signalée sur la carte elle-même, car rien d'autre à l'écran ne la
 * distingue d'une mesure. Le registre complet reste dans l'onglet Mesures.
 */

let vite: ViteDevServer;
let OverviewScreen: (props: { readonly state: ControlPlaneState; readonly overview: Overview; readonly onRefresh: () => void }) => ReturnType<typeof createElement>;
let PipelinesScreen: (props: { readonly state: ControlPlaneState; readonly overview: Overview; readonly pipelines: readonly Overview['pipelines'][number][]; readonly onRefresh: () => void }) => ReturnType<typeof createElement>;
let PipelineDetailScreen: (props: Record<string, unknown>) => ReturnType<typeof createElement>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  OverviewScreen = (await vite.ssrLoadModule('/src/screens/Overview.tsx') as { Overview: typeof OverviewScreen }).Overview;
  PipelinesScreen = (await vite.ssrLoadModule('/src/screens/Pipelines.tsx') as { Pipelines: typeof PipelinesScreen }).Pipelines;
  PipelineDetailScreen = (await vite.ssrLoadModule('/src/screens/PipelineDetail.tsx') as { PipelineDetail: typeof PipelineDetailScreen }).PipelineDetail;
});

after(async () => { await vite.close(); });

const cases = [
  { kind: 'live' as const, label: 'Lecture actuelle' },
  { kind: 'simulation' as const, label: 'Démonstration' },
  { kind: 'historical' as const, label: 'Dernier relevé' },
];

for (const provenance of cases) {
  test(`l’onglet Mesures nomme la provenance ${provenance.kind} dans le registre « Provenance et portée »`, () => {
    const pipeline = pipelineFixture(provenance.kind, 'dev-0');
    const overview = fixture([provenance.kind]);
    const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines: overview.pipelines, lastSuccessAt: new Date('2026-08-28T10:01:00Z') };
    const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
      state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'live' }, onRefresh: () => {},
    }));

    assert.match(markup, new RegExp(`<dt>Origine du relevé</dt><dd>service / ${pipeline.id} / ${provenance.label}</dd>`));
  });
}

test('une donnée qui n’est pas une mesure est signalée sur la carte elle-même', () => {
  for (const kind of ['simulation', 'historical'] as const) {
    const overview = fixture([kind]);
    const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines: overview.pipelines, lastSuccessAt: new Date('2026-08-28T10:01:00Z') };
    const markup = renderToStaticMarkup(createElement(OverviewScreen, { state, overview, onRefresh: () => {} }));

    assert.match(markup, /class="liaison__caveat"/);
    assert.match(markup, kind === 'simulation' ? /Démonstration/ : /archive/);
    assert.match(markup, new RegExp(`id="liaison-${overview.pipelines[0]!.id}"`));
  }
});

test('le registre des tables ne porte pas ce marquage : il n’affirme aucun état', () => {
  // Tables liste un avancement de copie, pas un état de liaison : y répéter
  // l'avertissement en ferait une décoration présente partout.
  for (const provenance of cases) {
    const overview = fixture([provenance.kind]);
    const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines: overview.pipelines, lastSuccessAt: new Date('2026-08-28T10:01:00Z') };
    const markup = renderToStaticMarkup(createElement(PipelinesScreen, { state, overview, pipelines: overview.pipelines, onRefresh: () => {} }));
    assert.doesNotMatch(markup, /Simulation|Démonstration|Relevé historique|Lecture actuelle/);
  }
});

function fixture(kinds: readonly Pipeline['quality']['evidenceKind'][]): Overview {
  const pipelines = kinds.map((evidenceKind, index) => pipelineFixture(evidenceKind, `dev-${index}`));
  return {
    revision: 8,
    generatedAt: '2026-08-28T10:00:00+00:00',
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources: kinds.map((evidenceKind, index) => ({ id: `dev-${index}`, environment: 'dev', evidenceKind, status: 'available' as const, error: null })),
  };
}

function pipelineFixture(evidenceKind: Pipeline['quality']['evidenceKind'], id: string): Pipeline {
  const observedAt = '2026-08-28T10:00:00+00:00';
  return {
    id,
    environment: 'dev',
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind },
    summary: 'Toutes les étapes déclarées observées',
    observedAt,
    stages: (['source', 'capture', 'raw', 'load', 'destination'] as const).map((stage) => ({
      id: stage,
      status: 'healthy' as const,
      observedAt,
      headline: `${stage} actif`,
      detail: 'Observation reçue.',
    })),
    lagSequences: 0,
    lagSeconds: 0,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: null,
  };
}
