/** Formatage. Une règle : ne jamais arrondir un chiffre qui porte une preuve.
 *
 *  30 320 398 ne devient pas « 30 M ». 0,5463 ms ne devient pas « 0,55 ». La
 *  forme abrégée existe pour les axes du graphe, où l'espace la justifie, et
 *  elle est toujours doublée de la valeur exacte au survol.
 */

/** Espace fine insécable — le séparateur de milliers du français. */
const THIN = ' ';

export function sequences(value: number): string {
  const sign = value < 0 ? '-' : '';
  const digits = Math.abs(Math.trunc(value)).toString();
  let out = '';
  for (let i = 0; i < digits.length; i += 1) {
    if (i > 0 && (digits.length - i) % 3 === 0) out += THIN;
    out += digits[i];
  }
  return sign + out;
}

/** Forme abrégée réservée aux graduations d'axe. Jamais dans une carte. */
export function axisTick(value: number): string {
  if (value === 0) return '0';
  if (Math.abs(value) < 1000) return sequences(value);
  if (Math.abs(value) < 1_000_000) return `${sequences(Math.round(value / 1000))}${THIN}k`;
  return `${(value / 1_000_000).toLocaleString('fr-FR', {
    maximumFractionDigits: 1,
  })}${THIN}M`;
}

/** Décimales exactes, séparateur français, jamais de troncature silencieuse. */
export function decimal(value: number, digits: number): string {
  return value.toLocaleString('fr-FR', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** La valeur telle qu'elle a été mesurée, sans rien ajouter ni retrancher.
 *
 *  Ni troncature — 0,5463 ne devient pas 0,55 — ni bourrage : une mesure de
 *  30,69 affichée « 30,6900 » revendique deux décimales qui n'existent pas.
 *  Une fausse précision use la confiance aussi sûrement qu'un arrondi. */
export function exact(value: number, maxDigits = 6): string {
  return value.toLocaleString('fr-FR', {
    minimumFractionDigits: 0,
    maximumFractionDigits: maxDigits,
  });
}

/** Durée lisible — un seul format partout : `min·s` sous l'heure, `h·min`
 *  sous deux jours, `j` au-delà. On ne mélange jamais « 196 s », « 38 h 53 »
 *  et « 90 501 s » sur un écran : 196 s → `3 min 16 s`, 90 501 s → `25 h 08`.
 *  La seconde reste affichée sous la minute (âge d'un relevé, fenêtre courte). */
export function formatDuration(totalSeconds: number): string {
  const s = Math.round(totalSeconds);
  if (s < 60) return `${sequences(s)}${THIN}s`;
  const minutes = Math.floor(s / 60);
  const restSeconds = s % 60;
  if (minutes < 60) {
    return restSeconds
      ? `${minutes}${THIN}min${THIN}${pad(restSeconds)}${THIN}s`
      : `${minutes}${THIN}min`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 48) {
    return restMinutes ? `${hours}${THIN}h${THIN}${pad(restMinutes)}` : `${hours}${THIN}h`;
  }
  return `${Math.round(hours / 24)}${THIN}j`;
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** Âge d'une mesure. Le seuil `stale` n'est pas cosmétique : au-delà, l'écran
 *  cesse d'affirmer et se contente de rapporter ce qu'il a lu. */
export interface Age {
  readonly seconds: number;
  readonly label: string;
  readonly stale: boolean;
}

export function age(observedAt: string, now: Date, staleAfterS = 120): Age {
  const seconds = Math.max(0, (now.getTime() - new Date(observedAt).getTime()) / 1000);
  return {
    seconds,
    label: seconds < 5 ? "à l'instant" : `il y a ${formatDuration(seconds)}`,
    stale: seconds > staleAfterS,
  };
}

const HORODATAGE = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZoneName: 'short',
});

/** Horodatage complet, à la seconde, avec le fuseau. Sert de preuve. */
export function timestamp(iso: string): string {
  return HORODATAGE.format(new Date(iso));
}

/** Position de journal sous sa forme canonique : receiver puis séquence. */
export function position(receiver: string, sequence: number): string {
  return `${receiver} · ${sequences(sequence)}`;
}
