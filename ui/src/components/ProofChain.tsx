import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import { stageLabel } from '../domain/pipelineDetail.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';
import {
  failClosedFrontier,
  isDocumentaryProof,
  isLiveConfirmedBreak,
  proofStageOrder,
  proofStationKicker,
  proofStations,
  type ProofStation,
} from './proofStations.ts';
import '../styles/proof-chain.css';

const evidenceTimeFormat = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
});

export function ProofChain({
  proof,
  variant,
  pipeline = null,
  onRefresh,
  selectedId = null,
  onSelectStage,
  stageButtonRef,
  showCta = true,
}: {
  readonly proof: ProofFocus;
  readonly variant: 'workspace' | 'inventory' | 'detail';
  readonly pipeline?: Pipeline | null;
  readonly onRefresh?: () => void;
  readonly selectedId?: StageId | null;
  readonly onSelectStage?: (stage: StageId) => void;
  readonly stageButtonRef?: (stage: StageId) => (node: HTMLButtonElement | null) => void;
  readonly showCta?: boolean;
}) {
  const stations = proofStations(proof, pipeline);
  const interactive = variant === 'detail' && onSelectStage !== undefined;
  const titleId = variant === 'detail' ? 'proof-object-title' : 'proof-focus-title';
  const retained = isDocumentaryProof(proof);
  const liveBreak = isLiveConfirmedBreak(proof);
  const compact = variant !== 'detail';

  return (
    <section
      className={`proof-object proof-chain proof-chain--${variant} proof-chain--${proof.proofScope.kind}${retained ? ' proof-chain--retained' : ''}${liveBreak ? ' proof-chain--live-break' : ''}${compact ? ' proof-chain--bordereau' : ''}`}
      data-scope={proof.proofScope.kind}
      data-instrument="waybill"
      aria-labelledby={titleId}
    >
      <ol className="proof-chain__stations" data-focus-stage={failClosedFrontier(proof.focus.stage)} aria-label="Chaîne de preuve Source, Capture, Raw durable, Chargement, Destination">
        {stations.map((station, index) => (
          <li
            key={station.id}
            className={stationClassName(station)}
            data-station={station.id}
            aria-current={station.isFocus ? 'step' : undefined}
            aria-label={station.isDestination && !station.isFocus ? 'État en aval' : undefined}
          >
            <span className="proof-chain__index" aria-hidden="true">{stationIndex(index)}</span>
            {station.isFocus ? (
              <article className={`proof-focus proof-focus--${proof.focus.kind}${proof.focus.stage === null ? ' proof-chain__unavailable' : ''}`}>
                <p className="proof-object__kicker proof-chain__kicker">{proofStationKicker(station)}</p>
                <FocusStation
                  proof={proof}
                  station={station}
                  titleId={titleId}
                  interactive={interactive}
                  selectedId={selectedId}
                  onSelectStage={onSelectStage}
                  stageButtonRef={stageButtonRef}
                  onRefresh={showCta ? onRefresh : undefined}
                  compact={compact}
                  showCta={showCta}
                />
              </article>
            ) : (
              <QuietStation
                proof={proof}
                station={station}
                interactive={interactive}
                selectedId={selectedId}
                onSelectStage={onSelectStage}
                stageButtonRef={stageButtonRef}
              />
            )}
            {index < stations.length - 1 ? <span className="proof-chain__link" aria-hidden="true">→</span> : null}
          </li>
        ))}
      </ol>
    </section>
  );
}

export function ProofWaybillIndex({
  focusId = null,
  confirmable = false,
}: {
  readonly focusId?: StageId | null;
  readonly confirmable?: boolean;
}) {
  const frontier = failClosedFrontier(focusId);
  const focusIndex = proofStageOrder.indexOf(frontier);
  return (
    <ol
      className={`proof-waybill-index${confirmable ? ' proof-waybill-index--confirmable' : ' proof-waybill-index--unconfirmed'}`}
      data-focus-stage={frontier}
      data-confirmable={confirmable ? 'true' : 'false'}
      data-instrument="waybill"
      aria-label="Bordereau de transport opérationnel Source, Capture, Raw durable, Chargement, Destination"
    >
      {proofStageOrder.map((id, index) => {
        const isFocus = frontier === id;
        const after = index > focusIndex;
        return (
          <li
            key={id}
            className={`proof-waybill-index__station${isFocus ? ' is-focus' : ''}${after ? ' is-after' : ''}`}
            aria-current={isFocus ? 'step' : undefined}
          >
            <span className="proof-waybill-index__index" aria-hidden="true">{stationIndex(index)}</span>
            <strong>{id === 'destination' ? 'Destination Snowflake' : stageLabel(id)}</strong>
            <small>{isFocus ? (confirmable ? 'Première rupture' : 'Première frontière') : after ? (confirmable ? 'Aval visible' : 'Non confirmable') : ''}</small>
            {index < proofStageOrder.length - 1 ? <span className="proof-waybill-index__link" aria-hidden="true">→</span> : null}
          </li>
        );
      })}
    </ol>
  );
}

function FocusStation({
  proof,
  station,
  titleId,
  interactive,
  selectedId,
  onSelectStage,
  stageButtonRef,
  onRefresh,
  compact,
  showCta,
}: {
  readonly proof: ProofFocus;
  readonly station: ProofStation;
  readonly titleId: string;
  readonly interactive: boolean;
  readonly selectedId: StageId | null;
  readonly onSelectStage?: (stage: StageId) => void;
  readonly stageButtonRef?: (stage: StageId) => (node: HTMLButtonElement | null) => void;
  readonly onRefresh?: () => void;
  readonly compact: boolean;
  readonly showCta: boolean;
}) {
  const title = proof.focus.stage === null
    ? proof.focus.label
    : station.isDestination ? 'Destination Snowflake' : proof.focus.label;

  return (
    <>
      <h2 id={titleId} tabIndex={interactive ? -1 : undefined}>{title}</h2>
      <p className="proof-focus__cause proof-chain__cause">{station.summary}</p>
      {compact ? null : <p className="proof-focus__evidence proof-chain__evidence">{proof.focus.evidence}</p>}
      {interactive && onSelectStage ? (
        <button
          ref={stageButtonRef?.(station.id)}
          className="page-action page-action--primary proof-object__focus-action"
          type="button"
          aria-expanded={selectedId === station.id}
          onClick={() => onSelectStage(station.id)}
        >{inspectLabel(station.id)}</button>
      ) : showCta ? <ProofCta proof={proof} onRefresh={onRefresh} /> : null}
    </>
  );
}

function QuietStation({
  proof,
  station,
  interactive,
  selectedId,
  onSelectStage,
  stageButtonRef,
}: {
  readonly proof: ProofFocus;
  readonly station: ProofStation;
  readonly interactive: boolean;
  readonly selectedId: StageId | null;
  readonly onSelectStage?: (stage: StageId) => void;
  readonly stageButtonRef?: (stage: StageId) => (node: HTMLButtonElement | null) => void;
}) {
  const destination = station.isDestination ? proof.downstream : null;
  const name = station.isDestination ? (destination?.label ?? 'Destination Snowflake') : station.label;

  return (
    <>
      <p className="proof-object__kicker proof-chain__kicker">{proofStationKicker(station)}</p>
      {interactive && onSelectStage ? (
        <button
          ref={stageButtonRef?.(station.id)}
          className={station.isDestination ? 'proof-chain__destination-name' : 'proof-stage-link'}
          type="button"
          aria-label={inspectLabel(station.id)}
          aria-expanded={selectedId === station.id}
          onClick={() => onSelectStage(station.id)}
        >{name}</button>
      ) : (
        <strong className="proof-chain__name">{name}</strong>
      )}
      <p className="proof-chain__summary">{station.summary}</p>
      {destination?.observedAt ? (
        <time dateTime={destination.observedAt}>
          {evidenceTimeFormat.format(new Date(destination.observedAt))} UTC
        </time>
      ) : null}
    </>
  );
}

function ProofCta({ proof, onRefresh }: { readonly proof: ProofFocus; readonly onRefresh?: () => void }) {
  const className = 'page-action page-action--primary';
  if (proof.cta.kind === 'link') {
    return <a className={className} href={proof.cta.href}>{proof.cta.label}</a>;
  }
  return (
    <button className={className} type="button" onClick={onRefresh}>{proof.cta.label}</button>
  );
}

function inspectLabel(id: StageId): string {
  switch (id) {
    case 'source': return 'Inspecter la source';
    case 'capture': return 'Inspecter la capture';
    case 'raw': return 'Inspecter le raw durable';
    case 'load': return 'Inspecter le chargement';
    case 'destination': return 'Inspecter la destination';
  }
}

function stationClassName(station: ProofStation): string {
  const destination = station.isDestination ? ' proof-chain__station--destination proof-object__destination' : '';
  const pending = station.afterBoundary ? ' proof-chain__station--after' : '';
  if (station.isFocus) {
    return `proof-chain__station proof-chain__station--${station.kind} proof-chain__station--focus proof-object__focus${destination}`;
  }
  return `proof-chain__station proof-chain__station--${station.kind}${pending}${destination}`;
}

function stationIndex(index: number): string {
  return String(index + 1).padStart(2, '0');
}
