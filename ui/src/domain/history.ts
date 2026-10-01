/**
 * Journal — ce qui s'est passé, en phrases.
 *
 * Le journal précédent publiait la taxonomie interne sur chaque ligne : un
 * type (`SESSION`, `ÉTAPE`, `CATALOGUE`, `RELEVÉ`), un verdict
 * (« constaté · figé ») et une origine (`MESURÉ` / `DÉDUIT`). Trois colonnes
 * qui décrivent la façon dont le produit sait, non ce qu'il s'est passé. Un
 * exploitant n'a rien à en faire : il veut savoir ce qui est arrivé, quand, et
 * si c'est encore vrai.
 *
 * Ce module garde donc les événements qui ont une conséquence, et abandonne
 * ceux qui ne racontent que la vie du client — l'arrivée d'un relevé, l'ouverture
 * d'une session d'écran.
 */

import type { ActivityEntry } from './activityJournal.ts';
import type { Pipeline } from './controlPlane.ts';
import { sloCheckLabel, sloSignalLabel, sloValueLabel } from './observability.ts';

export type HistoryTone = 'attention' | 'active' | 'muted';

export interface HistoryLine {
  readonly at: string;
  /** Ce qui s'est passé, en une phrase. */
  readonly label: string;
  /** Précision facultative. `null` quand elle répéterait le libellé. */
  readonly detail: string | null;
  readonly tone: HistoryTone;
}

/**
 * Événements qui ne décrivent que le fonctionnement du client, pas la liaison.
 *
 * « Premier relevé de la session » date de l'ouverture de l'écran : rejouer
 * la même page demain produirait la même ligne à une autre heure. Ce n'est pas
 * un fait de la liaison, donc pas une ligne de journal.
 */
const CLIENT_ONLY_TYPES = new Set(['Session', 'Relevé', 'Statut']);

/** Vrai quand l'entrée raconte la liaison, pas l'écran qui la regarde. */
export function isMeaningful(entry: ActivityEntry): boolean {
  return !CLIENT_ONLY_TYPES.has(entry.type);
}

/**
 * Traduit une entrée en ligne lisible.
 *
 * Le préfixe de type est retiré du libellé quand il s'y trouve : « Capture —
 * Prête à reprendre » se lit « Prête à reprendre », le reste étant porté par
 * la couleur et la date.
 */
export function historyLine(entry: ActivityEntry): HistoryLine {
  const label = entry.label.replace(/^[^—]+ — /, '').trim();
  return {
    at: entry.at,
    label: plainSpeech(label.length > 0 ? label : entry.label),
    detail: entry.detail === null ? null : plainDetail(entry.detail),
    tone: entry.tone,
  };
}

/**
 * Réécrit les formules du service dans les mots du produit.
 *
 * Le journal recopiait les libellés de la projection, qui parlent de capture,
 * de flux et de point d'arrêt — et, pire, portaient une injonction
 * (« Relancer la capture ») vers une action que l'écran n'offre pas. Un
 * journal raconte ce qui s'est passé ; il ne donne pas d'ordre.
 */
const SPEECH: readonly (readonly [RegExp, string])[] = [
  [/continuité prouvée/gi, 'sans interruption détectée'],
  [/la capture attend le relancement/gi, 'la lecture attend d’être relancée'],
  [/reprend le flux au point d’arrêt/gi, 'reprendra là où elle s’est arrêtée'],
  [/reprend le flux au point d'arrêt/gi, 'reprendra là où elle s’est arrêtée'],
  [/\bla capture\b/gi, 'la lecture'],
  [/\ble flux\b/gi, 'la copie'],
  [/point d’arrêt/gi, 'dernier point enregistré'],
  [/point d'arrêt/gi, 'dernier point enregistré'],
];

function plainSpeech(value: string): string {
  return SPEECH.reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), value);
}

/**
 * Le détail, nettoyé de ses consignes.
 *
 * Les fragments qui disent à l'opérateur de relancer sont retirés : la marche
 * à suivre appartient à la liaison, où l'on sait si l'action est possible.
 */
function plainDetail(detail: string): string | null {
  const kept = plainSpeech(detail)
    .split(' — ')
    .filter((part) => !/^relanc/i.test(part.trim()))
    .join(' — ')
    .trim();
  return kept.length > 0 ? kept : null;
}

/**
 * Le journal affiché : les faits de la liaison, du plus récent au plus ancien,
 * sans doublon d'instant et de libellé.
 *
 * Deux étapes observées au même instant avec le même libellé produisent deux
 * lignes identiques que rien ne distingue à l'écran ; n'en garder qu'une évite
 * de faire croire à deux événements.
 */
export function historyFrom(entries: readonly ActivityEntry[]): readonly HistoryLine[] {
  const seen = new Set<string>();
  const lines: HistoryLine[] = [];
  for (const entry of entries) {
    if (!isMeaningful(entry)) continue;
    const line = historyLine(entry);
    const key = `${line.at}|${line.label}`;
    if (seen.has(key)) continue;
    seen.add(key);
    lines.push(line);
  }
  return lines.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

/* ------------------------------------------------------------------ */
/* Alertes de supervision                                              */
/* ------------------------------------------------------------------ */

/**
 * Les alertes actives, remontées au journal.
 *
 * Le service publie des contrôles de fonctionnement et leurs alertes. Elles ne
 * sont pas servies sur l'installation observée aujourd'hui — `observability`
 * y vaut « non rattachée » — mais le jour où elles le seront, un écran qui les
 * ignore laisserait passer un dépassement en silence. Elles remontent donc
 * ici, au même endroit que le reste de ce qui demande une attention.
 *
 * Seules les alertes en cours (`firing`) sont retenues : une alerte résolue
 * appartient à l'historique, pas à ce qui appelle une décision. Et rien n'est
 * remonté si le relevé lui-même n'est pas exploitable — une alerte tirée d'un
 * relevé périmé affirmerait un problème qui n'existe peut-être plus.
 */
export function activeAlerts(pipeline: Pipeline): readonly HistoryLine[] {
  const observability = pipeline.observability;
  if (!observability || observability.status === 'unavailable') return [];
  if (observability.quality.freshness !== 'fresh') return [];

  return observability.alerts
    .filter((alert) => alert.lifecycleState === 'firing')
    .map((alert) => ({
      at: alert.firingSince,
      label: `${sloCheckLabel(alert.checkId)} — ${sloSignalLabel(alert.signalStatus).toLowerCase()}`,
      detail:
        alert.observed === null
          ? null
          : `relevé ${sloValueLabel(alert.observed, alert.unit)}, seuil ${sloValueLabel(alert.threshold, alert.unit)}`,
      tone: alert.severity === 'critical' ? 'attention' : 'active',
    }));
}
