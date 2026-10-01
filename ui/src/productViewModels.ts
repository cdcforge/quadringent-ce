import type { ControlPlaneState } from './data/controlPlaneController.ts';
import type { Pipeline } from './domain/controlPlane.ts';
import { decimal, formatDuration } from './domain/format.ts';
import { sourceAvailability } from './domain/scope.ts';

const integerFormat = new Intl.NumberFormat('fr-FR');

export interface SummaryMetric {
  readonly value: string;
  readonly unknownReason?: string;
}

export interface CounterMetric {
  readonly label: string;
  readonly value: string;
  readonly unknownReason?: string;
}

export interface WorkspaceSummary {
  readonly pipelines: SummaryMetric;
  readonly attention: SummaryMetric;
  readonly incidents: SummaryMetric;
  readonly recoveries: SummaryMetric;
  readonly latestObservation: SummaryMetric;
  readonly empty: {
    readonly title: string;
    readonly description: string;
  };
}

export function buildWorkspaceSummary(
  state: ControlPlaneState,
  pipelines: readonly Pipeline[],
): WorkspaceSummary {
  if (state.status === 'loading' || state.status === 'failed') {
    return {
      pipelines: unknownMetric(),
      attention: unknownMetric(),
      incidents: unknownMetric(),
      recoveries: unknownMetric(),
      latestObservation: unknownMetric(),
      empty: {
        title: 'Données indisponibles',
        description: 'Aucune donnée ne peut être confirmée pour l’instant.',
      },
    };
  }

  const attention = pipelines.filter((pipeline) => pipeline.status !== 'healthy').length;
  const incidents = pipelines.filter((pipeline) => pipeline.status === 'incident' || pipeline.incident !== null).length;
  const recoveries = pipelines.filter((pipeline) => pipeline.status === 'recovering').length;
  const latestObservation = latestObservedAt(pipelines);

  return {
    pipelines: knownMetric(pipelines.length),
    attention: knownMetric(attention),
    incidents: knownMetric(incidents),
    recoveries: knownMetric(recoveries),
    latestObservation: latestObservation
      ? knownMetric(formatTimestamp(latestObservation))
      : { value: '—' },
    empty: {
      title: pipelines.length ? 'Aucun pipeline ne demande d’attention' : 'Aucun pipeline observé',
      description: pipelines.length
        ? 'Le relevé actuel ne signale ni incident, ni couverture partielle, ni reprise en cours.'
        : 'Aucune donnée de pipeline n’est visible dans ce relevé.',
    },
  };
}

export function buildPriorityMeta(state: ControlPlaneState, recoveringCount: number): string {
  if (state.status === 'loading') return 'Vérification en cours';
  if (state.status === 'failed') return 'Données indisponibles — aucun relevé';
  return recoveringCount > 0 ? `${integerFormat.format(recoveringCount)} en reprise` : 'Aucune reprise observée';
}

export function buildStatusNarrative(state: ControlPlaneState, now: Date = new Date()): string {
  switch (state.status) {
    case 'loading':
      return 'Données indisponibles · en attente du premier relevé.';
    case 'failed':
      return `Données indisponibles · ${sanitizeMessage(state.message)}`;
    case 'refreshing':
      return 'Relevé reçu · actualisation en cours.';
    case 'ready':
      return 'Relevé reçu. Les informations affichées décrivent ce relevé, pas une livraison confirmée.';
    case 'degraded':
      return `Dernier relevé conservé il y a ${ageLabel(state.lastSuccessAt, now)} · ${sanitizeMessage(state.message)}`;
  }
}

export function buildPipelineMissingCopy(
  state: ControlPlaneState,
  _pipelineId: string,
): { readonly title: string; readonly description: string } {
  if (state.status === 'loading' || state.status === 'failed') {
    return {
      title: 'Données indisponibles',
      description:
        state.status === 'loading'
          ? 'Vérification en cours — la console ne peut pas encore confirmer ce pipeline.'
          : 'Données indisponibles — la console ne peut pas encore confirmer ce pipeline.',
    };
  }

  const confirmedCurrentInventory = state.status === 'ready'
    && state.connection === 'live'
    && 'overview' in state
    && state.overview.scope?.kind !== 'unavailable'
    && sourceAvailability(state.overview) === 'available';

  if (!confirmedCurrentInventory) {
    return {
      title: 'Présence du pipeline à confirmer',
      description: 'Les informations disponibles ne permettent pas de confirmer si ce pipeline fait partie du suivi actuel.',
    };
  }

  return {
    title: 'Pipeline non trouvé',
    description: 'Le relevé confirmé ne contient pas ce pipeline.',
  };
}

export function buildPipelineLead(state: ControlPlaneState, pipeline: Pipeline | null): string {
  if (pipeline) return pipeline.summary;
  return buildPipelineMissingCopy(state, '').description;
}

export function buildCounterMetrics(counters: Readonly<Record<string, number | null>>): readonly CounterMetric[] {
  return Object.entries(counters).map(([key, value]) => (
    value === null
      ? {
          label: counterLabel(key),
          value: 'Inconnu',
          unknownReason: 'Compteur à confirmer',
        }
      : {
          label: counterLabel(key),
          value: counterValue(key, value),
        }
  ));
}

function counterValue(key: string, value: number): string {
  switch (key) {
    case 'run_duration_s': return formatDuration(value);
    case 'cpu_ms_per_event': return `${decimal(value, 1)} ms`;
    case 'mean_mcpu': return `${decimal(value / 1000, 2)} cœur`;
    case 'payload_bytes_published': return `${integerFormat.format(value)} octets`;
    default: return integerFormat.format(value);
  }
}

function knownMetric(value: number | string): SummaryMetric {
  return { value: typeof value === 'number' ? integerFormat.format(value) : value };
}

function unknownMetric(): SummaryMetric {
  return { value: 'Inconnu', unknownReason: 'Données indisponibles' };
}

function latestObservedAt(pipelines: readonly Pipeline[]): string | null {
  let latest: string | null = null;
  for (const pipeline of pipelines) {
    if (!latest || new Date(pipeline.observedAt).getTime() > new Date(latest).getTime()) latest = pipeline.observedAt;
  }
  return latest;
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat('fr-FR', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'UTC',
  }).format(new Date(value));
}

function ageLabel(observedAt: Date, now: Date): string {
  const minutes = Math.max(0, Math.round((now.getTime() - observedAt.getTime()) / 60_000));
  if (minutes === 0) return '0 min';
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest}` : `${hours} h`;
}

function sanitizeMessage(message: string): string {
  const cleaned = message.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
  return cleaned || 'Données indisponibles';
}

function counterLabel(key: string): string {
  return counterLabels[key] ?? key.replace(/_/g, ' ');
}

const counterLabels: Readonly<Record<string, string>> = {
  events_published: 'Événements capturés',
  events_in_target: 'Événements constatés en cible · fenêtre en cours',
  duplicates_in_target: 'Doublons constatés en cible · fenêtre en cours',
  receiver_rotations: 'Rotations du journal',
  polls: 'Lectures du journal',
  errors: 'Erreurs de lecture',
  windows_published: 'Fenêtres publiées',
  load_retries: 'Reprises de chargement',
  empty_scans: 'Recherches à vide',
  idle_polls: 'Lectures à vide',
  run_duration_s: 'Durée de lecture',
  cpu_ms_per_event: 'Processeur par événement',
  mean_mcpu: 'Processeur moyen',
  payload_bytes_published: 'Volume capturé',
};
