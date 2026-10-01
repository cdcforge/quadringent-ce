import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { FleetPlan, FleetPlanTable, FleetRuntimePhase, FleetTable } from './controlPlane.ts';
import { parseFleetRuntime } from './controlPlane.ts';
import { installTestSite, TEST_SITE } from './siteFixture.ts';

installTestSite();
import {
  fleetPlanDataCopy,
  fleetPlanInitialCopy,
  fleetPlanTableStateCopy,
  fleetPlanStateCopy,
  fleetPlanUpdatesCopy,
  fleetInitialCopy,
  fleetTableDataCopy,
  fleetUpdatesCopy,
  publicNextActionDetail,
  publicNextActionLabel,
  runtimePhaseLabel,
  runtimeUpdatesCopy,
  fleetActionRequest,
} from './fleetView.ts';

function planTable(overrides: Partial<FleetPlanTable> = {}): FleetPlanTable {
  return {
    name: 'ADDRS1',
    rowCount: 218_150_587,
    dataSize: 10,
    journalImages: '*BOTH',
    identityStatus: 'blocked',
    identitySource: null,
    candidateKey: null,
    historicalAdmitted: false,
    historicalLane: null,
    blockedReasons: ['identity_unproven'],
    copiedRows: null,
    copiedBytes: null,
    historyProgress: null,
    ...overrides,
  };
}

function runtime(phase: FleetRuntimePhase) {
  return {
    formatVersion: 'quadringent-fleet-runtime-v1' as const,
    fleetId: TEST_SITE.fleetId,
    environment: TEST_SITE.runtimeEnvironment,
    pipelineId: TEST_SITE.runtimePipelineId,
    phase,
    checkpoint: null,
    capabilities: {
      refresh: { state: 'unavailable' as const, reason: 'refresh_unwired' },
      prepare: { state: 'unavailable' as const, reason: 'operator_access_unavailable' },
      start: { state: 'unavailable' as const, reason: 'operator_access_unavailable' },
      pause: { state: 'unavailable' as const, reason: 'operator_access_unavailable' },
      resume: { state: 'unavailable' as const, reason: 'operator_access_unavailable' },
    },
    tableStates: [],
  };
}

function plan(): FleetPlan {
  return {
    environment: TEST_SITE.environment,
    sourceSchema: TEST_SITE.sourceSchema,
    destinationNamespace: TEST_SITE.destinationNamespace,
    observedAt: '2026-09-13T09:00:00Z',
    freshness: 'fresh',
    continuity: 'uncertain',
    liveBlocked: true,
    certificationBlocked: true,
    historyAdmitted: false,
    cutoverCheckpoint: { receiver: 'TRNJRN3776', sequence: 1 },
    cutoverRequiredBeforeHistory: true,
    journal: { library: 'LEDGER', name: TEST_SITE.journalName, readerKind: 'multi_object', readerCount: 1 },
    identity: { keyedCount: 0, rrnCount: 0, blockedCount: 13, keyed: [], rrn: [], blocked: [] },
    observedTotals: { tableCount: 13, rowCount: 218_150_587, dataSize: 130 },
    historical: { maxConcurrency: 2, byteBudget: null, admittedCount: 0, excludedCount: 13 },
    cost: { status: 'unknown', observed: null, unknownBecause: 'not measured' },
    tables: [],
  };
}

function fleetTable(overrides: Partial<FleetTable> = {}): FleetTable {
  return {
    name: 'EXPENS',
    phase: 'LIVE',
    startCheckpoint: null,
    currentCheckpoint: { receiver: 'TRNJRN3776', sequence: 120 },
    journalTail: { receiver: 'TRNJRN3776', sequence: 120 },
    receiverChain: null,
    copiedRows: null,
    totalRows: null,
    continuityProven: true,
    gap: false,
    proofWindow: null,
    pausedFrom: null,
    blockedReason: null,
    admitted: true,
    estimatedCredits: null,
    reservedCredits: null,
    actualCredits: null,
    reconciliationProof: null,
    ...overrides,
  };
}

test('unknown fleet volumes never become zero and catalogue rows stay source-labelled', () => {
  const table = { totalRows: null } as unknown as FleetTable;
  assert.equal(fleetTableDataCopy(table), 'Volume non mesuré');
  assert.equal(fleetPlanInitialCopy(planTable()), 'À préparer');
  assert.match(fleetPlanInitialCopy(planTable()), /préparer/);
  assert.equal(fleetPlanDataCopy(planTable()), '218 150 587 lignes source');
  assert.doesNotMatch(fleetPlanDataCopy(planTable()), /copi/i);
});

test('catalogue admission alone never looks prepared without runtime evidence', () => {
  const admitted = planTable({ historicalAdmitted: true });
  assert.equal(fleetPlanInitialCopy(admitted), 'À préparer');
  assert.equal(fleetPlanTableStateCopy(admitted), 'À préparer');
  assert.doesNotMatch(fleetPlanInitialCopy(admitted), /prête/i);
});

test('plan updates describe runtime state, not the journal image format', () => {
  const table = planTable();
  assert.equal(fleetPlanUpdatesCopy(table), 'Pas encore démarrées');
  assert.equal(fleetPlanUpdatesCopy(table, 'PREPARED'), 'Pas encore démarrées');
  assert.equal(fleetPlanUpdatesCopy(table, 'HISTORICAL'), 'À confirmer');
  assert.equal(runtimeUpdatesCopy('UNKNOWN'), 'À confirmer');
});

test('runtime state remains bounded when the plan has no live execution', () => {
  assert.equal(fleetPlanStateCopy(plan(), null).label, 'Non établi');
  assert.equal(fleetPlanStateCopy(plan(), runtime('HISTORICAL')).label, 'En cours');
  assert.equal(fleetPlanStateCopy(plan(), runtime('BLOCKED')).label, 'Bloquée');
});

test('public action copy keeps technical action names out of the landing view', () => {
  const fleet = { summary: { nextAction: 'PREPARE', tableCount: TEST_SITE.manifest.length } } as any;
  const label = publicNextActionLabel(fleet);
  assert.equal(label, 'Préparer les 13 tables');
  assert.doesNotMatch(label, /preuve|certif|verdict|pipeline|checkpoint/i);
  assert.doesNotMatch(publicNextActionDetail(fleet), /preuve|certif|verdict|pipeline|checkpoint/i);
});

test('non-current fleet observations qualify table cells and live without copied totals', () => {
  const table = fleetTable();
  assert.equal(fleetInitialCopy(table), 'À confirmer');
  assert.equal(fleetInitialCopy(table, false), 'À confirmer');
  assert.equal(fleetUpdatesCopy(table, false), 'À confirmer');
  assert.equal(fleetInitialCopy({ ...table, copiedRows: 12, totalRows: 12 }), 'Terminée');
});

test('fleet action requests carry exactly the server contract keys', () => {
  const declared = `${TEST_SITE.siteId.toUpperCase()} ${TEST_SITE.environment}`;
  assert.deepEqual(fleetActionRequest(TEST_SITE, 'start'), {
    fleet_id: TEST_SITE.fleetId,
    environment: TEST_SITE.runtimeEnvironment,
    confirmation: `START ${declared}`,
  });
  assert.deepEqual(fleetActionRequest(TEST_SITE, 'refresh'), {
    fleet_id: TEST_SITE.fleetId,
    environment: TEST_SITE.runtimeEnvironment,
    confirmation: null,
  });
  const pause = fleetActionRequest(TEST_SITE, 'pause');
  assert.deepEqual(Object.keys(pause).sort(), ['confirmation', 'environment', 'fleet_id']);
  assert.equal(pause.confirmation, `PAUSE ${declared}`);
});

test('domain runtime phases keep plain-language labels and parse mixed table states', () => {
  const expected: Record<FleetRuntimePhase, string> = {
    NOT_PREPARED: 'À préparer',
    PREPARED: 'Prête à démarrer',
    HISTORICAL: 'Copie initiale en cours',
    CATCHING_UP: 'Rattrapage en cours',
    LIVE: 'En temps réel',
    RECONCILING: 'Vérification en cours',
    CERTIFIED: 'Certifiée',
    PAUSED: 'Suspendue',
    BLOCKED: 'Bloquée',
    UNKNOWN: 'À confirmer',
  };
  for (const [phase, label] of Object.entries(expected) as [FleetRuntimePhase, string][]) {
    assert.equal(runtimePhaseLabel(phase), label);
    assert.doesNotMatch(label, /checkpoint|receiver|journal|sequence|pipeline/i);
  }
  assert.equal(runtimePhaseLabel('READY'), 'Prête');

  const wire = {
    format_version: 'quadringent-fleet-runtime-v1',
    fleet_id: TEST_SITE.fleetId,
    environment: TEST_SITE.runtimeEnvironment,
    pipeline_id: TEST_SITE.runtimePipelineId,
    phase: 'HISTORICAL',
    checkpoint: null,
    capabilities: {
      refresh: { state: 'unavailable', reason: 'refresh_unwired' },
      prepare: { state: 'unavailable', reason: 'already_prepared' },
      start: { state: 'unavailable', reason: 'already_started' },
      pause: { state: 'available', reason: null },
      resume: { state: 'unavailable', reason: 'unsupported_action' },
    },
    table_states: TEST_SITE.manifest.map((name, index) => ({
      name,
      phase: index < 4 ? 'LIVE' : index < 8 ? 'CATCHING_UP' : 'READY',
      copied_rows: index < 8 ? 100 + index : null,
      total_rows: index < 8 ? 100 + index : null,
    })),
  };
  const parsed = parseFleetRuntime(wire);
  assert.equal(parsed?.phase, 'HISTORICAL');
  assert.equal(parsed?.tableStates[0].phase, 'LIVE');
  assert.equal(parsed?.tableStates[7].phase, 'CATCHING_UP');
  assert.equal(parsed?.tableStates[12].phase, 'READY');
  assert.equal(parsed?.tableStates[0].copiedRows, 100);
  assert.equal(parsed?.tableStates[12].copiedRows, null);
});

test('runtime phases past the copy keep updates observed and never claim a fresh start', () => {
  assert.equal(runtimeUpdatesCopy('CATCHING_UP'), 'Observées');
  assert.equal(runtimeUpdatesCopy('LIVE'), 'Observées');
  assert.equal(runtimeUpdatesCopy('READY'), 'Pas encore démarrées');
  const table = planTable();
  // Le domaine n'admet CATCHING_UP qu'après copied == total mesuré :
  // la copie initiale est terminée dès cette phase.
  assert.equal(fleetPlanInitialCopy(table, 'LIVE'), 'Terminée');
  assert.equal(fleetPlanInitialCopy(table, 'CATCHING_UP'), 'Terminée');
  assert.equal(fleetPlanInitialCopy(table, 'HISTORICAL'), 'En cours');
  assert.equal(fleetPlanTableStateCopy(table, 'CATCHING_UP'), 'Rattrapage en cours');
  assert.equal(fleetPlanStateCopy(plan(), runtime('LIVE')).label, 'Disponible');
});
