#!/usr/bin/env node
/**
 * Control plane de revue visuelle — répond au contrat /v1 avec un relevé
 * réaliste (13 tables, phases variées, trou de série). Aucune valeur n'est une
 * mesure : ce serveur sert uniquement à inspecter le rendu à 1440 px.
 *
 *   node scripts/dev-control-plane.mjs   → http://127.0.0.1:8844
 *   npm run dev                          → proxy /v1 → 8844
 */
import { createServer } from 'node:http';

const PORT = Number(process.env.DEV_CONTROL_PLANE_PORT || 8844);

const SITE = {
  site_id: 'acme',
  fleet_id: 'acme-test',
  environment: 'TEST',
  runtime_environment: 'test',
  destination_database: 'ACME_RAW',
  destination_schema: 'IBMI_TEST',
  destination_namespace: 'ACME_RAW.IBMI_TEST',
  source_schema: 'LEDGER',
  journal_name: 'TRNJRN',
  tables: [
    'ADDRS1', 'CAL001', 'COST1', 'CUSTOM1', 'ORDER', 'EXPENS', 'DATE01',
    'SALE', 'PLACE01', 'PLACES', 'CNTR', 'PRODUCT', 'HOLIDAYS',
  ],
  proof_table: 'SALE',
  runtime_pipeline_id: 'acme',
  ibmi_host: 'ibmi.acme.invalid',
  ibmi_user: 'CDCAPP',
  tls_ca_file: '/app/certs/ibmi-test-ca.pem',
  secret_ref_name: 'acme-test-ibmi',
  secret_ref_key: 'ISERIES_PASSWORD',
  snowflake_stage: 'IBMI_TEST_SALE_EXTERNAL_STAGE',
};

const MANIFEST = SITE.tables;

/* États de table choisis pour faire voir tous les cas du plateau : certifiées,
 * live en cours, historique, en pause, prête non démarrée, bloquée journal. */
const TABLE_STATES = {
  ADDRS1:    { phase: 'CERTIFIED',  copied: 218_150_575, total: 218_150_575 },
  CAL001:    { phase: 'CERTIFIED',  copied: 4_500,       total: 4_500 },
  COST1:     { phase: 'LIVE',       copied: 1_842_300,   total: 3_200_000 },
  CUSTOM1:   { phase: 'BLOCKED',    copied: null,        total: null },
  ORDER:     { phase: 'LIVE',       copied: 95_300,      total: 240_000 },
  EXPENS:    { phase: 'LIVE',       copied: 1_200_000,   total: 1_200_000 },
  DATE01:    { phase: 'HISTORICAL', copied: 45_000,      total: 45_000 },
  SALE:      { phase: 'LIVE',       copied: 76_500,      total: 180_000 },
  PLACE01:   { phase: 'PAUSED',     copied: 1_200,       total: 9_800 },
  PLACES:     { phase: 'CERTIFIED',  copied: 32_000,      total: 32_000 },
  CNTR:      { phase: 'LIVE',       copied: 240,         total: 240 },
  PRODUCT:   { phase: 'LIVE',       copied: 18_500,      total: 42_000 },
  HOLIDAYS: { phase: 'READY',      copied: null,        total: null },
};

const now = () => new Date().toISOString();
const hoursAgo = (h) => new Date(Date.now() - h * 3_600_000).toISOString();

/** Scénario de revue : `DEV_SCENARIO=stale` sert le plateau dégradé —
 *  authentification AS400 bloquée, relevé figé depuis 37 h, mesure morte. */
const SCENARIO = process.env.DEV_SCENARIO || 'healthy';
const STALE = SCENARIO === 'stale';
const OBSERVED = STALE ? hoursAgo(37) : now();

/** row_count catalogue est toujours servi ; seul le total runtime peut être
 *  null — les deux restent visibles côte à côte dans la grille. */
const PLAN_ROWS = { CUSTOM1: 48_000, HOLIDAYS: 3_600 };

function planTable(name) {
  const state = TABLE_STATES[name];
  const blocked = name === 'CUSTOM1';
  return {
    name,
    row_count: state.total ?? PLAN_ROWS[name],
    data_size: 10,
    journal_images: blocked ? '*BEFORE' : '*BOTH',
    identity_status: blocked ? 'blocked' : 'keyed',
    identity_source: blocked ? null : 'pk',
    candidate_key: blocked ? null : ['ID'],
    live_possible: !blocked,
    certification_possible: !blocked,
    historical_admitted: true,
    historical_lane: (MANIFEST.indexOf(name) % 2) + 1,
    blocked_reasons: blocked ? ['identity_unproven'] : [],
    copied_rows: null,
    copied_bytes: null,
    history_progress: null,
  };
}

const STAGE_STATUS = STALE
  ? { source: 'degraded', capture: 'incident', raw: 'unknown', load: 'unknown', destination: 'unknown' }
  : { source: 'healthy', capture: 'healthy', raw: 'healthy', load: 'healthy', destination: 'healthy' };

const STAGE_HEADLINE = STALE
  ? {
      source: 'Joignable — authentification refusée',
      capture: 'Lecture arrêtée en sécurité',
      raw: 'Non observée depuis l’arrêt',
      load: 'Non observée depuis l’arrêt',
      destination: 'Non observée depuis l’arrêt',
    }
  : {
      source: 'source actif',
      capture: 'capture actif',
      raw: 'raw actif',
      load: 'load actif',
      destination: 'destination actif',
    };

const pipeline = {
  id: 'acme',
  environment: 'test',
  status: STALE ? 'incident' : 'healthy',
  quality: {
    coverage: 'complete',
    freshness: STALE ? 'stale' : 'fresh',
    evidence_kind: 'live',
  },
  summary: STALE
    ? 'Capture arrêtée — authentification refusée par la source'
    : 'Toutes les étapes déclarées observées',
  observed_at: OBSERVED,
  stages: ['source', 'capture', 'raw', 'load', 'destination'].map((id) => ({
    id,
    status: STAGE_STATUS[id],
    observed_at: OBSERVED,
    headline: STAGE_HEADLINE[id],
    detail: STALE ? 'Dernière observation avant la coupure.' : 'Observation reçue.',
  })),
  lag_sequences: 1_247,
  lag_seconds: null,
  lag_series: {
    resolution_s: 30,
    sample_count: 6,
    unknown_sample_count: 1,
    buckets: [
      { start_s: 0, end_s: 30, kind: 'observed', samples: 1, unknown_samples: 0, coverage: 'complete', min: 1_340, max: 1_340, last: 1_340 },
      { start_s: 30, end_s: 60, kind: 'observed', samples: 2, unknown_samples: 1, coverage: 'gap', min: 1_100, max: 1_340, last: null },
      { start_s: 60, end_s: 120, kind: 'temporal_gap', samples: 0, unknown_samples: 0, coverage: 'gap', min: null, max: null, last: null },
      { start_s: 120, end_s: 150, kind: 'observed', samples: 3, unknown_samples: 0, coverage: 'complete', min: 1_150, max: 1_300, last: 1_247 },
    ],
  },
  counters: {
    events_published: 345_000,
    events_in_target: 342_580,
    duplicates_in_target: 0,
    polls: 1_810,
    payload_bytes_published: 96_400_000,
    errors: 0,
    empty_scans: 210,
    idle_polls: 96,
    windows_published: 754,
    receiver_rotations: 2,
    run_duration_s: 1_421,
    mean_mcpu: 30,
    cpu_ms_per_event: 0.5463,
  },
  incident: STALE
    ? { code: 'capture_stopped_fail_closed', type: 'capture_auth_blocked' }
    : null,
  fleet_plan: {
    environment: 'TEST',
    source_schema: 'LEDGER',
    destination_namespace: 'ACME_RAW.IBMI_TEST',
    observed_at: STALE ? hoursAgo(38) : now(),
    provenance: {
      kind: 'metadata_catalog',
      catalog_format: 'quadringent-fleet-catalog-v1',
      plan_format: 'quadringent-fleet-plan-v1',
      observed_at: STALE ? hoursAgo(38) : now(),
    },
    freshness: 'fresh',
    continuity: 'uncertain',
    live_blocked: true,
    certification_blocked: true,
    live_promise: 'blocked',
    certification_promise: 'blocked',
    promise_blockers: ['identity_unproven'],
    history_admitted: true,
    cutover_checkpoint: { receiver: 'TRNJRN3776', sequence: 1 },
    cutover_required_before_history: true,
    journal: {
      library: 'LEDGER', name: 'TRNJRN', reader_kind: 'multi_object', reader_count: 1,
      table_names: [...MANIFEST], continuity: 'uncertain',
    },
    identity: {
      keyed_count: 12, rrn_count: 0, blocked_count: 1,
      keyed: MANIFEST.filter((n) => n !== 'CUSTOM1'), rrn: [], blocked: ['CUSTOM1'],
    },
    observed_totals: { table_count: 13, row_count: 223_155_715, data_size: 130 },
    historical: {
      max_concurrency: 2,
      byte_budget: null,
      admitted_count: 13,
      excluded_count: 0,
      lanes: [
        {
          slot: 1,
          tables: ['ADDRS1', 'COST1', 'ORDER', 'DATE01', 'PLACE01', 'CNTR', 'HOLIDAYS'],
          row_count: 221_649_215,
          data_size: 70,
        },
        {
          slot: 2,
          tables: ['CAL001', 'CUSTOM1', 'EXPENS', 'SALE', 'PLACES', 'PRODUCT'],
          row_count: 1_506_500,
          data_size: 60,
        },
      ],
    },
    cost: { status: 'unknown', observed: null, unknown_because: 'not measured' },
    tables: MANIFEST.map(planTable),
  },
  fleet_runtime: {
    format_version: 'quadringent-fleet-runtime-v1',
    fleet_id: 'acme-test',
    environment: 'test',
    pipeline_id: 'acme',
    phase: 'LIVE',
    checkpoint: { receiver: 'TRNJRN3776', sequence: 345_089_832 },
    capabilities: {
      refresh: { state: 'available', reason: null },
      prepare: { state: 'unavailable', reason: 'operator_access_unavailable' },
      start: { state: 'unavailable', reason: 'operator_access_unavailable' },
      pause: { state: 'unavailable', reason: 'operator_access_unavailable' },
      resume: { state: 'unavailable', reason: 'operator_access_unavailable' },
    },
    table_states: MANIFEST.map((name) => ({
      name,
      phase: TABLE_STATES[name].phase,
      copied_rows: TABLE_STATES[name].copied,
      total_rows: TABLE_STATES[name].total,
    })),
  },
};

const overview = () => ({
  revision: 1,
  generated_at: now(),
  scope: { kind: 'single', environments: ['test'] },
  pipelines: [{ ...pipeline, observed_at: STALE ? OBSERVED : now() }],
  sources: [
    { id: 'acme', evidence_kind: 'live', environment: 'test', status: 'available', error: null },
  ],
});

const json = (res, status, body) => {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    ETag: 'W/"rev-1"',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(payload);
};

const server = createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost');
  if (url.pathname === '/v1/onboarding/defaults') {
    return json(res, 200, {
      defaults: { poll_seconds: 5, batch_entries: 500, tls: true },
      site: SITE,
    });
  }
  if (url.pathname === '/v1/overview') return json(res, 200, overview());
  if (url.pathname === '/v1/pipelines') {
    return json(res, 200, { revision: 1, pipelines: overview().pipelines });
  }
  if (url.pathname === '/v1/pipelines/acme') {
    return json(res, 200, { revision: 1, pipeline });
  }
  if (url.pathname === '/v1/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'Access-Control-Allow-Origin': '*',
    });
    res.write('event: stream.cursor\ndata: {"revision": 1}\n\n');
    const heartbeat = setInterval(() => res.write(': heartbeat\n\n'), 15_000);
    req.on('close', () => clearInterval(heartbeat));
    return;
  }
  json(res, 404, { error: 'not_found' });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`dev control plane → http://127.0.0.1:${PORT}`);
});
