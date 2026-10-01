import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

import { installTestSite } from '../domain/siteFixture.ts';
import { JOURNEY_COPY, PREREQUISITES, journeyMarks, journeySteps } from '../domain/journey.ts';

installTestSite();

let vite: ViteDevServer;
let Setup: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  (await vite.ssrLoadModule('/src/domain/siteFixture.ts')).installTestSite();
  Setup = (await vite.ssrLoadModule('/src/screens/Setup.tsx')).Setup;
});

after(async () => { await vite.close(); });

test('le parcours ouvre sur « Avant de commencer », avec un titre focusable hors ordre de tabulation', () => {
  const markup = renderToStaticMarkup(createElement(Setup, { state: { status: 'ready' } }));
  assert.match(markup, /<h1 class="journey__title" tabindex="-1">Relier un AS400 à Snowflake<\/h1>/);
  assert.match(markup, /<h2 class="journey__step-title" id="journey-step-title">Avant de commencer<\/h2>/);
  assert.match(markup, /Trois informations suffisent\. Rassemblez-les, la suite prend deux minutes\./);
});

test('l’indicateur d’avancement porte les cinq étapes, la première est courante', () => {
  const markup = renderToStaticMarkup(createElement(Setup, { state: { status: 'ready' } }));
  assert.match(markup, /<ol class="journey__marks" aria-label="Avancement">/);
  assert.equal((markup.match(/class="journey__mark journey__mark--/g) ?? []).length, 5);
  assert.match(markup, /class="journey__mark journey__mark--current"><span class="journey__mark-label">Préparation<\/span>/);
  assert.equal((markup.match(/journey__mark--todo/g) ?? []).length, 4);
  assert.doesNotMatch(markup, /journey__mark--done/);

  // La fonction pure sous-jacente porte les mêmes cinq étapes, dans l’ordre.
  // « Données » est la troisième : le choix des tables précède la destination,
  // comme le service les valide.
  const marks = journeyMarks('donnees');
  assert.deepEqual(marks.map((mark) => mark.step), [...journeySteps]);
  assert.deepEqual(marks.map((mark) => mark.state), ['done', 'done', 'current', 'todo', 'todo']);
});

test('la checklist de préparation liste les trois prérequis, sans jamais demander le mot de passe', () => {
  const markup = renderToStaticMarkup(createElement(Setup, { state: { status: 'ready' } }));
  for (const prerequisite of PREREQUISITES) {
    assert.match(markup, new RegExp(prerequisite.label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
  assert.match(markup, /Quadringent ne demande jamais de mot de passe/);
  assert.doesNotMatch(markup, /type="password"/);
});

test('le bouton d’avancement affiche le libellé de l’étape et reste actif même hors ligne', () => {
  const ready = renderToStaticMarkup(createElement(Setup, { state: { status: 'ready' } }));
  assert.match(ready, /<button type="button" class="journey__next"[^>]*>Commencer<\/button>/);
  assert.doesNotMatch(ready, /journey__back/);

  const failed = renderToStaticMarkup(createElement(Setup, { state: { status: 'failed', connection: 'offline', message: 'offline' } }));
  assert.match(failed, /Le service ne répond pas\. Vous pouvez remplir le parcours, mais la création attendra son retour\./);
  assert.doesNotMatch(failed, /<button[^>]*class="journey__next"[^>]*disabled=""/);
});

test('aucun jargon interne ni contrôle générique n’atteint l’écran d’installation', () => {
  const markup = renderToStaticMarkup(createElement(Setup, { state: { status: 'ready' } }));
  assert.doesNotMatch(markup, /preuve|certification|verdict|contrat|Kubernetes|pilot_max_seconds|batch_entries/i);
  assert.doesNotMatch(markup, /<select/);
});

test('les copies des cinq étapes du parcours restent renseignées et cohérentes', () => {
  for (const step of journeySteps) {
    const copy = JOURNEY_COPY[step];
    assert.ok(copy.title.length > 0, `étape ${step} sans titre`);
    assert.ok(copy.why.length > 0, `étape ${step} sans justification`);
    assert.ok(copy.next.length > 0, `étape ${step} sans libellé de bouton`);
  }
  assert.equal(JOURNEY_COPY.creer.next, 'Créer la liaison');
});

test('les styles du parcours existent et restent sans décor interdit', () => {
  const css = readFileSync(new URL('../styles/journey.css', import.meta.url), 'utf8');
  assert.match(css, /\.journey\s*\{/);
  assert.match(css, /\.journey__marks\s*\{/);
  assert.doesNotMatch(css, /linear-gradient|radial-gradient|backdrop-filter|blur\(/);
});
