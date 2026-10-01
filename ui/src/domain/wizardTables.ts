/**
 * Assistant v2 — étape « Tables » (docs/plans/2026-09-23-control-plane-v2-contract.md §2.3).
 *
 * `readiness` et `key_strategy` sont déjà calculés côté serveur
 * (`GET /v2/sources/{id}/tables`, `services/tables.py::TableRecord.to_dict`
 * / `quadringent.table_discovery.classify_table`) : cet écran ne recalcule
 * jamais une disponibilité lui-même, il traduit ce que le service renvoie en
 * un mot lisible (« prête / non journalisée / images incomplètes / sans
 * clé / journal incohérent »), à la manière de StatusWord.
 */

export type TableReadiness = 'ready' | 'not_journaled' | 'images_incomplete' | 'no_key' | 'journal_mismatch';
export type KeyStrategy = 'primary' | 'unique_index' | 'rrn';

export interface DiscoveredTable {
  readonly id: string;
  readonly library: string;
  readonly name: string;
  readonly approxRowCount: number;
  readonly approxSizeBytes: number;
  readonly readiness: TableReadiness;
  readonly keyStrategy: KeyStrategy;
  /** Colonnes de la clé déjà connue (index unique détecté par la
   *  découverte, ou choisie par un précédent `PATCH`). Vide tant qu'aucune
   *  clé n'a été trouvée ni déclarée. */
  readonly keyColumns: readonly string[];
  /** Commandes CL suggérées pour lever le blocage (journal absent/incomplet). Vide si rien à corriger. */
  readonly clFixCommands: readonly string[];
}

export interface TableReadinessCopy {
  readonly label: string;
  readonly description: string;
}

const READINESS_COPY: Readonly<Record<TableReadiness, TableReadinessCopy>> = {
  ready: {
    label: 'Prête',
    description: 'Le journal couvre les images avant/après et une clé exploitable est disponible.',
  },
  not_journaled: {
    label: 'Non journalisée',
    description: 'Le journal IBM i est obligatoire pour capturer cette table : il est absent.',
  },
  images_incomplete: {
    label: 'Images incomplètes',
    description: 'Le journal existe mais ne conserve pas les images avant/après nécessaires à la capture.',
  },
  no_key: {
    label: 'Sans clé',
    description: 'Aucune clé unique n’est déclarée : une clé doit être choisie avant de démarrer.',
  },
  journal_mismatch: {
    label: 'Journal incohérent',
    description: 'Le journal déclaré ne correspond pas à celui détecté sur l’IBM i : à vérifier avant de démarrer.',
  },
};

/** Traduction directe de l'état serveur — jamais recalculée. */
export function evaluateTableReadiness(table: DiscoveredTable): TableReadiness {
  return table.readiness;
}

export function tableReadinessCopy(state: TableReadiness): TableReadinessCopy {
  return READINESS_COPY[state];
}

export function requiresKeyChoice(table: DiscoveredTable): boolean {
  return table.readiness === 'no_key';
}

/**
 * Choisir la RRN (position physique) comme clé de remplacement a une
 * conséquence explicite sur la reprise (docs §294 : "sans clé métier :
 * identifiées par leur position physique (RRN)") — elle doit être
 * acquittée, jamais cochée par défaut.
 */
export function requiresRrnAcknowledgement(chosenKey: KeyStrategy): boolean {
  return chosenKey === 'rrn';
}

export const RRN_CONSEQUENCE_SENTENCE =
  'Sans clé métier, les lignes sont repérées par leur position physique (RRN) : une réorganisation de la table côté IBM i peut fausser le suivi des modifications.';

/**
 * ``choice`` porte la stratégie choisie par l'opérateur pour une table
 * ``no_key`` (jamais pour une table déjà prête). ``columns`` est requis et
 * non vide pour ``unique_index`` (``services/tables.py::choose_key`` rejette
 * sinon), ``rrnAcknowledged`` requis pour ``rrn``.
 */
export function isTableStartable(
  table: DiscoveredTable,
  choice?: { readonly key: Exclude<KeyStrategy, 'primary'>; readonly columns: readonly string[]; readonly rrnAcknowledged: boolean },
): boolean {
  const readiness = evaluateTableReadiness(table);
  if (readiness === 'not_journaled' || readiness === 'images_incomplete' || readiness === 'journal_mismatch') return false;
  if (readiness === 'no_key') {
    if (!choice) return false;
    if (choice.key === 'unique_index') return choice.columns.length > 0;
    if (choice.key === 'rrn') return choice.rrnAcknowledged;
    return false;
  }
  return true;
}
