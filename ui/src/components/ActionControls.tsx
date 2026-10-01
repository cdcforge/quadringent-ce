import { useEffect, useRef, useState } from 'react';
import type { ActionId, PipelineActionReceipt } from '../data/controlPlaneClient.ts';
import type { FleetCapability } from '../domain/controlPlane.ts';
import { formatUtc } from '../domain/fleetView.ts';
import { siteIdentity } from '../domain/siteIdentity.ts';
import { sequences } from '../domain/format.ts';

export const ACTION_LABELS: Readonly<Record<ActionId, string>> = {
  refresh: 'Actualiser l’état',
  prepare: 'Préparer les tables',
  start: 'Lancer la copie initiale',
  pause: 'Suspendre',
  resume: 'Reprendre',
};

/** Libellé d'action : le nombre de tables provient du manifeste publié par
 *  le service (ou du document observé), jamais d'une valeur codée ici. */
export function actionLabel(action: ActionId, tableCount?: number | null): string {
  if (action === 'prepare') {
    const count = tableCount ?? siteIdentity().manifest.length;
    return `Préparer les ${sequences(count)} tables`;
  }
  return ACTION_LABELS[action];
}

export interface BusinessActionControls {
  readonly pendingAction: ActionId | null;
  readonly busy: boolean;
  readonly receipt: PipelineActionReceipt | null;
  readonly actionError: boolean;
  readonly requestAction: (action: ActionId) => void;
  readonly confirmAction: (action: ActionId) => void;
  readonly cancelAction: () => void;
}

/**
 * Parcours de confirmation + exécution partagé entre l’Accueil et le détail :
 * la mutation est d’abord confirmée, puis le reçu du service est affiché tel quel.
 */
export function useBusinessActions(
  onBusinessAction: ((pipelineId: string, action: ActionId) => Promise<PipelineActionReceipt>) | undefined,
  pipelineId: string,
): BusinessActionControls {
  const [pendingAction, setPendingAction] = useState<ActionId | null>(null);
  const [runningAction, setRunningAction] = useState<ActionId | null>(null);
  const [receipt, setReceipt] = useState<PipelineActionReceipt | null>(null);
  const [actionError, setActionError] = useState(false);
  const busy = runningAction !== null;

  const runAction = async (action: ActionId) => {
    if (!onBusinessAction || busy) return;
    setPendingAction(null);
    setRunningAction(action);
    setReceipt(null);
    setActionError(false);
    try {
      setReceipt(await onBusinessAction(pipelineId, action));
    } catch {
      setActionError(true);
    } finally {
      setRunningAction(null);
    }
  };

  const requestAction = (action: ActionId) => {
    if (action === 'refresh') {
      void runAction(action);
      return;
    }
    setPendingAction(action);
  };

  return {
    pendingAction,
    busy,
    receipt,
    actionError,
    requestAction,
    confirmAction: (action: ActionId) => { void runAction(action); },
    cancelAction: () => setPendingAction(null),
  };
}

export function RefreshButton({
  label,
  describedBy,
  emphasized = false,
  disabled = false,
  onRefresh,
}: {
  readonly label: string;
  readonly describedBy: string;
  readonly emphasized?: boolean;
  readonly disabled?: boolean;
  readonly onRefresh: () => void;
}) {
  return (
    <button
      className={`page-action${emphasized ? ' page-action--primary' : ''}`}
      type="button"
      disabled={disabled}
      aria-describedby={describedBy}
      onClick={onRefresh}
    >
      {label}
    </button>
  );
}

export function SteeringButton({
  label,
  capability,
  describedBy,
  emphasized = false,
  disabled = false,
  onAction,
}: {
  readonly label: string;
  readonly capability: FleetCapability;
  readonly describedBy: string;
  readonly emphasized?: boolean;
  readonly disabled?: boolean;
  readonly onAction?: () => void;
}) {
  const available = capability.state === 'available' && !disabled && onAction !== undefined;
  return (
    <button
      className={`page-action${emphasized ? ' page-action--primary' : ''}`}
      type="button"
      disabled={!available}
      aria-describedby={available ? undefined : describedBy}
      onClick={available ? onAction : undefined}
    >
      {label}
    </button>
  );
}

export function BusinessActionDialog({
  action,
  onCancel,
  onConfirm,
}: {
  readonly action: ActionId | null;
  readonly onCancel: () => void;
  readonly onConfirm: (action: ActionId) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (action && !element.open) element.showModal();
    if (!action && element.open) element.close();
  }, [action]);
  return (
    <dialog ref={dialog} className="overview-action-dialog" onCancel={onCancel} aria-labelledby="overview-action-dialog-title">
      {action ? (
        <form method="dialog" onSubmit={() => onConfirm(action)}>
          <p className="page-kicker">{siteIdentity().siteId.toUpperCase()} · environnement {siteIdentity().environment}</p>
          <h2 id="overview-action-dialog-title">{actionLabel(action)}</h2>
          <p>Le service vérifiera les droits et la capacité, puis confirmera l’effet réellement observé.</p>
          <div className="overview-action-dialog__actions">
            <button className="page-action" type="button" onClick={onCancel}>Annuler</button>
            <button className="page-action page-action--primary" type="submit">Confirmer</button>
          </div>
        </form>
      ) : null}
    </dialog>
  );
}

export function ActionOutcome({ receipt, failed }: {
  readonly receipt: PipelineActionReceipt | null;
  readonly failed: boolean;
}) {
  if (failed) {
    return <p className="overview-action-outcome overview-action-outcome--failed" role="status">Action non exécutée. Aucun état vérifiable n’a été fourni.</p>;
  }
  if (!receipt) return null;
  const copy = actionOutcomeCopy(receipt);
  return (
    <p className={`overview-action-outcome overview-action-outcome--${receipt.state}`} role="status">
      <strong>{copy}</strong>
      <span> · {formatUtc(receipt.createdAt)}</span>
    </p>
  );
}

export function actionOutcomeCopy(receipt: PipelineActionReceipt): string {
  if (receipt.state === 'succeeded') {
    if (receipt.action === 'prepare') return `Préparation terminée. Les ${sequences(siteIdentity().manifest.length)} tables sont prêtes à commencer leur copie initiale.`;
    if (receipt.action === 'start') return `Copie initiale lancée. Quadringent suit maintenant les ${sequences(siteIdentity().manifest.length)} tables.`;
    if (receipt.action === 'pause') return 'Exécution suspendue. Le dernier état reste conservé.';
    if (receipt.action === 'resume') return 'Reprise effectuée. La copie reprend depuis le dernier point enregistré.';
    return 'Actualisation terminée. Le nouvel état est visible.';
  }
  if (receipt.state === 'conflict') return 'Une action est déjà en cours sur ce flux.';
  if (receipt.state === 'unavailable') return 'Cette commande n’est pas encore disponible.';
  const code = receipt.stages.execution.code;
  if (code === 'identity_refused') return `Actualisation impossible : un accès Snowflake ${siteIdentity().environment} dédié doit être rétabli.`;
  if (code === 'connector_unavailable') return 'Actualisation impossible : le connecteur Snowflake n’est pas disponible.';
  if (code === 'query_failed') return 'Actualisation impossible : Snowflake n’a pas fourni de résultat exploitable.';
  return 'Actualisation impossible : le nouvel état n’a pas pu être confirmé.';
}
