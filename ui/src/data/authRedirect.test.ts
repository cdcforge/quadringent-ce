import assert from 'node:assert/strict';
import test from 'node:test';
import { isAuthRedirectExempt, loginRedirectHash, wrapFetchWithAuthRedirect } from './authRedirect.ts';

test('auth/login, auth/logout and auth/me are exempt from the redirect — a 401 there is a normal answer', () => {
  assert.equal(isAuthRedirectExempt('/v2/auth/login'), true);
  assert.equal(isAuthRedirectExempt('/v2/auth/logout'), true);
  assert.equal(isAuthRedirectExempt('/v2/auth/me'), true);
  assert.equal(isAuthRedirectExempt('/v2/users'), false);
  assert.equal(isAuthRedirectExempt('/v2/pipelines'), false);
});

test('loginRedirectHash carries the current hash as returnTo, url-encoded', () => {
  assert.equal(loginRedirectHash('#/cockpit'), '#/login?returnTo=%23%2Fcockpit');
});

test('loginRedirectHash never loops back onto the login screen itself', () => {
  assert.equal(loginRedirectHash('#/login?returnTo=%23%2Fcockpit'), '#/login?returnTo=%23%2Fcockpit');
});

test('loginRedirectHash omits an empty or root returnTo', () => {
  assert.equal(loginRedirectHash(''), '#/login');
  assert.equal(loginRedirectHash('#/'), '#/login');
});

test('wrapFetchWithAuthRedirect navigates to the login screen on a 401 from a protected route', async () => {
  const navigated: string[] = [];
  const wrapped = wrapFetchWithAuthRedirect(
    async () => new Response(JSON.stringify({ error: { code: 'invalid_request', message: 'authentification requise', next_action: '', retryable: false } }), { status: 401 }),
    (hash) => navigated.push(hash),
    () => '#/cockpit',
  );
  const response = await wrapped('/v2/users');
  assert.equal(response.status, 401);
  assert.deepEqual(navigated, ['#/login?returnTo=%23%2Fcockpit']);
});

test('wrapFetchWithAuthRedirect never navigates on a 401 from an exempt auth route', async () => {
  const navigated: string[] = [];
  const wrapped = wrapFetchWithAuthRedirect(
    async () => new Response(JSON.stringify({ error: { code: 'invalid_request', message: '', next_action: '', retryable: false } }), { status: 401 }),
    (hash) => navigated.push(hash),
    () => '#/login',
  );
  await wrapped('/v2/auth/login');
  await wrapped('/v2/auth/me');
  assert.deepEqual(navigated, []);
});

test('wrapFetchWithAuthRedirect never navigates on a successful response', async () => {
  const navigated: string[] = [];
  const wrapped = wrapFetchWithAuthRedirect(
    async () => new Response(JSON.stringify({ items: [] }), { status: 200 }),
    (hash) => navigated.push(hash),
    () => '#/cockpit',
  );
  await wrapped('/v2/users');
  assert.deepEqual(navigated, []);
});
