/**
 * MetricTile — une tuile de mesure : libellé discret, valeur en mono, ligne de
 * provenance/fraîcheur. Une mesure absente ne devient jamais 0 ni une
 * estimation silencieuse : elle rend « — » et le mot « absent ».
 */
export type MetricProvenance = 'mesuré' | 'estimé' | 'absent';

export interface MetricTileProps {
  readonly label: string;
  readonly value: string | null;
  readonly unit?: string;
  readonly provenance: MetricProvenance;
  /** Âge de la mesure déjà formaté (ex. « il y a 3 min »). Omis si `provenance` est `absent`. */
  readonly age?: string;
}

export function MetricTile({ label, value, unit, provenance, age }: MetricTileProps) {
  const absent = provenance === 'absent' || value === null;
  return (
    <section className="metric-tile" aria-label={label}>
      <p className="metric-tile__label">{label}</p>
      <p className={`metric-tile__value mono${absent ? ' metric-tile__value--absent' : ''}`}>
        {absent ? '—' : value}
        {!absent && unit ? <span className="metric-tile__unit">{unit}</span> : null}
      </p>
      <p className={`metric-tile__provenance metric-tile__provenance--${absent ? 'absent' : provenance === 'estimé' ? 'estime' : 'mesure'}`}>
        {absent ? 'absent' : provenance}
        {!absent && age ? <span> · {age}</span> : null}
      </p>
    </section>
  );
}
