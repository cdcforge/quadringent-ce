import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import { ControlPlaneV2Client } from '../../data/controlPlaneV2Client.ts';
import { createWizardDemoFetch } from '../../data/fixtures/wizardDemo.ts';

let vite: ViteDevServer;
let WizardOnboarding: ComponentType<any>;
let shouldLoadExistingSource: (step: string) => boolean;
let selectResumeSource: (sources: readonly { id: string }[]) => { id: string } | null;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  WizardOnboarding = (await vite.ssrLoadModule('/src/screens/wizard/WizardOnboarding.tsx')).WizardOnboarding;
  shouldLoadExistingSource = (await vite.ssrLoadModule('/src/screens/wizard/WizardOnboarding.tsx')).shouldLoadExistingSource;
  selectResumeSource = (await vite.ssrLoadModule('/src/screens/wizard/WizardOnboarding.tsx')).selectResumeSource;
});

after(async () => { await vite.close(); });

// Un client de démonstration est injecté explicitement dans chaque test :
// WizardOnboarding ne référence plus les fixtures lui-même (elles sont
// chargées dynamiquement, uniquement en développement, dans wizardClient.ts)
// pour ne jamais apparaître dans le bundle de production.
const demoClient = () => new ControlPlaneV2Client({ fetchFn: createWizardDemoFetch() });

test('activation never reads a protected source list before authentication', () => {
  assert.equal(shouldLoadExistingSource('activate'), false);
  assert.equal(shouldLoadExistingSource('source'), true);
  assert.equal(shouldLoadExistingSource('snowflake'), true);
  assert.equal(shouldLoadExistingSource('tables'), true);
});

test('wizard resumes the most recently created source after credentials change', () => {
  assert.equal(selectResumeSource([]), null);
  assert.equal(selectResumeSource([{ id: 'old' }, { id: 'new' }])?.id, 'new');
});

test('without a token, the activation step refuses the form and explains why, never inviting a password to be typed', () => {
  const markup = renderToStaticMarkup(createElement(WizardOnboarding, { step: 'activate', activationToken: null, client: demoClient() }));
  assert.match(markup, /Activer votre compte administrateur/);
  assert.match(markup, /lien d.activation est invalide ou a déjà été utilisé/);
  assert.doesNotMatch(markup, /type="password"/);
});

test('with a token, the activation step shows exactly one password field and a disabled primary action', () => {
  const markup = renderToStaticMarkup(createElement(WizardOnboarding, { step: 'activate', activationToken: 'tok_abc', client: demoClient() }));
  assert.match(markup, /<input id="wizard-activate-password" type="password"/);
  assert.match(markup, /Activer le compte/);
  assert.match(markup, /class="action-button action-button--primary" disabled=""/);
});

test('the source step opens on host/account/password, with ports folded under "Options avancées"', () => {
  const markup = renderToStaticMarkup(createElement(WizardOnboarding, { step: 'source', client: demoClient() }));
  assert.match(markup, /Adresse et compte/);
  assert.match(markup, /<label for="wizard-source-host">Adresse \(hôte ou IP\)<\/label>/);
  assert.match(markup, /<label for="wizard-source-account">Compte<\/label>/);
  assert.match(markup, /<input id="wizard-source-password" type="password"/);
  assert.match(markup, /<details class="wizard__advanced"[^>]*>/);
  assert.doesNotMatch(markup, /<details class="wizard__advanced"[^>]* open=""/);
  assert.match(markup, /Options avancées \(ports\)/);
  // Ni le formulaire vide ni l'absence de test ne laissent Continuer actif.
  assert.match(markup, />Continuer<\/button>/);
  assert.match(markup, /aria-keyshortcuts="F3"/);
  assert.match(markup, /aria-keyshortcuts="F5"/);
});

test('the snowflake step never asks for a Snowflake admin secret — only the account identifier', () => {
  const markup = renderToStaticMarkup(createElement(WizardOnboarding, { step: 'snowflake', client: demoClient() }));
  assert.match(markup, /Votre compte Snowflake/);
  assert.match(markup, /<label for="wizard-snowflake-account">Identifiant de compte<\/label>/);
  assert.doesNotMatch(markup, /type="password"/);
  assert.doesNotMatch(markup, /mot de passe/i);
});

test('the tables step opens on a search field and a table, with Démarrer disabled with nothing selected', () => {
  const markup = renderToStaticMarkup(createElement(WizardOnboarding, { step: 'tables', client: demoClient() }));
  assert.match(markup, /Choisir les tables à copier/);
  assert.match(markup, /<label for="wizard-tables-search">/);
  assert.match(markup, /<table class="banded-table">/);
  assert.match(markup, />Démarrer<\/button>/);
});
