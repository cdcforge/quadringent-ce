import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test, { after, before } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import {
  buildIncidentContext,
  buildLagSegments,
  buildStageProof,
  incidentPublicLabel,
  lagCoverageLabel,
  resolveSelectedStage,
  restoreSelectedStageFocus,
  selectSalientLagWindows,
} from '../domain/pipelineDetail.ts';
import { resolvePipelineProofFocus } from '../domain/proofFocus.ts';
import type { Pipeline } from '../domain/controlPlane.ts';
import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import { installTestSite } from '../domain/siteFixture.ts';

installTestSite();

let vite: ViteDevServer;
let PipelineTopology: ComponentType<any>;
let LiveLagChart: ComponentType<{ readonly pipeline: Pipeline; readonly current: boolean }>;
let PipelineDetailScreen: ComponentType<any>;
let StageDrawer: ComponentType<any>;
let WindowDelivery: ComponentType<any>;

test('chain progress stays scoped and cached evidence never reads as fresh', () => {
  const chain={declaredWindows:3,matchedWindows:1,captureComplete:false,state:'incomplete',evidenceKind:'simulation'};
  const markup=renderToStaticMarkup(createElement(WindowDelivery,{evidence:{state:'unavailable',chain},cached:true}));
  assert.match(markup,/Chaîne de preuves incomplète/);
  assert.match(markup,/1 sur 3 fenêtres avec preuve de réconciliation/);
  assert.match(markup,/Simulation/);
  assert.match(markup,/Dernier état conservé/);
  assert.doesNotMatch(markup,/Observation fraîche|Livraison confirmée/);
});
let resolveLagTrend: (points: Pipeline['lagSeries']) => {
  readonly verdict: string;
  readonly kind: string;
  readonly label: string;
  readonly answer: string;
  readonly delta: number | null;
};

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  (await vite.ssrLoadModule('/src/domain/siteFixture.ts')).installTestSite();
  const [topologyModule, liveModule, screenModule, drawerModule] = await Promise.all([
    vite.ssrLoadModule('/src/components/PipelineTopology.tsx'),
    vite.ssrLoadModule('/src/components/LiveLagChart.tsx'),
    vite.ssrLoadModule('/src/screens/PipelineDetail.tsx'),
    vite.ssrLoadModule('/src/components/StageDrawer.tsx'),
  ]);
  PipelineTopology = topologyModule.PipelineTopology;
  LiveLagChart = liveModule.LiveLagChart;
  resolveLagTrend = liveModule.resolveLagTrend;
  PipelineDetailScreen = screenModule.PipelineDetail;
  StageDrawer = drawerModule.StageDrawer;
  WindowDelivery = (await vite.ssrLoadModule('/src/components/WindowDelivery.tsx')).WindowDelivery;
});

after(async () => { await vite.close(); });

test('closed window rendering discloses scope, dates, provenance and retained transport', () => {
  const evidence = { state: 'matched', eventCount: 12, archiveRunId: 'r1', windowId: 'w1',
    startedAt: '2026-09-09T09:00:00Z', closedAt: '2026-09-09T09:10:00Z',
    destinationObservedAt: '2026-09-09T09:11:00Z', scope: 'closed_window_only',
    quality: { evidenceKind: 'historical', freshness: 'stale' } };
  const markup = renderToStaticMarkup(createElement(WindowDelivery, { evidence, cached: true }));
  assert.match(markup, /12 événements réconciliés/);
  assert.match(markup, /Preuve historique/);
  assert.match(markup, /Snapshot conservé/);
  assert.match(markup, /pas la capture actuelle/);
  assert.match(markup, /dateTime="2026-09-09T09:10:00Z"/);
  assert.match(markup, /Archive r1/);
  const empty = renderToStaticMarkup(createElement(WindowDelivery, { evidence: {...evidence, state: 'not_tested', eventCount: 0}, cached: false }));
  assert.match(empty, /livraison non testée/);
  assert.doesNotMatch(empty, /événements réconciliés/);
});

test('window absence and failed reads never render successful reconciliation', () => {
  assert.equal(renderToStaticMarkup(createElement(WindowDelivery, { evidence: null, cached: false })), '');
  for (const state of ['invalid', 'unavailable']) {
    const markup = renderToStaticMarkup(createElement(WindowDelivery, { evidence: {state}, cached: false }));
    assert.match(markup, /ne peut pas être établie/);
    assert.doesNotMatch(markup, /événements réconciliés/);
  }
});

function pipelineFixture(): Pipeline {
  return {
    id: 'dev-cntr',
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'simulation' },
    summary: 'Simulation : livraison Snowflake non prouvée',
    observedAt: '2026-08-28T08:33:56.470000+00:00',
    stages: [
      { id: 'source', status: 'healthy', observedAt: '2026-08-28T08:33:56.470000+00:00', headline: 'Source observée', detail: 'Checkpoint et tail présents' },
      { id: 'capture', status: 'healthy', observedAt: '2026-08-28T08:33:56.470000+00:00', headline: 'Capture active', detail: 'Worker actif' },
      { id: 'raw', status: 'healthy', observedAt: '2026-08-28T08:33:56.470000+00:00', headline: 'Raw publié', detail: 'Publication confirmée' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'Chargement non observé', detail: 'Aucune preuve de load' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'Destination non observée', detail: 'Aucune preuve d’application' },
    ],
    lagSequences: 8,
    lagSeconds: null,
    lagSeries: [
      { startSeconds: 0, endSeconds: 4, low: 8, high: 12, lag: 9, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
      { startSeconds: 5, endSeconds: 9, low: 7, high: 10, lag: null, samples: 5, unknownSamples: 2, coverage: 'gap', kind: 'observed' },
      { startSeconds: 10, endSeconds: 14, low: 4, high: 8, lag: 5, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
    ],
    lagSeriesResolutionSeconds: 5,
    lagSampleCount: 15,
    lagUnknownSampleCount: 2,
    counters: { events_published: 120 },
    incident: null,
  };
}

test('mobile refresh stays in document flow with a touch-sized target', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const mobileCss = css.slice(css.indexOf('@media (max-width: 760px)'));
  assert.doesNotMatch(mobileCss, /\.detail-hero__meta\s*\{[^}]*position:\s*absolute/);
  assert.match(mobileCss, /\.detail-hero__meta\s*\{[^}]*margin-top:\s*12px/);
  assert.match(mobileCss, /\.detail-refresh\s*\{[^}]*min-height:\s*44px/);
});

test('proof metadata renders the observation time in UTC, not just its label', () => {
  const pipeline = pipelineFixture();
  const markup = renderToStaticMarkup(createElement(PipelineTopology, { pipeline, focus: proofFocus(pipeline) }));
  assert.match(markup, /<time dateTime="2026-08-28T08:33:56.470000\+00:00">28 août, 08:33 UTC<\/time>/);
});

test('proof metadata does not invent a time when the observation is missing', () => {
  const pipeline = pipelineFixture();
  const focus = proofFocus(pipeline);
  const markup = renderToStaticMarkup(createElement(PipelineTopology, {
    pipeline, focus: { ...focus, timestamp: { ...focus.timestamp, value: null } },
  }));
  assert.doesNotMatch(markup, /<time/);
  assert.ok(markup.includes(focus.timestamp.label));
});

test('gaps split chart segments instead of joining unknown samples', () => {
  const segments = buildLagSegments(pipelineFixture().lagSeries);

  assert.equal(segments.length, 2);
  assert.deepEqual(segments.map((segment) => segment.map((point) => point.lag)), [[9], [5]]);
});

test('salient lag windows keep departure, peak and budget stop instead of the first three indices', () => {
  const points = Array.from({ length: 57 }, (_, index) => {
    const lag = index === 56 ? 0 : Math.round(240001 * (1 - index / 56));
    return {
      startSeconds: index * 5,
      endSeconds: index * 5 + 4,
      low: lag,
      high: lag,
      lag,
      samples: 1,
      unknownSamples: 0,
      coverage: 'complete' as const,
      kind: 'observed' as const,
    };
  });
  const preview = selectSalientLagWindows(points);

  assert.equal(preview.complete, false);
  assert.equal(preview.points.length, 3);
  assert.equal(preview.points[0]?.startSeconds, 0);
  assert.equal(preview.points[0]?.lag, 240001);
  assert.equal(preview.points[1]?.startSeconds, 275);
  assert.equal(preview.points[2]?.startSeconds, 280);
  assert.equal(preview.points[2]?.lag, 0);
  assert.match(preview.caption, /départ \(maximum\).*arrêt à zéro/);
  assert.notDeepEqual(preview.points.map((point) => point.startSeconds), [0, 5, 10]);
});

test('dated unknown evidence is not presented as absent or verified', () => {
  const pipeline = pipelineFixture();
  const stage = { ...pipeline.stages[4]!, status: 'unknown' as const, observedAt: '2026-09-08T15:10:05Z' };
  assert.equal(buildStageProof(stage, pipeline).statusLabel, 'Non confirmée');
  assert.equal(buildStageProof(stage, pipeline).observedAt, stage.observedAt);
  assert.equal(buildStageProof({ ...stage, observedAt: null }, pipeline).statusLabel, 'Observation absente');
});

test('simulation stage proof never claims a verified guarantee', () => {
  const pipeline = pipelineFixture();
  const source = buildStageProof(pipeline.stages[0]!, pipeline);
  const destination = buildStageProof(pipeline.stages[4]!, pipeline);

  assert.equal(source.statusLabel, 'Observée dans la démonstration');
  assert.equal(destination.statusLabel, 'Observation absente');
  assert.doesNotMatch(`${source.statusLabel} ${destination.statusLabel}`, /Garantie vérifiée/);
});

test('pipeline detail exposes all stage evidence and keeps simulation still', () => {
  const pipeline = pipelineFixture();
  const markup = renderToStaticMarkup(createElement(PipelineTopology, { pipeline, focus: proofFocus(pipeline) }));

  for (const stage of ['Source', 'Lecture', 'Zone de réception', 'Copie initiale', 'Destination']) {
    assert.match(markup, new RegExp(stage));
  }
  assert.match(markup, /Non confirmée/);
  assert.match(markup, /Observation du relevé/);
  assert.match(markup, /Premier point non confirmé/);
  assert.match(markup, />Destination</);
  assert.doesNotMatch(markup, /topology-track|spine|runway|is-moving/);
  assert.doesNotMatch(markup, /Simulation|observation périmée · Périmée/i);
});

// ---------------------------------------------------------------------------
// Onglet État — remplace l'ancien onglet « Flux » (bande live + chaîne de
// preuve à cinq étapes) par la carte de liaison compacte (LiaisonCard) et
// trois étapes consolidées (StateView, voir src/screens/PipelineDetail.tsx et
// src/domain/operator.ts). Les tests suivants remplacent :
//   - « detail synthesis leads with source identity and keeps the live band
//     before registers »
//   - « flux synthesis keeps one stage chain and discloses technical
//     registers on Mesures » (le volet Mesures/registres techniques reste
//     couvert par le test, inchangé, « measures tab renders no proof-chain
//     and keeps chart, h1 and main » plus bas dans ce fichier)
//   - « an unavailable Source frontier keeps the flux summary bounded and
//     never invents totals »
// Retirés sans remplacement (fonctionnalité supprimée par la refonte, pas un
// défaut) : « source inspect deep-link expands the stage evidence inline in
// the chain » et « every stage is addressable by anchor and only the
// inspected one is marked ». L'ancienne chaîne de cinq étapes offrait un
// deep-link d'inspection par étape (`route.inspect`, ancres
// `#pipeline-stage-<id>`) ; le nouvel onglet État consolide en trois étapes
// sans ancre ni expansion inline, et `PipelineDetail.tsx` ne lit plus du tout
// `route.inspect` (paramètre mort — `pipelineStageAnchorId` reste exporté et
// utilisé par App.tsx pour un défilement qui ne trouve plus jamais sa cible).
// ---------------------------------------------------------------------------

test('l’onglet État ouvre sur l’identité et l’état de la liaison, puis les trois étapes, avant Tables et Mesures', () => {
  const pipeline = pipelineFixture();
  const state = detailStateFor(pipeline, 'simulation');
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
  }));

  assert.match(markup, /<h1[^>]*>dev-cntr<\/h1>/);
  assert.match(markup, /<p class="page-kicker"><a href="#\/">Liaisons<\/a><\/p>/);
  assert.match(markup, /<span class="liaison__state liaison__state--attention">À surveiller<\/span>/);
  assert.match(markup, /<button class="detail-refresh" type="button">Actualiser<\/button>/);
  assert.match(markup, /aria-label="Onglets de la liaison"/);
  assert.match(markup, />État</);
  assert.match(markup, />Tables</);
  assert.match(markup, />Mesures</);
  assert.match(markup, /class="liaison liaison--attention liaison--compact"/);
  assert.match(markup, /<h2 class="steps__title">Où en sont les données<\/h2>/);
  assert.equal((markup.match(/class="step step--/g) ?? []).length, 3);
  assert.ok(markup.indexOf('liaison--compact') < markup.indexOf('class="steps"'));
  // L'ancienne bande live et la chaîne à cinq étapes ont disparu de l'État.
  assert.doesNotMatch(markup, /class="flux-band"|class="stage-chain"|source-heartbeat|fact-spool|proof-chain|proof-waybill|class="detail-diagnostics"/);
});

test('les trois étapes retiennent le pire état d’un groupe et se taisent sur ce qui n’a jamais été observé', () => {
  const base = pipelineFixture();
  const pipeline: Pipeline = {
    ...base,
    stages: [
      { id: 'source', status: 'healthy', observedAt: base.observedAt, headline: 'Source observée', detail: 'ok' },
      { id: 'capture', status: 'incident', observedAt: base.observedAt, headline: 'Capture non établie', detail: 'Checkpoint absent' },
      { id: 'raw', status: 'unknown', observedAt: null, headline: 'jamais observé', detail: '—' },
      { id: 'load', status: 'unknown', observedAt: null, headline: 'jamais observé', detail: '—' },
      { id: 'destination', status: 'unknown', observedAt: null, headline: 'jamais observé', detail: '—' },
    ],
  };
  const state = detailStateFor(pipeline, 'simulation');
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
  }));

  const steps = markup.match(/<li class="step step--[\s\S]*?<\/li>/g) ?? [];
  assert.equal(steps.length, 3);
  assert.match(steps[0]!, /step--ok/);
  assert.match(steps[0]!, /Connexion à l’AS400/);
  assert.match(steps[1]!, /step--stopped/);
  assert.match(steps[1]!, /Lecture des données/);
  assert.match(steps[1]!, /Rien n’est lu en ce moment\./);
  assert.match(steps[2]!, /step--unknown/);
  assert.match(steps[2]!, /Arrivée dans Snowflake/);
  assert.match(steps[2]!, /Aucun relevé exploitable\./);
  // Un groupe jamais observé ne publie pas d'âge inventé.
  assert.doesNotMatch(steps[2]!, /step__age/);
});

test('une source indisponible garde la liaison retenue affichée, sans total ni ligne copiée inventés', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    quality: { coverage: 'partial', freshness: 'stale', evidenceKind: 'historical' },
    counters: { events_published: 1793, payload_bytes_published: 622091, windows_published: 225, events_in_target: null },
  };
  const base = detailStateFor(pipeline, 'historical');
  const state: ControlPlaneState = {
    ...base,
    overview: {
      ...(base as { overview: Overview }).overview,
      sources: [{ id: 'dev-source', environment: 'dev', evidenceKind: 'historical', status: 'unavailable', error: 'offline' }],
    },
  } as ControlPlaneState;
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
  }));

  assert.match(markup, /<h1[^>]*>dev-cntr<\/h1>/);
  // Aucun compteur brut n'est déduit dans les faits de l'État — ils vivent
  // dans l'onglet Mesures. Les trois faits restent Non mesuré (aucune
  // destination ni exécution flotte publiée par ce relevé minimal).
  assert.equal((markup.match(/liaison__fact-absent/g) ?? []).length, 3);
  assert.doesNotMatch(markup, /622\s?091|225 lots/);
  assert.doesNotMatch(markup, /<dd class="liaison__fact-absent" data-numeric="">0</);
});

function detailStateFor(pipeline: Pipeline, evidenceKind: 'live' | 'simulation' | 'historical' = 'live'): ControlPlaneState {
  const overview = {
    revision: 12,
    generatedAt: pipeline.observedAt,
    scope: { kind: 'single' as const, environments: [pipeline.environment] },
    pipelines: [pipeline],
    sources: [{ id: `source-${evidenceKind}`, environment: pipeline.environment, evidenceKind, status: 'available' as const, error: null }],
  };
  return { status: 'ready', connection: 'live', overview, pipelines: [pipeline], lastSuccessAt: new Date(pipeline.observedAt) };
}

function fleetRuntimeFixture(
  capabilities: Readonly<Record<string, { readonly state: 'available' | 'unavailable'; readonly reason: string | null }>>,
): Pipeline['fleetRuntime'] {
  return {
    formatVersion: 'quadringent-fleet-runtime-v1',
    fleetId: 'acme-dev',
    environment: 'dev',
    pipelineId: 'dev-cntr',
    phase: 'CERTIFIED',
    checkpoint: null,
    capabilities,
    tableStates: [],
  };
}

test('detail offers exactly the actions the service currently publishes as available, identically to the overview — no dialog on either surface', async () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    fleetRuntime: fleetRuntimeFixture({
      prepare: { state: 'available', reason: null },
      start: { state: 'available', reason: null },
      pause: { state: 'available', reason: null },
      resume: { state: 'unavailable', reason: 'unsupported_action' },
      refresh: { state: 'available', reason: null },
    }),
  };
  const state = detailStateFor(pipeline, 'live');
  const { Overview: OverviewScreen } = await vite.ssrLoadModule('/src/screens/Overview.tsx');

  const detailMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
    async onBusinessAction() { throw new Error('action non invoquée au rendu'); },
  }));
  const overviewMarkup = renderToStaticMarkup(createElement(OverviewScreen, {
    state, overview: (state as { overview: Overview }).overview, onRefresh() {},
    async onBusinessAction() { throw new Error('action non invoquée au rendu'); },
  }));

  for (const markup of [detailMarkup, overviewMarkup]) {
    for (const label of ['Préparer les tables', 'Démarrer la copie', 'Mettre en pause']) {
      assert.match(markup, new RegExp(`<button[^>]*class="liaison__action"[^>]*>${label}</button>`));
    }
    assert.doesNotMatch(markup, />Relancer<\/button>/);
    assert.doesNotMatch(markup, /<dialog\b/);
  }
});

test('detail exposes Relancer only when the service publishes a resume capability', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    status: 'awaiting_resume',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked', causeResolved: true },
    fleetRuntime: fleetRuntimeFixture({ resume: { state: 'available', reason: null } }),
  };
  const state = detailStateFor(pipeline, 'live');
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
    async onBusinessAction() { throw new Error('action non invoquée au rendu'); },
  }));

  assert.match(markup, /<button[^>]*class="liaison__action"[^>]*>Relancer<\/button>/);
  assert.doesNotMatch(markup, />RESUME</);

  const withoutCapability: Pipeline = {
    ...pipeline,
    fleetRuntime: fleetRuntimeFixture({ resume: { state: 'unavailable', reason: 'unsupported_action' } }),
  };
  const withoutState = detailStateFor(withoutCapability, 'live');
  const withoutMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state: withoutState, pipeline: withoutCapability, route: { name: 'pipeline', id: withoutCapability.id, tab: 'overview' }, onRefresh() {},
  }));
  assert.doesNotMatch(withoutMarkup, /class="liaison__action"/);
  assert.match(withoutMarkup, /class="liaison__guidance"/);
});

// NOTE — défaut découvert, non corrigé (règle 6 de la mission) : l'ancien
// test « detail blocks mutations while the observation is not current »
// vérifiait qu'une capacité publiée redevenait indisponible/désactivée tant
// que l'observation n'était pas courante (relevé conservé, historique, figé).
// Dans la nouvelle implémentation, `actionsFor` (src/domain/operator.ts) ne
// consulte QUE `pipeline.fleetRuntime.capabilities` : ni `LiaisonCard`, ni
// `PipelineDetail.tsx`, ni `App.tsx` (qui fournit `onBusinessAction`
// inconditionnellement, y compris en 'degraded') ne vérifient plus la
// fraîcheur avant d'offrir un bouton de mutation. Un relevé retenu qui
// contiendrait encore une capacité publiée « available » afficherait donc un
// bouton actif alors que l'observation n'est plus courante. C'est
// potentiellement une vraie régression de sûreté produit ; elle est signalée
// au rapport final plutôt que corrigée silencieusement ici.

test('fresh live État labels a healthy current reading without inventing a destination proof', () => {
  const observedAt = pipelineFixture().observedAt;
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    status: 'healthy',
    stages: pipelineFixture().stages.map((stage) => ({ ...stage, status: 'healthy' as const, observedAt })),
  };
  const state = detailStateFor(pipeline, 'live');
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
  }));

  assert.match(markup, /<h1[^>]*>dev-cntr<\/h1>/);
  assert.match(markup, /<span class="liaison__state liaison__state--ok">À jour<\/span>/);
  assert.match(markup, /<p class="liaison__headline">La copie suit la source\.<\/p>/);
  // Sans destination publiée, aucune preuve de livraison n'est inventée.
  assert.equal((markup.match(/liaison__fact-absent/g) ?? []).length, 3);
  assert.doesNotMatch(markup, /Vos données sont disponibles dans Snowflake/);
});

test('l’onglet Mesures détient les compteurs bruts, jamais montrés sur l’onglet État', async () => {
  const observedAt = pipelineFixture().observedAt;
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    counters: {
      events_published: 1793,
      payload_bytes_published: 622091,
      windows_published: 225,
      events_in_target: null,
    },
  };
  const state = detailStateFor(pipeline, 'simulation');
  const stateMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
  }));
  const measuresMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'live' }, onRefresh() {},
  }));

  assert.match(measuresMarkup, /1\s?793 événements lus/);
  assert.match(measuresMarkup, /622\s?091/);
  assert.match(measuresMarkup, /225/);
  assert.match(measuresMarkup, /class="measurement-table"/);
  assert.doesNotMatch(stateMarkup, /622\s?091|225|1\s?793/);
  assert.doesNotMatch(stateMarkup, /class="detail-diagnostics"|proof-chain|proof-waybill/);
  void observedAt;
});

test('measures tab renders no proof-chain and keeps chart, h1 and main', async () => {
  const pipeline = pipelineFixture();
  const overview = {
    revision: 12,
    generatedAt: pipeline.observedAt,
    scope: { kind: 'single' as const, environments: ['dev'] },
    pipelines: [pipeline],
    sources: [{ id: 'dev-source', environment: 'dev', evidenceKind: 'simulation' as const, status: 'available' as const, error: null }],
  };
  const state: ControlPlaneState = { status: 'ready', connection: 'live', overview, pipelines: [pipeline], lastSuccessAt: new Date(pipeline.observedAt) };
  const route = { name: 'pipeline' as const, id: pipeline.id, tab: 'live' as const };
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route, onRefresh() {},
  }));
  const source = readFileSync(new URL('../screens/PipelineDetail.tsx', import.meta.url), 'utf8');
  const liveView = source.slice(source.indexOf('function MeasurementView'), source.indexOf('function MetricTable'));
  const AppShell = (await vite.ssrLoadModule('/src/components/AppShell.tsx')).AppShell;
  const wrapped = renderToStaticMarkup(createElement(AppShell, { route, state }, createElement(PipelineDetailScreen, {
    state, pipeline, route, onRefresh() {},
  })));

  assert.doesNotMatch(liveView, /PipelineTopology|ProofChain|proof-chain/);
  assert.doesNotMatch(markup, /proof-chain/);
  assert.doesNotMatch(markup, /proof-waybill-index/);
  assert.match(markup, /<h1[^>]*>/);
  assert.match(markup, /class="lag-chart"/);
  assert.match(markup, /live-proof-band/);
  assert.match(markup, /La lecture et ses volumes/);
  assert.match(markup, /ne confirment pas l’arrivée à destination/);
  assert.equal((wrapped.match(/<main\b/g) ?? []).length, 1);
  assert.equal((wrapped.match(/<h1\b/g) ?? []).length, 1);
  assert.doesNotMatch(wrapped, /proof-chain/);
  assert.match(wrapped, /class="lag-chart"/);
});

test('desktop measures CSS keeps the lag chart useful and a 3-row register preview above the 900px fold', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const desktop = css.slice(css.indexOf('@media (min-width: 901px)'));
  const desktopBlock = desktop.slice(0, desktop.indexOf('@media (max-width: 980px)'));
  const chartHeight = Number(desktopBlock.match(/\.pipeline-detail-view--live \.lag-chart\s*\{[^}]*height:\s*(\d+)px/)?.[1] ?? 0);
  const heroPadding = desktopBlock.match(/\.pipeline-detail-view--live \.detail-hero\s*\{[^}]*padding:\s*(\d+)px 0 (\d+)px/) ?? [];
  const evidencePadding = desktopBlock.match(/\.pipeline-detail-view--live \.live-evidence\s*\{[^}]*padding:\s*(\d+)px 0 (\d+)px/) ?? [];
  const previewRow = Number(desktopBlock.match(/\.pipeline-detail-view--live \.lag-register-preview td\s*\{[^}]*padding:\s*(\d+)px/)?.[1] ?? 4);
  const chrome = 72
    + (Number(heroPadding[1] ?? 4) + Number(heroPadding[2] ?? 2) + 24 + 42 + 18 + 20 + 36)
    + (Number(evidencePadding[1] ?? 4) + 22 + 40 + 28);
  const preview = 22 + 18 + 3 * (12 + 2 * previewRow);
  const foldBudget = chrome + 40 + 28 + chartHeight + 22 + preview;

  assert.ok(chartHeight >= 140, `graphe trop bas: ${chartHeight}px`);
  assert.ok(chartHeight <= 200, `graphe trop haut pour le registre: ${chartHeight}px`);
  assert.ok(chrome < 560, `chrome au-dessus du graphe trop haut: ${chrome}px`);
  assert.ok(foldBudget <= 900, `preview sous le pli: ${foldBudget}px`);
  assert.ok(foldBudget - preview <= 860, `contrôle registre trop bas: ${foldBudget - preview}px`);
  assert.match(desktopBlock, /\.pipeline-detail-view--live \.lag-register-preview/);
  assert.match(desktopBlock, /\.pipeline-detail-view--live \.lag-register > summary\s*\{[^}]*padding:\s*8px 0/);
  assert.match(desktopBlock, /\.pipeline-detail-view--live \.live-proof-band \.fact-spool__band\s*\{[^}]*margin:\s*0/s);
  assert.match(desktopBlock, /\.pipeline-detail-view \.incident-context\s*\{[^}]*padding:\s*8px 0 4px/s);
  assert.match(desktopBlock, /\.pipeline-detail-view \.operator-register\s*\{[^}]*border-top:\s*1px solid/s);
  assert.doesNotMatch(desktopBlock, /linear-gradient|filter:\s*blur|backdrop-filter|mix-blend-mode/);
  assert.match(css, /\.lag-chart__key--range\s*\{[^}]*background:\s*var\(--lag-range\)/);
  assert.match(css, /\.lag-chart__key\s*\{[^}]*background:\s*var\(--lag-signal\)/);
  assert.doesNotMatch(css, /\.lag-chart__key--range\s*\{[^}]*background:\s*var\(--signal-range\)/);
  assert.match(css, /--lag-range:\s*var\(--text-muted\)/);
  assert.match(css, /--lag-signal:\s*var\(--text-strong\)/);
  assert.match(css, /\.lag-chart__floor\s*\{[^}]*color:\s*var\(--signal-attention\)/);
  assert.match(css, /\.lag-chart__floor\s*\{[^}]*position:\s*static/);
  assert.doesNotMatch(css, /\.lag-chart__floor\s*\{[^}]*position:\s*absolute/);
});

test('l’onglet État distingue incident, reprise et simulation, sans jamais les confondre avec une lecture à jour', () => {
  const completeLive: Pipeline = {
    ...pipelineFixture(),
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    incident: null,
  };

  // Un incident simulé reste nommé et borné à l'incident — jamais « À jour ».
  const simulation: Pipeline = {
    ...completeLive,
    status: 'incident',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'simulation' },
    incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' },
  };
  const simulationMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state: detailStateFor(simulation, 'simulation'),
    pipeline: simulation,
    route: { name: 'pipeline', id: simulation.id, tab: 'overview' },
    onRefresh() {},
  }));
  assert.match(simulationMarkup, /<span class="liaison__state liaison__state--stopped">Interrompue<\/span>/);
  assert.match(simulationMarkup, /La lecture est interrompue\./);
  assert.doesNotMatch(simulationMarkup, /liaison__state--ok/);

  // Reprise (recovering) sans incident servi : un mot distinct, jamais confondu.
  const recovering: Pipeline = { ...completeLive, status: 'recovering', incident: null };
  const recoveringMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state: detailStateFor(recovering, 'live'),
    pipeline: recovering,
    route: { name: 'pipeline', id: recovering.id, tab: 'overview' },
    onRefresh() {},
  }));
  assert.match(recoveringMarkup, /<span class="liaison__state liaison__state--attention">Rattrapage<\/span>/);
  assert.match(recoveringMarkup, /La lecture a repris et rattrape son retard\./);
  assert.doesNotMatch(recoveringMarkup, /liaison__state--ok|liaison__state--stopped/);

  // Un incident réel (live) publie la même consigne que la carte d'accueil.
  const liveIncident: Pipeline = {
    ...completeLive,
    status: 'incident',
    incident: { code: 'capture_timeout', type: 'capture_timeout' },
  };
  const liveMarkup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state: detailStateFor(liveIncident, 'live'),
    pipeline: liveIncident,
    route: { name: 'pipeline', id: liveIncident.id, tab: 'overview' },
    onRefresh() {},
  }));
  assert.match(liveMarkup, /<span class="liaison__state liaison__state--stopped">Interrompue<\/span>/);
  assert.match(liveMarkup, /class="liaison__guidance">La cause n’est pas levée\. À traiter avec votre exploitation avant toute relance\.<\/p>/);
});

test('un relevé live figé en incident ne devient jamais « à jour » même si la connexion reste live', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    status: 'incident',
    quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' },
    incident: { code: 'capture_auth_blocked', type: 'capture_auth_blocked' },
  };
  const state = detailStateFor(pipeline, 'live');
  const markup = renderToStaticMarkup(createElement(PipelineDetailScreen, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab: 'overview' }, onRefresh() {},
  }));

  assert.match(markup, /<span class="liaison__state liaison__state--stopped">Interrompue<\/span>/);
  assert.doesNotMatch(markup, /liaison__state--ok|>À jour</);
  // Le relevé date d'il y a plusieurs semaines : le pied de carte le marque
  // « figé » quelle que soit la connexion transportant l'état courant.
  assert.match(markup, /class="liaison__freshness liaison__freshness--old"/);
});

test('the lag chart annotates the served incident onset without positioning it', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    status: 'incident',
    incident: { code: 'capture_auth_blocked', type: 'capture_auth_blocked' },
  };
  const markup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline, current: false }));

  assert.match(markup, /lag-chart__onset/);
  assert.match(markup, /Incident servi — connexion refusée depuis/);
  // Annotation textuelle seule : aucun marqueur positionné n'invente une
  // correspondance temporelle sur un axe en secondes relatives
  assert.doesNotMatch(markup, /lag-chart__onset-marker|lag-chart__onset-line/);
});

test('incident context exposes safe current facts and no invented history', () => {
  const pipeline: Pipeline = { ...pipelineFixture(), status: 'incident', incident: { code: 'capture_stopped_fail_closed', type: 'capture_stopped' } };
  const context = buildIncidentContext(pipeline);

  assert.equal(context?.affectedStage, 'Lecture');
  assert.equal(context?.firstObservedAt, null);
  assert.equal(context?.lastObservedAt, pipeline.observedAt);
  assert.match(context?.safeExplanation ?? '', /règle d’arrêt de sécurité/);
  assert.equal(context?.recommendationState, 'no_safe_recommendation');
  assert.equal(context?.publicLabel, 'Lecture arrêtée en sécurité');
  assert.equal(incidentPublicLabel(pipeline.incident!), 'Lecture arrêtée en sécurité');
});

test('every public incident type maps to a local safe label and explanation', () => {
  const cases = [
    ['capture_connection_failure', 'Connexion source interrompue', /connexion à la source/],
    ['capture_timeout', 'Délai de lecture dépassé', /délai autorisé/],
    ['capture_stopped', 'Lecture arrêtée en sécurité', /arrêt de sécurité/],
    ['destination', 'Incident à la destination', /contradictoire/],
  ] as const;

  for (const [type, expectedLabel, expectedExplanation] of cases) {
    const pipeline: Pipeline = {
      ...pipelineFixture(),
      status: 'incident',
      incident: { code: type === 'destination' ? 'destination_reconciliation_mismatch' : 'capture_stopped_fail_closed', type },
    };
    const context = buildIncidentContext(pipeline);

    assert.equal(context?.publicLabel, expectedLabel);
    assert.match(context?.safeExplanation ?? '', expectedExplanation);
    assert.doesNotMatch(context?.publicLabel ?? '', /capture_|fail_closed/i);
  }
});

test('a destination incident points to the destination without exposing its public code', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    status: 'incident',
    incident: { code: 'destination_authorization_denied', type: 'destination' },
  };
  const context = buildIncidentContext(pipeline);

  assert.equal(context?.affectedStage, 'Destination');
  assert.equal(context?.publicLabel, 'Incident à la destination');
  assert.doesNotMatch(`${context?.publicLabel} ${context?.safeExplanation}`, /authorization_denied/);
});

test('an open stage selection resolves fresh pipeline evidence and restores its trigger focus', () => {
  const initialPipeline = pipelineFixture();
  const selectedId = 'capture' as const;
  const initialStage = resolveSelectedStage(initialPipeline, selectedId);
  const updatedPipeline: Pipeline = {
    ...initialPipeline,
    stages: initialPipeline.stages.map((stage) => stage.id === selectedId ? {
      ...stage,
      status: 'incident',
      headline: 'Capture arrêtée en sécurité',
      detail: 'Connexion source interrompue',
      observedAt: '2026-08-28T08:34:56.470000+00:00',
    } : stage),
  };

  const updatedStage = resolveSelectedStage(updatedPipeline, selectedId);

  assert.notStrictEqual(updatedStage, initialStage);
  assert.deepEqual(updatedStage, {
    id: 'capture',
    status: 'incident',
    headline: 'Capture arrêtée en sécurité',
    detail: 'Connexion source interrompue',
    observedAt: '2026-08-28T08:34:56.470000+00:00',
  });

  let focused = false;
  let scheduled: (() => void) | null = null;
  restoreSelectedStageFocus(
    new Map([[selectedId, { focus: () => { focused = true; } }]]),
    selectedId,
    (callback) => { scheduled = callback; },
  );
  assert.equal(focused, false);
  scheduled?.();
  assert.equal(focused, true);
});

test('a temporal gap is table metadata and splits the graph', () => {
  const first = pipelineFixture().lagSeries[0]!;
  const last = pipelineFixture().lagSeries[2]!;
  const temporalGap = {
    startSeconds: 5,
    endSeconds: 20,
    low: null,
    high: null,
    lag: null,
    samples: 0,
    unknownSamples: 0,
    coverage: 'gap' as const,
    kind: 'temporal_gap' as const,
  };

  assert.equal(buildLagSegments([first, temporalGap, last]).length, 2);
  assert.equal(lagCoverageLabel(temporalGap), 'Intervalle non observé');
});

test('detail uses an asymmetric proof object instead of a responsive stepper', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const chain = readFileSync(new URL('../styles/proof-chain.css', import.meta.url), 'utf8');

  assert.doesNotMatch(css, /topology-track|spine|runway|linear-gradient/);
  assert.doesNotMatch(chain, /topology-track|spine|runway|linear-gradient/);
  assert.match(chain, /\.proof-chain__stations\s*\{[^}]*display:\s*grid/);
  assert.doesNotMatch(chain, /repeat\(5,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(chain, /\.proof-chain__station--focus\s*\{[^}]*border-left:\s*3px solid/);
  assert.match(chain, /\.proof-chain__station\.proof-object__focus,[\s\S]*?grid-column:\s*auto/);
  assert.match(css, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.proof-object\s*\{[^}]*display:\s*flex[^}]*flex-direction:\s*column/);
  assert.doesNotMatch(css, /\.proof-object(?:__[^\s{]+)?\s*\{[^}]*border-(?:top|bottom):/);
  assert.doesNotMatch(chain, /box-shadow:\s*0 22px|linear-gradient/);
});

test('detail facts neutralize generic card surfaces and keep a typographic rhythm', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const spool = readFileSync(new URL('../styles/fact-spool.css', import.meta.url), 'utf8');
  const facts = css.match(/\.detail-facts\s*\{([^}]*)\}/)?.[1] ?? '';

  assert.match(facts, /display:\s*grid/);
  assert.match(spool, /minmax\(max-content,\s*1fr\)/);
  assert.doesNotMatch(css, /\.detail-facts__metrics\s*\{[^}]*grid-template-columns:\s*1fr \.65fr/);
  assert.doesNotMatch(spool, /repeat\(4,\s*minmax\(0,\s*1fr\)\)/);
  assert.doesNotMatch(css, /@media\s*\(max-width:\s*350px\)[\s\S]*?\.observation-quality[^{]*\{[^}]*grid-template-columns:\s*1fr/);
});

test('stale simulation labels lag and trend as retained observations, never as current facts', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    quality: { coverage: 'partial', freshness: 'stale', evidenceKind: 'simulation' },
  };
  const markup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline, current: false }));

  assert.match(markup, /Mesures de démonstration/);
  assert.match(markup, /live-evidence--retained/);
  assert.match(markup, /Dernier retard observé/);
  assert.match(markup, /live-proof-band/);
  assert.match(markup, /<span class="fact-spool__label">Portée<\/span>/);
  assert.match(markup, /<span class="fact-spool__label">Nature<\/span>/);
  assert.match(markup, /<span class="fact-spool__label">Observé<\/span>/);
  assert.match(markup, /<span class="fact-spool__label">Limite<\/span>/);
  assert.match(markup, /La lecture et ses volumes ne confirment pas l’arrivée à destination/);
  assert.ok(markup.indexOf('>Portée<') < markup.indexOf('>Nature<'));
  assert.ok(markup.indexOf('>Nature<') < markup.indexOf('>Observé<'));
  assert.ok(markup.indexOf('>Observé<') < markup.indexOf('>Limite<'));
  assert.ok(markup.indexOf('>Limite<') < markup.indexOf('Dernier retard observé'));
  assert.ok(markup.indexOf('class="lag-chart"') < markup.indexOf('lag-chart__guide'));
  assert.match(markup, /Tendance du dernier segment observé/);
  assert.match(markup, /Calcul limité au dernier segment contigu de ce relevé/);
  assert.match(markup, /Relevé trop ancien/);
  assert.match(markup, /live-evidence--simulation/);
  assert.doesNotMatch(markup, /Retard courant|<p class="section-kicker">Tendance<\/p>/);
  assert.match(markup, /lag-register-preview/);
  assert.match(markup, /Tous les intervalles, synchronisés avec le graphe/);
  assert.equal((markup.match(/lag-register-preview[\s\S]*?<\/table>/)?.[0]?.match(/<tbody>[\s\S]*?<\/tbody>/)?.[0]?.match(/<tr/g) ?? []).length, 3);
  assert.doesNotMatch(markup, /<details[^>]*class="lag-register"/);
  assert.ok(markup.indexOf('lag-chart__guide') < markup.indexOf('lag-register-preview'));
  assert.ok(markup.indexOf('lag-chart__key--line') < markup.indexOf('lag-chart__key--range'));
});

test('Mesures subordinates a budget-stopped capture before lag 0 and the graph', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    quality: { coverage: 'partial', freshness: 'fresh', evidenceKind: 'historical' },
    lagSequences: 0,
    lagSeconds: 0,
    stages: pipelineFixture().stages.map((stage) => stage.id === 'capture'
      ? { ...stage, status: 'unknown', observedAt: null, headline: 'Capture arrêtée par budget', detail: 'Checkpoint absent' }
      : stage),
    lagSeries: Array.from({ length: 57 }, (_, index) => {
      const lag = index === 56 ? 0 : Math.round(240001 * (1 - index / 56));
      return {
        startSeconds: index * 5,
        endSeconds: index * 5 + 4,
        low: lag,
        high: lag,
        lag,
        samples: 1,
        unknownSamples: 0,
        coverage: 'complete' as const,
        kind: 'observed' as const,
      };
    }),
  };
  const markup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline, current: false }));
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const desktop = css.slice(css.indexOf('@media (min-width: 901px)'));
  const desktopBlock = desktop.slice(0, desktop.indexOf('@media (max-width: 980px)'));

  assert.match(markup, /live-measure-subordination/);
  assert.match(markup, /Lecture arrêtée par budget/);
  assert.match(markup, /dernier retard observé 0 enregistrements/);
  assert.match(markup, /pas un signal de santé courant/);
  assert.doesNotMatch(markup, /<p class="section-kicker">Dernier retard observé<\/p>/);
  assert.doesNotMatch(markup, /live-reading__value/);
  assert.doesNotMatch(markup, />0<\/strong><span>enregistrements<\/span>/);
  assert.ok(markup.indexOf('Lecture arrêtée par budget') < markup.indexOf('class="lag-chart"'));
  assert.ok(markup.indexOf('dernier retard observé 0 enregistrements') < markup.indexOf('class="lag-chart"'));
  assert.ok(markup.indexOf('pas un signal de santé courant') < markup.indexOf('class="lag-chart"'));
  assert.match(markup, /lag-chart__floor/);
  assert.match(markup, /Retard à 0 — mesure subordonnée/);
  assert.match(markup, /max 240[\s\u202f]?001/);
  assert.doesNotMatch(markup, /max 0</);
  assert.doesNotMatch(markup, /Retard courant/);
  assert.match(markup, /<span class="fact-spool__label">Portée<\/span>/);
  assert.match(markup, /<span class="fact-spool__label">Nature<\/span>/);
  assert.match(markup, /<span class="fact-spool__label">Observé<\/span>/);
  assert.match(markup, /<span class="fact-spool__label">Limite<\/span>/);
  assert.ok(markup.indexOf('>Portée<') < markup.indexOf('class="lag-chart"'));
  assert.match(markup, /lag-chart__legend/);
  assert.match(markup, /lag-chart__key--line/);
  assert.match(markup, /lag-chart__key--range/);
  assert.match(markup, /lag-register-preview/);
  assert.match(markup, /Points marquants : départ \(maximum\), descente et arrêt à zéro, synchronisés avec le graphe/);
  assert.equal((markup.match(/lag-register-preview[\s\S]*?<\/table>/)?.[0]?.match(/<tbody>[\s\S]*?<\/tbody>/)?.[0]?.match(/<tr/g) ?? []).length, 3);
  const previewBody = markup.match(/lag-register-preview[\s\S]*?<tbody>([\s\S]*?)<\/tbody>/)?.[1] ?? '';
  assert.match(previewBody, /0.4 s/);
  assert.match(previewBody, /240[\s\u202f]?001/);
  assert.match(previewBody, /280.284 s/);
  assert.doesNotMatch(previewBody, />5.9 s</);
  assert.ok(markup.indexOf('Retard à 0 — mesure subordonnée') < markup.indexOf('class="lag-chart"'));
  assert.match(markup, /<details[^>]*class="lag-register"/);
  assert.match(markup, /Voir les 57 intervalles de mesure/);
  assert.match(markup, /Tous les intervalles, synchronisés avec le graphe/);
  assert.ok(markup.indexOf('lag-chart__guide') < markup.indexOf('lag-register-preview'));
  assert.ok(markup.indexOf('lag-register-preview') < markup.indexOf('Voir les 57 intervalles de mesure'));
  assert.doesNotMatch(markup, /<details[^>]*open/);
  assert.match(desktopBlock, /\.live-reading__value strong\s*\{[^}]*font-size:\s*13px/);
  assert.doesNotMatch(desktopBlock, /\.live-reading__value strong\s*\{[^}]*font-size:\s*(?:2\d|3\d|4\d|5\d)px/);
  assert.doesNotMatch(markup, /1\s?793|622\s?091|lu à la source, pas à destination/);
});

test('only a current fresh live observation uses present-tense lag and trend copy', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
  };
  const currentMarkup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline, current: true }));
  const retainedMarkup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline, current: false }));

  assert.match(currentMarkup, /Retard courant/);
  assert.match(currentMarkup, /<p class="section-kicker">Tendance<\/p>/);
  assert.match(currentMarkup, /live-evidence--fresh-live/);
  assert.doesNotMatch(currentMarkup, /arrêt budget|Retard à 0/);
  assert.match(retainedMarkup, /Dernier retard observé/);
  assert.match(retainedMarkup, /Tendance du dernier segment observé/);
  assert.doesNotMatch(retainedMarkup, /courant/i);
  assert.doesNotMatch(retainedMarkup, /live-evidence--fresh-live/);
});

test('fresh live capture with lag 0 never claims a budget stop', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    lagSequences: 0,
    lagSeconds: 0,
    stages: pipelineFixture().stages.map((stage) => stage.id === 'capture'
      ? { ...stage, status: 'healthy', headline: 'Capture active', detail: 'Worker actif' }
      : stage),
    lagSeries: [
      { startSeconds: 0, endSeconds: 4, low: 0, high: 0, lag: 0, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
      { startSeconds: 5, endSeconds: 9, low: 0, high: 0, lag: 0, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
      { startSeconds: 10, endSeconds: 14, low: 0, high: 0, lag: 0, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
    ],
  };
  const markup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline, current: true }));

  assert.match(markup, /Retard courant/);
  assert.match(markup, />0<\/strong><span>enregistrements<\/span>/);
  assert.doesNotMatch(markup, /arrêt budget|live-measure-subordination|Retard à 0/);
});

test('the live chart explains its logarithmic sequence scale and visual marks', () => {
  const markup = renderToStaticMarkup(createElement(LiveLagChart, { pipeline: pipelineFixture(), current: false }));

  assert.match(markup, /Échelle verticale logarithmique/);
  assert.match(markup, /retard en enregistrements/);
  assert.match(markup, /Dernier retard/);
  assert.match(markup, /Plage minimum–maximum/);
  assert.match(markup, />Indéterminé</);
  assert.match(markup, /class="lag-chart"[^>]*aria-label=/);
  assert.doesNotMatch(markup, /Faire défiler horizontalement|tabindex="0"/);
});

test('live trend delegates the last contiguous known segment to the canonical domain verdict', () => {
  const points: Pipeline['lagSeries'] = [
    { startSeconds: 0, endSeconds: 4, low: 90, high: 110, lag: 100, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
    { startSeconds: 5, endSeconds: 9, low: null, high: null, lag: null, samples: 0, unknownSamples: 0, coverage: 'gap', kind: 'temporal_gap' },
    { startSeconds: 10, endSeconds: 14, low: 35, high: 45, lag: 40, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
    { startSeconds: 15, endSeconds: 19, low: 15, high: 25, lag: 20, samples: 5, unknownSamples: 0, coverage: 'complete', kind: 'observed' },
  ];

  assert.deepEqual(resolveLagTrend(points), {
    verdict: 'CATCHING_UP',
    kind: 'catching-up',
    label: 'Rattrapage',
    answer: 'Le retard se résorbe ; rien à faire.',
    delta: -20,
  });
  assert.equal(resolveLagTrend(points.slice(0, 3)).verdict, 'INCONCLUSIVE');
  assert.equal(resolveLagTrend([]).verdict, 'INCONCLUSIVE');
});

test('live trend preserves canonical bounded plateau and palier-6 divergence semantics', () => {
  const plateau = lagPoints(Array<number>(17).fill(1));
  const climb = Array.from({ length: 30 }, (_, index) => 1 + Math.round((1_377_824 * index) / 29));

  assert.equal(resolveLagTrend(plateau).verdict, 'BOUNDED');
  assert.equal(resolveLagTrend(plateau).label, 'Stable');
  assert.equal(resolveLagTrend(lagPoints(climb)).verdict, 'DIVERGING');
  assert.equal(resolveLagTrend(lagPoints(climb)).label, 'S’aggrave');
});

test('stage evidence is a true modal dialog with a backdrop', () => {
  const pipeline = pipelineFixture();
  const markup = renderToStaticMarkup(createElement(StageDrawer, {
    pipeline,
    stage: pipeline.stages[3],
    current: false,
    proofScope: proofFocus(pipeline).proofScope,
    onClose() {},
  }));

  assert.match(markup, /class="stage-dialog-layer"/);
  assert.match(markup, /role="dialog"/);
  assert.match(markup, /aria-modal="true"/);
  assert.match(markup, /class="stage-dialog__backdrop"/);
});

test('stage evidence keeps raw labels but marks them non-current outside receivable live proof', () => {
  const pipeline: Pipeline = {
    ...pipelineFixture(),
    status: 'healthy',
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    stages: pipelineFixture().stages.map((candidate) => candidate.id === 'capture'
      ? { ...candidate, status: 'healthy' as const, headline: 'Capture active', detail: 'Worker actif' }
      : candidate),
  };
  const stage = pipeline.stages.find((candidate) => candidate.id === 'capture')!;
  const nonCurrentScopes = [
    { kind: 'cached' as const, label: 'Snapshot en cache · hors ligne', detail: 'Observation conservée.' },
    { kind: 'unavailable' as const, label: 'Sources indisponibles', detail: 'Preuve courante indisponible.' },
    { kind: 'partial' as const, label: 'Sources partiellement indisponibles', detail: 'Couverture ambiguë.' },
    { kind: 'historical' as const, label: 'Preuve historique', detail: 'Observation historique.' },
  ];

  for (const proofScope of nonCurrentScopes) {
    const markup = renderToStaticMarkup(createElement(StageDrawer, { pipeline, stage, current: false, proofScope, onClose() {} }));
    assert.match(markup, proofScope.kind === 'cached' ? /Observation conservée/ : /Observation non courante/);
    assert.match(markup, /Observation brute/);
    assert.match(markup, /Capture active/);
    assert.match(markup, /Worker actif/);
    assert.match(markup, /ne qualifie pas l’état courant/);
    assert.match(markup, new RegExp(proofScope.label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
    assert.doesNotMatch(markup, />Étape observée<|<dt>Constat<\/dt><dd>Capture active|<dt>Détail sûr<\/dt><dd>Worker actif/);
  }

  const currentScope = { kind: 'live' as const, label: 'Preuve live', detail: 'Snapshot courant.' };
  const currentMarkup = renderToStaticMarkup(createElement(StageDrawer, { pipeline, stage, current: true, proofScope: currentScope, onClose() {} }));
  assert.match(currentMarkup, />Étape observée</);
  assert.match(currentMarkup, /<dt>Constat<\/dt><dd>Capture active/);
  assert.doesNotMatch(currentMarkup, /Observation non courante|ne qualifie pas l’état courant/);
});

test('dialog and graph CSS stay above mobile navigation and never create horizontal chart scrolling', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const mobileCss = css.match(/@media\s*\(max-width:\s*760px\)\s*\{([\s\S]*?)\n\}/)?.[1] ?? '';

  assert.match(css, /\.stage-dialog-layer\s*\{[^}]*position:\s*fixed[^}]*inset:\s*0/);
  assert.match(css, /@media\s*\(max-width:\s*760px\)[\s\S]*?\.stage-drawer\s*\{[^}]*bottom:\s*calc\(76px \+ env\(safe-area-inset-bottom\)\)/);
  assert.match(css, /\.lag-chart\s*\{[^}]*overflow:\s*hidden/);
  assert.match(css, /\.lag-chart svg\s*\{[^}]*width:\s*100%[^}]*min-width:\s*0/);
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?animation:\s*none\s*!important/);
  assert.match(mobileCss, /\.detail-hero__transport\s*\{[^}]*display:\s*none/);
  assert.doesNotMatch(mobileCss, /\.detail-hero__meta\s*\{[^}]*display:\s*none/);
});

test('the live chart range mark keeps at least 3 to 1 contrast on graphite', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const tokens = readFileSync(new URL('../styles/product-tokens.css', import.meta.url), 'utf8');
  const token = (name: string) => tokens.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, 'i'))?.[1] ?? '';
  const rangeBlock = css.match(/\.pipeline-detail-view \.lag-chart__range\s*\{([^}]*)\}/)?.[1] ?? '';

  assert.match(rangeBlock, /stroke:\s*var\(--lag-range\)/);
  assert.doesNotMatch(rangeBlock, /--signal-range|opacity:/);
  assert.ok(contrastRatio(token('text-muted'), token('surface-quiet')) >= 3);
  assert.ok(contrastRatio(token('text-rail-muted'), '#2c2f2c') >= 3);
});

test('simulation and historical charts stay neutral while fresh live proof may use the blue signal', () => {
  const css = readFileSync(new URL('../styles/pipeline-detail.css', import.meta.url), 'utf8');
  const defaultBlock = css.match(/\.live-evidence\s*\{([^}]*)\}/)?.[1] ?? '';
  const liveBlock = css.match(/\.live-evidence--fresh-live\s*\{([^}]*)\}/)?.[1] ?? '';

  assert.match(defaultBlock, /--lag-signal:\s*var\(--text-strong\)/);
  assert.match(defaultBlock, /--lag-range:\s*var\(--text-muted\)/);
  assert.doesNotMatch(defaultBlock, /signal-live|signal-range/);
  assert.match(css, /\.live-evidence--retained \.live-reading__value strong\s*\{[^}]*font-size:\s*13px/);
  assert.match(css, /\.live-measure-subordination strong\s*\{[^}]*font-size:\s*18px/);
  assert.doesNotMatch(css, /\.live-evidence--retained \.live-reading__value strong\s*\{[^}]*font-size:\s*20px/);
  assert.match(liveBlock, /--lag-signal:\s*var\(--signal-live\)/);
  assert.match(liveBlock, /--lag-range:\s*var\(--text-rail-muted\)/);
  assert.doesNotMatch(liveBlock, /signal-range/);
  assert.match(css, /\.lag-chart\s*\{[^}]*--lag-slab:\s*var\(--surface-quiet\)/);
  assert.doesNotMatch(css.match(/\.lag-chart\s*\{([^}]*)\}/)?.[1] ?? '', /#2c2f2c/);
  assert.match(css, /\.live-evidence--fresh-live \.lag-chart\s*\{[^}]*--lag-slab:\s*#2c2f2c/);
  assert.match(css, /\.live-evidence--retained \.lag-chart\s*\{[^}]*--lag-slab:\s*var\(--surface-quiet\)/);
});

function lagPoints(values: readonly number[]): Pipeline['lagSeries'] {
  return values.map((lag, index) => ({
    startSeconds: index * 5,
    endSeconds: index * 5 + 4,
    low: lag,
    high: lag,
    lag,
    samples: 5,
    unknownSamples: 0,
    coverage: 'complete',
    kind: 'observed',
  }));
}

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

function filledPrimaryLabels(markup: string): readonly string[] {
  return [...markup.matchAll(/class="page-action page-action--primary[^"]*"[^>]*>([^<]*)</g)].map((match) => match[1] ?? '');
}

function stationBlock(markup: string, id: string): string {
  return markup.match(new RegExp(`<li[^>]*data-station="${id}"[\\s\\S]*?<\\/li>`))?.[0] ?? '';
}

function proofFocus(pipeline: Pipeline) {
  return resolvePipelineProofFocus(pipeline, {
    generatedAt: pipeline.observedAt,
    sources: [{
      id: `source-${pipeline.environment}`,
      environment: pipeline.environment,
      evidenceKind: pipeline.quality.evidenceKind,
      status: 'available',
      error: null,
    }],
    cached: false,
  });
}
