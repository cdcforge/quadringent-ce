export interface MetricProps {
  readonly label: string;
  readonly value: string;
  readonly unit?: string;
  readonly observedAt?: string;
  readonly source?: string;
  readonly unknownReason?: string;
}

export function Metric({
  label,
  value,
  unit,
  observedAt,
  source,
  unknownReason,
}: MetricProps) {
  return (
    <section className="metric" aria-label={label}>
      <p className="metric__label">{label}</p>
      <p className="metric__value num">
        <span>{value}</span>
        {unit ? <span className="metric__unit">{unit}</span> : null}
      </p>
      {unknownReason ? <p className="metric__unknown">Non établi : {unknownReason}</p> : null}
      {observedAt || source ? (
        <details className="metric__evidence">
          <summary>Observation et source</summary>
          <div className="metric__evidence-body">
            {observedAt ? (
              <time dateTime={observedAt}>{observedAt}</time>
            ) : (
              <span>Instant non transmis</span>
            )}
            {source ? <span> · {source}</span> : null}
          </div>
        </details>
      ) : null}
    </section>
  );
}
