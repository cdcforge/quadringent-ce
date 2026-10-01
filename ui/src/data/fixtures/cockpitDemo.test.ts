import assert from 'node:assert/strict';
import test from 'node:test';
import { ControlPlaneV2Client, pendingConfirmationId } from '../controlPlaneV2Client.ts';
import {
  createCockpitDemoFetch,
  DEMO_SOURCE_VENTES,
  DEMO_SOURCE_STOCKS,
  demoNameForPipeline,
  demoSourceIdForPipeline,
} from './cockpitDemo.ts';
import { buildConnections, connectionAggregateState, groupTablesBySource } from '../../domain/cockpit.ts';

function client(): ControlPlaneV2Client {
  return new ControlPlaneV2Client({ fetchFn: createCockpitDemoFetch() });
}

test('the demo dataset has two connections, eight tables total, one incident, one paused table, one copying', async () => {
  const c = client();
  const sources = await c.listSources();
  const destinations = await c.listDestinations();
  const pipelines = await c.listPipelines();
  assert.equal(sources.length, 2);
  assert.equal(destinations.length, 2);
  assert.equal(pipelines.length, 8);

  const grouped = groupTablesBySource(pipelines, demoSourceIdForPipeline, demoNameForPipeline);
  const connections = buildConnections(sources, destinations, grouped);
  assert.equal(connections.length, 2);
  const ventes = connections.find((c) => c.id === DEMO_SOURCE_VENTES)!;
  const stocks = connections.find((c) => c.id === DEMO_SOURCE_STOCKS)!;
  assert.equal(ventes.tables.length, 4);
  assert.equal(stocks.tables.length, 4);
  assert.equal(connectionAggregateState(ventes), 'attention');
  assert.equal(connectionAggregateState(stocks), 'copying');
  assert.equal(stocks.tables.some((t) => t.declaredState === 'paused'), true);
});

test('a non-sensitive pipeline action (pause) executes directly through the demo, verified by a fresh read', async () => {
  const c = client();
  const before = await c.getPipeline('pl_ventes_clients');
  assert.equal(before.declaredState, 'live');
  const envelope = await c.runPipelineAction('pl_ventes_clients', 'pause');
  assert.equal(envelope.after?.declaredState, 'paused');
  const after = await c.getPipeline('pl_ventes_clients');
  assert.equal(after.declaredState, 'paused');
});

test('a sensitive action (remove) requires confirmation, and approving it lets the rerun succeed', async () => {
  const c = client();
  await assert.rejects(
    () => c.runPipelineAction('pl_ventes_archives', 'remove'),
    (error: unknown) => {
      const id = pendingConfirmationId(error as import('../controlPlaneV2Client.ts').ControlPlaneV2Error);
      assert.ok(id);
      return true;
    },
  );
  const pending = await c.listConfirmations('pending');
  const confirmation = pending.find((item) => item.resourceId === 'pl_ventes_archives')!;
  assert.ok(confirmation);
  assert.deepEqual(confirmation.available, { approve: true, reject: true, execute: false });
  const approved = await c.approveConfirmation(confirmation.id);
  assert.equal(approved.state, 'approved');
  const readyToExecute = await c.getConfirmation(confirmation.id);
  assert.deepEqual(readyToExecute.available, { approve: false, reject: false, execute: true });
  const envelope = await c.runPipelineAction('pl_ventes_archives', 'remove', { confirmationToken: confirmation.id });
  assert.equal(envelope.after?.declaredState, 'stopped');
  assert.equal((await c.getConfirmation(confirmation.id)).state, 'used');
  assert.equal((await c.getPipeline('pl_ventes_archives')).declaredState, 'stopped');
});

test('dry_run never mutates state and reports the announced effect', async () => {
  const c = client();
  const before = await c.getPipeline('pl_stocks_entrepots');
  const envelope = await c.runPipelineAction('pl_stocks_entrepots', 'pause', { dryRun: true });
  assert.ok(envelope.dryRun);
  const after = await c.getPipeline('pl_stocks_entrepots');
  assert.equal(after.declaredState, before.declaredState);
});

test('source-level pause affects every table of that source only, and resume brings them back live', async () => {
  const c = client();
  await c.runSourceAction(DEMO_SOURCE_STOCKS, 'pause');
  const stocksPipelines = await Promise.all(['pl_stocks_articles', 'pl_stocks_entrepots', 'pl_stocks_alertes'].map((id) => c.getPipeline(id)));
  assert.ok(stocksPipelines.every((p) => p.declaredState === 'paused'));
  const ventesUnaffected = await c.getPipeline('pl_ventes_clients');
  assert.equal(ventesUnaffected.declaredState, 'live');
  await c.runSourceAction(DEMO_SOURCE_STOCKS, 'resume');
  const resumed = await c.getPipeline('pl_stocks_articles');
  assert.equal(resumed.declaredState, 'live');
});

test('metrics are deterministic across two reads and absent (null) for a paused pipeline', async () => {
  const c = client();
  const first = await c.getMetrics('pl_stocks_mouvements', '1h');
  const second = await c.getMetrics('pl_stocks_mouvements', '1h');
  assert.deepEqual(first, second);
  assert.equal(first.points.length, 12);
  assert.ok(first.points.every((point) => point.lagSeconds === null));

  const live = await c.getMetrics('pl_ventes_clients', '24h');
  assert.equal(live.points.length, 24);
  assert.ok(live.points.every((point) => point.lagSeconds !== null));
});

test('logs correlate the incident pipeline and never leak row data (no row/value fields)', async () => {
  const c = client();
  const all = await c.getLogs('pl_ventes_lignes');
  assert.ok(all.some((entry) => entry.incidentId !== null));
  const correlated = await c.getLogs('pl_ventes_lignes', { correlateIncident: true });
  assert.ok(correlated.length > 0);
  assert.ok(correlated.every((entry) => entry.incidentId !== null));
  for (const entry of all) {
    assert.doesNotMatch(JSON.stringify(entry), /"row"|"value"|"data"/);
  }
});

test('costs cover the three provenances: measured (connection), estimated, and absent (paused table)', async () => {
  const c = client();
  const measuredConnection = await c.getCosts('connection', DEMO_SOURCE_VENTES, '24h');
  assert.equal(measuredConnection.status, 'measured');
  const estimatedConnection = await c.getCosts('connection', DEMO_SOURCE_STOCKS, '24h');
  assert.equal(estimatedConnection.status, 'estimated');
  const absentTable = await c.getCosts('table', 'pl_stocks_mouvements', '24h');
  assert.equal(absentTable.status, 'absent');
  assert.equal(absentTable.amount, null);
});
