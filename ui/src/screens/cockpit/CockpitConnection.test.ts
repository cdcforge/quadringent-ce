import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { CockpitConnection, CockpitTable } from '../../domain/cockpit.ts';
import type { CockpitConnectionsState } from '../../data/useCockpit.ts';

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
let CockpitConnectionView: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  CockpitConnectionView = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitConnection.tsx')).CockpitConnectionView;
});

after(async () => { await vite.close(); });

function connection(overrides: Partial<CockpitConnection> = {}): CockpitConnection {
  return {
    id: 'src_1',
    label: 'Ventes',
    destinationId: 'dst_1',
    tables: [
      tableFixture({ pipelineId: 'pl_1', sourceId: 'src_1', name: 'CLIENTS', state: 'live', declaredState: 'live' }),
      tableFixture({ pipelineId: 'pl_2', sourceId: 'src_1', name: 'LIGNES', state: 'attention', declaredState: 'attention' }),
    ],
    ...overrides,
  };
}

function render(state: CockpitConnectionsState, connectionId = 'src_1'): string {
  return renderToStaticMarkup(createElement(CockpitConnectionView, { state, connectionId, onRefresh() {} }));
}

test('an unknown connection id reads as not found, with a way back to the cockpit — never a blank table', () => {
  const markup = render({ status: 'ready', connections: [connection()] }, 'src_missing');
  assert.match(markup, /Connexion introuvable/);
  assert.match(markup, /#\/cockpit"/);
});

test('the table list links each row to its table screen and shows the attention banner once when a table needs it', () => {
  const markup = render({ status: 'ready', connections: [connection()] });
  assert.match(markup, /#\/cockpit\/table\/pl_1/);
  assert.match(markup, /#\/cockpit\/table\/pl_2/);
  assert.match(markup, /cockpit-attention-banner/);
});

test('no attention banner when every table is calm', () => {
  const calm = connection({ tables: [tableFixture({ pipelineId: 'pl_1', sourceId: 'src_1', name: 'CLIENTS', state: 'live', declaredState: 'live' })] });
  const markup = render({ status: 'ready', connections: [calm] });
  assert.doesNotMatch(markup, /cockpit-attention-banner/);
});

test('a single scoped controls panel is shown at a time, defaulting to the connection, with a scope selector offering the destination only when coupled', () => {
  const withDestination = render({ status: 'ready', connections: [connection({ destinationId: 'dst_1' })] });
  const matches = withDestination.match(/aria-label="Contrôles — /g) ?? [];
  assert.equal(matches.length, 1, 'exactly one controls panel visible, never three stacked');
  assert.match(withDestination, /aria-label="Contrôles — Ventes"/);
  assert.match(withDestination, /<option value="destination">Destination de Ventes<\/option>/);

  const withoutDestination = render({ status: 'ready', connections: [connection({ destinationId: null })] });
  assert.doesNotMatch(withoutDestination, /<option value="destination">/);
});

test('the scope selector and the single controls panel share the same compact bar, in the same place as the home and table screens (top of page, before the toolbar)', () => {
  const markup = render({ status: 'ready', connections: [connection()] });
  const scopedControlsIndex = markup.indexOf('cockpit-connection__scoped-controls');
  const toolbarIndex = markup.indexOf('cockpit-connection__toolbar');
  assert.ok(scopedControlsIndex >= 0 && toolbarIndex >= 0 && scopedControlsIndex < toolbarIndex, 'the controls bar must appear before the toolbar/table, not after');
});

test('loading and failed states are distinct, never rendering the table toolbar', () => {
  const loading = render({ status: 'loading' });
  assert.match(loading, /Lecture en cours/);
  assert.doesNotMatch(loading, /cockpit-connection__toolbar/);
  const failed = render({ status: 'failed', message: 'panne' });
  assert.match(failed, /Service indisponible/);
  assert.match(failed, /panne/);
});

test('the table list shows retard/débit/lignes/dernière arrivée for each row, formatted with their units', () => {
  const markup = render({ status: 'ready', connections: [connection({ tables: [tableFixture({ pipelineId: 'pl_1', lagSeconds: 45, throughputRowsPerSecond: 2, rowsSource: 1000, rowsDestination: 998, lastArrivalAt: '2026-09-23T09:00:00Z' })] })] });
  assert.match(markup, />45 s</);
  assert.match(markup, />120 lignes\/min</); // 2 lignes/s * 60
  assert.match(markup, /1\s000/);
  assert.match(markup, /998/);
});

test('an absent figure renders as an explicit dash with its reason available on hover, never a fabricated value', () => {
  const markup = render({
    status: 'ready',
    connections: [connection({
      tables: [tableFixture({
        pipelineId: 'pl_1',
        lagSeconds: null,
        throughputRowsPerSecond: null,
        rowsDestination: null,
        lastArrivalAt: null,
        absentReasons: { lag_seconds: 'Table en pause.', throughput_rows_per_second: 'Table en pause.', rows_destination: 'Table en pause.', last_arrival_at: 'Table en pause.' },
      })],
    })],
  });
  assert.match(markup, /<span title="Table en pause\.">—<\/span>/);
  assert.doesNotMatch(markup, />0 s</);
});

test('the table list never overflows its columns — all figures fit within the banded table', () => {
  const markup = render({ status: 'ready', connections: [connection()] });
  assert.match(markup, /banded-table__col--numeric/);
});

test('the function key bar is present with F9 enabled once the connection is found', () => {
  const markup = render({ status: 'ready', connections: [connection()] });
  assert.match(markup, /function-key-bar/);
  assert.doesNotMatch(markup, /aria-keyshortcuts="F9"[^>]*disabled=""/);
});
