/**
 * Domaine du cockpit v2 — dérivations pures depuis les types du client
 * (`data/controlPlaneV2Client.ts`) vers ce que les écrans affichent. Aucun
 * accès réseau ici (voir `data/useCockpit.ts` pour l'orchestration).
 *
 * Le control plane v2 (docs/plans/2026-09-23-control-plane-v2-contract.md)
 * ne modélise pas encore de « connexion » comme entité propre : seulement
 * des `sources`, `destinations`, `tables`/`pipelines` séparés. La liste des
 * pipelines publie désormais `source_id` et `table_id`, utilisés pour
 * rattacher les tables. Le cockpit associe encore une source à sa
 * destination par position (ordre de création) ; une source sans destination
 * correspondante reste « non couplée ».
 */

import type { MetricPoint, PipelineDeclaredState, PipelineListRecordV2, SourceRecord, DestinationRecord } from '../data/controlPlaneV2Client.ts';
import type { StatusWordState } from '../components/foundation/StatusWord.tsx';

/** Les cinq mots de `StatusWord` ne couvrent pas `not_started` (le pipeline
 *  n'a encore jamais démarré) : le plus proche des cinq états déjà
 *  vocabulaire produit est « à l'arrêt », en attendant qu'un sixième mot
 *  soit tranché par le produit. */
export function declaredStateToStatusWord(state: PipelineDeclaredState): StatusWordState {
  switch (state) {
    case 'live': return 'live';
    case 'copying': return 'copying';
    case 'paused': return 'paused';
    case 'attention': return 'attention';
    case 'stopped': return 'stopped';
    case 'not_started': return 'stopped';
  }
}

export interface CockpitTable {
  readonly pipelineId: string;
  readonly sourceId: string;
  readonly name: string;
  readonly state: StatusWordState;
  readonly declaredState: PipelineDeclaredState;
  /** Figures observées (`GET /v2/pipelines`) — `null` + raison dans
   *  `absentReasons` quand le service ne peut pas les fournir (ex. table en
   *  pause), jamais une valeur fabriquée pour combler la colonne. */
  readonly lagSeconds: number | null;
  readonly throughputRowsPerSecond: number | null;
  readonly rowsSource: number | null;
  readonly rowsDestination: number | null;
  readonly lastArrivalAt: string | null;
  readonly absentReasons: Readonly<Record<string, string>>;
}

export interface CockpitConnection {
  readonly id: string;
  readonly label: string;
  readonly destinationId: string | null;
  readonly tables: readonly CockpitTable[];
}

/** Associe sources/destinations/pipelines en connexions. `tablesBySource`
 *  est groupé avec le `source_id` explicite de chaque pipeline réel. */
export function buildConnections(
  sources: readonly SourceRecord[],
  destinations: readonly DestinationRecord[],
  tablesBySource: ReadonlyMap<string, readonly CockpitTable[]>,
): readonly CockpitConnection[] {
  return sources.map((source, index) => ({
    id: source.id,
    label: source.displayName ?? source.host,
    destinationId: destinations[index]?.id ?? null,
    tables: tablesBySource.get(source.id) ?? [],
  }));
}

/** Un seul bandeau d'attention par écran (design §3) : vrai si au moins une
 *  table de la connexion est en `attention`. */
export function connectionNeedsAttention(connection: CockpitConnection): boolean {
  return connection.tables.some((table) => table.state === 'attention');
}

export function fleetNeedsAttention(connections: readonly CockpitConnection[]): boolean {
  return connections.some(connectionNeedsAttention);
}

/** État agrégé d'une connexion, pour la ligne d'accueil : `attention` prime
 *  sur tout, puis `copying` (une copie en cours reste le fait le plus actif
 *  à montrer), puis `paused` si rien n'est en direct, sinon `live`. Une
 *  connexion sans table reste `stopped` — jamais un état inventé. */
export function connectionAggregateState(connection: CockpitConnection): StatusWordState {
  const states = new Set(connection.tables.map((table) => table.state));
  if (states.has('attention')) return 'attention';
  if (states.has('copying')) return 'copying';
  if (states.has('live')) return 'live';
  if (states.has('paused')) return 'paused';
  return 'stopped';
}

/** Agrège le dernier point de mesure de chaque table d'une connexion : le
 *  retard retenu est le pire des tables (le plus élevé), le débit la somme —
 *  une table sans mesure (`null`, ex. en pause) n'annule pas les autres,
 *  mais si aucune table n'a de mesure, l'agrégat reste `null` (absent). */
export function aggregateConnectionMetrics(latestPoints: readonly (MetricPoint | null)[]): { readonly lagSeconds: number | null; readonly throughputRowsPerSecond: number | null } {
  const lagValues = latestPoints.map((point) => point?.lagSeconds ?? null).filter((value): value is number => value !== null);
  const throughputValues = latestPoints.map((point) => point?.throughputRowsPerSecond ?? null).filter((value): value is number => value !== null);
  return {
    lagSeconds: lagValues.length > 0 ? Math.max(...lagValues) : null,
    throughputRowsPerSecond: throughputValues.length > 0 ? throughputValues.reduce((sum, value) => sum + value, 0) : null,
  };
}

export type SortDirection = 'asc' | 'desc';
export type ConnectionTableSortKey = 'name' | 'state';

export function sortAndFilterTables(
  tables: readonly CockpitTable[],
  query: string,
  sortKey: ConnectionTableSortKey,
  direction: SortDirection,
): readonly CockpitTable[] {
  const needle = query.trim().toLowerCase();
  const filtered = needle ? tables.filter((table) => table.name.toLowerCase().includes(needle)) : tables;
  const sorted = [...filtered].sort((a, b) => {
    const left = sortKey === 'name' ? a.name : a.state;
    const right = sortKey === 'name' ? b.name : b.state;
    return left.localeCompare(right);
  });
  return direction === 'desc' ? sorted.reverse() : sorted;
}

/** Regroupe les pipelines par connexion avec une résolution explicite.
 *  Un pipeline sans source déclarée n'est jamais rattaché au hasard. */
export function groupTablesBySource(
  pipelines: readonly PipelineListRecordV2[],
  sourceIdForPipeline: (pipelineId: string) => string | null,
  nameForPipeline: (pipelineId: string) => string,
): Map<string, readonly CockpitTable[]> {
  const bySource = new Map<string, CockpitTable[]>();
  for (const pipeline of pipelines) {
    const sourceId = sourceIdForPipeline(pipeline.id);
    if (!sourceId) continue;
    const table: CockpitTable = {
      pipelineId: pipeline.id,
      sourceId,
      name: nameForPipeline(pipeline.id),
      state: declaredStateToStatusWord(pipeline.declaredState),
      declaredState: pipeline.declaredState,
      lagSeconds: pipeline.observation.lagSeconds,
      throughputRowsPerSecond: pipeline.observation.throughputRowsPerSecond,
      rowsSource: pipeline.observation.rowsSource,
      rowsDestination: pipeline.observation.rowsDestination,
      lastArrivalAt: pipeline.observation.lastArrivalAt,
      absentReasons: pipeline.observation.absentReasons,
    };
    const bucket = bySource.get(sourceId);
    if (bucket) bucket.push(table);
    else bySource.set(sourceId, [table]);
  }
  return bySource;
}
