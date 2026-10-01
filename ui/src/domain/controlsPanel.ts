/**
 * Machine à états pure du panneau de contrôles réutilisé à chaque niveau du
 * cockpit (accueil/flotte, connexion/source, table — docs/plans/
 * 2026-09-23-produit-fini-design.md §3 « contrôles à chaque niveau, toujours
 * au même endroit »). Aucun accès réseau ici : `ControlsPanel.tsx` appelle
 * cette machine et lui fournit les résultats de `controlPlaneV2Client.ts`.
 *
 * Parcours imposé par le produit : effet annoncé (dry_run) → confirmation →
 * exécution → vérification par relecture (jamais un simple « ça a dû marcher »).
 * Une action sensible peut répondre `pending_confirmation_required` : le
 * panneau reste ouvert sur « en attente d'approbation » jusqu'à ce que la
 * confirmation soit approuvée, puis l'opérateur relance explicitement.
 */

export type ControlActionKind = 'pause' | 'resume' | 'restart_initial_copy' | 'remove' | 'replay';
export type ControlLevel = 'table' | 'connection' | 'destination' | 'fleet';
export type ControlsPanelPhase =
  | 'idle'
  | 'previewing'
  | 'preview_ready'
  | 'running'
  | 'awaiting_confirmation'
  | 'verifying'
  | 'succeeded'
  | 'failed';

export interface ControlsPanelState {
  readonly phase: ControlsPanelPhase;
  readonly action: ControlActionKind | null;
  readonly preview: unknown | null;
  readonly confirmationId: string | null;
  readonly confirmationState: 'pending' | 'approved' | 'rejected' | null;
  readonly error: string | null;
}

export type ControlsPanelEvent =
  | { readonly type: 'request'; readonly action: ControlActionKind }
  | { readonly type: 'preview_ready'; readonly preview: unknown }
  | { readonly type: 'preview_failed'; readonly error: string }
  | { readonly type: 'confirm' }
  | { readonly type: 'cancel' }
  | { readonly type: 'run_succeeded' }
  | { readonly type: 'run_requires_confirmation'; readonly confirmationId: string }
  | { readonly type: 'run_failed'; readonly error: string }
  | { readonly type: 'confirmation_approved' }
  | { readonly type: 'confirmation_rejected' }
  | { readonly type: 'verified' }
  | { readonly type: 'verify_failed'; readonly error: string }
  | { readonly type: 'reset' };

export const INITIAL_CONTROLS_PANEL_STATE: ControlsPanelState = {
  phase: 'idle',
  action: null,
  preview: null,
  confirmationId: null,
  confirmationState: null,
  error: null,
};

export function controlsPanelReducer(state: ControlsPanelState, event: ControlsPanelEvent): ControlsPanelState {
  switch (event.type) {
    case 'request':
      if (state.phase !== 'idle' && state.phase !== 'succeeded' && state.phase !== 'failed') return state;
      return { ...INITIAL_CONTROLS_PANEL_STATE, phase: 'previewing', action: event.action };

    case 'preview_ready':
      if (state.phase !== 'previewing') return state;
      return { ...state, phase: 'preview_ready', preview: event.preview };

    case 'preview_failed':
      if (state.phase !== 'previewing') return state;
      return { ...state, phase: 'failed', error: event.error };

    case 'confirm':
      if (state.phase === 'preview_ready') return { ...state, phase: 'running' };
      if (state.phase === 'awaiting_confirmation' && state.confirmationState === 'approved') {
        return { ...state, phase: 'running' };
      }
      return state;

    case 'cancel':
      if (state.phase === 'preview_ready' || state.phase === 'failed') return INITIAL_CONTROLS_PANEL_STATE;
      return state;

    case 'run_succeeded':
      if (state.phase !== 'running') return state;
      return { ...state, phase: 'verifying' };

    case 'run_requires_confirmation':
      if (state.phase !== 'running') return state;
      return { ...state, phase: 'awaiting_confirmation', confirmationId: event.confirmationId, confirmationState: 'pending' };

    case 'run_failed':
      if (state.phase !== 'running') return state;
      return { ...state, phase: 'failed', error: event.error };

    case 'confirmation_approved':
      if (state.phase !== 'awaiting_confirmation') return state;
      return { ...state, confirmationState: 'approved' };

    case 'confirmation_rejected':
      if (state.phase !== 'awaiting_confirmation') return state;
      return { ...state, phase: 'failed', confirmationState: 'rejected', error: 'Confirmation rejetée : action non exécutée.' };

    case 'verified':
      if (state.phase !== 'verifying') return state;
      return { ...state, phase: 'succeeded' };

    case 'verify_failed':
      if (state.phase !== 'verifying') return state;
      return { ...state, phase: 'failed', error: event.error };

    case 'reset':
      return INITIAL_CONTROLS_PANEL_STATE;
  }
}

/** Actions considérées sensibles par le contrat (§2.4) : jamais exécutées
 *  sans confirmation approuvée, quel que soit le niveau. */
const SENSITIVE_ACTIONS = new Set<ControlActionKind>(['remove', 'restart_initial_copy', 'replay']);

export function requiresConfirmation(action: ControlActionKind): boolean {
  return SENSITIVE_ACTIONS.has(action);
}
