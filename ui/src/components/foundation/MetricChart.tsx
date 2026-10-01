import { buildSparkline, sparklineToAreaPaths, sparklineToPaths } from '../../domain/metricChart.ts';

const metricNumber = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 });

/**
 * MetricChart — sparkline SVG légère (sans dépendance de graphique), pour la
 * série d'un `MetricsSeries` (retard ou débit). Une série entièrement
 * absente (ex. table en pause) rend un message explicite, jamais un tracé
 * plat à zéro.
 */
export function MetricChart({
  label,
  values,
  unit,
  width = 320,
  height = 64,
}: {
  readonly label: string;
  readonly values: readonly (number | null)[];
  readonly unit: string;
  readonly width?: number;
  readonly height?: number;
}) {
  const sparkline = buildSparkline(values, width, height);
  const measured = values.filter((value): value is number => value !== null);

  if (!sparkline || measured.length === 0) {
    return (
      <section className="metric-chart metric-chart--absent" aria-label={label}>
        <p className="metric-chart__label">{label}</p>
        <p className="metric-chart__absent">Non mesuré sur cette fenêtre.</p>
      </section>
    );
  }

  const paths = sparklineToPaths(sparkline);
  const areas = sparklineToAreaPaths(sparkline, height);
  const last = measured.at(-1)!;

  return (
    <section className="metric-chart" aria-label={label}>
      <p className="metric-chart__label">{label}</p>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`${label} — dernière valeur ${metricNumber.format(last)}${unit}`}>
        {areas.map((d, index) => (
          <path key={index} d={d} className="metric-chart__area" />
        ))}
        {paths.map((d, index) => (
          <path key={index} d={d} className="metric-chart__line" />
        ))}
      </svg>
      <p className="metric-chart__scale mono" aria-hidden="true">
        <span>min {metricNumber.format(sparkline.min)}{unit}</span>
        <span>max {metricNumber.format(sparkline.max)}{unit}</span>
      </p>
      <p className="metric-chart__value mono">
        {metricNumber.format(last)}
        <span className="metric-chart__unit">{unit}</span>
      </p>
    </section>
  );
}
