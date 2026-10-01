import type { CSSProperties, ReactNode } from 'react';
import '../styles/fact-spool.css';

export interface FactSpoolField {
  readonly label: string;
  readonly value: ReactNode;
  readonly hint?: ReactNode;
  readonly nowrap?: boolean;
  readonly dominant?: boolean;
}

export interface FactSpoolBand {
  readonly key: string;
  readonly tone?: 'decision' | 'facts' | 'gesture';
  readonly fields: readonly FactSpoolField[];
}

export function FactSpool({
  bands,
  className,
  labelledBy,
  label,
  note,
}: {
  readonly bands: readonly FactSpoolBand[];
  readonly className?: string;
  readonly labelledBy?: string;
  readonly label?: string;
  readonly note?: ReactNode;
}) {
  return (
    <div
      className={['fact-spool', className].filter(Boolean).join(' ')}
      role="group"
      aria-labelledby={labelledBy}
      aria-label={labelledBy ? undefined : label}
    >
      {bands.map((band) => (
        <div
          key={band.key}
          className={`fact-spool__band fact-spool__band--${band.key}${band.tone ? ` fact-spool__band--${band.tone}` : ''}`}
          style={{ '--spool-cols': String(band.fields.length) } as CSSProperties}
        >
          {band.fields.map((field) => (
            <div
              key={field.label}
              className={`fact-spool__field${field.nowrap ? ' fact-spool__field--nowrap' : ''}${field.dominant ? ' fact-spool__field--dominant' : ''}`}
            >
              <span className="fact-spool__label">{field.label}</span>
              <span className="fact-spool__value">{field.value}</span>
              {field.hint ? <span className="fact-spool__hint">{field.hint}</span> : null}
            </div>
          ))}
        </div>
      ))}
      {note ? <p className="fact-spool__note">{note}</p> : null}
    </div>
  );
}
