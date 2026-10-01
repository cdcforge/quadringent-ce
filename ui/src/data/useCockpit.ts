import { useEffect, useRef, useState } from 'react';
import { createCockpitClient, createCockpitResolvers } from './cockpitClient.ts';
import type { ControlPlaneV2Client, CostSnapshot, LogEntry, LogLevel, MetricsSeries, PipelineRecordV2 } from './controlPlaneV2Client.ts';
import { aggregateConnectionMetrics, buildConnections, groupTablesBySource, type CockpitConnection } from '../domain/cockpit.ts';

/** Mémorise le client (démo ou réel) : un seul import dynamique par session,
 *  jamais de course entre deux appels concurrents qui créeraient chacun un
 *  client démo indépendant avec son propre état muté en mémoire. */
let cockpitClientPromise: Promise<ControlPlaneV2Client> | null = null;
export function getCockpitClient(): Promise<ControlPlaneV2Client> {
  if (!cockpitClientPromise) cockpitClientPromise = createCockpitClient();
  return cockpitClientPromise;
}

/**
 * Charge les connexions du cockpit (sources + destinations + pipelines,
 * assemblés par `domain/cockpit.ts::buildConnections`). Le client réel vient
 * de `createCockpitClient()` (démo en dev sans backend, sinon `/v2`) — voir
 * ce fichier pour pourquoi l'import est asynchrone.
 *
 * La résolution pipeline→source utilise les identifiants publiés par la
 * liste des pipelines ; les noms viennent du catalogue de chaque source.
 */
export type CockpitConnectionsState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly connections: readonly CockpitConnection[] }
  | { readonly status: 'failed'; readonly message: string };

export function useCockpitConnections(): { readonly state: CockpitConnectionsState; readonly reload: () => void } {
  const [state, setState] = useState<CockpitConnectionsState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    setState({ status: 'loading' });
    (async () => {
      const client = await getCockpitClient();
      const [sources, destinations, pipelines] = await Promise.all([
        client.listSources(),
        client.listDestinations(),
        client.listPipelines(),
      ]);
      const resolvers = await createCockpitResolvers(client, pipelines);
      const grouped = groupTablesBySource(pipelines, resolvers.sourceIdForPipeline, resolvers.nameForPipeline);
      return buildConnections(sources, destinations, grouped);
    })()
      .then((connections) => {
        if (alive.current) setState({ status: 'ready', connections });
      })
      .catch((error: unknown) => {
        if (alive.current) setState({ status: 'failed', message: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      alive.current = false;
    };
  }, [nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}

export interface ConnectionHomeRow {
  readonly connection: CockpitConnection;
  readonly lagSeconds: number | null;
  readonly throughputRowsPerSecond: number | null;
  readonly costToday: CostSnapshot | null;
}

export type CockpitHomeState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly rows: readonly ConnectionHomeRow[] }
  | { readonly status: 'failed'; readonly message: string };

/**
 * Vue Accueil : une ligne par connexion, avec retard/débit agrégés (dernier
 * point 1 h de chaque table) et le coût du jour de la connexion
 * (`GET /v2/costs?scope=connection`). Une requête de plus par connexion et
 * par table que `useCockpitConnections` seul ; acceptable pour le nombre de
 * connexions/tables attendu (dizaines, pas milliers).
 */
export function useCockpitHome(): { readonly state: CockpitHomeState; readonly reload: () => void } {
  const { state: connectionsState, reload } = useCockpitConnections();
  const [state, setState] = useState<CockpitHomeState>({ status: 'loading' });
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    if (connectionsState.status === 'loading') {
      setState({ status: 'loading' });
      return () => { alive.current = false; };
    }
    if (connectionsState.status === 'failed') {
      setState({ status: 'failed', message: connectionsState.message });
      return () => { alive.current = false; };
    }
    (async () => {
      const client = await getCockpitClient();
      const rows = await Promise.all(connectionsState.connections.map(async (connection): Promise<ConnectionHomeRow> => {
        const [points, costToday] = await Promise.all([
          Promise.all(connection.tables.map(async (table) => {
            const series = await client.getMetrics(table.pipelineId, '1h');
            return series.points.at(-1) ?? null;
          })),
          client.getCosts('connection', connection.id, '24h').catch(() => null),
        ]);
        const aggregate = aggregateConnectionMetrics(points);
        return { connection, lagSeconds: aggregate.lagSeconds, throughputRowsPerSecond: aggregate.throughputRowsPerSecond, costToday };
      }));
      return rows;
    })()
      .then((rows) => {
        if (alive.current) setState({ status: 'ready', rows });
      })
      .catch((error: unknown) => {
        if (alive.current) setState({ status: 'failed', message: error instanceof Error ? error.message : String(error) });
      });
    return () => { alive.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- connectionsState is the sole real dependency; recomputed identity each render otherwise.
  }, [connectionsState]);

  return { state, reload };
}

export interface CockpitTableData {
  readonly pipeline: PipelineRecordV2;
  readonly metrics: MetricsSeries;
  readonly logs: readonly LogEntry[];
  readonly costToday: CostSnapshot | null;
  /** Nom affichable (bibliothèque/table) ; l'écran retombe sur l'identifiant
   *  de table si le catalogue est momentanément indisponible. */
  readonly name?: string;
}

export type CockpitTableState =
  | { readonly status: 'loading' }
  | { readonly status: 'ready'; readonly data: CockpitTableData }
  | { readonly status: 'failed'; readonly message: string };

export interface CockpitTableLogFilters {
  readonly level?: LogLevel;
  readonly correlateIncident?: boolean;
}

/**
 * Charge le détail d'une table du cockpit : pipeline (`declared_state`),
 * métriques (fenêtre `window`), journaux (`logFilters`), coûts du jour. Les
 * quatre routes viennent du client v2 — voir controlPlaneV2Client.ts pour
 * lesquelles sont CONTRAT SEUL au moment de l'écrit.
 */
export function useCockpitTable(
  pipelineId: string,
  window: '1h' | '24h',
  logFilters: CockpitTableLogFilters,
): { readonly state: CockpitTableState; readonly reload: () => void } {
  const [state, setState] = useState<CockpitTableState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    setState({ status: 'loading' });
    (async () => {
      const client = await getCockpitClient();
      const [pipeline, metrics, logs, costToday, pipelines] = await Promise.all([
        client.getPipeline(pipelineId),
        client.getMetrics(pipelineId, window),
        client.getLogs(pipelineId, logFilters),
        client.getCosts('table', pipelineId, window).catch(() => null),
        client.listPipelines().catch(() => []),
      ]);
      const resolvers = await createCockpitResolvers(client, pipelines);
      const name = resolvers.nameForPipeline(pipelineId);
      return { pipeline, metrics, logs, costToday, name: name === pipelineId ? undefined : name };
    })()
      .then((data) => {
        if (alive.current) setState({ status: 'ready', data });
      })
      .catch((error: unknown) => {
        if (alive.current) setState({ status: 'failed', message: error instanceof Error ? error.message : String(error) });
      });
    return () => { alive.current = false; };
  }, [pipelineId, window, logFilters.level, logFilters.correlateIncident, nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}
