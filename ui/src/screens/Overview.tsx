/**
 * Accueil — l'état de toutes les liaisons, rien d'autre.
 *
 * L'écran répond à une seule question : est-ce que quelque chose demande mon
 * attention, et où ? Ce qui relève de la preuve, du détail par table ou de la
 * mesure vit dans le détail d'une liaison, pas ici.
 */

import { useEffect, useState } from 'react';
import type { ControlPlaneState } from '../data/useControlPlane.ts';
import type { ActionId, PipelineActionReceipt } from '../data/controlPlaneClient.ts';
import type { Overview as OverviewData } from '../domain/controlPlane.ts';
import { LiaisonCard } from '../components/LiaisonCard.tsx';
import { actionFailureFor, actionResultFor, boardSummary, byUrgency, liaisonView, type LiaisonView } from '../domain/operator.ts';
import { href } from '../router.ts';
import '../styles/liaison.css';

export function Overview({
  state,
  overview,
  onRefresh,
  onBusinessAction,
}: {
  state: ControlPlaneState;
  overview: OverviewData | null;
  onRefresh: () => void;
  onBusinessAction?: (pipelineId: string, action: ActionId) => Promise<PipelineActionReceipt>;
}) {
  const now = useNow();
  const [busy, setBusy] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const ready = state.status !== 'loading' && state.status !== 'failed' && overview !== null;
  const views: readonly LiaisonView[] = ready
    ? byUrgency(overview.pipelines.map((pipeline) => liaisonView(pipeline, now)))
    : [];

  return (
    <div className="board">
      <header className="board__head">
        <div>
          <h1 className="board__title">Vos liaisons</h1>
          <p className="board__summary">
            {ready ? boardSummary(views) : state.status === 'failed' ? 'État non lu' : 'Lecture en cours'}
          </p>
        </div>
        <button type="button" className="board__refresh" onClick={onRefresh} disabled={busy}>
          {state.status === 'failed' ? 'Réessayer' : 'Actualiser'}
        </button>
      </header>

      {actionNotice && <p role="status" className="liaison__guidance">{actionNotice}</p>}

      {!ready ? (
        <Unavailable state={state} />
      ) : views.length === 0 ? (
        <section className="board__empty">
          <h2>Aucune liaison pour l’instant</h2>
          <p>
            Reliez votre AS400 à Snowflake pour commencer. Le parcours prend quelques minutes et
            vous dit à chaque étape ce qu’il faut fournir.
          </p>
          <p>
            <a href={href({ name: 'setup' })}>Créer une liaison</a>
          </p>
        </section>
      ) : (
        <div className="board__list">
          {views.map((view) => (
            <LiaisonCard
              key={view.id}
              view={view}
              detailHref={href({ name: 'pipeline', id: view.id })}
              busy={busy}
              onAction={
                onBusinessAction
                  ? (action) => {
                      setBusy(true);
                      setActionNotice(null);
                      void onBusinessAction(view.id, action.id)
                        .then((receipt) => setActionNotice(actionResultFor(view.id, receipt)))
                        .catch(() => setActionNotice(actionFailureFor(view.id)))
                        .finally(() => setBusy(false));
                    }
                  : undefined
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Unavailable({ state }: { readonly state: ControlPlaneState }) {
  const loading = state.status === 'loading';
  return (
    <section className="board__empty">
      <h2>{loading ? 'Lecture en cours' : 'État non lu'}</h2>
      <p>
        {loading
          ? 'Quadringent interroge le service. L’état s’affichera dès la première réponse.'
          : 'Le service n’a pas répondu. Rien n’est affiché tant qu’un état n’est pas confirmé.'}
      </p>
    </section>
  );
}

/** Horloge d'écran — les durées affichées vieillissent sous les yeux. */
function useNow(tickMs = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), tickMs);
    return () => clearInterval(id);
  }, [tickMs]);
  return now;
}
