import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { testFleetWire, measuredTestFleetWire } from '../data/fixtures/test-fleet.ts';
import { ControlPlaneParseError, parseOverview, parsePipeline } from './controlPlane.ts';
import { installTestSite, TEST_SITE } from './siteFixture.ts';

installTestSite();

export function validOverviewFixture(): unknown {
  return {
    revision: 7,
    generated_at: '2026-08-28T10:00:00+00:00',
    scope: { kind: 'single', environments: ['dev'] },
    sources: [
      { id: 'dev-cntr', evidence_kind: 'live', environment: 'dev', status: 'available', error: null },
    ],
    pipelines: [
      {
        id: 'dev-cntr',
        environment: 'dev',
        status: 'degraded',
        quality: { coverage: 'partial', freshness: 'fresh', evidence_kind: 'live' },
        summary: 'Capture saine, livraison Snowflake non prouvée',
        observed_at: '2026-08-28T09:59:00+00:00',
        stages: [
          { id: 'source', status: 'healthy', observed_at: '2026-08-28T09:59:00+00:00', headline: 'Source observée', detail: 'Présente' },
          { id: 'capture', status: 'healthy', observed_at: '2026-08-28T09:59:00+00:00', headline: 'Capture active', detail: 'Présente' },
          { id: 'raw', status: 'healthy', observed_at: '2026-08-28T09:59:00+00:00', headline: 'Raw publié', detail: 'Présente' },
          { id: 'load', status: 'unknown', observed_at: null, headline: 'Chargement non observé', detail: 'Absent' },
          { id: 'destination', status: 'unknown', observed_at: null, headline: 'Destination non observée', detail: 'Absent' },
        ],
        lag_sequences: 3,
        lag_seconds: null,
        lag_series: null,
        counters: { events_published: 12, errors: null },
        incident: null,
      },
    ],
  };
}

function windowFixture() {
  return {state:'matched',archive_run_id:'r1',window_id:'w1',started_at:'2026-08-28T09:40:00+00:00',
    closed_at:'2026-08-28T09:50:00+00:00',destination_observed_at:'2026-08-28T09:51:00+00:00',
    event_count:12,scope:'closed_window_only',quality:{freshness:'stale',evidence_kind:'historical'}};
}

test('les coûts d’infrastructure conservent le tarif et refusent les fenêtres incohérentes', () => {
  const wire = (validOverviewFixture() as any).pipelines[0];
  wire.infrastructure_costs = {
    status:'available',collected_at:'2026-09-22T12:00:00Z',namespace:'quadringent-test',
    storage:{status:'measured',observed_at:'2026-09-21T08:00:00Z',bytes:'1073741824',
      price_per_gib_month:'0.037',monthly_run_rate:'0.037',currency:'USD',basis:'aws_public_standard_first_tier'},
    cluster:{status:'measured',start:'2026-09-21T00:00:00Z',end:'2026-09-22T00:00:00Z',
      allocated_amount:'0.25',cluster_amount:'3',idle_amount:null,currency:'EUR',basis:'opencost_no_idle_share'},
  };
  assert.equal(parsePipeline(wire).infrastructureCosts?.storage?.pricePerGibMonth,'0.037');
  assert.equal(parsePipeline(wire).infrastructureCosts?.cluster?.idleAmount,null);
  wire.infrastructure_costs.cluster.end='2026-09-22T12:00:00Z';
  assert.throws(()=>parsePipeline(wire),ControlPlaneParseError);
});

test('chain progress is preserved and inconsistent success is rejected', () => {
  const wire = (validOverviewFixture() as any).pipelines[0];
  const chain = {declared_windows:3, matched_windows:1, capture_complete:false, state:'incomplete', evidence_kind:'simulation'};
  wire.window_delivery = {state:'unavailable', chain};
  assert.equal(parsePipeline(wire).windowDelivery?.chain?.matchedWindows, 1);
  for (const change of [{matched_windows:4}, {declared_windows:true}, {state:'matched'}, {evidence_kind:'invented'}]) {
    wire.window_delivery = {state:'unavailable', chain:{...chain, ...change}};
    assert.equal(parsePipeline(wire).windowDelivery?.state, 'invalid');
  }
  wire.window_delivery = {...windowFixture(), chain:{...chain, matched_windows:3, capture_complete:true, state:'matched', evidence_kind:'historical'}};
  assert.equal(parsePipeline(wire).windowDelivery?.chain?.state, 'matched');
});

test('window delivery is retained without replacing the capture verdict', () => {
  const overview = validOverviewFixture() as any;
  const before = parsePipeline(overview.pipelines[0]);
  overview.pipelines[0].window_delivery = windowFixture();
  const after = parsePipeline(overview.pipelines[0]);
  assert.equal(after.windowDelivery?.state,'matched');
  assert.equal(after.status,before.status);
  assert.deepEqual(after.counters,before.counters);
  assert.deepEqual(after.quality,before.quality);
});

test('malformed window evidence is isolated, not a lost capture', () => {
  for (const patch of [{event_count:true},{event_count:0},{scope:'entire_pipeline'},
    {destination_observed_at:'2026-08-28T09:00:00+00:00'},{quality:{freshness:'fresh',evidence_kind:'invented'}}]) {
    const overview = validOverviewFixture() as any;
    overview.pipelines[0].window_delivery = {...windowFixture(),...patch};
    const pipeline=parsePipeline(overview.pipelines[0]);
    assert.equal(pipeline.windowDelivery?.state,'invalid');
    assert.equal(pipeline.status,'degraded');
  }
});

test('window evidence preserves absence, unavailable, empty and simulation boundaries', () => {
  const overview=validOverviewFixture() as any;
  const wire=overview.pipelines[0];
  assert.equal(parsePipeline(wire).windowDelivery,null);
  wire.window_delivery={state:'unavailable',reason:'window_proof_read_failed'};
  assert.deepEqual(parsePipeline(wire).windowDelivery,{state:'unavailable'});
  wire.window_delivery={...windowFixture(),state:'not_tested',event_count:0};
  assert.equal(parsePipeline(wire).windowDelivery?.state,'not_tested');
  wire.quality.evidence_kind='historical';
  wire.window_delivery={...windowFixture(),quality:{freshness:'fresh',evidence_kind:'live'}};
  assert.equal(parsePipeline(wire).windowDelivery?.state,'invalid');
  wire.quality.evidence_kind='simulation';
  wire.window_delivery={...windowFixture(),quality:{freshness:'fresh',evidence_kind:'live'}};
  assert.equal(parsePipeline(wire).windowDelivery?.state,'invalid');
  wire.window_delivery.quality.evidence_kind='simulation';
  const parsed=parsePipeline(wire).windowDelivery;
  assert.equal(parsed?.state,'matched');
  if (parsed?.state==='matched') assert.equal(parsed.quality.evidenceKind,'simulation');
});

test('actual Python window projection survives the frontend parser', () => {
  const output=execFileSync('python3',['-c',`
import sys, json
sys.path[:0]=['../src','../scripts','../tests']
import site_fixture  # noqa: F401 — installe les QUADRINGENT_* du site fictif acme
from test_window_delivery_projection import fresh_running_document, evidence, LIVE_SOURCE, NOW
from quadringent_control_plane.projection import project_console_document
document=fresh_running_document()
document['window_destination_proof']=evidence()
print(json.dumps(project_console_document(document,LIVE_SOURCE,NOW).to_dict()))
`],{encoding:'utf8'});
  const parsed=parsePipeline(JSON.parse(output));
  assert.equal(parsed.windowDelivery?.state,'matched');
  if (parsed.windowDelivery?.state==='matched') assert.equal(parsed.windowDelivery.eventCount,1);
});

test('fleet is omitted without regressing capture, SLO or window parsing', () => {
  const parsed = parsePipeline((validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]);
  assert.equal(parsed.fleet, null);
  assert.equal(parsed.status, 'degraded');
  assert.equal(parsed.windowDelivery, null);
  assert.equal(parsed.observability?.reason, 'observability_not_attached');
});

test('real Python fleet plan survives the frontend parser without invented runtime values', () => {
  const repoRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const output = execFileSync('python3', ['-c', String.raw`
import json
import site_fixture  # noqa: F401 — installe les QUADRINGENT_* du site fictif acme
from quadringent_control_plane.fleet_sidecar import generate_fleet_ui_sidecar
from test_fleet_plan import catalog_payload
print(json.dumps(generate_fleet_ui_sidecar(catalog_payload())["fleet"]))
`], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: 'src:tests' },
    encoding: 'utf8',
  });
  const plan = JSON.parse(output) as Record<string, unknown>;
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.fleet_plan = plan;
  const parsed = parsePipeline(wire).fleetPlan;
  assert.ok(parsed);
  assert.equal(parsed.observedTotals.tableCount, 13);
  assert.equal(parsed.observedTotals.rowCount, 218_150_587);
  assert.equal(parsed.tables[0]?.rowCount, 81_649_395);
  assert.equal(parsed.tables[0]?.copiedRows, null);
  assert.equal(parsed.cost.observed, null);
  assert.equal(parsed.cost.status, 'unknown');
  assert.equal(parsed.continuity, 'uncertain');
  assert.equal(parsed.identity.keyedCount, 1);
  assert.equal(parsed.identity.rrnCount, 12);
  assert.equal(parsed.identity.blockedCount, 0);
});

test('fleet plan fails closed on fake cost, totals, identity or unknown fields', () => {
  const repoRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const output = execFileSync('python3', ['-c', String.raw`
import json
import site_fixture  # noqa: F401 — installe les QUADRINGENT_* du site fictif acme
from quadringent_control_plane.fleet_sidecar import generate_fleet_ui_sidecar
from test_fleet_plan import catalog_payload
print(json.dumps(generate_fleet_ui_sidecar(catalog_payload())["fleet"]))
`], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: 'src:tests' },
    encoding: 'utf8',
  });
  const original = JSON.parse(output) as Record<string, any>;
  const mutations: Array<(plan: Record<string, any>) => void> = [
    (plan) => { plan.cost.observed = 0; },
    (plan) => { plan.observed_totals.row_count += 1; },
    (plan) => { plan.identity.keyed_count += 1; },
    (plan) => { plan.tables.pop(); },
    (plan) => { plan.environment = 'PROD'; },
    (plan) => { plan.destination_namespace = 'OTHER_RAW.OTHER_SCHEMA'; },
    (plan) => { plan.secret = 'forbidden'; },
  ];
  for (const mutate of mutations) {
    const plan = structuredClone(original);
    mutate(plan);
    const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
    wire.fleet_plan = plan;
    assert.throws(() => parsePipeline(wire), ControlPlaneParseError);
  }
});

function pipelineWireWithFleet(fleet: Record<string, unknown>): Record<string, unknown> {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  const wire = overview.pipelines[0]!;
  wire.status = 'degraded';
  wire.quality = { coverage: 'partial', freshness: 'stale', evidence_kind: 'historical' };
  wire.fleet = fleet;
  return wire;
}

test('autonomous Forge fleet parses thirteen tables and preserves unknown counts', () => {
  const parsed = parsePipeline(pipelineWireWithFleet(testFleetWire()));
  assert.ok(parsed.fleet);
  assert.equal(parsed.fleet.fleetId, TEST_SITE.fleetId);
  assert.equal(parsed.fleet.formatVersion, 'quadringent-fleet-v1');
  assert.equal(parsed.fleet.environment, TEST_SITE.environment);
  assert.equal(parsed.fleet.destinationNamespace, TEST_SITE.destinationNamespace);
  assert.deepEqual(parsed.fleet.tables.map((table) => table.name), [...TEST_SITE.manifest]);
  assert.equal(parsed.fleet.tables.length, 13);
  assert.equal(parsed.fleet.summary.certifiedCount, 0);
  assert.equal(parsed.fleet.tables[0]?.copiedRows, null);
  assert.equal(parsed.fleet.tables[0]?.totalRows, null);
  assert.equal(parsed.fleet.tables[0]?.continuityProven, null);
  assert.equal(parsed.fleet.tables[0]?.gap, null);
});

test('measured fleet values stay exact and unknowns stay null rather than zero', () => {
  const parsed = parsePipeline(pipelineWireWithFleet(measuredTestFleetWire()));
  const byName = Object.fromEntries(parsed.fleet!.tables.map((table) => [table.name, table]));
  assert.equal(byName.COST1?.copiedRows, 4_200);
  assert.equal(byName.COST1?.totalRows, 10_000);
  assert.equal(byName.CUSTOM1?.copiedRows, null);
  assert.equal(byName.CUSTOM1?.totalRows, null);
  assert.equal(byName.SALE?.reconciliationProof?.sourceCount, 10_194);
  assert.equal(byName.SALE?.reconciliationProof?.latencySeconds, 4.5);
  assert.equal(parsed.fleet?.summary.certifiedCount, 1);
  assert.equal(parsed.fleet?.summary.knownCopiedRows, 14_394);
});

test('fleet parse fails closed on incomplete, reordered, altered or unsafe payloads', () => {
  const invalid: Array<(fleet: Record<string, any>) => void> = [
    (fleet) => { fleet.tables.pop(); },
    (fleet) => { fleet.tables[0].name = 'CNTR'; fleet.tables[10].name = 'ADDRS1'; },
    (fleet) => { fleet.tables.push({ ...fleet.tables[0], name: 'FOURNIS' }); },
    (fleet) => { fleet.environment = 'PROD'; },
    (fleet) => { fleet.format_version = 'quadringent-fleet-v0'; },
    (fleet) => { fleet.destination_namespace = 'OTHER_RAW.OTHER_SCHEMA'; },
    (fleet) => { fleet.fleet_id = 'other-fleet'; },
    (fleet) => { fleet.credit_budget = Number.NaN; },
    (fleet) => { fleet.consumed_credits = -1; },
    (fleet) => { delete fleet.tables[0].copied_rows; },
    (fleet) => {
      fleet.tables[6].phase = 'CERTIFIED';
      fleet.tables[6].start_checkpoint = { receiver: 'TRNJRN3776', sequence: 1 };
      fleet.tables[6].admitted = true;
      fleet.tables[6].reserved_credits = 0;
      fleet.tables[6].actual_credits = 1;
      fleet.tables[6].proof_window = { start_utc: '2026-09-13T08:00:00Z', end_utc: '2026-09-13T09:00:00Z' };
      fleet.tables[6].reconciliation_proof = {
        window: { start_utc: '2026-09-13T08:00:00Z', end_utc: '2026-09-13T09:00:00Z' },
        source_count: 1, target_count: 1, missing: 0, extra: 0, duplicates: 0,
        source_hash: 'md5:not-a-sha',
        target_hash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        destination_freshness_seconds: 1, freshness_slo_seconds: 60, latency_seconds: 1,
        throughput_rows_per_second: 1, cost_units: 1,
      };
      fleet.summary.certified_count = 1;
      fleet.summary.next_table = 'ADDRS1';
    },
    (fleet) => {
      fleet.tables[6].phase = 'CERTIFIED';
      fleet.tables[6].start_checkpoint = { receiver: 'TRNJRN3776', sequence: 1 };
      fleet.tables[6].admitted = true;
      fleet.tables[6].reserved_credits = 0;
      fleet.tables[6].actual_credits = 1;
      fleet.tables[6].proof_window = { start_utc: '2026-09-13T08:00:00.500Z', end_utc: '2026-09-13T09:00:00Z' };
      fleet.tables[6].reconciliation_proof = {
        window: { start_utc: '2026-09-13T08:00:00.500Z', end_utc: '2026-09-13T09:00:00Z' },
        source_count: 1, target_count: 1, missing: 0, extra: 0, duplicates: 0,
        source_hash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        target_hash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        destination_freshness_seconds: 1, freshness_slo_seconds: 60, latency_seconds: 1,
        throughput_rows_per_second: 1, cost_units: 1,
      };
      fleet.summary.certified_count = 1;
    },
  ];
  for (const mutate of invalid) {
    const fleet = testFleetWire();
    mutate(fleet);
    assert.throws(() => parsePipeline(pipelineWireWithFleet(fleet)), ControlPlaneParseError);
  }
});

test('leftover business_circulation key is ignored and does not parse a comparator', () => {
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.business_circulation = { dataset: 'SALE', paths: [{ id: 'popsink' }] };
  const parsed = parsePipeline(wire);
  assert.equal(parsed.fleet, null);
  assert.equal('businessCirculation' in parsed, false);
});

const requiredSloCheckIds = [
  'capture_freshness',
  'capture_state',
  'capture_errors',
  'checkpoint_lag',
  's3_freshness',
  's3_requests',
  'snowpipe_queue',
  'canonical_freshness',
  'delivery_latency_p95',
  'delivery_latency_p99',
  'observability_freshness',
  'reconciliation',
  'snowflake_credits',
] as const;

export function validObservabilityFixture(): Record<string, unknown> {
  const checks = requiredSloCheckIds.map((id) => ({
    id,
    stage: id === 'snowpipe_queue' ? 'load' : id === 'observability_freshness' ? 'observability' : id.startsWith('capture_') ? 'capture' : 'destination',
    status: id === 'snowpipe_queue' ? 'breach' : 'pass',
    observed: id === 'capture_state' ? 'RUNNING' : id === 'snowpipe_queue' ? 11 : 1,
    threshold: id === 'capture_state' ? ['RUNNING', 'STOPPED_BUDGET'] : id === 'snowpipe_queue' ? 10 : 5,
    unit: id === 'capture_state' ? null : 'events',
    reason: id === 'snowpipe_queue' ? 'threshold_exceeded' : 'within_threshold',
  }));
  return {
    status: 'breach',
    quality: { coverage: 'complete', freshness: 'fresh', evidence_kind: 'live' },
    observed_at: '2026-08-28T09:59:00+00:00',
    reason: 'threshold_breach',
    checks,
    alerts: [
      {
        fingerprint: `sha256:${'a'.repeat(64)}`,
        check_id: 'snowpipe_queue',
        stage: 'load',
        lifecycle_state: 'firing',
        signal_status: 'breach',
        severity: 'critical',
        reason: 'threshold_exceeded',
        observed: 11,
        threshold: 10,
        unit: 'events',
        first_fired_at: '2026-08-28T09:55:00+00:00',
        firing_since: '2026-08-28T09:55:00+00:00',
        last_observed_at: '2026-08-28T09:59:00+00:00',
        resolved_at: null,
        occurrence_count: 1,
        evaluation_count: 2,
      },
    ],
  };
}

function healthyOverviewFixture(): { pipelines: Array<Record<string, unknown>> } {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  const pipeline = overview.pipelines[0]!;
  pipeline.status = 'healthy';
  pipeline.quality = { coverage: 'complete', freshness: 'fresh', evidence_kind: 'live' };
  pipeline.stages = (pipeline.stages as Array<Record<string, unknown>>).map((stage) => ({
    ...stage,
    status: 'healthy',
  }));
  return overview;
}

function microsecondOverviewFixture(): unknown {
  const overview = validOverviewFixture() as { generated_at: string; pipelines: Array<Record<string, unknown>> };
  const timestamp = '2026-08-28T08:33:56.470000+00:00';
  overview.generated_at = timestamp;
  overview.pipelines[0]!.observed_at = timestamp;
  overview.pipelines[0]!.stages = (overview.pipelines[0]!.stages as Array<Record<string, unknown>>).map((stage) => ({
    ...stage,
    observed_at: timestamp,
  }));
  return overview;
}

test('a destination-unobserved pipeline stays degraded', () => {
  const overview = parseOverview(validOverviewFixture());
  assert.deepEqual(overview.scope, { kind: 'single', environments: ['dev'] });
  assert.equal(overview.pipelines[0]?.status, 'degraded');
  assert.equal(overview.pipelines[0]?.quality.coverage, 'partial');
});

test('observability is parsed into a complete breach and alert lifecycle', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.observability = validObservabilityFixture();

  const parsed = parseOverview(overview).pipelines[0]!.observability;

  assert.equal(parsed.status, 'breach');
  assert.deepEqual(parsed.quality, { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' });
  assert.equal(parsed.checks.length, 13);
  assert.equal(parsed.checks.find((check) => check.id === 'snowpipe_queue')?.status, 'breach');
  assert.equal(parsed.alerts[0]?.lifecycleState, 'firing');
  assert.equal(parsed.alerts[0]?.lastObservedAt, '2026-08-28T09:59:00+00:00');
});

test('a legacy snapshot without observability is explicit and never green', () => {
  const parsed = parseOverview(validOverviewFixture()).pipelines[0]!.observability;

  assert.deepEqual(parsed, {
    status: 'unavailable',
    quality: { coverage: 'none', freshness: 'unavailable', evidenceKind: 'live' },
    observedAt: null,
    reason: 'observability_not_attached',
    checks: [],
    alerts: [],
  });
});

test('observability contradictions and unsafe values fail closed', () => {
  const mutations: Array<(observability: Record<string, any>) => void> = [
    (observability) => { observability.status = 'pass'; },
    (observability) => { observability.checks.pop(); },
    (observability) => { observability.checks[1].id = observability.checks[0].id; },
    (observability) => { observability.alerts[0].fingerprint = 'sha256:operator-secret'; },
    (observability) => { observability.alerts[0].signal_status = 'pass'; },
    (observability) => { observability.alerts[0].resolved_at = '2026-08-28T09:59:00+00:00'; },
    (observability) => { observability.checks[0].observed = Number.NaN; },
    (observability) => { observability.checks[0].reason = 'jdbc://operator:secret@source'; },
  ];

  for (const mutate of mutations) {
    const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
    const observability = structuredClone(validObservabilityFixture()) as Record<string, any>;
    mutate(observability);
    overview.pipelines[0]!.observability = observability;
    assert.throws(() => parseOverview(overview), ControlPlaneParseError);
  }
});

test('resolved SLO alerts preserve a monotonic public history', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  const observability = validObservabilityFixture() as Record<string, any>;
  observability.status = 'pass';
  observability.reason = 'within_policy';
  const check = observability.checks.find((candidate: Record<string, unknown>) => candidate.id === 'snowpipe_queue');
  check.status = 'pass';
  check.observed = 1;
  check.reason = 'within_threshold';
  const alert = observability.alerts[0];
  alert.lifecycle_state = 'resolved';
  alert.signal_status = 'pass';
  alert.severity = 'none';
  alert.reason = 'within_threshold';
  alert.observed = 1;
  alert.resolved_at = alert.last_observed_at;
  overview.pipelines[0]!.observability = observability;

  const parsed = parseOverview(overview).pipelines[0]!.observability;

  assert.equal(parsed.status, 'pass');
  assert.equal(parsed.alerts[0]?.lifecycleState, 'resolved');
  assert.equal(parsed.alerts[0]?.resolvedAt, parsed.alerts[0]?.lastObservedAt);
});

test('resolved SLO history survives a later policy threshold change', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  const observability = validObservabilityFixture() as Record<string, any>;
  observability.status = 'pass';
  observability.reason = 'within_policy';
  const check = observability.checks.find((candidate: Record<string, unknown>) => candidate.id === 'snowpipe_queue');
  check.status = 'pass';
  check.observed = 0;
  check.threshold = 0;
  check.reason = 'within_threshold';
  const alert = observability.alerts[0];
  alert.lifecycle_state = 'resolved';
  alert.signal_status = 'pass';
  alert.severity = 'none';
  alert.reason = 'within_threshold';
  alert.observed = 1;
  alert.threshold = 10;
  alert.resolved_at = alert.last_observed_at;
  overview.pipelines[0]!.observability = observability;

  const parsed = parseOverview(overview).pipelines[0]!.observability;

  assert.equal(parsed.status, 'pass');
  assert.equal(parsed.checks.find((candidate) => candidate.id === 'snowpipe_queue')?.threshold, 0);
  assert.equal(parsed.alerts[0]?.threshold, 10);
});

test('overview scope fails closed when absent, contradictory or malformed', () => {
  const cases: unknown[] = [
    { ...(validOverviewFixture() as Record<string, unknown>), scope: undefined },
    { ...(validOverviewFixture() as Record<string, unknown>), scope: { kind: 'single', environments: [] } },
    { ...(validOverviewFixture() as Record<string, unknown>), scope: { kind: 'single', environments: ['dev', 'prod'] } },
    { ...(validOverviewFixture() as Record<string, unknown>), scope: { kind: 'mixed', environments: ['dev'] } },
    { ...(validOverviewFixture() as Record<string, unknown>), scope: { kind: 'unavailable', environments: ['dev'] } },
    { ...(validOverviewFixture() as Record<string, unknown>), scope: { kind: 'invented', environments: [] } },
    { ...(validOverviewFixture() as Record<string, unknown>), scope: { kind: 'mixed', environments: ['dev', 'dev'] } },
  ];

  for (const value of cases) assert.throws(() => parseOverview(value), ControlPlaneParseError);
});

test('overview scope must equal the normalized union of pipeline and source environments', () => {
  const contradiction = validOverviewFixture() as {
    scope: { kind: string; environments: string[] };
    pipelines: Array<Record<string, unknown>>;
    sources: Array<Record<string, unknown>>;
  };
  contradiction.scope = { kind: 'single', environments: ['prod'] };
  contradiction.pipelines[0]!.environment = 'local';
  contradiction.sources[0]!.environment = 'local';
  assert.throws(() => parseOverview(contradiction), ControlPlaneParseError);

  const normalizedUnion = validOverviewFixture() as {
    scope: { kind: string; environments: string[] };
    sources: Array<Record<string, unknown>>;
  };
  normalizedUnion.scope = { kind: 'mixed', environments: ['dev', 'prod'] };
  normalizedUnion.sources.push({ id: 'prod-cntr', evidence_kind: 'live', environment: 'prod', status: 'unavailable', error: 'offline' });

  assert.deepEqual(parseOverview(normalizedUnion).scope, { kind: 'mixed', environments: ['dev', 'prod'] });
});

test('environment case and whitespace mismatches fail closed before display', () => {
  const mutations: Array<(overview: {
    scope: { kind: string; environments: string[] };
    pipelines: Array<Record<string, unknown>>;
    sources: Array<Record<string, unknown>>;
  }) => void> = [
    (overview) => { overview.scope.environments = ['DEV']; },
    (overview) => { overview.scope.environments = [' dev']; },
    (overview) => { overview.pipelines[0]!.environment = 'Dev'; },
    (overview) => { overview.sources[0]!.environment = 'dev '; },
  ];

  for (const mutate of mutations) {
    const overview = validOverviewFixture() as {
      scope: { kind: string; environments: string[] };
      pipelines: Array<Record<string, unknown>>;
      sources: Array<Record<string, unknown>>;
    };
    mutate(overview);
    assert.throws(() => parseOverview(overview), ControlPlaneParseError);
  }
});

test('gaps stay gaps in a live series', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.lag_series = {
    resolution_s: 5,
    sample_count: 10,
    unknown_sample_count: 2,
    buckets: [
      {
        start_s: 0,
        end_s: 4,
        min: 8,
        max: 12,
        last: 9,
        samples: 5,
        unknown_samples: 0,
        coverage: 'complete',
      },
      {
        start_s: 5,
        end_s: 9,
        min: 7,
        max: 10,
        last: null,
        samples: 5,
        unknown_samples: 2,
        coverage: 'gap',
      },
    ],
  };

  const pipeline = parsePipeline(overview.pipelines[0]);

  assert.equal(pipeline.lagSeries[0]?.lag, 9);
  assert.equal(pipeline.lagSeries[1]?.lag, null);
  assert.equal(pipeline.lagSeries[1]?.coverage, 'gap');
  assert.equal(pipeline.lagSeries[1]?.unknownSamples, 2);
  assert.equal(pipeline.lagSeriesResolutionSeconds, 5);
  assert.equal(pipeline.lagSampleCount, 10);
  assert.equal(pipeline.lagUnknownSampleCount, 2);
});

test('lag series rejects inconsistent unknown sample counts', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.lag_series = {
    resolution_s: 5,
    sample_count: 1,
    unknown_sample_count: 2,
    buckets: [],
  };

  assert.throws(() => parsePipeline(overview.pipelines[0]), ControlPlaneParseError);
});

test('a missing bucket interval becomes explicit gap metadata', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.lag_series = {
    resolution_s: 5,
    sample_count: 10,
    unknown_sample_count: 0,
    buckets: [
      { start_s: 0, end_s: 4, min: 8, max: 12, last: 9, samples: 5, unknown_samples: 0, coverage: 'complete' },
      { start_s: 20, end_s: 24, min: 4, max: 6, last: 4, samples: 5, unknown_samples: 0, coverage: 'complete' },
    ],
  };

  const pipeline = parsePipeline(overview.pipelines[0]);

  assert.equal(pipeline.lagSeries.length, 3);
  assert.deepEqual(pipeline.lagSeries[1], {
    startSeconds: 4,
    endSeconds: 20,
    low: null,
    high: null,
    lag: null,
    samples: 0,
    unknownSamples: 0,
    coverage: 'gap',
    kind: 'temporal_gap',
  });
});

test('a declared gap starts where coverage really ended', () => {
  // Cas réel : un seau fusionné déborde sa cellule nominale. La borne
  // attendue n'est pas start+résolution mais la fin réelle du seau
  // précédent — sinon le trou chevauche des échantillons observés.
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.lag_series = {
    resolution_s: 320,
    sample_count: 360,
    unknown_sample_count: 0,
    buckets: [
      { start_s: 10, end_s: 338.22, min: 1, max: 3, last: 1, samples: 178, unknown_samples: 0, coverage: 'complete', kind: 'observed' },
      { start_s: 338.22, end_s: 340, min: null, max: null, last: null, samples: 0, unknown_samples: 0, coverage: 'gap', kind: 'temporal_gap' },
      { start_s: 340, end_s: 659.41, min: 1, max: 2, last: 1, samples: 182, unknown_samples: 0, coverage: 'complete', kind: 'observed' },
    ],
  };

  const pipeline = parsePipeline(overview.pipelines[0]);

  assert.equal(pipeline.lagSeries.length, 3);
  assert.equal(pipeline.lagSeries[1]?.startSeconds, 338.22);
  assert.equal(pipeline.lagSeries[1]?.kind, 'temporal_gap');
});

test('a spillover bucket without declared gap synthesizes the real hole', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.lag_series = {
    resolution_s: 320,
    sample_count: 360,
    unknown_sample_count: 0,
    buckets: [
      { start_s: 10, end_s: 338.22, min: 1, max: 3, last: 1, samples: 178, unknown_samples: 0, coverage: 'complete' },
      { start_s: 340, end_s: 659.41, min: 1, max: 2, last: 1, samples: 182, unknown_samples: 0, coverage: 'complete' },
    ],
  };

  const pipeline = parsePipeline(overview.pipelines[0]);

  assert.equal(pipeline.lagSeries.length, 3);
  assert.equal(pipeline.lagSeries[1]?.startSeconds, 338.22);
  assert.equal(pipeline.lagSeries[1]?.endSeconds, 340);
  assert.equal(pipeline.lagSeries[1]?.kind, 'temporal_gap');
});

test('pipeline stages must follow the canonical topology order', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  const stages = overview.pipelines[0]!.stages as Array<Record<string, unknown>>;
  [stages[0], stages[1]] = [stages[1]!, stages[0]!];

  assert.throws(() => parsePipeline(overview.pipelines[0]), ControlPlaneParseError);
});

test('microsecond UTC timestamps from the control plane are accepted end to end', () => {
  const overview = parseOverview(microsecondOverviewFixture());

  assert.equal(overview.generatedAt, '2026-08-28T08:33:56.470000+00:00');
  assert.equal(overview.pipelines[0]?.observedAt, '2026-08-28T08:33:56.470000+00:00');
  assert.equal(overview.pipelines[0]?.stages[0]?.observedAt, '2026-08-28T08:33:56.470000+00:00');
});

test('a backend-generated UTC microsecond overview stays parseable in the frontend', () => {
  const repoRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const script = String.raw`
from datetime import datetime, timezone
import json

import site_fixture  # noqa: F401 — installe les QUADRINGENT_* du site fictif acme
from quadringent_control_plane.model import SourceDescriptor
from quadringent_control_plane.projection import build_overview, project_console_document

document = {
    "format_version": "as400-console-v1",
    "generated_at": "2026-08-28T08:33:56.470000+00:00",
    "flux": {"id": "dev-cntr", "label": "CNTR"},
    "run": {"state": "RUNNING", "last_error": None},
    "position": {
        "checkpoint": {"receiver": "TRNJRN3776", "sequence": 41},
        "source_tail": {"receiver": "TRNJRN3776", "sequence": 42},
    },
    "lag": {"current": {"value": 1}, "verdict": {"value": "STABLE"}},
    "counters": {"events_published": {"value": 120}, "errors": {"value": 0}},
}

projection = project_console_document(
    document,
    SourceDescriptor("dev-cntr", "live", "dev", "file:///snapshot.json"),
    now=datetime(2026, 8, 28, 8, 34, 0, tzinfo=timezone.utc),
)
overview = build_overview(
    (projection,),
    revision=7,
    generated_at=datetime(2026, 8, 28, 8, 33, 56, 470000, tzinfo=timezone.utc),
)
overview["sources"] = [
    {"id": "dev-cntr", "evidence_kind": "live", "environment": "dev", "status": "available", "error": None},
]
print(json.dumps(overview))
`;
  const output = execFileSync('python3', ['-c', script], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: 'src:tests' },
    encoding: 'utf8',
  }).trim();

  const overview = parseOverview(JSON.parse(output));

  assert.equal(overview.generatedAt, '2026-08-28T08:33:56.470000+00:00');
  assert.equal(overview.pipelines[0]?.observedAt, '2026-08-28T08:33:56.470000+00:00');
  assert.equal(overview.pipelines[0]?.stages[0]?.observedAt, '2026-08-28T08:33:56.470000+00:00');
});

test('invalid timestamps and unknown statuses fail closed', () => {
  assert.throws(
    () => parseOverview({ ...(validOverviewFixture() as Record<string, unknown>), generated_at: 'later' }),
    ControlPlaneParseError,
  );
  const invalid = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  invalid.pipelines[0]!.status = 'running';
  assert.throws(() => parseOverview(invalid), ControlPlaneParseError);
  assert.throws(
    () => parseOverview({ ...(validOverviewFixture() as Record<string, unknown>), generated_at: '2026-02-30' }),
    ControlPlaneParseError,
  );
  assert.throws(
    () => parseOverview({ ...(validOverviewFixture() as Record<string, unknown>), generated_at: '2026-08-28T24:00:00+00:00' }),
    ControlPlaneParseError,
  );
});

test('invalid quality, stage, incident and counter values fail closed', () => {
  const cases: Array<(overview: { pipelines: Array<Record<string, unknown>> }) => void> = [
    (overview) => { overview.pipelines[0]!.quality = { coverage: 'complete', freshness: 'fresh', evidence_kind: 'invented' }; },
    (overview) => { (overview.pipelines[0]!.stages as Array<Record<string, unknown>>)[0]!.id = 'worker'; },
    (overview) => { overview.pipelines[0]!.incident = { code: '', type: 'capture_timeout' }; },
    (overview) => { (overview.pipelines[0]!.counters as Record<string, unknown>).errors = Number.NaN; },
  ];
  for (const mutate of cases) {
    const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
    mutate(overview);
    assert.throws(() => parseOverview(overview), ControlPlaneParseError);
  }
});

test('an incident code carrying a sensitive payload is rejected', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.incident = {
    code: 'capture_stopped_fail_closed:jdbc://operator:secret@source',
    type: 'capture_connection_failure',
  };

  assert.throws(() => parseOverview(overview), ControlPlaneParseError);
});

test('a destination incident emitted by the backend contract is accepted from its public allowlist', () => {
  const overview = validOverviewFixture() as { pipelines: Array<Record<string, unknown>> };
  overview.pipelines[0]!.incident = {
    code: 'destination_reconciliation_mismatch',
    type: 'destination',
  };

  const parsed = parseOverview(overview);

  assert.deepEqual(parsed.pipelines[0]!.incident, {
    code: 'destination_reconciliation_mismatch',
    type: 'destination',
  });
});

test('healthy pipeline rejects stale freshness and historical evidence', () => {
  const overview = healthyOverviewFixture();
  overview.pipelines[0]!.quality = {
    coverage: 'complete',
    freshness: 'stale',
    evidence_kind: 'historical',
  };

  assert.throws(() => parseOverview(overview), ControlPlaneParseError);
});

test('healthy pipeline rejects any non-healthy stage', () => {
  const overview = healthyOverviewFixture();
  (overview.pipelines[0]!.stages as Array<Record<string, unknown>>)[1]!.status = 'unknown';

  assert.throws(() => parseOverview(overview), ControlPlaneParseError);
});

/* ------------------------------------------------------------------ */
/* Extension S5 — blocs top-level pass-through, jamais un 6e stage      */
/* ------------------------------------------------------------------ */

function s5Wire(): Record<string, unknown> {
  return {
    position: {
      checkpoint: { receiver: 'TRNJRN3776', sequence: 41 },
      source_tail: { receiver: 'TRNJRN3776', sequence: 1337 },
      receiver_first_sequence: 40,
      receiver_last_sequence: 1337,
    },
    flux: {
      id: 'dev-cntr',
      label: 'CNTR',
      journal: 'QJRN',
      journal_library: 'ACME_LIB',
      objects: ['CNTR', 'CLIENTS'],
      reader_path: 'journal',
      target: 'snowflake',
      job: 'CDCREADER1',
    },
    run: {
      state: 'RUNNING',
      started_at: '2026-08-28T08:00:00+00:00',
      elapsed_s: 3599.5,
      stopped_because: null,
      diagnostic: { type: 'budget', head: 'quota atteint', at: '2026-08-28T08:30:00+00:00' },
      source_pause: { retry_after: '2026-08-28T09:00:00+00:00', reason_code: 'journal_wrap' },
    },
    lag_verdict: 'BOUNDED',
    lag_verdict_reason: null,
    destination: {
      kind: 'snowflake',
      database: 'ACME_RAW',
      schema: 'DEV',
      stage: 'QS_STAGE',
      raw_table: 'CNTR_RAW',
      canonical_table: 'CNTR',
      run_tag: 'run-41',
      observed_at: '2026-08-28T09:58:00+00:00',
      load_checkpoint: { receiver: 'snowpipe', sequence: 900 },
      apply_checkpoint: { receiver: 'snowpipe', sequence: 901 },
      source_events: 120,
      raw_rows: 120,
      canonical_rows: 118,
      duplicates: 2,
    },
    destination_reason: null,
  };
}

test('the S5 extension stays absent — undefined, never invented', () => {
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  const parsed = parsePipeline(wire);

  assert.equal(parsed.position, undefined);
  assert.equal(parsed.flux, undefined);
  assert.equal(parsed.run, undefined);
  assert.equal(parsed.lagVerdict, undefined);
  assert.equal(parsed.lagVerdictReason, undefined);
  assert.equal(parsed.destination, undefined);
  assert.equal(parsed.destinationReason, undefined);
});

test('a fully served S5 projection parses with snake_case to camelCase mapping', () => {
  const wire = { ...(validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!, ...s5Wire() };
  const parsed = parsePipeline(wire);

  assert.deepEqual(parsed.position, {
    checkpoint: { receiver: 'TRNJRN3776', sequence: 41 },
    sourceTail: { receiver: 'TRNJRN3776', sequence: 1337 },
    receiverFirstSequence: 40,
    receiverLastSequence: 1337,
  });
  assert.deepEqual(parsed.flux, {
    id: 'dev-cntr',
    label: 'CNTR',
    journal: 'QJRN',
    journalLibrary: 'ACME_LIB',
    objects: ['CNTR', 'CLIENTS'],
    readerPath: 'journal',
    target: 'snowflake',
    job: 'CDCREADER1',
  });
  assert.deepEqual(parsed.run, {
    state: 'RUNNING',
    startedAt: '2026-08-28T08:00:00+00:00',
    elapsedSeconds: 3599.5,
    stoppedBecause: null,
    diagnostic: { type: 'budget', head: 'quota atteint', at: '2026-08-28T08:30:00+00:00' },
    sourcePause: { retryAfter: '2026-08-28T09:00:00+00:00', reasonCode: 'journal_wrap' },
  });
  assert.equal(parsed.lagVerdict, 'BOUNDED');
  assert.equal(parsed.lagVerdictReason, null);
  assert.deepEqual(parsed.destination, {
    kind: 'snowflake',
    database: 'ACME_RAW',
    schema: 'DEV',
    stage: 'QS_STAGE',
    rawTable: 'CNTR_RAW',
    canonicalTable: 'CNTR',
    runTag: 'run-41',
    observedAt: '2026-08-28T09:58:00+00:00',
    loadCheckpoint: { receiver: 'snowpipe', sequence: 900 },
    applyCheckpoint: { receiver: 'snowpipe', sequence: 901 },
    sourceEvents: 120,
    rawRows: 120,
    canonicalRows: 118,
    duplicates: 2,
  });
  assert.equal(parsed.destinationReason, null);
});

test('null S5 blocks stay null — a served null is not dropped back to absent', () => {
  const cases: ReadonlyArray<readonly [string, string]> = [
    ['position', 'position'],
    ['flux', 'flux'],
    ['run', 'run'],
    ['destination', 'destination'],
    ['lag_verdict', 'lagVerdict'],
    ['lag_verdict_reason', 'lagVerdictReason'],
    ['destination_reason', 'destinationReason'],
  ];
  for (const [snake, camel] of cases) {
    const wire = { ...(validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!, [snake]: null };
    const parsed = parsePipeline(wire) as Record<string, unknown>;
    assert.equal(parsed[camel], null, `${snake} doit rester null`);
  }
  // un texte déclaré passe tel quel
  const wire = { ...(validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!,
    lag_verdict: null, lag_verdict_reason: 'verdict_not_declared' };
  const parsed = parsePipeline(wire);
  assert.equal(parsed.lagVerdict, null);
  assert.equal(parsed.lagVerdictReason, 'verdict_not_declared');
});

test('malformed S5 members fail to null without failing the pipeline', () => {
  const wire = { ...(validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]! };
  wire.position = {
    checkpoint: 'garbage',
    source_tail: { receiver: 'TRNJRN3776', sequence: -1 },
    receiver_first_sequence: 'quarante',
    receiver_last_sequence: 5,
  };
  wire.flux = { objects: ['CNTR', 42], job: 7 };
  wire.run = {
    state: 42,
    elapsed_s: -3,
    diagnostic: 'oops',
    source_pause: { retry_after: 'bientôt', reason_code: 'wrap' },
  };
  wire.destination = {
    observed_at: 'hier',
    raw_rows: 3.14,
    duplicates: -1,
    apply_checkpoint: { receiver: 's', sequence: 'neuf-cent-un' },
  };

  const parsed = parsePipeline(wire);

  // checkpoint non-objet → null ; séquence négative → checkpoint null ; texte → null ; entier sain conservé
  assert.equal(parsed.position?.checkpoint, null);
  assert.equal(parsed.position?.sourceTail, null);
  assert.equal(parsed.position?.receiverFirstSequence, null);
  assert.equal(parsed.position?.receiverLastSequence, 5);
  // un item non-texte invalide toute la liste ; un membre non-texte devient null
  assert.equal(parsed.flux?.objects, null);
  assert.equal(parsed.flux?.job, null);
  // durée négative → null ; diagnostic non-objet → null ; timestamp invalide → null
  assert.equal(parsed.run?.state, null);
  assert.equal(parsed.run?.elapsedSeconds, null);
  assert.equal(parsed.run?.diagnostic, null);
  assert.deepEqual(parsed.run?.sourcePause, { retryAfter: null, reasonCode: 'wrap' });
  // timestamp invalide → null ; flottant → null ; négatif → null ; checkpoint incomplet → null
  assert.equal(parsed.destination?.observedAt, null);
  assert.equal(parsed.destination?.rawRows, null);
  assert.equal(parsed.destination?.duplicates, null);
  assert.equal(parsed.destination?.applyCheckpoint, null);
  // le pipeline lui-même reste parsé — aucun bloc S5 ne casse le contrat v1
  assert.equal(parsed.status, 'degraded');
  assert.equal(parsed.stages.length, 5);
});

test('a non-object S5 block fails to null — never a partial projection', () => {
  for (const [key, value] of [['position', []], ['flux', 'oops'], ['run', 42], ['destination', true]] as const) {
    const wire = { ...(validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!, [key]: value };
    const parsed = parsePipeline(wire) as Record<string, unknown>;
    assert.equal(parsed[key], null, `${key} non-objet → null`);
  }
});

test('fleet plan provenance is validated strictly — wrong catalog metadata rejects the plan', () => {
  const repoRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const output = execFileSync('python3', ['-c', String.raw`
import json
import site_fixture  # noqa: F401 — installe les QUADRINGENT_* du site fictif acme
from quadringent_control_plane.fleet_sidecar import generate_fleet_ui_sidecar
from test_fleet_plan import catalog_payload
print(json.dumps(generate_fleet_ui_sidecar(catalog_payload())["fleet"]))
`], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: 'src:tests' },
    encoding: 'utf8',
  });
  const plan = JSON.parse(output) as Record<string, any>;

  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.fleet_plan = plan;
  const good = parsePipeline(wire);
  assert.deepEqual(good.fleetPlan?.provenance, {
    kind: 'metadata_catalog',
    catalogFormat: 'quadringent-fleet-catalog-v1',
    planFormat: 'quadringent-fleet-plan-v1',
    observedAt: plan.observed_at,
  });

  for (const patch of [
    { kind: 'operator_note' },
    { catalog_format: 'autre-format' },
    { plan_format: 'autre-format' },
    { observed_at: '2026-01-01T00:00:00+00:00' },
  ]) {
    const broken = structuredClone(plan);
    broken.provenance = { ...broken.provenance, ...patch };
    const brokenWire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
    brokenWire.fleet_plan = broken;
    assert.throws(() => parsePipeline(brokenWire), ControlPlaneParseError, `provenance ${JSON.stringify(patch)}`);
  }
});

test('awaiting_resume parses as a first-class pipeline and stage status', () => {
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.status = 'awaiting_resume';
  wire.stages = (wire.stages as Array<Record<string, unknown>>).map((stage) =>
    stage.id === 'capture' ? { ...stage, status: 'awaiting_resume' } : stage,
  );
  const pipeline = parsePipeline(wire);
  assert.equal(pipeline.status, 'awaiting_resume');
  assert.equal(pipeline.stages.find((stage) => stage.id === 'capture')?.status, 'awaiting_resume');
});

test('incident cause_resolved fields parse and stay absent otherwise', () => {
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.incident = { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' };
  const plain = parsePipeline(wire);
  assert.equal(plain.incident?.causeResolved, undefined);
  wire.incident = {
    code: 'capture_stopped_fail_closed',
    type: 'capture_auth_blocked',
    cause_resolved: true,
    cause_resolved_observed_at: '2026-09-21T14:07:49Z',
  };
  const resolved = parsePipeline(wire);
  assert.equal(resolved.incident?.causeResolved, true);
  assert.equal(resolved.incident?.causeResolvedObservedAt, '2026-09-21T14:07:49Z');
});

test('resume block parses readiness, positions and measured backlog', () => {
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.resume = {
    state: 'ready',
    auth_observed_at: '2026-09-21T14:07:49Z',
    checkpoint: { receiver: 'DEMOJRN4114', sequence: 345089832 },
    tail: { receiver: 'DEMOJRN4158', sequence: 120332519 },
    backlog_sequences: 77777,
    backlog_receivers: 44,
  };
  const resume = parsePipeline(wire).resume;
  assert.equal(resume?.state, 'ready');
  assert.equal(resume?.authObservedAt, '2026-09-21T14:07:49Z');
  assert.deepEqual(resume?.checkpoint, { receiver: 'DEMOJRN4114', sequence: 345089832 });
  assert.deepEqual(resume?.tail, { receiver: 'DEMOJRN4158', sequence: 120332519 });
  assert.equal(resume?.backlogSequences, 77777);
  assert.equal(resume?.backlogReceivers, 44);
});

test('resume block stays tolerant: unknown state and missing members degrade, not fail', () => {
  const wire = (validOverviewFixture() as { pipelines: Array<Record<string, unknown>> }).pipelines[0]!;
  wire.resume = { state: 'invented' };
  const resume = parsePipeline(wire).resume;
  assert.equal(resume?.state, 'unavailable');
  assert.equal(resume?.checkpoint, null);
  assert.equal(resume?.backlogSequences, null);
  delete wire.resume;
  assert.equal(parsePipeline(wire).resume, undefined);
});
