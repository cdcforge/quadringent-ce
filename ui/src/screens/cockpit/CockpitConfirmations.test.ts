import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { ConfirmationRecord } from '../../data/controlPlaneV2Client.ts';

let vite: ViteDevServer;
let View: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  View = (await vite.ssrLoadModule('/src/screens/cockpit/CockpitConfirmations.tsx')).CockpitConfirmationsView;
});
after(async () => { await vite.close(); });

const pending: ConfirmationRecord = {
  id: 'cf_1', actionRef: 'pipeline.restart_initial_copy', resourceType: 'pipeline', resourceId: 'pl_1',
  reason: 'Copie initiale demandée', riskEstimate: null, requestedByKind: 'agent', requestedById: 'agent_1',
  expiresAt: '2026-09-29T09:00:00Z', state: 'pending', approvedByKind: null, approvedById: null,
  approvedAt: null, createdAt: '2026-09-28T09:00:00Z', available: { approve: true, reject: true, execute: false },
};

function render(state: unknown): string {
  return renderToStaticMarkup(createElement(View, { state, onRefresh() {}, onDecide() {} }));
}

test('loading, failure and empty inbox each have distinct content', () => {
  assert.match(render({ status: 'loading' }), /Lecture en cours/);
  assert.match(render({ status: 'failed', message: 'Indisponible' }), /Indisponible/);
  assert.match(render({ status: 'ready', items: [] }), /Aucune décision en attente/);
});

test('pending item shows scope, reason and only server-declared decisions', () => {
  const markup = render({ status: 'ready', items: [pending] });
  assert.match(markup, /Relancer la copie initiale/);
  assert.match(markup, /Copie initiale demandée/);
  assert.match(markup, /#\/cockpit\/table\/pl_1/);
  assert.match(markup, />Approuver</);
  assert.match(markup, />Rejeter</);
  const denied = render({ status: 'ready', items: [{ ...pending, available: { approve: false, reject: false, execute: false } }] });
  assert.doesNotMatch(denied, />Approuver</);
  assert.doesNotMatch(denied, />Rejeter</);
});

test('an approved copy can be explicitly executed, then verified, without approving again', () => {
  const approved = render({ status: 'ready', items: [{ ...pending, state: 'approved',
    available: { approve: false, reject: false, execute: true } }] });
  assert.match(approved, /Approuvée/);
  assert.match(approved, />Exécuter l’action</);
  assert.doesNotMatch(approved, />Approuver</);
});
