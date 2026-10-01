import type { LagPoint, Pipeline } from '../domain/controlPlane.ts';
import { lagTrend, VERDICT_COPY } from '../domain/lag.ts';
import { incidentOnset } from '../domain/liveBoard.ts';
import { buildLagSegments, lagCoverageLabel, selectSalientLagWindows } from '../domain/pipelineDetail.ts';
import { measureSubordination } from './proofStations.ts';
import { FactSpool } from './FactSpool.tsx';

const integerFormat = new Intl.NumberFormat('fr-FR');
const decimalFormat = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });

export interface LagTrend {
  readonly verdict: ReturnType<typeof lagTrend>['verdict'];
  readonly kind: 'bounded' | 'catching-up' | 'diverging' | 'inconclusive';
  readonly label: string;
  readonly answer: string;
  readonly delta: number | null;
}

export function resolveLagTrend(points: readonly LagPoint[]): LagTrend {
  const segment = buildLagSegments(points).at(-1) ?? [];
  const values = segment.map((point) => point.lag!);
  const trend = lagTrend(values);
  const copy = VERDICT_COPY[trend.verdict];
  const delta = values.length < 2 ? null : values[values.length - 1]! - values[0]!;
  return {
    verdict: trend.verdict,
    kind: trend.verdict.toLocaleLowerCase('en-US').replace('_', '-') as LagTrend['kind'],
    label: copy.label,
    answer: copy.answer,
    delta,
  };
}

export function LiveLagChart({ pipeline, current }: { readonly pipeline: Pipeline; readonly current: boolean }) {
  const points = pipeline.lagSeries;
  const trend = resolveLagTrend(points);
  const subordination = measureSubordination(pipeline);
  const currentLive = current && pipeline.quality.evidenceKind === 'live' && pipeline.quality.freshness === 'fresh' && subordination === null;
  const evidenceLabel = currentLive
    ? 'Lecture actuelle'
    : pipeline.quality.evidenceKind === 'live'
      ? pipeline.quality.freshness === 'fresh'
        ? 'Système réel · portée limitée'
        : 'Système réel · relevé non recevable'
    : pipeline.quality.evidenceKind === 'simulation'
      ? 'Mesures de démonstration'
      : 'Mesures antérieures';
  const lagLabel = currentLive ? 'Retard courant' : 'Dernier retard observé';
  const trendLabel = currentLive ? 'Tendance' : 'Tendance du dernier segment observé';
  const trendAnswer = currentLive && !subordination
    ? trend.answer
    : 'Calcul limité au dernier segment contigu de ce relevé.';
  const lagValue = pipeline.lagSequences === null ? 'Inconnu' : integerFormat.format(pipeline.lagSequences);
  const [subordinationHeadline, subordinationHint] = splitSubordination(subordination);
  const lagUnit = pipeline.lagSequences === null
    ? ''
    : pipeline.lagSequences === 1 ? ' enregistrement' : ' enregistrements';

  return (
    <section className={`live-evidence live-evidence--${pipeline.quality.evidenceKind}${currentLive ? ' live-evidence--fresh-live' : ' live-evidence--retained'}${subordination ? ' live-evidence--subordinated' : ''}`} aria-labelledby="lag-series-title">
      <FactSpool
        className="live-proof-band"
        label="Portée de la mesure"
        bands={[{
          key: 'quality',
          fields: [
            { label: 'Portée', value: evidenceLabel, nowrap: true },
            { label: 'Nature', value: natureLabel(pipeline), nowrap: true },
            {
              label: 'Observé',
              value: <>{freshnessLabel(pipeline)} · <time dateTime={pipeline.observedAt}>{formatTimestamp(pipeline.observedAt)}</time></>,
            },
            { label: 'Limite', value: MEASURE_LIMIT },
          ],
        }]}
      />
      {subordination ? (
        <p className="live-measure-subordination">
          <strong>{subordinationHeadline}</strong>
          <span>dernier retard observé {lagValue}{lagUnit} — {subordinationHint}</span>
        </p>
      ) : (
        <div className="live-reading">
          <div className={`live-reading__trend live-reading__trend--${trend.kind}`}>
            <p className="section-kicker">{trendLabel}</p>
            <strong>{trend.label}</strong>
            <small>{trendAnswer}</small>
            {trend.delta === null ? null : <span className="live-reading__delta">Variation · {trend.delta > 0 ? '+' : ''}{integerFormat.format(trend.delta)} {Math.abs(trend.delta) === 1 ? 'enregistrement' : 'enregistrements'}</span>}
          </div>
          <div className="live-reading__value">
            <p className="section-kicker">{lagLabel}</p>
            <p><strong>{lagValue}</strong>{pipeline.lagSequences === null ? null : <span>{pipeline.lagSequences === 1 ? 'enregistrement' : 'enregistrements'}</span>}</p>
          </div>
        </div>
      )}
      {subordination ? (
        <p className={`live-measure-trend live-measure-trend--${trend.kind}`}>
          <span className="section-kicker">{trendLabel}</span>
          <strong>{trend.label}</strong>
          <small>{trendAnswer}</small>
        </p>
      ) : null}

      {points.length === 0 || pipeline.lagSeriesResolutionSeconds === null ? (
        <div className="live-evidence__empty">
          <p className="section-kicker">Retard source</p>
          <h2 id="lag-series-title">Série non mesurée</h2>
          <p>Aucune mesure de retard n’est fournie par ce relevé ; aucune courbe n’est inventée.</p>
        </div>
      ) : <LagGraph pipeline={pipeline} points={points} subordinated={subordination !== null} />}
    </section>
  );
}

function LagGraph({ pipeline, points, subordinated }: { readonly pipeline: Pipeline; readonly points: readonly LagPoint[]; readonly subordinated: boolean }) {
  const width = 960;
  const height = 180;
  const padding = { left: 32, right: 16, top: 14, bottom: 22 };
  const maxEnd = Math.max(...points.map((point) => point.endSeconds), 1);
  const observedMax = Math.max(...points.flatMap((point) => [point.high ?? 0, point.lag ?? 0]), 0);
  const maxLag = Math.max(observedMax, 1);
  const x = (point: LagPoint) => padding.left + ((point.startSeconds + point.endSeconds) / 2 / maxEnd) * (width - padding.left - padding.right);
  const y = (value: number) => {
    const normalized = Math.log10(value + 1) / Math.log10(maxLag + 1);
    return padding.top + (1 - normalized) * (height - padding.top - padding.bottom);
  };
  const segments = buildLagSegments(points);
  const floorNote = floorAnnotation(pipeline, subordinated);
  // Début de l'incident servi — texte, pas marqueur positionné : l'axe du
  // tracé est en secondes relatives, sans origine temporelle publiée.
  const onset = pipeline.incident !== null ? incidentOnset(pipeline, pipeline.incident.type) : null;
  const preview = selectSalientLagWindows(points);
  const previewPoints = preview.points;
  const previewIsComplete = preview.complete;

  return (
    <>
      <div className="lag-chart__head">
        <div><p className="section-kicker">Mesure continue</p><h2 id="lag-series-title">Retard source, sans interpolation</h2></div>
        <p>Une mesure toutes les {decimalFormat.format(pipeline.lagSeriesResolutionSeconds!)} s · {integerFormat.format(pipeline.lagSampleCount)} mesures</p>
      </div>
      {pipeline.lagUnknownSampleCount > 0 ? <p className="live-evidence__warning">{integerFormat.format(pipeline.lagUnknownSampleCount)} mesures inconnues restent explicitement vides.</p> : null}
      {floorNote ? <p className="lag-chart__floor">{floorNote}</p> : null}
      {onset !== null ? (
        <p className="lag-chart__onset">
          Incident servi — {onset.word} depuis <time dateTime={onset.at}>{formatTimestamp(onset.at)}</time>
        </p>
      ) : null}
      <div className="lag-chart" aria-label="Retard en enregistrements, échelle verticale logarithmique, sans interpolation des données absentes.">
        <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
          <line className="lag-chart__axis" vectorEffect="non-scaling-stroke" x1={padding.left} y1={height - padding.bottom} x2={width - padding.right} y2={height - padding.bottom} />
          {points.filter((point) => point.coverage === 'complete' && point.low !== null && point.high !== null).map((point) => (
            <line className="lag-chart__range" vectorEffect="non-scaling-stroke" key={`${point.startSeconds}-${point.endSeconds}`} x1={x(point)} x2={x(point)} y1={y(point.high!)} y2={y(point.low!)} />
          ))}
          {segments.map((segment, index) => segment.length === 1 ? (
            <circle className="lag-chart__point" vectorEffect="non-scaling-stroke" key={`segment-${index}`} cx={x(segment[0]!)} cy={y(segment[0]!.lag!)} r="4" />
          ) : (
            <polyline className="lag-chart__line" vectorEffect="non-scaling-stroke" key={`segment-${index}`} points={segment.map((point) => `${x(point)},${y(point.lag!)}`).join(' ')} />
          ))}
        </svg>
        <div className="lag-chart__scale"><span>0 s</span><span>max {integerFormat.format(observedMax)}</span><span>{decimalFormat.format(maxEnd)} s</span></div>
      </div>
      <div className="lag-chart__guide">
        <p>Échelle verticale logarithmique · retard en enregistrements</p>
        <ul className="lag-chart__legend" aria-label="Légende du graphe">
          <li><span className="lag-chart__key lag-chart__key--line" aria-hidden="true" />Dernier retard</li>
          <li><span className="lag-chart__key lag-chart__key--range" aria-hidden="true" />Plage minimum–maximum</li>
        </ul>
      </div>
      <div className="lag-register-preview">
        <LagRegisterTable
          points={previewPoints}
          caption={preview.caption}
        />
      </div>
      {previewIsComplete ? null : (
        <details className="lag-register">
          <summary>Voir les {integerFormat.format(points.length)} intervalles de mesure</summary>
          <div className="lag-table-wrap">
            <LagRegisterTable points={points} caption="Tous les intervalles, synchronisés avec le graphe" />
          </div>
        </details>
      )}
    </>
  );
}

function LagRegisterTable({ points, caption }: { readonly points: readonly LagPoint[]; readonly caption: string }) {
  return (
    <table className="lag-table">
      <caption>{caption}</caption>
      <thead><tr><th scope="col">Temps</th><th scope="col">Dernier</th><th scope="col">Minimum</th><th scope="col">Maximum</th><th scope="col">Mesures</th><th scope="col">Couverture</th></tr></thead>
      <tbody>{points.map((point) => (
        <tr key={`${point.startSeconds}-${point.endSeconds}`} className={point.coverage === 'gap' ? 'is-gap' : undefined}>
          <th scope="row">{decimalFormat.format(point.startSeconds)}–{decimalFormat.format(point.endSeconds)} s</th>
          <td data-label="Dernier">{formatLag(point.lag)}</td>
          <td data-label="Minimum">{formatLag(point.low)}</td>
          <td data-label="Maximum">{formatLag(point.high)}</td>
          <td data-label="Mesures">{integerFormat.format(point.samples)}</td>
          <td data-label="Couverture">{lagCoverageLabel(point)}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function floorAnnotation(pipeline: Pipeline, subordinated: boolean): string | null {
  if (!subordinated) return null;
  if (pipeline.lagSequences === 0 || pipeline.lagSeconds === 0) {
    return 'Retard à 0 — mesure subordonnée';
  }
  return null;
}

const MEASURE_LIMIT = 'La lecture et ses volumes ne confirment pas l’arrivée à destination.';

function splitSubordination(copy: string | null): readonly [string, string] {
  if (!copy) return ['', ''];
  const [headline, ...rest] = copy.split(', ');
  return [headline ?? copy, rest.join(', ') || 'pas un signal de santé courant'];
}

function natureLabel(pipeline: Pipeline): string {
  return { live: 'Système réel', historical: 'Relevé antérieur', simulation: 'Démonstration' }[pipeline.quality.evidenceKind];
}

function freshnessLabel(pipeline: Pipeline): string {
  if (pipeline.quality.freshness === 'fresh') {
    if (pipeline.quality.evidenceKind === 'historical') return 'Récente au moment du relevé antérieur · fraîcheur actuelle non établie';
    if (pipeline.quality.evidenceKind === 'simulation') return 'Récente dans la démonstration · fraîcheur réelle non établie';
    return 'Observation récente';
  }
  return { late: 'Relevé tardif', stale: 'Relevé trop ancien', clock_untrusted: 'Horloge non fiable' }[pipeline.quality.freshness];
}

function formatLag(value: number | null): string {
  return value === null ? 'Inconnu' : integerFormat.format(value);
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }).format(new Date(value));
}
