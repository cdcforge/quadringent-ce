import type { Overview } from '../domain/controlPlane.ts';
import { sourceAvailabilityCopy } from '../domain/scope.ts';

export function SourceAvailabilityBanner({ overview }: { readonly overview: Pick<Overview, 'sources'> }) {
  const copy = sourceAvailabilityCopy(overview);
  if (!copy) return null;
  return (
    <aside className="simulation-banner source-availability-banner" aria-label={copy.label}>
      <strong>{copy.label}</strong>
      <span>{copy.detail}</span>
    </aside>
  );
}
