/**
 * Géométrie pure d'un graphe en ligne (sparkline) — aucune dépendance de
 * graphique lourde : la même approche que `LiveLagChart.tsx` (SVG à la
 * main), généralisée aux séries `MetricPoint` du cockpit v2 (retard/débit,
 * fenêtres 1h/24h). Une valeur absente (`null`) coupe le tracé plutôt que
 * de l'interpoler ou de la ramener à zéro.
 */
export interface SparklineSegment {
  readonly points: readonly { readonly x: number; readonly y: number }[];
}

export interface Sparkline {
  readonly segments: readonly SparklineSegment[];
  readonly min: number;
  readonly max: number;
}

/** Marge verticale (haut et bas), en fraction de la hauteur totale : un pic
 *  ou un creux touchant le bord du cadre se lit mal (rien ne dépasse « en
 *  haut » ou « en bas » à l'œil) — le tracé reste dans la bande centrale. */
const VERTICAL_PADDING_RATIO = 0.12;

/** `null` : aucune valeur mesurée dans la série (jamais une plage 0..0
 *  fabriquée — voir `buildSparkline`). */
export function buildSparkline(values: readonly (number | null)[], width: number, height: number): Sparkline | null {
  const measured = values.filter((value): value is number => value !== null);
  if (measured.length === 0) return null;
  const min = Math.min(...measured, 0);
  const max = Math.max(...measured);
  const span = max - min || 1;
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  const topPad = height * VERTICAL_PADDING_RATIO;
  const usableHeight = height - topPad * 2;

  const segments: SparklineSegment[] = [];
  let current: { x: number; y: number }[] = [];
  values.forEach((value, index) => {
    if (value === null) {
      if (current.length > 0) segments.push({ points: current });
      current = [];
      return;
    }
    const x = stepX * index;
    const y = topPad + usableHeight - ((value - min) / span) * usableHeight;
    current.push({ x, y });
  });
  if (current.length > 0) segments.push({ points: current });

  return { segments, min, max };
}

export function sparklineToPaths(sparkline: Sparkline): readonly string[] {
  return sparkline.segments
    .filter((segment) => segment.points.length > 0)
    .map((segment) => segment.points.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' '));
}

/** Variante fermée de `sparklineToPaths`, pour l'aire sous la courbe :
 *  chaque segment redescend jusqu'à la base (`height`) puis revient à son
 *  point de départ — une aire par segment, jamais un remplissage qui
 *  franchit une coupure (valeur absente). */
export function sparklineToAreaPaths(sparkline: Sparkline, height: number): readonly string[] {
  return sparkline.segments
    .filter((segment) => segment.points.length > 0)
    .map((segment) => {
      const line = segment.points.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
      const first = segment.points[0]!;
      const last = segment.points[segment.points.length - 1]!;
      return `${line} L${last.x.toFixed(1)},${height.toFixed(1)} L${first.x.toFixed(1)},${height.toFixed(1)} Z`;
    });
}
