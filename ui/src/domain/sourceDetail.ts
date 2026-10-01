import type {
  FluxIdentity,
  JournalCheckpoint,
  JournalPosition,
  Pipeline,
  RunProjection,
  Stage,
  StageId,
} from './controlPlane.ts';
import { age, formatDuration, position, sequences, timestamp } from './format.ts';
import type { BoardTone } from './liveBoard.ts';
import { resumeReadiness } from './liveBoard.ts';
import { stageLabel, stageStatusLabel } from './pipelineDetail.ts';

/**
 * View-model du détail Source (L1/L2/L3) — extension S5 null-tolérante.
 *
 * Trois cas sont distingués partout, jamais fusionnés :
 * - extension absente du relevé (champ undefined/null) → « Non instrumenté » ;
 * - bloc servi mais membre absent → « non mesuré » / « inconnu » ;
 * - valeur mesurée → affichée telle quelle, jamais arrondie ni verdée.
 *
 * Le seul vert de la page est `apply HH:MM:SS` : la seule preuve mesurée de
 * livraison destination. Un checkpoint ou un compteur n'est pas une arrivée.
 */

export const NOT_INSTRUMENTED = 'Non mesuré';

const clockUtcFormat = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZone: 'UTC',
});

/** HH:MM:SS UTC — l'horodatage de preuve, jamais une heure locale. */
export function clockUtc(iso: string): string {
  return clockUtcFormat.format(new Date(iso));
}

export function checkpointGlyph(checkpoint: JournalCheckpoint): string {
  return `${checkpoint.receiver}:${sequences(checkpoint.sequence)}`;
}

/* ------------------------------------------------------------------ */
/* Glyphe position journal — checkpoint → tail + fenêtre receiver       */
/* ------------------------------------------------------------------ */

export interface PositionGlyph {
  /** `false` = extension S5 absente du relevé (non instrumenté). */
  readonly instrumented: boolean;
  /** Forme canonique : `receiver:seq → receiver:seq`. */
  readonly value: string;
  /** Fenêtre du receiver actif quand elle est publiée, sinon null. */
  readonly window: string | null;
  /** Pourquoi la position est incomplète — jamais masquée. */
  readonly reason: string | null;
}

export function positionGlyph(pipeline: Pipeline): PositionGlyph {
  const projected = pipeline.position ?? null;
  if (projected === null) {
    // Avant S5, le checkpoint runtime reste une mesure servie ; le bloc de
    // reprise ou la sonde catalogue peuvent déjà servir le tail courant.
    const runtime = pipeline.fleetRuntime?.checkpoint ?? pipeline.resume?.checkpoint ?? null;
    const tail = pipeline.resume?.tail ?? pipeline.fleetPlan?.cutoverCheckpoint ?? null;
    if (runtime !== null && tail !== null) {
      return {
        instrumented: true,
        value: `${checkpointGlyph(runtime)} → ${checkpointGlyph(tail)}`,
        window: null,
        reason: 'fin du journal relevée en continu',
      };
    }
    if (runtime !== null) {
      return {
        instrumented: false,
        value: `${checkpointGlyph(runtime)} → fin inconnue`,
        window: null,
        reason: 'fin du journal non mesurée — mesure non publiée par le service',
      };
    }
    return {
      instrumented: false,
      value: NOT_INSTRUMENTED,
      window: null,
      reason: 'position journal non mesurée — mesure non publiée par le service',
    };
  }
  const window = receiverWindow(projected);
  const checkpoint = projected.checkpoint;
  // Le bloc resume porte la fin du journal mesurée par la sonde catalogue —
  // plus fraîche que le tail du relevé figé quand la capture est parquée.
  const tail = pipeline.resume?.tail ?? projected.sourceTail;
  if (checkpoint === null && tail === null) {
    return {
      instrumented: true,
      value: 'Inconnu — bornes non publiées',
      window,
      reason: 'checkpoint et fin du journal absents du relevé',
    };
  }
  return {
    instrumented: true,
    value: `${checkpoint === null ? 'inconnu' : checkpointGlyph(checkpoint)} → ${tail === null ? 'fin inconnue' : checkpointGlyph(tail)}`,
    window,
    reason: checkpoint === null || tail === null ? 'borne manquante dans le relevé' : null,
  };
}

function receiverWindow(position: JournalPosition): string | null {
  const first = position.receiverFirstSequence;
  const last = position.receiverLastSequence;
  if (first === null && last === null) return null;
  const low = first === null ? 'inconnue' : sequences(first);
  const high = last === null ? 'inconnue' : sequences(last);
  return `séquences ${low} → ${high}`;
}

/** Lignes L3 de la position — même distinction instrumenté/mesuré. */
export function positionRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const projected = pipeline.position ?? null;
  if (projected === null) {
    const runtime = pipeline.fleetRuntime?.checkpoint ?? pipeline.resume?.checkpoint ?? null;
    const resumeTail = pipeline.resume?.tail ?? pipeline.fleetPlan?.cutoverCheckpoint ?? null;
    return [
      ['Checkpoint de lecture', runtime === null ? 'Inconnu — exécution non projetée' : `${position(runtime.receiver, runtime.sequence)} (runtime)`],
      ['Fin du journal', resumeTail === null ? 'Non mesurée — mesure non publiée par le service' : `${position(resumeTail.receiver, resumeTail.sequence)} (relevé continu)`],
      ['Séquences du receiver', 'Non mesurées — mesure non publiée par le service'],
    ];
  }
  const tail = pipeline.resume?.tail ?? projected.sourceTail;
  return [
    ['Checkpoint de lecture', projected.checkpoint === null ? 'Inconnu — non publié' : position(projected.checkpoint.receiver, projected.checkpoint.sequence)],
    ['Fin du journal', tail === null ? 'Inconnu — non publié' : position(tail.receiver, tail.sequence)],
    ['Première séquence du receiver', projected.receiverFirstSequence === null ? 'Inconnu — non publiée' : sequences(projected.receiverFirstSequence)],
    ['Dernière séquence du receiver', projected.receiverLastSequence === null ? 'Inconnu — non publiée' : sequences(projected.receiverLastSequence)],
  ];
}

/* ------------------------------------------------------------------ */
/* Tampon apply — la seule preuve verte de livraison                    */
/* ------------------------------------------------------------------ */

export interface ApplyStamp {
  /** `apply 09:42:13` quand `destination.observed_at` est mesuré. */
  readonly label: string;
  readonly measured: boolean;
  readonly at: string | null;
  readonly reason: string | null;
}

const DESTINATION_REASON_LABELS: Readonly<Record<string, string>> = {
  destination_not_attached: 'destination non rattachée au relevé',
  destination_invalid: 'bloc destination illisible',
};

export function destinationReasonLabel(reason: string | null | undefined): string {
  if (reason === undefined || reason === null) return 'raison non déclarée';
  return DESTINATION_REASON_LABELS[reason] ?? reason;
}

export function destinationApplyStamp(pipeline: Pipeline): ApplyStamp {
  const destination = pipeline.destination ?? null;
  if (destination !== null && destination.observedAt !== null) {
    return {
      label: `livré ${clockUtc(destination.observedAt)}`,
      measured: true,
      at: destination.observedAt,
      reason: null,
    };
  }
  if (destination !== null) {
    return {
      label: 'date non observée',
      measured: false,
      at: null,
      reason: 'relevé destination servi sans date d’application',
    };
  }
  if (pipeline.destination === null) {
    return {
      label: 'date non servie',
      measured: false,
      at: null,
      reason: destinationReasonLabel(pipeline.destinationReason),
    };
  }
  return {
    label: 'date non servie',
    measured: false,
    at: null,
    reason: 'mesure non publiée par le service',
  };
}

/** Watermark L1 : apply mesuré si S5 le sert, sinon le compteur de fenêtre. */
export function destinationStamp(pipeline: Pipeline, current: boolean): {
  readonly value: string;
  readonly hint: string | null;
  readonly tone: BoardTone;
} {
  const stamp = destinationApplyStamp(pipeline);
  if (stamp.measured) {
    const namespace = pipeline.fleetPlan?.destinationNamespace ?? pipeline.fleet?.destinationNamespace ?? null;
    return {
      value: stamp.label,
      hint: namespace === null ? 'observation destination' : `${namespace} · observation destination`,
      tone: current ? 'positive' : 'muted',
    };
  }
  const inTarget = pipeline.counters.events_in_target ?? null;
  const namespace = pipeline.fleetPlan?.destinationNamespace ?? pipeline.fleet?.destinationNamespace ?? null;
  if (inTarget !== null) {
    return {
      value: `${sequences(inTarget)} événement${inTarget > 1 ? 's' : ''} constaté${inTarget > 1 ? 's' : ''}`,
      hint: `${stamp.label} · ${namespace === null ? 'cumul constaté — pas une réconciliation' : `${namespace} · cumul constaté — pas une réconciliation`}`,
      tone: 'active',
    };
  }
  if (namespace !== null) {
    return {
      value: namespace,
      hint: `${stamp.label} · ${stamp.reason ?? 'compteur de livraison non servi'}`,
      tone: 'muted',
    };
  }
  return { value: 'Destination · non datée', hint: stamp.reason, tone: 'muted' };
}

/* ------------------------------------------------------------------ */
/* Chaîne des cinq étapes — preuves, âges, apply = seul vert            */
/* ------------------------------------------------------------------ */

export interface StageChainStep {
  readonly id: StageId;
  readonly label: string;
  readonly statusLabel: string;
  /** Ce que l'étape prouve — headline projetée, jamais reformulée. */
  readonly evidence: string;
  readonly detail: string;
  readonly observedAt: string | null;
  readonly ageLabel: string;
  /** `apply HH:MM:SS` — seule la destination le porte, seul vert mesuré. */
  readonly apply: string | null;
  readonly tone: BoardTone;
}

export function stageChain(pipeline: Pipeline, now: Date, current: boolean): readonly StageChainStep[] {
  const stamp = destinationApplyStamp(pipeline);
  const ready = resumeReadiness(pipeline) === 'ready';
  return pipeline.stages.map((stage) => chainStep(stage, now, current, stamp, ready));
}

function chainStep(stage: Stage, now: Date, current: boolean, stamp: ApplyStamp, ready: boolean): StageChainStep {
  const apply = stage.id === 'destination' && stamp.measured ? stamp.label : null;
  const ageLabel = stage.observedAt === null ? 'jamais observée' : `observée ${age(stage.observedAt, now).label}`;
  let tone: BoardTone;
  if (apply !== null) {
    tone = current ? 'positive' : 'muted';
  } else if (stage.status === 'incident' || stage.status === 'degraded') {
    tone = 'attention';
  } else if (stage.status === 'healthy' && stage.observedAt !== null) {
    tone = current ? 'active' : 'muted';
  } else {
    tone = 'muted';
  }
  return {
    id: stage.id,
    label: stageLabel(stage.id),
    // « Disponible » affirme une disponibilité courante — sur un relevé figé,
    // l'étape saine n'est qu'observée, pas prouvée à l'instant présent.
    // Une capture arrêtée dont la cause est résolue attend le relancement.
    statusLabel: ready && stage.id === 'capture' && (stage.status === 'incident' || stage.status === 'awaiting_resume')
      ? 'À reprendre'
      : !current && stage.status === 'healthy'
        ? 'Observée'
        : stageStatusLabel(stage.status),
    // Quand le statut porte déjà « À reprendre », la preuve montre le détail.
    evidence: ready && stage.id === 'capture' && (stage.status === 'incident' || stage.status === 'awaiting_resume')
      ? stage.detail
      : stage.headline,
    detail: stage.detail,
    observedAt: stage.observedAt,
    ageLabel,
    apply,
    tone,
  };
}

/* ------------------------------------------------------------------ */
/* Verdict lag projeté — déclaré par le worker, jamais déduit           */
/* ------------------------------------------------------------------ */

export interface ProjectedLagVerdict {
  readonly label: string;
  /** Raison quand le verdict n'est pas mesuré. */
  readonly reason: string | null;
  readonly instrumented: boolean;
}

const PROJECTED_LAG_VERDICTS: Readonly<Record<string, string>> = {
  STABLE: 'Stable',
  BOUNDED: 'Borné',
  CATCHING_UP: 'Rattrapage',
  DIVERGING: 'Divergence',
};

const LAG_VERDICT_REASONS: Readonly<Record<string, string>> = {
  verdict_not_declared: 'verdict non déclaré par le worker',
  verdict_out_of_contract: 'verdict hors contrat',
};

export function projectedLagVerdict(pipeline: Pipeline): ProjectedLagVerdict {
  if (pipeline.lagVerdict === undefined && pipeline.lagVerdictReason === undefined) {
    return { label: NOT_INSTRUMENTED, reason: 'mesure non publiée par le service', instrumented: false };
  }
  const verdict = pipeline.lagVerdict ?? null;
  if (verdict !== null) {
    return { label: PROJECTED_LAG_VERDICTS[verdict] ?? verdict, reason: null, instrumented: true };
  }
  const reason = pipeline.lagVerdictReason ?? null;
  return {
    label: 'Indéterminé',
    reason: reason === null ? 'raison non déclarée' : (LAG_VERDICT_REASONS[reason] ?? reason),
    instrumented: true,
  };
}

/* ------------------------------------------------------------------ */
/* Exécution projetée — run.state, démarrage, diagnostic, pause         */
/* ------------------------------------------------------------------ */

const RUN_STATE_LABELS: Readonly<Record<string, string>> = {
  RUNNING: 'En cours',
  PAUSED_SOURCE: 'Pause source',
  STOPPED_BUDGET: 'Arrêt budget',
  STOPPED_PROOF_CHAIN: 'Arrêt — fenêtres clôturées',
  STOPPED_FAIL_CLOSED: 'Arrêt en sécurité',
  STOPPED_AUTH_BLOCKED: 'Authentification bloquée',
};

export function runStateLabel(state: string | null): string {
  if (state === null) return 'Inconnu — état non publié';
  return RUN_STATE_LABELS[state] ?? state;
}

/** Lignes L3 du run — présentes même quand le bloc n'est pas instrumenté. */
export function runDetailRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const run = pipeline.run ?? null;
  if (run === null) {
    return [
      ['État du run', 'Non mesuré — mesure non publiée par le service'],
      ['Démarrage', NOT_INSTRUMENTED],
      ['Durée mesurée', 'Non mesurée'],
      ['Cause d’arrêt', 'Non mesurée'],
      ['Dernier diagnostic', NOT_INSTRUMENTED],
      ['Pause source', 'Non mesurée'],
    ];
  }
  return [
    ['État du run', runStateLabel(run.state)],
    ['Démarrage', run.startedAt === null ? 'Inconnu — non mesuré' : `${timestamp(run.startedAt)}`],
    ['Durée mesurée', run.elapsedSeconds === null ? 'Inconnue — non mesurée' : formatDuration(run.elapsedSeconds)],
    ['Cause d’arrêt', run.stoppedBecause ?? 'Aucune déclarée'],
    ['Dernier diagnostic', diagnosticCopy(run)],
    ['Pause source', sourcePauseCopy(run)],
  ];
}

function diagnosticCopy(run: RunProjection): string {
  const diagnostic = run.diagnostic;
  if (diagnostic === null) return 'Aucun diagnostic publié';
  const parts = [
    diagnostic.type === null ? null : `type ${diagnostic.type}`,
    diagnostic.head,
    diagnostic.at === null ? null : `relevé ${timestamp(diagnostic.at)}`,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? 'Diagnostic publié sans contenu projetable' : parts.join(' · ');
}

function sourcePauseCopy(run: RunProjection): string {
  const pause = run.sourcePause;
  if (pause === null) return 'Aucune pause source déclarée';
  const parts = [
    pause.retryAfter === null ? null : `reprise après ${timestamp(pause.retryAfter)}`,
    pause.reasonCode === null ? null : `code ${pause.reasonCode}`,
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? 'Pause déclarée sans échéance projetable' : parts.join(' · ');
}

/* ------------------------------------------------------------------ */
/* Identité du flux — journal, objets, lecteur, job                     */
/* ------------------------------------------------------------------ */

export function fluxIdentityRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const flux = pipeline.flux ?? null;
  if (flux === null) {
    return [
      ['Flux', 'Non mesuré — mesure non publiée par le service'],
      ['Journal', NOT_INSTRUMENTED],
      ['Objets déclarés', NOT_INSTRUMENTED],
      ['Chemin de lecture', NOT_INSTRUMENTED],
      ['Cible', 'Non mesurée'],
      ['Job', NOT_INSTRUMENTED],
    ];
  }
  return [
    ['Flux', [flux.label, flux.id].filter((part): part is string => part !== null).join(' · ') || 'Inconnu — non publié'],
    ['Journal', journalCopy(flux)],
    ['Objets déclarés', flux.objects === null ? 'Inconnus — non publiés' : (flux.objects.length === 0 ? 'Aucun objet déclaré' : flux.objects.join(', '))],
    ['Chemin de lecture', flux.readerPath ?? 'Inconnu — non publié'],
    ['Cible', flux.target ?? 'Inconnue — non publiée'],
    ['Job', flux.job ?? 'Inconnu — non publié'],
  ];
}

function journalCopy(flux: FluxIdentity): string {
  if (flux.journal === null && flux.journalLibrary === null) return 'Inconnu — non publié';
  if (flux.journal === null) return flux.journalLibrary!;
  if (flux.journalLibrary === null) return flux.journal;
  return `${flux.journalLibrary}/${flux.journal}`;
}

/** Couverture d'une table par les objets déclarés du flux — L2. */
export function fluxObjectCoverage(flux: FluxIdentity | null | undefined, table: string): string {
  if (flux === undefined || flux === null) {
    return 'Non mesurée — identité de la liaison absente du relevé';
  }
  const objects = flux.objects;
  if (objects === null) {
    return 'Non mesurée — contenu de la liaison non publié';
  }
  const needle = table.toUpperCase();
  const covered = objects.some((object) => {
    const candidate = object.toUpperCase();
    return candidate === needle || candidate.endsWith(`.${needle}`) || candidate.endsWith(`/${needle}`);
  });
  return covered ? 'Déclarée dans la liaison' : 'Hors périmètre déclaré';
}

/* ------------------------------------------------------------------ */
/* Destination — checkpoints, comptes, run tag (L3)                     */
/* ------------------------------------------------------------------ */

export function destinationRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const destination = pipeline.destination ?? null;
  if (destination === null) {
    const reason = pipeline.destination === null
      ? destinationReasonLabel(pipeline.destinationReason)
      : 'mesure non publiée par le service';
    return [
      ['Bloc destination', pipeline.destination === null ? `Non rattachée — ${reason}` : `Non mesurée — ${reason}`],
      ['Checkpoint de chargement', NOT_INSTRUMENTED],
      ['Checkpoint d’application', NOT_INSTRUMENTED],
      ['Événements source', NOT_INSTRUMENTED],
      ['Lignes raw', NOT_INSTRUMENTED],
      ['Lignes canoniques', NOT_INSTRUMENTED],
      ['Doublons', NOT_INSTRUMENTED],
    ];
  }
  return [
    ['Observation destination', destination.observedAt === null ? 'Inconnue — non mesurée' : timestamp(destination.observedAt)],
    ['Nature', destination.kind ?? 'Inconnue — non publiée'],
    ['Base', destination.database ?? 'Inconnue — non publiée'],
    ['Schéma', destination.schema ?? 'Inconnu — non publié'],
    ['Stage', destination.stage ?? 'Inconnu — non publié'],
    ['Table raw', destination.rawTable ?? 'Inconnue — non publiée'],
    ['Table canonique', destination.canonicalTable ?? 'Inconnue — non publiée'],
    ['Run tag', destination.runTag ?? 'Inconnu — non publié'],
    ['Checkpoint de chargement', destinationCheckpointCopy(destination.loadCheckpoint)],
    ['Checkpoint d’application', destinationCheckpointCopy(destination.applyCheckpoint)],
    ['Événements source', countCopy(destination.sourceEvents)],
    ['Lignes raw', countCopy(destination.rawRows)],
    ['Lignes canoniques', countCopy(destination.canonicalRows)],
    ['Doublons', countCopy(destination.duplicates)],
  ];
}

function destinationCheckpointCopy(checkpoint: JournalCheckpoint | null): string {
  return checkpoint === null ? 'Inconnu — non mesuré' : position(checkpoint.receiver, checkpoint.sequence);
}

function countCopy(value: number | null): string {
  return value === null ? 'Inconnu — non mesuré' : sequences(value);
}

/** Registre L3 : toutes les lignes destination, regroupées par section. */
export function destinationDetailSections(pipeline: Pipeline): {
  readonly applyStamp: ApplyStamp;
  readonly rows: ReadonlyArray<readonly [string, string]>;
} {
  return { applyStamp: destinationApplyStamp(pipeline), rows: destinationRows(pipeline) };
}
