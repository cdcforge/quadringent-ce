import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type { EvidenceKind, Overview, Pipeline, Stage, StageId } from './controlPlane.ts';
import { prioritizePipelines } from './pipelineView.ts';
import { href, inspectHref } from '../router.ts';

export type ProofBreakReason = 'unavailable' | 'incident' | 'unknown' | 'stale' | 'degraded' | 'planned-stop';
export type ProofScopeKind = EvidenceKind | 'mixed' | 'cached' | 'stale' | 'partial' | 'unavailable';

export interface ProofBreak {
  readonly stage: StageId;
  readonly reason: ProofBreakReason;
  readonly cause: string;
}

export interface ProofFocusObject {
  readonly kind: 'break' | 'completion' | 'unavailable';
  readonly stage: StageId | null;
  readonly label: string;
  readonly cause: string;
  readonly evidence: string;
}

export interface ProofDownstream {
  readonly stage: 'destination';
  readonly label: string;
  readonly status: 'not-established' | 'retained-observation';
  readonly detail: string;
  readonly observedAt: string | null;
}

export interface ProofFocus {
  readonly pipelineId: string | null;
  readonly environment: string | null;
  readonly verdict: {
    readonly headline: string;
    readonly detail: string;
  };
  readonly firstBreak: ProofBreak | null;
  readonly upstream: {
    readonly stages: readonly StageId[];
    readonly label: string;
  };
  readonly focus: ProofFocusObject;
  readonly downstream: ProofDownstream | null;
  readonly proofScope: {
    readonly kind: ProofScopeKind;
    readonly label: string;
    readonly detail: string;
  };
  readonly timestamp: {
    readonly value: string | null;
    readonly label: string;
  };
  readonly cta:
    | { readonly kind: 'link'; readonly label: string; readonly href: string }
    | { readonly kind: 'refresh'; readonly label: string };
}

export interface PipelineProofContext {
  readonly generatedAt: string;
  readonly sources: Overview['sources'];
  readonly cached: boolean;
}

const stageOrder = ['source', 'capture', 'raw', 'load', 'destination'] as const;

const stageLabels: Readonly<Record<StageId, string>> = {
  source: 'Source',
  capture: 'Lecture',
  raw: 'Zone de réception',
  load: 'Copie initiale',
  destination: 'Destination',
};

const expectedEvidence: Readonly<Record<StageId, string>> = {
  source: 'Disponibilité source',
  capture: 'Reprise de lecture',
  raw: 'Réception durable',
  load: 'Copie initiale',
  destination: 'Arrivée confirmée',
};

export function resolveProofFocus(state: ControlPlaneState): ProofFocus {
  if (state.status === 'loading' || state.status === 'failed') {
    return unavailableFocus(state.status === 'loading');
  }

  const pipeline = prioritizePipelines(state.overview.pipelines)[0] ?? null;
  if (!pipeline) return emptyFocus(state.overview, isCachedState(state));

  return resolvePipelineProofFocus(pipeline, {
    generatedAt: state.overview.generatedAt,
    sources: state.overview.sources,
    cached: isCachedState(state),
  });
}

export function resolvePipelineProofFocus(pipeline: Pipeline, context: PipelineProofContext): ProofFocus {
  const { cached } = context;
  const sourceAvailability = pipelineSourceAvailability(context.sources, pipeline);
  const firstBreak = resolveFirstBreak(sourceAvailability, pipeline);
  const proofScope = resolveProofScope(pipeline, cached, sourceAvailability);
  const timestamp = {
    value: pipeline.observedAt,
    label: cached ? 'Dernier relevé conservé' : 'Observation du relevé',
  };

  if (!firstBreak) {
    return completionFocus(pipeline, proofScope, timestamp, cached);
  }

  const breakIndex = stageOrder.indexOf(firstBreak.stage);
  const upstreamStages = stageOrder.slice(0, breakIndex);
  const focus: ProofFocusObject = {
    kind: 'break',
    stage: firstBreak.stage,
    label: stageLabels[firstBreak.stage],
    cause: firstBreak.cause,
    evidence: `Attendu · ${expectedEvidence[firstBreak.stage]}`,
  };

  return {
    pipelineId: pipeline.id,
    environment: pipeline.environment,
    verdict: {
      headline: cached ? 'Dernière livraison non confirmée.' : 'Livraison non confirmée.',
      detail: `Premier point non confirmé : ${stageReference(firstBreak.stage)}.`,
    },
    firstBreak,
    upstream: {
      stages: upstreamStages,
      label: upstreamStages.map((stage) => stageLabels[stage]).join(' · '),
    },
    focus,
    downstream: downstreamAfterBreak(pipeline, firstBreak.stage),
    proofScope,
    timestamp,
    cta: {
      kind: 'link',
      label: `Inspecter ${stageReference(firstBreak.stage)}`,
      href: inspectHref(pipeline.id, firstBreak.stage),
    },
  };
}

type PipelineSourceAvailability = 'available' | 'partial' | 'unavailable' | 'unmapped' | 'mismatch';

function resolveFirstBreak(sourceAvailability: PipelineSourceAvailability, pipeline: Pipeline): ProofBreak | null {
  if (sourceAvailability === 'unavailable') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: 'La source est indisponible. Le dernier relevé reste consultable.',
    };
  }
  if (sourceAvailability === 'partial') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: 'Les sources sont partiellement indisponibles ; l’arrivée ne peut pas être confirmée.',
    };
  }
  if (sourceAvailability === 'unmapped') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: 'Aucune source n’est rattachée à cet environnement ; la disponibilité n’est pas établie.',
    };
  }
  if (sourceAvailability === 'mismatch') {
    return {
      stage: 'source',
      reason: 'unavailable',
      cause: 'La nature des sources est incohérente ou mixte ; l’arrivée ne peut pas être confirmée.',
    };
  }

  if (pipeline.quality.freshness === 'stale') {
    return {
      stage: 'source',
      reason: 'stale',
      cause: 'Le relevé le plus récent est trop ancien.',
    };
  }
  if (pipeline.quality.freshness === 'clock_untrusted') {
    return {
      stage: 'source',
      reason: 'stale',
      cause: 'L’heure du relevé n’est pas fiable.',
    };
  }

  let stageCandidate: ProofBreak | null = null;
  for (const stageId of stageOrder) {
    const stage = pipeline.stages.find((candidate) => candidate.id === stageId);
    if (!stage) {
      stageCandidate = unknownStageBreak(stageId);
      break;
    }
    const broken = stageBreak(stage);
    if (broken) {
      stageCandidate = broken;
      break;
    }
  }

  const qualificationCandidate = pipelineQualificationBreak(pipeline);
  if (!stageCandidate) return qualificationCandidate;
  if (!qualificationCandidate) return stageCandidate;
  return stageOrder.indexOf(stageCandidate.stage) <= stageOrder.indexOf(qualificationCandidate.stage)
    ? stageCandidate
    : qualificationCandidate;
}

function pipelineQualificationBreak(pipeline: Pipeline): ProofBreak | null {
  if (pipeline.incident) {
    const destinationIncident = pipeline.incident.type === 'destination';
    return {
      stage: destinationIncident ? 'destination' : 'capture',
      reason: 'incident',
      cause: destinationIncident
        ? 'Un incident de destination est signalé ; le relevé ne permet pas de le qualifier davantage.'
        : 'Un incident de lecture est signalé ; le relevé ne permet pas de le qualifier davantage.',
    };
  }
  if (pipeline.status === 'incident') {
    return {
      stage: 'destination',
      reason: 'incident',
      cause: 'Un incident global est signalé ; la livraison n’est pas confirmée.',
    };
  }
  if (pipeline.quality.freshness === 'late') {
    return {
      stage: 'destination',
      reason: 'degraded',
      cause: 'Le relevé est tardif ; la livraison n’est pas confirmée.',
    };
  }
  if (pipeline.quality.coverage !== 'complete') {
    return {
      stage: 'destination',
      reason: 'unknown',
      cause: 'Le relevé est incomplet ; la livraison n’est pas confirmée.',
    };
  }
  if (pipeline.status === 'recovering') {
    return {
      stage: 'destination',
      reason: 'degraded',
      cause: 'La reprise est en cours ; la livraison n’est pas encore confirmée.',
    };
  }
  if (pipeline.status === 'unknown') {
    return {
      stage: 'destination',
      reason: 'unknown',
      cause: 'L’état de la pipeline est inconnu ; la livraison n’est pas confirmée.',
    };
  }
  if (pipeline.status === 'planned_stop') {
    return {
      stage: 'destination',
      reason: 'planned-stop',
      cause: 'La pipeline est en pause planifiée ; la livraison courante n’est pas confirmée.',
    };
  }
  if (pipeline.status === 'awaiting_resume') {
    return {
      stage: 'capture',
      reason: 'planned-stop',
      cause: 'La cause de l’arrêt est résolue ; la capture attend le relancement.',
    };
  }
  if (pipeline.status === 'degraded' && pipeline.quality.evidenceKind === 'live') {
    return {
      stage: 'destination',
      reason: 'degraded',
      cause: 'L’état global est dégradé ; la livraison n’est pas confirmée.',
    };
  }
  return null;
}

function stageBreak(stage: Stage): ProofBreak | null {
  if (stage.status === 'incident') {
    return { stage: stage.id, reason: 'incident', cause: stage.detail };
  }
  if (stage.status === 'degraded') {
    return { stage: stage.id, reason: 'degraded', cause: stage.detail };
  }
  if (stage.status === 'planned_stop') {
    return { stage: stage.id, reason: 'planned-stop', cause: 'Cette étape est en pause planifiée.' };
  }
  if (stage.status === 'awaiting_resume') {
    return { stage: stage.id, reason: 'planned-stop', cause: 'Cette étape attend le relancement.' };
  }
  if (stage.status === 'unknown' || stage.observedAt === null) return unknownStageBreak(stage.id);
  return null;
}

function unknownStageBreak(stage: StageId): ProofBreak {
  const cause: Readonly<Record<StageId, string>> = {
    source: 'Aucune observation de la source n’a été reçue.',
    capture: 'Aucune reprise de lecture n’a été observée.',
    raw: 'Aucune réception durable n’a été observée.',
    load: 'Aucune copie initiale n’a été confirmée.',
    destination: 'Aucune arrivée n’a été confirmée.',
  };
  return { stage, reason: 'unknown', cause: cause[stage] };
}

function downstreamAfterBreak(pipeline: Pipeline, breakStage: StageId): ProofDownstream | null {
  if (breakStage === 'destination') return null;
  const destination = pipeline.stages.find((stage) => stage.id === 'destination');
  if (destination?.status === 'healthy' && destination.observedAt !== null) {
    return {
      stage: 'destination',
      label: 'Destination',
      status: 'retained-observation',
      detail: 'Dernière observation conservée ; elle ne confirme pas la livraison au-delà du point non confirmé.',
      observedAt: destination.observedAt,
    };
  }
  return {
    stage: 'destination',
    label: 'Destination',
    status: 'not-established',
    detail: 'Non confirmée dans le relevé actuel.',
    observedAt: destination?.observedAt ?? null,
  };
}

function completionFocus(
  pipeline: Pipeline,
  proofScope: ProofFocus['proofScope'],
  timestamp: ProofFocus['timestamp'],
  cached: boolean,
): ProofFocus {
  const copy = completionCopy(pipeline.quality.evidenceKind, cached);
  return {
    pipelineId: pipeline.id,
    environment: pipeline.environment,
    verdict: copy,
    firstBreak: null,
    upstream: {
      stages: ['source', 'capture', 'raw', 'load'],
      label: 'Source · Lecture · Zone de réception · Copie initiale',
    },
    focus: {
      kind: 'completion',
      stage: 'destination',
      label: 'Destination',
      cause: completionCause(pipeline.quality.evidenceKind, cached),
      evidence: 'Reçu · Arrivée confirmée',
    },
    downstream: null,
    proofScope,
    timestamp,
    cta: {
      kind: 'link',
      label: 'Examiner la livraison',
      href: href({ name: 'pipeline', id: pipeline.id }),
    },
  };
}

function completionCopy(kind: EvidenceKind, cached: boolean): ProofFocus['verdict'] {
  if (cached) {
    return {
      headline: 'Dernière livraison confirmée dans le relevé conservé.',
      detail: 'Cette observation conservée ne confirme pas l’état courant.',
    };
  }
  switch (kind) {
    case 'simulation':
      return {
        headline: 'Livraison confirmée dans la démonstration.',
        detail: 'Cette démonstration ne confirme pas l’état réel.',
      };
    case 'historical':
      return {
        headline: 'Livraison confirmée dans un relevé antérieur.',
        detail: 'Ce relevé antérieur ne confirme pas l’état courant.',
      };
    case 'live':
      return {
        headline: 'Livraison confirmée dans ce relevé.',
        detail: 'Les cinq étapes sont observées, récentes et cohérentes.',
      };
  }
}

function completionCause(kind: EvidenceKind, cached: boolean): string {
  if (cached) return 'La dernière arrivée confirmée appartient au relevé conservé.';
  if (kind === 'simulation') return 'La destination est observée dans la démonstration uniquement.';
  if (kind === 'historical') return 'La destination est observée dans le relevé antérieur uniquement.';
  return 'L’arrivée est confirmée dans le relevé courant.';
}

function resolveProofScope(
  pipeline: Pipeline,
  cached: boolean,
  sourceAvailability: PipelineSourceAvailability,
): ProofFocus['proofScope'] {
  if (cached) {
    return {
      kind: 'cached',
      label: 'Relevé conservé · hors ligne',
      detail: 'La dernière composition connue est conservée sans être présentée comme actuelle.',
    };
  }
  if (sourceAvailability === 'unavailable') {
    return {
      kind: 'unavailable',
      label: 'Sources indisponibles',
      detail: 'Aucune source de ce périmètre ne permet de confirmer le relevé courant.',
    };
  }
  if (sourceAvailability === 'partial') {
    return {
      kind: 'partial',
      label: 'Sources partiellement indisponibles',
      detail: 'La couverture des sources est ambiguë ; aucune livraison courante n’est confirmée.',
    };
  }
  if (sourceAvailability === 'unmapped') {
    return {
      kind: 'unavailable',
      label: 'Disponibilité source non établie',
      detail: 'Aucune source n’est rattachée à cet environnement ; aucune livraison courante n’est confirmée.',
    };
  }
  if (sourceAvailability === 'mismatch') {
    return {
      kind: 'mixed',
      label: 'Nature des sources mixte ou incohérente',
      detail: 'La nature des sources ne correspond pas clairement à celle du pipeline ; aucune livraison courante n’est confirmée.',
    };
  }
  if (pipeline.quality.freshness === 'stale' || pipeline.quality.freshness === 'clock_untrusted') {
    return {
      kind: 'stale',
      label: scopedBoundaryLabel(pipeline, pipeline.quality.freshness === 'stale' ? 'Relevé trop ancien' : 'Horloge non fiable'),
      detail: `${nonLiveBoundary(pipeline)}Le relevé n’est pas assez récent pour confirmer l’état courant.`,
    };
  }
  if (pipeline.quality.freshness === 'late') {
    return {
      kind: 'stale',
      label: scopedBoundaryLabel(pipeline, 'Relevé tardif'),
      detail: `${nonLiveBoundary(pipeline)}Le relevé n’est pas assez récent pour confirmer l’état courant.`,
    };
  }
  if (pipeline.quality.coverage !== 'complete') {
    return {
      kind: 'partial',
      label: scopedBoundaryLabel(pipeline, 'Relevé incomplet'),
      detail: `${nonLiveBoundary(pipeline)}Le relevé ne couvre pas toutes les étapes ; la livraison n’est pas confirmée.`,
    };
  }
  switch (pipeline.quality.evidenceKind) {
    case 'simulation': return { kind: 'simulation', label: 'Démonstration', detail: 'Cette démonstration ne représente pas l’état réel.' };
    case 'historical': return { kind: 'historical', label: 'Relevé antérieur', detail: 'Ce relevé antérieur ne représente pas l’état actuel.' };
    case 'live': return { kind: 'live', label: 'Système réel', detail: 'Relevé déclaré par le service ; sa fraîcheur est indiquée séparément.' };
  }
}

function scopedBoundaryLabel(pipeline: Pipeline, boundary: string): string {
  if (pipeline.quality.evidenceKind === 'simulation') return `Démonstration · ${boundary.toLocaleLowerCase('fr-FR')}`;
  if (pipeline.quality.evidenceKind === 'historical') return `Relevé antérieur · ${boundary.toLocaleLowerCase('fr-FR')}`;
  return boundary;
}

function nonLiveBoundary(pipeline: Pipeline): string {
  if (pipeline.quality.evidenceKind === 'simulation') return 'Cette démonstration ne représente pas l’état réel. ';
  if (pipeline.quality.evidenceKind === 'historical') return 'Ce relevé antérieur ne représente pas l’état actuel. ';
  return '';
}

function unavailableFocus(loading: boolean): ProofFocus {
  return {
    pipelineId: null,
    environment: null,
    verdict: {
      headline: loading ? 'Relevé en cours de lecture.' : 'État de livraison indisponible.',
      detail: loading
        ? 'Aucun verdict n’est calculé avant la réception du premier relevé.'
        : 'Aucun relevé exploitable n’est disponible. Aucune santé ni rupture n’est déduite.',
    },
    firstBreak: null,
    upstream: { stages: [], label: 'Aucune étape amont observée' },
    focus: {
      kind: 'unavailable',
      stage: null,
      label: 'Étape indisponible',
      cause: loading ? 'En attente du premier relevé.' : 'Le service ne fournit aucun relevé exploitable.',
      evidence: 'Attendu · Relevé du service',
    },
    downstream: {
      stage: 'destination',
      label: 'Destination',
      status: 'not-established',
      detail: 'Non confirmée sans relevé.',
      observedAt: null,
    },
    proofScope: {
      kind: 'unavailable',
      label: 'Preuve non établie',
      detail: 'Aucun relevé conservé en session.',
    },
    timestamp: { value: null, label: 'Aucune observation' },
    cta: { kind: 'refresh', label: loading ? 'Relancer la lecture' : 'Actualiser' },
  };
}

function emptyFocus(overview: Overview, cached: boolean): ProofFocus {
  const unavailable = unavailableFocus(false);
  const evidenceScope = emptyEvidenceScope(overview);
  const inventoryEstablished = !cached
    && overview.sources.length > 0
    && overview.sources.every((source) => source.status === 'available');
  return {
    ...unavailable,
    verdict: {
      headline: inventoryEstablished ? 'Aucun pipeline observé.' : 'Présence des pipelines non établie.',
      detail: inventoryEstablished
        ? 'L’absence de pipeline ne constitue pas une preuve de livraison.'
        : 'Le relevé disponible ne permet pas de confirmer la présence des pipelines.',
    },
    focus: {
      ...unavailable.focus,
      label: inventoryEstablished ? 'Aucune étape à examiner' : 'Inventaire non établi',
      cause: inventoryEstablished
        ? 'Le relevé ne contient aucune pipeline.'
        : 'Les sources ne permettent pas d’établir l’inventaire courant.',
    },
    proofScope: cached
      ? { kind: 'cached', label: 'Relevé conservé · hors ligne', detail: 'Inventaire conservé, non actuel.' }
      : evidenceScope,
    timestamp: { value: overview.generatedAt, label: cached ? 'Dernier relevé conservé' : 'Observation du relevé' },
  };
}

function emptyEvidenceScope(overview: Overview): ProofFocus['proofScope'] {
  if (overview.sources.length === 0) {
    return {
      kind: 'unavailable',
      label: 'Preuve non établie',
      detail: 'Aucune source ne permet de confirmer la couverture du relevé.',
    };
  }
  const availableSources = overview.sources.filter((source) => source.status === 'available').length;
  if (availableSources === 0) {
    return {
      kind: 'unavailable',
      label: 'Sources indisponibles',
      detail: 'Aucune source ne permet d’établir un inventaire courant.',
    };
  }
  if (availableSources !== overview.sources.length) {
    return {
      kind: 'partial',
      label: 'Sources partiellement indisponibles',
      detail: 'La couverture des sources ne permet pas d’établir un inventaire exhaustif.',
    };
  }
  const kinds = new Set(overview.sources.map((source) => source.evidenceKind));
  if (kinds.size > 1) {
    return {
      kind: 'mixed',
      label: 'Natures mixtes',
      detail: 'Plusieurs natures de relevé coexistent ; elles ne confirment pas un état réel global.',
    };
  }
  switch (kinds.values().next().value as EvidenceKind | undefined) {
    case 'simulation': return { kind: 'simulation', label: 'Démonstration', detail: 'Cette démonstration ne représente pas l’état réel.' };
    case 'historical': return { kind: 'historical', label: 'Relevé antérieur', detail: 'Ce relevé antérieur ne représente pas l’état actuel.' };
    case 'live': return { kind: 'live', label: 'Système réel', detail: 'Relevé déclaré par le service ; sa fraîcheur est indiquée séparément.' };
    default: return { kind: 'unavailable', label: 'Preuve non établie', detail: 'Aucune chaîne de livraison n’est visible.' };
  }
}

function pipelineSourceAvailability(sources: Overview['sources'], pipeline: Pipeline): PipelineSourceAvailability {
  const candidates = sources.filter((source) => source.environment === pipeline.environment);
  if (candidates.length === 0) return 'unmapped';
  const availableCount = candidates.filter((source) => source.status === 'available').length;
  if (availableCount === 0) return 'unavailable';
  if (availableCount !== candidates.length) return 'partial';
  const candidateKinds = new Set(candidates.map((source) => source.evidenceKind));
  if (candidateKinds.size !== 1 || !candidateKinds.has(pipeline.quality.evidenceKind)) return 'mismatch';
  return 'available';
}

function isCachedState(state: Exclude<ControlPlaneState, { status: 'loading' | 'failed' }>): boolean {
  return state.status === 'degraded' || state.connection === 'offline' || state.connection === 'reconnecting';
}

function stageReference(stage: StageId): string {
  switch (stage) {
    case 'source': return 'la source';
    case 'capture': return 'la lecture';
    case 'raw': return 'la réception';
    case 'load': return 'la copie initiale';
    case 'destination': return 'la destination';
  }
}
