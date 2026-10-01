import { useCallback, useRef } from 'react';
import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';
import { resolveSelectedStage, restoreSelectedStageFocus } from '../domain/pipelineDetail.ts';
import { href } from '../router.ts';
import { ProofChain } from './ProofChain.tsx';
import { StageDrawer } from './StageDrawer.tsx';

const observationTimeFormat = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
});

export function PipelineTopology({
  pipeline,
  focus,
  inspect = null,
}: {
  readonly pipeline: Pipeline;
  readonly focus: ProofFocus;
  readonly inspect?: StageId | null;
}) {
  const selectedId = inspect;
  const stageButtons = useRef(new Map<StageId, HTMLButtonElement>());
  const selected = resolveSelectedStage(pipeline, selectedId);

  const closeDrawer = useCallback(() => {
    const id = selectedId;
    window.location.hash = href({ name: 'pipeline', id: pipeline.id, tab: 'overview' });
    restoreSelectedStageFocus(stageButtons.current, id);
  }, [pipeline.id, selectedId]);

  const openStage = useCallback((stage: StageId) => {
    window.location.hash = href({ name: 'pipeline', id: pipeline.id, tab: 'overview', inspect: stage });
  }, [pipeline.id]);

  const buttonRef = (stage: StageId) => (node: HTMLButtonElement | null) => {
    if (node) stageButtons.current.set(stage, node);
    else stageButtons.current.delete(stage);
  };

  return (
    <section className="pipeline-topology" aria-labelledby="proof-object-title">
      <div className="proof-object__meta">
        <p className="section-kicker">Point de preuve</p>
        <p>
          {focus.timestamp.label}
          {focus.timestamp.value ? <> · <time dateTime={focus.timestamp.value}>{observationTimeFormat.format(new Date(focus.timestamp.value))} UTC</time></> : null}
        </p>
      </div>

      <ProofChain
        proof={focus}
        variant="detail"
        pipeline={pipeline}
        selectedId={selectedId}
        onSelectStage={openStage}
        stageButtonRef={buttonRef}
      />

      {selected ? (
        <StageDrawer
          pipeline={pipeline}
          stage={selected}
          current={focus.proofScope.kind === 'live'}
          proofScope={focus.proofScope}
          onClose={closeDrawer}
        />
      ) : null}
    </section>
  );
}
