import type { FleetTableName } from '../../domain/controlPlane.ts';
import { TEST_SITE } from '../../domain/siteFixture.ts';

const WINDOW = {
  start_utc: '2026-09-13T08:00:00Z',
  end_utc: '2026-09-13T09:00:00Z',
} as const;

function emptyTable(name: FleetTableName): Record<string, unknown> {
  return {
    name,
    phase: 'NOT_PREPARED',
    start_checkpoint: null,
    current_checkpoint: null,
    journal_tail: null,
    receiver_chain: null,
    copied_rows: null,
    total_rows: null,
    continuity_proven: null,
    gap: null,
    proof_window: null,
    paused_from: null,
    blocked_reason: null,
    admitted: false,
    estimated_credits: null,
    reserved_credits: null,
    actual_credits: null,
    reconciliation_proof: null,
  };
}

function checkpoint(receiver: string, sequence: number): Record<string, unknown> {
  return { receiver, sequence };
}

function unavailableCapabilities(): Record<string, { state: 'unavailable'; reason: string }> {
  return {
    refresh: { state: 'unavailable', reason: 'refresh_unwired' },
    prepare: { state: 'unavailable', reason: 'operator_access_unavailable' },
    start: { state: 'unavailable', reason: 'operator_access_unavailable' },
    pause: { state: 'unavailable', reason: 'operator_access_unavailable' },
    resume: { state: 'unavailable', reason: 'operator_access_unavailable' },
  };
}

export function testFleetWire(mutate?: (fleet: Record<string, any>) => void): Record<string, unknown> {
  const fleet = {
    fleet_id: TEST_SITE.fleetId,
    format_version: 'quadringent-fleet-v1',
    environment: TEST_SITE.environment,
    destination_namespace: TEST_SITE.destinationNamespace,
    max_concurrency: 2,
    credit_budget: 40,
    consumed_credits: 0,
    reserved_credits: 0,
    tables: TEST_SITE.manifest.map((name) => emptyTable(name)),
    summary: {
      certified_count: 0,
      table_count: TEST_SITE.manifest.length,
      running_count: 0,
      admitted_count: 0,
      known_copied_rows: null,
      known_total_rows: null,
      credit_budget: 40,
      consumed_credits: 0,
      reserved_credits: 0,
      over_budget: false,
      next_action: 'PREPARE',
      next_reason: 'missing_start_checkpoint',
      next_table: 'ADDRS1',
    },
    capabilities: unavailableCapabilities(),
  };
  mutate?.(fleet);
  return fleet;
}

export function measuredTestFleetWire(): Record<string, unknown> {
  return testFleetWire((fleet) => {
    const byName = Object.fromEntries(
      (fleet.tables as Array<Record<string, unknown>>).map((table) => [table.name, table]),
    );
    Object.assign(byName.CAL001, {
      phase: 'READY',
      start_checkpoint: checkpoint('TRNJRN3776', 10),
    });
    Object.assign(byName.COST1, {
      phase: 'HISTORICAL',
      start_checkpoint: checkpoint('TRNJRN3776', 20),
      current_checkpoint: checkpoint('TRNJRN3776', 80),
      copied_rows: 4_200,
      total_rows: 10_000,
      continuity_proven: null,
      gap: null,
      admitted: true,
      estimated_credits: 1.5,
      reserved_credits: 1.5,
    });
    Object.assign(byName.CUSTOM1, {
      phase: 'HISTORICAL',
      start_checkpoint: checkpoint('TRNJRN3776', 30),
      copied_rows: null,
      total_rows: null,
      admitted: true,
      estimated_credits: 2,
      reserved_credits: 2,
    });
    Object.assign(byName.ORDER, {
      phase: 'CATCHING_UP',
      start_checkpoint: checkpoint('TRNJRN3776', 1),
      current_checkpoint: checkpoint('TRNJRN3776', 40),
      journal_tail: checkpoint('TRNJRN3776', 90),
      continuity_proven: true,
      gap: false,
      admitted: true,
      estimated_credits: 1,
      reserved_credits: 1,
    });
    Object.assign(byName.EXPENS, {
      phase: 'LIVE',
      start_checkpoint: checkpoint('TRNJRN3776', 5),
      current_checkpoint: checkpoint('TRNJRN3776', 120),
      journal_tail: checkpoint('TRNJRN3776', 120),
      continuity_proven: true,
      gap: false,
      admitted: true,
      estimated_credits: 0.8,
      reserved_credits: 0,
      actual_credits: 0.8,
    });
    Object.assign(byName.DATE01, {
      phase: 'RECONCILING',
      start_checkpoint: checkpoint('TRNJRN3776', 8),
      current_checkpoint: checkpoint('TRNJRN3776', 55),
      journal_tail: checkpoint('TRNJRN3776', 55),
      continuity_proven: true,
      gap: false,
      proof_window: { ...WINDOW },
      admitted: true,
      estimated_credits: 0.4,
      reserved_credits: 0,
      actual_credits: 0.4,
    });
    Object.assign(byName.SALE, {
      phase: 'CERTIFIED',
      start_checkpoint: checkpoint('TRNJRN3776', 2),
      current_checkpoint: checkpoint('TRNJRN3776', 200),
      journal_tail: checkpoint('TRNJRN3776', 200),
      copied_rows: 10_194,
      total_rows: 10_194,
      continuity_proven: true,
      gap: false,
      proof_window: { ...WINDOW },
      admitted: true,
      estimated_credits: 1.2,
      reserved_credits: 0,
      actual_credits: 1.2,
      reconciliation_proof: {
        window: { ...WINDOW },
        source_count: 10_194,
        target_count: 10_194,
        missing: 0,
        extra: 0,
        duplicates: 0,
        source_hash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        target_hash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        destination_freshness_seconds: 12,
        freshness_slo_seconds: 60,
        latency_seconds: 4.5,
        throughput_rows_per_second: 1200,
        cost_units: 1.2,
      },
    });
    fleet.max_concurrency = 4;
    fleet.consumed_credits = 2.4;
    fleet.reserved_credits = 4.5;
    fleet.summary = {
      certified_count: 1,
      table_count: TEST_SITE.manifest.length,
      running_count: 3,
      admitted_count: 6,
      known_copied_rows: 14_394,
      known_total_rows: 20_194,
      credit_budget: 40,
      consumed_credits: 2.4,
      reserved_credits: 4.5,
      over_budget: false,
      next_action: 'PREPARE',
      next_reason: 'missing_start_checkpoint',
      next_table: 'ADDRS1',
    };
    fleet.capabilities = {
      refresh: { state: 'available', reason: null },
      prepare: { state: 'available', reason: null },
      start: { state: 'available', reason: null },
      pause: { state: 'available', reason: null },
      resume: { state: 'unavailable', reason: 'not_paused' },
    };
  });
}
