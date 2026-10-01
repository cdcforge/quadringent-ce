import { useEffect, useRef, useState } from 'react';
import { LiveLagChart } from '../components/LiveLagChart.tsx';
import type { ControlPlaneState } from '../data/useControlPlane.ts';
import type { ActionId, PipelineActionReceipt } from '../data/controlPlaneClient.ts';
import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import { coverageLabel, dataNatureLabel, freshnessLabel } from '../domain/pipelineDetail.ts';
import {
  destinationApplyStamp,
  destinationRows,
  fluxIdentityRows,
  positionRows,
  projectedLagVerdict,
  runDetailRows,
} from '../domain/sourceDetail.ts';
import {
  deriveThroughput,
  pipelineSectionView,
  transportFor,
  type CounterSample,
  type SectionView,
} from '../domain/liveBoard.ts';
import { resolvePipelineProofFocus, type ProofFocus } from '../domain/proofFocus.ts';
import { timestamp } from '../domain/format.ts';
import { LiaisonCard } from '../components/LiaisonCard.tsx';
import { actionFailureFor, actionResultFor, liaisonView, stepsFor, type ActionOffer, type LiaisonView } from '../domain/operator.ts';
import { tableRows, tablesSummary } from '../domain/tableView.ts';
import '../styles/tables.css';
import '../styles/liaison.css';
import { href, type PipelineTab, type Route } from '../router.ts';
import { buildCounterMetrics } from '../productViewModels.ts';
import '../styles/pipeline-detail.css';

const numberFormat = new Intl.NumberFormat('fr-FR');
const timeFormat = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
});

const tabs: ReadonlyArray<{ readonly name: PipelineTab; readonly label: string }> = [
  { name: 'overview', label: 'État' },
  { name: 'tables', label: 'Tables' },
  { name: 'live', label: 'Mesures' },
];

export function pipelineStageAnchorId(stage: StageId): string {
  return `pipeline-stage-${stage}`;
}

export function PipelineDetail({
  state,
  pipeline,
  route,
  onRefresh,
  onBusinessAction,
}: {
  readonly state: ControlPlaneState;
  readonly pipeline: Pipeline | null;
  readonly route: Extract<Route, { name: 'pipeline' }>;
  readonly onRefresh: () => void;
  readonly onBusinessAction?: (pipelineId: string, action: ActionId) => Promise<PipelineActionReceipt>;
}) {
  const now = useNow();
  const samples = useRef(new Map<string, CounterSample>());
  const [busy, setBusy] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const currentTab = route.tab ?? 'overview';
  const unavailable = pipeline === null || state.status === 'loading' || state.status === 'failed';
  const transport = transportFor(state, false);
  const previous = pipeline ? samples.current.get(pipeline.id) : undefined;

  const focus = pipeline && !unavailable
    ? resolvePipelineProofFocus(pipeline, {
        generatedAt: state.overview.generatedAt,
        sources: state.overview.sources,
        cached: isCachedState(state),
      })
    : null;
  const view = pipeline && !unavailable
    ? pipelineSectionView(pipeline, { sources: state.overview.sources, transport, previous })
    : null;

  useEffect(() => {
    if (unavailable || pipeline === null) return;
    const previousSample = samples.current.get(pipeline.id);
    const current = { events: pipeline.counters.events_published ?? null, at: pipeline.observedAt };
    const derived = deriveThroughput(previousSample, current);
    const lastRate =
      derived.kind === 'measured'
        ? { value: derived.value, at: pipeline.observedAt }
        : previousSample?.lastRate ?? null;
    samples.current.set(pipeline.id, { ...current, lastRate });
  }, [unavailable, pipeline]);

  const liaison = pipeline && !unavailable ? liaisonView(pipeline, now) : null;

  return (
    <div className={`product-view pipeline-detail-view pipeline-detail-view--${currentTab}`}>
      <header className="detail-hero">
        <p className="page-kicker">
          <a href={href({ name: 'overview' })}>Liaisons</a>
        </p>
        <div className="detail-hero__line">
          <div className="detail-hero__identity">
            <h1 className="page-title">{pipeline?.id ?? route.id}</h1>
            {liaison?.destination ? (
              <p className="detail-hero__destination">Vers {liaison.destination}</p>
            ) : null}
          </div>
          {liaison ? (
            <span className={`liaison__state liaison__state--${liaison.health}`}>{liaison.stateLabel}</span>
          ) : null}
          <button className="detail-refresh" type="button" onClick={onRefresh}>Actualiser</button>
        </div>
      </header>

      {actionNotice && <p role="status" className="liaison__guidance">{actionNotice}</p>}

      <nav className="detail-tabs" aria-label="Onglets de la liaison">
        {tabs.map((tab) => (
          <a
            key={tab.name}
            href={href({ name: 'pipeline', id: route.id, tab: tab.name })}
            aria-current={currentTab === tab.name ? 'page' : undefined}
            className={currentTab === tab.name ? 'is-current' : undefined}
          >
            {tab.label}
          </a>
        ))}
      </nav>

      {unavailable || pipeline === null || view === null || focus === null || liaison === null ? (
        <section className="detail-empty" aria-live="polite">
          <strong>L’état de cette liaison n’a pas encore été lu.</strong>
          <button className="page-action" type="button" onClick={onRefresh}>Réessayer</button>
        </section>
      ) : currentTab === 'tables' ? (
        <TablesView pipeline={pipeline} view={view} />
      ) : currentTab === 'live' ? (
        <MeasurementView pipeline={pipeline} focus={focus} />
      ) : (
        <StateView
          pipeline={pipeline}
          liaison={liaison}
          now={now}
          busy={busy}
          onAction={
            onBusinessAction
              ? (action: ActionOffer) => {
                  setBusy(true);
                  setActionNotice(null);
                  void onBusinessAction(pipeline.id, action.id)
                    .then((receipt) => setActionNotice(actionResultFor(pipeline.id, receipt)))
                    .catch(() => setActionNotice(actionFailureFor(pipeline.id)))
                    .finally(() => setBusy(false));
                }
              : undefined
          }
        />
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* Vue d'ensemble — l'état, puis les trois étapes                          */
/* ---------------------------------------------------------------------- */

function StateView({
  pipeline,
  liaison,
  now,
  busy,
  onAction,
}: {
  readonly pipeline: Pipeline;
  readonly liaison: LiaisonView;
  readonly now: Date;
  readonly busy: boolean;
  readonly onAction?: (action: ActionOffer) => void;
}) {
  const steps = stepsFor(pipeline, now);
  return (
    <div className="detail-state-view">
      <LiaisonCard view={liaison} busy={busy} onAction={onAction} compact />

      {steps.length > 0 && (
        <section className="steps" aria-label="Étapes de la copie">
          <h2 className="steps__title">Où en sont les données</h2>
          <ol className="steps__list">
            {steps.map((step) => (
              <li className={`step step--${step.health}`} key={step.id}>
                <span className="step__dot" aria-hidden="true" />
                <div className="step__body">
                  <p className="step__label">{step.label}</p>
                  <p className="step__detail">{step.detail}</p>
                </div>
                {step.observed !== null && <span className="step__age">{step.observed}</span>}
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------------- */
/* L1 — Flux : bande live, chaîne de preuve, incidents, tables             */

/* ---------------------------------------------------------------------- */
/* L2 — Tables : l'état de copie, table par table                          */
/* ---------------------------------------------------------------------- */

function TablesView({ pipeline }: { readonly pipeline: Pipeline; readonly view: SectionView }) {
  const rows = tableRows(pipeline);
  return (
    <section className="tables" aria-labelledby="detail-tables-title">
      <header className="tables__head">
        <div>
          <h2 className="tables__title" id="detail-tables-title">Tables de cette liaison</h2>
          <p className="tables__summary">{tablesSummary(rows)}</p>
        </div>
      </header>
      {rows.length === 0 ? (
        <p className="tables__none">
          Le service n’a pas encore publié l’état des tables de cette liaison.
        </p>
      ) : (
        <ul className="tables__list">
          {rows.map((row) => (
            <li className={`tables__row tables__row--${row.state}`} key={row.name}>
              <span className="tables__name">{row.name}</span>
              <span className="tables__state">{row.stateLabel}</span>
              {row.progress !== null && (
                <span
                  className="tables__progress"
                  role="img"
                  aria-label={`Copie à ${Math.round(row.progress * 100)} %`}
                >
                  <span className="tables__progress-fill" style={{ width: `${row.progress * 100}%` }} />
                </span>
              )}
              <span
                className={row.rows === null ? 'tables__rows tables__rows--absent' : 'tables__rows'}
                data-numeric=""
              >
                {row.rows ?? 'Non mesuré'}
                {row.rows !== null && <span className="tables__rows-unit"> lignes</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------------- */
/* L3 — Mesures : compteurs, tracé, registres techniques                   */
/* ---------------------------------------------------------------------- */

function MeasurementView({ pipeline, focus }: { readonly pipeline: Pipeline; readonly focus: ProofFocus }) {
  const metrics = buildCounterMetrics(pipeline.counters);
  const events = pipeline.counters.events_published;
  const current = focus.proofScope.kind === 'live' && pipeline.quality.freshness === 'fresh';
  const lagVerdict = projectedLagVerdict(pipeline);
  const applyStamp = destinationApplyStamp(pipeline);

  const runRows: ReadonlyArray<readonly [string, string]> = [
    ...runDetailRows(pipeline),
    [
      'Verdict de retard déclaré',
      lagVerdict.instrumented
        ? lagVerdict.reason !== null
          ? `${lagVerdict.label} — ${lagVerdict.reason}`
          : lagVerdict.label
        : `${lagVerdict.label} — ${lagVerdict.reason ?? 'mesure non publiée par le service'}`,
    ],
    [
      'Dernière livraison',
      applyStamp.measured
        ? `${applyStamp.label} UTC`
        : `${applyStamp.label} — ${applyStamp.reason ?? 'non mesuré'}`,
    ],
  ];

  return (
    <>
      <section className="measurement-summary" aria-labelledby="measurement-title">
        <p className="section-kicker">Mesures observées</p>
        <h2 id="measurement-title">
          {events === null || events === undefined
            ? 'Aucun événement lu'
            : `${formatRows(events)} événements lus`}
        </h2>
        <p>Ces chiffres viennent de la lecture à la source ; ce qui est arrivé dans Snowflake se lit dans l’onglet État.</p>
        <div className="measurement-summary__meta">
          <span>
            Lecture du <time dateTime={pipeline.observedAt}>{formatTime(pipeline.observedAt)} UTC</time>
          </span>
        </div>
      </section>
      <LiveLagChart pipeline={pipeline} current={current} />
      <details className="detail-diagnostics">
        <summary>Compteurs publiés</summary>
        <div className="detail-diagnostics__body">
          <MetricTable metrics={metrics} />
        </div>
      </details>
      <details className="detail-diagnostics">
        <summary>Position de lecture</summary>
        <div className="detail-diagnostics__body">
          <TechnicalRows rows={positionRows(pipeline)} />
        </div>
      </details>
      <details className="detail-diagnostics">
        <summary>Destination — position et compteurs</summary>
        <div className="detail-diagnostics__body">
          <TechnicalRows rows={destinationRows(pipeline)} />
        </div>
      </details>
      <details className="detail-diagnostics">
        <summary>Identité de la liaison</summary>
        <div className="detail-diagnostics__body">
          <TechnicalRows rows={fluxIdentityRows(pipeline)} />
        </div>
      </details>
      <details className="detail-diagnostics">
        <summary>Exécution et diagnostic</summary>
        <div className="detail-diagnostics__body">
          <TechnicalRows rows={runRows} />
        </div>
      </details>
      <details className="detail-diagnostics">
        <summary>Provenance et portée</summary>
        <div className="detail-diagnostics__body">
          <TechnicalRows rows={provenanceRows(pipeline)} />
        </div>
      </details>
    </>
  );
}

function TechnicalRows({ rows }: { readonly rows: ReadonlyArray<readonly [string, string]> }) {
  return (
    <dl className="technical-facts">
      {rows.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function provenanceRows(pipeline: Pipeline): ReadonlyArray<readonly [string, string]> {
  const rows: Array<readonly [string, string]> = [
    ['Origine du relevé', `service / ${pipeline.id} / ${dataNatureLabel(pipeline.quality.evidenceKind)}`],
    ['Relevé lu', timestamp(pipeline.observedAt)],
    ['Fraîcheur', freshnessLabel(pipeline.quality.freshness)],
    ['Couverture', coverageLabel(pipeline.quality.coverage)],
    ['Source déclarée', pipeline.environment.toUpperCase()],
  ];
  const plan = pipeline.fleetPlan ?? null;
  if (plan === null) {
    rows.push(['Plan de flotte', 'Non servi']);
    return rows;
  }
  rows.push(['Schéma source', plan.sourceSchema]);
  rows.push(['Namespace destination', plan.destinationNamespace]);
  rows.push(['Journal du plan', `${plan.journal.library}/${plan.journal.name}`]);
  rows.push(['Catalogue observé', timestamp(plan.observedAt)]);
  if (plan.provenance !== null && plan.provenance !== undefined) {
    rows.push([
      'Provenance du plan',
      `${plan.provenance.kind} · ${plan.provenance.catalogFormat} · ${plan.provenance.planFormat}`,
    ]);
  } else {
    rows.push(['Provenance du plan', 'Non mesurée']);
  }
  return rows;
}

/* ---------------------------------------------------------------------- */
/* Action sûre — même parcours que l'Accueil                               */

/**
 * Même parcours que l’Accueil : une commande disponible est confirmée dans le
 * dialogue, le reçu du service est affiché, puis la lecture est relancée.
 * Aucune commande n’est présentée comme disponible sans capacité publiée.
 */

function MetricTable({ metrics }: { readonly metrics: ReturnType<typeof buildCounterMetrics> }) {
  if (metrics.length === 0) return <p className="measurement-empty">Aucun compteur disponible.</p>;
  return (
    <table className="measurement-table">
      <caption>Compteurs de la dernière lecture</caption>
      <thead><tr><th scope="col">Mesure</th><th scope="col">Valeur</th></tr></thead>
      <tbody>{metrics.map((metric) => <tr key={metric.label}><th scope="row">{metric.label}</th><td>{metric.value}</td></tr>)}</tbody>
    </table>
  );
}

/* ---------------------------------------------------------------------- */
/* Verdict et portée — mêmes mots qu'avant, toujours bornés               */
/* ---------------------------------------------------------------------- */

// Le summary servi peut porter un locus qui ne correspond pas au type
// d'incident (la projection ne le vérifiait pas toujours) : le type fait foi.

/** « En direct » n'affirme une lecture courante que si la portée de preuve
 *  est live ET la fraîcheur servie fraîche — un relevé live-kind figé reste
 *  muet : les badges de portée disent déjà « Relevé trop ancien ». Les
 *  natures simulation/historique nomment la donnée, pas la fraîcheur :
 *  toujours affichées. */

function isCachedState(state: ControlPlaneState): boolean {
  return state.status === 'degraded' || state.connection === 'offline' || state.connection === 'reconnecting';
}

function useNow(tickMs = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), tickMs);
    return () => clearInterval(id);
  }, [tickMs]);
  return now;
}

function formatRows(value: number | null): string {
  return value === null ? 'Pas encore disponible' : numberFormat.format(value);
}

function formatTime(value: string): string {
  return timeFormat.format(new Date(value));
}
