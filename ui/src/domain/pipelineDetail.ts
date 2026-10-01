import type { Coverage, EvidenceKind, Freshness, Incident, LagPoint, Pipeline, Stage, StageId, StageStatus } from './controlPlane.ts';

export interface StageProofView {
  readonly statusLabel: string;
  readonly observedAt: string | null;
  readonly provenance: string;
}

export interface IncidentContextView {
  readonly publicLabel: string;
  readonly safeExplanation: string;
  readonly affectedStage: 'Lecture' | 'Destination';
  readonly firstObservedAt: null;
  readonly lastObservedAt: string;
  readonly recommendationState: 'no_safe_recommendation';
}

interface FocusTarget {
  focus(): void;
}

type FocusScheduler = (callback: () => void) => void;

export function resolveSelectedStage(pipeline: Pipeline, selectedId: StageId | null): Stage | null {
  if (selectedId === null) return null;
  return pipeline.stages.find((stage) => stage.id === selectedId) ?? null;
}

export function restoreSelectedStageFocus(
  stageButtons: ReadonlyMap<StageId, FocusTarget>,
  selectedId: StageId | null,
  schedule: FocusScheduler = queueMicrotask,
): void {
  if (selectedId === null) return;
  schedule(() => stageButtons.get(selectedId)?.focus());
}

export function incidentPublicLabel(incident: Incident): string {
  switch (incident.type) {
    case 'capture_connection_failure': return 'Connexion source interrompue';
    case 'capture_timeout': return 'Délai de lecture dépassé';
    case 'capture_stopped': return 'Lecture arrêtée en sécurité';
    case 'capture_auth_blocked': return 'Authentification source bloquée';
    case 'capture_position_review': return 'Position de reprise à revoir';
    case 'destination': return 'Incident à la destination';
  }
}

export function buildLagSegments(points: readonly LagPoint[]): readonly (readonly LagPoint[])[] {
  const segments: LagPoint[][] = [];
  let current: LagPoint[] = [];
  for (const point of points) {
    if (point.coverage === 'gap' || point.lag === null) {
      if (current.length > 0) segments.push(current);
      current = [];
      continue;
    }
    current.push(point);
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

export function lagCoverageLabel(point: LagPoint): string {
  if (point.kind === 'temporal_gap') return 'Intervalle non observé';
  if (point.coverage === 'gap') return `Manque · ${point.unknownSamples} inconnus`;
  return 'Mesurée';
}

export interface SalientLagPreview {
  readonly points: readonly LagPoint[];
  readonly caption: string;
  readonly complete: boolean;
}

export function sameLagWindow(left: LagPoint, right: LagPoint): boolean {
  return left.startSeconds === right.startSeconds && left.endSeconds === right.endSeconds;
}

export function selectSalientLagWindows(points: readonly LagPoint[]): SalientLagPreview {
  if (points.length <= 3) {
    return {
      points,
      caption: 'Tous les intervalles, synchronisés avec le graphe',
      complete: true,
    };
  }

  const first = points[0]!;
  const last = points[points.length - 1]!;
  let maximum = first;
  for (const point of points) {
    if ((point.lag ?? Number.NEGATIVE_INFINITY) > (maximum.lag ?? Number.NEGATIVE_INFINITY)) {
      maximum = point;
    }
  }
  const lastZero = [...points].reverse().find((point) => point.lag === 0) ?? last;
  const lastNonZero = [...points].reverse().find((point) => point.lag !== null && point.lag > 0);

  const selected: LagPoint[] = [];
  const add = (point: LagPoint | undefined) => {
    if (!point) return;
    if (selected.some((item) => sameLagWindow(item, point))) return;
    selected.push(point);
  };
  add(first);
  add(maximum);
  add(lastZero);
  if (selected.length < 3) add(lastNonZero);
  if (selected.length < 3) add(last);
  if (selected.length < 3) add(points[Math.floor((points.length - 1) / 2)]);
  selected.sort((left, right) => left.startSeconds - right.startSeconds || left.endSeconds - right.endSeconds);

  const maxIsFirst = sameLagWindow(maximum, first);
  const stopIsZero = lastZero.lag === 0;
  const caption = stopIsZero
    ? maxIsFirst
      ? 'Points marquants : départ (maximum), descente et arrêt à zéro, synchronisés avec le graphe'
      : 'Points marquants : départ, maximum et dernier intervalle à zéro, synchronisés avec le graphe'
    : maxIsFirst
      ? 'Points marquants : départ (maximum) et dernier intervalle, synchronisés avec le graphe'
      : 'Points marquants : départ, maximum et dernier intervalle, synchronisés avec le graphe';

  return { points: selected.slice(0, 3), caption, complete: false };
}

export function buildStageProof(stage: Stage, pipeline: Pipeline): StageProofView {
  let statusLabel: string;
  if (stage.status === 'healthy' && pipeline.quality.evidenceKind === 'simulation') {
    statusLabel = 'Observée dans la démonstration';
  } else if (stage.status === 'healthy' && pipeline.quality.evidenceKind === 'historical') {
    statusLabel = 'Observée dans le dernier relevé';
  } else if (stage.status === 'healthy') {
    statusLabel = 'Étape observée';
  } else if (stage.status === 'incident') {
    statusLabel = 'Incident observé';
  } else if (stage.status === 'degraded') {
    statusLabel = 'Observation partielle';
  } else if (stage.status === 'planned_stop') {
    statusLabel = 'Arrêt observé';
  } else if (stage.status === 'awaiting_resume') {
    statusLabel = 'Prête à reprendre';
  } else if (stage.observedAt !== null) {
    statusLabel = 'Non confirmée';
  } else {
    statusLabel = 'Observation absente';
  }
  return {
    statusLabel,
    observedAt: stage.observedAt,
    provenance: `service / ${pipeline.id} / ${dataNatureLabel(pipeline.quality.evidenceKind)}`,
  };
}

export function stageLabel(stage: Stage['id']): string {
  switch (stage) {
    case 'source': return 'Source';
    case 'capture': return 'Lecture';
    case 'raw': return 'Zone de réception';
    case 'load': return 'Copie initiale';
    case 'destination': return 'Destination';
  }
}

export function stageStatusLabel(status: StageStatus): string {
  switch (status) {
    case 'healthy': return 'Disponible';
    case 'degraded': return 'Partiel';
    case 'incident': return 'À vérifier';
    case 'planned_stop': return 'En pause';
    case 'awaiting_resume': return 'Prête à reprendre';
    case 'unknown': return 'Non disponible';
  }
}

export function coverageLabel(coverage: Coverage): string {
  switch (coverage) {
    case 'complete': return 'Complète';
    case 'partial': return 'Partielle';
    case 'gap': return 'Avec manques';
    case 'none': return 'Absente';
  }
}

export function freshnessLabel(freshness: Freshness): string {
  switch (freshness) {
    case 'fresh': return 'Récente';
    case 'late': return 'En retard';
    case 'stale': return 'Trop ancienne';
    case 'clock_untrusted': return 'Horloge non fiable';
  }
}

export function dataNatureLabel(kind: EvidenceKind): string {
  switch (kind) {
    case 'live': return 'Lecture actuelle';
    case 'historical': return 'Dernier relevé';
    case 'simulation': return 'Démonstration';
  }
}

export function buildIncidentContext(pipeline: Pipeline): IncidentContextView | null {
  if (!pipeline.incident) return null;
  const destinationIncident = pipeline.incident.type === 'destination';
  const safeExplanation = pipeline.incident.type === 'capture_connection_failure'
    ? 'La lecture ne peut plus confirmer la connexion à la source.'
    : pipeline.incident.type === 'capture_timeout'
      ? 'La lecture a dépassé le délai autorisé.'
      : pipeline.incident.type === 'capture_auth_blocked'
        ? 'La source a refusé la connexion : réparez le compte puis réarmez la garde.'
        : pipeline.incident.type === 'capture_position_review'
          ? 'Le point de reprise ne permet plus de fenêtre sûre : ré-ancrez le checkpoint.'
          : destinationIncident
            ? 'La preuve d’arrivée à destination est contradictoire ou en échec.'
            : 'La lecture s’est arrêtée selon la règle d’arrêt de sécurité.';
  return {
    publicLabel: incidentPublicLabel(pipeline.incident),
    safeExplanation,
    affectedStage: destinationIncident ? 'Destination' : 'Lecture',
    firstObservedAt: null,
    lastObservedAt: pipeline.observedAt,
    recommendationState: 'no_safe_recommendation',
  };
}
