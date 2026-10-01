/**
 * La carte d'une liaison — l'unité de lecture du produit.
 *
 * Elle se lit de haut en bas dans l'ordre des questions de l'opérateur :
 * un point de couleur et un mot disent l'état, une phrase dit ce qui se passe,
 * un bloc dit quoi faire, trois chiffres disent où on en est.
 *
 * Elle n'affiche un bouton que pour une action que le service a déclarée
 * disponible ; la consigne et les boutons s'excluent, jamais les deux à la
 * fois — un ordre doublé d'un bouton grisé est le pire des deux mondes.
 */

import { useState } from 'react';
import type { ActionOffer, LiaisonView } from '../domain/operator.ts';
import { factValue } from '../domain/operator.ts';

export function LiaisonCard({
  view,
  detailHref,
  onAction,
  busy = false,
  compact = false,
}: {
  readonly view: LiaisonView;
  readonly detailHref?: string;
  readonly onAction?: (action: ActionOffer) => void;
  readonly busy?: boolean;
  /** Sur le détail, l'identité est déjà dans le titre de page : la répéter
   *  ici ferait lire deux fois le même fait. */
  readonly compact?: boolean;
}) {
  const titleId = `liaison-${view.id}`;
  // Une action de pilotage engage la source et la destination — lancer une
  // première copie, relancer une lecture. Elle passe par une confirmation
  // explicite : un clic de trop ne doit pas déclencher un tel travail.
  const [pending, setPending] = useState<ActionOffer | null>(null);
  return (
    <article
      className={`liaison liaison--${view.health}${compact ? ' liaison--compact' : ''}`}
      aria-labelledby={compact ? undefined : titleId}
      aria-label={compact ? `État de ${view.name}` : undefined}
    >
      {!compact && (
      <header className="liaison__head">
        <span className="liaison__dot" aria-hidden="true" />
        <div className="liaison__identity">
          <h2 className="liaison__name" id={titleId}>
            {detailHref ? <a href={detailHref}>{view.name}</a> : view.name}
          </h2>
          {view.destination !== null && (
            <p className="liaison__destination">Vers {view.destination}</p>
          )}
        </div>
        <span className={`liaison__state liaison__state--${view.health}`}>{view.stateLabel}</span>
      </header>
      )}

      {view.caveat !== null && (
        <p className="liaison__caveat" role="note">{view.caveat}</p>
      )}

      <p className="liaison__headline">{view.headline}</p>

      {view.actions.length > 0 ? (
        pending !== null ? (
          <div className="liaison__confirm" role="alertdialog" aria-label={`Confirmer : ${pending.label}`}>
            <p className="liaison__confirm-question">{pending.label} ?</p>
            <p className="liaison__confirm-effect">{pending.effect}</p>
            <div className="liaison__confirm-actions">
              <button type="button" className="liaison__cancel" onClick={() => setPending(null)}>
                Annuler
              </button>
              <button
                type="button"
                className="liaison__action"
                disabled={busy}
                onClick={() => {
                  const action = pending;
                  setPending(null);
                  onAction?.(action);
                }}
              >
                Confirmer
              </button>
            </div>
          </div>
        ) : (
          <div className="liaison__actions">
            {view.actions.map((action) => (
              <button
                key={action.id}
                type="button"
                className="liaison__action"
                disabled={busy || onAction === undefined}
                onClick={() => setPending(action)}
              >
                {action.label}
              </button>
            ))}
            <span className="liaison__effect">{view.actions[0]?.effect}</span>
          </div>
        )
      ) : view.guidance !== null ? (
        <p className="liaison__guidance">{view.guidance}</p>
      ) : null}

      <dl className="liaison__facts">
        {view.facts.map((fact) => (
          <div className="liaison__fact" key={fact.label}>
            <dt>{fact.label}</dt>
            <dd className={fact.value === null ? 'liaison__fact-absent' : undefined} data-numeric="">
              {factValue(fact)}
            </dd>
            {fact.note !== undefined && <p className="liaison__fact-note">{fact.note}</p>}
          </div>
        ))}
      </dl>

      <footer className="liaison__foot">
        <span className={view.outdated ? 'liaison__freshness liaison__freshness--old' : 'liaison__freshness'}>
          {view.freshness}
        </span>
        {detailHref && (
          <a className="liaison__more" href={detailHref}>
            Voir le détail
          </a>
        )}
      </footer>
    </article>
  );
}
