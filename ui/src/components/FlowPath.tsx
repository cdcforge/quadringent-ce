import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import { stageLabel } from '../domain/pipelineDetail.ts';
import { flowEvidenceClass, flowMotionClass, flowPathAccessibleLabel } from '../domain/pipelineView.ts';

const compactLabels: Readonly<Record<StageId, string>> = {
  source: 'Src', capture: 'Lect', raw: 'Récep', load: 'Copie', destination: 'Dest',
};

export function FlowPath({ pipeline, compact = false }: { readonly pipeline: Pipeline; readonly compact?: boolean }) {
  return (
    <ol className={`${flowEvidenceClass(pipeline)} ${flowMotionClass(pipeline)}${compact ? ' flow-path--compact' : ''}`} aria-label={flowPathAccessibleLabel(pipeline)}>
      {pipeline.stages.map((stage, index) => (
        <li key={stage.id} className={`flow-path__stage flow-path__stage--${stage.status}`}>
          <span className="flow-path__marker" aria-hidden="true" />
          <span>{compact ? compactLabels[stage.id] : stageLabel(stage.id)}</span>
          {index < pipeline.stages.length - 1 ? <span className="flow-path__arrow" aria-hidden="true">→</span> : null}
        </li>
      ))}
    </ol>
  );
}
