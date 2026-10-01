import type { EvidenceKind, Observability, SloAlertLifecycle, SloSignalStatus } from './controlPlane.ts';

const checkIds = [
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
] as const;

export function observabilityFixture({
  status = 'pass',
  lifecycle = status === 'pass' ? 'resolved' : 'firing',
  freshness = 'fresh',
  evidenceKind = 'live',
}: {
  readonly status?: SloSignalStatus;
  readonly lifecycle?: SloAlertLifecycle;
  readonly freshness?: 'fresh' | 'stale' | 'clock_untrusted';
  readonly evidenceKind?: EvidenceKind;
} = {}): Observability {
  const signalCheck = status === 'pass' ? 'snowpipe_queue' : 'snowpipe_queue';
  const checks = checkIds.map((id) => ({
    id,
    stage: stageFor(id),
    status: id === signalCheck ? status : 'pass' as const,
    observed: id === 'capture_state' ? 'RUNNING' : id === signalCheck ? (status === 'pass' ? 1 : status === 'breach' ? 11 : null) : 1,
    threshold: id === 'capture_state' ? ['RUNNING', 'STOPPED_BUDGET'] : id === signalCheck ? (status === 'unobserved' ? null : 10) : 5,
    unit: id === 'capture_state' || status === 'unobserved' && id === signalCheck ? null : unitFor(id),
    reason: id === signalCheck
      ? status === 'pass' ? 'within_threshold' : status === 'breach' ? 'threshold_exceeded' : 'measurement_missing'
      : 'within_threshold',
  }));
  const observedAt = '2026-08-28T08:33:56.470000+00:00';
  const alertStatus = lifecycle === 'resolved' ? 'pass' : status === 'pass' ? 'breach' : status;
  const alertReason = lifecycle === 'resolved' ? 'within_threshold' : alertStatus === 'breach' ? 'threshold_exceeded' : 'measurement_missing';
  const alertObserved = lifecycle === 'resolved' ? 1 : alertStatus === 'breach' ? 11 : null;
  const alertThreshold = alertStatus === 'unobserved' ? null : 10;
  return {
    status,
    quality: { coverage: 'complete', freshness, evidenceKind },
    observedAt,
    reason: status === 'pass' ? 'within_policy' : status === 'breach' ? 'threshold_breach' : 'measurement_gap',
    checks,
    alerts: [
      {
        fingerprint: `sha256:${'a'.repeat(64)}`,
        checkId: signalCheck,
        stage: 'snowpipe',
        lifecycleState: lifecycle,
        signalStatus: lifecycle === 'resolved' ? 'pass' : alertStatus,
        severity: lifecycle === 'resolved' ? 'none' : alertStatus === 'breach' ? 'critical' : 'warning',
        reason: alertReason,
        observed: alertObserved,
        threshold: alertThreshold,
        unit: alertStatus === 'unobserved' ? null : 'files',
        firstFiredAt: '2026-08-28T08:30:00+00:00',
        firingSince: '2026-08-28T08:30:00+00:00',
        lastObservedAt: observedAt,
        resolvedAt: lifecycle === 'resolved' ? observedAt : null,
        occurrenceCount: 1,
        evaluationCount: 2,
      },
    ],
  };
}

function stageFor(id: typeof checkIds[number]): string {
  if (id.startsWith('capture_')) return 'capture';
  if (id === 'checkpoint_lag') return 'checkpoint';
  if (id.startsWith('s3_')) return id === 's3_requests' ? 'cost' : 's3';
  if (id === 'snowpipe_queue') return 'snowpipe';
  if (id.startsWith('delivery_')) return 'delivery';
  if (id === 'observability_freshness') return 'observability';
  if (id === 'snowflake_credits') return 'cost';
  return 'canonical';
}

function unitFor(id: typeof checkIds[number]): string {
  if (id.endsWith('freshness') || id.startsWith('delivery_')) return 'seconds';
  if (id === 'snowpipe_queue') return 'files';
  if (id === 'checkpoint_lag') return 'sequences';
  if (id === 's3_requests') return 'requests/24h';
  if (id === 'snowflake_credits') return 'credits/24h';
  return 'events';
}
