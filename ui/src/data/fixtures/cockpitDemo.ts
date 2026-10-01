/**
 * Mode démo du cockpit v2 — sert des réponses fixes, sans backend, pour le
 * développement local et les tests d'écran. Même principe que
 * `wizardDemo.ts` : `createCockpitDemoFetch` renvoie une fonction
 * compatible avec l'option `fetchFn` de `ControlPlaneV2Client`.
 *
 * Jeu de données : deux connexions (Ventes, Stocks), huit tables au total,
 * un incident (`pl_ventes_lignes` en `attention`), une table en pause
 * (`pl_stocks_mouvements`), une copie initiale en cours
 * (`pl_stocks_articles`), le reste en direct. Séries de métriques 1 h/24 h
 * synthétiques mais déterministes (pas de `Math.random()` : un test qui lit
 * deux fois la même fenêtre doit voir les mêmes points). Coûts couvrant les
 * trois provenances (mesuré/estimé/absent).
 */

import type { PipelineDeclaredState } from '../controlPlaneV2Client.ts';

export const DEMO_SOURCE_VENTES = 'src_demo_ventes';
export const DEMO_SOURCE_STOCKS = 'src_demo_stocks';
export const DEMO_DESTINATION_VENTES = 'dst_demo_ventes';
export const DEMO_DESTINATION_STOCKS = 'dst_demo_stocks';

interface DemoPipeline {
  readonly id: string;
  readonly sourceId: string;
  readonly name: string;
  declaredState: PipelineDeclaredState;
}

const DEMO_PIPELINES: readonly DemoPipeline[] = [
  { id: 'pl_ventes_clients', sourceId: DEMO_SOURCE_VENTES, name: 'CLIENTS', declaredState: 'live' },
  { id: 'pl_ventes_commandes', sourceId: DEMO_SOURCE_VENTES, name: 'COMMANDES', declaredState: 'live' },
  { id: 'pl_ventes_lignes', sourceId: DEMO_SOURCE_VENTES, name: 'LIGNES_CDE', declaredState: 'attention' },
  { id: 'pl_ventes_archives', sourceId: DEMO_SOURCE_VENTES, name: 'ARCHIVES_90J', declaredState: 'live' },
  { id: 'pl_stocks_articles', sourceId: DEMO_SOURCE_STOCKS, name: 'ARTICLES', declaredState: 'copying' },
  { id: 'pl_stocks_mouvements', sourceId: DEMO_SOURCE_STOCKS, name: 'MOUVEMENTS', declaredState: 'paused' },
  { id: 'pl_stocks_entrepots', sourceId: DEMO_SOURCE_STOCKS, name: 'ENTREPOTS', declaredState: 'live' },
  { id: 'pl_stocks_alertes', sourceId: DEMO_SOURCE_STOCKS, name: 'ALERTES_SEUIL', declaredState: 'live' },
];

/** L'incident de démonstration : corrèle les journaux de `pl_ventes_lignes`. */
const DEMO_INCIDENT_ID = 'inc_demo_lignes_retard';

let confirmationSequence = 0;

export function createCockpitDemoFetch(): (input: string, init?: RequestInit) => Promise<Response> {
  const pipelines = DEMO_PIPELINES.map((pipeline) => ({ ...pipeline }));
  const confirmations = new Map<string, { readonly actionRef: string; readonly resourceId: string; state: 'pending' | 'approved' | 'rejected' | 'used' }>();

  return async (input, init) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const [path, search] = input.replace(/^.*\/v2/, '').split('?');
    const query = new URLSearchParams(search ?? '');
    const body = init?.body ? JSON.parse(init.body as string) as Record<string, unknown> : {};

    if (method === 'GET' && path === '/sources') {
      return json(200, {
        items: [
          { id: DEMO_SOURCE_VENTES, display_name: 'Ventes', ibmi_host: 'as400-ventes.demo.local' },
          { id: DEMO_SOURCE_STOCKS, display_name: 'Stocks', ibmi_host: 'as400-stocks.demo.local' },
        ],
        next_cursor: null,
      });
    }

    if (method === 'GET' && path === '/destinations') {
      return json(200, {
        items: [
          { id: DEMO_DESTINATION_VENTES, account_identifier: 'demo-ventes-xy1', verification_state: 'verified', sql_script: '' },
          { id: DEMO_DESTINATION_STOCKS, account_identifier: 'demo-stocks-xy2', verification_state: 'verified', sql_script: '' },
        ],
        next_cursor: null,
      });
    }

    if (method === 'GET' && path === '/pipelines') {
      return json(200, { items: pipelines.map(pipelineListDict), next_cursor: null });
    }

    const pipelineMatch = /^\/pipelines\/([^/]+)$/.exec(path);
    if (method === 'GET' && pipelineMatch) {
      const pipeline = pipelines.find((item) => item.id === pipelineMatch[1]);
      if (!pipeline) return notFound(path);
      return json(200, pipelineDict(pipeline));
    }

    const actionMatch = /^\/pipelines\/([^/]+)\/actions\/([^/]+)$/.exec(path);
    if (method === 'POST' && actionMatch) {
      const [, pipelineId, action] = actionMatch;
      const pipeline = pipelines.find((item) => item.id === pipelineId);
      if (!pipeline) return notFound(path);
      const dryRun = body.dry_run === true;
      const sensitive = action === 'remove' || action === 'restart_initial_copy' || action === 'replay';

      if (dryRun) {
        return json(200, envelope(null, null, `/pipelines/${pipelineId}`, { action, effect: demoActionEffect(action) }));
      }

      if (sensitive) {
        const token = typeof body.confirmation_token === 'string' ? body.confirmation_token : null;
        const confirmation = token ? confirmations.get(token) : null;
        if (!confirmation || confirmation.state !== 'approved' || confirmation.resourceId !== pipelineId || confirmation.actionRef !== `pipeline.${action}`) {
          confirmationSequence += 1;
          const confirmationId = `cf_demo_${confirmationSequence}`;
          confirmations.set(confirmationId, { actionRef: `pipeline.${action}`, resourceId: pipelineId, state: 'pending' });
          return json(409, {
            error: {
              code: 'pending_confirmation_required',
              message: `confirmation requise (id=${confirmationId}) — voir /v2/confirmations/${confirmationId}`,
              next_action: 'Approuvez la confirmation.',
              retryable: false,
            },
          });
        }
      }

      const before = pipelineDict(pipeline);
      pipeline.declaredState = nextDeclaredState(action as PipelineActionKind, pipeline.declaredState);
      if (sensitive) confirmations.get(body.confirmation_token as string)!.state = 'used';
      return json(200, envelope(before, pipelineDict(pipeline), `/pipelines/${pipelineId}`, null));
    }

    const levelActionMatch = /^\/(sources|destinations)\/([^/]+)\/actions\/(pause|resume)$/.exec(path);
    if (method === 'POST' && levelActionMatch) {
      const [, kind, id, action] = levelActionMatch;
      const sourceId = kind === 'sources' ? id : sourceIdForDestination(id);
      const affected = pipelines.filter((pipeline) => pipeline.sourceId === sourceId);
      for (const pipeline of affected) pipeline.declaredState = action === 'pause' ? 'paused' : 'live';
      return json(200, envelope(null, { affected: affected.map((item) => item.id) }, `/${kind}/${id}`, null));
    }

    const fleetActionMatch = /^\/actions\/(pause_all|resume_all)$/.exec(path);
    if (method === 'POST' && fleetActionMatch) {
      for (const pipeline of pipelines) pipeline.declaredState = fleetActionMatch[1] === 'pause_all' ? 'paused' : 'live';
      return json(200, envelope(null, { affected: pipelines.map((item) => item.id) }, '/pipelines', null));
    }

    const metricsMatch = /^\/pipelines\/([^/]+)\/metrics$/.exec(path);
    if (method === 'GET' && metricsMatch) {
      const window = query.get('window') === '24h' ? '24h' : '1h';
      return json(200, { points: demoMetricsPoints(metricsMatch[1], window) });
    }

    const logsMatch = /^\/pipelines\/([^/]+)\/logs$/.exec(path);
    if (method === 'GET' && logsMatch) {
      return json(200, { items: demoLogs(logsMatch[1], query.get('level'), query.get('correlate_incident') === 'true') });
    }

    if (method === 'GET' && path === '/costs') {
      return json(200, demoCost(query.get('scope'), query.get('id')));
    }

    if (method === 'GET' && path === '/confirmations') {
      const stateFilter = query.get('state') ?? 'pending';
      const items = [...confirmations.entries()]
        .filter(([, record]) => stateFilter === 'all' || record.state === stateFilter)
        .map(([id, record]) => confirmationDict(id, record));
      return json(200, { items, next_cursor: null });
    }

    const confirmationMatch = /^\/confirmations\/([^/]+)$/.exec(path);
    if (method === 'GET' && confirmationMatch) {
      const record = confirmations.get(confirmationMatch[1]);
      return record ? json(200, confirmationDict(confirmationMatch[1], record)) : notFound(path);
    }

    const approveMatch = /^\/confirmations\/([^/]+)\/approve$/.exec(path);
    if (method === 'POST' && approveMatch) {
      const record = confirmations.get(approveMatch[1]);
      if (!record) return notFound(path);
      record.state = 'approved';
      return json(200, envelope(null, confirmationDict(approveMatch[1], record), `/confirmations/${approveMatch[1]}`, null));
    }

    const rejectMatch = /^\/confirmations\/([^/]+)\/reject$/.exec(path);
    if (method === 'POST' && rejectMatch) {
      const record = confirmations.get(rejectMatch[1]);
      if (!record) return notFound(path);
      record.state = 'rejected';
      return json(200, envelope(null, confirmationDict(rejectMatch[1], record), `/confirmations/${rejectMatch[1]}`, null));
    }

    if (method === 'GET' && path === '/audit') {
      return json(200, { items: [], next_cursor: null });
    }

    return notFound(path);

    function json(status: number, payload: unknown): Response {
      return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
    }
  };
}

type PipelineActionKind = 'pause' | 'resume' | 'remove' | 'restart_initial_copy' | 'replay';

function nextDeclaredState(action: PipelineActionKind, current: PipelineDeclaredState): PipelineDeclaredState {
  switch (action) {
    case 'pause': return 'paused';
    case 'resume': return 'live';
    case 'remove': return 'stopped';
    case 'restart_initial_copy': return 'copying';
    case 'replay': return current;
  }
}

function demoActionEffect(action: string): string {
  switch (action) {
    case 'pause': return 'Suspend la lecture ; la position déjà atteinte est conservée.';
    case 'resume': return 'Reprend la lecture depuis la dernière position enregistrée.';
    case 'remove': return 'Retire définitivement cet élément du suivi.';
    case 'restart_initial_copy': return 'Reprend la copie initiale depuis son début.';
    case 'replay': return 'Rejoue les événements de la plage indiquée.';
    default: return 'Effet non décrit.';
  }
}

function pipelineDict(pipeline: DemoPipeline): Record<string, unknown> {
  return { id: pipeline.id, declared_state: pipeline.declaredState };
}

/** `GET /v2/pipelines` (liste) porte, en plus de `pipelineDict`, l'id de
 *  table/destination et les figures observées — voir
 *  `PipelineListRecord.to_dict` côté service. Une table en pause n'a plus
 *  de lecture active : retard/débit/lignes destination/dernière arrivée
 *  restent `null`, avec leur raison, jamais une valeur figée maquillée en
 *  mesure fraîche. Les lignes source restent connues (comptage IBM i,
 *  indépendant de l'état du pipeline). */
function pipelineListDict(pipeline: DemoPipeline): Record<string, unknown> {
  const paused = pipeline.declaredState === 'paused';
  const baseLag = (pipeline.name.length * 7) % 90;
  const baseThroughput = (pipeline.name.length * 3) % 40 + 5;
  const rowsSource = 10_000 + pipeline.name.length * 2_500;
  // La copie initiale en cours n'a pas encore rattrapé la source ; un
  // incident (« attention ») laisse un petit écart visible entre les deux
  // comptes — jamais un chiffre inventé, dérivé du même id déterministe.
  const rowsDestination = pipeline.declaredState === 'copying'
    ? Math.round(rowsSource * 0.62)
    : pipeline.declaredState === 'attention'
      ? rowsSource - 340
      : rowsSource;
  const absentReasons: Record<string, string> = {};
  if (paused) {
    absentReasons.lag_seconds = 'Table en pause — aucune lecture en cours.';
    absentReasons.throughput_rows_per_second = 'Table en pause — aucune lecture en cours.';
    absentReasons.rows_destination = 'Table en pause — dernière synchronisation non actualisée.';
    absentReasons.last_arrival_at = 'Table en pause — aucune arrivée depuis la mise en pause.';
  }
  return {
    ...pipelineDict(pipeline),
    table_id: `tbl_${pipeline.id}`,
    source_id: pipeline.sourceId,
    destination_id: destinationIdForSource(pipeline.sourceId),
    lag_seconds: paused ? null : baseLag,
    throughput_rows_per_second: paused ? null : baseThroughput,
    rows_source: rowsSource,
    rows_destination: paused ? null : rowsDestination,
    last_arrival_at: paused ? null : '2026-09-23T09:00:00Z',
    absent_reasons: absentReasons,
  };
}

function destinationIdForSource(sourceId: string): string {
  return sourceId === DEMO_SOURCE_VENTES ? DEMO_DESTINATION_VENTES : DEMO_DESTINATION_STOCKS;
}

function confirmationDict(id: string, record: { readonly actionRef: string; readonly resourceId: string; readonly state: string }): Record<string, unknown> {
  return {
    id,
    action_ref: record.actionRef,
    resource_type: 'pipeline',
    resource_id: record.resourceId,
    reason: `Action sensible « ${record.actionRef} » sur ${record.resourceId} (démonstration).`,
    risk_estimate: null,
    requested_by_kind: 'human',
    requested_by_id: 'op_demo',
    expires_at: null,
    state: record.state,
    approved_by_kind: record.state === 'approved' ? 'human' : null,
    approved_by_id: record.state === 'approved' ? 'op_demo' : null,
    approved_at: record.state === 'approved' ? '2026-09-23T09:30:00Z' : null,
    created_at: '2026-09-23T09:00:00Z',
    available: {
      approve: record.state === 'pending',
      reject: record.state === 'pending',
      execute: record.state === 'approved' && ['pipeline.remove', 'pipeline.restart_initial_copy'].includes(record.actionRef),
    },
  };
}

function sourceIdForDestination(destinationId: string): string {
  return destinationId === DEMO_DESTINATION_VENTES ? DEMO_SOURCE_VENTES : DEMO_SOURCE_STOCKS;
}

/** Série déterministe : un point toutes les 5 min sur 1 h (12 points), toutes
 *  les heures sur 24 h (24 points). Le retard oscille légèrement autour d'une
 *  valeur propre à chaque pipeline (dérivée de son id) — jamais aléatoire. */
function demoMetricsPoints(pipelineId: string, window: '1h' | '24h'): readonly Record<string, unknown>[] {
  const pipeline = DEMO_PIPELINES.find((item) => item.id === pipelineId);
  const baseLag = pipeline ? (pipeline.name.length * 7) % 90 : 30;
  const baseThroughput = pipeline ? (pipeline.name.length * 3) % 40 + 5 : 10;
  const count = window === '1h' ? 12 : 24;
  const stepSeconds = window === '1h' ? 300 : 3600;
  const paused = pipeline?.declaredState === 'paused';
  return Array.from({ length: count }, (_, index) => {
    const atSeconds = index * stepSeconds;
    const wobble = Math.sin(index / 2) * 5;
    return {
      at: new Date(Date.UTC(2026, 8, 23, 9, 0, 0) + atSeconds * 1000).toISOString(),
      lag_seconds: paused ? null : Math.max(0, Math.round(baseLag + wobble)),
      throughput_rows_per_second: paused ? null : Math.max(0, Math.round(baseThroughput + wobble)),
    };
  });
}

function demoLogs(pipelineId: string, level: string | null, correlateIncident: boolean): readonly Record<string, unknown>[] {
  const isIncidentPipeline = pipelineId === 'pl_ventes_lignes';
  const entries: Record<string, unknown>[] = [
    { at: '2026-09-23T08:55:00Z', level: 'info', message: 'Lecture du journal reprise depuis le dernier point de contrôle.', incident_id: null },
    { at: '2026-09-23T09:00:00Z', level: 'info', message: 'Fenêtre de livraison fermée et confirmée côté Snowflake.', incident_id: null },
  ];
  if (isIncidentPipeline) {
    entries.push(
      { at: '2026-09-23T09:10:00Z', level: 'warning', message: 'Retard croissant détecté sur la lecture du journal.', incident_id: DEMO_INCIDENT_ID },
      { at: '2026-09-23T09:12:00Z', level: 'error', message: 'Connexion à la destination Snowflake refusée (identifiants).', incident_id: DEMO_INCIDENT_ID },
    );
  }
  const byLevel = level ? entries.filter((entry) => entry.level === level) : entries;
  return correlateIncident ? byLevel.filter((entry) => entry.incident_id !== null) : byLevel;
}

function demoCost(scope: string | null, id: string | null): Record<string, unknown> {
  // Coûts mesurés au niveau connexion, estimés ou absents au niveau table,
  // selon l'id — donne aux trois provenances une table à montrer côté écran.
  if (scope === 'connection') {
    const measured = id === DEMO_SOURCE_VENTES;
    return measured
      ? { window: '24h', status: 'measured', amount: 4.82, currency: 'USD', basis: 'warehouse_credits', collected_at: '2026-09-23T09:00:00Z' }
      : { window: '24h', status: 'estimated', amount: 2.10, currency: 'USD', basis: 'estimation_debit_moyen', collected_at: '2026-09-23T09:00:00Z' };
  }
  if (id === 'pl_stocks_mouvements') {
    // La table en pause n'a aucun coût mesurable depuis sa mise en pause.
    return { window: '24h', status: 'absent', amount: null, currency: null, basis: null, collected_at: null };
  }
  return { window: '24h', status: 'estimated', amount: 0.34, currency: 'USD', basis: 'estimation_debit_moyen', collected_at: '2026-09-23T09:00:00Z' };
}

function envelope(before: unknown, after: unknown, path: string, dryRun: unknown): Record<string, unknown> {
  return { before, after, verify: { method: 'GET', path: `/v2${path}` }, dry_run: dryRun };
}

function notFound(path: string): Response {
  return new Response(JSON.stringify({
    error: { code: 'not_found', message: `Route de démonstration inconnue : ${path}`, next_action: 'Vérifiez l’appel du client.', retryable: false },
  }), { status: 404, headers: { 'Content-Type': 'application/json' } });
}

/** Résout l'id de source (convention `pl_<source>_<table>`) et le nom d'une
 *  table de démonstration — utilisé par `domain/cockpit.ts::groupTablesBySource`. */
export function demoSourceIdForPipeline(pipelineId: string): string | null {
  return DEMO_PIPELINES.find((item) => item.id === pipelineId)?.sourceId ?? null;
}

export function demoNameForPipeline(pipelineId: string): string {
  return DEMO_PIPELINES.find((item) => item.id === pipelineId)?.name ?? pipelineId;
}
