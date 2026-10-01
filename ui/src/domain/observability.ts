import type { Observability, SloCheck, SloSignalStatus, SloValue } from './controlPlane.ts';

const numberFormat = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 3 });

const checkLabels: Readonly<Record<string, string>> = {
  capture_freshness: 'Fraîcheur de capture',
  capture_state: 'État de capture',
  capture_errors: 'Erreurs de capture',
  checkpoint_lag: 'Retard du point de reprise',
  s3_freshness: 'Fraîcheur du stockage S3',
  s3_requests: 'Demandes au stockage S3 sur 24 h',
  snowpipe_queue: 'File Snowpipe en attente',
  canonical_freshness: 'Fraîcheur de la table canonique',
  delivery_latency_p95: 'Latence de livraison p95',
  delivery_latency_p99: 'Latence de livraison p99',
  reconciliation: 'Réconciliation de bout en bout',
  snowflake_credits: 'Crédits Snowflake sur 24 h',
  observability_freshness: 'Fraîcheur des contrôles',
};

const stageLabels: Readonly<Record<string, string>> = {
  capture: 'Lecture',
  checkpoint: 'Point de reprise',
  s3: 'Stockage S3',
  cost: 'Coûts observés',
  snowpipe: 'Snowpipe',
  canonical: 'Table canonique',
  delivery: 'Livraison',
  destination: 'Destination',
};

const reasonLabels: Readonly<Record<string, string>> = {
  within_threshold: 'Dans la limite définie',
  running: 'Capture en cours',
  planned_stop: 'Arrêt planifié conforme',
  threshold_exceeded: 'Limite dépassée',
  freshness_exceeded: 'Fraîcheur dépassée',
  count_mismatch: 'Écart de réconciliation',
  capture_fail_closed: 'Capture arrêtée en sécurité',
  measurement_missing: 'Mesure absente',
  run_missing: 'État de capture absent',
  run_state_unknown: 'État de capture non reconnu',
  position_missing: 'Position de lecture absente',
  receiver_chain_required: 'Chaîne de réception à confirmer',
  proof_missing: 'Preuve de destination absente',
  counts_missing: 'Comptages de réconciliation incomplets',
  clock_untrusted: 'Horloge non fiable',
};

const valueLabels: Readonly<Record<string, string>> = {
  RUNNING: 'En cours',
  PAUSED_SOURCE: 'Source en pause',
  STOPPED_BUDGET: 'Arrêt planifié',
  STOPPED_FAIL_CLOSED: 'Arrêt en sécurité',
  STOPPED_AUTH_BLOCKED: 'Authentification bloquée',
};

const checkOrder = Object.keys(checkLabels);

export function sloCheckLabel(id: string): string {
  return checkLabels[id] ?? 'Contrôle retiré';
}

export function sloStageLabel(stage: string): string {
  return stageLabels[stage] ?? 'Étape retirée';
}

export function sloReasonLabel(reason: string): string {
  const metering = /^metering_window_(\d{10})_(\d{10})_([1-9]\d{0,3})$/.exec(reason);
  if (metering) {
    const [, start, end, count] = metering;
    if (Number(end) - Number(start) === 86400 && Number(count) <= 1000) {
      const format = (seconds: string) => new Intl.DateTimeFormat('fr-FR', {
        timeZone: 'UTC', dateStyle: 'short', timeStyle: 'short',
      }).format(new Date(Number(seconds) * 1000));
      return `Du ${format(start)} au ${format(end)} UTC · ${count} relevé${count === '1' ? '' : 's'} horaire${count === '1' ? '' : 's'}`;
    }
  }
  return reasonLabels[reason] ?? 'Motif non publié';
}

export function sloSignalLabel(status: SloSignalStatus): string {
  if (status === 'pass') return 'Conforme';
  if (status === 'breach') return 'Dépassement';
  return 'À mesurer';
}

export function sloValueLabel(value: SloValue, unit: string | null): string {
  if (value === null) return 'Non mesuré';
  if (typeof value !== 'number' && typeof value !== 'string') {
    return value.map((item) => publicValueLabel(item, unit)).join(' ou ');
  }
  const rendered = typeof value === 'number' ? numberFormat.format(value) : publicValueLabel(value, unit);
  const renderedUnit = unit ? unitLabel(unit, typeof value === 'number' ? value : null) : null;
  return renderedUnit ? `${rendered} ${renderedUnit}` : rendered;
}

export function orderedSloChecks(checks: readonly SloCheck[]): readonly SloCheck[] {
  return [...checks].sort((left, right) => {
    const leftIndex = checkOrder.indexOf(left.id);
    const rightIndex = checkOrder.indexOf(right.id);
    return (leftIndex < 0 ? Number.MAX_SAFE_INTEGER : leftIndex)
      - (rightIndex < 0 ? Number.MAX_SAFE_INTEGER : rightIndex);
  });
}

export function observabilityIsCurrent(observability: Observability, currentEvidence: boolean): boolean {
  return currentEvidence
    && observability.status !== 'unavailable'
    && observability.quality.coverage === 'complete'
    && observability.quality.freshness === 'fresh'
    && observability.quality.evidenceKind === 'live';
}

export function observabilityBoundary(observability: Observability): string {
  if (observability.status === 'unavailable') return 'Contrôles non publiés';
  if (observability.quality.coverage !== 'complete') return 'Couverture des contrôles partielle';
  if (observability.quality.freshness === 'stale') return 'Observation des contrôles périmée';
  if (observability.quality.freshness === 'clock_untrusted') return 'Fraîcheur des contrôles non établie';
  if (observability.quality.evidenceKind === 'simulation') return 'Contrôles bornés à la simulation';
  if (observability.quality.evidenceKind === 'historical') return 'Contrôles du snapshot historique';
  return 'Observation des contrôles fraîche';
}

function publicValueLabel(value: string, unit: string | null): string {
  return valueLabels[value] ?? (unit === 'state' ? 'État non publié' : 'Valeur non publiée');
}

function unitLabel(unit: string, value: number | null): string | null {
  const singular = value === 1;
  switch (unit) {
    case 'seconds': return 's';
    case 'requests/24h': return `${singular ? 'demande' : 'demandes'} / 24 h`;
    case 'credits/24h': return `${singular ? 'crédit' : 'crédits'} / 24 h`;
    case 'warehousecredits/delayed24h': return `${singular ? 'crédit' : 'crédits'} warehouse / 24 h différées`;
    case 'state': return null;
    case 'errors': return singular ? 'erreur' : 'erreurs';
    case 'files': return singular ? 'fichier' : 'fichiers';
    case 'events': return singular ? 'événement' : 'événements';
    case 'sequences': return singular ? 'position' : 'positions';
    default: return unit;
  }
}
