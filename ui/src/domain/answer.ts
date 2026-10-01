import type { Flux } from './types';

/** « Est-ce que ça avance ? »
 *
 *  La seule question à laquelle toute vue doit répondre sans qu'on aille voir
 *  ailleurs. La réponse est dérivée de deux faits tenus séparés — l'état
 *  d'exécution et la trajectoire du plancher — parce que les confondre est
 *  précisément ce qui produit un statut « live » à côté de 7,9 millions
 *  d'événements de retard.
 */
export interface Answer {
  /** Oui, non, ou l'aveu qu'on ne sait pas. Jamais un quatrième cas. */
  readonly verdict: 'oui' | 'non' | 'indéterminé';
  /** La clause qui justifie, en cinq mots. */
  readonly because: string;
  /** Ce qu'il y a à faire. Vide quand il n'y a rien à faire. */
  readonly next?: string;
}

export function advanceAnswer(flux: Flux): Answer {
  const run = flux.runState.value;
  const lag = flux.lag.current.value;
  const trend = flux.lag.verdict.value;

  if (run === 'STOPPED_FAIL_CLOSED') {
    return {
      verdict: 'non',
      because: 'arrêté fail-closed',
      next:
        'Le worker s’est arrêté plutôt que d’avancer sur un état partiel. Le ' +
        'checkpoint est intact : relancer reprend à la dernière séquence acquise.',
    };
  }
  if (run === 'STOPPED_AUTH_BLOCKED') {
    return {
      verdict: 'non',
      because: 'authentification source bloquée',
      next:
        'La source a refusé la connexion et la garde ne tente plus aucun ' +
        'sign-on : réparer le compte côté IBM i, puis réarmer la garde.',
    };
  }
  if (run === 'PAUSED_SOURCE') {
    return {
      verdict: 'non',
      because: 'source en pause',
      next:
        'La source ne répond pas (coupure ou maintenance). Aucune tentative ' +
        'n’est faite avant l’échéance ; la reprise est automatique depuis le checkpoint.',
    };
  }
  if (run === 'STOPPED_BUDGET') {
    return {
      verdict: 'non',
      because: 'arrêté, budget atteint',
      next: 'Relancer le Job. Le checkpoint reprend où il s’est arrêté.',
    };
  }
  if (run === 'UNKNOWN' || run === null) {
    return { verdict: 'indéterminé', because: 'état d’exécution non relevé' };
  }

  if (trend === 'DIVERGING') {
    return {
      verdict: 'non',
      because: 'le plancher monte',
      next:
        'Le retard ne revient plus au tail. Vérifier le débit de lecture contre ' +
        'la production du journal : une divergence tenue est un déficit de capacité, ' +
        'pas un incident passager.',
    };
  }
  if (trend === null || trend === 'INCONCLUSIVE') {
    return { verdict: 'indéterminé', because: 'tendance non calculable' };
  }
  if (lag === null) {
    return { verdict: 'indéterminé', because: 'retard non calculable' };
  }
  if (trend === 'CATCHING_UP') {
    return { verdict: 'oui', because: 'le retard se résorbe' };
  }

  // BOUNDED couvre deux régimes très différents, et le verdict seul ne les
  // sépare pas : le seuil de pente de `lag_trend` vaut 0,25 × le retard moyen,
  // si bien qu'une descente linéaire depuis un très gros retard rend BOUNDED,
  // pas CATCHING_UP. C'est le plancher qui tranche, alors on le lit.
  const floorFirst = flux.lag.floorFirstThird.value;
  const floorLast = flux.lag.floorLastThird.value;
  if (floorFirst !== null && floorLast !== null && floorLast < floorFirst) {
    return { verdict: 'oui', because: 'le plancher descend' };
  }

  return {
    verdict: 'oui',
    because: lag <= 1 ? 'au tail' : 'le retard revient au plancher',
  };
}
