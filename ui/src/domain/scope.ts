import type { Overview, Scope } from './controlPlane.ts';

export type SourceAvailability = 'available' | 'partial' | 'unavailable' | 'unestablished';

export interface SourceAvailabilityCopy {
  readonly label: string;
  readonly detail: string;
}

export function scopeLabel(scope: Scope | null | undefined): string {
  if (!scope || scope.kind === 'unavailable') return 'Non confirmé';
  if (scope.kind === 'mixed') {
    const environments = scope.environments
      .map((environment) => environment.toLocaleUpperCase('fr-FR'))
      .join(', ');
    return environments || 'Non confirmé';
  }
  return scope.environments[0]?.toLocaleUpperCase('fr-FR') ?? 'Non confirmé';
}

export function sourceAvailability(overview: Pick<Overview, 'sources'>): SourceAvailability {
  if (overview.sources.length === 0) return 'unestablished';
  const unavailable = overview.sources.filter((source) => source.status === 'unavailable').length;
  if (unavailable === 0) return 'available';
  if (unavailable === overview.sources.length) return 'unavailable';
  return 'partial';
}

export function sourceAvailabilityCopy(overview: Pick<Overview, 'sources'>): SourceAvailabilityCopy | null {
  switch (sourceAvailability(overview)) {
    case 'available':
      return null;
    case 'partial':
      return {
        label: 'Sources partiellement indisponibles',
        detail: 'Le relevé ne couvre pas toutes les sources : les nombres affichés ne prouvent pas l’exhaustivité.',
      };
    case 'unavailable':
      return {
        label: 'Sources indisponibles',
        detail: 'Le dernier relevé conservé reste visible, mais aucun état courant ni nombre exhaustif ne peut être confirmé.',
      };
    case 'unestablished':
      return {
        label: 'Disponibilité des sources non établie',
        detail: 'Les informations reçues ne permettent pas de confirmer la couverture du relevé.',
      };
  }
}
