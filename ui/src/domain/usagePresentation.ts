import type { EvidenceKind, Fleet, FleetPlan, Pipeline } from './controlPlane.ts';
import { buildMeasuredUsage, type MeasuredUsageView } from './usage.ts';

export type UsageGroupId = 'progression' | 'reliability' | 'activity' | 'destination';

export interface UsageGroupView {
  readonly id: UsageGroupId;
  readonly label: string;
  readonly explanation: string;
  readonly metrics: readonly MeasuredUsageView[];
}

export interface PipelineUsagePresentation {
  readonly pipelineId: string;
  readonly environment: string;
  readonly observedAt: string;
  readonly provenance: Pipeline['quality']['evidenceKind'];
  readonly freshness: Pipeline['quality']['freshness'];
  readonly coverage: Pipeline['quality']['coverage'];
  readonly primary: MeasuredUsageView | null;
  readonly groups: readonly UsageGroupView[];
  readonly allMetrics: readonly MeasuredUsageView[];
}

const groupDefinitions: readonly {
  readonly id: UsageGroupId;
  readonly label: string;
  readonly explanation: string;
  readonly keys: readonly string[];
}[] = [
  {
    id: 'progression',
    label: 'Progression',
    explanation: 'Compteurs de sortie de capture. Ils ne prouvent pas l’arrivée en destination.',
    keys: ['events_published', 'payload_bytes_published', 'windows_published'],
  },
  {
    id: 'reliability',
    label: 'Fiabilité',
    explanation: 'Erreurs relevées par le service de lecture. Un zéro reste une mesure, jamais une preuve de bonne santé.',
    keys: ['errors'],
  },
  {
    id: 'activity',
    label: 'Activité de lecture',
    explanation: 'Travail observé sur la source. Une rotation du journal n’est pas une erreur.',
    keys: [
      'polls',
      'idle_polls',
      'empty_scans',
      'receiver_rotations',
      'run_duration_s',
      'mean_mcpu',
      'cpu_ms_per_event',
    ],
  },
  {
    id: 'destination',
    label: 'Destination · fenêtre',
    explanation:
      'Compteurs côté destination sur la fenêtre observée — pas une réconciliation.',
    keys: ['events_in_target', 'duplicates_in_target'],
  },
] as const;

const primaryPriority = ['events_published', 'payload_bytes_published'] as const;

export function buildUsagePresentation(pipelines: readonly Pipeline[]): readonly PipelineUsagePresentation[] {
  return buildMeasuredUsage(pipelines).map((pipeline) => {
    const sourcePipeline = pipelines.find((candidate) => candidate.id === pipeline.pipelineId);
    if (!sourcePipeline) throw new Error(`Pipeline de télémétrie introuvable : ${pipeline.pipelineId}`);
    const byKey = new Map(pipeline.metrics.map((metric) => [metric.key, metric]));
    const groups = groupDefinitions.map((definition) => ({
      id: definition.id,
      label: definition.label,
      explanation: definition.explanation,
      metrics: definition.keys.flatMap((key) => {
        const metric = byKey.get(key);
        return metric ? [metric] : [];
      }),
    }));
    const allMetrics = groups.flatMap((group) => group.metrics);
    const primary = primaryPriority
      .map((key) => byKey.get(key))
      .find((metric): metric is MeasuredUsageView => metric !== undefined)
      ?? allMetrics[0]
      ?? null;

    return {
      pipelineId: pipeline.pipelineId,
      environment: pipeline.environment,
      observedAt: pipeline.observedAt,
      provenance: pipeline.provenance,
      freshness: sourcePipeline.quality.freshness,
      coverage: sourcePipeline.quality.coverage,
      primary,
      groups,
      allMetrics,
    };
  });
}

/* ------------------------------------------------------------------ */
/* Estimations dérivées — jamais présentées comme des mesures           */
/* ------------------------------------------------------------------ */

/** Une estimation dérivée d'entrées mesurées, avec sa règle citée et la date
 *  du relevé qui l'alimente. Rendue sous l'étiquette « Estimation » —
 *  « estimation · méthode : {règle} · relevé {date} ». Jamais convertie en
 *  mesure, jamais en zéro quand les entrées manquent. */
export interface DerivedUsageEstimate {
  readonly pipelineId: string | null;
  readonly label: string;
  readonly value: number;
  readonly unit: string;
  /** La règle de calcul citée : « cpu_ms_per_event × événements publiés ». */
  readonly method: string;
  readonly observedAt: string;
  readonly provenance: EvidenceKind | null;
}

/** Estimations calculables à partir des entrées mesurées — CPU cumulé, débit
 *  moyen de publication, crédits restants sur le budget déclaré. Une entrée
 *  absente ne produit aucune estimation : la dimension reste « Non mesuré ». */
export function buildDerivedEstimates(
  pipelines: readonly Pipeline[],
  fleet: { readonly fleet: Fleet; readonly observedAt: string } | null,
): readonly DerivedUsageEstimate[] {
  const estimates: DerivedUsageEstimate[] = [];
  for (const pipeline of pipelines) {
    const events = pipeline.counters.events_published ?? null;
    const cpuPerEvent = pipeline.counters.cpu_ms_per_event ?? null;
    const bytes = pipeline.counters.payload_bytes_published ?? null;
    const duration = pipeline.counters.run_duration_s ?? null;
    if (events !== null && cpuPerEvent !== null) {
      // Unité adaptée à la magnitude : « 18 222 512 ms de CPU » est illisible,
      // « 5,1 h de CPU » se scanne — le calcul servi reste cité dans `method`.
      const cpuMs = cpuPerEvent * events;
      const [cpuValue, cpuUnit] =
        cpuMs >= 3_600_000 ? [cpuMs / 3_600_000, 'h de processeur']
        : cpuMs >= 60_000 ? [cpuMs / 60_000, 'min de processeur']
        : [cpuMs, 'ms de processeur'];
      estimates.push({
        pipelineId: pipeline.id,
        label: 'Processeur cumulé du traitement',
        value: cpuValue,
        unit: cpuUnit,
        method: 'processeur par événement × événements capturés',
        observedAt: pipeline.observedAt,
        provenance: pipeline.quality.evidenceKind,
      });
    }
    if (bytes !== null && duration !== null && duration > 0) {
      const rate = bytes / duration;
      const [rateValue, rateUnit] =
        rate >= 1_048_576 ? [rate / 1_048_576, 'Mio / s']
        : rate >= 1024 ? [rate / 1024, 'Kio / s']
        : [rate, 'octets / s'];
      estimates.push({
        pipelineId: pipeline.id,
        label: 'Débit moyen de publication',
        value: rateValue,
        unit: rateUnit,
        method: 'octets publiés ÷ durée du traitement',
        observedAt: pipeline.observedAt,
        provenance: pipeline.quality.evidenceKind,
      });
    }
  }
  if (fleet !== null) {
    estimates.push({
      pipelineId: null,
      label: 'Crédits restants sur le budget déclaré',
      value: fleet.fleet.creditBudget - fleet.fleet.consumedCredits - fleet.fleet.reservedCredits,
      unit: 'crédits',
      method: 'budget − consommé − réservé',
      observedAt: fleet.observedAt,
      provenance: null,
    });
  }
  return estimates;
}

/* ------------------------------------------------------------------ */
/* Bornes honnêtes — ce que le relevé ne mesure pas                     */
/* ------------------------------------------------------------------ */

export interface CostBoundary {
  readonly label: string;
  readonly detail: string;
}

/** Liste des dimensions non mesurées — attribution de crédits, coût en
 *  devise, arrivées en destination, compteurs absents. Une dimension mesurée
 *  n'apparaît pas ici ; une dimension absente est dite « non mesurée »,
 *  jamais ramenée à zéro. */
export function costBoundaries(
  pipelines: readonly Pipeline[],
  fleet: Fleet | null,
  fleetPlan: FleetPlan | null,
  hasMeasuredCounters: boolean,
): readonly CostBoundary[] {
  const boundaries: CostBoundary[] = [];
  if (fleet === null) {
    boundaries.push({
      label: 'Attribution de crédits',
      detail: `non mesurée — ${fleetPlan?.cost.unknownBecause ?? 'ni manifeste de crédits ni plan de flotte servi'}`,
    });
  }
  if (!hasMeasuredCounters) {
    boundaries.push({
      label: 'Compteurs de consommation',
      detail: 'non publiés pour ces connexions',
    });
  }
  if (!pipelines.some((pipeline) => (pipeline.counters.events_in_target ?? null) !== null)) {
    boundaries.push({
      label: 'Arrivées en destination',
      detail: 'non mesurées — compteur non publié',
    });
  }
  boundaries.push({
    label: 'Coût en devise',
    detail: 'non mesuré — aucun rapport de coût ni tarif servi par le service',
  });
  return boundaries;
}
