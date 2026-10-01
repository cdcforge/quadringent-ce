import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let resolveFunctionKeyTrigger: typeof import('./useFunctionKeys.ts').resolveFunctionKeyTrigger;
let useFunctionKeys: typeof import('./useFunctionKeys.ts').useFunctionKeys;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  const module = await vite.ssrLoadModule('/src/components/foundation/useFunctionKeys.ts');
  resolveFunctionKeyTrigger = module.resolveFunctionKeyTrigger;
  useFunctionKeys = module.useFunctionKeys;
});

after(async () => { await vite.close(); });

test('une touche déclarée et non désactivée résout sa liaison', () => {
  const refresh = { key: 'F5' as const, onTrigger: () => {} };
  const resolved = resolveFunctionKeyTrigger([refresh], 'F5', false);
  assert.equal(resolved, refresh);
});

test('aucune liaison ne se déclenche pendant une saisie dans un champ', () => {
  const refresh = { key: 'F5' as const, onTrigger: () => {} };
  assert.equal(resolveFunctionKeyTrigger([refresh], 'F5', true), null);
});

test('une liaison désactivée (aucune cible sur l’écran) ne se déclenche jamais', () => {
  const back = { key: 'F3' as const, onTrigger: () => {}, disabled: true };
  assert.equal(resolveFunctionKeyTrigger([back], 'F3', false), null);
});

test('une touche non déclarée par l’écran ne résout rien', () => {
  const refresh = { key: 'F5' as const, onTrigger: () => {} };
  assert.equal(resolveFunctionKeyTrigger([refresh], 'F12', false), null);
});

test('useFunctionKeys traduit chaque liaison en entrée avec son libellé français et son état désactivé', () => {
  const Host: ComponentType<{ readonly onEntries: (entries: unknown) => void }> = ({ onEntries }) => {
    const entries = useFunctionKeys([
      { key: 'F3', onTrigger: () => {} },
      { key: 'F9', onTrigger: () => {}, disabled: true },
    ]);
    onEntries(entries);
    return null;
  };
  let captured: unknown;
  renderToStaticMarkup(createElement(Host, { onEntries: (entries) => { captured = entries; } }));
  assert.deepEqual(captured, [
    { key: 'F3', label: 'Revenir', disabled: false },
    { key: 'F9', label: 'Pause/Reprise', disabled: true },
  ]);
});
