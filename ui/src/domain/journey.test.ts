import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  JOURNEY_COPY,
  PREREQUISITES,
  checkFrom,
  journeyMarks,
  journeySteps,
  nextStep,
  previousStep,
  serverStepFor,
  problemCopy,
  pendingCopy,
} from './journey.ts';

test('le parcours traverse cinq étapes, préparation comprise', () => {
  // « Quelles données » précède « où les mettre » : c'est l'ordre naturel, et
  // c'est aussi celui dans lequel le service valide.
  assert.deepEqual(journeySteps, ['preparer', 'source', 'donnees', 'destination', 'creer']);
});

test('l’enchaînement des étapes est borné aux deux extrémités', () => {
  assert.equal(previousStep('preparer'), null);
  assert.equal(nextStep('preparer'), 'source');
  assert.equal(nextStep('creer'), null);
  assert.equal(previousStep('creer'), 'destination');
});

test('la première étape ne soumet rien au service — elle n’a rien à valider', () => {
  assert.equal(serverStepFor('preparer'), null);
  assert.equal(serverStepFor('source'), 'permissions');
  // Le choix des tables est jugé avec le suivi des modifications, pas avec la
  // destination : mal mapper l'étape reprochait les tables sur l'écran Snowflake.
  assert.equal(serverStepFor('donnees'), 'journal');
  assert.equal(serverStepFor('destination'), 'destination');
  assert.equal(serverStepFor('creer'), 'verdict');
});

test('chaque étape dit ce qu’elle fait et pourquoi', () => {
  for (const step of journeySteps) {
    const copy = JOURNEY_COPY[step];
    assert.ok(copy.title.length > 0, `${step} doit avoir un titre`);
    assert.ok(copy.why.length > 0, `${step} doit dire pourquoi`);
    assert.ok(copy.next.length > 0, `${step} doit nommer son action`);
  }
});

test('aucun réglage technique n’apparaît dans les textes du parcours', () => {
  const surfaced = [
    ...Object.values(JOURNEY_COPY).flatMap((copy) => [copy.title, copy.why, copy.next]),
    ...PREREQUISITES.flatMap((item) => [item.label, item.detail]),
  ].join(' ').toLowerCase();

  for (const forbidden of [
    'batch', 'poll', 'timeout', 'tls_ca', 'replica', 'pilote', 'sidecar',
    'checkpoint', 'receiver', 'séquence', 'stage', 'journal',
  ]) {
    assert.ok(!surfaced.includes(forbidden), `« ${forbidden} » ne doit pas apparaître : ${surfaced}`);
  }
});

test('le parcours ne demande jamais un mot de passe, seulement où il est déposé', () => {
  const passwordItem = PREREQUISITES.find((item) => item.detail.includes('mot de passe'));
  assert.ok(passwordItem, 'la préparation doit parler du mot de passe');
  assert.match(passwordItem!.detail, /ne demande jamais de mot de passe/);
});

test('l’avancement marque le passé, le présent et le reste', () => {
  const marks = journeyMarks('donnees');
  assert.deepEqual(
    marks.map((mark) => mark.state),
    ['done', 'done', 'current', 'todo', 'todo'],
  );
});

test('erreurs et blocages se disent ensemble : tous deux empêchent d’avancer', () => {
  const check = checkFrom({
    errors: ["L'hôte IBM i est obligatoire"],
    blocked: ['TLS est obligatoire ; le plaintext IBM i est interdit'],
    unproven: [],
  });
  assert.equal(check.passed, false);
  assert.equal(check.problems.length, 2);
});

test('un refus du service est redit dans les mots de l’écran', () => {
  const check = checkFrom({
    errors: ["L'hôte IBM i est obligatoire", 'La référence du secret Kubernetes est obligatoire'],
    blocked: [],
    unproven: [],
  });
  assert.deepEqual(check.problems, [
    'Indiquez l’adresse de votre AS400.',
    'Indiquez où le mot de passe est déposé : le nom du dépôt et celui de l’entrée.',
  ]);
  const surfaced = check.problems.join(' ').toLowerCase();
  for (const forbidden of ['ibm i', 'kubernetes', 'secret', 'tls', 'plaintext']) {
    assert.ok(!surfaced.includes(forbidden), `« ${forbidden} » ne doit pas atteindre l’écran`);
  }
});

test('un refus inconnu est montré tel quel plutôt que tu', () => {
  const inconnu = 'Une règle inédite du service';
  assert.equal(problemCopy(inconnu), inconnu);
  const check = checkFrom({ errors: [inconnu], blocked: [], unproven: [] });
  assert.deepEqual(check.problems, [inconnu]);
});

test('un point non prouvé n’empêche pas d’avancer, mais n’est pas tu', () => {
  const check = checkFrom({
    errors: [],
    blocked: [],
    unproven: ['Connectivité IBM i non observée'],
  });
  assert.equal(check.passed, true);
  assert.deepEqual(check.pending, [
    'La connexion à votre AS400 sera éprouvée à la mise en service.',
  ]);
});

test('les réserves qui disent la même attente ne sont pas répétées', () => {
  const pending = pendingCopy([
    'Connectivité IBM i non observée',
    'Connectivité IBM i non observée',
    'Destination Snowflake non vérifiée par ce formulaire',
  ]);
  assert.equal(pending.length, 2);
});

test('une réserve jamais traduite n’est pas inventée', () => {
  assert.deepEqual(pendingCopy(['Réserve inédite du service']), []);
});

test('un refus qui porte des valeurs est traduit, pas recopié', () => {
  const check = checkFrom({
    errors: [],
    blocked: ['Destination Snowflake non provisionnée pour 2 des 13 tables sélectionnées'],
    unproven: [],
  });
  assert.equal(
    check.problems[0],
    '2 des 13 tables choisies n’ont pas encore d’emplacement prêt dans Snowflake. À faire préparer par votre exploitation.',
  );
});

test('une réserve à valeur variable est traduite elle aussi', () => {
  const pending = pendingCopy([
    '3 table(s) sans clé métier : identifiées par leur position physique (RRN) — une réorganisation de fichier (RGZPFM, CLRPFM) exigera une resynchronisation',
  ]);
  assert.equal(pending.length, 1);
  // RRN, RGZPFM et CLRPFM ne veulent rien dire pour un exploitant data.
  for (const forbidden of ['rrn', 'rgzpfm', 'clrpfm']) {
    assert.ok(!pending[0]!.toLowerCase().includes(forbidden), `« ${forbidden} » subsiste : ${pending[0]}`);
  }
});

test('la destination imposée est redite comme une correction à faire', () => {
  const check = checkFrom({
    errors: [],
    blocked: ['La destination Snowflake doit rester DEV_RAW.AS400_RD'],
    unproven: [],
  });
  assert.match(check.problems[0]!, /DEV_RAW\.AS400_RD/);
  assert.match(check.problems[0]!, /Corrigez la base et le schéma/);
});
