import { useEffect, useRef, useState } from 'react';
import { actionConfirmCopy } from '../../domain/operator.ts';

/**
 * ActionButton — un bouton d'action, avec confirmation obligatoire pour les
 * actions destructrices ou coûteuses (`requiresConfirmation`). Le texte de
 * confirmation vient de `operator.ts` (`actionConfirmCopy`), jamais codé en
 * dur ici. `disabled` reflète le contrat produit : un bouton n'existe que si
 * le service déclare la capacité `available`.
 */
export function ActionButton({
  label,
  onAction,
  disabled = false,
  requiresConfirmation = false,
  primary = false,
}: {
  readonly label: string;
  readonly onAction: () => void;
  readonly disabled?: boolean;
  readonly requiresConfirmation?: boolean;
  readonly primary?: boolean;
}) {
  const [pending, setPending] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (pending && !element.open) element.showModal();
    if (!pending && element.open) element.close();
  }, [pending]);

  const requestAction = () => {
    if (disabled) return;
    if (requiresConfirmation) {
      setPending(true);
      return;
    }
    onAction();
  };

  return (
    <>
      <button
        type="button"
        className={`action-button${primary ? ' action-button--primary' : ''}`}
        disabled={disabled}
        onClick={requestAction}
      >
        {label}
      </button>
      {requiresConfirmation ? (
        <dialog
          ref={dialog}
          className="action-button__confirm"
          onCancel={() => setPending(false)}
          aria-labelledby="action-button-confirm-title"
        >
          {pending ? (
            <form
              method="dialog"
              onSubmit={() => {
                setPending(false);
                onAction();
              }}
            >
              <p id="action-button-confirm-title">{actionConfirmCopy(label)}</p>
              <div className="action-button__confirm-actions">
                <button type="button" className="action-button" onClick={() => setPending(false)}>Annuler</button>
                <button type="submit" className="action-button action-button--primary">Confirmer</button>
              </div>
            </form>
          ) : null}
        </dialog>
      ) : null}
    </>
  );
}
