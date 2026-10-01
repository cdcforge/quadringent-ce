import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let StatusWord: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  StatusWord = (await vite.ssrLoadModule('/src/components/foundation/StatusWord.tsx')).StatusWord;
});

after(async () => { await vite.close(); });

test('le mot du statut est toujours visible, jamais seulement porté par la couleur', () => {
  const markup = renderToStaticMarkup(createElement(StatusWord, { state: 'live' }));
  assert.match(markup, /status-word--live/);
  assert.match(markup, /En direct/);
  // Le carré de couleur est décoratif : il ne doit pas porter seul le sens.
  assert.match(markup, /aria-hidden="true"/);
});

test('les cinq états produisent chacun un libellé français distinct', () => {
  const labels = ['live', 'copying', 'paused', 'attention', 'stopped'].map((state) =>
    renderToStaticMarkup(createElement(StatusWord, { state })));
  assert.match(labels[0], />En direct</);
  assert.match(labels[1], />Copie en cours</);
  assert.match(labels[2], />En pause</);
  assert.match(labels[3], />Attention</);
  assert.match(labels[4], />Arrêté</);
});

test('le rendu compact ajoute son modificateur de classe sans changer le libellé', () => {
  const markup = renderToStaticMarkup(createElement(StatusWord, { state: 'attention', compact: true }));
  assert.match(markup, /status-word--compact/);
  assert.match(markup, />Attention</);
});
