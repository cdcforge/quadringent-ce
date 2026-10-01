import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type {
  Coverage,
  EvidenceKind,
  Freshness,
  Incident,
  Overview,
  Pipeline,
  PipelineStatus,
  Source,
  Stage,
  StageId,
} from '../domain/controlPlane.ts';

const observedAt = '2026-08-30T09:42:00+00:00';
let vite: ViteDevServer;
let OverviewScreen: ComponentType<any>;
let PipelinesScreen: ComponentType<any>;
let PipelineDetailScreen: ComponentType<any>;
let IncidentsScreen: ComponentType<any>;
let UsageScreen: ComponentType<any>;

before(async () => {
  vite = await createServer({
    server: { middlewareMode: true, hmr: false },
    appType: 'custom',
    logLevel: 'silent',
  });
  const [overview, pipelines, detail, incidents, usage] = await Promise.all([
    vite.ssrLoadModule('/src/screens/Overview.tsx'),
    vite.ssrLoadModule('/src/screens/Pipelines.tsx'),
    vite.ssrLoadModule('/src/screens/PipelineDetail.tsx'),
    vite.ssrLoadModule('/src/screens/Incidents.tsx'),
    vite.ssrLoadModule('/src/screens/Usage.tsx'),
  ]);
  OverviewScreen = overview.Overview;
  PipelinesScreen = pipelines.Pipelines;
  PipelineDetailScreen = detail.PipelineDetail;
  IncidentsScreen = incidents.Incidents;
  UsageScreen = usage.Usage;
});

after(async () => { await vite.close(); });

test('loading and failed-without-cache routes expose unknown availability, never a successful empty state', () => {
  const loading: ControlPlaneState = { status: 'loading', connection: 'connecting' };
  const failed: ControlPlaneState = { status: 'failed', connection: 'offline', message: 'Control plane inaccessible' };

  const overviewLoading = render(OverviewScreen, { state: loading, overview: null, onRefresh() {} });
  const overviewFailed = render(OverviewScreen, { state: failed, overview: null, onRefresh() {} });
  const pipelinesMarkup = render(PipelinesScreen, { state: failed, overview: null, pipelines: [], onRefresh() {} });
  const detailMarkup = render(PipelineDetailScreen, {
    state: failed,
    pipeline: null,
    route: { name: 'pipeline', id: 'alpha', tab: 'overview' },
    onRefresh() {},
  });
  const incidentsLoading = render(IncidentsScreen, { state: loading, overview: null, pipelines: [], onRefresh() {} });
  const incidentsFailed = render(IncidentsScreen, { state: failed, overview: null, pipelines: [], onRefresh() {} });
  const usageLoadingMarkup = render(UsageScreen, { state: loading, overview: null, pipelines: [], onRefresh() {} });
  const usageFailedMarkup = render(UsageScreen, { state: failed, overview: null, pipelines: [], onRefresh() {} });

  // Accueil distingue une lecture en cours d'un service qui n'a pas répondu.
  assert.match(overviewLoading, /<h2>Lecture en cours<\/h2>/);
  assert.match(overviewFailed, /<h2>État non lu<\/h2>/);
  // Tables, Détail et Journal restent bornés — jamais un tableau vide confirmé.
  assert.match(pipelinesMarkup, /<h2>Lecture en cours<\/h2>/);
  assert.match(detailMarkup, /L’état de cette liaison n’a pas encore été lu\./);
  assert.match(incidentsLoading, /<h2>Lecture en cours<\/h2>/);
  assert.match(incidentsFailed, /<h2>Journal non lu<\/h2>/);
  // NOTE — défaut découvert : Consommation ne distingue pas loading de failed
  // (les deux affichent « Lecture en cours »), contrairement à Accueil et
  // Journal. Signalé au rapport, non corrigé ici (règle 6 de la mission).
  assert.match(usageLoadingMarkup, /<h2>Lecture en cours<\/h2>/);
  assert.match(usageFailedMarkup, /<h2>Lecture en cours<\/h2>/);

  for (const markup of [overviewLoading, overviewFailed, pipelinesMarkup, detailMarkup, incidentsLoading, incidentsFailed, usageLoadingMarkup, usageFailedMarkup]) {
    assertNoCurrentDeliveryClaim(markup);
    assert.doesNotMatch(markup, /Aucune liaison|Aucune table|Rien à signaler|Aucun événement n’a été enregistré/);
  }
});

test('cached, partial-source and unavailable-source views remain fail-closed across overview, inventory and detail', () => {
  const completeLive = pipelineFixture();
  const cachedOverview = overviewFixture([completeLive]);
  const cachedState: ControlPlaneState = {
    status: 'degraded',
    connection: 'reconnecting',
    overview: cachedOverview,
    pipelines: cachedOverview.pipelines,
    lastSuccessAt: new Date(observedAt),
    message: 'Rafraîchissement impossible',
  };
  const cachedMarkup = render(OverviewScreen, { state: cachedState, overview: cachedOverview, onRefresh() {} });

  // Le relevé retenu reste affiché, marqué non courant — jamais présenté à jour.
  assert.match(cachedMarkup, new RegExp(`id="liaison-${completeLive.id}"`));
  assert.match(cachedMarkup, /liaison__freshness--old/);
  assertNoCurrentDeliveryClaim(cachedMarkup);

  const partialOverview = overviewFixture([completeLive], [
    sourceFixture({ id: 'source-a', status: 'available' }),
    sourceFixture({ id: 'source-b', status: 'unavailable' }),
  ]);
  const partialState = readyState(partialOverview);
  const partialOverviewMarkup = render(OverviewScreen, { state: partialState, overview: partialOverview, onRefresh() {} });
  const partialDetailMarkup = render(PipelineDetailScreen, {
    state: partialState,
    pipeline: completeLive,
    route: { name: 'pipeline', id: completeLive.id, tab: 'overview' },
    onRefresh() {},
  });

  // NOTE — défaut découvert : aucun écran ne signale plus explicitement une
  // couverture de sources partielle (`SourceAvailabilityBanner` est devenu
  // du code mort — voir scope.test.ts). La liaison reste néanmoins servie
  // sans fabriquer de succès complet.
  assert.match(partialOverviewMarkup, new RegExp(`id="liaison-${completeLive.id}"`));
  assertNoCurrentDeliveryClaim(partialOverviewMarkup);
  assertNoCurrentDeliveryClaim(partialDetailMarkup);

  const unavailableOverview = overviewFixture([completeLive], [sourceFixture({ status: 'unavailable' })]);
  const unavailableState = readyState(unavailableOverview);
  const unavailableMarkup = render(PipelinesScreen, {
    state: unavailableState,
    overview: unavailableOverview,
    pipelines: unavailableOverview.pipelines,
    onRefresh() {},
  });

  assert.match(unavailableMarkup, new RegExp(`id="tables-${completeLive.id}"`));
  assertNoCurrentDeliveryClaim(unavailableMarkup);
});

test('a confirmed empty inventory describes absence as missing proof, not successful delivery', () => {
  const overview = overviewFixture([], [sourceFixture()]);
  const state = readyState(overview);
  const markup = render(PipelinesScreen, { state, overview, pipelines: [], onRefresh() {} });

  assert.match(markup, /<h2>Aucune table<\/h2>/);
  assert.match(markup, /Les tables apparaîtront dès qu’une liaison en déclarera\./);
  assertNoCurrentDeliveryClaim(markup);
});

test('the measures route stays neutral outside fresh live evidence and permits live wording only for fresh complete live proof', () => {
  const stale = pipelineFixture({ status: 'degraded', freshness: 'stale' });
  const staleOverview = overviewFixture([stale]);
  const staleMarkup = render(PipelineDetailScreen, {
    state: readyState(staleOverview),
    pipeline: stale,
    route: { name: 'pipeline', id: stale.id, tab: 'live' },
    onRefresh() {},
  });

  assert.match(staleMarkup, />Mesures</);
  assert.match(staleMarkup, /Relevé trop ancien/);
  assert.match(staleMarkup, /relevé non recevable/);
  assert.doesNotMatch(staleMarkup, />Mesures live</);
  assertNoCurrentDeliveryClaim(staleMarkup);

  const live = pipelineFixture({ lagSequences: 4 });
  const liveOverview = overviewFixture([live]);
  const liveMarkup = render(PipelineDetailScreen, {
    state: readyState(liveOverview),
    pipeline: live,
    route: { name: 'pipeline', id: live.id, tab: 'live' },
    onRefresh() {},
  });

  assert.match(liveMarkup, /Lecture actuelle/);
  assert.match(liveMarkup, /Système réel/);
  assert.match(liveMarkup, /Observation récente/);
  assert.match(liveMarkup, /Retard courant/);
  assert.doesNotMatch(liveMarkup, /Démonstration|Dernier relevé|Informations partielles/);
});

test('simulation and historical incident signals stay evidenced facts, never a fabricated current action; recovering remains explicit', () => {
  for (const evidenceKind of ['simulation', 'historical'] as const) {
    const pipeline = pipelineFixture({
      evidenceKind,
      status: 'incident',
      incident: { code: 'destination_load_failed', type: 'destination' },
    });
    const overview = overviewFixture([pipeline], [sourceFixture({ evidenceKind })]);
    const markup = render(IncidentsScreen, {
      state: readyState(overview),
      overview,
      pipelines: overview.pipelines,
      onRefresh() {},
    });

    assert.match(markup, /<span class="history-attention__state">Interrompue<\/span>/);
    assert.match(markup, new RegExp(`href="#/pipeline/${pipeline.id}"`));
    assert.doesNotMatch(markup, />Une action requise\.</);
    assert.doesNotMatch(markup, />À traiter</);
    assertNoCurrentDeliveryClaim(markup);
  }

  const recovering = pipelineFixture({ status: 'recovering' });
  const recoveringOverview = overviewFixture([recovering]);
  const recoveringMarkup = render(IncidentsScreen, {
    state: readyState(recoveringOverview),
    overview: recoveringOverview,
    pipelines: recoveringOverview.pipelines,
    onRefresh() {},
  });

  assert.match(recoveringMarkup, /<p class="history__label">Rétablissement en cours<\/p>/);
  assert.match(recoveringMarkup, /une lecture complète reste requise pour confirmer/);
  assertNoCurrentDeliveryClaim(recoveringMarkup);
});

test('fresh live usage with partial pipeline coverage still renders measured counters as-is, never a completeness claim', () => {
  const pipeline = pipelineFixture({ coverage: 'partial', counters: { events_published: 12 } });
  const overview = overviewFixture([pipeline]);
  const markup = render(UsageScreen, {
    state: readyState(overview),
    overview,
    pipelines: overview.pipelines,
    onRefresh() {},
  });

  assert.match(markup, /<dt>Enregistrements lus à la source<\/dt><dd data-numeric="">12<\/dd>/);
  // Le module Consommation ne lit ni coverage ni freshness : il ne peut donc
  // pas fabriquer une affirmation de complétude à partir d'une couverture
  // partielle — il n'en fait simplement aucune.
  assert.doesNotMatch(markup, /Système réel|À vérifier|Pas encore disponible/);
});

test('polls-only usage remains a source observation and never invents a captured-events reading', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture({ counters: { polls: 42 } }),
    destination: {
      kind: 'snowflake', database: 'DEV_RAW', schema: 'AS400_RD', stage: null, rawTable: null,
      canonicalTable: null, runTag: null, observedAt, loadCheckpoint: null, applyCheckpoint: null,
      sourceEvents: null, rawRows: null, canonicalRows: 3, duplicates: null,
    },
  };
  const overview = overviewFixture([pipeline]);
  const markup = render(UsageScreen, {
    state: readyState(overview),
    overview,
    pipelines: overview.pipelines,
    onRefresh() {},
  });

  // `polls` ne fait pas partie de la liste blanche de consumption.ts : la
  // ligne « Enregistrements lus à la source » (dérivée d'events_published)
  // reste non mesurée plutôt que d'emprunter la valeur d'un compteur interne.
  assert.match(markup, /<dt>Enregistrements lus à la source<\/dt><dd class="consumption__absent" data-numeric="">Non mesuré<\/dd>/);
  assert.doesNotMatch(markup, /<dd[^>]*data-numeric="">42<\/dd>/);
});

function render(component: ComponentType<any>, props: Readonly<Record<string, unknown>>): string {
  return renderToStaticMarkup(createElement(component, props));
}

function assertNoCurrentDeliveryClaim(markup: string): void {
  for (const forbidden of [
    /Livraison prouvée dans ce snapshot/i,
    /preuve live fraîche/i,
    /Ouvrir les mesures live/i,
    /réconciliation cible est observée dans le snapshot courant/i,
    /Destination (?:prouvée|confirmée|vérifiée)/i,
    />0 table|>0 liaison|>0 incident/i,
  ]) {
    assert.doesNotMatch(markup, forbidden);
  }
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

function overviewFixture(
  pipelines: readonly Pipeline[],
  sources: readonly Source[] = [sourceFixture()],
): Overview {
  return {
    revision: 42,
    generatedAt: observedAt,
    scope: { kind: 'single', environments: ['dev'] },
    pipelines,
    sources,
  };
}

function sourceFixture(overrides: Partial<Source> = {}): Source {
  const status = overrides.status ?? 'available';
  return {
    id: overrides.id ?? 'dev-source',
    environment: overrides.environment ?? 'dev',
    evidenceKind: overrides.evidenceKind ?? 'live',
    status,
    error: overrides.error ?? (status === 'unavailable' ? 'source offline' : null),
  };
}

interface PipelineOverrides {
  readonly status?: PipelineStatus;
  readonly evidenceKind?: EvidenceKind;
  readonly freshness?: Freshness;
  readonly coverage?: Coverage;
  readonly counters?: Pipeline['counters'];
  readonly incident?: Incident | null;
  readonly lagSequences?: number | null;
}

function pipelineFixture(overrides: PipelineOverrides = {}): Pipeline {
  const evidenceKind = overrides.evidenceKind ?? 'live';
  const freshness = overrides.freshness ?? 'fresh';
  const coverage = overrides.coverage ?? 'complete';
  const status = overrides.status ?? (evidenceKind === 'live' && freshness === 'fresh' && coverage === 'complete' ? 'healthy' : 'degraded');
  return {
    id: 'alpha',
    environment: 'dev',
    status,
    quality: { coverage, freshness, evidenceKind },
    summary: status === 'recovering' ? 'Reprise observée, livraison à reconfirmer' : 'État borné au snapshot',
    observedAt,
    stages: completeStages(),
    lagSequences: overrides.lagSequences ?? null,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: overrides.counters ?? {},
    incident: overrides.incident ?? null,
  };
}

function completeStages(): readonly Stage[] {
  const labels: Readonly<Record<StageId, string>> = {
    source: 'Source observée',
    capture: 'Capture observée',
    raw: 'Raw observé',
    load: 'Chargement observé',
    destination: 'Destination observée',
  };
  return (['source', 'capture', 'raw', 'load', 'destination'] as const).map((id) => ({
    id,
    status: 'healthy',
    observedAt,
    headline: labels[id],
    detail: 'Signal reçu dans la portée du snapshot.',
  }));
}
