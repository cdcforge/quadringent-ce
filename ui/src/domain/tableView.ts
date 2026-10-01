/**
 * Tables — l'état de copie d'une table, en langage d'exploitant.
 *
 * L'écran précédent exposait les valeurs AS/400 telles quelles : la colonne
 * « Images journal » affichait `*AFTER` ou `*BOTH`, la colonne « Identité »
 * affichait `RRN`. Ce sont des réglages de capture : ils décident de ce que le
 * lecteur sait reconstituer, mais ils ne disent pas à un exploitant si ses
 * données sont là. Ils restent dans l'onglet Mesures.
 *
 * Ici, une table a un état, un nombre de lignes, et rien d'autre.
 */

import type { FleetRuntimeTablePhase, FleetRuntimeTableState, Pipeline } from './controlPlane.ts';
import { sequences } from './format.ts';

export type TableState = 'copiee' | 'encours' | 'attente' | 'bloquee' | 'inconnue';

export interface TableRow {
  readonly name: string;
  readonly state: TableState;
  readonly stateLabel: string;
  /** Lignes copiées, mises en forme. `null` si la mesure n'est pas publiée. */
  readonly rows: string | null;
  /** Avancement de 0 à 1 pour les copies en cours. `null` sinon. */
  readonly progress: number | null;
}

/**
 * Les phases du service deviennent quatre états.
 *
 * Le service distingue `HISTORICAL`, `CATCHING_UP`, `LIVE`, `RECONCILING` :
 * quatre moments d'une même chose du point de vue de l'exploitant — la copie
 * travaille. `CERTIFIED` est le seul qui change sa décision : les données sont
 * là et vérifiées.
 */
export function tableStateFor(phase: FleetRuntimeTablePhase): { state: TableState; label: string } {
  switch (phase) {
    case 'CERTIFIED':
      return { state: 'copiee', label: 'Copiée' };
    case 'HISTORICAL':
    case 'CATCHING_UP':
    case 'LIVE':
    case 'RECONCILING':
      return { state: 'encours', label: 'Copie en cours' };
    case 'READY':
    case 'PREPARED':
    case 'NOT_PREPARED':
      return { state: 'attente', label: 'En attente' };
    case 'PAUSED':
      return { state: 'attente', label: 'En pause' };
    case 'BLOCKED':
      return { state: 'bloquee', label: 'Bloquée' };
    default:
      return { state: 'inconnue', label: 'État inconnu' };
  }
}

function rowFor(table: FleetRuntimeTableState): TableRow {
  const { state, label } = tableStateFor(table.phase);
  const copied = table.copiedRows ?? null;
  const total = table.totalRows ?? null;
  return {
    name: table.name,
    state,
    stateLabel: label,
    rows: copied === null ? null : sequences(copied),
    // L'avancement n'a de sens que pendant une copie : à 100 % il ne dit rien
    // de plus que l'état, et une barre pleine sur chaque ligne fait du bruit.
    progress:
      state === 'encours' && copied !== null && total !== null && total > 0
        ? Math.min(1, copied / total)
        : null,
  };
}

/** Les tables d'une liaison, problèmes d'abord, puis par ordre alphabétique. */
export function tableRows(pipeline: Pipeline): readonly TableRow[] {
  const states = pipeline.fleetRuntime?.tableStates ?? [];
  const rank: Readonly<Record<TableState, number>> = {
    bloquee: 0,
    inconnue: 1,
    attente: 2,
    encours: 3,
    copiee: 4,
  };
  return states
    .map(rowFor)
    .sort((a, b) => rank[a.state] - rank[b.state] || a.name.localeCompare(b.name, 'fr'));
}

/** Résumé d'en-tête : ce que l'opérateur lit avant de parcourir la liste. */
export function tablesSummary(rows: readonly TableRow[]): string {
  if (rows.length === 0) return 'Aucune table déclarée';
  const copied = rows.filter((row) => row.state === 'copiee').length;
  const problems = rows.filter((row) => row.state === 'bloquee').length;
  if (problems > 0) {
    return problems === 1
      ? `1 table bloquée sur ${sequences(rows.length)}`
      : `${sequences(problems)} tables bloquées sur ${sequences(rows.length)}`;
  }
  if (copied === rows.length) {
    return rows.length === 1 ? '1 table copiée' : `${sequences(rows.length)} tables copiées`;
  }
  return `${sequences(copied)} tables copiées sur ${sequences(rows.length)}`;
}
