import type { Pipeline } from '../domain/controlPlane.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';
import { href } from '../router.ts';
import {
  failClosedFrontier,
  isDocumentaryProof,
  proofStationKicker,
  proofStations,
} from './proofStations.ts';

export type PipelineProofGroup = 'broken' | 'stale' | 'established';

export function pipelineProofGroup(focus: ProofFocus): PipelineProofGroup {
  if (focus.proofScope.kind === 'stale' || focus.proofScope.kind === 'cached') return 'stale';
  return focus.firstBreak ? 'broken' : 'established';
}

export function PipelineRow({
  pipeline,
  focus,
  primary = false,
}: {
  readonly pipeline: Pipeline;
  readonly focus: ProofFocus;
  readonly primary?: boolean;
}) {
  const focusStation = proofStations(focus, pipeline).find((station) => station.isFocus);
  const kicker = focusStation ? proofStationKicker(focusStation) : 'Première preuve non confirmable';
  const aval = focus.firstBreak
    ? `Aval ${focus.downstream?.label ?? 'Destination Snowflake'} · non confirmable`
    : null;
  const action = primary && focus.cta.kind === 'link'
    ? <a className="page-action page-action--primary" href={focus.cta.href}>{focus.cta.label}</a>
    : <span>{focus.cta.label}</span>;

  return (
    <article className={entryClassName(focus)} data-focus-stage={failClosedFrontier(focus.focus.stage)}>
      <p className="pipeline-entry__stamp">{focus.firstBreak ? 'Rupture' : 'Établissement'} · {focus.focus.label}</p>
      <p className="pipeline-entry__identity pipeline-entry__id">
        <a href={href({ name: 'pipeline', id: pipeline.id })}>{pipeline.id}</a>
      </p>
      <p className="pipeline-entry__scope">{pipeline.environment.toLocaleUpperCase('fr-FR')} · {focus.proofScope.label}</p>
      <p className="pipeline-entry__action">
        <span>{kicker}</span>
        {action}
        {aval ? <span>{aval}</span> : null}
      </p>
    </article>
  );
}

function entryClassName(focus: ProofFocus): string {
  const group = pipelineProofGroup(focus);
  const documentary = isDocumentaryProof(focus);
  return `pipeline-entry pipeline-entry--${group}${documentary ? ' pipeline-entry--documentary' : ''}`;
}
