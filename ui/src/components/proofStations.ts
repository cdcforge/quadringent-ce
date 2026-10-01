import type { Pipeline, StageId } from '../domain/controlPlane.ts';
import { formatDuration, sequences } from '../domain/format.ts';
import { stageLabel } from '../domain/pipelineDetail.ts';
import type { ProofFocus } from '../domain/proofFocus.ts';

export const proofStageOrder = ['source', 'capture', 'raw', 'load', 'destination'] as const;

export function failClosedFrontier(stage: StageId | null | undefined): StageId {
  return stage ?? 'source';
}

export const afterBoundaryCopy = 'Non confirmé après la première rupture';

export type ProofStationKind = 'established' | 'break' | 'completion' | 'pending' | 'retained' | 'unavailable';

export interface ProofStation {
  readonly id: StageId;
  readonly label: string;
  readonly kind: ProofStationKind;
  readonly isFocus: boolean;
  readonly isDestination: boolean;
  readonly afterBoundary: boolean;
  readonly summary: string;
}

export function proofStations(proof: ProofFocus, pipeline: Pipeline | null = null): readonly ProofStation[] {
  const focusIndex = proof.focus.stage ? proofStageOrder.indexOf(proof.focus.stage) : 0;
  const stagedFocus = proof.focus.stage !== null;

  return proofStageOrder.map((id, index) => {
    const isDestination = id === 'destination';
    const isFocus = stagedFocus ? proof.focus.stage === id : index === 0;
    const afterBoundary = stagedFocus ? index > focusIndex : index > 0;
    const kind = stationKind(proof, id, isFocus, afterBoundary, isDestination);
    return {
      id,
      label: stageLabel(id),
      kind,
      isFocus,
      isDestination,
      afterBoundary,
      summary: stationSummary(proof, pipeline, id, kind, isFocus, afterBoundary),
    };
  });
}

export interface ProofChamber {
  readonly upstream: readonly ProofStation[];
  readonly boundary: ProofStation | null;
  readonly arrival: ProofStation | null;
}

export function proofChamber(proof: ProofFocus, pipeline: Pipeline | null = null): ProofChamber {
  const stations = proofStations(proof, pipeline);
  const boundary = stations.find((station) => station.isFocus) ?? null;
  const upstream = stations.filter((station) => station.kind === 'established' && !station.isFocus);
  const arrival = boundary?.isDestination ? null : stations.find((station) => station.isDestination) ?? null;
  return { upstream, boundary, arrival };
}

export function isDocumentaryProof(proof: ProofFocus): boolean {
  return proof.proofScope.kind !== 'live';
}

export function isLiveConfirmedBreak(proof: ProofFocus): boolean {
  return proof.proofScope.kind === 'live' && proof.focus.kind === 'break';
}

export const proofStationRegister: Readonly<Record<StageId, string>> = {
  source: 'Disponibilité source',
  capture: 'Reprise de lecture',
  raw: 'Réception durable',
  load: 'Copie initiale',
  destination: 'Arrivée confirmée',
};

export function proofStationKicker(station: ProofStation): string {
  if (station.isFocus && station.kind === 'break') return 'Premier point non confirmé';
  if (station.isFocus && station.kind === 'completion') return 'Point de livraison';
  if (station.isFocus && station.kind === 'unavailable') return 'Étape indisponible';
  if (station.kind === 'established') return 'Établi';
  if (station.kind === 'retained') return 'Observation retenue';
  if (station.afterBoundary) return 'Non confirmé';
  if (station.isDestination) return 'Destination';
  return proofStationRegister[station.id];
}

export function captureBoardFact(pipeline: Pipeline): string {
  return captureFacts(pipeline) || 'Lecture non établie';
}

export function captureStateBoardFact(pipeline: Pipeline | null): string {
  return stageOf(pipeline, 'capture')?.headline?.trim() || 'État de lecture non établi';
}

export function captureStoppedByBudget(pipeline: Pipeline | null): boolean {
  return /arrêtée par budget/i.test(stageOf(pipeline, 'capture')?.headline ?? '');
}

export function measureSubordination(pipeline: Pipeline): string | null {
  if (captureStoppedByBudget(pipeline)) {
    return 'Lecture arrêtée par budget, pas un signal de santé courant';
  }
  const zeroLag = pipeline.lagSequences === 0 || pipeline.lagSeconds === 0;
  if (zeroLag && pipeline.quality.evidenceKind !== 'live') {
    return 'Relevé antérieur, pas un signal de santé courant';
  }
  return null;
}

export function lagBoardFact(pipeline: Pipeline | null): string {
  if (!pipeline) return 'retard non établi';
  if (pipeline.lagSequences !== null) return qualifyZeroLag(pipeline, `retard de ${sequences(pipeline.lagSequences)} ${enregistrementsLabel(pipeline.lagSequences)}`);
  return lagFact(pipeline);
}

export interface CaptureVolumeParts {
  readonly events: string;
  readonly bytes: string;
  readonly windows: string;
  readonly qualification: string;
}

export const captureVolumeQualification = 'lu à la source, pas à destination';

export function captureVolumeParts(pipeline: Pipeline | null): CaptureVolumeParts {
  const events = observedCounter(pipeline, 'events_published');
  const bytes = observedCounter(pipeline, 'payload_bytes') ?? observedCounter(pipeline, 'payload_bytes_published');
  const windows = observedCounter(pipeline, 'windows_published');
  return {
    events: events === null ? 'événements lus non établis' : `${sequences(events)} événements`,
    bytes: bytes === null ? 'octets non établis' : `${sequences(bytes)} octets`,
    windows: windows === null ? 'lots non établis' : `${sequences(windows)} lots`,
    qualification: captureVolumeQualification,
  };
}

export function captureVolumesBoardFact(pipeline: Pipeline | null): string {
  const parts = captureVolumeParts(pipeline);
  return joinClauses([parts.events, parts.bytes, parts.windows, parts.qualification], ' · ');
}

export function windowsBoardFact(pipeline: Pipeline): string {
  const published = observedCounter(pipeline, 'windows_published');
  if (published !== null) return `${sequences(published)} lots`;
  const chain = pipeline.windowDelivery?.chain;
  if (chain) return `${sequences(chain.matchedWindows)} / ${sequences(chain.declaredWindows)} lots`;
  return 'Lots non établis';
}

export function destinationBoardFact(proof: ProofFocus, pipeline: Pipeline): string {
  return destinationFacts(proof, pipeline);
}

export function destinationLimitFact(proof: ProofFocus | null, pipeline: Pipeline | null): string {
  if (!proof) return 'Destination non confirmée. Limite de la mesure.';
  return joinClauses([destinationFacts(proof, pipeline), 'Limite de la mesure']);
}

function stationKind(
  proof: ProofFocus,
  id: StageId,
  isFocus: boolean,
  afterBoundary: boolean,
  isDestination: boolean,
): ProofStationKind {
  if (isFocus) {
    if (proof.focus.kind === 'completion') return 'completion';
    if (proof.focus.kind === 'unavailable') return 'unavailable';
    return 'break';
  }
  if (proof.upstream.stages.includes(id)) return 'established';
  if (isDestination && proof.downstream?.status === 'retained-observation') return 'retained';
  if (afterBoundary) return 'pending';
  if (proof.focus.kind === 'unavailable') return 'unavailable';
  return 'pending';
}

function stationSummary(
  proof: ProofFocus,
  pipeline: Pipeline | null,
  id: StageId,
  kind: ProofStationKind,
  isFocus: boolean,
  afterBoundary: boolean,
): string {
  const facts = factsFor(id, proof, pipeline, kind);
  if (isFocus) return joinClauses([proof.focus.cause, facts]);
  if (afterBoundary) {
    const trailing = facts || (id === 'destination' ? destinationFacts(proof, pipeline) : 'Non confirmé');
    return joinClauses([afterBoundaryCopy, trailing]);
  }
  return facts || 'Confirmé dans ce relevé';
}

function factsFor(id: StageId, proof: ProofFocus, pipeline: Pipeline | null, kind: ProofStationKind): string {
  switch (id) {
    case 'source': return sourceFacts(pipeline);
    case 'capture': return captureFacts(pipeline);
    case 'raw': return rawFacts(pipeline, kind);
    case 'load': return loadFacts(proof, pipeline, kind);
    case 'destination': return destinationFacts(proof, pipeline);
  }
}

function sourceFacts(pipeline: Pipeline | null): string {
  const headline = stageOf(pipeline, 'source')?.headline?.trim();
  return headline ?? '';
}

function captureFacts(pipeline: Pipeline | null): string {
  if (!pipeline) return '';
  const headline = stageOf(pipeline, 'capture')?.headline?.trim() ?? '';
  return joinClauses([headline, lagFact(pipeline)]);
}

function lagFact(pipeline: Pipeline): string {
  if (pipeline.lagSeconds !== null) return qualifyZeroLag(pipeline, `retard de ${formatDuration(pipeline.lagSeconds)}`);
  if (pipeline.lagSequences !== null) return qualifyZeroLag(pipeline, `retard de ${sequences(pipeline.lagSequences)} ${enregistrementsLabel(pipeline.lagSequences)}`);
  return 'retard non établi';
}

function enregistrementsLabel(count: number): string {
  return count === 1 ? 'enregistrement' : 'enregistrements';
}

function qualifyZeroLag(pipeline: Pipeline, fact: string): string {
  if (!/\bretard de 0\b/.test(fact)) return fact;
  const captureHeadline = stageOf(pipeline, 'capture')?.headline ?? '';
  if (/arrêtée par budget/i.test(captureHeadline)) {
    return `${fact} · subordonné à la lecture arrêtée par budget, pas un signal de santé courant`;
  }
  if (pipeline.quality.evidenceKind === 'historical' || pipeline.quality.evidenceKind === 'simulation') {
    return `${fact} · relevé antérieur, pas un signal de santé courant`;
  }
  return fact;
}

function rawFacts(pipeline: Pipeline | null, kind: ProofStationKind): string {
  const headline = stageOf(pipeline, 'raw')?.headline?.trim() ?? '';
  if (kind === 'established') return headline || 'Réception confirmée';
  if (kind === 'break' || kind === 'unavailable' || kind === 'completion') return headline;
  return 'Non confirmé';
}

function loadFacts(proof: ProofFocus, pipeline: Pipeline | null, kind: ProofStationKind): string {
  const historical = proof.proofScope.kind === 'historical' || pipeline?.quality.evidenceKind === 'historical';
  const headline = stageOf(pipeline, 'load')?.headline?.trim() ?? '';
  const established = kind === 'established';
  if (established) return joinClauses([historical ? 'Relevé antérieur' : '', headline]);
  return joinClauses([historical ? 'Relevé antérieur' : '', 'Non confirmé']);
}

function destinationFacts(proof: ProofFocus, pipeline: Pipeline | null): string {
  const events = observedCounter(pipeline, 'events_in_target');
  const eventsPart = events === null ? '' : `${sequences(events)} événements arrivés`;
  if (proof.downstream?.status === 'retained-observation') {
    return joinClauses([proof.downstream.detail, eventsPart]);
  }
  const absent = proof.downstream
    ? proof.downstream.detail
    : proof.focus.stage === 'destination'
      ? proof.focus.cause
      : 'Non confirmée dans le relevé actuel.';
  return joinClauses([absent, eventsPart]);
}

function stageOf(pipeline: Pipeline | null, id: StageId) {
  return pipeline?.stages.find((stage) => stage.id === id);
}

export function observedCounter(pipeline: Pipeline | null | undefined, key: string): number | null {
  if (!pipeline) return null;
  const value = pipeline.counters[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function joinClauses(parts: readonly (string | null | undefined)[], separator = '. '): string {
  const clauses = parts
    .map((part) => part?.replace(/\s+/g, ' ').trim())
    .filter((part): part is string => Boolean(part));
  if (!clauses.length) return '';
  if (separator === '. ') {
    return clauses
      .map((clause, index) => (index === 0 || /[.!?…]$/.test(clauses[index - 1]!) ? clause : clause))
      .reduce((acc, clause, index) => {
        if (index === 0) return clause;
        return acc.endsWith('.') ? `${acc} ${clause}` : `${acc}. ${clause}`;
      }, '');
  }
  return clauses.join(separator);
}
