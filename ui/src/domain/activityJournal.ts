import type {
  FleetRuntimePhase,
  Freshness,
  IncidentType,
  Overview,
  Pipeline,
  PipelineStatus,
  StageStatus,
} from './controlPlane.ts';
import { runtimePhaseLabel } from './fleetView.ts';
import { sequences } from './format.ts';
import {
  incidentCause,
  incidentHeadline,
  journalRows,
  observationRows,
  resumeReadiness,
  sectionAlert,
} from './liveBoard.ts';
import { freshnessLabel } from './pipelineDetail.ts';
import { statusCopy } from '../router.ts';

/**
 * Journal d'activité de l'écran Activité (#/incidents).
 *
 * L'API ne sert pas de timeline d'événements native : le journal est
 * entièrement « déduit des relevés », et chaque entrée porte son origine :
 *
 * - `mesuré` — événements significatifs lus dans le relevé courant :
 *   incident déclaré, arrêt planifié, reprise observée, jalons d'étapes et
 *   catalogue datés par le service (via `observationRows`/`journalRows` de
 *   liveBoard — dates servies, rien d'inventé) ;
 * - `déduit` — transitions calculées en comparant les révisions reçues
 *   pendant la session d'écran : incident apparu / modifié / résolu,
 *   changement de statut, de fraîcheur du relevé, de phase d'exécution,
 *   d'étape de lecture.
 *
 * Chaque entrée porte : heure, type, source, verdict, détail — et l'étiquette
 * d'origine « mesuré » / « déduit » rendue à l'écran.
 */

export type ActivityTone = 'attention' | 'active' | 'muted';

export interface ActivityEntry {
  /** Instant de l'événement — servi pour les entrées mesurées, instant de
   *  génération du relevé pour les transitions déduites. */
  readonly at: string;
  /** Type affiché : Incident, Arrêt planifié, Reprise, Étape, Catalogue,
   *  Relevé, Statut, Phase, Session. */
  readonly type: string;
  /** Connexion qui porte l'événement. */
  readonly source: string;
  /** Issue courte : « en cours », « constaté », « apparu », « résolu »… */
  readonly verdict: string;
  readonly label: string;
  readonly detail: string | null;
  /** « mesuré » : servi par le relevé — « déduit » : calculé entre révisions. */
  readonly origin: 'mesuré' | 'déduit';
  readonly tone: ActivityTone;
}

/** Événements significatifs servis par le relevé courant — incident déclaré,
 *  arrêt planifié, reprise, jalons datés (étapes, catalogue, relevé lu). Ce
 *  sont des faits servis : origine « mesuré ». `held` qualifie le verdict
 *  quand le relevé est figé ou conservé — « constaté · figé », jamais
 *  présenté comme une mesure live. */
export function snapshotActivityEvents(pipeline: Pipeline, held: boolean): readonly ActivityEntry[] {
  const entries: ActivityEntry[] = [];

  if (pipeline.incident !== null) {
    const alert = sectionAlert(pipeline, { transport: 'live', retained: null });
    if (alert !== null) {
      entries.push({
        at: alert.onset?.at ?? alert.observedAt,
        type: 'Incident',
        source: pipeline.id,
        verdict: held ? 'constaté · figé' : 'en cours',
        label: alert.headline,
        detail:
          alert.remedy !== null ? `${alert.cause} — ${alert.remedy}` : alert.cause,
        origin: 'mesuré',
        tone: 'attention',
      });
    }
  }

  const captureStage = pipeline.stages.find((stage) => stage.id === 'capture') ?? null;
  if (pipeline.status === 'planned_stop' || captureStage?.status === 'planned_stop') {
    entries.push({
      at: captureStage?.observedAt ?? pipeline.observedAt,
      type: 'Arrêt planifié',
      source: pipeline.id,
      verdict: 'observé',
      label: 'Lecture mise en pause — arrêt demandé',
      detail: 'rien n’indique un incident',
      origin: 'mesuré',
      tone: 'muted',
    });
  }

  if (pipeline.status === 'recovering') {
    entries.push({
      at: pipeline.observedAt,
      type: 'Reprise',
      source: pipeline.id,
      verdict: held ? 'reprise constatée · figé' : 'reprise en cours',
      label: 'Rétablissement en cours',
      detail: 'une lecture complète reste requise pour confirmer',
      origin: 'mesuré',
      tone: 'active',
    });
  }

  // Jalons datés servis par le relevé — réutilise journalRows de liveBoard
  // avec une session vide : les observations gardent leur étiquette
  // (« étape figée » quand le relevé est retenu).
  for (const row of journalRows([], observationRows(pipeline), held)) {
    entries.push({
      at: row.at,
      type:
        row.tag.startsWith('étape')
          ? 'Étape'
          : row.tag.startsWith('catalogue')
            ? 'Catalogue'
            : 'Relevé',
      source: pipeline.id,
      verdict: row.tag.endsWith('figé') ? 'constaté · figé' : 'constaté',
      label: row.label,
      detail: null,
      origin: 'mesuré',
      tone: 'muted',
    });
  }

  return entries;
}

/* ------------------------------------------------------------------ */
/* Journal de session — transitions déduites entre révisions reçues     */
/* ------------------------------------------------------------------ */

interface ActivitySignature {
  readonly status: PipelineStatus;
  readonly incident: IncidentType | '';
  readonly freshness: Freshness;
  readonly phase: FleetRuntimePhase | '';
  readonly capture: StageStatus | '';
  /** true quand l'incident est retenu avec sa cause vérifiée résolue —
   *  l'entrée dit « Prête à reprendre » au lieu du titre brut d'incident. */
  readonly ready: boolean;
}

export interface ActivityLog {
  readonly revision: number;
  readonly signature: string;
  readonly parts: ActivitySignature;
  /** Relevés reçus pour cette connexion depuis l'ouverture de l'écran. */
  readonly received: number;
  readonly entries: readonly ActivityEntry[];
}

function signatureParts(pipeline: Pipeline): ActivitySignature {
  return {
    status: pipeline.status,
    incident: pipeline.incident?.type ?? '',
    freshness: pipeline.quality.freshness,
    phase: pipeline.fleetRuntime?.phase ?? '',
    capture: pipeline.stages.find((stage) => stage.id === 'capture')?.status ?? '',
    ready: resumeReadiness(pipeline) === 'ready',
  };
}

function signatureOf(parts: ActivitySignature): string {
  return [parts.status, parts.incident, parts.freshness, parts.phase, parts.capture, parts.ready].join('|');
}

function statusWord(status: PipelineStatus): string {
  return statusCopy(status).label;
}

/** Libellé d'état d'un relevé : le titre de l'incident quand il est typé,
 *  « Un incident est signalé » pour un statut incident sans type servi —
 *  le vocabulaire de l'écran Activité, pas la pilule « Incident actif ». */
function stateLabel(parts: ActivitySignature): string {
  if (parts.ready === true) return 'Prête à reprendre';
  if (parts.incident !== '') return incidentHeadline(parts.incident);
  if (parts.status === 'incident') return 'Un incident est signalé';
  return statusWord(parts.status);
}

function stageWord(status: StageStatus | ''): string {
  return status === '' ? 'non observée' : statusCopy(status).label;
}

function phaseWord(phase: FleetRuntimePhase | ''): string {
  return phase === '' ? 'non projetée' : runtimePhaseLabel(phase);
}

function statusTone(status: PipelineStatus): ActivityTone {
  return status === 'incident' || status === 'awaiting_resume'
    ? 'attention'
    : status === 'recovering'
      ? 'active'
      : 'muted';
}

function freshnessTransitionLabel(previous: Freshness, next: Freshness): string {
  if (next === 'stale') return 'Relevé devenu trop ancien';
  if (next === 'clock_untrusted') return 'Horloge du relevé non fiable';
  if (next === 'fresh') return 'Relevé redevenu courant';
  return `Fraîcheur du relevé : ${freshnessLabel(previous)} → ${freshnessLabel(next)}`;
}

/** Les transitions d'une révision à l'autre — « incident apparu », « incident
 *  résolu », « statut : X → Y ». Quand l'incident change, le changement de
 *  statut qui l'accompagne est couvert par l'entrée incident — pas doublé. */
function transitionEntries(
  previous: ActivitySignature,
  next: ActivitySignature,
  at: string,
  source: string,
): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  const push = (
    type: string,
    verdict: string,
    label: string,
    detail: string | null,
    tone: ActivityTone,
  ): void => {
    entries.push({ at, type, source, verdict, label, detail, origin: 'déduit', tone });
  };

  if (previous.incident !== next.incident) {
    if (next.incident === '') {
      push('Incident', 'résolu', 'Incident résolu — plus signalé dans le relevé', null, 'active');
    } else if (previous.incident === '') {
      push(
        'Incident',
        'apparu',
        `Incident apparu — ${incidentHeadline(next.incident)}`,
        incidentCause(next.incident),
        'attention',
      );
    } else {
      push(
        'Incident',
        'modifié',
        `Incident modifié — ${incidentHeadline(next.incident)}`,
        incidentCause(next.incident),
        'attention',
      );
    }
  }
  if (previous.status !== next.status && previous.incident === next.incident) {
    push(
      'Statut',
      'déduit',
      `Statut : ${stateLabel(previous)} → ${stateLabel(next)}`,
      null,
      statusTone(next.status),
    );
  }
  if (previous.ready !== next.ready) {
    push(
      'Incident',
      next.ready ? 'cause résolue' : 'reprise non confirmée',
      next.ready
        ? 'Cause de l’incident vérifiée résolue — la capture attend le relancement'
        : 'Reprise non confirmée — la vérification de la source est trop ancienne',
      null,
      'attention',
    );
  }
  if (previous.freshness !== next.freshness) {
    push(
      'Relevé',
      'déduit',
      freshnessTransitionLabel(previous.freshness, next.freshness),
      null,
      next.freshness === 'fresh' ? 'active' : 'muted',
    );
  }
  if (previous.phase !== next.phase) {
    push(
      'Phase',
      'déduit',
      `Phase d’exécution : ${phaseWord(previous.phase)} → ${phaseWord(next.phase)}`,
      null,
      'muted',
    );
  }
  if (previous.capture !== next.capture && previous.status === next.status) {
    push(
      'Étape',
      'déduit',
      `Étape Lecture : ${stageWord(previous.capture)} → ${stageWord(next.capture)}`,
      null,
      'muted',
    );
  }
  return entries;
}

/** Entrées de session pour un relevé : le premier relevé reçu, puis une
 *  entrée par transition déduite entre révisions. Un relevé identique à la
 *  signature précédente n'ajoute aucune ligne — la continuité est dite par
 *  le compteur « N relevés reçus », pas par des lignes répétées. Idempotent
 *  sous double rendu : la révision évite le doublon. */
export function activitySessionEntries(
  log: Map<string, ActivityLog>,
  pipeline: Pipeline,
  overview: Pick<Overview, 'revision' | 'generatedAt'>,
): readonly ActivityEntry[] {
  const parts = signatureParts(pipeline);
  const signature = signatureOf(parts);
  const previous = log.get(pipeline.id);
  if (previous !== undefined && previous.revision === overview.revision) {
    return previous.entries;
  }
  const received = (previous?.received ?? 0) + 1;

  let entries: readonly ActivityEntry[];
  if (previous === undefined) {
    entries = [
      {
        at: overview.generatedAt,
        type: 'Session',
        source: pipeline.id,
        verdict: 'reçu',
        label: `Premier relevé de la session — ${stateLabel(parts)}`,
        detail: null,
        origin: 'déduit',
        tone: 'muted',
      },
    ];
  } else if (previous.signature === signature) {
    entries = previous.entries;
  } else {
    entries = [
      ...transitionEntries(previous.parts, parts, overview.generatedAt, pipeline.id),
      ...previous.entries,
    ].slice(0, 12);
  }

  log.set(pipeline.id, { revision: overview.revision, signature, parts, received, entries });
  return entries;
}

/** Fusionne événements mesurés et transitions déduites de toutes les
 *  connexions — tri strict décroissant, dédupliqué, plafonné. À instant
 *  identique, les faits significatifs (incident, arrêt, reprise) passent
 *  avant les transitions, puis les jalons datés. */
export function mergeActivityEntries(
  perPipeline: readonly {
    readonly pipeline: Pipeline;
    readonly session: readonly ActivityEntry[];
    readonly events: readonly ActivityEntry[];
  }[],
  cap = 16,
): readonly ActivityEntry[] {
  const rank = (entry: ActivityEntry): number =>
    entry.type === 'Incident'
      ? 0
      : entry.type === 'Arrêt planifié' || entry.type === 'Reprise'
        ? 1
        : entry.origin === 'déduit'
          ? 2
          : 3;
  const rows = perPipeline.flatMap(({ session, events }) => [...events, ...session]);
  rows.sort((a, b) => {
    const delta = Date.parse(b.at) - Date.parse(a.at);
    if (delta !== 0) return delta;
    const order = rank(a) - rank(b);
    if (order !== 0) return order;
    return a.source.localeCompare(b.source) || a.label.localeCompare(b.label);
  });
  const seen = new Set<string>();
  return rows
    .filter((row) => {
      const key = `${row.at}|${row.type}|${row.source}|${row.label}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .slice(0, cap);
}

/** Méta du journal — la preuve mesurée que la boucle tourne, affichée sous
 *  le titre : « révision 298 · générée il y a 2 s · 214 relevés reçus ». */
export function activityJournalMeta(revision: number, generatedAge: string, received: number): string {
  return `révision ${sequences(revision)} · générée ${generatedAge} · ${sequences(received)} relevé${received > 1 ? 's' : ''} reçu${received > 1 ? 's' : ''}`;
}
