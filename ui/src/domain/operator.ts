/**
 * Couche opérateur — la seule couche autorisée à produire du texte d'écran.
 *
 * Elle traduit la projection du service en quatre réponses, dans cet ordre :
 *
 *   1. quel état          → `health` + `stateLabel` (une couleur, un mot)
 *   2. quel fait chiffré  → `facts` (trois au maximum, mesurés ou tus)
 *   3. quoi faire         → `guidance` + `actions`
 *   4. sur quelle preuve  → `evidence` (relégué en profondeur)
 *
 * Trois règles tiennent ce module :
 *
 * - Ce qui n'est pas mesuré n'est pas affiché. Une valeur absente vaut `null`
 *   et se rend « Non mesuré » ; elle ne devient jamais 0, ni une estimation,
 *   ni un libellé technique de repli.
 * - Ce qui est interne n'est pas traduit, il est tu. Les compteurs de boucle
 *   du lecteur (sondages, balayages à vide, rotations) ne deviennent pas
 *   « lectures à vide » : ils ne remontent pas jusqu'ici.
 * - Une action n'est offerte que si le service la déclare disponible. Une
 *   capacité indisponible ne produit ni bouton, ni injonction : elle produit
 *   au plus une consigne qui dit où l'acte se fait réellement.
 */

import type { EvidenceKind, FleetCapability, Pipeline, PipelineStatus } from './controlPlane.ts';
import type { ActionId } from '../data/controlPlaneClient.ts';
import { age, sequences } from './format.ts';
import type { PipelineActionReceipt } from '../data/controlPlaneClient.ts';
import type { ControlActionKind, ControlLevel } from './controlsPanel.ts';

/** Santé d'une liaison — ce que la couleur porte, et rien d'autre. */
export type Health = 'ok' | 'attention' | 'stopped' | 'unknown';

/** Un fait chiffré. `value === null` signifie « mesure absente », jamais zéro. */
export interface Fact {
  readonly label: string;
  readonly value: string | null;
  /** Précision courte, affichée sous la valeur. Omise si elle répète le label. */
  readonly note?: string;
}

/** Une action réellement exécutable : le service l'a déclarée disponible. */
export interface ActionOffer {
  readonly id: ActionId;
  readonly label: string;
  /** Ce que l'action fait, en une phrase, pour la confirmation. */
  readonly effect: string;
}

export interface LiaisonView {
  readonly id: string;
  /** Titre affiché. Reste l'identifiant tant que le service n'en publie pas d'autre. */
  readonly name: string;
  /** Où les données arrivent, en clair. `null` si le service ne le dit pas. */
  readonly destination: string | null;
  readonly health: Health;
  /** L'état en deux mots au plus. */
  readonly stateLabel: string;
  /** La phrase principale : ce qui se passe, sans jargon. */
  readonly headline: string;
  /** Ce que l'opérateur doit faire. `null` s'il n'y a rien à faire. */
  readonly guidance: string | null;
  readonly facts: readonly Fact[];
  readonly actions: readonly ActionOffer[];
  /** Âge du relevé, en clair. */
  readonly freshness: string;
  /** Vrai quand le relevé est trop vieux pour qu'on affirme quoi que ce soit. */
  readonly outdated: boolean;
  /**
   * Avertissement sur la nature de ce qui est affiché, ou `null` quand il
   * s'agit d'une lecture réelle et courante.
   *
   * Une démonstration et un rejeu d'historique se rendent à l'écran comme une
   * mesure : rien ne les distingue d'un relevé du jour. Un exploitant qui
   * prend une décision sur un écran de simulation ne s'en rend compte qu'après.
   */
  readonly caveat: string | null;
}

const MEASURE_ABSENT = 'Non mesuré';

/** Rend une valeur de fait. Le seul endroit où l'absence prend un mot. */
export function factValue(fact: Fact): string {
  return fact.value ?? MEASURE_ABSENT;
}

/* ------------------------------------------------------------------ */
/* État                                                                */
/* ------------------------------------------------------------------ */

interface StateCopy {
  readonly health: Health;
  readonly label: string;
}

/**
 * L'état du service devient un état d'opérateur.
 *
 * `awaiting_resume` est délibérément rendu « Lecture arrêtée » et non « Prête
 * à reprendre » : du point de vue de l'exploitant, le fait qui compte est que
 * plus rien n'est lu. Que la cause soit levée appartient à la phrase, pas à
 * l'état — sans quoi l'écran a l'air d'aller bien alors qu'il ne lit plus.
 */
export function stateCopy(status: PipelineStatus): StateCopy {
  switch (status) {
    case 'healthy':
      return { health: 'ok', label: 'À jour' };
    case 'recovering':
      return { health: 'attention', label: 'Rattrapage' };
    case 'degraded':
      return { health: 'attention', label: 'À surveiller' };
    case 'incident':
      return { health: 'stopped', label: 'Interrompue' };
    case 'planned_stop':
      return { health: 'stopped', label: 'Arrêt volontaire' };
    case 'awaiting_resume':
      return { health: 'stopped', label: 'Lecture arrêtée' };
    case 'unknown':
      return { health: 'unknown', label: 'État inconnu' };
  }
}

/* ------------------------------------------------------------------ */
/* Phrase principale                                                   */
/* ------------------------------------------------------------------ */

/**
 * Durée au registre de la conversation : « 2 jours », pas « 44 h 32 ».
 *
 * L'arrondi est délibéré et n'entame pas la règle de vérité : il porte sur un
 * écart de temps, jamais sur une mesure publiée par le service. La durée
 * exacte reste lisible à l'endroit qui la prouve — l'onglet Mesures.
 */
export function approximateDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return 'moins d’une minute';
  const minutes = Math.round(s / 60);
  if (minutes < 60) return minutes === 1 ? '1 minute' : `${minutes} minutes`;
  const hours = Math.round(s / 3600);
  if (hours < 24) return hours === 1 ? '1 heure' : `${hours} heures`;
  const days = Math.round(s / 86400);
  if (days < 7) return days === 1 ? '1 jour' : `${days} jours`;
  const weeks = Math.round(s / 604800);
  return weeks === 1 ? '1 semaine' : `${weeks} semaines`;
}

/** Depuis combien de temps la lecture est interrompue, en clair. */
function stoppedSince(pipeline: Pipeline, now: Date): string | null {
  const at = pipeline.incident?.declaredAt ?? pipeline.run?.diagnostic?.at ?? null;
  if (!at) return null;
  const seconds = (now.getTime() - new Date(at).getTime()) / 1000;
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return approximateDuration(seconds);
}

/**
 * La phrase que l'opérateur lit en premier.
 *
 * Elle décrit une situation, jamais un mécanisme : ni position de lecture, ni
 * nom de tâche, ni état de protocole. Quand la cause d'un arrêt est levée, la
 * phrase le dit — c'est ce qui distingue une panne en cours d'un arrêt qui
 * n'attend plus que d'être relancé.
 */
export function headlineFor(pipeline: Pipeline, now: Date): string {
  const { status } = pipeline;
  const since = stoppedSince(pipeline, now);

  if (status === 'awaiting_resume') {
    const base = since ? `Arrêtée depuis ${since}.` : 'La lecture est arrêtée.';
    return pipeline.incident?.causeResolved
      ? `${base} La connexion à la source fonctionne de nouveau.`
      : base;
  }
  if (status === 'incident') {
    return since ? `Interrompue depuis ${since}.` : 'La lecture est interrompue.';
  }
  if (status === 'planned_stop') {
    return since ? `Arrêtée volontairement depuis ${since}.` : 'Arrêtée volontairement.';
  }
  if (status === 'recovering') return 'La lecture a repris et rattrape son retard.';
  if (status === 'degraded') return 'La copie avance, mais une vérification manque.';
  if (status === 'unknown') return 'Le dernier relevé ne permet pas de conclure.';
  return 'La copie suit la source.';
}

/* ------------------------------------------------------------------ */
/* Consigne                                                            */
/* ------------------------------------------------------------------ */

/**
 * Ce que l'opérateur doit faire, quand il ne peut pas le faire ici.
 *
 * Cette fonction n'est appelée que si aucune action n'est disponible. Elle ne
 * donne jamais un ordre en l'air : soit elle dit où l'acte se fait, soit elle
 * se tait. Une consigne sans destinataire vaut moins que pas de consigne.
 */
export function guidanceFor(pipeline: Pipeline, offered: readonly ActionOffer[]): string | null {
  if (offered.length > 0) return null;
  const { status } = pipeline;

  if (status === 'awaiting_resume' || status === 'incident') {
    // Le service dit pourquoi la reprise n'est pas offerte. La distinction
    // porte : « ce déploiement ne sait pas relancer » appelle l'exploitation,
    // « les vérifications ne sont pas réunies » appelle l'attente.
    const reason = pipeline.fleetRuntime?.capabilities?.resume?.reason ?? null;
    if (reason === 'resume_not_ready') {
      return 'La reprise n’est pas encore sûre : les vérifications de reprise ne sont pas toutes réunies. Cet écran l’indiquera dès qu’elles le seront.';
    }
    if (reason === 'not_pausable_in_phase') {
      // Le produit sait relancer, mais pas dans l'état où se trouve la copie.
      // Renvoyer vers l'exploitation serait faux : il n'y a rien à demander.
      return 'La copie n’est pas dans un état où la reprise s’applique. Actualisez pour voir si cela change.';
    }
    if (!pipeline.incident?.causeResolved) {
      return 'La cause n’est pas levée. À traiter avec votre exploitation avant toute relance.';
    }
    return 'La relance ne se pilote pas depuis Quadringent. À demander à votre exploitation.';
  }

  if (status === 'unknown') {
    return 'Aucun relevé exploitable. À signaler si l’écran reste dans cet état.';
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */

const ACTION_COPY: Readonly<Record<ActionId, { label: string; effect: string }>> = {
  refresh: { label: 'Actualiser', effect: 'Relit l’état auprès du service.' },
  prepare: { label: 'Préparer les tables', effect: 'Crée les emplacements de destination.' },
  start: { label: 'Démarrer la copie', effect: 'Lance la première copie, puis le suivi en continu.' },
  pause: { label: 'Mettre en pause', effect: 'Arrête la lecture. La position est conservée.' },
  resume: { label: 'Relancer', effect: 'Reprend la lecture là où elle s’était arrêtée.' },
};

const PILOT_ACTIONS: readonly ActionId[] = ['prepare', 'start', 'pause', 'resume'];

/**
 * Les actions offertes — strictement celles que le service déclare disponibles.
 *
 * `refresh` est exclu : relire l'état n'est pas une décision d'exploitation, il
 * a sa place dans l'en-tête de page, pas dans le bloc d'action d'une liaison.
 */
export function actionsFor(
  capabilities: Readonly<Record<string, FleetCapability>> | null | undefined,
): readonly ActionOffer[] {
  if (!capabilities) return [];
  return PILOT_ACTIONS.flatMap((id) => {
    const capability = capabilities[id];
    if (!capability || capability.state !== 'available') return [];
    return [{ id, label: ACTION_COPY[id].label, effect: ACTION_COPY[id].effect }];
  });
}

/* ------------------------------------------------------------------ */
/* Faits chiffrés                                                      */
/* ------------------------------------------------------------------ */

/**
 * Les trois faits qui répondent à « c'est à jour ? ».
 *
 * Ne remontent ici que des grandeurs qu'un exploitant peut vérifier lui-même
 * dans Snowflake : des lignes, des tables, des doublons. Les compteurs propres
 * au lecteur restent dans l'onglet Mesures.
 *
 * Aucune valeur n'est reconstituée : si le service ne publie pas la mesure,
 * le fait vaut `null` et l'écran écrit « Non mesuré ».
 */
export function factsFor(pipeline: Pipeline): readonly Fact[] {
  const destination = pipeline.destination;
  const tables = pipeline.fleetRuntime?.tableStates ?? [];
  const copied = tables.filter((table) => table.phase === 'CERTIFIED').length;

  const rows = destination?.canonicalRows ?? destination?.rawRows ?? null;
  const duplicates = destination?.duplicates ?? null;

  return [
    {
      label: 'Lignes dans Snowflake',
      value: rows === null ? null : sequences(rows),
    },
    {
      label: 'Tables copiées',
      value: tables.length === 0 ? null : `${copied} sur ${tables.length}`,
    },
    {
      label: 'Doublons',
      value: duplicates === null ? null : duplicates === 0 ? 'Aucun' : sequences(duplicates),
    },
  ];
}

/* ------------------------------------------------------------------ */
/* Assemblage                                                          */
/* ------------------------------------------------------------------ */

/** Destination en clair, sans identifiant interne quand il n'apporte rien. */
function destinationLabel(pipeline: Pipeline): string | null {
  const destination = pipeline.destination;
  if (!destination?.database || !destination.schema) return null;
  return `${destination.database}.${destination.schema}`;
}

/**
 * Le relevé le plus récent qui décrit vraiment la liaison.
 *
 * Les étapes ont chacune leur âge ; retenir le plus récent évite d'annoncer
 * « figé depuis deux jours » alors que la destination a été relue à l'instant.
 */
function latestObservation(pipeline: Pipeline): string | null {
  const candidates = [
    pipeline.observedAt,
    pipeline.destination?.observedAt ?? null,
    ...pipeline.stages.map((stage) => stage.observedAt),
  ].filter((value): value is string => typeof value === 'string' && value.length > 0);
  if (candidates.length === 0) return null;
  return candidates.reduce((latest, value) => (Date.parse(value) > Date.parse(latest) ? value : latest));
}

/**
 * Ce qu'il faut dire avant que l'écran ne soit pris pour argent comptant.
 *
 * L'ordre compte : une donnée qui n'est pas réelle prime sur une donnée
 * simplement vieille — savoir qu'on regarde une démonstration change tout,
 * savoir qu'elle date ne change que la confiance qu'on lui accorde.
 */
export function caveatFor(kind: EvidenceKind, outdated: boolean): string | null {
  if (kind === 'simulation') return 'Démonstration — ces chiffres ne viennent pas de votre système.';
  if (kind === 'historical') return 'Relevé d’archive — ce n’est pas l’état actuel.';
  if (outdated) return 'Ce relevé n’est plus récent : l’état a pu changer depuis.';
  return null;
}

export function liaisonView(pipeline: Pipeline, now: Date): LiaisonView {
  const { health, label } = stateCopy(pipeline.status);
  const actions = actionsFor(pipeline.fleetRuntime?.capabilities);
  const observedAt = latestObservation(pipeline);
  const observed = observedAt ? age(observedAt, now) : null;

  return {
    id: pipeline.id,
    name: pipeline.id,
    destination: destinationLabel(pipeline),
    health,
    stateLabel: label,
    headline: headlineFor(pipeline, now),
    guidance: guidanceFor(pipeline, actions),
    facts: factsFor(pipeline),
    actions,
    freshness: observed ? `Relevé ${observed.label}` : 'Aucun relevé',
    outdated: observed === null || observed.stale,
    caveat: caveatFor(pipeline.quality.evidenceKind, observed === null || observed.stale),
  };
}

/**
 * Verdict d'ensemble — ce que la page d'accueil annonce en un mot.
 *
 * Le pire état l'emporte : une liaison arrêtée parmi dix ne se noie pas dans
 * une moyenne rassurante.
 */
const SEVERITY: Readonly<Record<Health, number>> = {
  ok: 0,
  attention: 1,
  unknown: 2,
  stopped: 3,
};

export function worstHealth(views: readonly LiaisonView[]): Health {
  return views.reduce<Health>(
    (worst, view) => (SEVERITY[view.health] > SEVERITY[worst] ? view.health : worst),
    'ok',
  );
}

/** Les liaisons qui demandent une attention, d'abord ; l'ordre reste stable. */
export function byUrgency(views: readonly LiaisonView[]): readonly LiaisonView[] {
  return [...views].sort((a, b) => SEVERITY[b.health] - SEVERITY[a.health]);
}

/** Résumé d'accueil : un mot, éventuellement un compte. */
export function boardSummary(views: readonly LiaisonView[]): string {
  if (views.length === 0) return 'Aucune liaison';
  const attention = views.filter((view) => view.health !== 'ok').length;
  if (attention === 0) return views.length === 1 ? 'Votre liaison est à jour' : 'Toutes vos liaisons sont à jour';
  if (attention === 1) return '1 liaison demande votre attention';
  return `${attention} liaisons demandent votre attention`;
}

export const OPERATOR_COPY = { MEASURE_ABSENT } as const;

/* ------------------------------------------------------------------ */
/* Étapes                                                              */
/* ------------------------------------------------------------------ */

/**
 * Les trois étapes que l'opérateur reconnaît.
 *
 * Le service en publie cinq (source, capture, raw, load, destination), mais
 * « raw » et « load » sont deux moments d'un même transfert : les distinguer
 * à l'écran oblige l'opérateur à connaître le découpage interne pour lire son
 * état. On garde donc le fait — les données sont-elles arrivées — et on laisse
 * le découpage à l'onglet Mesures.
 */
export type StepId = 'connexion' | 'lecture' | 'arrivee';

export interface Step {
  readonly id: StepId;
  readonly label: string;
  readonly health: Health;
  /** Ce qui est constaté, en langage courant. */
  readonly detail: string;
  /** Âge du constat. `null` si l'étape n'a jamais été observée. */
  readonly observed: string | null;
}

const STEP_SOURCES: Readonly<Record<StepId, { label: string; stages: readonly string[] }>> = {
  connexion: { label: 'Connexion à l’AS400', stages: ['source'] },
  lecture: { label: 'Lecture des données', stages: ['capture'] },
  arrivee: { label: 'Arrivée dans Snowflake', stages: ['raw', 'load', 'destination'] },
};

function stepHealth(status: string): Health {
  if (status === 'healthy') return 'ok';
  if (status === 'degraded') return 'attention';
  if (status === 'incident' || status === 'planned_stop' || status === 'awaiting_resume') return 'stopped';
  return 'unknown';
}

/**
 * Réduit les étapes publiées à trois. Pour un regroupement, l'état retenu est
 * le moins bon : une arrivée « saine » qui masque un transfert en défaut
 * donnerait un écran faussement rassurant.
 */
export function stepsFor(pipeline: Pipeline, now: Date): readonly Step[] {
  return (Object.keys(STEP_SOURCES) as StepId[]).flatMap((id) => {
    const { label, stages } = STEP_SOURCES[id];
    const matching = pipeline.stages.filter((stage) => stages.includes(stage.id));
    if (matching.length === 0) return [];

    const worst = matching.reduce((acc, stage) =>
      SEVERITY[stepHealth(stage.status)] > SEVERITY[stepHealth(acc.status)] ? stage : acc,
    );
    const observedAt = matching
      .map((stage) => stage.observedAt)
      .filter((value): value is string => typeof value === 'string' && value.length > 0)
      .reduce<string | null>(
        (latest, value) => (latest === null || Date.parse(value) > Date.parse(latest) ? value : latest),
        null,
      );

    const health = stepHealth(worst.status);
    return [{
      id,
      label,
      health,
      detail: stepDetail(id, health),
      observed: observedAt === null ? null : age(observedAt, now).label,
    }];
  });
}

/**
 * Ce qu'une étape constate, dit par le produit et non par le service.
 *
 * Les libellés publiés par le service décrivent son propre découpage — « Raw
 * publié », « Prête à reprendre » — et se contredisent d'une étape à l'autre :
 * une étape « prête à reprendre » sous une liaison « arrêtée » laisse
 * l'opérateur arbitrer entre deux vocabulaires. Le texte est donc dérivé de
 * l'état constaté, qui lui est une donnée, et de l'étape concernée.
 */
function stepDetail(id: StepId, health: Health): string {
  if (health === 'unknown') return 'Aucun relevé exploitable.';
  if (id === 'connexion') {
    return health === 'ok'
      ? 'L’AS400 répond et le compte est accepté.'
      : health === 'attention'
        ? 'La connexion répond, mais une vérification manque.'
        : 'L’AS400 ne répond pas, ou refuse le compte.';
  }
  if (id === 'lecture') {
    return health === 'ok'
      ? 'Les changements sont lus au fil de l’eau.'
      : health === 'attention'
        ? 'La lecture avance, mais une vérification manque.'
        : 'Rien n’est lu en ce moment.';
  }
  return health === 'ok'
    ? 'Les données sont écrites dans Snowflake.'
    : health === 'attention'
      ? 'L’écriture avance, mais une vérification manque.'
      : 'Plus rien n’arrive dans Snowflake.';
}

/** Une fenêtre de métering mesure un warehouse ; elle n'est pas une facture de liaison. */
export function snowflakeCostFor(pipeline: Pipeline, now = new Date()) {
  const observation = pipeline.observability;
  const check = observation?.checks.find((item) => item.id === 'snowflake_credits');
  const window = check && /^metering_window_(\d{10})_(\d{10})_([1-9]\d{0,3})$/.exec(check.reason);
  const start = window ? Number(window[1]) : 0;
  const end = window ? Number(window[2]) : 0;
  const delay = (now.getTime() / 1000 - end) / 3600;
  const measured = check !== undefined && check.status !== 'unobserved'
    && check.unit === 'warehousecredits/delayed24h' && typeof check.observed === 'number'
    && Number.isFinite(check.observed) && check.observed >= 0 && window != null
    && end - start === 86400 && delay >= 6 && Number(window[3]) <= 1000;
  const kind = observation?.quality.evidenceKind;
  const observedAt = observation?.observedAt ? Date.parse(observation.observedAt) : NaN;
  const fresh = observation?.quality.freshness === 'fresh' && now.getTime() - observedAt <= 300_000
    && now.getTime() >= observedAt;
  const current = fresh && kind === 'live' && pipeline.quality?.evidenceKind === 'live';
  const costs = pipeline.costs;
  let amount = costs?.pricePerCredit == null ? 'Prix non déclaré' : 'Non mesuré';
  if (measured && current && costs?.amount != null && costs.currency !== null) {
    amount = new Intl.NumberFormat('fr-FR', { style: 'currency', currency: costs.currency,
      minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(costs.amount));
  }
  const caveat = kind === 'simulation' || pipeline.quality?.evidenceKind === 'simulation' ? 'Données simulées'
    : kind === 'historical' || pipeline.quality?.evidenceKind === 'historical' ? 'Relevé historique' : observation && !fresh ? 'Relevé ancien ou date incertaine' : null;
  return {
    creditsLabel: 'Crédits Snowflake mesurés', amountLabel: 'Coût calculé au prix déclaré',
    credits: measured ? new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 6 }).format(check!.observed as number) : null,
    amount,
    window: measured ? `24 h, arrêtée il y a ${new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(delay)} h · ${new Date(start * 1000).toISOString()} → ${new Date(end * 1000).toISOString()}` : 'Fenêtre non mesurée',
    scope: costs ? `Ensemble du warehouse ${costs.warehouse} · ne pas additionner entre liaisons` : 'Mesure du warehouse · périmètre non publié',
    caveat,
    explanation: 'Montant calculé à partir des crédits mesurés et du prix contractuel déclaré ; ce n’est pas une facture.',
    absent: 'Non mesuré',
  };
}

export function unmeasuredCostsFor(pipeline: Pipeline): readonly string[] {
  const missing: string[] = [];
  const costs = snowflakeCostFor(pipeline);
  if (costs.credits === null) missing.push('Les crédits Snowflake consommés ne sont pas encore mesurés.');
  if (costs.amount === 'Prix non déclaré') missing.push('Aucun prix déclaré : les crédits restent affichés sans conversion en devise.');
  if (!pipeline.infrastructureCosts?.storage || !pipeline.infrastructureCosts?.cluster) missing.push('Les coûts S3 ou cluster restent non mesurés lorsque leur relevé manque.');
  missing.push('Les requêtes S3, transferts réseau AWS et autres services ne sont pas chiffrés.');
  return missing;
}
export const connectionNetworkFailure = 'Le service n’a pas répondu. L’enregistrement de la liaison n’est pas confirmé. Vérifiez les liaisons enregistrées avant de réessayer.';

export function actionResultFor(pipelineId: string, receipt: PipelineActionReceipt): string {
  const result = receipt.stages.intent.state === 'rejected' && receipt.stages.execution.state === 'not_started'
    ? 'Action refusée avant exécution.'
    : receipt.state === 'succeeded' && receipt.stages.observedEffect.state === 'succeeded'
      ? 'Action exécutée, effet confirmé par le service.'
      : 'L’effet de l’action n’est pas confirmé. Actualisez l’état avant de réessayer.';
  return `${pipelineId} : ${result} Reçu : ${receipt.id}.`;
}

export function actionFailureFor(pipelineId: string): string {
  return `${pipelineId} : le service n’a pas confirmé l’action. Actualisez l’état avant de réessayer.`;
}

/** Fenêtres et devises restent séparées : aucun total entre mesures incomparables. */
export function infrastructureCostsFor(pipeline: Pipeline, now = new Date()) {
  const proof = pipeline.infrastructureCosts;
  const age = proof ? now.getTime() - Date.parse(proof.collectedAt) : Infinity;
  const current = pipeline.quality?.evidenceKind === 'live' && age >= 0 && age <= 26 * 3600_000;
  const candidateStorage = current ? proof?.storage : null;
  const storageAge = candidateStorage ? now.getTime() - Date.parse(candidateStorage.observedAt) : Infinity;
  const storage = storageAge >= 0 && storageAge <= 72 * 3600_000 ? candidateStorage : null;
  const candidateCluster = current ? proof?.cluster : null;
  const clusterAge = candidateCluster ? now.getTime() - Date.parse(candidateCluster.end) : Infinity;
  const cluster = clusterAge >= 0 && clusterAge <= 72 * 3600_000 ? candidateCluster : null;
  const money = (value: string | null, currency = 'USD') => value === null ? 'Non mesuré'
    : new Intl.NumberFormat('fr-FR',{style:'currency',currency,maximumFractionDigits:4}).format(Number(value));
  const date = (value: string) => new Intl.DateTimeFormat('fr-FR',{dateStyle:'medium',timeStyle:'short',timeZone:'UTC'}).format(new Date(value))+' UTC';
  return {
    title:'Stockage et cluster',
    lines:[
      {label:'Stockage S3 Standard',value:storage ? new Intl.NumberFormat('fr-FR',{maximumFractionDigits:2}).format(storage.bytes/1024**3)+' Gio' : 'Non mesuré',detail:storage ? 'Relevé du '+date(storage.observedAt) : 'Relevé quotidien indisponible ou ancien.'},
      {label:'S3 par mois, à volume constant',value:storage ? money(storage.monthlyRunRate) : 'Non mesuré',detail:storage ? new Intl.NumberFormat('fr-FR',{maximumFractionDigits:8}).format(Number(storage.pricePerGibMonth))+' USD/Gio-mois. Estimation au tarif public AWS relevé, première tranche. Hors requêtes, transferts, remises et autres classes.' : 'Un relevé et son tarif sont nécessaires au calcul.'},
      {label:'Cluster attribué à Quadringent',value:cluster ? money(cluster.allocatedAmount,cluster.currency) : 'Non mesuré',detail:cluster && proof ? 'Namespace '+proof.namespace+' · '+date(cluster.start)+' → '+date(cluster.end) : 'Allocation du namespace non disponible.'},
      {label:'Actifs du cluster, total de contexte',value:cluster ? money(cluster.clusterAmount,cluster.currency) : 'Non mesuré',detail:'Modèle OpenCost : nœuds, volumes et gestion du cluster recensés.'},
      {label:'Ressources inutilisées du cluster',value:cluster ? money(cluster.idleAmount,cluster.currency) : 'Non mesuré',detail:'Incluses dans le contexte du cluster, non attribuées au namespace.'},
    ],
    note:(current && proof ? 'Collecte du '+date(proof.collectedAt)+'. ' : '')+'Le stockage couvre le bucket déclaré du site ; l’allocation couvre son namespace. Ne pas additionner des liaisons qui partagent ces ressources. Les allocations OpenCost utilisent le modèle de prix configuré sur le cluster. Les ressources inutilisées et les frais partagés ne sont pas répartis sur Quadringent. Ces calculs ne sont pas une facture AWS ; les fenêtres et devises restent séparées.',
  };
}

/* ------------------------------------------------------------------ */
/* Barre de touches de fonction (FunctionKeyBar)                       */
/* ------------------------------------------------------------------ */

/**
 * Les quatre raccourcis du bandeau discret de bas d'écran, en écho aux
 * touches de fonction des terminaux 5250 sans en singer l'esthétique :
 * un mot d'action en français, jamais un jargon technique.
 */
export type FunctionKeyId = 'F3' | 'F5' | 'F9' | 'F12';

export interface FunctionKeyCopy {
  readonly key: FunctionKeyId;
  readonly label: string;
}

export const FUNCTION_KEY_COPY: Readonly<Record<FunctionKeyId, FunctionKeyCopy>> = {
  F3: { key: 'F3', label: 'Revenir' },
  F5: { key: 'F5', label: 'Actualiser' },
  F9: { key: 'F9', label: 'Pause/Reprise' },
  F12: { key: 'F12', label: 'Journaux' },
};

export function functionKeyCopy(key: FunctionKeyId): FunctionKeyCopy {
  return FUNCTION_KEY_COPY[key];
}

/** Texte de confirmation pour ActionButton — une action destructrice ou
 *  coûteuse ne s'exécute jamais sans qu'on redise ce qu'elle fait. */
export function actionConfirmCopy(label: string): string {
  return `Confirmer « ${label} » ? Cette action sera exécutée immédiatement.`;
}

/* ------------------------------------------------------------------ */
/* Assistant de connexion v2 — activation admin, source, Snowflake,    */
/* tables (docs/plans/2026-09-23-control-plane-v2-contract.md §2).     */
/* ------------------------------------------------------------------ */

export const WIZARD_COPY = {
  activate: {
    kicker: 'Premier accès',
    title: 'Activer votre compte administrateur',
    lead: 'Ce lien est à usage unique. Choisissez un mot de passe pour activer votre compte.',
    passwordLabel: 'Nouveau mot de passe',
    primary: 'Activer le compte',
  },
  source: {
    kicker: 'Source IBM i',
    title: 'Adresse et compte',
    lead: 'Le compte indiqué servira à préparer la capture. Le mot de passe n’est jamais conservé en clair.',
    hostLabel: 'Adresse (hôte ou IP)',
    accountLabel: 'Compte',
    passwordLabel: 'Mot de passe',
    advanced: 'Options avancées (ports)',
    portLabel: 'Port',
    sslPortLabel: 'Port chiffré (SSL)',
    test: 'Tester',
    primary: 'Continuer',
    trustCertificate: 'Faire confiance à ce certificat',
    // Cette installation n'a pas de sonde IBM i câblée (`SourcesService.test`
    // sans `source_probe`) : le test se limite à vérifier que le secret a
    // été enregistré, jamais présenté comme un test réseau réussi.
    unavailable: 'Test non disponible sur cette installation : seul l’enregistrement du mot de passe a été vérifié. Le réseau, le certificat et l’authentification n’ont pas été testés.',
    existingSourceHint: 'Une source est déjà configurée. « Tester » utilise son mot de passe enregistré. Saisir un nouveau mot de passe crée une nouvelle source et conserve l’ancienne.',
  },
  snowflake: {
    kicker: 'Destination Snowflake',
    title: 'Votre compte Snowflake',
    lead: 'Indiquez le compte, la base et le schéma où recevoir les données. Aucun secret d’administration Snowflake n’est demandé ici.',
    accountLabel: 'Identifiant de compte',
    databaseLabel: 'Base de destination',
    schemaLabel: 'Schéma de destination (facultatif)',
    schemaHint: 'Un schéma renseigné reçoit l’historique et le miroir. Sans schéma, l’historique va dans RAW et le miroir dans CURATED.',
    invalidScope: 'Identifiant invalide : 63 caractères maximum, lettre ou _ initial, puis lettres, chiffres, _ ou $.',
    scriptTitle: 'Script SQL à exécuter dans Snowflake',
    scriptLead: 'Exécutez ce script avec un rôle administrateur Snowflake ; il crée un rôle, un utilisateur de service (authentifié par paire de clés) et l’entrepôt dédiés à Quadringent.',
    copy: 'Copier',
    download: 'Télécharger',
    downloadKey: 'Télécharger la clé privée',
    setupNext: 'Exécutez le script dans Snowflake, puis vérifiez l’accès. Vous pouvez conserver une copie de la clé privée ; elle ne sera pas remise à nouveau.',
    setupNextWithoutKey: 'Exécutez le script dans Snowflake, puis vérifiez l’accès.',
    verify: 'Vérifier l’accès',
    verifying: 'Vérification en cours…',
    verifyEffect: 'Le contrôle crée puis retire une table de test dans les schémas de destination et peut réveiller l’entrepôt.',
    verified: 'Accès Snowflake vérifié. Vous pouvez choisir les tables à copier.',
    verificationFailed: 'L’accès Snowflake n’est pas vérifié. Contrôlez le script et les droits accordés, puis réessayez.',
    keyAlreadyIssued: 'La clé a été remise lors de la création. Elle ne peut pas être téléchargée à nouveau. Utilisez la copie conservée.',
    primary: 'Continuer',
  },
  tables: {
    kicker: 'Vos tables',
    title: 'Choisir les tables à copier',
    lead: 'Chaque table est vérifiée avant de pouvoir démarrer : journal IBM i et clé de suivi des modifications.',
    search: 'Rechercher une bibliothèque ou une table',
    recheck: 'Revérifier',
    clPanelTitle: 'Commandes CL à exécuter sur l’IBM i',
    start: 'Démarrer',
    keyChoiceLabel: 'Clé de suivi des modifications',
    keyChoiceUniqueIndex: 'Déclarer une clé (colonnes séparées par des virgules)',
    keyChoiceRrn: 'Utiliser la position physique (RRN)',
    keyColumnsLabel: 'Colonnes de la clé',
    rrnAcknowledge: 'Je comprends la conséquence de ce choix.',
  },
} as const;

export function wizardCheckLabel(state: 'ok' | 'attention' | 'failed' | 'unknown'): string {
  switch (state) {
    case 'ok': return 'OK';
    case 'attention': return 'Attention';
    case 'failed': return 'Échec';
    case 'unknown': return 'Non testé';
  }
}

/* ------------------------------------------------------------------ */
/* Cockpit v2 — panneau de contrôles, réutilisé à chaque niveau        */
/* (table, connexion/source, destination, flotte). Voir                */
/* domain/controlsPanel.ts pour la machine à états ; ce module ne      */
/* porte que le texte.                                                 */
/* ------------------------------------------------------------------ */

export interface ControlActionCopy {
  readonly label: string;
  /** Ce que l'action fait, en une phrase — affiché comme « effet annoncé »
   *  avant confirmation, jamais silencieusement supposé. */
  readonly effect: string;
}

const CONTROL_ACTION_COPY: Readonly<Record<ControlActionKind, ControlActionCopy>> = {
  pause: { label: 'Suspendre', effect: 'Suspend la lecture ; la position déjà atteinte est conservée.' },
  resume: { label: 'Reprendre', effect: 'Reprend la lecture depuis la dernière position enregistrée.' },
  restart_initial_copy: { label: 'Relancer la copie initiale', effect: 'Reprend la copie initiale depuis son début ; les données déjà chargées sont remplacées.' },
  remove: { label: 'Retirer', effect: 'Retire définitivement cet élément du suivi. Cette action est terminale.' },
  replay: { label: 'Rejouer une plage', effect: 'Rejoue les événements de la plage indiquée vers la destination.' },
};

export function controlActionCopy(action: ControlActionKind): ControlActionCopy {
  return CONTROL_ACTION_COPY[action];
}

const CONTROL_LEVEL_COPY: Readonly<Record<ControlLevel, string>> = {
  table: 'cette table',
  connection: 'toutes les tables de cette connexion',
  destination: 'toutes les tables de cette destination',
  fleet: 'toutes les tables',
};

/** Titre court du niveau, affiché comme en-tête du panneau de contrôles
 *  (design §3 : « scope title » — un opérateur doit voir d'un coup d'œil à
 *  quelle échelle une action va s'appliquer avant même de la choisir). */
const CONTROL_LEVEL_TITLE: Readonly<Record<ControlLevel, string>> = {
  table: 'Table',
  connection: 'Connexion',
  destination: 'Destination',
  fleet: 'Toutes les connexions',
};

export function controlLevelTitle(level: ControlLevel): string {
  return CONTROL_LEVEL_TITLE[level];
}

/** Phrase d'effet complète : l'action, à l'échelle du niveau concerné —
 *  jamais une action de flotte présentée comme si elle ne touchait qu'une table. */
export function controlActionAnnouncement(action: ControlActionKind, level: ControlLevel): string {
  const copy = controlActionCopy(action);
  return `${copy.effect} Portée : ${CONTROL_LEVEL_COPY[level]}.`;
}

export const CONTROLS_PANEL_COPY = {
  previewing: 'Calcul de l’effet…',
  confirmTitle: 'Confirmer l’action',
  confirm: 'Confirmer',
  cancel: 'Annuler',
  running: 'Exécution en cours…',
  awaitingConfirmation: 'Action sensible : en attente d’approbation.',
  approve: 'Approuver',
  reject: 'Rejeter',
  rerun: 'Relancer, maintenant approuvée',
  verifying: 'Vérification de l’effet par relecture…',
  succeeded: 'Effet vérifié par relecture.',
  failedGeneric: 'Action non exécutée.',
} as const;

export const CONFIRMATIONS_COPY = {
  title: 'Confirmations',
  summary: 'Décisions en attente et actions approuvées.',
  empty: 'Aucune décision en attente',
  emptyDetail: 'Les nouvelles demandes apparaîtront ici.',
  approve: 'Approuver',
  reject: 'Rejeter',
  execute: 'Exécuter l’action',
  cancel: 'Annuler',
  approveQuestion: 'Approuver cette demande ?',
  rejectQuestion: 'Rejeter cette demande ?',
  executeQuestion: 'Exécuter cette action approuvée ?',
  approveFinal: 'Confirmer l’approbation',
  rejectFinal: 'Confirmer le rejet',
  executeFinal: 'Confirmer l’exécution',
  approved: 'Demande approuvée. L’action n’a pas encore été exécutée.',
  rejected: 'Demande rejetée. Aucune action n’a été exécutée.',
  executed: 'Action exécutée et effet vérifié auprès du service.',
  executionUnavailable: 'Cette action approuvée ne peut pas être exécutée depuis cet écran.',
  changed: 'Cette demande n’est plus disponible. La liste a été actualisée.',
} as const;

export function confirmationActionLabel(actionRef: string): string {
  switch (actionRef) {
    case 'pipeline.remove': return 'Retirer la table';
    case 'pipeline.restart_initial_copy': return 'Relancer la copie initiale';
    case 'pipeline.replay': return 'Rejouer les événements';
    default: return 'Action sensible';
  }
}
