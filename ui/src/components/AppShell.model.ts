import type { ConnectionState } from '../data/controlPlaneController.ts';
import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import { resolveProofFocus } from '../domain/proofFocus.ts';
import { scopeLabel } from '../domain/scope.ts';

export interface ConnectionCopy {
  readonly label: string;
  readonly detail: string;
}

export type WorkspaceFocusScheduler = (focusWorkspace: () => void) => void;

interface MobileDetailsTarget {
  open: boolean;
  querySelector(selector: string): { focus(): void } | null;
}

interface MobileDetailsKeyEvent {
  readonly key: string;
  readonly currentTarget: MobileDetailsTarget;
  preventDefault(): void;
  stopPropagation(): void;
}

export function connectionCopy(
  connection: ConnectionState,
  status?: ControlPlaneState['status'],
  transport: 'current' | 'technical' = 'current',
): ConnectionCopy {
  if (status === 'refreshing' || status === 'loading') {
    return { label: 'Vérification en cours', detail: 'Le service est en cours de vérification' };
  }
  if (status === 'degraded' || status === 'failed') {
    return { label: 'Indisponible', detail: 'Le service est indisponible' };
  }
  switch (connection) {
    case 'live':
      return { label: 'Disponible', detail: transport === 'technical'
        ? 'Le service répond ; l’état des données est indiqué séparément'
        : 'Le service répond' };
    case 'reconnecting':
      return { label: 'Reconnexion en cours', detail: 'La connexion au service reprend' };
    case 'offline':
      return { label: 'Hors ligne', detail: 'La connexion au service est interrompue' };
    case 'connecting':
    default:
      return { label: 'Connexion en cours', detail: 'La connexion au service est en cours' };
  }
}

export function closeMobileDetailsOnEscape(event: MobileDetailsKeyEvent): boolean {
  if (event.key !== 'Escape' || !event.currentTarget.open) return false;
  event.preventDefault();
  event.stopPropagation();
  event.currentTarget.open = false;
  event.currentTarget.querySelector('summary')?.focus();
  return true;
}

export function liveAnnouncement(state: ControlPlaneState): string {
  const service = connectionCopy(state.connection, state.status).detail;
  const scope = scopeLabel(state.status === 'loading' || state.status === 'failed' ? null : state.overview.scope);
  if (state.status === 'loading' || state.status === 'failed') {
    return `${service}. Environnement : ${scope}. Données indisponibles.`;
  }
  const focus = resolveProofFocus(state);
  const dataState = announcementDataState(focus);
  return `${service}. Environnement : ${scope}. ${dataState}.`;
}

function announcementDataState(focus: ReturnType<typeof resolveProofFocus>): string {
  switch (focus.proofScope.kind) {
    case 'live':
      return focus.focus.kind === 'completion' ? 'Données disponibles dans Snowflake' : 'Données à vérifier';
    case 'simulation':
      return 'Données de démonstration';
    case 'historical':
      return 'Dernier relevé historique';
    case 'cached':
      return 'Dernier relevé conservé';
    case 'stale':
      return 'Relevé pas à jour';
    case 'partial':
    case 'mixed':
      return 'Informations partielles';
    case 'unavailable':
      return 'Données indisponibles';
  }
}

/** Libellé court et lisible de l'état des données affiché dans la coquille. */
export function evidenceStateLabel(shortLabel: string): string {
  switch (shortLabel) {
    case 'LIVE': return 'Relevé récent';
    case 'SIM': return 'Démonstration';
    case 'HIST': return 'Relevé historique';
    case 'CACHE': return 'Relevé conservé';
    case 'PARTIEL': return 'Informations partielles';
    case 'NON COURANT': return 'Pas à jour';
    default: return 'À vérifier';
  }
}

export function scheduleWorkspaceFocus(focusWorkspace: () => void): void {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => focusWorkspace());
    return;
  }
  if (typeof queueMicrotask === 'function') {
    queueMicrotask(focusWorkspace);
    return;
  }
  setTimeout(focusWorkspace, 0);
}

export function skipWorkspaceInteraction(
  event: { preventDefault(): void },
  focusWorkspace: () => void,
  scheduleFocus: WorkspaceFocusScheduler = scheduleWorkspaceFocus,
): void {
  event.preventDefault();
  scheduleFocus(focusWorkspace);
}

export function restoreWorkspaceAfterNavigation(
  scrollToOrigin: () => void,
  focusWorkspace: () => void,
  scheduleFocus: WorkspaceFocusScheduler = scheduleWorkspaceFocus,
): void {
  scrollToOrigin();
  scheduleFocus(focusWorkspace);
}
