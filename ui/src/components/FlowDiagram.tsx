import { Fragment, type ReactNode } from 'react';
import type { Pipeline, Stage, StageId } from '../domain/controlPlane.ts';
import { position, timestamp } from '../domain/format.ts';
import { incidentShortCause, type SectionView } from '../domain/liveBoard.ts';

/**
 * Diagramme de flux horizontal — l'objet visuel héro du plateau Sources.
 *
 * Cinq nœuds reliés : Source → Lecture → Réception → Copie → Destination.
 * L'état se lit en couleur, jamais en phrase :
 *   vert      = mesuré dans le relevé courant,
 *   ambre     = à vérifier (observation dégradée),
 *   rouge     = incident déclaré — un drapeau marque le nœud cassé,
 *   gris      = observée mais relevé non courant, ou pause,
 *   pointillé = jamais observée.
 *
 * Le trait entre deux nœuds est le flux : un point y coule quand le relevé
 * est frais, il est figé quand le relevé est ancien, et il s'arrête net
 * juste avant le nœud en incident — la cassure. La position du journal
 * est un repère sur le segment de lecture ; receiver:seq reste en
 * tooltip. Le détail de l'incident vit dans le <details> du drapeau.
 */

const NODE_LABELS: Readonly<Record<StageId, string>> = {
  source: 'Source',
  capture: 'Lecture',
  raw: 'Réception',
  load: 'Copie',
  destination: 'Destination',
};

type NodeTone = 'proven' | 'check' | 'down' | 'still' | 'off';

const NODE_STATE_SR: Readonly<Record<NodeTone, string>> = {
  proven: 'mesurée dans le relevé courant',
  check: 'à vérifier — observation dégradée',
  down: 'incident déclaré',
  still: 'observée — relevé non courant',
  off: 'jamais observée',
};

/** Drapeau « prête à reprendre » : ambre, pas rouge — la cause est résolue,
 *  il reste une décision, pas une panne. */
const READY_SR = 'prête à reprendre — cause résolue';

const STAGE_ORDER: readonly StageId[] = ['source', 'capture', 'raw', 'load', 'destination'];

interface FlowNode {
  readonly id: StageId;
  readonly label: string;
  readonly tone: NodeTone;
  readonly title: string;
  readonly flagged: boolean;
}

function nodeTone(stage: Stage | undefined, current: boolean): NodeTone {
  if (stage === undefined || stage.observedAt === null || stage.status === 'unknown') return 'off';
  if (stage.status === 'incident') return 'down';
  if (stage.status === 'degraded' || stage.status === 'awaiting_resume') return 'check';
  if (stage.status === 'planned_stop') return 'still';
  // healthy + observée : verte seulement si le relevé est courant.
  return current ? 'proven' : 'still';
}

function nodeTitle(stage: Stage | undefined): string {
  if (stage === undefined) return 'étape non projetée dans le relevé';
  const when = stage.observedAt === null ? 'jamais observée' : timestamp(stage.observedAt);
  return `${stage.headline} · ${when}`;
}

/** Trait entre deux nœuds : coule vers un nœud mesuré, s'arrête net avant
 *  un nœud en incident, en pointillés vers un nœud jamais observé. */
type EdgeTone = 'live' | 'still' | 'faint' | 'cut';

function edgeTone(next: NodeTone): EdgeTone {
  if (next === 'down') return 'cut';
  if (next === 'proven') return 'live';
  if (next === 'off') return 'faint';
  return 'still';
}

export function FlowDiagram({
  pipeline,
  view,
  flag,
}: {
  readonly pipeline: Pipeline;
  readonly view: SectionView;
  /** Corps de l'alerte incident — rendu dans le <details> du drapeau. */
  readonly flag: (() => ReactNode) | null;
}) {
  const flagStage: StageId | null =
    view.alert !== null && view.alert.severity === 'incident'
      ? view.alert.locus === 'Destination'
        ? 'destination'
        : 'capture'
      : null;

  const ready = view.alert?.ready === true;
  const nodes: FlowNode[] = STAGE_ORDER.map((id) => {
    const stage = pipeline.stages.find((item) => item.id === id);
    const flagged = flagStage === id;
    return {
      id,
      label: NODE_LABELS[id],
      // Le nœud porteur du drapeau est rouge quand l'incident est actif,
      // ambre quand la cause est résolue et qu'il reste à relancer.
      tone: flagged ? (ready ? 'check' : 'down') : nodeTone(stage, view.current),
      title: nodeTitle(stage),
      flagged,
    };
  });

  const railLabel = `Flux mesuré — ${nodes
    .map((node) => `${node.label} : ${NODE_STATE_SR[node.tone]}`)
    .join(', ')}`;

  return (
    <figure
      className={`flow${view.retained !== null ? ' flow--held' : ''}${view.current ? ' flow--live' : ''}`}
      aria-label={railLabel}
    >
      <ol className="flow__rail">
        {nodes.map((node, index) => (
          <Fragment key={node.id}>
            {index > 0 && (
              <li className={`flow__edge flow__edge--${edgeTone(node.tone)}`} aria-hidden="true">
                <i className="flow__line" />
                <i className="flow__runner" />
                {node.id === 'capture' && view.checkpoint !== null && (
                  <i
                    className="flow__tick"
                    title={`Position journal — ${position(view.checkpoint.receiver, view.checkpoint.sequence)}`}
                  />
                )}
              </li>
            )}
            <li
              className={`flow__node flow__node--${node.tone}${node.flagged ? ' flow__node--flagged' : ''}`}
              data-stage={node.id}
              title={node.title}
              aria-current={node.flagged ? 'step' : undefined}
            >
              <span className="flow__dot" aria-hidden="true">
                {node.flagged && <FlagIcon />}
              </span>
              <span className="flow__name" aria-hidden="true">
                {node.label}
              </span>
              <span className="sr-only">
                {`${node.label} : ${node.flagged && ready ? READY_SR : NODE_STATE_SR[node.tone]}`}
              </span>
              {node.flagged && flag !== null && (
                <details
                  className={`flow-flag${ready ? ' flow-flag--ready' : ''}${index >= 3 ? ' flow-flag--right' : index === 0 ? ' flow-flag--left' : ''}`}
                >
                  <summary>
                    <FlagIcon />
                    {pipeline.incident !== null && (
                      <span className="flow-flag__cause">
                        {ready ? 'prête à reprendre' : incidentShortCause(pipeline.incident.type)}
                      </span>
                    )}
                  </summary>
                  {flag()}
                </details>
              )}
            </li>
          </Fragment>
        ))}
      </ol>
    </figure>
  );
}

/** Petit drapeau planté sur le nœud cassé — la marque de l'incident. */
export function FlagIcon() {
  return (
    <svg className="flow-flag__icon" viewBox="0 0 10 12" aria-hidden="true" focusable="false">
      <path className="flow-flag__pole" d="M1.2 0.8v10.4" />
      <path className="flow-flag__cloth" d="M1.2 1.4h7.2L5.9 4.4l2.5 3H1.2z" />
    </svg>
  );
}
