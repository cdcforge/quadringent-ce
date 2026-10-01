import { exact, sequences } from './format.ts';
import { siteIdentity, type SiteIdentity } from './siteIdentity.ts';
import type {
  CapabilityId,
  Coverage,
  EvidenceKind,
  Freshness,
  Fleet,
  FleetPlan,
  FleetPlanTable,
  FleetRuntime,
  FleetRuntimePhase,
  FleetRuntimeTablePhase,
  FleetPhase,
  FleetSafeAction,
  FleetTable,
  ReconciliationProof,
} from './controlPlane.ts';

export const UNMEASURED = 'Non mesuré';

/**
 * Copy intended for the landing view.  The catalogue, execution state and
 * technical receipt remain separate concerns: the public view must not turn a
 * catalogue row count into a claim that rows reached Snowflake.
 */
export type PublicStateTone = 'neutral' | 'positive' | 'attention' | 'blocked';

export interface PublicStateCopy {
  readonly label: string;
  readonly tone: PublicStateTone;
}

/**
 * Jetons de confirmation opérateur, dérivés de l'identité déclarée du site
 * — la même convention que le control plane : « ACTION SITE ENVIRONNEMENT ».
 * Aucune valeur n'est figée dans l'interface.
 */
export function fleetActionConfirmations(
  site: SiteIdentity = siteIdentity(),
): Readonly<Record<CapabilityId, string | null>> {
  const declared = `${site.siteId.toUpperCase()} ${site.environment}`;
  return {
    refresh: null,
    prepare: `PREPARE ${declared}`,
    start: `START ${declared}`,
    pause: `PAUSE ${declared}`,
    resume: `RESUME ${declared}`,
  };
}

/**
 * Corps exact accepté par le serveur d'actions : {fleet_id, environment,
 * confirmation}. Aucune clé supplémentaire n'est jamais envoyée — le repli
 * historique « dataset » a été retiré du contrat.
 */
export function fleetActionRequest(
  site: SiteIdentity,
  action: CapabilityId,
): { readonly fleet_id: string; readonly environment: string; readonly confirmation: string | null } {
  return {
    fleet_id: site.fleetId,
    environment: site.runtimeEnvironment,
    confirmation: fleetActionConfirmations(site)[action],
  };
}

const phaseLabels: Readonly<Record<FleetPhase, string>> = {
  NOT_PREPARED: 'À préparer',
  READY: 'Prête',
  HISTORICAL: 'Historique',
  CATCHING_UP: 'Rattrapage',
  LIVE: 'Temps réel',
  RECONCILING: 'Réconciliation',
  CERTIFIED: 'Certifiée',
  PAUSED: 'Suspendue',
  BLOCKED: 'Bloquée',
};

const phaseScopes: Readonly<Record<FleetPhase, 'partial' | 'complete' | 'gap'>> = {
  NOT_PREPARED: 'partial',
  READY: 'partial',
  HISTORICAL: 'partial',
  CATCHING_UP: 'partial',
  LIVE: 'partial',
  RECONCILING: 'partial',
  CERTIFIED: 'complete',
  PAUSED: 'partial',
  BLOCKED: 'gap',
};

const nextActionCopy: Readonly<Record<FleetSafeAction, string>> = {
  PREPARE: 'Préparer les tables',
  ADMIT_HISTORICAL: 'Lancer l’historique',
  RECORD_HISTORY_PROGRESS: 'Continuer l’historique',
  PROVE_CONTINUITY: 'Vérifier la continuité du journal',
  CATCH_UP_TO_TAIL: 'Rattraper le journal',
  RECORD_ACTUAL_COST: 'Enregistrer le coût réel',
  OPEN_RECONCILIATION: 'Ouvrir la réconciliation',
  CERTIFY: 'Certifier la table',
  RESUME: 'Reprendre',
  INSPECT_BLOCKED: 'Examiner le blocage',
  NONE: 'Aucune action sûre',
};

const utcFormat = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
});

const dayFormat = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

export function formatUtc(value: string): string {
  return `${utcFormat.format(new Date(value))} UTC`;
}

export function fleetObservationCopy(evidenceKind: EvidenceKind, observedAt: string): string {
  if (evidenceKind === 'live') return 'Observation en cours';
  if (evidenceKind === 'simulation') return 'Observation simulée';
  return `Observation historique · ${dayFormat.format(new Date(observedAt))}`;
}

export function phaseLabel(phase: FleetPhase): string {
  return phaseLabels[phase];
}

export function phaseScope(phase: FleetPhase): 'partial' | 'complete' | 'gap' {
  return phaseScopes[phase];
}

/** The short, non-technical state shown in the main table. */
export function publicPhaseLabel(phase: FleetPhase): string {
  switch (phase) {
    case 'NOT_PREPARED': return 'À préparer';
    case 'READY': return 'Prête';
    case 'HISTORICAL':
    case 'CATCHING_UP':
    case 'RECONCILING': return 'En cours';
    case 'LIVE':
    case 'CERTIFIED': return 'Disponible';
    case 'PAUSED': return 'Suspendue';
    case 'BLOCKED': return 'Bloquée';
  }
}

export function runtimePhaseLabel(phase: FleetRuntimePhase | FleetRuntimeTablePhase): string {
  switch (phase) {
    case 'NOT_PREPARED': return 'À préparer';
    case 'READY': return 'Prête';
    case 'PREPARED': return 'Prête à démarrer';
    case 'HISTORICAL': return 'Copie initiale en cours';
    case 'CATCHING_UP': return 'Rattrapage en cours';
    case 'LIVE': return 'En temps réel';
    case 'RECONCILING': return 'Vérification en cours';
    case 'CERTIFIED': return 'Certifiée';
    case 'PAUSED': return 'Suspendue';
    case 'BLOCKED': return 'Bloquée';
    case 'UNKNOWN': return 'À confirmer';
  }
}

/** Runtime status for the updates column. A plan-only row has not started. */
export function runtimeUpdatesCopy(phase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined): string {
  switch (phase) {
    case 'HISTORICAL': return 'À confirmer';
    case 'CATCHING_UP':
    case 'LIVE':
    case 'RECONCILING':
    case 'CERTIFIED': return 'Observées';
    case 'BLOCKED': return 'Bloquées';
    case 'UNKNOWN': return 'À confirmer';
    case 'NOT_PREPARED':
    case 'READY':
    case 'PREPARED':
    case 'PAUSED':
    case undefined:
    case null: return 'Pas encore démarrées';
  }
}

function rowProgressCopy(copiedRows: number | null, totalRows: number): string {
  if (copiedRows === null) return 'En cours';
  return `${sequences(copiedRows)} / ${sequences(totalRows)} lignes`;
}

/** Source/catalogue volume, explicitly not a destination volume. */
export function fleetPlanDataCopy(table: FleetPlanTable): string {
  return `${sequences(table.rowCount)} lignes source`;
}

export function fleetTableDataCopy(table: FleetTable): string {
  return table.totalRows === null ? 'Volume non mesuré' : `${sequences(table.totalRows)} lignes source`;
}

export function fleetPlanInitialCopy(
  table: FleetPlanTable,
  runtimePhase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined = null,
): string {
  // Admission in the catalogue is only a planning decision. Without an
  // observed runtime phase it must not look like preparation or execution has
  // already happened.
  if (runtimePhase === null || runtimePhase === undefined) return 'À préparer';
  if (runtimePhase === 'BLOCKED') return 'Bloquée';
  if (runtimePhase === 'UNKNOWN') return 'À confirmer';
  // Le domaine n'admet CATCHING_UP qu'une fois la copie mesurée complète
  // (copied == total) : dès cette phase, la copie initiale est terminée.
  if (
    runtimePhase === 'CATCHING_UP'
    || runtimePhase === 'LIVE'
    || runtimePhase === 'RECONCILING'
    || runtimePhase === 'CERTIFIED'
  ) return 'Terminée';
  if (runtimePhase === 'HISTORICAL') return rowProgressCopy(table.copiedRows, table.rowCount);
  if (runtimePhase === 'PAUSED') return 'Suspendue';
  if (runtimePhase === 'READY' || runtimePhase === 'PREPARED') return 'Prête à démarrer';
  if (runtimePhase === 'NOT_PREPARED') return 'À préparer';
  return 'À confirmer';
}

export function fleetPlanUpdatesCopy(
  _table: FleetPlanTable,
  runtimePhase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined = null,
): string {
  return runtimeUpdatesCopy(runtimePhase);
}

export function fleetPlanTableStateCopy(
  _table: FleetPlanTable,
  runtimePhase: FleetRuntimePhase | FleetRuntimeTablePhase | null | undefined = null,
): string {
  if (runtimePhase !== null && runtimePhase !== undefined) return runtimePhaseLabel(runtimePhase);
  return 'À préparer';
}

export function fleetInitialCopy(table: FleetTable, observationCurrent = true): string {
  if (!observationCurrent) return 'À confirmer';
  switch (table.phase) {
    case 'NOT_PREPARED': return 'Pas encore démarrée';
    case 'READY': return 'Prête à démarrer';
    case 'HISTORICAL':
    case 'CATCHING_UP':
    case 'RECONCILING':
      return table.totalRows !== null ? rowProgressCopy(table.copiedRows, table.totalRows) : 'En cours';
    case 'LIVE':
    case 'CERTIFIED':
      // A runtime phase alone does not prove that the initial copy reached
      // its source total. In particular, LIVE may be emitted before the
      // copied-row counters are available.
      if (table.copiedRows === null || table.totalRows === null) return 'À confirmer';
      return table.copiedRows === table.totalRows
        ? 'Terminée'
        : rowProgressCopy(table.copiedRows, table.totalRows);
    case 'PAUSED': return 'Suspendue';
    case 'BLOCKED': return 'Bloquée';
  }
}

export function fleetUpdatesCopy(table: FleetTable, observationCurrent = true): string {
  if (!observationCurrent) return 'À confirmer';
  if (table.phase === 'NOT_PREPARED' || table.phase === 'READY') return 'Pas encore démarrées';
  if (table.phase === 'BLOCKED') return 'Bloquées';
  if (table.phase === 'PAUSED' && table.currentCheckpoint === null) return 'Suspendues';
  if (table.phase === 'HISTORICAL') return 'À confirmer';
  if (table.currentCheckpoint !== null || table.journalTail !== null || table.continuityProven === true) {
    return 'Observées';
  }
  return 'À confirmer';
}

export function fleetTableStateCopy(table: FleetTable): string {
  return publicPhaseLabel(table.phase);
}

export function fleetStateCopy(
  fleet: Fleet,
  evidenceKind: EvidenceKind = 'live',
  cached = false,
  quality: { readonly coverage?: Coverage; readonly freshness?: Freshness } = {},
): PublicStateCopy {
  if (
    cached
    || evidenceKind !== 'live'
    || (quality.coverage !== undefined && quality.coverage !== 'complete')
    || (quality.freshness !== undefined && quality.freshness !== 'fresh')
  ) return { label: 'À confirmer', tone: 'attention' };
  if (fleet.tables.some((table) => table.phase === 'BLOCKED') || fleet.summary.overBudget) {
    return { label: 'Bloquée', tone: 'blocked' };
  }
  if (fleet.summary.tableCount > 0 && fleet.summary.certifiedCount === fleet.summary.tableCount) {
    return { label: 'Disponible', tone: 'positive' };
  }
  if (fleet.summary.runningCount > 0 || fleet.summary.admittedCount > 0) {
    return { label: 'En cours', tone: 'attention' };
  }
  if (fleet.summary.nextAction === 'PREPARE') return { label: 'À préparer', tone: 'attention' };
  return { label: 'À confirmer', tone: 'attention' };
}

export function fleetPlanStateCopy(
  plan: FleetPlan,
  runtime: FleetRuntime | null,
  cached = false,
): PublicStateCopy {
  if (cached) return { label: 'À confirmer', tone: 'attention' };
  // Le runtime est projeté en direct par l'exécuteur : sa phase est une
  // mesure actuelle indépendante de l'âge du relevé catalogue, qui ne
  // qualifie que les données déclaratives (volumes, découpage).
  if (runtime) {
    switch (runtime.phase) {
      case 'BLOCKED': return { label: 'Bloquée', tone: 'blocked' };
      case 'UNKNOWN': return { label: 'À confirmer', tone: 'attention' };
      case 'NOT_PREPARED': return { label: 'À préparer', tone: 'attention' };
      case 'PREPARED': return { label: 'Prête à démarrer', tone: 'attention' };
      case 'HISTORICAL':
      case 'CATCHING_UP':
      case 'RECONCILING': return { label: 'En cours', tone: 'attention' };
      case 'LIVE':
      case 'CERTIFIED': return { label: 'Disponible', tone: 'positive' };
      case 'PAUSED': return { label: 'Suspendue', tone: 'neutral' };
    }
  }
  if (plan.freshness !== 'fresh') return { label: 'À confirmer', tone: 'attention' };
  return { label: 'Non établi', tone: 'attention' };
}

export function evidenceKindCopy(kind: EvidenceKind): string {
  switch (kind) {
    case 'live': return 'En direct';
    case 'historical': return 'Relevé historique';
    case 'simulation': return 'Simulation';
  }
}

export function certifiedCopy(fleet: Fleet): string {
  return `${sequences(fleet.summary.certifiedCount)} / ${sequences(fleet.summary.tableCount)}`;
}

export function fleetVolumeCopy(fleet: Fleet): string {
  return fleet.summary.knownTotalRows === null
    ? 'Volume source non mesuré'
    : `${sequences(fleet.summary.knownTotalRows)} lignes source identifiées`;
}

export function historyCopy(table: FleetTable): string {
  if (table.copiedRows === null && table.totalRows === null) return UNMEASURED;
  if (table.copiedRows !== null && table.totalRows !== null) {
    return `${sequences(table.copiedRows)} / ${sequences(table.totalRows)}`;
  }
  if (table.copiedRows !== null) return `${sequences(table.copiedRows)} lignes`;
  return `${sequences(table.totalRows as number)} au total`;
}

export function journalCopy(table: FleetTable): string {
  const checkpoint = table.currentCheckpoint ?? table.journalTail ?? table.startCheckpoint;
  if (!checkpoint) return UNMEASURED;
  return `${checkpoint.receiver} · ${sequences(checkpoint.sequence)}`;
}

export function reconciliationCopy(table: FleetTable): string {
  const proof = table.reconciliationProof;
  if (!proof) return UNMEASURED;
  if (proof.missing === 0 && proof.extra === 0 && proof.duplicates === 0 && proof.sourceHash === proof.targetHash) {
    return 'Conforme';
  }
  return `Écarts ${sequences(proof.missing + proof.extra + proof.duplicates)}`;
}

export function performanceCopy(proof: ReconciliationProof | null): string {
  if (!proof) return UNMEASURED;
  return `${exact(proof.latencySeconds)} s · ${exact(proof.throughputRowsPerSecond)} lig/s`;
}

export function costCopy(table: FleetTable): string {
  if (table.actualCredits !== null) return `${exact(table.actualCredits)} crédit`;
  if (table.reservedCredits !== null && table.reservedCredits !== 0) {
    return `${exact(table.reservedCredits)} réservé`;
  }
  if (table.estimatedCredits !== null) return `${exact(table.estimatedCredits)} estimé`;
  return UNMEASURED;
}

export function resultCopy(table: FleetTable): string {
  const parts = [reconciliationCopy(table)];
  const performance = performanceCopy(table.reconciliationProof);
  const cost = costCopy(table);
  if (performance !== UNMEASURED) parts.push(performance);
  if (cost !== UNMEASURED) parts.push(cost);
  return parts.join(' · ');
}

export function nextActionLabel(fleet: Fleet): string {
  if (fleet.summary.nextAction === 'PREPARE') {
    return `Préparer les ${sequences(fleet.summary.tableCount)} tables`;
  }
  return nextActionCopy[fleet.summary.nextAction];
}

export function capabilityReasonCopy(reason: string | null, action: CapabilityId): string | null {
  if (reason === null) return null;
  if (reason === 'not_live' || reason === 'stale_proof') {
    return action === 'refresh'
      ? 'Le relevé affiché est ancien. Une source active doit produire un nouveau relevé.'
      : 'Le relevé affiché est ancien. Le pilotage reprend après reconnexion de la source active.';
  }
  if (reason === 'refresh_unwired') {
    return 'L’actualisation de la flotte n’est pas encore reliée au service.';
  }
  if (reason === 'operator_access_unavailable') {
    return `L’accès opérateur ${siteIdentity().environment} requis pour cette commande n’est pas disponible.`;
  }
  return action === 'refresh'
    ? 'L’actualisation est indisponible sur ce relevé.'
    : 'Le pilotage est indisponible sur ce relevé.';
}

export function nextActionDetail(fleet: Fleet): string {
  const table = fleet.summary.nextTable;
  const scope = table ? ` pour ${table}` : '';
  switch (fleet.summary.nextAction) {
    case 'PREPARE':
      return `Enregistrer le point de départ des ${sequences(fleet.summary.tableCount)} tables avant toute copie historique.`;
    case 'ADMIT_HISTORICAL':
      return `Les prérequis sont réunis${scope}. La copie historique peut démarrer dans la limite de capacité prévue.`;
    case 'RECORD_HISTORY_PROGRESS':
      return `La copie historique est en cours${scope}. Actualiser les volumes avant l’étape suivante.`;
    case 'PROVE_CONTINUITY':
      return `La copie historique est terminée${scope}. Vérifier qu’aucune mise à jour IBM i n’a été perdue pendant la bascule.`;
    case 'CATCH_UP_TO_TAIL':
      return `Appliquer les mises à jour intervenues pendant l’historique${scope}, jusqu’au temps réel.`;
    case 'RECORD_ACTUAL_COST':
      return `La consommation réelle doit être enregistrée${scope} avant de poursuivre.`;
    case 'OPEN_RECONCILIATION':
      return `Comparer les volumes, empreintes, manquants, extras et doublons${scope}.`;
    case 'CERTIFY':
      return `Toutes les preuves requises sont disponibles${scope}. La table peut être certifiée.`;
    case 'RESUME':
      return `La table suspendue${scope} peut reprendre depuis son dernier point prouvé.`;
    case 'INSPECT_BLOCKED':
      return `Une preuve empêche la poursuite${scope}. Ouvrir le détail avant toute reprise.`;
    case 'NONE':
      return `Les ${sequences(fleet.summary.tableCount)} tables sont certifiées ; aucune action supplémentaire n’est requise.`;
  }
}

/** Short action copy for the landing view; detailed operator terminology stays
 * in the technical drawer. */
export function publicNextActionLabel(fleet: Fleet): string {
  switch (fleet.summary.nextAction) {
    case 'PREPARE': return `Préparer les ${sequences(fleet.summary.tableCount)} tables`;
    case 'ADMIT_HISTORICAL': return 'Lancer la copie initiale';
    case 'RESUME': return 'Reprendre';
    case 'INSPECT_BLOCKED': return 'Voir ce qui bloque';
    case 'NONE': return 'Actualiser l’état';
    default: return 'Actualiser l’état';
  }
}

export function publicNextActionDetail(fleet: Fleet): string {
  switch (fleet.summary.nextAction) {
    case 'PREPARE': return 'Enregistrer le point de départ avant de commencer la copie initiale.';
    case 'ADMIT_HISTORICAL': return `Les ${sequences(fleet.summary.tableCount)} tables sont prêtes. La copie initiale peut commencer.`;
    case 'RECORD_HISTORY_PROGRESS': return 'La copie initiale est en cours. Relire l’état avant de poursuivre.';
    case 'PROVE_CONTINUITY': return 'Relire les mises à jour pour confirmer qu’elles ont toutes été prises en compte.';
    case 'CATCH_UP_TO_TAIL': return 'Rattraper les mises à jour intervenues pendant la copie initiale.';
    case 'RECORD_ACTUAL_COST': return 'Le coût réel doit être relevé avant de poursuivre.';
    case 'OPEN_RECONCILIATION': return 'Comparer les données reçues et attendues avant de poursuivre.';
    case 'CERTIFY': return 'Toutes les mesures sont disponibles. Ouvrir le détail pour continuer.';
    case 'RESUME': return 'La reprise peut commencer depuis le dernier point enregistré.';
    case 'INSPECT_BLOCKED': return 'Une étape empêche la poursuite. Ouvrir le détail avant toute reprise.';
    case 'NONE': return 'Aucune prochaine action n’est requise dans le relevé actuel.';
  }
}
