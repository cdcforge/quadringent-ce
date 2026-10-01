import type { EvidenceKind, Overview, Pipeline, PipelineStatus } from './controlPlane.ts';
import { href, statusCopy } from '../router.ts';
import { incidentPublicLabel, stageLabel } from './pipelineDetail.ts';

export interface PipelineFilters {
  readonly name: string;
  readonly status: PipelineStatus | 'all';
  readonly environment: string | 'all';
}

export interface WorkspaceVerdict {
  readonly headline: string;
  readonly detail: string;
  readonly destinationUnverified: number;
  readonly evidenceMode: WorkspaceEvidenceMode;
}
export interface EvidenceCopy { readonly label: string; readonly detail: string; }
export type WorkspaceEvidenceMode = EvidenceKind | 'mixed' | 'unestablished';
export interface WorkspaceFocus {
  readonly pipeline: Pipeline | null;
  readonly actionLabel: string;
  readonly actionHref: string | null;
}

const statusPriority: Readonly<Record<PipelineStatus, number>> = {
  incident: 0,
  awaiting_resume: 1,
  unknown: 2,
  degraded: 3,
  planned_stop: 4,
  recovering: 5,
  healthy: 6,
};

export function prioritizePipelines(pipelines: readonly Pipeline[]): readonly Pipeline[] {
  return [...pipelines].sort((left, right) => {
    const priority = priorityOf(left) - priorityOf(right);
    return priority !== 0 ? priority : left.id.localeCompare(right.id);
  });
}

export function workspaceFocus(overview: Overview): WorkspaceFocus {
  const pipeline = prioritizePipelines(overview.pipelines)[0] ?? null;
  if (!pipeline) {
    return { pipeline: null, actionLabel: 'Aucune action disponible', actionHref: null };
  }
  return {
    pipeline,
    actionLabel: `Examiner ${pipeline.id}`,
    actionHref: href({ name: 'pipeline', id: pipeline.id }),
  };
}

export function filterPipelines(pipelines: readonly Pipeline[], filters: PipelineFilters): readonly Pipeline[] {
  const name = filters.name.trim().toLocaleLowerCase('fr-FR');
  return prioritizePipelines(pipelines.filter((pipeline) => (
    (!name || pipeline.id.toLocaleLowerCase('fr-FR').includes(name))
    && (filters.status === 'all' || pipeline.status === filters.status)
    && (filters.environment === 'all' || pipeline.environment === filters.environment)
  )));
}

export function summarizeWorkspace(overview: Overview): WorkspaceVerdict {
  const destinationUnverified = overview.pipelines.filter(hasUnverifiedDestination).length;
  const evidenceMode = workspaceEvidenceMode(overview);
  const incidents = overview.pipelines.filter((pipeline) => pipeline.status === 'incident' || pipeline.incident !== null).length;
  const unknownOrStale = overview.pipelines.filter(isUnknownOrStale).length;
  const unavailableSources = overview.sources.filter((source) => source.status === 'unavailable').length;
  const noSourceAvailable = overview.sources.length > 0 && unavailableSources === overview.sources.length;

  if (overview.pipelines.length === 0 && noSourceAvailable) {
    return { headline: 'État non confirmé : sources indisponibles', detail: 'Aucune source disponible : aucun état ne peut être établi.', destinationUnverified, evidenceMode };
  }

  if (incidents > 0) {
    return {
      headline: `${incidents} incident${incidents > 1 ? 's' : ''} demande${incidents > 1 ? 'nt' : ''} une action`,
      detail: withSourceQualification('Les pipelines en incident passent avant toute autre lecture.', unavailableSources),
      destinationUnverified,
      evidenceMode,
    };
  }
  if (destinationUnverified > 0) {
    return {
      headline: `${destinationUnverified} destination${destinationUnverified > 1 ? 's ne sont pas vérifiées' : ' n’est pas vérifiée'}`,
      detail: withSourceQualification('L’arrivée dans la destination ne peut pas être confirmée avec les informations disponibles.', unavailableSources),
      destinationUnverified,
      evidenceMode,
    };
  }
  if (unknownOrStale > 0) {
    return {
      headline: `${unknownOrStale} pipeline${unknownOrStale > 1 ? 's restent' : ' reste'} à confirmer`,
      detail: withSourceQualification('Un état inconnu ou un relevé ancien ne peut pas être confirmé.', unavailableSources),
      destinationUnverified,
      evidenceMode,
    };
  }
  return {
    headline: evidenceHeadline(evidenceMode),
    detail: evidenceMode === 'live'
      ? withSourceQualification('Le relevé actuel ne signale pas de problème, dans la limite de sa couverture.', unavailableSources)
      : withSourceQualification(evidenceCopy(overview).detail, unavailableSources),
    destinationUnverified,
    evidenceMode,
  };
}

export function isSimulatedOrHistorical(overview: Overview): boolean {
  return workspaceEvidenceMode(overview) !== 'live';
}

export function evidenceCopy(overview: Overview): EvidenceCopy {
  switch (workspaceEvidenceMode(overview)) {
    case 'unestablished': return { label: 'État des données non confirmé', detail: 'Les informations reçues ne permettent pas de confirmer l’état des données.' };
    case 'simulation': return { label: 'Démonstration', detail: 'Ces données de démonstration ne représentent pas l’état réel.' };
    case 'historical': return { label: 'Relevé historique', detail: 'Ce relevé passé ne représente pas l’état actuel.' };
    case 'mixed': return { label: 'Données de natures différentes', detail: 'Des informations de natures différentes sont mélangées ; aucun état global ne peut être confirmé.' };
    case 'live': return { label: 'Relevé récent', detail: 'Relevé fourni par le service ; sa date est indiquée séparément.' };
  }
}

/** Provenance persistante partagée par les deux surfaces opérateur. */
export function evidenceBannerCopy(overview: Overview): EvidenceCopy {
  return evidenceCopy(overview);
}

export function flowEvidenceClass(pipeline: Pick<Pipeline, 'quality'>): string {
  return `flow-path flow-path--${pipeline.quality.evidenceKind}`;
}

export function flowMotionClass(pipeline: Pipeline): 'flow-path--moving' | 'flow-path--still' {
  return pipeline.quality.evidenceKind === 'live'
    && pipeline.quality.freshness === 'fresh'
    && !hasUnverifiedDestination(pipeline)
    ? 'flow-path--moving'
    : 'flow-path--still';
}

export function flowPathAccessibleLabel(pipeline: Pipeline): string {
  return `Chemin du pipeline ${pipeline.id} : ${pipeline.stages.map((stage) => {
    const label = stageLabel(stage.id);
    if (pipeline.quality.evidenceKind === 'live') return `${label} : ${statusCopy(stage.status).label}`;
    if (pipeline.quality.evidenceKind === 'simulation') {
      return stage.status === 'healthy'
        ? `${label} : observée dans la démonstration, état réel non confirmé`
        : `${label} : ${statusCopy(stage.status).label} — démonstration, état réel non confirmé`;
    }
    return stage.status === 'healthy'
      ? `${label} : relevé passé, pas un état actuel`
      : `${label} : ${statusCopy(stage.status).label} — relevé passé, pas un état actuel`;
  }).join(', ')}`;
}

export function hasUnverifiedDestination(pipeline: Pipeline): boolean {
  const destination = pipeline.stages.find((stage) => stage.id === 'destination');
  return destination?.status === 'unknown' || destination?.observedAt === null;
}

export function isUnknownOrStale(pipeline: Pipeline): boolean {
  return pipeline.status === 'unknown' || pipeline.quality.freshness === 'stale' || pipeline.quality.freshness === 'clock_untrusted';
}

export function pipelineIssue(pipeline: Pipeline): string {
  if (pipeline.status === 'incident') return pipeline.summary;
  if (pipeline.status === 'awaiting_resume') return 'Prête à reprendre';
  if (pipeline.incident) return incidentPublicLabel(pipeline.incident);
  if (hasUnverifiedDestination(pipeline)) return 'Arrivée en destination non confirmée';
  if (isUnknownOrStale(pipeline)) return 'État inconnu ou relevé ancien';
  if (pipeline.status === 'degraded') return pipeline.summary;
  if (pipeline.status === 'planned_stop') return 'Arrêt planifié';
  if (pipeline.status === 'recovering') return 'Reprise en cours';
  return pipeline.quality.evidenceKind === 'simulation' ? 'Démonstration : état réel non confirmé' : 'Aucun problème signalé';
}

function priorityOf(pipeline: Pipeline): number {
  if (pipeline.status === 'incident' || pipeline.incident !== null) return 0;
  if (isUnknownOrStale(pipeline)) return 1;
  return statusPriority[pipeline.status];
}

function withSourceQualification(detail: string, unavailableSources: number): string {
  return unavailableSources === 0 ? detail : `${detail} Couverture partielle : ${unavailableSources} source${unavailableSources > 1 ? 's' : ''} indisponible${unavailableSources > 1 ? 's' : ''}.`;
}

export function workspaceEvidenceMode(overview: Overview): WorkspaceEvidenceMode {
  if (overview.sources.length === 0) return 'unestablished';
  for (const pipeline of overview.pipelines) {
    const candidates = overview.sources.filter((source) => source.environment === pipeline.environment);
    if (candidates.length === 0) return 'unestablished';
    const candidateKinds = new Set(candidates.map((source) => source.evidenceKind));
    if (candidateKinds.size !== 1 || !candidateKinds.has(pipeline.quality.evidenceKind)) return 'mixed';
  }
  const kinds = new Set<EvidenceKind>([
    ...overview.sources.map((source) => source.evidenceKind),
    ...overview.pipelines.map((pipeline) => pipeline.quality.evidenceKind),
  ]);
  if (kinds.size > 1) return 'mixed';
  return kinds.values().next().value ?? 'unestablished';
}

function evidenceHeadline(mode: WorkspaceEvidenceMode): string {
  switch (mode) {
    case 'unestablished': return 'Nature et état non confirmés';
    case 'simulation': return 'Démonstration disponible, état réel non confirmé';
    case 'historical': return 'Relevé passé disponible, état actuel non confirmé';
    case 'mixed': return 'Données de natures différentes, état global non confirmé';
    case 'live': return 'Aucune attention requise dans le relevé actuel';
  }
}
