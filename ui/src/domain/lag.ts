import type { LagVerdict } from './types';

/** Port fidèle de `continuous.lag_trend` (src/quadringent/continuous.py).
 *
 *  La console ne réinvente pas le verdict : elle applique la même règle que le
 *  worker, pour qu'un écran et un log ne puissent jamais se contredire. Le test
 *  de parité vit dans `lag.test.ts`.
 *
 *  La règle tient en une phrase : ce qui compte n'est pas la pente, c'est le
 *  plancher. Un lecteur qui suit ne tient pas un retard plat, il oscille et
 *  revient périodiquement près de zéro. Le retard diverge quand le plancher du
 *  dernier tiers dépasse celui du premier — pas quand le dernier point est haut.
 */
export interface LagTrend {
  verdict: LagVerdict;
  diverging: boolean;
  samples: number;
  first: number | null;
  last: number | null;
  min: number | null;
  max: number | null;
  mean?: number;
  floorFirstThird?: number;
  floorLastThird?: number;
  slopePerSample: number;
}

export function lagTrend(
  samples: readonly number[],
  options: { floorRatio?: number; tolerance?: number; floorAbsolute?: number } = {},
): LagTrend {
  const floorRatio = options.floorRatio ?? 4.0;
  const tolerance = options.tolerance ?? 0.25;
  const floorAbsolute = options.floorAbsolute ?? 1000;

  const values = samples.map((item) => Math.trunc(item));

  if (values.length < 2) {
    return {
      verdict: 'INCONCLUSIVE',
      diverging: false,
      samples: values.length,
      first: values.length ? values[0] : null,
      last: values.length ? values[values.length - 1] : null,
      min: values.length ? Math.min(...values) : null,
      max: values.length ? Math.max(...values) : null,
      slopePerSample: 0,
    };
  }

  const count = values.length;
  const meanX = (count - 1) / 2;
  const meanY = values.reduce((a, b) => a + b, 0) / count;
  let denominator = 0;
  let numerator = 0;
  for (let index = 0; index < count; index += 1) {
    denominator += (index - meanX) ** 2;
    numerator += (index - meanX) * (values[index] - meanY);
  }
  const slope = denominator ? numerator / denominator : 0;

  const third = Math.max(1, Math.floor(count / 3));
  const floorFirst = Math.min(...values.slice(0, third));
  const floorLast = Math.min(...values.slice(count - third));
  const threshold = tolerance * Math.max(Math.abs(meanY), 1);

  // Un plancher qui monte est à lui seul une divergence : exiger en plus le
  // test de pente avait masqué une montée 1 -> 1 377 824, parce que le seuil
  // de pente est proportionnel au retard moyen (palier 6, 2026-08-27).
  const floorRose =
    floorLast > Math.max(floorFirst, 1) * floorRatio && floorLast > floorAbsolute;

  let verdict: LagVerdict;
  if (floorRose || (slope > threshold && floorLast > floorFirst)) {
    verdict = 'DIVERGING';
  } else if (slope < -threshold && floorLast < floorFirst) {
    verdict = 'CATCHING_UP';
  } else {
    verdict = 'BOUNDED';
  }

  return {
    verdict,
    diverging: verdict === 'DIVERGING',
    samples: count,
    first: values[0],
    last: values[count - 1],
    min: Math.min(...values),
    max: Math.max(...values),
    mean: Math.round(meanY * 10) / 10,
    floorFirstThird: floorFirst,
    floorLastThird: floorLast,
    slopePerSample: Math.round(slope * 1000) / 1000,
  };
}

/** Ce que le verdict veut dire, en français, sans jargon générique.
 *
 *  `answer` répond à la seule question qui compte. `path` est la trajectoire du
 *  plancher, dessinée : c'est la même information que le mot, portée par une
 *  forme, pour qu'un écran monochrome reste lisible et qu'une lecture de trois
 *  secondes suffise. Le tracé se lit toujours contre la ligne du tail, qui est
 *  la base du glyphe.
 */
export const VERDICT_COPY: Record<
  LagVerdict,
  { label: string; answer: string; path: string; severe: boolean }
> = {
  BOUNDED: {
    label: 'Stable',
    answer: 'Le retard revient à zéro. Le flux suit la source.',
    // Des pointes qui retombent chaque fois sur la ligne du tail.
    path: 'M0 11 L3 11 L6 2 L9 11 L13 11 L16 5 L19 11 L22 11',
    severe: false,
  },
  CATCHING_UP: {
    label: 'Rattrapage',
    answer: 'Le retard se résorbe ; rien à faire.',
    path: 'M0 2 L7 4 L14 8 L22 11',
    severe: false,
  },
  DIVERGING: {
    label: 'S’aggrave',
    answer: 'Le retard monte sans revenir ; il faut intervenir.',
    path: 'M0 11 L7 9 L14 5 L22 1',
    severe: true,
  },
  INCONCLUSIVE: {
    label: 'Indéterminé',
    answer: 'Moins de deux mesures ; la tendance n’est pas calculable.',
    path: 'M2 11 L3 11 M10 11 L11 11 M19 11 L20 11',
    severe: false,
  },
};
