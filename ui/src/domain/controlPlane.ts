import { installedSiteIdentity, type SiteIdentity } from './siteIdentity.ts';

export type PipelineStatus =
  | 'healthy'
  | 'recovering'
  | 'degraded'
  | 'incident'
  | 'unknown'
  | 'planned_stop'
  | 'awaiting_resume';

export type Coverage = 'complete' | 'partial' | 'gap' | 'none';
export type Freshness = 'fresh' | 'late' | 'stale' | 'clock_untrusted';
export type EvidenceKind = 'live' | 'historical' | 'simulation';
export type StageId = 'source' | 'capture' | 'raw' | 'load' | 'destination';
export type ScopeKind = 'single' | 'mixed' | 'unavailable';

export interface Scope {
  readonly kind: ScopeKind;
  readonly environments: readonly string[];
}

export interface ObservationQuality {
  readonly coverage: Coverage;
  readonly freshness: Freshness;
  readonly evidenceKind: EvidenceKind;
}

export type StageStatus = 'healthy' | 'degraded' | 'incident' | 'unknown' | 'planned_stop' | 'awaiting_resume';
export interface Stage {
  readonly id: StageId;
  readonly status: StageStatus;
  readonly observedAt: string | null;
  readonly headline: string;
  readonly detail: string;
}

export type IncidentCode =
  | 'capture_stopped_fail_closed'
  | 'destination_configuration_missing'
  | 'destination_configuration_invalid'
  | 'destination_credential_missing'
  | 'destination_credential_denied'
  | 'destination_credential_invalid'
  | 'destination_unreachable'
  | 'destination_authorization_denied'
  | 'destination_contract_incompatible'
  | 'destination_environment_mismatch'
  | 'destination_activation_inconsistent'
  | 'destination_source_checkpoint_mismatch'
  | 'destination_load_failed'
  | 'destination_load_timeout'
  | 'destination_load_contract_invalid'
  | 'destination_load_checkpoint_conflict'
  | 'destination_apply_failed'
  | 'destination_apply_timeout'
  | 'destination_apply_contract_invalid'
  | 'destination_apply_checkpoint_conflict'
  | 'destination_load_ahead_of_source'
  | 'destination_apply_ahead_of_load'
  | 'destination_reconciliation_mismatch'
  | 'destination_reconciliation_failed'
  | 'destination_reconciliation_inconsistent';
export type IncidentType = 'capture_connection_failure' | 'capture_timeout' | 'capture_stopped' | 'capture_auth_blocked' | 'capture_position_review' | 'destination';
export interface Incident {
  readonly code: IncidentCode;
  readonly type: IncidentType;
  /** Vrai quand une preuve fraîche montre que la cause a disparu — l'incident
   *  reste servi (historique), mais l'état courant devient « prête à
   *  reprendre » plutôt que « incident actif ». */
  readonly causeResolved?: boolean;
  readonly causeResolvedObservedAt?: string | null;
  /** Début réel de l'incident — l'observation de l'étape peut être rafraîchie
   *  par la sonde alors que le refus remonte au relevé figé. */
  readonly declaredAt?: string | null;
}

export interface LagPoint {
  readonly startSeconds: number;
  readonly endSeconds: number;
  readonly low: number | null;
  readonly high: number | null;
  readonly lag: number | null;
  readonly samples: number;
  readonly unknownSamples: number;
  readonly coverage: 'complete' | 'gap';
  readonly kind: 'observed' | 'temporal_gap';
}

/** Réservé aux surfaces Usage ultérieures : jamais une estimation implicite. */
export interface Usage {
  readonly observedAt: string;
  readonly values: Readonly<Record<string, number | null>>;
}

export type SloSignalStatus = 'pass' | 'breach' | 'unobserved';
export type SloStatus = SloSignalStatus | 'unavailable';
export type SloValue = number | string | readonly string[] | null;
export type SloAlertLifecycle = 'firing' | 'resolved';
export type SloAlertSeverity = 'none' | 'warning' | 'critical';

export interface SloCheck {
  readonly id: string;
  readonly stage: string;
  readonly status: SloSignalStatus;
  readonly observed: SloValue;
  readonly threshold: SloValue;
  readonly unit: string | null;
  readonly reason: string;
}

export interface SloAlert {
  readonly fingerprint: string;
  readonly checkId: string;
  readonly stage: string;
  readonly lifecycleState: SloAlertLifecycle;
  readonly signalStatus: SloSignalStatus;
  readonly severity: SloAlertSeverity;
  readonly reason: string;
  readonly observed: SloValue;
  readonly threshold: SloValue;
  readonly unit: string | null;
  readonly firstFiredAt: string;
  readonly firingSince: string;
  readonly lastObservedAt: string;
  readonly resolvedAt: string | null;
  readonly occurrenceCount: number;
  readonly evaluationCount: number;
}

export interface Observability {
  readonly status: SloStatus;
  readonly quality: {
    readonly coverage: 'complete' | 'partial' | 'none';
    readonly freshness: 'fresh' | 'stale' | 'clock_untrusted' | 'unavailable';
    readonly evidenceKind: EvidenceKind;
  };
  readonly observedAt: string | null;
  readonly reason: string;
  readonly checks: readonly SloCheck[];
  readonly alerts: readonly SloAlert[];
}

export interface WindowChainProgress {
  readonly declaredWindows: number;
  readonly matchedWindows: number;
  readonly captureComplete: boolean;
  readonly state: 'incomplete' | 'matched';
  readonly evidenceKind: EvidenceKind;
}

export type WindowDelivery = {readonly chain?: WindowChainProgress} & (
  | { readonly state: 'invalid' | 'unavailable' }
  | { readonly state: 'matched' | 'not_tested'; readonly archiveRunId: string; readonly windowId: string;
      readonly startedAt: string; readonly closedAt: string; readonly destinationObservedAt: string;
      readonly eventCount: number; readonly scope: 'closed_window_only';
      readonly quality: { readonly freshness: Freshness; readonly evidenceKind: EvidenceKind } });

// Invariantes produit : version des formats et bornes de concurrence. Les
// identités d'installation (flotte, environnement, destination, manifeste)
// proviennent du site déclaré — voir siteIdentity().
export const FLEET_FORMAT_VERSION = 'quadringent-fleet-v1';
export const FLEET_MIN_CONCURRENCY = 1;
export const FLEET_MAX_CONCURRENCY = 4;
export const FLEET_RUNTIME_FORMAT_VERSION = 'quadringent-fleet-runtime-v1';

export type FleetTableName = string;
export type FleetPhase =
  | 'NOT_PREPARED'
  | 'READY'
  | 'HISTORICAL'
  | 'CATCHING_UP'
  | 'LIVE'
  | 'RECONCILING'
  | 'CERTIFIED'
  | 'PAUSED'
  | 'BLOCKED';
export type FleetBlockedReason =
  | 'receiver_discontinuity'
  | 'sequence_gap'
  | 'unknown_history_progress'
  | 'missing_start_checkpoint'
  | 'unproven_continuity'
  | 'journal_tail_unobserved'
  | 'cost_overrun'
  | 'unknown_cost'
  | 'operator_stop';
export type FleetSafeAction =
  | 'PREPARE'
  | 'ADMIT_HISTORICAL'
  | 'RECORD_HISTORY_PROGRESS'
  | 'PROVE_CONTINUITY'
  | 'CATCH_UP_TO_TAIL'
  | 'RECORD_ACTUAL_COST'
  | 'OPEN_RECONCILIATION'
  | 'CERTIFY'
  | 'RESUME'
  | 'INSPECT_BLOCKED'
  | 'NONE';
export type CapabilityId = 'refresh' | 'prepare' | 'start' | 'pause' | 'resume';
export type CapabilityState = 'available' | 'unavailable';

export interface FleetCapability {
  readonly state: CapabilityState;
  readonly reason: string | null;
}

export interface JournalCheckpoint {
  readonly receiver: string;
  readonly sequence: number;
}

export interface ReceiverSpan {
  readonly receiver: string;
  readonly firstSequence: number;
  readonly lastSequence: number;
}

export interface ProofWindow {
  readonly startUtc: string;
  readonly endUtc: string;
}

export interface ReconciliationProof {
  readonly window: ProofWindow;
  readonly sourceCount: number;
  readonly targetCount: number;
  readonly missing: number;
  readonly extra: number;
  readonly duplicates: number;
  readonly sourceHash: string;
  readonly targetHash: string;
  readonly destinationFreshnessSeconds: number;
  readonly freshnessSloSeconds: number;
  readonly latencySeconds: number;
  readonly throughputRowsPerSecond: number;
  readonly costUnits: number;
}

export interface FleetTable {
  readonly name: FleetTableName;
  readonly phase: FleetPhase;
  readonly startCheckpoint: JournalCheckpoint | null;
  readonly currentCheckpoint: JournalCheckpoint | null;
  readonly journalTail: JournalCheckpoint | null;
  readonly receiverChain: readonly ReceiverSpan[] | null;
  readonly copiedRows: number | null;
  readonly totalRows: number | null;
  readonly continuityProven: boolean | null;
  readonly gap: boolean | null;
  readonly proofWindow: ProofWindow | null;
  readonly pausedFrom: FleetPhase | null;
  readonly blockedReason: FleetBlockedReason | null;
  readonly admitted: boolean;
  readonly estimatedCredits: number | null;
  readonly reservedCredits: number | null;
  readonly actualCredits: number | null;
  readonly reconciliationProof: ReconciliationProof | null;
}

export interface FleetSummary {
  readonly certifiedCount: number;
  readonly tableCount: number;
  readonly runningCount: number;
  readonly admittedCount: number;
  readonly knownCopiedRows: number | null;
  readonly knownTotalRows: number | null;
  readonly creditBudget: number;
  readonly consumedCredits: number;
  readonly reservedCredits: number;
  readonly overBudget: boolean;
  readonly nextAction: FleetSafeAction;
  readonly nextReason: string;
  readonly nextTable: FleetTableName | null;
}

export interface Fleet {
  readonly fleetId: string;
  readonly formatVersion: string;
  readonly environment: string;
  readonly destinationNamespace: string;
  readonly maxConcurrency: number;
  readonly creditBudget: number;
  readonly consumedCredits: number;
  readonly reservedCredits: number;
  readonly tables: readonly FleetTable[];
  readonly summary: FleetSummary;
  readonly capabilities: Readonly<Record<CapabilityId, FleetCapability>>;
}

export type FleetPlanContinuity = 'proven' | 'uncertain' | 'broken';
export type FleetPlanIdentityStatus = 'keyed' | 'rrn' | 'blocked';

/** Progression d'une copie historique publiée par l'orchestrateur
 *  (document history-progress). « invalid » signifie que ce qui est publié ne
 *  se lit pas — distinct de « rien n'a été publié » (null). */
export type FleetHistoryStatus = 'running' | 'complete' | 'failed' | 'invalid';

export interface FleetHistoryProgress {
  readonly status: FleetHistoryStatus;
  readonly runId: string | null;
  readonly updatedAt: string | null;
  readonly plannedRows: number | null;
  readonly publishedRows: number | null;
  readonly publishedBytes: number | null;
  readonly publishedObjects: number | null;
  readonly chunksPending: number | null;
  readonly chunksRunning: number | null;
  readonly chunksReadDone: number | null;
  readonly chunksPublished: number | null;
  readonly chunksFailed: number | null;
}

export interface FleetPlanTable {
  readonly name: FleetTableName;
  readonly rowCount: number;
  readonly dataSize: number;
  readonly journalImages: '*AFTER' | '*BOTH' | '*BEFORE';
  readonly identityStatus: FleetPlanIdentityStatus;
  readonly identitySource: string | null;
  readonly candidateKey: readonly string[] | null;
  readonly historicalAdmitted: boolean;
  readonly historicalLane: number | null;
  readonly blockedReasons: readonly string[];
  readonly copiedRows: number | null;
  readonly copiedBytes: number | null;
  readonly historyProgress: FleetHistoryProgress | null;
}

export interface FleetPlan {
  readonly environment: string;
  readonly sourceSchema: string;
  readonly destinationNamespace: string;
  /** Origine du plan — validée à la lecture ; optionnelle pour les fixtures. */
  readonly provenance?: FleetPlanProvenance;
  readonly observedAt: string;
  readonly freshness: 'fresh' | 'stale' | 'clock_untrusted';
  readonly continuity: FleetPlanContinuity;
  readonly liveBlocked: boolean;
  readonly certificationBlocked: boolean;
  readonly historyAdmitted: boolean;
  readonly cutoverCheckpoint: JournalCheckpoint;
  readonly cutoverRequiredBeforeHistory: true;
  readonly journal: {
    readonly library: string;
    readonly name: string;
    readonly readerKind: 'multi_object';
    readonly readerCount: 1;
  };
  readonly identity: {
    readonly keyedCount: number;
    readonly rrnCount: number;
    readonly blockedCount: number;
    readonly keyed: readonly FleetTableName[];
    readonly rrn: readonly FleetTableName[];
    readonly blocked: readonly FleetTableName[];
  };
  readonly observedTotals: {
    readonly tableCount: number;
    readonly rowCount: number;
    readonly dataSize: number;
  };
  readonly historical: {
    readonly maxConcurrency: number;
    readonly byteBudget: number | null;
    readonly admittedCount: number;
    readonly excludedCount: number;
  };
  readonly cost: { readonly status: 'unknown'; readonly observed: null; readonly unknownBecause: string };
  readonly tables: readonly FleetPlanTable[];
}

export type FleetRuntimePhase =
  | 'NOT_PREPARED'
  | 'PREPARED'
  | 'HISTORICAL'
  | 'CATCHING_UP'
  | 'LIVE'
  | 'RECONCILING'
  | 'CERTIFIED'
  | 'PAUSED'
  | 'BLOCKED'
  | 'UNKNOWN';

/** Une voie expose sa phase domaine réelle : READY n'existe qu'au niveau
 * table — le reste partage le vocabulaire de la phase agrégée. */
export type FleetRuntimeTablePhase = FleetRuntimePhase | 'READY';

export interface FleetRuntimeTableState {
  readonly name: FleetTableName;
  readonly phase: FleetRuntimeTablePhase;
  /** Compteurs mesurés de la copie initiale — null tant que rien n'est mesuré. */
  readonly copiedRows: number | null;
  readonly totalRows: number | null;
}

export interface FleetRuntime {
  readonly formatVersion: string;
  readonly fleetId: string;
  readonly environment: string;
  readonly pipelineId: string;
  readonly phase: FleetRuntimePhase;
  readonly checkpoint: JournalCheckpoint | null;
  readonly capabilities: Readonly<Record<CapabilityId, FleetCapability>>;
  readonly tableStates: readonly FleetRuntimeTableState[];
}

/* ------------------------------------------------------------------------
 * Extension S5 — pass-through projeté par le control plane.
 *
 * Le serveur émet ces blocs top-level en plus du contrat v1 (position, flux,
 * run, lag_verdict, destination). Ils sont tous optionnels et null-tolérants :
 * - clé absente ou null  → l'extension n'est pas déployée : « non instrumenté » ;
 * - membre null          → le serveur n'a pas pu le projeter : « non mesuré » ;
 * - valeur hors contrat  → rejetée à null par le parseur, jamais reflétée.
 * Aucun de ces champs n'est un 6e stage : la chaîne reste à cinq étapes.
 * --------------------------------------------------------------------- */

/** Position journal projetée — checkpoint de lecture et tail de la source. */
export interface JournalPosition {
  readonly checkpoint: JournalCheckpoint | null;
  readonly sourceTail: JournalCheckpoint | null;
  readonly receiverFirstSequence: number | null;
  readonly receiverLastSequence: number | null;
}

/** Identité publique du flux CDC — libellés sûrs, jamais d'hôte ni secret. */
export interface FluxIdentity {
  readonly id: string | null;
  readonly label: string | null;
  readonly journal: string | null;
  readonly journalLibrary: string | null;
  readonly objects: readonly string[] | null;
  readonly readerPath: string | null;
  readonly target: string | null;
  readonly job: string | null;
}

/** Diagnostic reformulé du dernier arrêt — type allowlisté, tête expurgée. */
export interface RunDiagnostic {
  readonly type: string | null;
  readonly head: string | null;
  readonly at: string | null;
}

/** Pause demandée par la source — échéance et code raison projetés. */
export interface SourcePause {
  readonly retryAfter: string | null;
  readonly reasonCode: string | null;
}

export interface RunProjection {
  readonly state: string | null;
  readonly startedAt: string | null;
  readonly elapsedSeconds: number | null;
  readonly stoppedBecause: string | null;
  readonly diagnostic: RunDiagnostic | null;
  readonly sourcePause: SourcePause | null;
}

/** Preuve de livraison projetée — checkpoints et comptes côté destination. */
export interface DestinationProjection {
  readonly kind: string | null;
  readonly database: string | null;
  readonly schema: string | null;
  readonly stage: string | null;
  readonly rawTable: string | null;
  readonly canonicalTable: string | null;
  readonly runTag: string | null;
  readonly observedAt: string | null;
  readonly loadCheckpoint: JournalCheckpoint | null;
  readonly applyCheckpoint: JournalCheckpoint | null;
  readonly sourceEvents: number | null;
  readonly rawRows: number | null;
  readonly canonicalRows: number | null;
  readonly duplicates: number | null;
}

/** Origine du plan de flotte — validée à la lecture, conservée pour L3. */
export interface FleetPlanProvenance {
  readonly kind: string;
  readonly catalogFormat: string;
  readonly planFormat: string;
  readonly observedAt: string;
}

/** État de reprise projeté — servi quand la capture est parquée. « ready »
 *  signifie : cause de l'arrêt résolue par une sonde fraîche + continuité
 *  prouvée ; jamais une reprise automatique — l'opérateur décide. */
export interface ResumeReadiness {
  readonly state: 'ready' | 'blocked' | 'unavailable';
  readonly authObservedAt: string | null;
  readonly checkpoint: JournalCheckpoint | null;
  readonly tail: JournalCheckpoint | null;
  readonly backlogSequences: number | null;
  readonly backlogReceivers: number | null;
}

export interface WarehouseCosts {
  readonly scope: 'warehouse';
  readonly warehouse: string;
  readonly pricePerCredit: string | null;
  readonly currency: string | null;
  readonly amount: string | null;
}

export interface Pipeline {
  readonly infrastructureCosts?: InfrastructureCosts | null;
  readonly costs?: WarehouseCosts | null;
  readonly id: string;
  readonly environment: string;
  readonly status: PipelineStatus;
  readonly quality: ObservationQuality;
  readonly summary: string;
  readonly observedAt: string;
  readonly stages: readonly Stage[];
  readonly lagSequences: number | null;
  readonly lagSeconds: number | null;
  readonly lagSeries: readonly LagPoint[];
  readonly lagSeriesResolutionSeconds: number | null;
  readonly lagSampleCount: number;
  readonly lagUnknownSampleCount: number;
  readonly counters: Readonly<Record<string, number | null>>;
  readonly incident: Incident | null;
  /** Toujours présent après parsing ; optionnel pour les fixtures typées historiques. */
  readonly observability?: Observability;
  readonly windowDelivery?: WindowDelivery | null;
  readonly fleet?: Fleet | null;
  readonly fleetPlan?: FleetPlan | null;
  readonly fleetRuntime?: FleetRuntime | null;
  /** Extension S5 — absente (undefined) tant que le serveur ne la projette pas. */
  readonly position?: JournalPosition | null;
  readonly flux?: FluxIdentity | null;
  readonly run?: RunProjection | null;
  readonly lagVerdict?: string | null;
  readonly lagVerdictReason?: string | null;
  readonly destination?: DestinationProjection | null;
  readonly destinationReason?: string | null;
  readonly resume?: ResumeReadiness | null;
}

export interface Source {
  readonly id: string;
  readonly evidenceKind: EvidenceKind;
  readonly environment: string;
  readonly status: 'available' | 'unavailable';
  readonly error: string | null;
}

export interface Overview {
  readonly revision: number;
  readonly generatedAt: string;
  readonly scope: Scope;
  readonly pipelines: readonly Pipeline[];
  readonly sources: readonly Source[];
}

export class ControlPlaneParseError extends Error {
  readonly field: string;

  constructor(field: string) {
    super(`Réponse control plane invalide : ${field}`);
    this.name = 'ControlPlaneParseError';
    this.field = field;
  }
}

const pipelineStatuses = new Set<PipelineStatus>([
  'healthy', 'recovering', 'degraded', 'incident', 'unknown', 'planned_stop', 'awaiting_resume',
]);
const coverages = new Set<Coverage>(['complete', 'partial', 'gap', 'none']);
const freshnesses = new Set<Freshness>(['fresh', 'late', 'stale', 'clock_untrusted']);
const evidenceKinds = new Set<EvidenceKind>(['live', 'historical', 'simulation']);
const scopeKinds = new Set<ScopeKind>(['single', 'mixed', 'unavailable']);
const stageIds = new Set<StageId>(['source', 'capture', 'raw', 'load', 'destination']);
const canonicalStageIds: readonly StageId[] = ['source', 'capture', 'raw', 'load', 'destination'];
const stageStatuses = new Set<StageStatus>(['healthy', 'degraded', 'incident', 'unknown', 'planned_stop', 'awaiting_resume']);
const incidentCodes = new Set<IncidentCode>([
  'capture_stopped_fail_closed',
  'destination_configuration_missing',
  'destination_configuration_invalid',
  'destination_credential_missing',
  'destination_credential_denied',
  'destination_credential_invalid',
  'destination_unreachable',
  'destination_authorization_denied',
  'destination_contract_incompatible',
  'destination_environment_mismatch',
  'destination_activation_inconsistent',
  'destination_source_checkpoint_mismatch',
  'destination_load_failed',
  'destination_load_timeout',
  'destination_load_contract_invalid',
  'destination_load_checkpoint_conflict',
  'destination_apply_failed',
  'destination_apply_timeout',
  'destination_apply_contract_invalid',
  'destination_apply_checkpoint_conflict',
  'destination_load_ahead_of_source',
  'destination_apply_ahead_of_load',
  'destination_reconciliation_mismatch',
  'destination_reconciliation_failed',
  'destination_reconciliation_inconsistent',
]);
const incidentTypes = new Set<IncidentType>(['capture_connection_failure', 'capture_timeout', 'capture_stopped', 'capture_auth_blocked', 'capture_position_review', 'destination']);
const sloSignalStatuses = new Set<SloSignalStatus>(['pass', 'breach', 'unobserved']);
const sloStatuses = new Set<SloStatus>(['pass', 'breach', 'unobserved', 'unavailable']);
const sloAlertLifecycles = new Set<SloAlertLifecycle>(['firing', 'resolved']);
const sloAlertSeverities = new Set<SloAlertSeverity>(['none', 'warning', 'critical']);
const observabilityCoverages = new Set<Observability['quality']['coverage']>(['complete', 'partial', 'none']);
const observabilityFreshnesses = new Set<Observability['quality']['freshness']>(['fresh', 'stale', 'clock_untrusted', 'unavailable']);
const requiredSloCheckIds = new Set([
  'capture_freshness',
  'capture_state',
  'capture_errors',
  'checkpoint_lag',
  's3_freshness',
  's3_requests',
  'snowpipe_queue',
  'canonical_freshness',
  'delivery_latency_p95',
  'delivery_latency_p99',
  'observability_freshness',
  'reconciliation',
  'snowflake_credits',
]);
const publicSloTokenPattern = /^[a-z][a-z0-9_]{0,63}$/;
const publicSloValueTokenPattern = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const publicSloUnitPattern = /^[a-z][a-z0-9/]{0,31}$/;
const publicSha256Pattern = /^sha256:[0-9a-f]{64}$/;
const utcTimestampPattern = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|\+00:00)$/;
const capabilityIds: readonly CapabilityId[] = ['refresh', 'prepare', 'start', 'pause', 'resume'];
const capabilityStates = new Set<CapabilityState>(['available', 'unavailable']);
const fleetPhases = new Set<FleetPhase>([
  'NOT_PREPARED', 'READY', 'HISTORICAL', 'CATCHING_UP', 'LIVE', 'RECONCILING', 'CERTIFIED', 'PAUSED', 'BLOCKED',
]);
const fleetBlockedReasons = new Set<FleetBlockedReason>([
  'receiver_discontinuity',
  'sequence_gap',
  'unknown_history_progress',
  'missing_start_checkpoint',
  'unproven_continuity',
  'journal_tail_unobserved',
  'cost_overrun',
  'unknown_cost',
  'operator_stop',
]);
const fleetSafeActions = new Set<FleetSafeAction>([
  'PREPARE',
  'ADMIT_HISTORICAL',
  'RECORD_HISTORY_PROGRESS',
  'PROVE_CONTINUITY',
  'CATCH_UP_TO_TAIL',
  'RECORD_ACTUAL_COST',
  'OPEN_RECONCILIATION',
  'CERTIFY',
  'RESUME',
  'INSPECT_BLOCKED',
  'NONE',
]);
const backfillPhases = new Set<FleetPhase>(['HISTORICAL', 'CATCHING_UP']);
const fleetTokenPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const fleetKeys = [
  'fleet_id',
  'format_version',
  'environment',
  'destination_namespace',
  'max_concurrency',
  'credit_budget',
  'consumed_credits',
  'reserved_credits',
  'tables',
  'summary',
  'capabilities',
] as const;
const fleetTableKeys = [
  'name',
  'phase',
  'start_checkpoint',
  'current_checkpoint',
  'journal_tail',
  'receiver_chain',
  'copied_rows',
  'total_rows',
  'continuity_proven',
  'gap',
  'proof_window',
  'paused_from',
  'blocked_reason',
  'admitted',
  'estimated_credits',
  'reserved_credits',
  'actual_credits',
  'reconciliation_proof',
] as const;
const fleetPlanKeys = [
  'environment', 'source_schema', 'destination_namespace', 'observed_at', 'provenance',
  'freshness', 'continuity', 'live_blocked', 'certification_blocked', 'live_promise',
  'certification_promise', 'promise_blockers', 'history_admitted', 'cutover_checkpoint',
  'cutover_required_before_history', 'journal', 'identity', 'observed_totals', 'historical',
  'cost', 'tables',
] as const;
const fleetPlanTableKeys = [
  'name', 'row_count', 'data_size', 'journal_images', 'identity_status', 'identity_source',
  'candidate_key', 'live_possible', 'certification_possible', 'historical_admitted',
  'historical_lane', 'blocked_reasons', 'copied_rows', 'copied_bytes', 'history_progress',
] as const;
const historyProgressKeys = [
  'status', 'run_id', 'updated_at', 'planned_rows', 'published_rows', 'published_bytes',
  'published_objects',
  'chunks_pending', 'chunks_running', 'chunks_read_done', 'chunks_published', 'chunks_failed',
] as const;
const historyProgressStatuses = new Set<FleetHistoryStatus>(['running', 'complete', 'failed', 'invalid']);
const fleetPlanContinuities = new Set<FleetPlanContinuity>(['proven', 'uncertain', 'broken']);
const fleetPlanFreshnesses = new Set<FleetPlan['freshness']>(['fresh', 'stale', 'clock_untrusted']);
const fleetRuntimePhases = new Set<FleetRuntimePhase>([
  'NOT_PREPARED', 'PREPARED', 'HISTORICAL', 'CATCHING_UP', 'LIVE',
  'RECONCILING', 'CERTIFIED', 'PAUSED', 'BLOCKED', 'UNKNOWN',
]);
const fleetRuntimeTablePhases = new Set<FleetRuntimeTablePhase>([
  ...fleetRuntimePhases, 'READY',
]);
const fleetRuntimeKeys = [
  'format_version', 'fleet_id', 'environment', 'pipeline_id', 'phase', 'checkpoint', 'capabilities', 'table_states',
] as const;
const fleetRuntimeTableKeys = ['name', 'phase', 'copied_rows', 'total_rows'] as const;
const checkpointKeys = ['receiver', 'sequence'] as const;
const spanKeys = ['receiver', 'first_sequence', 'last_sequence'] as const;
const windowKeys = ['start_utc', 'end_utc'] as const;
const proofKeys = [
  'window',
  'source_count',
  'target_count',
  'missing',
  'extra',
  'duplicates',
  'source_hash',
  'target_hash',
  'destination_freshness_seconds',
  'freshness_slo_seconds',
  'latency_seconds',
  'throughput_rows_per_second',
  'cost_units',
] as const;
const summaryKeys = [
  'certified_count',
  'table_count',
  'running_count',
  'admitted_count',
  'known_copied_rows',
  'known_total_rows',
  'credit_budget',
  'consumed_credits',
  'reserved_credits',
  'over_budget',
  'next_action',
  'next_reason',
  'next_table',
] as const;

export function parseOverview(value: unknown): Overview {
  const object = record(value, 'overview');
  const pipelines = array(object.pipelines, 'pipelines').map((item, index) => parsePipeline(item, `pipelines[${index}]`));
  const sources = array(object.sources, 'sources').map((item, index) => parseSource(item, `sources[${index}]`));
  const ids = new Set<string>();
  for (const pipeline of pipelines) {
    if (ids.has(pipeline.id)) fail('pipelines: duplicate id');
    ids.add(pipeline.id);
  }
  return {
    revision: nonNegativeInteger(object.revision, 'revision'),
    generatedAt: timestamp(object.generated_at, 'generated_at'),
    scope: parseScope(object.scope, [...pipelines, ...sources].map((item) => item.environment)),
    pipelines,
    sources,
  };
}

function parseScope(value: unknown, observedEnvironments: readonly string[]): Scope {
  const object = record(value, 'scope');
  const kind = enumValue(object.kind, scopeKinds, 'scope.kind');
  const environments = array(object.environments, 'scope.environments')
    .map((value, index) => environment(value, `scope.environments[${index}]`));
  const observed = [...new Set(observedEnvironments)].sort();
  if (new Set(environments).size !== environments.length) fail('scope.environments');
  if (
    (kind === 'single' && environments.length !== 1)
    || (kind === 'mixed' && environments.length < 2)
    || (kind === 'unavailable' && environments.length !== 0)
    || environments.length !== observed.length
    || environments.some((environment, index) => environment !== observed[index])
  ) fail('scope');
  return { kind, environments };
}

export function parsePipeline(value: unknown, field = 'pipeline'): Pipeline {
  const object = record(value, field);
  const quality = parseQuality(object.quality, `${field}.quality`);
  const stages = array(object.stages, `${field}.stages`).map((item, index) => parseStage(item, `${field}.stages[${index}]`));
  const ids = new Set<string>();
  for (const stage of stages) {
    if (ids.has(stage.id)) fail(`${field}.stages: duplicate id`);
    ids.add(stage.id);
  }
  if (
    stages.length !== canonicalStageIds.length
    || stages.some((stage, index) => stage.id !== canonicalStageIds[index])
  ) fail(`${field}.stages`);
  const lagSeries = parseLagSeries(object.lag_series, `${field}.lag_series`);
  const pipeline: Pipeline = {
    id: text(object.id, `${field}.id`),
    environment: environment(object.environment, `${field}.environment`),
    status: enumValue(object.status, pipelineStatuses, `${field}.status`),
    quality,
    summary: text(object.summary, `${field}.summary`),
    observedAt: timestamp(object.observed_at, `${field}.observed_at`),
    stages,
    lagSequences: nullableNumber(object.lag_sequences, `${field}.lag_sequences`, true),
    lagSeconds: nullableNumber(object.lag_seconds, `${field}.lag_seconds`),
    lagSeries: lagSeries.points,
    lagSeriesResolutionSeconds: lagSeries.resolutionSeconds,
    lagSampleCount: lagSeries.sampleCount,
    lagUnknownSampleCount: lagSeries.unknownSampleCount,
    counters: numberRecord(object.counters, `${field}.counters`),
    incident: nullableIncident(object.incident, `${field}.incident`),
    observability: parseObservability(object.observability, `${field}.observability`, quality.evidenceKind),
    costs: parseCosts(object.costs, `${field}.costs`),
    infrastructureCosts: parseInfrastructureCosts(object.infrastructure_costs, `${field}.infrastructure_costs`),
    windowDelivery: parseWindowDelivery(object.window_delivery, `${field}.window_delivery`, quality.evidenceKind),
    fleet: parseFleet(object.fleet, `${field}.fleet`),
    fleetPlan: parseFleetPlan(object.fleet_plan, `${field}.fleet_plan`),
    fleetRuntime: parseFleetRuntime(object.fleet_runtime, `${field}.fleet_runtime`),
    position: passThroughBlock(object.position, parseJournalPosition),
    flux: passThroughBlock(object.flux, parseFluxIdentity),
    run: passThroughBlock(object.run, parseRunProjection),
    lagVerdict: passThroughText(object.lag_verdict),
    lagVerdictReason: passThroughText(object.lag_verdict_reason),
    destination: passThroughBlock(object.destination, parseDestinationProjection),
    destinationReason: passThroughText(object.destination_reason),
    resume: passThroughBlock(object.resume, parseResumeReadiness),
  };
  if (pipeline.status === 'healthy') {
    const quality = pipeline.quality;
    if (
      quality.coverage !== 'complete'
      || quality.freshness !== 'fresh'
      || quality.evidenceKind !== 'live'
      || stages.some((stage) => stage.status !== 'healthy')
    ) fail(`${field}.status`);
  }
  return pipeline;
}

export function parseFleet(value: unknown, field = 'fleet'): Fleet | null {
  if (value === undefined || value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, fleetKeys, field);
  if (object.fleet_id !== site().fleetId) fail(`${field}.fleet_id`);
  if (object.format_version !== FLEET_FORMAT_VERSION) fail(`${field}.format_version`);
  if (object.environment !== site().environment) fail(`${field}.environment`);
  if (object.destination_namespace !== site().destinationNamespace) fail(`${field}.destination_namespace`);
  const maxConcurrency = nonNegativeInteger(object.max_concurrency, `${field}.max_concurrency`);
  if (maxConcurrency < FLEET_MIN_CONCURRENCY || maxConcurrency > FLEET_MAX_CONCURRENCY) {
    fail(`${field}.max_concurrency`);
  }
  const creditBudget = nonNegativeNumber(object.credit_budget, `${field}.credit_budget`);
  const consumedCredits = nonNegativeNumber(object.consumed_credits, `${field}.consumed_credits`);
  const reservedCredits = nonNegativeNumber(object.reserved_credits, `${field}.reserved_credits`);
  const tables = array(object.tables, `${field}.tables`).map((item, index) => (
    parseFleetTable(item, `${field}.tables[${index}]`)
  ));
  if (tables.length !== site().manifest.length) fail(`${field}.tables`);
  const seen = new Set<string>();
  tables.forEach((table, index) => {
    if (table.name !== site().manifest[index] || seen.has(table.name)) fail(`${field}.tables`);
    seen.add(table.name);
  });
  const summedReserved = tables.reduce((sum, table) => sum + (table.reservedCredits ?? 0), 0);
  if (summedReserved !== reservedCredits) fail(`${field}.reserved_credits`);
  const runningCount = tables.filter((table) => backfillPhases.has(table.phase)).length;
  if (runningCount > maxConcurrency) fail(`${field}.max_concurrency`);
  const summary = parseFleetSummary(object.summary, `${field}.summary`, {
    tables,
    creditBudget,
    consumedCredits,
    reservedCredits,
  });
  return {
    fleetId: site().fleetId,
    formatVersion: FLEET_FORMAT_VERSION,
    environment: site().environment,
    destinationNamespace: site().destinationNamespace,
    maxConcurrency,
    creditBudget,
    consumedCredits,
    reservedCredits,
    tables,
    summary,
    capabilities: parseFleetCapabilities(object.capabilities, `${field}.capabilities`),
  };
}

export function parseFleetPlan(value: unknown, field = 'fleet_plan'): FleetPlan | null {
  if (value === undefined || value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, fleetPlanKeys, field);
  if (object.environment !== site().environment) fail(`${field}.environment`);
  if (object.destination_namespace !== site().destinationNamespace) fail(`${field}.destination_namespace`);
  const observedAt = timestamp(object.observed_at, `${field}.observed_at`);
  const freshness = enumValue(object.freshness, fleetPlanFreshnesses, `${field}.freshness`);
  const continuity = enumValue(object.continuity, fleetPlanContinuities, `${field}.continuity`);
  const liveBlocked = booleanValue(object.live_blocked, `${field}.live_blocked`);
  const certificationBlocked = booleanValue(object.certification_blocked, `${field}.certification_blocked`);
  if (object.live_promise !== (liveBlocked ? 'blocked' : 'possible')) fail(`${field}.live_promise`);
  if (object.certification_promise !== (certificationBlocked ? 'blocked' : 'possible')) {
    fail(`${field}.certification_promise`);
  }
  stringList(object.promise_blockers, `${field}.promise_blockers`);

  const provenance = record(object.provenance, `${field}.provenance`);
  assertExactKeys(provenance, ['kind', 'catalog_format', 'plan_format', 'observed_at'], `${field}.provenance`);
  if (
    provenance.kind !== 'metadata_catalog'
    || provenance.catalog_format !== 'quadringent-fleet-catalog-v1'
    || provenance.plan_format !== 'quadringent-fleet-plan-v1'
    || timestamp(provenance.observed_at, `${field}.provenance.observed_at`) !== observedAt
  ) fail(`${field}.provenance`);

  const checkpoint = parseCheckpoint(object.cutover_checkpoint, `${field}.cutover_checkpoint`);
  if (checkpoint === null || object.cutover_required_before_history !== true) fail(`${field}.cutover_checkpoint`);

  const journalObject = record(object.journal, `${field}.journal`);
  assertExactKeys(
    journalObject,
    ['library', 'name', 'reader_kind', 'reader_count', 'table_names', 'continuity'],
    `${field}.journal`,
  );
  if (
    journalObject.reader_kind !== 'multi_object'
    || journalObject.reader_count !== 1
    || journalObject.continuity !== continuity
    || !sameStrings(journalObject.table_names, site().manifest)
  ) fail(`${field}.journal`);

  const historicalObject = record(object.historical, `${field}.historical`);
  assertExactKeys(
    historicalObject,
    ['max_concurrency', 'byte_budget', 'admitted_count', 'excluded_count', 'lanes'],
    `${field}.historical`,
  );
  const maxConcurrency = nonNegativeInteger(historicalObject.max_concurrency, `${field}.historical.max_concurrency`);
  if (maxConcurrency < FLEET_MIN_CONCURRENCY || maxConcurrency > FLEET_MAX_CONCURRENCY) {
    fail(`${field}.historical.max_concurrency`);
  }
  const byteBudget = nullableInteger(historicalObject.byte_budget, `${field}.historical.byte_budget`);
  const admittedCount = nonNegativeInteger(historicalObject.admitted_count, `${field}.historical.admitted_count`);
  const excludedCount = nonNegativeInteger(historicalObject.excluded_count, `${field}.historical.excluded_count`);

  const tables = array(object.tables, `${field}.tables`).map((item, index) => (
    parseFleetPlanTable(item, `${field}.tables[${index}]`, maxConcurrency)
  ));
  if (tables.length !== site().manifest.length) fail(`${field}.tables`);
  tables.forEach((table, index) => {
    if (table.name !== site().manifest[index]) fail(`${field}.tables`);
  });
  if (
    tables.filter((table) => table.historicalAdmitted).length !== admittedCount
    || admittedCount + excludedCount !== site().manifest.length
    || booleanValue(object.history_admitted, `${field}.history_admitted`) !== (admittedCount > 0)
  ) fail(`${field}.historical`);
  parseFleetPlanLanes(historicalObject.lanes, `${field}.historical.lanes`, tables, maxConcurrency);

  const identityObject = record(object.identity, `${field}.identity`);
  assertExactKeys(identityObject, ['keyed_count', 'rrn_count', 'blocked_count', 'keyed', 'rrn', 'blocked'], `${field}.identity`);
  const keyed = tables.filter((table) => table.identityStatus === 'keyed').map((table) => table.name);
  const rrn = tables.filter((table) => table.identityStatus === 'rrn').map((table) => table.name);
  const blocked = tables.filter((table) => table.identityStatus === 'blocked').map((table) => table.name);
  if (
    nonNegativeInteger(identityObject.keyed_count, `${field}.identity.keyed_count`) !== keyed.length
    || nonNegativeInteger(identityObject.rrn_count, `${field}.identity.rrn_count`) !== rrn.length
    || nonNegativeInteger(identityObject.blocked_count, `${field}.identity.blocked_count`) !== blocked.length
    || !sameStrings(identityObject.keyed, keyed)
    || !sameStrings(identityObject.rrn, rrn)
    || !sameStrings(identityObject.blocked, blocked)
  ) fail(`${field}.identity`);

  const totalsObject = record(object.observed_totals, `${field}.observed_totals`);
  assertExactKeys(totalsObject, ['table_count', 'row_count', 'data_size'], `${field}.observed_totals`);
  const tableCount = nonNegativeInteger(totalsObject.table_count, `${field}.observed_totals.table_count`);
  const rowCount = nonNegativeInteger(totalsObject.row_count, `${field}.observed_totals.row_count`);
  const dataSize = nonNegativeInteger(totalsObject.data_size, `${field}.observed_totals.data_size`);
  if (
    tableCount !== site().manifest.length
    || rowCount !== tables.reduce((sum, table) => sum + table.rowCount, 0)
    || dataSize !== tables.reduce((sum, table) => sum + table.dataSize, 0)
  ) fail(`${field}.observed_totals`);

  const costObject = record(object.cost, `${field}.cost`);
  assertExactKeys(costObject, ['status', 'observed', 'unknown_because'], `${field}.cost`);
  if (costObject.status !== 'unknown' || costObject.observed !== null) fail(`${field}.cost`);
  const unknownBecause = text(costObject.unknown_because, `${field}.cost.unknown_because`);

  return {
    environment: site().environment,
    sourceSchema: text(object.source_schema, `${field}.source_schema`),
    destinationNamespace: site().destinationNamespace,
    provenance: {
      kind: text(provenance.kind, `${field}.provenance.kind`),
      catalogFormat: text(provenance.catalog_format, `${field}.provenance.catalog_format`),
      planFormat: text(provenance.plan_format, `${field}.provenance.plan_format`),
      observedAt: timestamp(provenance.observed_at, `${field}.provenance.observed_at`),
    },
    observedAt,
    freshness,
    continuity,
    liveBlocked,
    certificationBlocked,
    historyAdmitted: admittedCount > 0,
    cutoverCheckpoint: checkpoint,
    cutoverRequiredBeforeHistory: true,
    journal: {
      library: text(journalObject.library, `${field}.journal.library`),
      name: text(journalObject.name, `${field}.journal.name`),
      readerKind: 'multi_object',
      readerCount: 1,
    },
    identity: {
      keyedCount: keyed.length,
      rrnCount: rrn.length,
      blockedCount: blocked.length,
      keyed,
      rrn,
      blocked,
    },
    observedTotals: { tableCount, rowCount, dataSize },
    historical: { maxConcurrency, byteBudget, admittedCount, excludedCount },
    cost: { status: 'unknown', observed: null, unknownBecause },
    tables,
  };
}

export function parseFleetRuntime(value: unknown, field = 'fleet_runtime'): FleetRuntime | null {
  if (value === undefined || value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, fleetRuntimeKeys, field);
  if (object.format_version !== FLEET_RUNTIME_FORMAT_VERSION) fail(`${field}.format_version`);
  if (object.fleet_id !== site().fleetId) fail(`${field}.fleet_id`);
  if (object.environment !== site().runtimeEnvironment) fail(`${field}.environment`);
  if (object.pipeline_id !== site().runtimePipelineId) fail(`${field}.pipeline_id`);
  const phase = enumValue(object.phase, fleetRuntimePhases, `${field}.phase`);
  const checkpoint = parseRuntimeCheckpoint(object.checkpoint, `${field}.checkpoint`);
  const capabilities = parseFleetCapabilities(object.capabilities, `${field}.capabilities`);
  const tableStates = array(object.table_states, `${field}.table_states`).map((item, index) => (
    parseFleetRuntimeTableState(item, `${field}.table_states[${index}]`)
  ));
  if (tableStates.length !== site().manifest.length) fail(`${field}.table_states`);
  tableStates.forEach((table, index) => {
    if (table.name !== site().manifest[index]) fail(`${field}.table_states`);
  });
  return {
    formatVersion: FLEET_RUNTIME_FORMAT_VERSION,
    fleetId: site().fleetId,
    environment: site().runtimeEnvironment,
    pipelineId: site().runtimePipelineId,
    phase,
    checkpoint,
    capabilities,
    tableStates,
  };
}

function parseFleetRuntimeTableState(value: unknown, field: string): FleetRuntimeTableState {
  const object = record(value, field);
  assertExactKeys(object, fleetRuntimeTableKeys, field);
  const name = text(object.name, `${field}.name`);
  if (!site().manifest.includes(name as FleetTableName)) fail(`${field}.name`);
  const copiedRows = nullableInteger(object.copied_rows, `${field}.copied_rows`);
  const totalRows = nullableInteger(object.total_rows, `${field}.total_rows`);
  if (copiedRows !== null && totalRows !== null && copiedRows > totalRows) fail(`${field}.copied_rows`);
  return {
    name: name as FleetTableName,
    phase: enumValue(object.phase, fleetRuntimeTablePhases, `${field}.phase`),
    copiedRows,
    totalRows,
  };
}

function parseRuntimeCheckpoint(value: unknown, field: string): JournalCheckpoint | null {
  if (value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, checkpointKeys, field);
  const receiver = text(object.receiver, `${field}.receiver`);
  if (receiver !== receiver.trim()) fail(`${field}.receiver`);
  return {
    receiver,
    sequence: nonNegativeInteger(object.sequence, `${field}.sequence`),
  };
}

function parseFleetPlanTable(value: unknown, field: string, maxConcurrency: number): FleetPlanTable {
  const object = record(value, field);
  assertExactKeys(object, fleetPlanTableKeys, field);
  const name = text(object.name, `${field}.name`);
  if (!site().manifest.includes(name as FleetTableName)) fail(`${field}.name`);
  const identityStatus = enumValue(
    object.identity_status,
    new Set<FleetPlanIdentityStatus>(['keyed', 'rrn', 'blocked']),
    `${field}.identity_status`,
  );
  const candidateKey = object.candidate_key === null
    ? null
    : stringList(object.candidate_key, `${field}.candidate_key`);
  const identitySource = object.identity_source === null
    ? null
    : text(object.identity_source, `${field}.identity_source`);
  // keyed et rrn portent tous deux une cle de correspondance : metier pour
  // keyed, position physique (_rrn) pour rrn. Seul blocked n'en a pas.
  const hasIdentity = identityStatus === 'keyed' || identityStatus === 'rrn';
  if (hasIdentity !== (candidateKey !== null && candidateKey.length > 0 && identitySource !== null)) {
    fail(`${field}.identity_status`);
  }
  const historicalAdmitted = booleanValue(object.historical_admitted, `${field}.historical_admitted`);
  const historicalLane = nullableInteger(object.historical_lane, `${field}.historical_lane`);
  if (
    (historicalLane !== null) !== historicalAdmitted
    || (historicalLane !== null && (historicalLane < 1 || historicalLane > maxConcurrency))
  ) {
    fail(`${field}.historical_lane`);
  }
  booleanValue(object.live_possible, `${field}.live_possible`);
  booleanValue(object.certification_possible, `${field}.certification_possible`);
  const journalImages = enumValue(object.journal_images, new Set(['*AFTER', '*BOTH', '*BEFORE'] as const), `${field}.journal_images`);
  const copiedRows = nullableInteger(object.copied_rows, `${field}.copied_rows`);
  const copiedBytes = nullableInteger(object.copied_bytes, `${field}.copied_bytes`);
  const historyProgress = parseHistoryProgress(object.history_progress, `${field}.history_progress`);
  // Rejoue la règle du sidecar : jamais de lignes ni d'octets copiés sans
  // progression observée, et la progression observée rend exactement les
  // lignes et les octets copiés.
  if (historyProgress === null || historyProgress.status === 'invalid') {
    if (copiedRows !== null) fail(`${field}.copied_rows`);
    if (copiedBytes !== null) fail(`${field}.copied_bytes`);
  } else {
    if (historyProgress.publishedRows !== copiedRows) fail(`${field}.copied_rows`);
    if (historyProgress.publishedBytes !== copiedBytes) fail(`${field}.copied_bytes`);
  }
  return {
    name: name as FleetTableName,
    rowCount: nonNegativeInteger(object.row_count, `${field}.row_count`),
    dataSize: nonNegativeInteger(object.data_size, `${field}.data_size`),
    journalImages,
    identityStatus,
    identitySource,
    candidateKey,
    historicalAdmitted,
    historicalLane,
    blockedReasons: stringList(object.blocked_reasons, `${field}.blocked_reasons`),
    copiedRows,
    copiedBytes,
    historyProgress,
  };
}

function parseHistoryProgress(value: unknown, field: string): FleetHistoryProgress | null {
  if (value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, historyProgressKeys, field);
  const status = enumValue(object.status, historyProgressStatuses, `${field}.status`);
  if (status === 'invalid') {
    for (const key of historyProgressKeys) {
      if (key !== 'status' && object[key] !== null) fail(`${field}.${key}`);
    }
    return {
      status,
      runId: null,
      updatedAt: null,
      plannedRows: null,
      publishedRows: null,
      publishedBytes: null,
      publishedObjects: null,
      chunksPending: null,
      chunksRunning: null,
      chunksReadDone: null,
      chunksPublished: null,
      chunksFailed: null,
    };
  }
  return {
    status,
    runId: text(object.run_id, `${field}.run_id`),
    updatedAt: timestamp(object.updated_at, `${field}.updated_at`),
    plannedRows: nonNegativeInteger(object.planned_rows, `${field}.planned_rows`),
    publishedRows: nonNegativeInteger(object.published_rows, `${field}.published_rows`),
    publishedBytes: nonNegativeInteger(object.published_bytes, `${field}.published_bytes`),
    publishedObjects: nonNegativeInteger(object.published_objects, `${field}.published_objects`),
    chunksPending: nonNegativeInteger(object.chunks_pending, `${field}.chunks_pending`),
    chunksRunning: nonNegativeInteger(object.chunks_running, `${field}.chunks_running`),
    chunksReadDone: nonNegativeInteger(object.chunks_read_done, `${field}.chunks_read_done`),
    chunksPublished: nonNegativeInteger(object.chunks_published, `${field}.chunks_published`),
    chunksFailed: nonNegativeInteger(object.chunks_failed, `${field}.chunks_failed`),
  };
}

function parseFleetPlanLanes(
  value: unknown,
  field: string,
  tables: readonly FleetPlanTable[],
  maxConcurrency: number,
): void {
  const expected = new Map(tables.filter((table) => table.historicalLane !== null).map((table) => [table.name, table]));
  const seen = new Set<string>();
  for (const [index, item] of array(value, field).entries()) {
    const lane = record(item, `${field}[${index}]`);
    assertExactKeys(lane, ['slot', 'tables', 'row_count', 'data_size'], `${field}[${index}]`);
    const slot = nonNegativeInteger(lane.slot, `${field}[${index}].slot`);
    if (slot < 1 || slot > maxConcurrency) fail(`${field}[${index}].slot`);
    const names = stringList(lane.tables, `${field}[${index}].tables`);
    const laneTables = names.map((name) => expected.get(name as FleetTableName));
    if (laneTables.some((table) => table === undefined)) fail(`${field}[${index}].tables`);
    for (const table of laneTables) {
      if (!table || table.historicalLane !== slot || seen.has(table.name)) fail(`${field}[${index}].tables`);
      seen.add(table.name);
    }
    if (
      nonNegativeInteger(lane.row_count, `${field}[${index}].row_count`) !== laneTables.reduce((sum, table) => sum + (table?.rowCount ?? 0), 0)
      || nonNegativeInteger(lane.data_size, `${field}[${index}].data_size`) !== laneTables.reduce((sum, table) => sum + (table?.dataSize ?? 0), 0)
    ) fail(`${field}[${index}]`);
  }
  if (seen.size !== expected.size) fail(field);
}

function stringList(value: unknown, field: string): readonly string[] {
  return array(value, field).map((item, index) => text(item, `${field}[${index}]`));
}

function sameStrings(value: unknown, expected: readonly string[]): boolean {
  if (!Array.isArray(value) || value.length !== expected.length) return false;
  return value.every((item, index) => item === expected[index]);
}

function parseFleetTable(value: unknown, field: string): FleetTable {
  const object = record(value, field);
  assertExactKeys(object, fleetTableKeys, field);
  const name = text(object.name, `${field}.name`);
  if (!site().manifest.includes(name as FleetTableName)) fail(`${field}.name`);
  const phase = enumValue(object.phase, fleetPhases, `${field}.phase`);
  const startCheckpoint = parseCheckpoint(object.start_checkpoint, `${field}.start_checkpoint`);
  const currentCheckpoint = parseCheckpoint(object.current_checkpoint, `${field}.current_checkpoint`);
  const journalTail = parseCheckpoint(object.journal_tail, `${field}.journal_tail`);
  const receiverChain = parseReceiverChain(object.receiver_chain, `${field}.receiver_chain`);
  const copiedRows = nullableInteger(object.copied_rows, `${field}.copied_rows`);
  const totalRows = nullableInteger(object.total_rows, `${field}.total_rows`);
  if (copiedRows !== null && totalRows !== null && copiedRows > totalRows) fail(`${field}.copied_rows`);
  const continuityProven = nullableBoolean(object.continuity_proven, `${field}.continuity_proven`);
  const gap = nullableBoolean(object.gap, `${field}.gap`);
  const proofWindow = parseProofWindow(object.proof_window, `${field}.proof_window`);
  const pausedFrom = object.paused_from === null
    ? null
    : enumValue(object.paused_from, fleetPhases, `${field}.paused_from`);
  if (pausedFrom === 'PAUSED' || pausedFrom === 'BLOCKED' || pausedFrom === 'CERTIFIED') {
    fail(`${field}.paused_from`);
  }
  const blockedReason = object.blocked_reason === null
    ? null
    : enumValue(object.blocked_reason, fleetBlockedReasons, `${field}.blocked_reason`);
  const admitted = booleanValue(object.admitted, `${field}.admitted`);
  const estimatedCredits = nullableNumber(object.estimated_credits, `${field}.estimated_credits`);
  const reservedCredits = nullableNumber(object.reserved_credits, `${field}.reserved_credits`);
  const actualCredits = nullableNumber(object.actual_credits, `${field}.actual_credits`);
  const reconciliationProof = parseReconciliationProof(object.reconciliation_proof, `${field}.reconciliation_proof`);
  if (phase === 'NOT_PREPARED' && startCheckpoint !== null) fail(`${field}.start_checkpoint`);
  if (phase !== 'NOT_PREPARED' && phase !== 'BLOCKED' && phase !== 'PAUSED' && startCheckpoint === null) {
    fail(`${field}.start_checkpoint`);
  }
  if (phase === 'PAUSED') {
    if (pausedFrom === null) fail(`${field}.paused_from`);
  } else if (pausedFrom !== null) {
    fail(`${field}.paused_from`);
  }
  if (phase === 'BLOCKED') {
    if (blockedReason === null) fail(`${field}.blocked_reason`);
  } else if (blockedReason !== null) {
    fail(`${field}.blocked_reason`);
  }
  if (
    (phase === 'HISTORICAL' || phase === 'CATCHING_UP' || phase === 'LIVE' || phase === 'RECONCILING' || phase === 'CERTIFIED')
    && !admitted
  ) fail(`${field}.admitted`);
  const backfill = backfillPhases.has(phase) || (phase === 'PAUSED' && pausedFrom !== null && backfillPhases.has(pausedFrom));
  if (backfill && (estimatedCredits === null || reservedCredits === null || estimatedCredits <= 0)) {
    fail(`${field}.estimated_credits`);
  }
  if ((phase === 'LIVE' || phase === 'RECONCILING' || phase === 'CERTIFIED') && (actualCredits === null || reservedCredits !== 0)) {
    fail(`${field}.actual_credits`);
  }
  if (phase === 'RECONCILING' || phase === 'CERTIFIED') {
    if (proofWindow === null) fail(`${field}.proof_window`);
    if (phase === 'CERTIFIED' && reconciliationProof === null) fail(`${field}.reconciliation_proof`);
    if (reconciliationProof !== null && (reconciliationProof.window.startUtc !== proofWindow.startUtc
      || reconciliationProof.window.endUtc !== proofWindow.endUtc)) {
      fail(`${field}.reconciliation_proof`);
    }
  } else if (reconciliationProof !== null) {
    fail(`${field}.reconciliation_proof`);
  }
  return {
    name: name as FleetTableName,
    phase,
    startCheckpoint,
    currentCheckpoint,
    journalTail,
    receiverChain,
    copiedRows,
    totalRows,
    continuityProven,
    gap,
    proofWindow,
    pausedFrom,
    blockedReason,
    admitted,
    estimatedCredits,
    reservedCredits,
    actualCredits,
    reconciliationProof,
  };
}

function parseCheckpoint(value: unknown, field: string): JournalCheckpoint | null {
  if (value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, checkpointKeys, field);
  const receiver = fleetToken(object.receiver, `${field}.receiver`);
  const sequence = nonNegativeInteger(object.sequence, `${field}.sequence`);
  return { receiver, sequence };
}

function parseReceiverChain(value: unknown, field: string): readonly ReceiverSpan[] | null {
  if (value === null) return null;
  const items = array(value, field);
  if (items.length === 0) fail(field);
  const seen = new Set<string>();
  return items.map((item, index) => {
    const object = record(item, `${field}[${index}]`);
    assertExactKeys(object, spanKeys, `${field}[${index}]`);
    const receiver = fleetToken(object.receiver, `${field}[${index}].receiver`);
    if (seen.has(receiver)) fail(`${field}[${index}].receiver`);
    seen.add(receiver);
    const firstSequence = nonNegativeInteger(object.first_sequence, `${field}[${index}].first_sequence`);
    const lastSequence = nonNegativeInteger(object.last_sequence, `${field}[${index}].last_sequence`);
    if (lastSequence < firstSequence) fail(`${field}[${index}].last_sequence`);
    return { receiver, firstSequence, lastSequence };
  });
}

function parseProofWindow(value: unknown, field: string): ProofWindow | null {
  if (value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, windowKeys, field);
  const startUtc = alignedUtc(object.start_utc, `${field}.start_utc`);
  const endUtc = alignedUtc(object.end_utc, `${field}.end_utc`);
  if (Date.parse(startUtc) >= Date.parse(endUtc)) fail(field);
  return { startUtc, endUtc };
}

function parseReconciliationProof(value: unknown, field: string): ReconciliationProof | null {
  if (value === null) return null;
  const object = record(value, field);
  assertExactKeys(object, proofKeys, field);
  const window = parseProofWindow(object.window, `${field}.window`);
  if (window === null) fail(`${field}.window`);
  const sourceHash = fleetHash(object.source_hash, `${field}.source_hash`);
  const targetHash = fleetHash(object.target_hash, `${field}.target_hash`);
  const freshnessSloSeconds = nonNegativeNumber(object.freshness_slo_seconds, `${field}.freshness_slo_seconds`);
  if (freshnessSloSeconds === 0) fail(`${field}.freshness_slo_seconds`);
  return {
    window,
    sourceCount: nonNegativeInteger(object.source_count, `${field}.source_count`),
    targetCount: nonNegativeInteger(object.target_count, `${field}.target_count`),
    missing: nonNegativeInteger(object.missing, `${field}.missing`),
    extra: nonNegativeInteger(object.extra, `${field}.extra`),
    duplicates: nonNegativeInteger(object.duplicates, `${field}.duplicates`),
    sourceHash,
    targetHash,
    destinationFreshnessSeconds: nonNegativeNumber(
      object.destination_freshness_seconds,
      `${field}.destination_freshness_seconds`,
    ),
    freshnessSloSeconds,
    latencySeconds: nonNegativeNumber(object.latency_seconds, `${field}.latency_seconds`),
    throughputRowsPerSecond: nonNegativeNumber(
      object.throughput_rows_per_second,
      `${field}.throughput_rows_per_second`,
    ),
    costUnits: nonNegativeNumber(object.cost_units, `${field}.cost_units`),
  };
}

function parseFleetSummary(
  value: unknown,
  field: string,
  fleet: {
    readonly tables: readonly FleetTable[];
    readonly creditBudget: number;
    readonly consumedCredits: number;
    readonly reservedCredits: number;
  },
): FleetSummary {
  const object = record(value, field);
  assertExactKeys(object, summaryKeys, field);
  const certifiedCount = nonNegativeInteger(object.certified_count, `${field}.certified_count`);
  const tableCount = nonNegativeInteger(object.table_count, `${field}.table_count`);
  const runningCount = nonNegativeInteger(object.running_count, `${field}.running_count`);
  const admittedCount = nonNegativeInteger(object.admitted_count, `${field}.admitted_count`);
  if (tableCount !== site().manifest.length || certifiedCount > site().manifest.length) fail(`${field}.table_count`);
  const expectedCertified = fleet.tables.filter((table) => table.phase === 'CERTIFIED').length;
  const expectedRunning = fleet.tables.filter((table) => backfillPhases.has(table.phase)).length;
  const expectedAdmitted = fleet.tables.filter((table) => table.admitted).length;
  if (certifiedCount !== expectedCertified || runningCount !== expectedRunning || admittedCount !== expectedAdmitted) {
    fail(field);
  }
  const knownCopiedRows = nullableInteger(object.known_copied_rows, `${field}.known_copied_rows`);
  const knownTotalRows = nullableInteger(object.known_total_rows, `${field}.known_total_rows`);
  const copiedValues = fleet.tables.map((table) => table.copiedRows);
  const totalValues = fleet.tables.map((table) => table.totalRows);
  if (knownCopiedRows !== knownSum(copiedValues) || knownTotalRows !== knownSum(totalValues)) fail(field);
  const creditBudget = nonNegativeNumber(object.credit_budget, `${field}.credit_budget`);
  const consumedCredits = nonNegativeNumber(object.consumed_credits, `${field}.consumed_credits`);
  const reservedCredits = nonNegativeNumber(object.reserved_credits, `${field}.reserved_credits`);
  if (
    creditBudget !== fleet.creditBudget
    || consumedCredits !== fleet.consumedCredits
    || reservedCredits !== fleet.reservedCredits
  ) fail(field);
  const overBudget = booleanValue(object.over_budget, `${field}.over_budget`);
  if (overBudget !== (consumedCredits > creditBudget)) fail(`${field}.over_budget`);
  const nextAction = enumValue(object.next_action, fleetSafeActions, `${field}.next_action`);
  const nextReason = fleetToken(object.next_reason, `${field}.next_reason`);
  const nextTable = object.next_table === null ? null : text(object.next_table, `${field}.next_table`);
  if (nextTable !== null && !site().manifest.includes(nextTable as FleetTableName)) fail(`${field}.next_table`);
  return {
    certifiedCount,
    tableCount,
    runningCount,
    admittedCount,
    knownCopiedRows,
    knownTotalRows,
    creditBudget,
    consumedCredits,
    reservedCredits,
    overBudget,
    nextAction,
    nextReason,
    nextTable: nextTable as FleetTableName | null,
  };
}

function parseFleetCapabilities(
  value: unknown,
  field: string,
): Readonly<Record<CapabilityId, FleetCapability>> {
  const object = record(value, field);
  assertExactKeys(object, capabilityIds, field);
  const capabilities = {} as Record<CapabilityId, FleetCapability>;
  for (const id of capabilityIds) {
    const item = record(object[id], `${field}.${id}`);
    assertExactKeys(item, ['state', 'reason'], `${field}.${id}`);
    const state = enumValue(item.state, capabilityStates, `${field}.${id}.state`);
    const reason = item.reason === null ? null : text(item.reason, `${field}.${id}.reason`);
    if (state === 'unavailable' && reason === null) fail(`${field}.${id}.reason`);
    capabilities[id] = { state, reason };
  }
  return capabilities;
}

function knownSum(values: readonly (number | null)[]): number | null {
  const known = values.filter((value): value is number => value !== null);
  return known.length === 0 ? null : known.reduce((sum, value) => sum + value, 0);
}

function parseWindowDelivery(value: unknown, field: string, sourceKind: EvidenceKind): WindowDelivery | null {
  if (value === undefined || value === null) return null;
  try {
    const object=record(value,field);
    let chain: WindowChainProgress | undefined;
    if (object.chain !== undefined) {
      const item=record(object.chain,field);
      const declared=item.declared_windows, matched=item.matched_windows;
      if (typeof declared!=='number' || !Number.isInteger(declared) || declared<1 || declared>128
          || typeof matched!=='number' || !Number.isInteger(matched) || matched<0 || matched>declared
          || typeof item.capture_complete!=='boolean') fail(field);
      const state=enumValue(item.state,new Set(['incomplete','matched'] as const),field);
      const evidenceKind=enumValue(item.evidence_kind,evidenceKinds,field);
      if ((state==='matched') !== (matched===declared && item.capture_complete)
          || (state==='matched') !== (object.state==='matched')
          || (sourceKind==='simulation' && evidenceKind!=='simulation')
          || (sourceKind==='historical' && evidenceKind==='live')) fail(field);
      chain={declaredWindows:declared,matchedWindows:matched,captureComplete:item.capture_complete,state,evidenceKind};
    }
    const chainField=chain ? {chain} : {};
    if (object.state==='invalid' || object.state==='unavailable') return {state:object.state,...chainField};
    if (object.state!=='matched' && object.state!=='not_tested') fail(field);
    if (object.scope!=='closed_window_only') fail(field);
    const archiveRunId=text(object.archive_run_id,field);
    const windowId=text(object.window_id,field);
    if (![archiveRunId,windowId].every((id) => /^[a-z0-9][a-z0-9-]{0,79}$/.test(id))) fail(field);
    const startedAt=timestamp(object.started_at,field);
    const closedAt=timestamp(object.closed_at,field);
    const destinationObservedAt=timestamp(object.destination_observed_at,field);
    if (Date.parse(startedAt)>=Date.parse(closedAt) || Date.parse(closedAt)>Date.parse(destinationObservedAt)) fail(field);
    const eventCount=object.event_count;
    if (typeof eventCount!=='number' || !Number.isSafeInteger(eventCount) || eventCount<0
        || (object.state==='matched' ? eventCount===0 : eventCount!==0)) fail(field);
    const qualityObject=record(object.quality,field);
    const freshness=enumValue(qualityObject.freshness,freshnesses,field);
    const evidenceKind=enumValue(qualityObject.evidence_kind,evidenceKinds,field);
    if (chain && chain.evidenceKind!==evidenceKind) fail(field);
    if ((sourceKind==='simulation' && evidenceKind!=='simulation')
        || (sourceKind==='historical' && evidenceKind==='live')) fail(field);
    return {state:object.state,archiveRunId,windowId,startedAt,closedAt,destinationObservedAt,eventCount,
      scope:'closed_window_only',quality:{freshness,evidenceKind},...chainField};
  } catch (error) {
    if (!(error instanceof ControlPlaneParseError)) throw error;
    return {state:'invalid'};
  }
}

function nullableInteger(value: unknown, field: string): number | null {
  return nullableNumber(value, field, true);
}

export function unavailableObservability(evidenceKind: EvidenceKind): Observability {
  return {
    status: 'unavailable',
    quality: { coverage: 'none', freshness: 'unavailable', evidenceKind },
    observedAt: null,
    reason: 'observability_not_attached',
    checks: [],
    alerts: [],
  };
}

export function pipelineObservability(pipeline: Pipeline): Observability {
  return pipeline.observability ?? unavailableObservability(pipeline.quality.evidenceKind);
}

function parseObservability(
  value: unknown,
  field: string,
  pipelineEvidenceKind: EvidenceKind,
): Observability {
  if (value === undefined) return unavailableObservability(pipelineEvidenceKind);
  const object = record(value, field);
  const status = enumValue(object.status, sloStatuses, `${field}.status`);
  const qualityObject = record(object.quality, `${field}.quality`);
  const quality: Observability['quality'] = {
    coverage: enumValue(qualityObject.coverage, observabilityCoverages, `${field}.quality.coverage`),
    freshness: enumValue(qualityObject.freshness, observabilityFreshnesses, `${field}.quality.freshness`),
    evidenceKind: enumValue(qualityObject.evidence_kind, evidenceKinds, `${field}.quality.evidence_kind`),
  };
  if (quality.evidenceKind !== pipelineEvidenceKind) fail(`${field}.quality.evidence_kind`);

  const reason = sloToken(object.reason, `${field}.reason`);
  const checks = array(object.checks, `${field}.checks`).map((item, index) => parseSloCheck(item, `${field}.checks[${index}]`));
  const alerts = array(object.alerts, `${field}.alerts`).map((item, index) => parseSloAlert(item, `${field}.alerts[${index}]`));

  if (status === 'unavailable') {
    if (
      quality.coverage !== 'none'
      || quality.freshness !== 'unavailable'
      || object.observed_at !== null
      || reason !== 'observability_not_attached'
      || checks.length !== 0
      || alerts.length !== 0
    ) fail(field);
    return unavailableObservability(pipelineEvidenceKind);
  }

  const observedAt = timestamp(object.observed_at, `${field}.observed_at`);
  if (quality.coverage === 'none' || quality.freshness === 'unavailable') fail(`${field}.quality`);
  const expectedReason = status === 'pass' ? 'within_policy' : status === 'breach' ? 'threshold_breach' : 'measurement_gap';
  if (reason !== expectedReason || checks.length === 0) fail(`${field}.reason`);

  const checkIds = new Set<string>();
  for (const check of checks) {
    if (checkIds.has(check.id)) fail(`${field}.checks: duplicate id`);
    checkIds.add(check.id);
  }
  // Sur-ensemble toléré : un backend plus récent peut publier des checks
  // additionnels sans réduire la couverture des contrôles requis.
  const hasRequiredChecks = [...requiredSloCheckIds].every((id) => checkIds.has(id));
  const activeUnknownCheck = alerts.some((alert) => alert.lifecycleState === 'firing' && !checkIds.has(alert.checkId));
  if (
    (quality.coverage === 'complete' && (!hasRequiredChecks || activeUnknownCheck))
    || (quality.coverage === 'partial' && hasRequiredChecks && !activeUnknownCheck)
  ) fail(`${field}.quality.coverage`);

  const expectedStatus: SloSignalStatus = checks.some((check) => check.status === 'breach')
    ? 'breach'
    : checks.some((check) => check.status === 'unobserved')
      ? 'unobserved'
      : 'pass';
  if (status !== expectedStatus) fail(`${field}.status`);

  const alertIds = new Set<string>();
  const checksById = new Map(checks.map((check) => [check.id, check]));
  for (const alert of alerts) {
    if (alertIds.has(alert.checkId)) fail(`${field}.alerts: duplicate check id`);
    alertIds.add(alert.checkId);
    if (Date.parse(alert.lastObservedAt) > Date.parse(observedAt)) fail(`${field}.alerts.last_observed_at`);
    const check = checksById.get(alert.checkId);
    if (alert.lifecycleState === 'firing' && check && (
      alert.stage !== check.stage
      || alert.signalStatus !== check.status
      || alert.reason !== check.reason
      || alert.unit !== check.unit
      || !sloValueEqual(alert.observed, check.observed)
      || !sloValueEqual(alert.threshold, check.threshold)
    )) fail(`${field}.alerts`);
  }

  return { status, quality, observedAt, reason, checks, alerts };
}

function parseSloCheck(value: unknown, field: string): SloCheck {
  const object = record(value, field);
  return {
    id: sloToken(object.id, `${field}.id`),
    stage: sloToken(object.stage, `${field}.stage`),
    status: enumValue(object.status, sloSignalStatuses, `${field}.status`),
    observed: sloValue(object.observed, `${field}.observed`),
    threshold: sloValue(object.threshold, `${field}.threshold`),
    unit: sloUnit(object.unit, `${field}.unit`),
    reason: sloToken(object.reason, `${field}.reason`),
  };
}

function parseSloAlert(value: unknown, field: string): SloAlert {
  const object = record(value, field);
  const fingerprint = text(object.fingerprint, `${field}.fingerprint`);
  if (!publicSha256Pattern.test(fingerprint)) fail(`${field}.fingerprint`);
  const lifecycleState = enumValue(object.lifecycle_state, sloAlertLifecycles, `${field}.lifecycle_state`);
  const signalStatus = enumValue(object.signal_status, sloSignalStatuses, `${field}.signal_status`);
  const severity = enumValue(object.severity, sloAlertSeverities, `${field}.severity`);
  const firstFiredAt = timestamp(object.first_fired_at, `${field}.first_fired_at`);
  const firingSince = timestamp(object.firing_since, `${field}.firing_since`);
  const lastObservedAt = timestamp(object.last_observed_at, `${field}.last_observed_at`);
  const resolvedAt = nullableTimestamp(object.resolved_at, `${field}.resolved_at`);
  const occurrenceCount = positiveInteger(object.occurrence_count, `${field}.occurrence_count`);
  const evaluationCount = positiveInteger(object.evaluation_count, `${field}.evaluation_count`);
  if (
    occurrenceCount > evaluationCount
    || Date.parse(firstFiredAt) > Date.parse(firingSince)
    || Date.parse(firingSince) > Date.parse(lastObservedAt)
  ) fail(field);
  if (lifecycleState === 'firing') {
    const expectedSeverity = signalStatus === 'breach' ? 'critical' : signalStatus === 'unobserved' ? 'warning' : 'none';
    if (signalStatus === 'pass' || severity !== expectedSeverity || resolvedAt !== null) fail(field);
  } else if (signalStatus !== 'pass' || severity !== 'none' || resolvedAt !== lastObservedAt) {
    fail(field);
  }
  return {
    fingerprint,
    checkId: sloToken(object.check_id, `${field}.check_id`),
    stage: sloToken(object.stage, `${field}.stage`),
    lifecycleState,
    signalStatus,
    severity,
    reason: sloToken(object.reason, `${field}.reason`),
    observed: sloValue(object.observed, `${field}.observed`),
    threshold: sloValue(object.threshold, `${field}.threshold`),
    unit: sloUnit(object.unit, `${field}.unit`),
    firstFiredAt,
    firingSince,
    lastObservedAt,
    resolvedAt,
    occurrenceCount,
    evaluationCount,
  };
}

function sloToken(value: unknown, field: string): string {
  const result = text(value, field);
  if (!publicSloTokenPattern.test(result)) fail(field);
  return result;
}

function sloUnit(value: unknown, field: string): string | null {
  if (value === null) return null;
  const result = text(value, field);
  if (!publicSloUnitPattern.test(result)) fail(field);
  return result;
}

function sloValue(value: unknown, field: string): SloValue {
  if (value === null) return null;
  if (typeof value === 'number') return nonNegativeNumber(value, field);
  if (typeof value === 'string') {
    if (!publicSloValueTokenPattern.test(value)) fail(field);
    return value;
  }
  if (Array.isArray(value) && value.length <= 16) {
    return value.map((item, index) => {
      if (typeof item !== 'string' || !publicSloValueTokenPattern.test(item)) fail(`${field}[${index}]`);
      return item;
    });
  }
  return fail(field);
}

function sloValueEqual(left: SloValue, right: SloValue): boolean {
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((item, index) => item === right[index]);
  }
  return left === right;
}

function parseLagSeries(value: unknown, field: string): {
  readonly points: readonly LagPoint[];
  readonly resolutionSeconds: number | null;
  readonly sampleCount: number;
  readonly unknownSampleCount: number;
} {
  if (value === null || value === undefined) {
    return { points: [], resolutionSeconds: null, sampleCount: 0, unknownSampleCount: 0 };
  }
  const object = record(value, field);
  const resolutionSeconds = positiveNumber(object.resolution_s, `${field}.resolution_s`);
  const sampleCount = nonNegativeInteger(object.sample_count, `${field}.sample_count`);
  const unknownSampleCount = nonNegativeInteger(object.unknown_sample_count, `${field}.unknown_sample_count`);
  if (unknownSampleCount > sampleCount) fail(`${field}.unknown_sample_count`);
  let previousEnd: number | null = null;
  let previousObservedStart: number | null = null;
  let pendingTemporalGapEnd: number | null = null;
  const points: LagPoint[] = [];
  array(object.buckets, `${field}.buckets`).forEach((value, index) => {
    const bucketField = `${field}.buckets[${index}]`;
    const bucket = record(value, bucketField);
    const startSeconds = nonNegativeNumber(bucket.start_s, `${bucketField}.start_s`);
    const endSeconds = nonNegativeNumber(bucket.end_s, `${bucketField}.end_s`);
    const kind = bucket.kind === undefined ? 'observed' : bucket.kind;
    if (kind !== 'observed' && kind !== 'temporal_gap') fail(`${bucketField}.kind`);
    const samples = kind === 'observed'
      ? positiveInteger(bucket.samples, `${bucketField}.samples`)
      : nonNegativeInteger(bucket.samples, `${bucketField}.samples`);
    const unknownSamples = nonNegativeInteger(bucket.unknown_samples, `${bucketField}.unknown_samples`);
    if (endSeconds < startSeconds || unknownSamples > samples || (previousEnd !== null && startSeconds < previousEnd)) {
      fail(bucketField);
    }
    const coverage = bucket.coverage;
    if (coverage !== 'complete' && coverage !== 'gap') fail(`${bucketField}.coverage`);
    if (
      (kind === 'observed' && coverage === 'complete' && unknownSamples !== 0)
      || (kind === 'observed' && coverage === 'gap' && unknownSamples === 0)
      || (kind === 'temporal_gap' && (coverage !== 'gap' || samples !== 0 || unknownSamples !== 0))
    ) {
      fail(`${bucketField}.coverage`);
    }
    const low = nullableNumber(bucket.min, `${bucketField}.min`, true);
    const high = nullableNumber(bucket.max, `${bucketField}.max`, true);
    const lag = nullableNumber(bucket.last, `${bucketField}.last`, true);
    const knownSamples = samples - unknownSamples;
    if (kind === 'temporal_gap') {
      // Le trou déclaré pave exactement l'intervalle non couvert : il commence
      // où le relevé précédent s'est arrêté (un seau fusionné peut déborder sa
      // cellule nominale) et finit où la couverture reprend.
      const tolerance = Number.EPSILON * Math.max(1, Math.abs(previousEnd ?? 0), Math.abs(startSeconds));
      if (
        low !== null
        || high !== null
        || lag !== null
        || previousEnd === null
        || Math.abs(startSeconds - previousEnd) > tolerance
        || endSeconds <= startSeconds
        || pendingTemporalGapEnd !== null
      ) fail(bucketField);
      pendingTemporalGapEnd = endSeconds;
    } else if (knownSamples === 0) {
      if (low !== null || high !== null || lag !== null) fail(bucketField);
    } else if (low === null || high === null || low > high) {
      fail(bucketField);
    }
    if (kind === 'observed' && coverage === 'complete' && (lag === null || low === null || high === null || lag < low || lag > high)) {
      fail(bucketField);
    }
    if (kind === 'observed' && coverage === 'gap' && lag !== null) fail(`${bucketField}.last`);

    if (kind === 'observed' && previousObservedStart !== null && previousEnd !== null) {
      const expectedStart = previousObservedStart + resolutionSeconds;
      const tolerance = Number.EPSILON * Math.max(1, Math.abs(expectedStart), Math.abs(startSeconds));
      if (pendingTemporalGapEnd !== null) {
        if (Math.abs(pendingTemporalGapEnd - startSeconds) > tolerance) fail(bucketField);
        pendingTemporalGapEnd = null;
      } else if (startSeconds > expectedStart + tolerance && startSeconds > previousEnd + tolerance) {
        points.push({
          startSeconds: previousEnd,
          endSeconds: startSeconds,
          low: null,
          high: null,
          lag: null,
          samples: 0,
          unknownSamples: 0,
          coverage: 'gap',
          kind: 'temporal_gap',
        });
      }
    }
    previousEnd = endSeconds;
    if (kind === 'observed') previousObservedStart = startSeconds;
    points.push({ startSeconds, endSeconds, low, high, lag, samples, unknownSamples, coverage, kind });
  });
  if (pendingTemporalGapEnd !== null) fail(field);
  if (
    points.reduce((sum, point) => sum + point.samples, 0) !== sampleCount
    || points.reduce((sum, point) => sum + point.unknownSamples, 0) !== unknownSampleCount
  ) fail(field);
  return { points, resolutionSeconds, sampleCount, unknownSampleCount };
}

function parseQuality(value: unknown, field: string): ObservationQuality {
  const object = record(value, field);
  return {
    coverage: enumValue(object.coverage, coverages, `${field}.coverage`),
    freshness: enumValue(object.freshness, freshnesses, `${field}.freshness`),
    evidenceKind: enumValue(object.evidence_kind, evidenceKinds, `${field}.evidence_kind`),
  };
}

function parseStage(value: unknown, field: string): Stage {
  const object = record(value, field);
  return {
    id: enumValue(object.id, stageIds, `${field}.id`),
    status: enumValue(object.status, stageStatuses, `${field}.status`),
    observedAt: nullableTimestamp(object.observed_at, `${field}.observed_at`),
    headline: text(object.headline, `${field}.headline`),
    detail: text(object.detail, `${field}.detail`),
  };
}

function parseSource(value: unknown, field: string): Source {
  const object = record(value, field);
  const status = object.status;
  if (status !== 'available' && status !== 'unavailable') fail(`${field}.status`);
  return {
    id: text(object.id, `${field}.id`),
    evidenceKind: enumValue(object.evidence_kind, evidenceKinds, `${field}.evidence_kind`),
    environment: environment(object.environment, `${field}.environment`),
    status,
    error: nullableText(object.error, `${field}.error`),
  };
}

function nullableIncident(value: unknown, field: string): Incident | null {
  if (value === null) return null;
  const object = record(value, field);
  const resolved =
    object.cause_resolved === true
      ? {
          causeResolved: true as const,
          causeResolvedObservedAt:
            typeof object.cause_resolved_observed_at === 'string' &&
            utcTimestampPattern.test(object.cause_resolved_observed_at)
              ? object.cause_resolved_observed_at
              : null,
        }
      : {};
  const declaredAt =
    typeof object.declared_at === 'string' && utcTimestampPattern.test(object.declared_at)
      ? { declaredAt: object.declared_at }
      : {};
  return {
    code: enumValue(object.code, incidentCodes, `${field}.code`),
    type: enumValue(object.type, incidentTypes, `${field}.type`),
    ...resolved,
    ...declaredAt,
  };
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) fail(field);
  return Object.fromEntries(Object.entries(value));
}

function array(value: unknown, field: string): readonly unknown[] {
  if (!Array.isArray(value)) fail(field);
  return value;
}

function text(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) fail(field);
  return value;
}

function environment(value: unknown, field: string): string {
  const result = text(value, field);
  const canonical = result.trim().toLowerCase();
  if (result !== canonical) fail(field);
  return canonical;
}

function nullableText(value: unknown, field: string): string | null {
  return value === null ? null : text(value, field);
}

function timestamp(value: unknown, field: string): string {
  const result = text(value, field);
  const match = utcTimestampPattern.exec(result);
  if (!match) fail(field);
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const milliseconds = Number(`${match[7] ?? '0'}000`.slice(0, 3));
  const parsed = new Date(0);
  parsed.setUTCFullYear(year, month - 1, day);
  parsed.setUTCHours(hour, minute, second, milliseconds);
  if (Number.isNaN(parsed.getTime())) fail(field);
  if (
    parsed.getUTCFullYear() !== year
    || parsed.getUTCMonth() !== month - 1
    || parsed.getUTCDate() !== day
    || parsed.getUTCHours() !== hour
    || parsed.getUTCMinutes() !== minute
    || parsed.getUTCSeconds() !== second
    || parsed.getUTCMilliseconds() !== milliseconds
  ) fail(field);
  return result;
}

function nullableTimestamp(value: unknown, field: string): string | null {
  return value === null ? null : timestamp(value, field);
}

function nonNegativeInteger(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) fail(field);
  return value;
}

function positiveInteger(value: unknown, field: string): number {
  const result = nonNegativeInteger(value, field);
  if (result === 0) fail(field);
  return result;
}

function nonNegativeNumber(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) fail(field);
  return value;
}

function positiveNumber(value: unknown, field: string): number {
  const result = nonNegativeNumber(value, field);
  if (result === 0) fail(field);
  return result;
}

function nullableNumber(value: unknown, field: string, integer = false): number | null {
  if (value === null) return null;
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || (integer && !Number.isSafeInteger(value))) fail(field);
  return value;
}

function numberRecord(value: unknown, field: string): Readonly<Record<string, number | null>> {
  const object = record(value, field);
  const result: Record<string, number | null> = {};
  for (const [key, item] of Object.entries(object)) {
    if (!key) fail(field);
    result[key] = nullableNumber(item, `${field}.${key}`);
  }
  return result;
}

function enumValue<T extends string>(value: unknown, allowed: ReadonlySet<T>, field: string): T {
  if (typeof value !== 'string') fail(field);
  return [...allowed].find((item) => item === value) ?? fail(field);
}

function assertExactKeys(value: Record<string, unknown>, keys: readonly string[], field: string): void {
  const expected = new Set(keys);
  const actual = Object.keys(value);
  if (actual.length !== expected.size || actual.some((key) => !expected.has(key))) fail(field);
}

function booleanValue(value: unknown, field: string): boolean {
  if (typeof value !== 'boolean') fail(field);
  return value;
}

function nullableBoolean(value: unknown, field: string): boolean | null {
  if (value === null) return null;
  return booleanValue(value, field);
}

function fleetToken(value: unknown, field: string): string {
  const result = text(value, field);
  if (!fleetTokenPattern.test(result)) fail(field);
  return result;
}

function fleetHash(value: unknown, field: string): string {
  const result = text(value, field);
  if (!publicSha256Pattern.test(result)) fail(field);
  return result;
}

function alignedUtc(value: unknown, field: string): string {
  const result = timestamp(value, field);
  const match = utcTimestampPattern.exec(result);
  if (!match || Number(`${match[7] ?? '0'}000`.slice(0, 3)) !== 0) fail(field);
  return result;
}

function fail(field: string): never {
  throw new ControlPlaneParseError(field);
}

/** Identité du site publiée par le service ; l'absence est un refus, pas un
 *  défaut — aucun document ne se valide contre une valeur implicite. */
function site(): SiteIdentity {
  const identity = installedSiteIdentity();
  if (!identity) fail('site_identity');
  return identity;
}

/* ------------------------------------------------------------------------
 * Extension S5 — lecture tolérante du pass-through projeté.
 *
 * Ces blocs sont additifs : un contenu malformé retombe à null plutôt que de
 * rejeter un pipeline par ailleurs valide (le serveur applique la même
 * politique — `destination_invalid` devient null + reason). `undefined`
 * distingue « extension non servie » de « bloc servi null ».
 * --------------------------------------------------------------------- */

function passThroughText(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'string' || !value.trim()) return null;
  return value;
}

function passThroughBlock<T>(
  value: unknown,
  parse: (object: Record<string, unknown>) => T,
): T | null | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  try {
    return parse(value as Record<string, unknown>);
  } catch (error) {
    if (error instanceof ControlPlaneParseError) return null;
    throw error;
  }
}

function passThroughMemberText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function passThroughMemberInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function passThroughMemberDuration(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function passThroughMemberTimestamp(value: unknown): string | null {
  return typeof value === 'string' && utcTimestampPattern.test(value) ? value : null;
}

function passThroughMemberCheckpoint(value: unknown): JournalCheckpoint | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const object = value as Record<string, unknown>;
  const receiver = passThroughMemberText(object.receiver);
  const sequence = passThroughMemberInteger(object.sequence);
  if (receiver === null || sequence === null) return null;
  return { receiver, sequence };
}

function passThroughMemberObjects(value: unknown): readonly string[] | null {
  if (!Array.isArray(value)) return null;
  const items = value.map((item) => passThroughMemberText(item));
  return items.every((item): item is string => item !== null) ? items : null;
}

function parseJournalPosition(object: Record<string, unknown>): JournalPosition {
  return {
    checkpoint: passThroughMemberCheckpoint(object.checkpoint),
    sourceTail: passThroughMemberCheckpoint(object.source_tail),
    receiverFirstSequence: passThroughMemberInteger(object.receiver_first_sequence),
    receiverLastSequence: passThroughMemberInteger(object.receiver_last_sequence),
  };
}

function parseFluxIdentity(object: Record<string, unknown>): FluxIdentity {
  return {
    id: passThroughMemberText(object.id),
    label: passThroughMemberText(object.label),
    journal: passThroughMemberText(object.journal),
    journalLibrary: passThroughMemberText(object.journal_library),
    objects: passThroughMemberObjects(object.objects),
    readerPath: passThroughMemberText(object.reader_path),
    target: passThroughMemberText(object.target),
    job: passThroughMemberText(object.job),
  };
}

function parseRunDiagnostic(value: unknown): RunDiagnostic | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const object = value as Record<string, unknown>;
  return {
    type: passThroughMemberText(object.type),
    head: passThroughMemberText(object.head),
    at: passThroughMemberTimestamp(object.at),
  };
}

function parseSourcePause(value: unknown): SourcePause | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const object = value as Record<string, unknown>;
  return {
    retryAfter: passThroughMemberTimestamp(object.retry_after),
    reasonCode: passThroughMemberText(object.reason_code),
  };
}

function parseRunProjection(object: Record<string, unknown>): RunProjection {
  return {
    state: passThroughMemberText(object.state),
    startedAt: passThroughMemberTimestamp(object.started_at),
    elapsedSeconds: passThroughMemberDuration(object.elapsed_s),
    stoppedBecause: passThroughMemberText(object.stopped_because),
    diagnostic: parseRunDiagnostic(object.diagnostic),
    sourcePause: parseSourcePause(object.source_pause),
  };
}

function parseResumeReadiness(object: Record<string, unknown>): ResumeReadiness {
  return {
    state:
      object.state === 'ready' ? 'ready'
      : object.state === 'blocked' ? 'blocked'
      : 'unavailable',
    authObservedAt: passThroughMemberTimestamp(object.auth_observed_at),
    checkpoint: passThroughMemberCheckpoint(object.checkpoint),
    tail: passThroughMemberCheckpoint(object.tail),
    backlogSequences: passThroughMemberInteger(object.backlog_sequences),
    backlogReceivers: passThroughMemberInteger(object.backlog_receivers),
  };
}

function parseDestinationProjection(object: Record<string, unknown>): DestinationProjection {
  return {
    kind: passThroughMemberText(object.kind),
    database: passThroughMemberText(object.database),
    schema: passThroughMemberText(object.schema),
    stage: passThroughMemberText(object.stage),
    rawTable: passThroughMemberText(object.raw_table),
    canonicalTable: passThroughMemberText(object.canonical_table),
    runTag: passThroughMemberText(object.run_tag),
    observedAt: passThroughMemberTimestamp(object.observed_at),
    loadCheckpoint: passThroughMemberCheckpoint(object.load_checkpoint),
    applyCheckpoint: passThroughMemberCheckpoint(object.apply_checkpoint),
    sourceEvents: passThroughMemberInteger(object.source_events),
    rawRows: passThroughMemberInteger(object.raw_rows),
    canonicalRows: passThroughMemberInteger(object.canonical_rows),
    duplicates: passThroughMemberInteger(object.duplicates),
  };
}

function parseCosts(value: unknown, field: string): WarehouseCosts | null {
  if (value === null || value === undefined) return null;
  const object = record(value, field);
  assertExactKeys(object, ['scope', 'warehouse', 'price_per_credit', 'currency', 'amount'], field);
  if (object.scope !== 'warehouse') fail(`${field}.scope`);
  const warehouse = text(object.warehouse, `${field}.warehouse`);
  if (!/^[A-Za-z_][A-Za-z0-9_$]{0,62}$/.test(warehouse)) fail(`${field}.warehouse`);
  const numeric = (input: unknown, key: string): string | null => {
    if (input === null) return null;
    if (typeof input !== 'string' || !/^(?:0|[1-9][0-9]*)(?:[.][0-9]+)?$/.test(input)
      || !Number.isFinite(Number(input))) fail(`${field}.${key}`);
    return input as string;
  };
  const pricePerCredit = numeric(object.price_per_credit, 'price_per_credit');
  const amount = numeric(object.amount, 'amount');
  const currency = object.currency === null ? null : text(object.currency, `${field}.currency`);
  if ((currency === null) !== (pricePerCredit === null) || (currency !== null && !/^[A-Z]{3}$/.test(currency))
      || (amount !== null && pricePerCredit === null)) fail(field);
  return { scope: 'warehouse', warehouse, pricePerCredit, currency, amount };
}

export interface InfrastructureCosts {
  readonly collectedAt: string;
  readonly namespace: string;
  readonly storage: { readonly observedAt: string; readonly bytes: number; readonly pricePerGibMonth: string; readonly monthlyRunRate: string } | null;
  readonly cluster: { readonly start: string; readonly end: string; readonly allocatedAmount: string; readonly clusterAmount: string; readonly idleAmount: string | null; readonly currency: string } | null;
}

function parseInfrastructureCosts(value: unknown, field: string): InfrastructureCosts | null {
  if (value === undefined || value === null) return null;
  const object = record(value, field);
  if (object.status === 'unavailable') return null;
  if (object.status !== 'available') fail(`${field}.status`);
  const numeric = (v: unknown, name: string): string => {
    if (typeof v !== 'string' || v.length > 96 || !/^[0-9]+(?:[.][0-9]+)?$/.test(v)
      || !Number.isFinite(Number(v)) || Number(v) > 1e18) fail(`${field}.${name}`);
    return v as string;
  };
  const storage = record(object.storage, `${field}.storage`);
  const cluster = record(object.cluster, `${field}.cluster`);
  if (!['measured','unavailable'].includes(String(storage.status)) || !['measured','unavailable'].includes(String(cluster.status))) fail(field);
  if (storage.status === 'measured' && (storage.currency !== 'USD' || storage.basis !== 'aws_public_standard_first_tier')) fail(field);
  if (cluster.status === 'measured' && (cluster.basis !== 'opencost_no_idle_share' || typeof cluster.currency !== 'string' || !/^[A-Z]{3}$/.test(cluster.currency))) fail(field);
  const collectedAt = timestamp(object.collected_at, `${field}.collected_at`);
  if (storage.status === 'measured' && Date.parse(timestamp(storage.observed_at, field)) > Date.parse(collectedAt)) fail(field);
  if (cluster.status === 'measured') {
    const start = Date.parse(timestamp(cluster.start, field));
    const end = Date.parse(timestamp(cluster.end, field));
    if (end - start !== 24 * 3600_000 || end > Date.parse(collectedAt)) fail(field);
  }
  return {
    collectedAt,
    namespace: text(object.namespace, `${field}.namespace`),
    storage: storage.status === 'measured' ? {
      observedAt: timestamp(storage.observed_at, `${field}.storage.observed_at`),
      bytes: Number(numeric(storage.bytes,'bytes')),pricePerGibMonth:numeric(storage.price_per_gib_month,'price_per_gib_month'),
      monthlyRunRate:numeric(storage.monthly_run_rate,'monthly_run_rate'),
    } : null,
    cluster: cluster.status === 'measured' ? {
      start:timestamp(cluster.start,`${field}.cluster.start`),end:timestamp(cluster.end,`${field}.cluster.end`),
      allocatedAmount:numeric(cluster.allocated_amount,'allocated_amount'),clusterAmount:numeric(cluster.cluster_amount,'cluster_amount'),
      idleAmount:cluster.idle_amount === null ? null : numeric(cluster.idle_amount,'idle_amount'),currency:cluster.currency as string,
    } : null,
  };
}
