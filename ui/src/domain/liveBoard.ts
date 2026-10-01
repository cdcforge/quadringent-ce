import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import type {
  EvidenceKind,
  FleetPlanTable,
  FleetRuntimeTablePhase,
  IncidentType,
  Pipeline,
  Source,
} from './controlPlane.ts';
import { age, decimal, formatDuration, position, sequences, timestamp } from './format.ts';
import { VERDICT_COPY, lagTrend } from './lag.ts';
import { buildLagSegments } from './pipelineDetail.ts';
import { pipelineIssue, prioritizePipelines } from './pipelineView.ts';
import { resolvePipelineProofFocus } from './proofFocus.ts';
import { sourceAvailability } from './scope.ts';
import { runtimePhaseLabel } from './fleetView.ts';

/**
 * View-model du plateau Sources (L0).
 *
 * Tout ce qui est calculé ici est une lecture honnête de la projection :
 * - null devient « Inconnu — {raison} », jamais 0 ;
 * - le débit est dérivé entre relevés (Δ events_published / Δ observed_at) avec
 *   fenêtre toujours affichée ; une régression du compteur est un redémarrage ;
 * - le vert (positive) n'est rendu que pour une mesure live + fresh + source
 *   disponible + transport courant ;
 * - quand le relevé est conservé, périmé ou gelé, les vitals sont figés et
 *   qualifiés « dernière observation ».
 */

export type Transport = 'live' | 'retained' | 'frozen';
export type BoardTone = 'positive' | 'active' | 'attention' | 'muted';

export interface CounterSample {
  readonly events: number | null;
  readonly at: string;
  /** Dernier débit mesuré de la session — conservé quand le compteur gèle,
   *  pour dire « dernier débit mesuré avant gel » au lieu de « non mesuré ». */
  readonly lastRate?: { readonly value: string; readonly at: string } | null;
}

export function transportFor(state: ControlPlaneState, frozen: boolean): Transport {
  if (frozen) return 'frozen';
  // Un rafraîchissement en cours n'interrompt pas le canal : le relevé
  // conservé seconde le temps de lire, la connexion reste ouverte.
  const live =
    (state.status === 'ready' || state.status === 'refreshing') && state.connection === 'live';
  return live ? 'live' : 'retained';
}

/* ------------------------------------------------------------------ */
/* Débit dérivé — fenêtre toujours affichée                            */
/* ------------------------------------------------------------------ */

export interface ThroughputView {
  readonly kind: 'measured' | 'idle' | 'restarted' | 'single' | 'unknown';
  readonly value: string;
  readonly window: string | null;
  readonly note: string | null;
  readonly tone: BoardTone;
}

function windowLabel(previousAt: string, currentAt: string): string | null {
  const seconds = Math.round((Date.parse(currentAt) - Date.parse(previousAt)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 1) return 'fenêtre < 1 s';
  return `fenêtre ${formatDuration(seconds)}`;
}

export function deriveThroughput(
  previous: CounterSample | undefined,
  current: CounterSample,
): ThroughputView {
  if (current.events === null) {
    return { kind: 'unknown', value: 'Inconnu — compteur non publié', window: null, note: null, tone: 'muted' };
  }
  if (previous === undefined) {
    return {
      kind: 'single',
      value: 'Non calculable — un seul relevé',
      window: null,
      note: `compteur ${sequences(current.events)} publiés au relevé`,
      tone: 'muted',
    };
  }
  const window = windowLabel(previous.at, current.at);
  if (previous.events === null) {
    return {
      kind: 'unknown',
      value: 'Non calculable — compteur absent du relevé précédent',
      window,
      note: null,
      tone: 'muted',
    };
  }
  const seconds = (Date.parse(current.at) - Date.parse(previous.at)) / 1000;
  if (!(seconds > 0)) {
    return {
      kind: 'unknown',
      value: 'Non calculable — relevés au même instant',
      window,
      note: null,
      tone: 'muted',
    };
  }
  const delta = current.events - previous.events;
  if (delta < 0) {
    return {
      kind: 'restarted',
      value: 'Redémarrage observé',
      window,
      note: `compteur repris à ${sequences(current.events)}`,
      tone: 'attention',
    };
  }
  if (delta === 0) {
    return {
      kind: 'idle',
      value: '0 événement',
      window,
      note: `total ${sequences(current.events)} publiés`,
      tone: 'muted',
    };
  }
  const perSecond = delta / seconds;
  const rate =
    perSecond >= 100 ? sequences(Math.round(perSecond))
    : Number.isInteger(perSecond) ? sequences(perSecond)
    : perSecond >= 1 ? decimal(perSecond, 1)
    : decimal(perSecond, 2);
  return {
    kind: 'measured',
    value: `${rate} év/s`,
    window,
    note: `Δ +${sequences(delta)} · total ${sequences(current.events)} publiés`,
    tone: 'active',
  };
}

/* ------------------------------------------------------------------ */
/* Retard — lag_sequences + verdict + trous                            */
/* ------------------------------------------------------------------ */

export interface LagView {
  readonly value: string;
  readonly verdict: string;
  readonly severe: boolean;
  readonly gaps: string | null;
  readonly tone: BoardTone;
}

export function lagVitals(pipeline: Pipeline): LagView {
  const segment = buildLagSegments(pipeline.lagSeries).at(-1) ?? [];
  const values = segment.map((point) => point.lag!);
  const trend = lagTrend(values);
  const copy = VERDICT_COPY[trend.verdict];
  const severe = copy.severe;
  const gapCount = pipeline.lagSeries.filter(
    (point) => point.coverage === 'gap' || point.kind === 'temporal_gap',
  ).length;
  const gaps = gapCount > 0 ? `${gapCount} intervalle${gapCount > 1 ? 's' : ''} sans mesure` : null;
  const verdict = copy.label;

  if (pipeline.lagSequences === null) {
    return {
      value: 'Inconnu — non mesuré',
      verdict: gaps ? `${verdict} · relevé partiel` : verdict,
      severe,
      gaps,
      tone: 'muted',
    };
  }
  return {
    value: `${sequences(pipeline.lagSequences)} séquence${pipeline.lagSequences > 1 ? 's' : ''} de retard`,
    verdict,
    severe,
    gaps,
    tone: severe ? 'attention' : 'active',
  };
}

/* ------------------------------------------------------------------ */
/* Disponibilité de la source rattachée au pipeline                     */
/* ------------------------------------------------------------------ */

export interface PipelineSourceAvailability {
  readonly kind: 'available' | 'partial' | 'unavailable' | 'unmapped';
  readonly label: string;
  readonly hint: string | null;
  readonly tone: BoardTone;
}

export function sourceAvailabilityFor(pipeline: Pipeline, sources: readonly Source[]): PipelineSourceAvailability {
  const mapped = sources.filter((source) => source.environment === pipeline.environment);
  if (mapped.length === 0) {
    return {
      kind: 'unmapped',
      label: 'Inconnue — aucune source déclarée',
      hint: null,
      tone: 'muted',
    };
  }
  const down = mapped.filter((source) => source.status !== 'available').length;
  // « available » signifie endpoint joignable — l'authentification et la
  // lecture ne sont pas mesurées par ce statut. Les couches sont nommées :
  // le réseau répond ≠ la source accepte la connexion (cas
  // capture_auth_blocked : réseau joignable, authentification refusée).
  if (down === 0) {
    return {
      kind: 'available',
      label: 'Réseau joignable',
      hint:
        resumeReadiness(pipeline) === 'ready'
          ? 'connexion re-vérifiée — capture en pause'
          : pipeline.incident?.type === 'capture_auth_blocked'
            ? 'authentification refusée — couche distincte'
            : 'authentification non mesurée ici',
      tone: 'active',
    };
  }
  if (down === mapped.length) {
    return {
      kind: 'unavailable',
      label: 'Réseau injoignable',
      hint: 'aucune source ne répond',
      tone: 'attention',
    };
  }
  return {
    kind: 'partial',
    label: `Partielle · ${mapped.length - down}/${mapped.length} joignables`,
    hint: 'au moins une source ne répond pas',
    tone: 'attention',
  };
}

/* ------------------------------------------------------------------ */
/* Raison de rétention — pourquoi les vitals sont figés                 */
/* ------------------------------------------------------------------ */

/** Types d'incident dont la cause peut être prouvée résolue par la sonde
 *  catalogue : elle ouvre une connexion fraîche, donc son succès vaut
 *  preuve que la source accepte de nouveau la connexion. */
const RESUMABLE_INCIDENT_TYPES: ReadonlySet<IncidentType> = new Set([
  'capture_auth_blocked',
  'capture_stopped',
  'capture_connection_failure',
  'capture_timeout',
]);

/** « Prête à reprendre » : la cause de l'arrêt est prouvée disparue —
 *  champ serveur d'abord, sinon dérivé du plan frais (continuité prouvée
 *  par une sonde qui vient d'ouvrir une connexion). Jamais pour une
 *  capture qui tourne, jamais sans preuve fraîche. */
export function resumeReadiness(pipeline: Pipeline): 'ready' | null {
  const incident = pipeline.incident;
  if (incident === null) return null;
  if (pipeline.resume?.state === 'ready' || incident.causeResolved === true) return 'ready';
  if (!RESUMABLE_INCIDENT_TYPES.has(incident.type)) return null;
  if (
    pipeline.status !== 'incident' &&
    pipeline.status !== 'awaiting_resume' &&
    pipeline.status !== 'planned_stop'
  ) {
    return null;
  }
  const plan = pipeline.fleetPlan ?? null;
  if (plan === null || plan.freshness !== 'fresh' || plan.continuity !== 'proven') return null;
  return 'ready';
}

export function retainedReason(
  pipeline: Pipeline,
  transport: Transport,
  availability: PipelineSourceAvailability,
): string | null {
  if (transport === 'frozen') return 'actualisation gelée';
  if (transport === 'retained') return 'relevé conservé';
  switch (pipeline.quality.freshness) {
    case 'stale': return 'relevé trop ancien';
    case 'clock_untrusted': return 'horloge non fiable';
    case 'late': return 'relevé tardif';
    default: break;
  }
  switch (availability.kind) {
    case 'unavailable': return 'source indisponible';
    case 'partial': return 'sources partiellement indisponibles';
    case 'unmapped': return 'aucune source déclarée';
    default: return null;
  }
}

/* ------------------------------------------------------------------ */
/* État de run en un mot                                                */
/* ------------------------------------------------------------------ */

export interface RunWord {
  readonly label: string;
  readonly tone: BoardTone;
}

function boundedRunWord(label: string, tone: BoardTone, evidenceKind: EvidenceKind): RunWord {
  if (evidenceKind === 'simulation') return { label: `${label} · démonstration`, tone: 'muted' };
  if (evidenceKind === 'historical') return { label: `${label} · relevé antérieur`, tone: 'muted' };
  return { label, tone };
}

export function runStateWord(
  pipeline: Pipeline,
  opts: { readonly retained: string | null; readonly current: boolean },
): RunWord {
  if (opts.retained !== null) {
    // Le gel ne rend pas la vérité inconnue : le dernier mot mesuré reste
    // servi, désaturé — « figé » vit dans le badge et l'âge dans le
    // heartbeat, pas dans un « Inconnu » qui ferait croire à l'absence
    // de donnée.
    const word = runStateWord(pipeline, { retained: null, current: false });
    return word.tone === 'positive' || word.tone === 'active' ? { ...word, tone: 'muted' } : word;
  }
  const incident = pipeline.incident;
  if (incident !== null) {
    if (resumeReadiness(pipeline) === 'ready') {
      return boundedRunWord('Prête à reprendre', 'attention', pipeline.quality.evidenceKind);
    }
    if (incident.type === 'capture_stopped') {
      return boundedRunWord('Arrêtée en sécurité', 'muted', pipeline.quality.evidenceKind);
    }
    if (incident.type === 'destination') {
      return boundedRunWord('Incident à la destination', 'attention', pipeline.quality.evidenceKind);
    }
    return boundedRunWord('Capture interrompue', 'attention', pipeline.quality.evidenceKind);
  }
  if (pipeline.status === 'awaiting_resume') {
    return boundedRunWord('Prête à reprendre', 'attention', pipeline.quality.evidenceKind);
  }
  if (pipeline.status === 'incident') {
    return boundedRunWord('Incident déclaré', 'attention', pipeline.quality.evidenceKind);
  }
  if (pipeline.status === 'planned_stop') {
    return boundedRunWord('En pause', 'muted', pipeline.quality.evidenceKind);
  }
  if (pipeline.status === 'recovering') {
    return boundedRunWord('Reprise en cours', 'active', pipeline.quality.evidenceKind);
  }

  const phase = pipeline.fleetRuntime?.phase ?? null;
  if (phase !== null) {
    const toneFor = (positive: BoardTone, muted: BoardTone = 'muted'): BoardTone =>
      opts.current ? positive : muted;
    switch (phase) {
      case 'LIVE': return boundedRunWord('Capture active', toneFor('positive'), pipeline.quality.evidenceKind);
      case 'CERTIFIED': return boundedRunWord('Certifiée', toneFor('positive'), pipeline.quality.evidenceKind);
      case 'CATCHING_UP': return boundedRunWord('Rattrapage', toneFor('active'), pipeline.quality.evidenceKind);
      case 'RECONCILING': return boundedRunWord('Vérification', toneFor('active'), pipeline.quality.evidenceKind);
      case 'HISTORICAL': return boundedRunWord('Copie initiale', toneFor('active'), pipeline.quality.evidenceKind);
      case 'PREPARED': return boundedRunWord('Prête à démarrer', 'muted', pipeline.quality.evidenceKind);
      case 'NOT_PREPARED': return boundedRunWord('À préparer', 'muted', pipeline.quality.evidenceKind);
      case 'PAUSED': return boundedRunWord('En pause', 'muted', pipeline.quality.evidenceKind);
      case 'BLOCKED': return boundedRunWord('Bloquée', 'attention', pipeline.quality.evidenceKind);
      default: return boundedRunWord('Inconnu — phase non mesurée', 'muted', pipeline.quality.evidenceKind);
    }
  }

  const captureStage = pipeline.stages.find((stage) => stage.id === 'capture') ?? null;
  if (captureStage?.status === 'planned_stop') {
    return boundedRunWord('En pause', 'muted', pipeline.quality.evidenceKind);
  }
  if (captureStage?.status === 'healthy') {
    return boundedRunWord(
      'Capture active',
      opts.current ? 'positive' : 'muted',
      pipeline.quality.evidenceKind,
    );
  }
  return boundedRunWord('Inconnu — exécution non projetée', 'muted', pipeline.quality.evidenceKind);
}

/* ------------------------------------------------------------------ */
/* Badges de portée                                                     */
/* ------------------------------------------------------------------ */

export interface ScopeBadge {
  readonly label: string;
  readonly tone: 'frozen' | 'attention' | 'muted';
}

export function scopeBadges(
  pipeline: Pipeline,
  transport: Transport,
  availability: PipelineSourceAvailability,
): ScopeBadge[] {
  const badges: ScopeBadge[] = [];
  if (transport === 'frozen') badges.push({ label: 'Actualisation gelée', tone: 'frozen' });
  else if (transport === 'retained') badges.push({ label: 'Relevé conservé', tone: 'frozen' });
  switch (pipeline.quality.freshness) {
    case 'stale': badges.push({ label: 'Relevé trop ancien', tone: 'attention' }); break;
    case 'clock_untrusted': badges.push({ label: 'Horloge non fiable', tone: 'attention' }); break;
    case 'late': badges.push({ label: 'Relevé tardif', tone: 'attention' }); break;
    default: break;
  }
  if (pipeline.status === 'planned_stop') badges.push({ label: 'Arrêt planifié', tone: 'muted' });
  if (availability.kind === 'partial') badges.push({ label: 'Informations partielles', tone: 'attention' });
  if (availability.kind === 'unavailable') badges.push({ label: 'Sources indisponibles', tone: 'attention' });
  if (availability.kind === 'unmapped') badges.push({ label: 'Source non déclarée', tone: 'muted' });
  if (pipeline.quality.coverage === 'partial') badges.push({ label: 'Couverture partielle', tone: 'attention' });
  return badges;
}

/* ------------------------------------------------------------------ */
/* Grille des tables — runtime ∪ plan ∪ fleet                           */
/* ------------------------------------------------------------------ */

export interface BoardTableRow {
  readonly name: string;
  readonly phase: FleetRuntimeTablePhase | null;
  readonly phaseLabel: string;
  readonly copiedRows: number | null;
  readonly totalRows: number | null;
  readonly progress: number | null;
  /** Part du volume observé (totalRows / plus grand total mesuré) — la barre. */
  readonly volumeShare: number | null;
  readonly journalImages: FleetPlanTable['journalImages'] | null;
  readonly blockedReasons: readonly string[];
  readonly identityStatus: string | null;
  readonly admitted: boolean;
  readonly cataloguedAt: string | null;
  readonly problem: 'blocked' | 'journal' | 'unknown' | null;
  readonly paused: boolean;
  readonly verdict: { readonly label: string; readonly tone: BoardTone };
  /** PHASE+VERDICT fusionnés : le verdict ne s'affiche que s'il diverge. */
  readonly state: {
    readonly primary: string;
    readonly secondary: string | null;
    readonly tone: BoardTone;
  };
}

const BLOCKED_REASON_LABELS: Readonly<Record<string, string>> = {
  identity_unproven: 'identité non prouvée',
};

export function blockedReasonLabel(reason: string): string {
  return BLOCKED_REASON_LABELS[reason] ?? reason;
}

const PHASE_ORDER: Readonly<Record<FleetRuntimeTablePhase, number>> = {
  CERTIFIED: 7,
  RECONCILING: 6,
  LIVE: 5,
  CATCHING_UP: 4,
  HISTORICAL: 3,
  PREPARED: 2,
  READY: 2,
  NOT_PREPARED: 1,
  PAUSED: 0,
  BLOCKED: 0,
  UNKNOWN: 0,
};

function rowProblem(
  phase: FleetRuntimeTablePhase | null,
  blockedReasons: readonly string[],
  identityStatus: string | null,
  journalImages: FleetPlanTable['journalImages'] | null,
  admitted: boolean,
): 'blocked' | 'journal' | 'unknown' | null {
  if (phase === 'BLOCKED' || blockedReasons.length > 0 || identityStatus === 'blocked') return 'blocked';
  if (journalImages === '*BEFORE') return 'journal';
  if (phase === 'UNKNOWN' || (phase === null && admitted)) return 'unknown';
  return null;
}

function tableVerdict(
  phase: FleetRuntimeTablePhase | null,
  problem: 'blocked' | 'journal' | 'unknown' | null,
  blockedReasons: readonly string[],
  admitted: boolean,
  hasRuntime: boolean,
  current: boolean,
): { label: string; tone: BoardTone } {
  if (problem === 'blocked') {
    const reason = blockedReasons.length > 0 ? ` — ${blockedReasonLabel(blockedReasons[0]!)}` : '';
    return { label: `Bloquée${reason}`, tone: 'attention' };
  }
  if (problem === 'journal') return { label: 'Images non exploitables', tone: 'attention' };
  if (problem === 'unknown') {
    return { label: 'Inconnu — phase non mesurée', tone: 'muted' };
  }
  if (phase === 'PAUSED') return { label: 'Suspendue', tone: 'muted' };
  const liveTone = (tone: BoardTone): BoardTone => (current ? tone : 'muted');
  switch (phase) {
    case 'CERTIFIED': return { label: 'Certifiée', tone: liveTone('positive') };
    case 'LIVE': return { label: 'En temps réel', tone: liveTone('active') };
    case 'RECONCILING': return { label: 'Vérification en cours', tone: liveTone('active') };
    case 'CATCHING_UP': return { label: 'Rattrapage', tone: liveTone('active') };
    case 'HISTORICAL': return { label: 'Copie en cours', tone: liveTone('active') };
    case 'PREPARED':
    case 'READY': return { label: 'Prête', tone: 'muted' };
    case 'NOT_PREPARED': return { label: 'À préparer', tone: 'muted' };
    default: break;
  }
  if (!hasRuntime) {
    return admitted
      ? { label: 'Inconnu — exécution non projetée', tone: 'muted' }
      : { label: 'Hors manifeste — pas de copie prévue', tone: 'muted' };
  }
  return { label: 'Inconnu — phase non mesurée', tone: 'muted' };
}

/** PHASE+VERDICT en une cellule : le verdict seul porte la ligne ; la phase ne
 *  réapparaît qu'en seconde ligne quand le verdict diverge (problème journal
 *  sur une phase LIVE, phase inconnue, etc.). */
function mergedTableState(
  phase: FleetRuntimeTablePhase | null,
  phaseLabel: string,
  verdict: { readonly label: string; readonly tone: BoardTone },
): BoardTableRow['state'] {
  if (verdict.label === phaseLabel || verdict.label.startsWith(phaseLabel)) {
    return { primary: verdict.label, secondary: null, tone: verdict.tone };
  }
  return {
    primary: verdict.label,
    secondary: phase !== null ? `phase ${phaseLabel}` : null,
    tone: verdict.tone,
  };
}

export function tableRows(pipeline: Pipeline, current: boolean): BoardTableRow[] {
  const runtimeStates = new Map(
    (pipeline.fleetRuntime?.tableStates ?? []).map((table) => [table.name, table]),
  );
  const planTables = new Map((pipeline.fleetPlan?.tables ?? []).map((table) => [table.name, table]));
  const fleetTables = new Map((pipeline.fleet?.tables ?? []).map((table) => [table.name, table]));
  const names = new Set<string>([
    ...runtimeStates.keys(),
    ...planTables.keys(),
    ...fleetTables.keys(),
  ]);

  const merged = [...names].map((name) => {
    const runtime = runtimeStates.get(name) ?? null;
    const plan = planTables.get(name) ?? null;
    const fleet = fleetTables.get(name) ?? null;
    return {
      name,
      runtime,
      plan,
      fleet,
      totalRows: runtime?.totalRows ?? fleet?.totalRows ?? plan?.rowCount ?? null,
    };
  });
  const maxTotal = merged.reduce(
    (max, row) => (row.totalRows !== null && row.totalRows > max ? row.totalRows : max),
    0,
  );

  const rows: BoardTableRow[] = merged.map(({ name, runtime, plan, fleet, totalRows }) => {
    const phase: FleetRuntimeTablePhase | null = runtime?.phase ?? fleet?.phase ?? null;
    const copiedRows =
      runtime ? runtime.copiedRows
      : fleet ? fleet.copiedRows
      : plan ? plan.copiedRows
      : null;
    const progress =
      copiedRows !== null && totalRows !== null && totalRows > 0
        ? Math.min(1, Math.max(0, copiedRows / totalRows))
        : null;
    const journalImages = plan?.journalImages ?? null;
    const blockedReasons = plan?.blockedReasons ?? [];
    const identityStatus = plan?.identityStatus ?? null;
    const admitted = plan?.historicalAdmitted ?? fleet !== null;
    const problem = rowProblem(phase, blockedReasons, identityStatus, journalImages, admitted);

    const phaseLabel = phase === null ? 'Inconnu — non mesurée' : runtimePhaseLabel(phase);
    const verdict = tableVerdict(
      phase,
      problem,
      blockedReasons,
      admitted,
      pipeline.fleetRuntime !== null,
      current,
    );
    return {
      name,
      phase,
      phaseLabel,
      copiedRows,
      totalRows,
      progress,
      volumeShare: totalRows !== null && maxTotal > 0 ? totalRows / maxTotal : null,
      journalImages,
      blockedReasons,
      identityStatus,
      admitted,
      cataloguedAt: pipeline.fleetPlan?.observedAt ?? null,
      problem,
      paused: phase === 'PAUSED',
      verdict,
      state: mergedTableState(phase, phaseLabel, verdict),
    };
  });

  const tier = (row: BoardTableRow): number => {
    if (row.problem === 'blocked') return 0;
    if (row.problem === 'journal') return 1;
    if (row.problem === 'unknown') return 2;
    if (row.paused) return 3;
    return 4;
  };
  rows.sort((a, b) => {
    const tierDelta = tier(a) - tier(b);
    if (tierDelta !== 0) return tierDelta;
    const phaseDelta = (b.phase === null ? -1 : PHASE_ORDER[b.phase]) - (a.phase === null ? -1 : PHASE_ORDER[a.phase]);
    if (phaseDelta !== 0) return phaseDelta;
    return a.name.localeCompare(b.name);
  });
  return rows;
}

/* ------------------------------------------------------------------ */
/* Vue de section — tout ce que l'en-tête affiche                       */
/* ------------------------------------------------------------------ */

export interface SectionView {
  readonly availability: PipelineSourceAvailability;
  readonly retained: string | null;
  readonly stopped: boolean;
  readonly current: boolean;
  readonly runWord: RunWord;
  readonly badges: readonly ScopeBadge[];
  readonly checkpoint: { readonly receiver: string; readonly sequence: number } | null;
  readonly checkpointMissingReason: string;
  readonly throughput: ThroughputView;
  readonly lag: LagView;
  readonly rows: readonly BoardTableRow[];
  readonly alert: SectionAlert | null;
  /** Boucle décisionnelle d'un incident : pas de reprise automatique, où la
   *  mesure reprendra, dernier débit avant gel. null hors incident. */
  readonly resume: ResumeView | null;
  /** Diagnostic sûreté : « a-t-on perdu des données ? » — servi par le plan. */
  readonly safety: SafetyLine | null;
  readonly watermark: DestinationWatermark;
  readonly totals: TableTotals;
  readonly observations: readonly ObservationRow[];
}

export function pipelineSectionView(
  pipeline: Pipeline,
  ctx: {
    readonly sources: readonly Source[];
    readonly transport: Transport;
    readonly previous: CounterSample | undefined;
  },
): SectionView {
  const availability = sourceAvailabilityFor(pipeline, ctx.sources);
  const retained = retainedReason(pipeline, ctx.transport, availability);
  const current =
    retained === null &&
    pipeline.quality.evidenceKind === 'live' &&
    pipeline.quality.freshness === 'fresh' &&
    availability.kind === 'available';
  const checkpoint = pipeline.fleetRuntime?.checkpoint ?? null;
  const stopped =
    pipeline.incident?.type === 'capture_stopped' ||
    pipeline.status === 'planned_stop' ||
    pipeline.fleetRuntime?.phase === 'PAUSED';
  const rows = tableRows(pipeline, current);
  const alert = sectionAlert(pipeline, { transport: ctx.transport, retained });
  return {
    availability,
    retained,
    stopped,
    current,
    runWord: runStateWord(pipeline, { retained, current }),
    badges: scopeBadges(pipeline, ctx.transport, availability),
    checkpoint,
    checkpointMissingReason:
      pipeline.fleetRuntime == null ? 'exécution non projetée' : 'checkpoint non émis',
    throughput: deriveThroughput(ctx.previous, {
      events: pipeline.counters.events_published ?? null,
      at: pipeline.observedAt,
    }),
    lag: lagVitals(pipeline),
    rows,
    alert,
    resume:
      alert !== null && alert.severity === 'incident'
        ? resumptionView(pipeline, ctx.previous?.lastRate ?? null)
        : null,
    safety: safetyLine(pipeline),
    watermark: destinationWatermark(pipeline),
    totals: tableTotals(pipeline, rows),
    observations: observationRows(pipeline),
  };
}

/* ------------------------------------------------------------------ */
/* Heartbeat — l'instrument qui tourne, pas la pilule « en direct »     */
/* ------------------------------------------------------------------ */

export interface Heartbeat {
  /** « relevé il y a 4 s » ou « relevés figés depuis il y a 37 h ». */
  readonly measure: string;
  /** Révision + âge de génération + relevés reçus — la boucle démontrée. */
  readonly channel: string;
  readonly stale: boolean;
  readonly tone: BoardTone;
}

/** Le tick servi par l'overview : révision + instant de génération du
 *  snapshot + nombre de relevés reçus depuis l'ouverture de l'écran. C'est
 *  la preuve mesurée que la boucle de lecture tourne — jamais une assertion
 *  « en direct » : le compteur s'incrémente sous les yeux de l'opérateur. */
export interface ReadingTick {
  readonly revision: number;
  readonly generatedAt: string;
  /** Relevés distincts reçus depuis le début de la session d'écran — le
   *  compteur vivant qui remplace le jargon « canal SSE ouvert ». */
  readonly received: number;
}

export function heartbeat(
  pipeline: Pipeline,
  transport: Transport,
  now: Date,
  generated: ReadingTick | null = null,
): Heartbeat {
  const reading = age(pipeline.observedAt, now);
  const received = generated?.received ?? null;
  const receivedCopy =
    received !== null ? `${sequences(received)} relevé${received > 1 ? 's' : ''} reçu${received > 1 ? 's' : ''}` : null;
  const channelState =
    transport === 'live'
      ? (receivedCopy ?? 'canal de mesure ouvert')
      : transport === 'frozen'
        ? `${receivedCopy ?? 'canal de mesure ouvert'} · affichage suspendu`
        : 'canal de mesure interrompu';
  const channel = generated !== null
    ? `révision ${sequences(generated.revision)} · générée ${age(generated.generatedAt, now).label} · ${channelState}`
    : channelState;
  const stale =
    transport !== 'live' ||
    reading.stale ||
    pipeline.quality.freshness === 'stale' ||
    pipeline.quality.freshness === 'clock_untrusted' ||
    pipeline.quality.freshness === 'late';
  const measure = stale
    ? `relevés figés depuis ${reading.label}`
    : `relevé ${reading.label}`;
  return {
    measure,
    channel,
    stale,
    tone: stale ? 'attention' : 'active',
  };
}

/* ------------------------------------------------------------------ */
/* Bandeau de dégradation unique — sévérité + cause + depuis + remède   */
/* ------------------------------------------------------------------ */

export interface SectionAlert {
  readonly severity: 'incident' | 'degrade';
  readonly headline: string;
  /** Étape qui porte l'incident : « Lecture » pour capture_*, « Destination »
   *  pour destination — le lieu est nommé, pas laissé à deviner. null pour une
   *  dégradation de relevé sans étape fautive servie. */
  readonly locus: 'Lecture' | 'Destination' | null;
  readonly cause: string;
  /** Guidance de prochaine étape sûre — jamais un bouton, jamais une action. */
  readonly remedy: string | null;
  /** Relevé sur lequel l'alerte s'appuie — « depuis » calculé au rendu. */
  readonly observedAt: string;
  /** Début de l'incident servi par l'étape impliquée — distinct de l'âge du
   *  relevé : « connexion refusée depuis {timestamp} ». null si l'étape n'est pas datée. */
  readonly onset: { readonly at: string; readonly word: 'connexion refusée' | 'arrêt' | 'incident' } | null;
  /** Vrai quand une preuve fraîche montre la cause disparue : l'alerte
   *  devient « prête à reprendre » — action, plus réparation. */
  readonly ready: boolean;
  readonly tone: 'attention' | 'muted';
}

const INCIDENT_ALERT_COPY: Readonly<
  Record<string, { readonly headline: string; readonly cause: string; readonly remedy: string }>
> = {
  capture_connection_failure: {
    headline: 'Connexion AS400 interrompue',
    cause: 'la capture ne joint plus la source',
    remedy: 'Vérifier la joignabilité de la source puis relancer la capture.',
  },
  capture_timeout: {
    headline: 'Lecture AS400 trop lente',
    cause: 'la capture dépasse le délai de lecture accordé',
    remedy: 'Vérifier la charge de la source puis relancer la capture.',
  },
  capture_stopped: {
    headline: 'Capture arrêtée en sécurité',
    cause: 'arrêt fail-closed — la reprise repart d’un point propre',
    remedy: 'Corriger la cause de l’arrêt puis relancer la capture.',
  },
  capture_auth_blocked: {
    headline: 'Authentification AS400 bloquée',
    cause: 'la source refuse la connexion — les relevés ne sont plus alimentés',
    remedy: 'Réparer le compte AS400 puis relancer la capture.',
  },
  capture_position_review: {
    headline: 'Position de reprise à revoir',
    cause: 'la capture attend une confirmation de position avant de repartir',
    remedy: 'Confirmer la position de reprise puis relancer la capture.',
  },
  destination: {
    headline: 'Incident à la destination',
    cause: 'la livraison vers la destination est interrompue',
    remedy: 'Réparer la configuration destination puis relancer la livraison.',
  },
};

/** L'étape qui porte l'incident : capture_* → capture, destination →
 *  destination. Son observed_at date le début constaté de l'incident —
 *  servi, jamais deviné. Exportée pour que l'onglet Mesures annote le tracé
 *  avec le même début d'incident que la bande d'alerte. */
export function incidentOnset(pipeline: Pipeline, type: IncidentType): SectionAlert['onset'] {
  const stageId = type === 'destination' ? 'destination' : 'capture';
  // `declared_at` garde le début réel de l'incident : l'observation de l'étape
  // est rafraîchie par la sonde alors que le refus date du relevé figé.
  const observedAt = pipeline.incident?.declaredAt
    ?? pipeline.stages.find((stage) => stage.id === stageId)?.observedAt
    ?? null;
  if (observedAt === null) return null;
  const word =
    type === 'capture_auth_blocked' ? 'connexion refusée'
    : type === 'capture_stopped' ? 'arrêt'
    : 'incident';
  return { at: observedAt, word };
}

/** Une seule alerte mise en scène par section : l'incident déclaré d'abord,
 *  sinon la dégradation du relevé. L'affichage suspendu par l'opérateur n'est
 *  pas une alerte — c'est un choix, déjà signalé par les badges de portée. */
export function sectionAlert(
  pipeline: Pipeline,
  ctx: { readonly transport: Transport; readonly retained: string | null },
): SectionAlert | null {
  const incident = pipeline.incident;
  if (incident !== null) {
    const ready = resumeReadiness(pipeline) === 'ready';
    if (ready) {
      return {
        severity: 'incident',
        headline: 'Cause résolue',
        locus: incident.type === 'destination' ? 'Destination' : 'Lecture',
        cause: 'la source accepte de nouveau la connexion — la capture attend le relancement',
        remedy: 'Relancer la capture reprend le flux au point d’arrêt.',
        observedAt: pipeline.observedAt,
        onset: incidentOnset(pipeline, incident.type),
        ready: true,
        tone: 'attention',
      };
    }
    const copy = INCIDENT_ALERT_COPY[incident.type] ?? {
      headline: 'Incident déclaré',
      cause: incident.type,
      remedy: 'Traiter la cause puis relancer.',
    };
    return {
      severity: 'incident',
      headline: copy.headline,
      locus: incident.type === 'destination' ? 'Destination' : 'Lecture',
      cause: copy.cause,
      remedy: copy.remedy,
      observedAt: pipeline.observedAt,
      onset: incidentOnset(pipeline, incident.type),
      ready: false,
      tone: 'attention',
    };
  }
  if (ctx.transport === 'frozen' || ctx.retained === null) return null;
  const cause =
    ctx.transport === 'retained'
      ? 'le canal de mesure est interrompu — le plateau conserve le dernier relevé lu'
      : `le dernier relevé n’est plus recevable : ${ctx.retained}`;
  return {
    severity: 'degrade',
    headline: 'Relevé dégradé',
    locus: null,
    cause,
    remedy:
      ctx.transport === 'retained'
        ? 'Le plateau reprendra vie au retour du canal de mesure.'
        : 'Vérifier que la capture tourne encore puis confirmer le relevé.',
    observedAt: pipeline.observedAt,
    onset: null,
    ready: false,
    tone: 'attention',
  };
}

/* ------------------------------------------------------------------ */
/* Boucle décisionnelle — où la mesure reprendra, sans faux espoir      */
/* ------------------------------------------------------------------ */

export interface ResumeView {
  /** État de reprise observé : aucune nouvelle tentative dans le relevé —
   *  le canal d'observation ouvert ne doit pas laisser croire à une
   *  reprise automatique de la capture. null quand le titre « Prête à
   *  reprendre » porte déjà le message. */
  readonly retry: string | null;
  /** Ligne de flux journal → publiés → destination : la chaîne que la
   *  mesure reprendra, compteur par compteur, « non mesuré » si absent. */
  readonly flow: string;
  /** Dernier débit mesuré avant gel (échantillon précédent de session) —
   *  null quand rien n'a jamais été calculable : une ligne « non mesuré »
   *  n'apporte rien sous une alerte de reprise. */
  readonly lastRate: string | null;
}

export function resumptionView(
  pipeline: Pipeline,
  lastRate: CounterSample['lastRate'],
): ResumeView {
  const checkpoint =
    pipeline.resume?.checkpoint ?? pipeline.fleetRuntime?.checkpoint ?? null;
  const backlogSequences = pipeline.resume?.backlogSequences ?? null;
  const lastRateCopy =
    lastRate != null
      ? `Dernier débit mesuré avant gel : ${lastRate.value} · relevé ${timestamp(lastRate.at)}`
      : null;
  if (resumeReadiness(pipeline) === 'ready') {
    return {
      retry: null,
      flow:
        checkpoint !== null
          ? backlogSequences !== null
            ? `Repartir de ${position(checkpoint.receiver, checkpoint.sequence)} · ${sequences(backlogSequences)} séquences à relire`
            : `Repartir de ${position(checkpoint.receiver, checkpoint.sequence)} · volume à relire non mesuré`
          : 'Point de reprise non mesuré',
      lastRate: lastRateCopy,
    };
  }
  const published = pipeline.counters.events_published ?? null;
  const inTarget = pipeline.counters.events_in_target ?? null;
  const flow = [
    `journal ${checkpoint !== null ? position(checkpoint.receiver, checkpoint.sequence) : 'non mesuré'}`,
    published !== null ? `${sequences(published)} publiés` : 'publiés non mesurés',
    inTarget !== null ? `${sequences(inTarget)} à la destination` : 'destination non mesurée',
  ].join(' → ');
  return {
    retry: 'Reprise : aucune nouvelle tentative observée — intervention requise',
    flow: `Flux : ${flow}`,
    lastRate: lastRateCopy ?? 'Dernier débit avant gel : non mesuré',
  };
}

/* ------------------------------------------------------------------ */
/* Diagnostic sûreté — « a-t-on perdu des données ? »                   */
/* ------------------------------------------------------------------ */

export interface SafetyLine {
  readonly text: string;
  readonly tone: BoardTone;
}

/** Libellé français de la continuité servie par le plan — jamais l'énum
 *  brute (« continuité proven » tronqué). */
export function continuityLabel(continuity: 'proven' | 'uncertain' | 'broken'): string {
  switch (continuity) {
    case 'proven': return 'prouvée';
    case 'uncertain': return 'incertaine';
    case 'broken': return 'rompue';
  }
}

/** Continuité prouvée → « aucun trou de séquence détecté · reprise depuis
 *  {checkpoint} ». L'ancre de reprise = la position courante du journal
 *  (runtime), sinon le checkpoint de bascule déclaré par le plan. */
export function safetyLine(pipeline: Pipeline): SafetyLine | null {
  const plan = pipeline.fleetPlan ?? null;
  if (plan === null) return null;
  const anchor =
    pipeline.resume?.checkpoint ?? pipeline.fleetRuntime?.checkpoint ?? plan.cutoverCheckpoint;
  const anchorCopy = `reprise depuis ${position(anchor.receiver, anchor.sequence)}`;
  switch (plan.continuity) {
    case 'proven':
      return { text: `aucun événement manquant détecté · ${anchorCopy}`, tone: 'active' };
    case 'uncertain':
      return {
        text: `continuité incertaine — événements manquants possibles · ${anchorCopy}`,
        tone: 'muted',
      };
    case 'broken':
      return {
        text: `événements manquants détectés — continuité rompue · ${anchorCopy}`,
        tone: 'attention',
      };
  }
}

/* ------------------------------------------------------------------ */
/* Watermark destination — events_in_target ou « non instrumenté »      */
/* ------------------------------------------------------------------ */

export interface DestinationWatermark {
  readonly value: string;
  readonly hint: string | null;
  readonly tone: BoardTone;
}

export function destinationWatermark(pipeline: Pipeline): DestinationWatermark {
  const inTarget = pipeline.counters.events_in_target ?? null;
  const namespace =
    pipeline.fleetPlan?.destinationNamespace ?? pipeline.fleet?.destinationNamespace ?? null;
  if (inTarget !== null) {
    return {
      value: `${sequences(inTarget)} événement${inTarget > 1 ? 's' : ''} constaté${inTarget > 1 ? 's' : ''}`,
      hint:
        (namespace !== null ? `${namespace} · ` : '') +
        'cumul constaté — pas une réconciliation',
      tone: 'active',
    };
  }
  if (namespace !== null) {
    return {
      value: namespace,
      hint: 'compteur de livraison non servi',
      tone: 'muted',
    };
  }
  return { value: 'Destination · non datée', hint: null, tone: 'muted' };
}

/* ------------------------------------------------------------------ */
/* Totaux de grille — footer calculé sur les données présentes          */
/* ------------------------------------------------------------------ */

export interface TableTotals {
  readonly tableCount: number;
  /** Volume du catalogue (plan), si le plan est servi. */
  readonly cataloguedRows: number | null;
  /** Somme des copied_rows réellement mesurés par le runtime. */
  readonly copiedRows: number | null;
  readonly problems: number;
}

export function tableTotals(pipeline: Pipeline, rows: readonly BoardTableRow[]): TableTotals {
  let copied = 0;
  let copiedSeen = false;
  let problems = 0;
  for (const row of rows) {
    if (row.copiedRows !== null) {
      copied += row.copiedRows;
      copiedSeen = true;
    }
    if (row.problem !== null) problems += 1;
  }
  return {
    tableCount: rows.length,
    cataloguedRows: pipeline.fleetPlan?.observedTotals.rowCount ?? null,
    copiedRows: copiedSeen ? copied : null,
    problems,
  };
}

/* ------------------------------------------------------------------ */
/* Journal des observations — déduit du relevé, rien d'inventé          */
/* ------------------------------------------------------------------ */

export interface ObservationRow {
  readonly at: string;
  readonly label: string;
  readonly kind: 'releve' | 'catalogue' | 'etape';
}

const STAGE_OBSERVATION_LABELS: Readonly<Record<string, string>> = {
  source: 'Source',
  capture: 'Capture',
  raw: 'Zone brute',
  load: 'Chargement',
  destination: 'Destination',
};

export function observationRows(pipeline: Pipeline): ObservationRow[] {
  // Le summary servi peut porter un locus qui contredit le type d'incident
  // (la projection ne le vérifiait pas toujours) : le type fait foi.
  const releveLabel =
    pipeline.incident !== null
      ? resumeReadiness(pipeline) === 'ready'
        ? 'Relevé lu — capture prête à reprendre'
        : `Relevé lu — ${incidentHeadline(pipeline.incident.type)}`
      : `Relevé lu — ${pipeline.summary}`;
  const rows: ObservationRow[] = [
    { at: pipeline.observedAt, label: releveLabel, kind: 'releve' },
  ];
  const plan = pipeline.fleetPlan;
  if (plan !== null && plan !== undefined) {
    rows.push({
      at: plan.observedAt,
      label:
        `Catalogue observé — ${sequences(plan.observedTotals.rowCount)} lignes sur ` +
        `${plan.observedTotals.tableCount} tables · continuité ${continuityLabel(plan.continuity)}`,
      kind: 'catalogue',
    });
  }
  const recoveredAt =
    pipeline.incident?.causeResolvedObservedAt ?? pipeline.resume?.authObservedAt ?? null;
  if (recoveredAt !== null) {
    rows.push({ at: recoveredAt, label: 'Connexion source re-vérifiée', kind: 'etape' });
  }
  for (const stage of pipeline.stages) {
    if (stage.observedAt === null) {
      rows.push({
        at: pipeline.observedAt,
        label: `${STAGE_OBSERVATION_LABELS[stage.id] ?? stage.id} — jamais observée`,
        kind: 'etape',
      });
      continue;
    }
    if (stage.observedAt !== pipeline.observedAt || stage.status !== 'healthy') {
      rows.push({
        at: stage.observedAt,
        label: `${STAGE_OBSERVATION_LABELS[stage.id] ?? stage.id} — ${stage.headline}`,
        kind: 'etape',
      });
    }
  }
  return rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, 8);
}

/* ------------------------------------------------------------------ */
/* File d'attention + verdict global                                    */
/* ------------------------------------------------------------------ */

export interface AttentionItem {
  readonly pipeline: Pipeline;
  readonly cause: string;
}

/** Cause courte par type d'incident — la file énonce la cause, pas le
 *  symptôme (« authentification refusée », pas « incident signalé »). */
const INCIDENT_ATTENTION_CAUSE: Readonly<Record<IncidentType, string>> = {
  capture_connection_failure: 'connexion source interrompue',
  capture_timeout: 'lecture AS400 trop lente',
  capture_stopped: 'capture arrêtée en sécurité',
  capture_auth_blocked: 'authentification refusée',
  capture_position_review: 'position de reprise à revoir',
  destination: 'incident à la destination',
};

export function attentionItems(
  state: ControlPlaneState,
  transport: Transport,
  now: Date,
): AttentionItem[] {
  if (state.status === 'loading' || state.status === 'failed') return [];
  const overview = state.overview;
  const cached = transport === 'retained';
  const ctx = { generatedAt: overview.generatedAt, sources: overview.sources, cached };
  const items: AttentionItem[] = [];
  for (const pipeline of prioritizePipelines(overview.pipelines)) {
    const availability = sourceAvailabilityFor(pipeline, overview.sources);
    const held = retainedReason(pipeline, transport, availability) !== null;
    // Le gel est référencé par un token, jamais répété en phrase — la
    // déclaration canonique « relevés figés depuis… » vit dans le heartbeat.
    const frozenToken = held ? `figé ${age(pipeline.observedAt, now).label}` : null;
    let cause: string | null;
    if (pipeline.incident !== null) {
      cause =
        resumeReadiness(pipeline) === 'ready'
          ? 'prête à reprendre'
          : INCIDENT_ATTENTION_CAUSE[pipeline.incident.type] ?? 'incident déclaré';
    } else {
      const focus = resolvePipelineProofFocus(pipeline, ctx);
      if (focus.firstBreak !== null) {
        cause = focus.firstBreak.reason === 'stale'
          ? frozenToken ?? 'relevé trop ancien'
          : focus.firstBreak.cause;
      } else {
        const issue = pipelineIssue(pipeline);
        cause = issue !== 'Aucun problème signalé' ? issue : null;
      }
    }
    if (cause === null) continue;
    if (frozenToken !== null && cause !== frozenToken) cause = `${cause} · ${frozenToken}`;
    items.push({ pipeline, cause });
  }
  return items;
}

export interface PlateauVerdict {
  readonly label: 'Ça avance' | 'Ça coince' | 'À reprendre' | 'On ne sait pas';
  readonly detail: string;
  readonly tone: 'positive' | 'attention' | 'muted';
}

const PROOF_SCOPE_LABELS: Readonly<Record<string, string>> = {
  simulation: 'démonstration uniquement',
  historical: 'relevé antérieur',
  stale: 'relevé trop ancien',
  cached: 'relevé conservé',
  partial: 'sources partielles',
  mixed: 'natures mixtes',
  unavailable: 'données non disponibles',
};

/* ------------------------------------------------------------------ */
/* Journal de session — transitions déduites des relevés, jamais        */
/* inventées. Partagé par le plateau Sources et l'écran Activité.       */
/* ------------------------------------------------------------------ */

export interface SessionEntry {
  readonly at: string;
  readonly label: string;
}

export interface SessionLog {
  readonly signature: string;
  readonly revision: number;
  /** Relevés reçus pour cette connexion depuis l'ouverture de la session. */
  readonly total: number;
  /** Relevés identiques consécutifs — ≥ 2 s'affiche en ligne collapsée. */
  readonly identicalRun: number;
  readonly entries: readonly SessionEntry[];
}

/** Une ligne par relevé généré — la révision défile même quand la source est
 *  morte, c'est la preuve que l'observateur tourne. L'état qui change est dit ;
 *  les relevés identiques sont collapsés en une ligne vivante (« N relevés
 *  identiques depuis le début de session ») dont les deux compteurs
 *  s'incrémentent — la preuve devient démonstration. Idempotent sous double
 *  rendu : la révision évite le doublon. */
export function sessionEntries(
  log: Map<string, SessionLog>,
  pipeline: Pipeline,
  view: SectionView,
  generated: ReadingTick,
): readonly SessionEntry[] {
  const signature = [
    view.runWord.label,
    view.retained ?? '',
    pipeline.incident?.type ?? '',
    pipeline.quality.freshness,
    pipeline.fleetRuntime?.checkpoint?.sequence ?? '',
    pipeline.counters.events_published ?? '',
    pipeline.lagSequences ?? '',
  ].join('|');
  const previous = log.get(pipeline.id);
  if (previous !== undefined && previous.revision === generated.revision) {
    return previous.entries;
  }
  const stateLine = [
    view.runWord.label,
    view.checkpoint !== null
      ? position(view.checkpoint.receiver, view.checkpoint.sequence)
      : null,
    pipeline.counters.events_published !== null &&
    pipeline.counters.events_published !== undefined
      ? `${sequences(pipeline.counters.events_published)} publiés`
      : null,
    pipeline.lagSequences !== null
      ? `${sequences(pipeline.lagSequences)} séquence${pipeline.lagSequences > 1 ? 's' : ''} de retard`
      : null,
  ]
    .filter((part): part is string => part !== null)
    .join(' · ');

  const total = (previous?.total ?? 0) + 1;
  if (previous === undefined) {
    const entries = [{ at: generated.generatedAt, label: `Premier relevé de la session — ${stateLine}` }];
    log.set(pipeline.id, { signature, revision: generated.revision, total, identicalRun: 1, entries });
    return entries;
  }
  if (previous.signature === signature) {
    // Relevé identique : on ne multiplie pas les lignes, on fait vivre la
    // première — « N relevés identiques » qui s'incrémente à chaque révision.
    const run = previous.identicalRun + 1;
    const scope = run === total ? 'depuis le début de session' : 'd’affilée';
    const collapsed: SessionEntry = {
      at: generated.generatedAt,
      label: `Révision ${sequences(generated.revision)} · ${sequences(run)} relevés identiques ${scope}`,
    };
    const rest = previous.identicalRun > 1 ? previous.entries.slice(1) : previous.entries;
    const entries = [collapsed, ...rest].slice(0, 6);
    log.set(pipeline.id, { signature, revision: generated.revision, total, identicalRun: run, entries });
    return entries;
  }
  const entries = [
    { at: generated.generatedAt, label: `Révision ${sequences(generated.revision)} — ${stateLine}` },
    ...previous.entries,
  ].slice(0, 6);
  log.set(pipeline.id, { signature, revision: generated.revision, total, identicalRun: 1, entries });
  return entries;
}

/** Une seule timeline, tri strict décroissant : les ticks de session (le
 *  relevé qui défile) et les observations datées du relevé (étapes,
 *  catalogue) partagent le même journal, étiquetés par origine. Quand le
 *  relevé est retenu, les étiquettes le disent — « étape figée » : ces lignes
 *  sont déduites du relevé figé (canal d'observation), pas une mesure live. */
export function journalRows(
  session: readonly SessionEntry[],
  observations: readonly ObservationRow[],
  held: boolean,
): readonly { readonly at: string; readonly label: string; readonly tag: string }[] {
  const rows = [
    ...session.map((entry) => ({ at: entry.at, label: entry.label, tag: 'session' })),
    ...observations
      .filter((row) => !(row.kind === 'releve' && session.length > 0))
      .map((row) => ({
        at: row.at,
        label: row.label,
        tag:
          row.kind === 'catalogue'
            ? held ? 'catalogue figé' : 'catalogue'
            : row.kind === 'etape'
              ? held ? 'étape figée' : 'étape'
              : held ? 'relevé figé' : 'relevé',
      })),
  ];
  return rows.sort((a, b) => Date.parse(b.at) - Date.parse(a.at)).slice(0, 10);
}

/* ------------------------------------------------------------------ */
/* Vocabulaire incident — libellés français servis par le bandeau et    */
/* réutilisés par le journal d'activité.                                */
/* ------------------------------------------------------------------ */

export function incidentHeadline(type: IncidentType): string {
  return INCIDENT_ALERT_COPY[type]?.headline ?? 'Incident déclaré';
}

export function incidentCause(type: IncidentType): string | null {
  return INCIDENT_ALERT_COPY[type]?.cause ?? null;
}

/** Cause courte (3-5 mots) portée par le drapeau sur le nœud cassé — le
 *  même vocabulaire que la file d'attention : la cause, pas le symptôme. */
export function incidentShortCause(type: IncidentType): string {
  return INCIDENT_ATTENTION_CAUSE[type] ?? 'incident déclaré';
}

export function plateauVerdict(state: ControlPlaneState, transport: Transport): PlateauVerdict {
  const unknown = (detail: string): PlateauVerdict => ({ label: 'On ne sait pas', detail, tone: 'muted' });
  if (transport === 'frozen') {
    return unknown('actualisation gelée — relevé affiché figé');
  }
  if (state.status === 'loading') return unknown('premier relevé en cours de lecture');
  if (state.status === 'failed') return unknown(`relevé indisponible — ${state.message}`);

  const overview = state.overview;
  const cached = transport === 'retained';
  if (overview.pipelines.length === 0) {
    return unknown(
      sourceAvailability(overview) === 'unavailable'
        ? 'aucune source disponible — relevé vide'
        : 'aucune connexion dans le relevé',
    );
  }

  const ctx = { generatedAt: overview.generatedAt, sources: overview.sources, cached };
  const focuses = prioritizePipelines(overview.pipelines).map((pipeline) => ({
    pipeline,
    focus: resolvePipelineProofFocus(pipeline, ctx),
  }));
  const suffix = cached ? ' · relevé conservé' : '';

  const awaiting = focuses.find(
    ({ pipeline }) => resumeReadiness(pipeline) === 'ready',
  );
  if (awaiting) {
    return {
      label: 'À reprendre',
      detail: `${awaiting.pipeline.id} · cause résolue — la capture attend le relancement${suffix}`,
      tone: 'attention',
    };
  }
  const incident = focuses.find(({ focus }) => focus.firstBreak?.reason === 'incident');
  if (incident) {
    return {
      label: 'Ça coince',
      detail: `${incident.pipeline.id} · ${incident.focus.firstBreak!.cause}${suffix}`,
      tone: 'attention',
    };
  }
  const broken = focuses.find(({ focus }) => focus.firstBreak !== null);
  if (broken) {
    return unknown(`${broken.pipeline.id} · ${broken.focus.firstBreak!.cause}${suffix}`);
  }
  if (cached) {
    return unknown('relevé conservé — état courant non confirmé');
  }
  const bounded = focuses.find(({ focus }) => focus.proofScope.kind !== 'live');
  if (bounded) {
    const scope = PROOF_SCOPE_LABELS[bounded.focus.proofScope.kind] ?? bounded.focus.proofScope.kind;
    return unknown(`${bounded.pipeline.id} · livraison confirmée seulement : ${scope}`);
  }
  return {
    label: 'Ça avance',
    detail:
      overview.pipelines.length === 1
        ? 'livraison confirmée dans le relevé courant'
        : `${overview.pipelines.length} connexions · livraison confirmée dans le relevé courant`,
    tone: 'positive',
  };
}
