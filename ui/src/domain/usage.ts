import type { EvidenceKind, Pipeline } from './controlPlane.ts';

interface MeasuredUsageInput {
  readonly kind: 'measured';
  readonly key: string;
  readonly label: string;
  readonly value: number;
  readonly unit: string;
  readonly source: string;
  readonly observedAt: string;
  readonly provenance: EvidenceKind;
}

interface EstimateUsageInput {
  readonly kind: 'estimate';
  readonly label: string;
  readonly value: number;
  readonly currency: string;
  readonly methodology: string;
  readonly assumptions: readonly string[];
  readonly asOf: string;
  readonly region: string;
}

export interface MeasuredUsageView extends MeasuredUsageInput {
  readonly badge: 'Mesuré';
}

export interface EstimateUsageView extends EstimateUsageInput {
  readonly badge: 'Estimation';
  readonly unit: string;
}

export type UsageView = MeasuredUsageView | EstimateUsageView;

export interface PipelineUsageView {
  readonly pipelineId: string;
  readonly environment: string;
  readonly observedAt: string;
  readonly provenance: EvidenceKind;
  readonly metrics: readonly MeasuredUsageView[];
}

interface CounterDefinition {
  readonly label: string;
  readonly unit: string;
}

const counterDefinitions: Readonly<Record<string, CounterDefinition>> = {
  events_published: { label: 'Événements capturés', unit: 'événements' },
  payload_bytes_published: { label: 'Volume capturé', unit: 'octets' },
  cpu_ms_per_event: { label: 'Processeur par événement', unit: 'ms / événement' },
  mean_mcpu: { label: 'Processeur moyen', unit: 'cœur' },
  duplicates_in_target: { label: 'Doublons constatés en destination', unit: 'doublons (fenêtre)' },
  empty_scans: { label: 'Recherches à vide', unit: 'recherches' },
  errors: { label: 'Erreurs de lecture', unit: 'erreurs' },
  events_in_target: { label: 'Événements constatés en destination', unit: 'événements (fenêtre)' },
  idle_polls: { label: 'Lectures à vide', unit: 'lectures' },
  polls: { label: 'Lectures du journal', unit: 'lectures' },
  receiver_rotations: { label: 'Rotations du journal', unit: 'rotations' },
  run_duration_s: { label: 'Durée de lecture', unit: 'secondes' },
  windows_published: { label: 'Fenêtres publiées', unit: 'fenêtres' },
};

export function usageView(input: MeasuredUsageInput): MeasuredUsageView;
export function usageView(input: EstimateUsageInput): EstimateUsageView;
export function usageView(input: MeasuredUsageInput | EstimateUsageInput): UsageView {
  if (!Number.isFinite(input.value)) throw new Error('La valeur d’usage doit être finie');
  if (input.kind === 'measured') {
    if (!input.unit.trim() || !input.source.trim() || !input.observedAt.trim() || !input.provenance) {
      throw new Error('Une mesure requiert unité, source, observation et provenance');
    }
    return { ...input, badge: 'Mesuré' };
  }

  if (
    !input.methodology.trim()
    || !input.assumptions.length
    || input.assumptions.some((assumption) => !assumption.trim())
    || !input.asOf.trim()
    || !input.region.trim()
  ) {
    throw new Error('Une estimation requiert méthodologie, hypothèse, date et région');
  }
  return { ...input, badge: 'Estimation', unit: input.currency };
}

export function buildMeasuredUsage(pipelines: readonly Pipeline[]): readonly PipelineUsageView[] {
  return pipelines.map((pipeline) => ({
    pipelineId: pipeline.id,
    environment: pipeline.environment,
    observedAt: pipeline.observedAt,
    provenance: pipeline.quality.evidenceKind,
    metrics: Object.entries(pipeline.counters).flatMap(([key, value]) => {
      const definition = counterDefinitions[key];
      if (definition === undefined || value === null) return [];
      return [usageView({
        kind: 'measured',
        key,
        label: definition.label,
        value,
        unit: definition.unit,
        source: `control plane / ${pipeline.id} / counters.${key}`,
        observedAt: pipeline.observedAt,
        provenance: pipeline.quality.evidenceKind,
      })];
    }),
  }));
}
