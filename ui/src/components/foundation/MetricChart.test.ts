import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

let vite: ViteDevServer;
let MetricChart: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  MetricChart = (await vite.ssrLoadModule('/src/components/foundation/MetricChart.tsx')).MetricChart;
});

after(async () => { await vite.close(); });

test('a series with measured values renders an SVG path and the last value', () => {
  const markup = renderToStaticMarkup(createElement(MetricChart, { label: 'Retard', values: [10, 20, 15], unit: ' s' }));
  assert.match(markup, /<svg/);
  assert.match(markup, /<path/);
  assert.match(markup, />15</);
});

test('fractional delivery time and low throughput remain readable', () => {
  const lag = renderToStaticMarkup(createElement(MetricChart, { label: 'Délai IBM i → miroir', values: [31, 10.05], unit: ' s' }));
  const throughput = renderToStaticMarkup(createElement(MetricChart, { label: 'Débit', values: [0.05668451265178483], unit: ' lignes/s' }));
  assert.match(lag, /10,05/);
  assert.match(throughput, /0,06/);
  assert.doesNotMatch(throughput, /0\.05668451265178483/);
});

test('an all-null series renders an explicit "non mesuré" message, never an empty or zeroed chart', () => {
  const markup = renderToStaticMarkup(createElement(MetricChart, { label: 'Débit', values: [null, null], unit: ' l/s' }));
  assert.doesNotMatch(markup, /<svg/);
  assert.match(markup, /Non mesuré/);
});
