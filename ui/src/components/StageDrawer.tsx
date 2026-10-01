import { useEffect, useId, useRef } from 'react';
import type { Pipeline, Stage } from '../domain/controlPlane.ts';
import { buildStageProof, stageLabel } from '../domain/pipelineDetail.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';

const focusableSelector = 'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function StageDrawer({ pipeline, stage, current, proofScope, onClose }: {
  readonly pipeline: Pipeline;
  readonly stage: Stage;
  readonly current: boolean;
  readonly proofScope: ProofFocus['proofScope'];
  readonly onClose: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const proof = buildStageProof(stage, pipeline);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [])];
      if (!focusable.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      if (event.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose]);

  return (
    <div className="stage-dialog-layer">
      <button className="stage-dialog__backdrop" type="button" tabIndex={-1} aria-label="Fermer la preuve d’étape" onClick={onClose} />
      <aside ref={dialogRef} className="stage-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}>
        <div className="stage-drawer__head">
          <div>
            <p className="section-kicker">Preuve d’étape</p>
            <h2 className="stage-drawer__title" id={titleId}>{stageLabel(stage.id)}</h2>
          </div>
          <button ref={closeRef} className="stage-drawer__close" type="button" onClick={onClose}>Fermer</button>
        </div>
        <p className={`stage-drawer__status stage-drawer__status--${stage.status}`}>
          {current ? proof.statusLabel : proofScope.kind === 'cached' ? 'Observation conservée' : 'Observation non courante'}
        </p>
        <dl className="stage-drawer__facts">
          <div><dt>{current ? 'Constat' : 'Observation brute'}</dt><dd>{current ? stage.headline : <q>{stage.headline}</q>}</dd></div>
          <div><dt>{current ? 'Détail sûr' : 'Détail brut'}</dt><dd>{current ? stage.detail : <q>{stage.detail}</q>}</dd></div>
          <div><dt>Dernière observation</dt><dd>{stage.observedAt ? <time dateTime={stage.observedAt}>{formatTimestamp(stage.observedAt)}</time> : 'Non observée'}</dd></div>
          <div><dt>Provenance</dt><dd>{proof.provenance}{current ? null : ` · Portée : ${proofScope.label} · non courante`}</dd></div>
        </dl>
        <p className="stage-drawer__limit" id={descriptionId}>
          {current
            ? 'Le snapshot v1 expose un constat agrégé. Les métriques et dépendances absentes restent non mesurées.'
            : `Portée non courante · ${proofScope.label}. Cette observation brute décrit le snapshot reçu et ne qualifie pas l’état courant.`}
        </p>
      </aside>
    </div>
  );
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' }).format(new Date(value));
}
