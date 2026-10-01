import assert from 'node:assert/strict';
import test from 'node:test';
import {
  controlsPanelReducer,
  INITIAL_CONTROLS_PANEL_STATE,
  requiresConfirmation,
  type ControlsPanelState,
} from './controlsPanel.ts';

function apply(events: Parameters<typeof controlsPanelReducer>[1][], initial: ControlsPanelState = INITIAL_CONTROLS_PANEL_STATE): ControlsPanelState {
  return events.reduce(controlsPanelReducer, initial);
}

test('a non-sensitive action goes preview -> confirm -> run -> verify -> succeeded', () => {
  const state = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: { effect: 'Suspend la table.' } },
    { type: 'confirm' },
    { type: 'run_succeeded' },
    { type: 'verified' },
  ]);
  assert.equal(state.phase, 'succeeded');
  assert.equal(state.action, 'pause');
});

test('remove/restart_initial_copy/replay are flagged sensitive; pause/resume are not', () => {
  assert.equal(requiresConfirmation('remove'), true);
  assert.equal(requiresConfirmation('restart_initial_copy'), true);
  assert.equal(requiresConfirmation('replay'), true);
  assert.equal(requiresConfirmation('pause'), false);
  assert.equal(requiresConfirmation('resume'), false);
});

test('a sensitive action can come back pending_confirmation_required and waits for approval before rerunning', () => {
  const pending = apply([
    { type: 'request', action: 'remove' },
    { type: 'preview_ready', preview: { effect: 'Retire la table du suivi.' } },
    { type: 'confirm' },
    { type: 'run_requires_confirmation', confirmationId: 'cf_1' },
  ]);
  assert.equal(pending.phase, 'awaiting_confirmation');
  assert.equal(pending.confirmationState, 'pending');

  // confirming again before approval must not start the run
  const stillWaiting = controlsPanelReducer(pending, { type: 'confirm' });
  assert.equal(stillWaiting.phase, 'awaiting_confirmation');

  const approved = controlsPanelReducer(pending, { type: 'confirmation_approved' });
  assert.equal(approved.confirmationState, 'approved');
  assert.equal(approved.phase, 'awaiting_confirmation', 'approval alone never executes the action');

  const running = controlsPanelReducer(approved, { type: 'confirm' });
  assert.equal(running.phase, 'running');

  const done = apply([{ type: 'run_succeeded' }, { type: 'verified' }], running);
  assert.equal(done.phase, 'succeeded');
});

test('a rejected confirmation fails the panel with an explicit reason, never a silent no-op', () => {
  const pending = apply([
    { type: 'request', action: 'replay' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'run_requires_confirmation', confirmationId: 'cf_2' },
  ]);
  const rejected = controlsPanelReducer(pending, { type: 'confirmation_rejected' });
  assert.equal(rejected.phase, 'failed');
  assert.equal(rejected.confirmationState, 'rejected');
  assert.match(rejected.error ?? '', /rejetée/);
});

test('a failed dry_run never reaches running, and cancel from preview_ready returns to idle without residue', () => {
  const failed = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_failed', error: 'Aperçu indisponible.' },
  ]);
  assert.equal(failed.phase, 'failed');
  assert.equal(failed.error, 'Aperçu indisponible.');

  const cancelled = apply([
    { type: 'request', action: 'resume' },
    { type: 'preview_ready', preview: {} },
    { type: 'cancel' },
  ]);
  assert.deepEqual(cancelled, INITIAL_CONTROLS_PANEL_STATE);
});

test('a verify failure after a successful run still reports failed, never a silent success', () => {
  const state = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'run_succeeded' },
    { type: 'verify_failed', error: 'Relecture impossible : nouvel état non confirmé.' },
  ]);
  assert.equal(state.phase, 'failed');
  assert.match(state.error ?? '', /Relecture impossible/);
});

test('out-of-phase events are ignored (no phantom transitions), e.g. run_succeeded while idle', () => {
  const state = controlsPanelReducer(INITIAL_CONTROLS_PANEL_STATE, { type: 'run_succeeded' });
  assert.deepEqual(state, INITIAL_CONTROLS_PANEL_STATE);
});

test('a new request while idle, succeeded or failed starts a clean cycle; not while busy', () => {
  const afterSuccess = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'run_succeeded' },
    { type: 'verified' },
    { type: 'request', action: 'resume' },
  ]);
  assert.equal(afterSuccess.phase, 'previewing');
  assert.equal(afterSuccess.action, 'resume');

  const whileRunning = apply([
    { type: 'request', action: 'pause' },
    { type: 'preview_ready', preview: {} },
    { type: 'confirm' },
    { type: 'request', action: 'resume' },
  ]);
  assert.equal(whileRunning.phase, 'running', 'a request mid-flight must not interrupt the running action');
  assert.equal(whileRunning.action, 'pause');
});
