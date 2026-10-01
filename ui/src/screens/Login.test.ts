import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import { ControlPlaneV2Client } from '../data/controlPlaneV2Client.ts';

let vite: ViteDevServer;
let Login: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  Login = (await vite.ssrLoadModule('/src/screens/Login.tsx')).Login;
});

after(async () => { await vite.close(); });

function fakeClient(handler: (input: string, init?: RequestInit) => Response) {
  return new ControlPlaneV2Client({ fetchFn: async (input, init) => handler(input, init) });
}

test('renders exactly one email and one password field, disabled until both are filled', () => {
  const markup = renderToStaticMarkup(
    createElement(Login, { client: fakeClient(() => new Response('{}', { status: 200 })), onLoggedIn: () => {} }),
  );
  assert.match(markup, /Se connecter/);
  assert.match(markup, /<label for="login-email">Adresse e-mail<\/label>/);
  assert.match(markup, /<input id="login-email" type="text"/);
  assert.match(markup, /<label for="login-password">Mot de passe<\/label>/);
  assert.match(markup, /<input id="login-password" type="password"/);
  assert.match(markup, /class="action-button action-button--primary" disabled=""/);
});

test('a prefilled email (post-activation, no session) is carried into the field value', () => {
  const markup = renderToStaticMarkup(
    createElement(Login, {
      client: fakeClient(() => new Response('{}', { status: 200 })),
      prefilledEmail: 'admin@example.com',
      onLoggedIn: () => {},
    }),
  );
  assert.match(markup, /<input id="login-email" type="text" value="admin@example.com"/);
});
