import assert from 'node:assert/strict';
import test from 'node:test';
import { liveCockpitResolvers } from './cockpitClient.ts';
import { buildConnections, groupTablesBySource } from '../domain/cockpit.ts';
import type { PipelineListRecordV2 } from './controlPlaneV2Client.ts';

test('live cockpit groups real pipeline ids under their declared source and displays table names', () => {
  const pipelines: readonly PipelineListRecordV2[] = [
    {
      id: 'pl_orders', tableId: 'tbl_orders', sourceId: 'src_test400', destinationId: 'dst_snowflake',
      declaredState: 'live',
      observation: {
        lagSeconds: 4, throughputRowsPerSecond: 1, rowsSource: 111,
        rowsDestination: 111, lastArrivalAt: null, absentReasons: {},
      },
    },
    {
      id: 'pl_tail', tableId: 'tbl_tail', sourceId: 'src_test400', destinationId: 'dst_snowflake',
      declaredState: 'live',
      observation: {
        lagSeconds: 2, throughputRowsPerSecond: 1, rowsSource: 2,
        rowsDestination: 2, lastArrivalAt: null, absentReasons: {},
      },
    },
  ];
  const resolvers = liveCockpitResolvers(pipelines, new Map([
    ['tbl_orders', 'TESTLIB.QDC_ORDERS'],
    ['tbl_tail', 'TESTLIB.QDC_TAIL'],
  ]));
  const grouped = groupTablesBySource(pipelines, resolvers.sourceIdForPipeline, resolvers.nameForPipeline);
  const connections = buildConnections(
    [{ id: 'src_test400', displayName: 'TEST400', host: 'test400.example' }],
    [{ id: 'dst_snowflake', accountIdentifier: 'example', verificationState: 'verified', sqlScript: '', privateKeyPem: null }],
    grouped,
  );

  assert.equal(connections[0]?.tables.length, 2);
  assert.deepEqual(connections[0]?.tables.map((table) => table.name), [
    'TESTLIB.QDC_ORDERS', 'TESTLIB.QDC_TAIL',
  ]);
  assert.equal(resolvers.sourceIdForPipeline('unknown'), null);
  assert.equal(resolvers.nameForPipeline('unknown'), 'unknown');
});
