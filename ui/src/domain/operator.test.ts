import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import type { FleetCapability, Pipeline } from './controlPlane.ts';
import {
  actionsFor,
  boardSummary,
  factValue,
  factsFor,
  guidanceFor,
  headlineFor,
  liaisonView,
  stateCopy,
  worstHealth,
  approximateDuration,
  caveatFor,
  actionResultFor,
  actionFailureFor,
} from './operator.ts';
import type { PipelineActionReceipt } from '../data/controlPlaneClient.ts';

test('le retour d’action distingue un effet relu, un refus et une issue incertaine', () => {
  const receipt = { id: 'receipt-123', state: 'succeeded', stages: {
    intent: { state: 'recorded' }, execution: { state: 'completed' }, observedEffect: { state: 'succeeded' },
  } } as PipelineActionReceipt;
  assert.match(actionResultFor('exemple', receipt), /effet confirmé.*receipt-123/);
  assert.match(actionResultFor('exemple', { ...receipt, state: 'unavailable', stages: { ...receipt.stages,
    intent: { ...receipt.stages.intent, state: 'rejected' }, execution: { ...receipt.stages.execution, state: 'not_started' },
  } }), /refusée avant exécution/);
  assert.match(actionResultFor('exemple', { ...receipt, stages: { ...receipt.stages,
    observedEffect: { ...receipt.stages.observedEffect, state: 'unknown' },
  } }), /effet.*pas confirmé/);
  assert.match(actionFailureFor('exemple'), /Actualisez.*avant de réessayer/);
});

const NOW = new Date('2026-09-21T17:34:00Z');

/** Les formateurs produisent des espaces fines insécables ; les comparaisons
 *  de ce fichier portent sur les mots, pas sur la variété d'espace utilisée. */
function plain(value: string): string {
  return value.replace(/[\u202f\u00a0\u2009]/g, ' ');
}

/** Relevé réel du service : capture arrêtée en sécurité, cause levée. */
function stoppedPipeline(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    id: 'example-corp',
    environment: 'dev',
    status: 'awaiting_resume',
    quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' },
    summary: 'Prête à reprendre',
    observedAt: '2026-09-19T21:02:01.075000+00:00',
    stages: [
      {
        id: 'source',
        status: 'healthy',
        observedAt: '2026-09-21T17:30:47+00:00',
        headline: 'Connexion vérifiée',
        detail: 'Authentification et catalogue vérifiés',
      },
    ],
    lagSequences: 1,
    lagSeconds: null,
    lagSeries: [],
    lagSeriesResolutionSeconds: null,
    lagSampleCount: 0,
    lagUnknownSampleCount: 0,
    counters: {},
    incident: {
      code: 'capture_stopped_fail_closed',
      type: 'capture_auth_blocked',
      causeResolved: true,
      declaredAt: '2026-09-19T21:02:01.075000+00:00',
    },
    destination: {
      kind: 'snowflake',
      database: 'DEV_RAW',
      schema: 'AS400_RD',
      stage: null,
      rawTable: null,
      canonicalTable: null,
      runTag: null,
      observedAt: '2026-09-21T17:28:06.449388+00:00',
      loadCheckpoint: null,
      applyCheckpoint: null,
      sourceEvents: 221885292,
      rawRows: 221885292,
      canonicalRows: 221885292,
      duplicates: 0,
    },
    fleetRuntime: {
      formatVersion: 'quadringent-fleet-runtime-v1',
      fleetId: 'example-corp-dev',
      environment: 'dev',
      pipelineId: 'example-corp',
      phase: 'CERTIFIED',
      checkpoint: null,
      capabilities: {
        prepare: { state: 'unavailable', reason: 'already_prepared' },
        start: { state: 'unavailable', reason: 'already_started' },
        pause: { state: 'unavailable', reason: 'unsupported_action' },
        resume: { state: 'unavailable', reason: 'unsupported_action' },
        refresh: { state: 'available', reason: null },
      },
      tableStates: [
        { name: 'SALE', phase: 'CERTIFIED', copiedRows: 68788758, totalRows: 68788758 },
        { name: 'CNTR', phase: 'CERTIFIED', copiedRows: 210, totalRows: 210 },
      ],
    },
    ...overrides,
  } as Pipeline;
}

test('une capture arrêtée n’est jamais présentée comme allant bien', () => {
  const { health, label } = stateCopy('awaiting_resume');
  assert.equal(health, 'stopped');
  assert.equal(label, 'Lecture arrêtée');
});

test('la phrase principale dit la durée d’arrêt et la levée de cause', () => {
  const headline = plain(headlineFor(stoppedPipeline(), NOW));
  assert.match(headline, /^Arrêtée depuis 2 jours\./);
  assert.match(headline, /La connexion à la source fonctionne de nouveau\./);
});

test('aucune action n’est offerte quand le service ne déclare rien de disponible', () => {
  const capabilities: Record<string, FleetCapability> = {
    prepare: { state: 'unavailable', reason: 'already_prepared' },
    start: { state: 'unavailable', reason: 'already_started' },
    pause: { state: 'unavailable', reason: 'unsupported_action' },
    resume: { state: 'unavailable', reason: 'unsupported_action' },
    refresh: { state: 'available', reason: null },
  };
  // `refresh` reste volontairement hors du bloc d'action d'une liaison.
  assert.deepEqual(actionsFor(capabilities), []);
});

test('une capacité disponible produit une action, et une seule', () => {
  const offers = actionsFor({
    prepare: { state: 'unavailable', reason: 'already_prepared' },
    start: { state: 'unavailable', reason: 'already_started' },
    pause: { state: 'unavailable', reason: 'unsupported_action' },
    resume: { state: 'available', reason: null },
    refresh: { state: 'available', reason: null },
  });
  assert.equal(offers.length, 1);
  assert.equal(offers[0]?.id, 'resume');
  assert.equal(offers[0]?.label, 'Relancer');
});

test('sans action disponible, la consigne dit où l’acte se fait', () => {
  const guidance = guidanceFor(stoppedPipeline(), []);
  assert.equal(
    guidance,
    'La relance ne se pilote pas depuis Quadringent. À demander à votre exploitation.',
  );
});

test('une action disponible remplace la consigne — jamais les deux', () => {
  const offers = actionsFor({
    resume: { state: 'available', reason: null },
  } as Record<string, FleetCapability>);
  assert.equal(guidanceFor(stoppedPipeline(), offers), null);
});

test('les faits chiffrés ne remontent que des grandeurs vérifiables', () => {
  const facts = factsFor(stoppedPipeline());
  assert.deepEqual(
    facts.map((fact) => fact.label),
    ['Lignes dans Snowflake', 'Tables copiées', 'Doublons'],
  );
  assert.equal(plain(facts[0]!.value!), '221 885 292');
  assert.equal(facts[1]?.value, '2 sur 2');
  assert.equal(facts[2]?.value, 'Aucun');
});

test('une mesure absente vaut « Non mesuré », jamais zéro', () => {
  const pipeline = stoppedPipeline({ destination: null, fleetRuntime: null });
  const facts = factsFor(pipeline);
  for (const fact of facts) {
    assert.equal(fact.value, null);
    assert.equal(factValue(fact), 'Non mesuré');
  }
});

test('zéro doublon mesuré se dit « Aucun », et reste distinct d’une absence de mesure', () => {
  const measured = factsFor(stoppedPipeline())[2];
  assert.equal(factValue(measured!), 'Aucun');
  const absent = factsFor(stoppedPipeline({ destination: null }))[2];
  assert.equal(factValue(absent!), 'Non mesuré');
});

test('la fraîcheur retient le relevé le plus récent, pas le plus ancien', () => {
  // Le pipeline est daté du 19, mais la destination a été relue le 21 :
  // l'écran ne doit pas annoncer « figé depuis deux jours ».
  const view = liaisonView(stoppedPipeline(), NOW);
  assert.equal(plain(view.freshness), 'Relevé il y a 3 min 13 s');
});

test('aucun vocabulaire technique ne franchit la couche opérateur', () => {
  const view = liaisonView(stoppedPipeline(), NOW);
  const surfaced = [
    view.name,
    view.stateLabel,
    view.headline,
    view.guidance ?? '',
    view.freshness,
    view.destination ?? '',
    ...view.facts.map((fact) => `${fact.label} ${factValue(fact)}`),
    ...view.actions.map((action) => `${action.label} ${action.effect}`),
  ].join(' ').toLowerCase();

  for (const forbidden of [
    'receiver', 'séquence', 'sequence', 'checkpoint', 'sonde', 'apply',
    'polls', 'mcpu', 'instrumenté', 'sidecar', 'watermark', 'jitter',
    'backofflimit', 'deployment', 'retrievejournal', 'raw ',
  ]) {
    assert.ok(!surfaced.includes(forbidden), `« ${forbidden} » ne doit pas atteindre l’écran : ${surfaced}`);
  }
});

test('le verdict d’ensemble retient le pire état, pas la moyenne', () => {
  const ok = liaisonView(stoppedPipeline({ status: 'healthy', incident: null }), NOW);
  const stopped = liaisonView(stoppedPipeline(), NOW);
  assert.equal(worstHealth([ok, ok]), 'ok');
  assert.equal(worstHealth([ok, stopped, ok]), 'stopped');
});

test('le résumé d’accueil compte ce qui demande une attention', () => {
  const ok = liaisonView(stoppedPipeline({ status: 'healthy', incident: null }), NOW);
  const stopped = liaisonView(stoppedPipeline(), NOW);
  assert.equal(boardSummary([]), 'Aucune liaison');
  assert.equal(boardSummary([ok]), 'Votre liaison est à jour');
  assert.equal(boardSummary([ok, ok]), 'Toutes vos liaisons sont à jour');
  assert.equal(boardSummary([ok, stopped]), '1 liaison demande votre attention');
  assert.equal(boardSummary([stopped, stopped]), '2 liaisons demandent votre attention');
});

test('les durées d’écran se disent au registre de la conversation', () => {
  assert.equal(approximateDuration(30), 'moins d’une minute');
  assert.equal(approximateDuration(60), '1 minute');
  assert.equal(approximateDuration(25 * 60), '25 minutes');
  assert.equal(approximateDuration(3600), '1 heure');
  assert.equal(approximateDuration(5 * 3600), '5 heures');
  assert.equal(approximateDuration(86400), '1 jour');
  // 44 h 32 d'arrêt se lit « 2 jours », pas « 44 h 32 ».
  assert.equal(approximateDuration(44 * 3600 + 32 * 60), '2 jours');
  assert.equal(approximateDuration(20 * 86400), '3 semaines');
});

test('la consigne distingue « pas relançable ici » de « pas encore sûr »', () => {
  // Le service publie resume_not_ready quand le lecteur est bien monté mais
  // que les vérifications de reprise ne sont pas réunies. Renvoyer l'opérateur
  // vers son exploitation serait faux : il n'y a rien à demander, il faut
  // attendre.
  const notReady = stoppedPipeline({
    fleetRuntime: {
      ...(stoppedPipeline().fleetRuntime as object),
      capabilities: {
        prepare: { state: 'unavailable', reason: 'already_prepared' },
        start: { state: 'unavailable', reason: 'reader_stopped_fail_closed' },
        pause: { state: 'unavailable', reason: 'unsupported_action' },
        resume: { state: 'unavailable', reason: 'resume_not_ready' },
        refresh: { state: 'available', reason: null },
      },
    },
  } as Partial<Pipeline>);

  assert.match(guidanceFor(notReady, [])!, /pas encore sûre/);
  assert.match(guidanceFor(stoppedPipeline(), [])!, /À demander à votre exploitation/);
});

test('une cause non levée renvoie vers le traitement, pas vers la relance', () => {
  const unresolved = stoppedPipeline({
    incident: {
      code: 'capture_stopped_fail_closed',
      type: 'capture_auth_blocked',
      causeResolved: false,
      declaredAt: '2026-09-19T21:02:01.075000+00:00',
    },
  } as Partial<Pipeline>);
  assert.match(guidanceFor(unresolved, [])!, /La cause n’est pas levée/);
});

test('une démonstration ne se laisse jamais lire comme une mesure réelle', () => {
  // Rien à l'écran ne distingue un chiffre simulé d'un chiffre mesuré : c'est
  // l'avertissement qui le fait, et rien d'autre.
  assert.match(caveatFor('simulation', false)!, /Démonstration/);
  assert.match(caveatFor('simulation', false)!, /ne viennent pas de votre système/);
});

test('un rejeu d’archive dit qu’il n’est pas l’état actuel', () => {
  assert.match(caveatFor('historical', false)!, /archive/);
  assert.match(caveatFor('historical', false)!, /pas l’état actuel/);
});

test('une lecture réelle et récente n’a rien à avertir', () => {
  assert.equal(caveatFor('live', false), null);
});

test('un relevé réel mais périmé le dit, au lieu d’affirmer un état courant', () => {
  assert.match(caveatFor('live', true)!, /n’est plus récent/);
});

test('la nature de la donnée prime sur son âge', () => {
  // Une simulation périmée reste d'abord une simulation : savoir qu'on regarde
  // une démonstration change la décision, savoir qu'elle date ne change que la
  // confiance qu'on lui accorde.
  assert.match(caveatFor('simulation', true)!, /Démonstration/);
});

test('la vue d’une liaison porte l’avertissement qui la concerne', () => {
  const simulated = stoppedPipeline({
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'simulation' },
  } as Partial<Pipeline>);
  assert.match(liaisonView(simulated, NOW).caveat!, /Démonstration/);

  // Le relevé de destination du fixture date de six minutes : sur une lecture
  // réelle, c'est l'âge qui parle.
  assert.match(liaisonView(stoppedPipeline(), NOW).caveat!, /n’est plus récent/);
});

test('une lecture réelle et fraîche n’affiche aucun avertissement', () => {
  const justRead = new Date('2026-09-21T17:28:30Z');
  assert.equal(liaisonView(stoppedPipeline(), justRead).caveat, null);
});

test('une phase non pausable ne renvoie pas vers l’exploitation', () => {
  // Le déploiement sait relancer ; c'est l'état de la copie qui ne s'y prête
  // pas. Dire « à demander à votre exploitation » enverrait l'opérateur
  // réclamer quelque chose qui n'a pas lieu d'être.
  const notPausable = stoppedPipeline({
    fleetRuntime: {
      ...(stoppedPipeline().fleetRuntime as object),
      capabilities: {
        prepare: { state: 'unavailable', reason: 'already_prepared' },
        start: { state: 'unavailable', reason: 'already_started' },
        pause: { state: 'unavailable', reason: 'not_pausable_in_phase' },
        resume: { state: 'unavailable', reason: 'not_pausable_in_phase' },
        refresh: { state: 'available', reason: null },
      },
    },
  } as Partial<Pipeline>);

  const guidance = guidanceFor(notPausable, [])!;
  assert.match(guidance, /n’est pas dans un état où la reprise s’applique/);
  assert.ok(!/exploitation/.test(guidance), `consigne trompeuse : ${guidance}`);
});
