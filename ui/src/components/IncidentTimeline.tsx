import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import { buildIncidentContext, stageLabel } from '../domain/pipelineDetail.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';
import { failClosedFrontier } from './proofStations.ts';

export function IncidentTimeline({ pipeline, current = true, proofScope, focusStage = null }: {
  readonly pipeline: Pipeline;
  readonly current?: boolean;
  readonly proofScope?: ProofFocus['proofScope'];
  readonly focusStage?: StageId | null;
}) {
  const context = buildIncidentContext(pipeline);
  const retained = proofScope?.kind === 'cached';
  const recovering = pipeline.status === 'recovering';
  const activeIncident = current && pipeline.status === 'incident';
  const frontier = failClosedFrontier(focusStage);
  if (context) {
    return (
      <section className="incident-context" aria-labelledby="incident-context-title">
        <div className="section-head">
          <div>
            <p className="section-kicker">{recovering ? 'Reprise observée' : activeIncident ? 'Incident courant' : retained ? 'Incident conservé' : 'Incident observé'}</p>
            <h2 className="section-title" id="incident-context-title">{context.publicLabel}</h2>
          </div>
          {activeIncident ? <span className="incident-context__active">Actif</span> : recovering ? <span className="incident-context__recovery">Rétablissement à confirmer</span> : null}
        </div>
        <dl className="incident-context__facts">
          <div><dt>Explication sûre</dt><dd>{context.safeExplanation}</dd></div>
          <div><dt>Étape affectée</dt><dd>{context.affectedStage}</dd></div>
          <div><dt>Première observation</dt><dd>Non publiée par l’API</dd></div>
          <div><dt>Dernière observation</dt><dd><time dateTime={context.lastObservedAt}>{context.lastObservedAt}</time></dd></div>
          <div><dt>{activeIncident ? 'Action recommandée' : 'Vérification sûre'}</dt><dd>{activeIncident ? 'Aucune action sûre automatisée ; vérifier la connectivité et les journaux protégés du data plane.' : 'Aucune action opérateur actuelle n’est établie ; vérifier la connectivité et les journaux protégés avant toute décision.'}</dd></div>
        </dl>
        {recovering ? (
          <p className="incident-context__derived">Reprise observée · rétablissement à confirmer. Le signal incident reste une observation tant que la reprise n’est pas établie.</p>
        ) : !activeIncident ? (
          <p className="incident-context__derived">Portée · {proofScope?.label ?? 'Observation non courante'}. Ce constat ne décrit pas une situation opérateur actuelle.</p>
        ) : null}
        <p className="incident-context__history">Historique des incidents passés non publié par l’API.</p>
      </section>
    );
  }

  const emptyTitle = recovering
    ? 'Rétablissement à confirmer'
    : current
      ? 'Aucun incident structuré courant'
      : retained
        ? 'Aucun incident structuré dans le snapshot conservé'
        : 'Aucun incident structuré observé';

  return (
    <section className="incident-context incident-context--register" aria-labelledby="incident-context-title">
      <p className="section-kicker">{recovering ? 'Reprise observée' : 'Contexte incident'}</p>
      <h2 className="section-title" id="incident-context-title">{emptyTitle}</h2>
      <p className="incident-context__register-fact">
        Frontière de preuve · {stageLabel(frontier)}. Ce constat n’est pas un incident structuré.
      </p>
      {pipeline.status === 'incident' ? (
        <p className="incident-context__derived"><strong>signal déduit</strong> — le verdict est Incident, sans objet incident public associé.</p>
      ) : null}
      {recovering ? (
        <p className="incident-context__derived">La reprise est observée dans ce snapshot ; son achèvement reste à confirmer.</p>
      ) : !current ? <p className="incident-context__derived">Portée · {proofScope?.label ?? 'Observation non courante'}. Cette observation ne prescrit aucune action opérateur.</p> : null}
      <p className="incident-context__history">Historique des incidents passés non publié par l’API.</p>
    </section>
  );
}
