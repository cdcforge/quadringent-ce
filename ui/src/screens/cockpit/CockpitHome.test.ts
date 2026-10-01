import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { CockpitConnection, CockpitTable } from '../../domain/cockpit.ts';
import type { CockpitHomeState, ConnectionHomeRow } from '../../data/useCockpit.ts';

function tableFixture(overrides: Partial<CockpitTable>): CockpitTable {
  return {
    pipelineId: 'pl_1',
    sourceId: 'src_1',
    name: 'CLIENTS',
    state: 'live',
    declaredState: 'live',
    lagSeconds: 12,
    throughputRowsPerSecond: 3,
    rowsSource: 1000,
    rowsDestination: 998,
    lastArrivalAt: '2026-09-23T09:00:00Z',
    absentReasons: {},
    ...overrides,
  };
}

let vite: ViteDevServer;
let CockpitHomeView: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  CockpitHomeView = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitHome.tsx')).CockpitHomeView;
});

after(async () => { await vite.close(); });

function connection(overrides: Partial<CockpitConnection>): CockpitConnection {
  return { id: 'src_1', label: 'Ventes', destinationId: 'dst_1', tables: [], ...overrides };
}

function row(overrides: Partial<ConnectionHomeRow> = {}): ConnectionHomeRow {
  return {
    connection: connection({}),
    lagSeconds: 12,
    throughputRowsPerSecond: 3,
    costToday: { scope: 'connection', id: 'src_1', window: '24h', status: 'measured', amount: 4.5, currency: 'USD', basis: 'warehouse_credits', collectedAt: '2026-09-23T09:00:00Z' },
    ...overrides,
  };
}

function render(state: CockpitHomeState): string {
  return renderToStaticMarkup(createElement(CockpitHomeView, { state, onRefresh() {} }));
}

test('loading and failed each read as their own state, never as an empty successful list', () => {
  const loading = render({ status: 'loading' });
  assert.match(loading, /Lecture en cours/);
  const failed = render({ status: 'failed', message: 'Service /v2 indisponible' });
  assert.match(failed, /Service indisponible/);
  assert.match(failed, /Service \/v2 indisponible/);
});

test('zero connections offers the wizard link, not a bare empty table', () => {
  const markup = render({ status: 'ready', rows: [] });
  assert.match(markup, /Lancer l.assistant/);
  assert.match(markup, /#\/wizard\/source/);
  assert.match(markup, /#\/cockpit\/confirmations/);
});

test('one row per connection, each linking to its connection screen, with no attention banner when nothing needs attention', () => {
  const markup = render({ status: 'ready', rows: [row({ connection: connection({ id: 'src_1', label: 'Ventes' }) })] });
  assert.match(markup, /#\/cockpit\/connection\/src_1/);
  assert.match(markup, />Ventes</);
  assert.doesNotMatch(markup, /cockpit-attention-banner/);
});

test('exactly one attention banner appears when any connection needs attention, never one per row', () => {
  const withAttentionTable: CockpitConnection = connection({
    id: 'src_2',
    tables: [tableFixture({ pipelineId: 'pl_1', sourceId: 'src_2', name: 'LIGNES', state: 'attention', declaredState: 'attention' })],
  });
  const markup = render({
    status: 'ready',
    rows: [row({ connection: connection({ id: 'src_1' }) }), row({ connection: withAttentionTable })],
  });
  const matches = markup.match(/cockpit-attention-banner/g) ?? [];
  assert.equal(matches.length, 1);
});

test('an absent lag/throughput/cost renders as an explicit dash or "Absent", never a fabricated zero', () => {
  const markup = render({
    status: 'ready',
    rows: [row({ lagSeconds: null, throughputRowsPerSecond: null, costToday: null })],
  });
  assert.match(markup, />—</);
  assert.match(markup, />Absent</);
  assert.doesNotMatch(markup, />0 s</);
});

test('an estimated cost is labelled as such, a measured one is not', () => {
  const estimated = render({ status: 'ready', rows: [row({ costToday: { scope: 'connection', id: 'src_1', window: '24h', status: 'estimated', amount: 1, currency: 'USD', basis: 'x', collectedAt: null } })] });
  assert.match(estimated, /\(estimé\)/);
  const measured = render({ status: 'ready', rows: [row()] });
  assert.doesNotMatch(measured, /\(estimé\)/);
});

test('the function key bar declares F3/F5/F9/F12, F9 disabled without rows and F12 always disabled (no journal at this level)', () => {
  const empty = render({ status: 'ready', rows: [] });
  assert.match(empty, /function-key-bar/);
  assert.match(empty, /F3/);
  assert.match(empty, /F5/);
  const f9Button = /<button type="button" class="function-key-bar__key" disabled=""[^>]*aria-keyshortcuts="F9"/;
  assert.match(empty, f9Button);
  const f12Button = /aria-keyshortcuts="F12"/;
  assert.match(empty, f12Button);

  const withRows = render({ status: 'ready', rows: [row()] });
  assert.doesNotMatch(withRows, /aria-keyshortcuts="F9"[^>]*disabled=""/);
});
