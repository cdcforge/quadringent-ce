/**
 * Consommation — ce que la copie a coûté, pour qui la paie.
 *
 * Le service publie treize compteurs. Neuf décrivent la boucle interne du
 * lecteur : nombre de sondages, sondages sans résultat, balayages vides,
 * rotations de fichier, fenêtres publiées. Les traduire mot à mot — « lectures
 * à vide », « recherches à vide » — produit des libellés français qui restent
 * des mesures d'ingénieur : personne ne décide quoi que ce soit avec.
 *
 * Ce module tient donc une liste blanche. Un compteur n'atteint l'écran que
 * s'il répond à une question qu'un exploitant se pose : combien de données
 * ont transité, pendant combien de temps, pour quelle puissance machine. Les
 * autres restent dans la projection, disponibles pour qui débogue.
 */

import { unmeasuredCostsFor } from './operator.ts';
import type { Pipeline } from './controlPlane.ts';
import { decimal, formatDuration, sequences } from './format.ts';

export interface ConsumptionLine {
  readonly label: string;
  /** Valeur mise en forme, ou `null` quand le service ne publie pas la mesure. */
  readonly value: string | null;
  /** Unité ou précision, affichée à côté de la valeur. */
  readonly unit: string | null;
}

export interface ConsumptionView {
  readonly pipelineId: string;
  readonly lines: readonly ConsumptionLine[];
  /** Ce qui n'est pas mesuré du tout, dit sans détour. */
  readonly unmeasured: readonly string[];
}

/** Octets en unités lisibles. La précision suit la taille, jamais l'inverse. */
export function humanBytes(bytes: number): string {
  if (bytes < 1024) return `${sequences(bytes)} octets`;
  const units = ['Kio', 'Mio', 'Gio', 'Tio'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${decimal(value, value < 10 ? 1 : 0)} ${units[unit]}`;
}

/**
 * Les seules mesures qui remontent à l'écran.
 *
 * `mean_mcpu` est publié en millièmes de cœur : le rendre en cœurs évite à la
 * fois l'unité interne et le faux-ami « 201 » qui se lit comme 201 processeurs.
 */
export function consumptionFor(pipeline: Pipeline): ConsumptionView {
  const counters = pipeline.counters;
  const read = counters.events_published ?? null;
  const bytes = counters.payload_bytes_published ?? null;
  const duration = counters.run_duration_s ?? null;
  const mcpu = counters.mean_mcpu ?? null;
  const delivered = pipeline.destination?.canonicalRows ?? pipeline.destination?.rawRows ?? null;

  return {
    pipelineId: pipeline.id,
    lines: [
      {
        label: 'Enregistrements lus à la source',
        value: read === null ? null : sequences(read),
        unit: null,
      },
      {
        label: 'Lignes livrées dans Snowflake',
        value: delivered === null ? null : sequences(delivered),
        unit: null,
      },
      {
        label: 'Volume transféré',
        value: bytes === null ? null : humanBytes(bytes),
        unit: null,
      },
      {
        label: 'Temps de traitement',
        value: duration === null ? null : formatDuration(duration),
        unit: null,
      },
      {
        label: 'Puissance machine moyenne',
        value: mcpu === null ? null : decimal(mcpu / 1000, 2),
        unit: mcpu === null ? null : 'cœur',
      },
    ],
    unmeasured: unmeasuredCostsFor(pipeline),
  };
}

/**
 * Ce que le produit ne sait pas, dit avant qu'on le lui demande.
 *
 * L'écran s'appelait « Coûts » et promettait de suivre le coût des transferts
 * pour conclure, tout en bas, que le coût n'est pas mesuré. La promesse et sa
 * rétractation ne peuvent pas cohabiter : la limite se dit d'emblée.
 */

/** Vrai quand aucune mesure n'est disponible — l'écran le dit au lieu d'afficher un tableau vide. */
export function hasMeasures(view: ConsumptionView): boolean {
  return view.lines.some((line) => line.value !== null);
}
