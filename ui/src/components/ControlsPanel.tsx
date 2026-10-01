import { useReducer } from 'react';
import {
  controlsPanelReducer,
  INITIAL_CONTROLS_PANEL_STATE,
  requiresConfirmation,
  type ControlActionKind,
  type ControlLevel,
} from '../domain/controlsPanel.ts';
import { controlActionAnnouncement, controlActionCopy, controlLevelTitle, CONTROLS_PANEL_COPY } from '../domain/operator.ts';

/** Actions sensibles (confirmation obligatoire, `domain/controlsPanel.ts` ::
 *  `requiresConfirmation`) affichées avec un style visuellement distinct —
 *  jamais mêlées aux actions quotidiennes (design §1, point 1). */
const DESTRUCTIVE_ACTIONS: ReadonlySet<ControlActionKind> = new Set(['remove', 'restart_initial_copy', 'replay']);

/**
 * ControlsPanel — le panneau de contrôles réutilisé à chaque niveau du
 * cockpit (table, connexion/source, destination, flotte — docs/plans/
 * 2026-09-23-produit-fini-design.md §3). Un seul emplacement, un seul
 * comportement, quel que soit le niveau : effet annoncé (dry_run) →
 * confirmation → exécution → vérification par relecture. Une action
 * sensible qui revient en attente d'approbation reste affichée jusqu'à
 * l'approbation ou le rejet — jamais un état muet.
 *
 * Ce composant ne connaît pas `controlPlaneV2Client` : l'écran lui fournit
 * `dryRun`/`execute`/`verify`/`approve`/`reject`, ce qui le rend testable
 * sans réseau et réutilisable à un niveau qui n'a pas encore de client câblé
 * (source/destination/flotte : routes CONTRAT SEUL au moment de l'écrit —
 * voir controlPlaneV2Client.ts).
 */
export interface ControlsPanelProps {
  readonly level: ControlLevel;
  readonly targetLabel: string;
  /** Actions proposées à ce niveau — le service ne déclare jamais toutes les
   *  actions partout (ex. `remove`/`replay` n'existent qu'au niveau table). */
  readonly availableActions: readonly ControlActionKind[];
  readonly dryRun: (action: ControlActionKind) => Promise<unknown>;
  readonly execute: (action: ControlActionKind, confirmationToken?: string) => Promise<{ readonly pendingConfirmationId: string | null }>;
  readonly verify: () => Promise<void>;
  readonly approveConfirmation?: (confirmationId: string) => Promise<void>;
  readonly rejectConfirmation?: (confirmationId: string) => Promise<void>;
}

export function ControlsPanel({
  level,
  targetLabel,
  availableActions,
  dryRun,
  execute,
  verify,
  approveConfirmation,
  rejectConfirmation,
}: ControlsPanelProps) {
  const [state, dispatch] = useReducer(controlsPanelReducer, INITIAL_CONTROLS_PANEL_STATE);
  const busy = state.phase === 'previewing' || state.phase === 'running' || state.phase === 'verifying';

  const request = (action: ControlActionKind) => {
    if (busy) return;
    dispatch({ type: 'request', action });
    dryRun(action)
      .then((preview) => dispatch({ type: 'preview_ready', preview }))
      .catch((error: unknown) => dispatch({ type: 'preview_failed', error: errorMessage(error) }));
  };

  const runNow = (confirmationToken?: string) => {
    if (!state.action) return;
    dispatch({ type: 'confirm' });
    execute(state.action, confirmationToken)
      .then((result) => {
        if (result.pendingConfirmationId) {
          dispatch({ type: 'run_requires_confirmation', confirmationId: result.pendingConfirmationId });
          return;
        }
        dispatch({ type: 'run_succeeded' });
        verify()
          .then(() => dispatch({ type: 'verified' }))
          .catch((error: unknown) => dispatch({ type: 'verify_failed', error: errorMessage(error) }));
      })
      .catch((error: unknown) => dispatch({ type: 'run_failed', error: errorMessage(error) }));
  };

  const approve = () => {
    if (!state.confirmationId || !approveConfirmation) return;
    approveConfirmation(state.confirmationId)
      .then(() => dispatch({ type: 'confirmation_approved' }))
      .catch((error: unknown) => dispatch({ type: 'run_failed', error: errorMessage(error) }));
  };

  const reject = () => {
    if (!state.confirmationId || !rejectConfirmation) return;
    rejectConfirmation(state.confirmationId)
      .then(() => dispatch({ type: 'confirmation_rejected' }))
      .catch((error: unknown) => dispatch({ type: 'run_failed', error: errorMessage(error) }));
  };

  const quietActions = availableActions.filter((action) => !DESTRUCTIVE_ACTIONS.has(action));
  const destructiveActions = availableActions.filter((action) => DESTRUCTIVE_ACTIONS.has(action));

  return (
    <section className="controls-panel" aria-label={`Contrôles — ${targetLabel}`}>
      <div className="controls-panel__bar">
        <p className="controls-panel__scope">
          <span className="controls-panel__scope-level">{controlLevelTitle(level)}</span>
          <span className="controls-panel__scope-target">{targetLabel}</span>
        </p>

        {availableActions.length > 0 ? (
          <div className="controls-panel__actions">
            {quietActions.map((action) => (
              <button
                key={action}
                type="button"
                className="controls-panel__action"
                disabled={busy}
                onClick={() => request(action)}
              >
                {controlActionCopy(action).label}
              </button>
            ))}
            {destructiveActions.length > 0 ? (
              <details className="controls-panel__more">
                <summary>Plus</summary>
                <div className="controls-panel__more-menu" role="menu">
                  {destructiveActions.map((action) => (
                    <button
                      key={action}
                      type="button"
                      role="menuitem"
                      className="controls-panel__action controls-panel__action--destructive"
                      disabled={busy}
                      onClick={() => request(action)}
                    >
                      {controlActionCopy(action).label}
                    </button>
                  ))}
                </div>
              </details>
            ) : null}
          </div>
        ) : null}
      </div>

      {state.phase === 'previewing' ? <p role="status">{CONTROLS_PANEL_COPY.previewing}</p> : null}

      {state.phase === 'preview_ready' && state.action ? (
        <div className="controls-panel__confirm" role="alertdialog" aria-label={CONTROLS_PANEL_COPY.confirmTitle}>
          <p>{controlActionAnnouncement(state.action, level)}</p>
          <div className="controls-panel__confirm-actions">
            <button type="button" className="controls-panel__cancel" onClick={() => dispatch({ type: 'cancel' })}>{CONTROLS_PANEL_COPY.cancel}</button>
            <button type="button" className="controls-panel__action controls-panel__action--primary" onClick={() => runNow()}>
              {CONTROLS_PANEL_COPY.confirm}
            </button>
          </div>
        </div>
      ) : null}

      {state.phase === 'running' ? <p role="status">{CONTROLS_PANEL_COPY.running}</p> : null}

      {state.phase === 'awaiting_confirmation' ? (
        <div className="controls-panel__pending" role="status">
          <p>{CONTROLS_PANEL_COPY.awaitingConfirmation}</p>
          {state.confirmationState === 'pending' ? (
            <div className="controls-panel__confirm-actions">
              {approveConfirmation ? <button type="button" className="controls-panel__action controls-panel__action--primary" onClick={approve}>{CONTROLS_PANEL_COPY.approve}</button> : null}
              {rejectConfirmation ? <button type="button" className="controls-panel__cancel" onClick={reject}>{CONTROLS_PANEL_COPY.reject}</button> : null}
            </div>
          ) : null}
          {state.confirmationState === 'approved' ? (
            <button type="button" className="controls-panel__action controls-panel__action--primary" onClick={() => runNow(state.confirmationId ?? undefined)}>
              {CONTROLS_PANEL_COPY.rerun}
            </button>
          ) : null}
        </div>
      ) : null}

      {state.phase === 'verifying' ? <p role="status">{CONTROLS_PANEL_COPY.verifying}</p> : null}

      {state.phase === 'succeeded' ? (
        <p className="controls-panel__outcome controls-panel__outcome--succeeded" role="status">
          {CONTROLS_PANEL_COPY.succeeded}
        </p>
      ) : null}

      {state.phase === 'failed' ? (
        <p className="controls-panel__outcome controls-panel__outcome--failed" role="status">
          {state.error ?? CONTROLS_PANEL_COPY.failedGeneric}
        </p>
      ) : null}
    </section>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : CONTROLS_PANEL_COPY.failedGeneric;
}

export { requiresConfirmation };
