import type { PipelineStatus } from '../domain/controlPlane.ts';
import { statusCopy } from '../router.ts';

export function StatusBadge({ status }: { readonly status: PipelineStatus }) {
  const copy = statusCopy(status);

  return (
    <span className={`status-badge status-badge--${status}`}>
      <span aria-hidden="true" className="status-badge__shape" />
      <span className="status-badge__label">{copy.label}</span>
      <span className="sr-only">{copy.description}</span>
    </span>
  );
}
