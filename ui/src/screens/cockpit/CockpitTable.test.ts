import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { CockpitTableData, CockpitTableState } from '../../data/useCockpit.ts';

let vite: ViteDevServer;
let CockpitTableView: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  CockpitTableView = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitTable.tsx')).CockpitTableView;
});

after(async () => { await vite.close(); });

function tableData(overrides: Partial<CockpitTableData> = {}): CockpitTableData {
  return {
    pipeline: { id: 'pl_ventes_lignes', declaredState: 'attention' },
    metrics: { window: '1h', points: [{ at: '2026-09-23T09:00:00Z', lagSeconds: 12, throughputRowsPerSecond: 3 }] },
    logs: [
      { at: '2026-09-23T09:00:00Z', level: 'info', message: 'Fenêtre confirmée.', incidentId: null },
      { at: '2026-09-23T09:10:00Z', level: 'error', message: 'Connexion refusée.', incidentId: 'inc_1' },
    ],
    costToday: { scope: 'table', id: 'pl_ventes_lignes', window: '24h', status: 'estimated', amount: 0.34, currency: 'USD', basis: 'estimation_debit_moyen', collectedAt: '2026-09-23T09:00:00Z' },
    ...overrides,
  };
}

function render(props: Record<string, unknown>): string {
  return renderToStaticMarkup(createElement(CockpitTableView, {
    pipelineId: 'pl_ventes_lignes',
    tab: 'metrics',
    state: { status: 'ready', data: tableData() } as CockpitTableState,
    window: '1h',
    onWindowChange() {},
    level: undefined,
    onLevelChange() {},
    correlateIncident: false,
    onCorrelateIncidentChange() {},
    onRefresh() {},
    ...props,
  }));
}

test('all five tabs are always present and the active one is marked current', () => {
  const markup = render({ tab: 'logs' });
  for (const label of ['Métriques', 'Dernières lignes', 'Journaux', 'Coûts', 'Preuves']) {
    assert.match(markup, new RegExp(`>${label}<`));
  }
  assert.match(markup, /aria-current="page" href="#\/cockpit\/table\/pl_ventes_lignes\/logs">Journaux</);
});

test('the metrics tab charts render, and the rows tab never leaks row values, only timestamped activity', () => {
  const metrics = render({ tab: 'metrics' });
  assert.match(metrics, /Retard/);
  assert.match(metrics, /Débit/);

  const rows = render({ tab: 'rows' });
  assert.match(rows, /jamais de valeur de ligne/);
  assert.match(rows, /Fenêtre confirmée/);
  assert.doesNotMatch(rows, /Connexion refusée/, 'only info-level entries belong in "Dernières lignes"');
});

test('loader telemetry identifies delivery latency and the bounded source', () => {
  const data = tableData({
    metrics: {
      window: '1h',
      points: [{ at: '2026-09-28T21:04:00Z', lagSeconds: 8.1, throughputRowsPerSecond: null }],
      provenance: 'journal_chargeur_kubernetes',
      freshness: 'bornée aux 2000 dernières lignes du pod',
      reason: 'Le retard est le délai IBM i → MERGE des lots livrés ; la série est bornée au journal conservé.',
    },
  });
  const markup = render({ state: { status: 'ready', data } });
  assert.match(markup, /Délai IBM i → miroir/);
  assert.match(markup, /bornée aux 2000 dernières lignes/);
});

test('the logs tab shows every entry with its incident tag, and exposes level/correlation filter controls', () => {
  const markup = render({ tab: 'logs' });
  assert.match(markup, /Fenêtre confirmée/);
  assert.match(markup, /Connexion refusée/);
  assert.match(markup, /inc_1/);
  assert.match(markup, /Corrélés à un incident seulement/);
});

test('the costs tab distinguishes measured/estimated from absent, and the proofs tab is honestly absent for v2', () => {
  const estimated = render({ tab: 'costs' });
  assert.match(estimated, /estimé/);
  const absent = render({ tab: 'costs', state: { status: 'ready', data: tableData({ costToday: null }) } });
  assert.match(absent, /Absent/);

  const proofs = render({ tab: 'proofs' });
  assert.match(proofs, /Absent/);
  assert.doesNotMatch(proofs, /reconcil/i);
});

test('loading and failed states never render tab content or the controls panel', () => {
  const loading = render({ state: { status: 'loading' } });
  assert.match(loading, /Lecture en cours/);
  assert.doesNotMatch(loading, /cockpit-table__tab"/);

  const failed = render({ state: { status: 'failed', message: 'panne v2' } });
  assert.match(failed, /Service indisponible/);
  assert.match(failed, /panne v2/);
});

test('the table controls panel offers all five contract actions', () => {
  const markup = render({ tab: 'metrics' });
  for (const label of ['Suspendre', 'Reprendre', 'Relancer la copie initiale', 'Retirer', 'Rejouer une plage']) {
    assert.match(markup, new RegExp(`>${label}<`));
  }
});

test('the function key bar is present and F12 jumps to the logs tab hash', () => {
  const markup = render({ tab: 'metrics' });
  assert.match(markup, /function-key-bar/);
  assert.match(markup, /aria-keyshortcuts="F12"/);
});
