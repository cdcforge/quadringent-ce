import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let MetricTile: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  MetricTile = (await vite.ssrLoadModule('/src/components/foundation/MetricTile.tsx')).MetricTile;
});

after(async () => { await vite.close(); });

test('une mesure présente rend la valeur en mono avec sa provenance et son âge', () => {
  const markup = renderToStaticMarkup(createElement(MetricTile, {
    label: 'Retard', value: '42', unit: 's', provenance: 'mesuré', age: 'il y a 3 min',
  }));
  assert.match(markup, /metric-tile__value mono/);
  assert.match(markup, />42</);
  assert.match(markup, />s</);
  assert.match(markup, /mesuré/);
  assert.match(markup, /il y a 3 min/);
  assert.doesNotMatch(markup, /—/);
});

test('une mesure absente rend « — » et le mot « absent », jamais 0 ni une estimation silencieuse', () => {
  const markup = renderToStaticMarkup(createElement(MetricTile, {
    label: 'Retard', value: null, provenance: 'absent',
  }));
  assert.match(markup, /metric-tile__value--absent/);
  assert.match(markup, />—</);
  assert.match(markup, />absent</);
  assert.doesNotMatch(markup, />0</);
});

test('une valeur estimée porte le mot « estimé », distinct d’une mesure', () => {
  const markup = renderToStaticMarkup(createElement(MetricTile, {
    label: 'Coût mensuel', value: '12,50', unit: '€', provenance: 'estimé',
  }));
  assert.match(markup, /estimé/);
  assert.doesNotMatch(markup, /metric-tile__value--absent/);
});
