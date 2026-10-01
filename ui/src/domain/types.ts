/** Contrat de données de la console.
 *
 *  Règle unique : rien n'entre dans l'écran sans dire d'où ça vient et quand
 *  ça a été mesuré. `Reading<T>` porte cette obligation dans le type — on ne
 *  peut pas afficher un chiffre nu, le compilateur l'interdit.
 *
 *  Les champs miroitent `CaptureMetrics.snapshot()`
 *  (src/quadringent/continuous.py) : mêmes noms, mêmes nullités. `lag_sequences`
 *  vaut `None` côté worker quand le retard n'est pas calculable ; ici il vaut
 *  `null` et s'affiche « inconnu ». Jamais zéro à sa place.
 */

export interface Provenance {
  /** Chemin, requête ou endpoint exact. Doit être vérifiable à la main. */
  readonly source: string;
  /** Instant de la mesure côté source, ISO 8601 avec fuseau. */
  readonly observedAt: string;
}

export interface Reading<T> extends Provenance {
  readonly value: T | null;
  /** Obligatoire quand `value` est `null`. Affiché tel quel à l'écran. */
  readonly unknownBecause?: string;
  /** La valeur est une borne inférieure, pas une mesure. Rendue « ≥ x ».
   *  Un relevé qui dit « 5 h et plus » ne doit pas s'afficher comme « 5 h ». */
  readonly atLeast?: boolean;
}

export function reading<T>(
  value: T,
  source: string,
  observedAt: string,
): Reading<T> {
  return { value, source, observedAt };
}

export function atLeast(
  value: number,
  source: string,
  observedAt: string,
): Reading<number> {
  return { value, source, observedAt, atLeast: true };
}

export function unknown<T>(
  reason: string,
  source: string,
  observedAt: string,
): Reading<T> {
  return { value: null, unknownBecause: reason, source, observedAt };
}

/* ------------------------------------------------------------------------ */
/* Retard                                                                     */
/* ------------------------------------------------------------------------ */

/** Verdicts de `continuous.lag_trend`. Mêmes littéraux que le Python. */
export type LagVerdict = 'BOUNDED' | 'CATCHING_UP' | 'DIVERGING' | 'INCONCLUSIVE';

/** Un point de la série de retard.
 *
 *  `measured` distingue un échantillon réellement relevé d'un point de forme
 *  connu par une propriété démontrée (« la montée est monotone », « le retard
 *  est resté à 1 »). Le graphe ne trace jamais un segment interpolé du même
 *  trait qu'un segment échantillonné.
 */
export interface LagPoint {
  /** Secondes depuis le début du run. */
  readonly t: number;
  readonly lag: number;
  readonly measured: boolean;
  /** Nature du segment qui relie le point précédent à celui-ci.
   *  `sampled`        — la trajectoire est échantillonnée, trait plein
   *  `known-constant` — la valeur est démontrée constante (min = max), trait plein
   *  `interpolated`   — on ne connaît que les deux bouts, pointillé
   *  Le graphe ne trace jamais un segment déduit du même trait qu'un segment
   *  mesuré. Par défaut : `interpolated`. */
  readonly edgeFromPrevious?: 'sampled' | 'known-constant' | 'interpolated';
  /** Ce que ce point prouve, en une clause. Affiché au survol. */
  readonly note?: string;
}

export interface LagSeries extends Provenance {
  readonly points: readonly LagPoint[];
  /** Nombre d'échantillons relevés par le worker sur la fenêtre. */
  readonly sampleCount: number;
  /** `true` si `points` contient tous les échantillons. Sinon le graphe le dit. */
  readonly complete: boolean;
  /** Pourquoi la série n'est pas complète. Obligatoire si `complete` est faux. */
  readonly incompleteBecause?: string;
}

export interface LagState {
  /** Retard courant, en séquences de journal. `null` = non calculable. */
  readonly current: Reading<number>;
  readonly verdict: Reading<LagVerdict>;
  /** Plancher du premier et du dernier tiers — le signal qui tranche. */
  readonly floorFirstThird: Reading<number>;
  readonly floorLastThird: Reading<number>;
  readonly max: Reading<number>;
  readonly series: LagSeries;
}

/* ------------------------------------------------------------------------ */
/* Position dans le journal                                                   */
/* ------------------------------------------------------------------------ */

export interface JournalPosition {
  readonly receiver: string;
  readonly sequence: number;
}

export interface PositionState {
  /** Checkpoint durable — ce qui est acquis. Avance après le raw, jamais avant. */
  readonly checkpoint: Reading<JournalPosition>;
  /** Tail de la source au dernier relevé du catalogue. */
  readonly sourceTail: Reading<JournalPosition>;
  /** Receiver actif et sa fenêtre de séquences. */
  readonly receiverFirstSequence: Reading<number>;
  readonly receiverLastSequence: Reading<number>;
}

/* ------------------------------------------------------------------------ */
/* Compteurs — miroir de CaptureMetrics                                       */
/* ------------------------------------------------------------------------ */

export interface Counters {
  readonly polls: Reading<number>;
  readonly errors: Reading<number>;
  readonly windowsPublished: Reading<number>;
  readonly eventsPublished: Reading<number>;
  /** Lignes réellement présentes dans la cible. Distinct des événements publiés
   *  sur le raw : c'est le chiffre qui prouve que la charge est arrivée. */
  readonly eventsInTarget: Reading<number>;
  readonly duplicatesInTarget: Reading<number>;
  readonly receiverRotations: Reading<number>;
  readonly meanMilliCpu: Reading<number>;
  readonly cpuMsPerEvent: Reading<number>;
  readonly runDurationS: Reading<number>;
}

/* ------------------------------------------------------------------------ */
/* Chronologie — la fiabilité visible                                         */
/* ------------------------------------------------------------------------ */

/** Nature d'un fait, telle que classée par les rapports de mesure.
 *  `outcome_kind` du worker : clean | defect | architectural | diverged. */
export type EventKind =
  | 'absorbed'    /* incident rattrapé sans perte : la preuve de solidité */
  | 'rotation'    /* franchissement de receiver */
  | 'recovery'    /* reprise après arrêt ou suppression du pod */
  | 'diverged'    /* le retard n'est jamais revenu au plancher */
  | 'defect';     /* défaut corrigé depuis, gardé pour la traçabilité */

export interface TimelineEntry extends Provenance {
  readonly id: string;
  readonly kind: EventKind;
  /** Une ligne, au vocabulaire du domaine. */
  readonly title: string;
  /** Le chiffre qui qualifie le fait, déjà formaté avec son unité. */
  readonly figure?: string;
  /** Ce qui s'est passé et comment ça s'est terminé. Deux phrases au plus. */
  readonly detail: string;
  readonly durationS?: number;
}

/* ------------------------------------------------------------------------ */
/* Flux                                                                       */
/* ------------------------------------------------------------------------ */

export type ReaderPath = 'RetrieveJournal' | 'DISPLAY_JOURNAL';

/** État d'exploitation du flux. Distinct du verdict de retard : un flux peut
 *  être arrêté avec un retard borné, ou en marche avec un retard qui diverge.
 *  Ces deux dimensions restent volontairement indépendantes. */
export type RunState =
  | 'RUNNING'
  | 'PAUSED_SOURCE'
  | 'STOPPED_FAIL_CLOSED'
  | 'STOPPED_AUTH_BLOCKED'
  | 'STOPPED_BUDGET'
  | 'UNKNOWN';

export interface Flux {
  readonly id: string;
  readonly label: string;
  readonly journal: string;
  readonly journalLibrary: string;
  readonly objects: readonly string[];
  readonly readerPath: ReaderPath;
  readonly target: string;
  /** Nom du Job qui a produit la mesure — l'ancre de vérification. */
  readonly job: string;
  readonly runState: Reading<RunState>;
  readonly runStartedAt: Reading<string>;
  readonly position: PositionState;
  readonly lag: LagState;
  readonly counters: Counters;
  readonly timeline: readonly TimelineEntry[];
  /** Ce que la mesure ne couvre pas. Affiché sur l'écran du flux, pas caché. */
  readonly caveats: readonly string[];
}

export interface ConsoleSnapshot {
  /** Instant où la console a lu la source. Distinct de `observedAt`. */
  readonly fetchedAt: string;
  readonly flux: readonly Flux[];
}
