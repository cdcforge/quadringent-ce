import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildConnections,
  connectionAggregateState,
  connectionNeedsAttention,
  declaredStateToStatusWord,
  fleetNeedsAttention,
  aggregateConnectionMetrics,
  groupTablesBySource,
  sortAndFilterTables,
  type CockpitTable,
} from './cockpit.ts';
import type { SourceRecord, DestinationRecord, PipelineListRecordV2 } from '../data/controlPlaneV2Client.ts';

test('declaredStateToStatusWord maps the six pipeline states to the five StatusWord words, not_started folding to stopped', () => {
  assert.equal(declaredStateToStatusWord('live'), 'live');
  assert.equal(declaredStateToStatusWord('copying'), 'copying');
  assert.equal(declaredStateToStatusWord('paused'), 'paused');
  assert.equal(declaredStateToStatusWord('attention'), 'attention');
  assert.equal(declaredStateToStatusWord('stopped'), 'stopped');
  assert.equal(declaredStateToStatusWord('not_started'), 'stopped');
});

function table(overrides: Partial<CockpitTable>): CockpitTable {
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

function listPipeline(overrides: Partial<PipelineListRecordV2> & { readonly id: string; readonly declaredState: PipelineListRecordV2['declaredState'] }): PipelineListRecordV2 {
  return {
    tableId: overrides.id,
    sourceId: '',
    destinationId: '',
    observation: { lagSeconds: null, throughputRowsPerSecond: null, rowsSource: null, rowsDestination: null, lastArrivalAt: null, absentReasons: {} },
    ...overrides,
  };
}

test('buildConnections pairs sources with destinations by position and attaches grouped tables', () => {
  const sources: readonly SourceRecord[] = [
    { id: 'src_1', displayName: 'Ventes', host: 'as400-1.local' },
    { id: 'src_2', displayName: null, host: 'as400-2.local' },
  ];
  const destinations: readonly DestinationRecord[] = [
    { id: 'dst_1', accountIdentifier: 'abcd-1', destinationDatabase: 'QUADRINGENT', destinationSchema: null, verificationState: 'declared_not_verified', sqlScript: '', privateKeyPem: null },
  ];
  const tablesBySource = new Map([['src_1', [table({})]]]);
  const connections = buildConnections(sources, destinations, tablesBySource);
  assert.equal(connections.length, 2);
  assert.equal(connections[0]!.label, 'Ventes');
  assert.equal(connections[0]!.destinationId, 'dst_1');
  assert.equal(connections[0]!.tables.length, 1);
  // src_2 has no matching destination (only one destination exists) and no tables — never fabricated.
  assert.equal(connections[1]!.label, 'as400-2.local');
  assert.equal(connections[1]!.destinationId, null);
  assert.deepEqual(connections[1]!.tables, []);
});

test('connectionNeedsAttention and fleetNeedsAttention are true only when a table is actually in attention', () => {
  const calm = { id: 'c1', label: 'c1', destinationId: null, tables: [table({ state: 'live' }), table({ state: 'paused' })] };
  const alarmed = { id: 'c2', label: 'c2', destinationId: null, tables: [table({ state: 'attention' })] };
  assert.equal(connectionNeedsAttention(calm), false);
  assert.equal(connectionNeedsAttention(alarmed), true);
  assert.equal(fleetNeedsAttention([calm]), false);
  assert.equal(fleetNeedsAttention([calm, alarmed]), true);
});

test('connectionAggregateState prioritizes attention, then copying, then live, then paused, and stopped for no tables', () => {
  const of = (states: CockpitTable['state'][]) => connectionAggregateState({
    id: 'c', label: 'c', destinationId: null, tables: states.map((state) => table({ state })),
  });
  assert.equal(of(['live', 'attention']), 'attention');
  assert.equal(of(['live', 'copying']), 'copying');
  assert.equal(of(['paused', 'live']), 'live');
  assert.equal(of(['paused']), 'paused');
  assert.equal(of([]), 'stopped');
});

test('sortAndFilterTables filters case-insensitively by name and sorts by name or state, either direction', () => {
  const tables = [table({ name: 'LIGNES_CDE', state: 'paused' }), table({ name: 'CLIENTS', state: 'live' }), table({ name: 'COMMANDES', state: 'attention' })];
  const byNameAsc = sortAndFilterTables(tables, '', 'name', 'asc').map((t) => t.name);
  assert.deepEqual(byNameAsc, ['CLIENTS', 'COMMANDES', 'LIGNES_CDE']);
  const byNameDesc = sortAndFilterTables(tables, '', 'name', 'desc').map((t) => t.name);
  assert.deepEqual(byNameDesc, ['LIGNES_CDE', 'COMMANDES', 'CLIENTS']);
  const filtered = sortAndFilterTables(tables, 'comm', 'name', 'asc').map((t) => t.name);
  assert.deepEqual(filtered, ['COMMANDES']);
});

test('groupTablesBySource ignores pipelines whose id does not resolve to a known source, never guessing', () => {
  const pipelines: readonly PipelineListRecordV2[] = [
    listPipeline({ id: 'pl_1', declaredState: 'live' }),
    listPipeline({ id: 'pl_orphan', declaredState: 'paused' }),
  ];
  const grouped = groupTablesBySource(
    pipelines,
    (id) => (id === 'pl_1' ? 'src_1' : null),
    (id) => (id === 'pl_1' ? 'CLIENTS' : id),
  );
  assert.equal(grouped.size, 1);
  assert.equal(grouped.get('src_1')![0]!.name, 'CLIENTS');
});

test('groupTablesBySource carries the observed figures through onto each CockpitTable, absent reasons included', () => {
  const pipelines: readonly PipelineListRecordV2[] = [
    listPipeline({
      id: 'pl_1',
      declaredState: 'paused',
      observation: {
        lagSeconds: null,
        throughputRowsPerSecond: null,
        rowsSource: 500,
        rowsDestination: null,
        lastArrivalAt: null,
        absentReasons: { lag_seconds: 'Table en pause.', rows_destination: 'Table en pause.' },
      },
    }),
  ];
  const grouped = groupTablesBySource(pipelines, () => 'src_1', () => 'MOUVEMENTS');
  const [row] = grouped.get('src_1')!;
  assert.equal(row!.lagSeconds, null);
  assert.equal(row!.rowsSource, 500);
  assert.equal(row!.rowsDestination, null);
  assert.equal(row!.absentReasons.lag_seconds, 'Table en pause.');
});

test('aggregateConnectionMetrics takes the worst lag and sums throughput, ignoring absent tables but not zeroing out when all are absent', () => {
  const of = (points: Array<{ lagSeconds: number | null; throughputRowsPerSecond: number | null } | null>) =>
    aggregateConnectionMetrics(points.map((p) => (p ? { at: '2026-09-23T09:00:00Z', ...p } : null)));
  assert.deepEqual(of([{ lagSeconds: 10, throughputRowsPerSecond: 2 }, { lagSeconds: 40, throughputRowsPerSecond: 5 }]), { lagSeconds: 40, throughputRowsPerSecond: 7 });
  assert.deepEqual(of([{ lagSeconds: 10, throughputRowsPerSecond: 2 }, null]), { lagSeconds: 10, throughputRowsPerSecond: 2 });
  assert.deepEqual(of([null, null]), { lagSeconds: null, throughputRowsPerSecond: null });
  assert.deepEqual(of([]), { lagSeconds: null, throughputRowsPerSecond: null });
});
