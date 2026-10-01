import { ControlPlaneV2Client, type PipelineListRecordV2 } from './controlPlaneV2Client.ts';
import { wrapFetchWithAuthRedirect } from './authRedirect.ts';

/**
 * Fabrique le client du cockpit v2. Sans control plane `/v2` déployé, le
 * cockpit tourne en mode démo (fixtures locales, aucun réseau) — même
 * principe que `wizardClient.ts`. L'import de `fixtures/cockpitDemo.ts` est
 * **dynamique et gardé par `import.meta.env.DEV`** (constante figée au
 * build) afin que Rollup élimine toute la branche du bundle de production ;
 * `scripts/verify-build.mjs` fait échouer le build au moindre résidu.
 *
 * `VITE_QUADRINGENT_V2_BASE_PATH` bascule vers un vrai control plane une
 * fois celui-ci déployé (ex. `/v2`) — identique à `wizardClient.ts`, les
 * deux pointent vers le même service.
 */
export async function createCockpitClient(): Promise<ControlPlaneV2Client> {
  const basePath = import.meta.env.VITE_QUADRINGENT_V2_BASE_PATH as string | undefined;
  if (import.meta.env.DEV && !basePath) {
    const { createCockpitDemoFetch } = await import('./cockpitDemoBridge.ts');
    return new ControlPlaneV2Client({ fetchFn: createCockpitDemoFetch() });
  }
  // Tâche « auth-login » : une réponse 401 (session absente/expirée, mode
  // « authentification exigée » côté serveur) renvoie l'utilisateur vers
  // l'écran de connexion, avec l'écran demandé à retrouver au retour — voir
  // `authRedirect.ts` et `screens/Login.tsx`.
  return new ControlPlaneV2Client({
    basePath: basePath ?? '/v2',
    fetchFn: wrapFetchWithAuthRedirect(fetch, (hash) => { window.location.hash = hash; }, () => window.location.hash),
  });
}

export interface CockpitPipelineResolvers {
  readonly sourceIdForPipeline: (pipelineId: string) => string | null;
  readonly nameForPipeline: (pipelineId: string) => string;
}

/** La liste réelle des pipelines publie `source_id` et `table_id`. */
export function liveCockpitResolvers(
  pipelines: readonly PipelineListRecordV2[],
  tableNames: ReadonlyMap<string, string>,
): CockpitPipelineResolvers {
  const byId = new Map(pipelines.map((pipeline) => [pipeline.id, pipeline]));
  return {
    sourceIdForPipeline: (id) => byId.get(id)?.sourceId || null,
    nameForPipeline: (id) => {
      const tableId = byId.get(id)?.tableId;
      return tableId ? tableNames.get(tableId) ?? tableId : id;
    },
  };
}

/** Résout les noms depuis le catalogue de chaque source, sans masquer les
 * pipelines si ce catalogue devient momentanément indisponible. */
export async function createCockpitResolvers(
  client: ControlPlaneV2Client,
  pipelines: readonly PipelineListRecordV2[],
): Promise<CockpitPipelineResolvers> {
  const basePath = import.meta.env.VITE_QUADRINGENT_V2_BASE_PATH as string | undefined;
  if (import.meta.env.DEV && !basePath) {
    const { demoSourceIdForPipeline, demoNameForPipeline } = await import('./cockpitDemoBridge.ts');
    return { sourceIdForPipeline: demoSourceIdForPipeline, nameForPipeline: demoNameForPipeline };
  }
  const sourceIds = [...new Set(pipelines.map((pipeline) => pipeline.sourceId).filter(Boolean))];
  const batches = await Promise.all(sourceIds.map((sourceId) => client.listTables(sourceId).catch(() => [])));
  const tableNames = new Map<string, string>();
  for (const tables of batches) {
    for (const table of tables) {
      tableNames.set(table.id, [table.library, table.name].filter(Boolean).join('.') || table.id);
    }
  }
  return liveCockpitResolvers(pipelines, tableNames);
}
