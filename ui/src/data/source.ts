import type { ConsoleSnapshot } from '../domain/types';
import { FIXTURE } from './fixtures/soak-2026-08-27.ts';

/** D'où la console tire son état.
 *
 *  Un seul point d'entrée, pour qu'il n'y ait jamais de doute sur qui porte
 *  l'état réel — c'est exactement ce qui manque quand la configuration est
 *  éclatée entre source, modèle, souscription et cible.
 */
export interface MetricsSource {
  readonly kind: 'fixture' | 'live';
  /** Ce que l'écran affiche en permanence en haut. Doit être vrai. */
  readonly label: string;
  read(): Promise<ConsoleSnapshot>;
}

export interface SourceEnvironment {
  readonly DEV: boolean;
  readonly VITE_USE_FIXTURE?: string;
  readonly VITE_CONTROL_PLANE_URL?: string;
}

export function configuredControlPlaneUrl(environment: SourceEnvironment = import.meta.env): string {
  const value = environment.VITE_CONTROL_PLANE_URL?.trim();
  if (!value) throw new Error('VITE_CONTROL_PLANE_URL is required');
  return value.replace(/\/$/, '');
}

const developmentFixtureSource: MetricsSource = {
  kind: 'fixture',
  label:
    'Fixture synthétique vide — aucune mesure runtime. L’adaptateur temps réel ' +
    'n’est pas branché.',
  async read() {
    return FIXTURE;
  },
};

/** La fixture est une opt-in locale : elle ne doit jamais devenir une donnée prod. */
export function configuredSource(
  environment: SourceEnvironment = import.meta.env,
): MetricsSource {
  if (environment.DEV && environment.VITE_USE_FIXTURE === 'true') {
    return developmentFixtureSource;
  }
  return liveSource(configuredControlPlaneUrl(environment));
}

/** Adaptateur temps réel — DÉLIBÉRÉMENT NON IMPLÉMENTÉ.
 *
 *  Il n'existe pas de surface HTTP à interroger : le worker écrit
 *  `CaptureMetrics.snapshot()` en JSON sur stdout, poll par poll. Brancher la
 *  console demande d'abord une décision côté worker, pas côté écran :
 *
 *    a. un endpoint `/metrics/snapshot` servant le dernier snapshot, ou
 *    b. un objet S3 réécrit à chaque poll et lu par la console, ou
 *    c. une table Snowflake alimentée par le même chemin que les événements.
 *
 *  (b) est cohérent avec le reste : le raw est déjà sur S3, le coût S3 est un
 *  coût d'opérations et un PUT par poll reste dans le même ordre de grandeur
 *  qu'un PUT par lot. Tant que ce n'est pas tranché, la console dit qu'elle lit
 *  une fixture plutôt que de faire semblant.
 */
export function liveSource(_endpoint: string): MetricsSource {
  throw new Error(
    'Adaptateur temps réel non implémenté : aucune surface de métriques n’est ' +
      'exposée par le worker. Voir le commentaire de liveSource dans data/source.ts.',
  );
}
