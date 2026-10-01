import type { ObservationQuality as Quality } from '../domain/controlPlane.ts';

const coverage = {
  complete: 'Couverture complète',
  partial: 'Couverture partielle',
  gap: 'Trou de télémétrie',
  none: 'Aucune couverture',
} as const;

const freshness = {
  late: 'Observation tardive',
  stale: 'Observation périmée',
  clock_untrusted: 'Horloge non fiable',
} as const;

const evidence = {
  live: 'Système réel',
  historical: 'Preuve historique',
  simulation: 'Simulation',
} as const;

export function coverageLabel(quality: Quality): string {
  return coverage[quality.coverage];
}

export function evidenceLabel(kind: Quality['evidenceKind'] | Quality): string {
  return evidence[typeof kind === 'string' ? kind : kind.evidenceKind];
}

export function freshnessLabel(quality: Quality): string {
  if (quality.freshness !== 'fresh') return freshness[quality.freshness];
  return freshObservationLabel(quality);
}

export function registerFreshnessLabel(quality: Quality): string {
  switch (quality.freshness) {
    case 'stale': return 'Observation périmée';
    case 'late': return 'Observation tardive';
    case 'clock_untrusted': return 'Horloge non fiable';
    case 'fresh':
      if (quality.evidenceKind === 'historical') return 'Fraîche au snapshot';
      if (quality.evidenceKind === 'simulation') return 'Fraîche dans la simulation';
      return 'Observation fraîche';
  }
}

export function proofScopeNamesFreshness(label: string): boolean {
  return /périmé|tardive|horloge non fiable/i.test(label);
}

export function ObservationQuality({ quality }: { readonly quality: Quality }) {
  return (
    <dl className="observation-quality" aria-label="Qualité de l’observation">
      <div className="observation-quality__item">
        <dt>Couverture</dt>
        <dd>{coverageLabel(quality)}</dd>
      </div>
      <div className="observation-quality__item">
        <dt>Fraîcheur</dt>
        <dd>{freshnessLabel(quality)}</dd>
      </div>
      <div className="observation-quality__item">
        <dt>Provenance</dt>
        <dd>{evidenceLabel(quality)}</dd>
      </div>
    </dl>
  );
}

function freshObservationLabel(quality: Quality): string {
  if (quality.evidenceKind === 'historical') return 'Fraîche au moment du snapshot historique · expiration actuelle non établie';
  if (quality.evidenceKind === 'simulation') return 'Fraîche dans le scénario simulé · fraîcheur live non établie';
  return 'Observation fraîche';
}
