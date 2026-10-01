import assert from 'node:assert/strict';
import test from 'node:test';
import { href, inspectHref, parseRoute, statusCopy, surfaceHref } from './router.ts';

test('navigation routes round-trip through the product paths', () => {
  const routes = [
    { name: 'overview' as const },
    { name: 'pipelines' as const },
    { name: 'incidents' as const },
    { name: 'usage' as const },
    { name: 'setup' as const },
    { name: 'wizard' as const, step: 'activate' as const },
    { name: 'wizard' as const, step: 'source' as const },
    { name: 'wizard' as const, step: 'snowflake' as const },
    { name: 'wizard' as const, step: 'tables' as const },
  ];

  for (const route of routes) {
    assert.deepEqual(parseRoute(href(route)), route);
  }
});

test('an unknown wizard step falls back to the activation step, not to the overview', () => {
  assert.deepEqual(parseRoute('#/wizard/unknown-step'), { name: 'wizard', step: 'activate' });
});

test('pipeline detail uses the singular /pipeline path', () => {
  const route = { name: 'pipeline', id: 'dev-cntr', tab: 'live' } as const;
  assert.equal(href(route), '#/pipeline/dev-cntr/live');
  assert.deepEqual(parseRoute('#/pipeline/dev-cntr/live'), route);
});

test('the canonical /source path is a full alias of the pipeline detail', () => {
  const route = { name: 'source', id: 'example-corp' } as const;
  assert.equal(href(route), '#/source/example-corp');
  assert.deepEqual(parseRoute('#/source/example-corp'), route);
  // Onglet et inspection d'étape traversent l'alias comme sur /pipeline.
  assert.deepEqual(
    parseRoute('#/source/example-corp/live?inspect=capture'),
    { name: 'source', id: 'example-corp', tab: 'live', inspect: 'capture' },
  );
  // L'onglet Tables (L2) est routable sur les deux noms canoniques.
  assert.deepEqual(
    parseRoute('#/source/example-corp/tables'),
    { name: 'source', id: 'example-corp', tab: 'tables' },
  );
  assert.deepEqual(
    parseRoute('#/pipeline/example-corp/tables'),
    { name: 'pipeline', id: 'example-corp', tab: 'tables' },
  );
  assert.equal(
    href({ name: 'source', id: 'example-corp', tab: 'tables' }),
    '#/source/example-corp/tables',
  );
  assert.equal(
    href({ name: 'source', id: 'example-corp', tab: 'overview', inspect: 'source' }),
    '#/source/example-corp/overview?inspect=source',
  );
  // Les surfaces non servies tombent en repli comme sur /pipeline.
  assert.deepEqual(parseRoute('#/source/example-corp/integrity'), { name: 'overview' });
  assert.deepEqual(parseRoute('#/source/%ZZ'), { name: 'overview' });
});

test('source inspect deep-links round-trip through Synthèse without becoming a tab', () => {
  const route = { name: 'pipeline', id: 'local-proof', tab: 'overview', inspect: 'source' } as const;
  assert.equal(href(route), '#/pipeline/local-proof/overview?inspect=source');
  assert.deepEqual(parseRoute('#/pipeline/local-proof/overview?inspect=source'), route);
  assert.deepEqual(parseRoute('#/pipeline/local-proof?inspect=source'), { name: 'pipeline', id: 'local-proof', inspect: 'source' });
  assert.deepEqual(parseRoute('#/pipeline/local-proof?inspect=unknown'), { name: 'pipeline', id: 'local-proof' });
  assert.equal(inspectHref('local-proof', 'source'), '#/pipeline/local-proof/overview?inspect=source');
  // La clé de surface distingue un changement d'étape inspectée d'une simple
  // navigation : l'étape demandée reçoit le focus au lieu de rendre la main au workspace.
  assert.equal(surfaceHref(route), '#/pipeline/local-proof/overview?inspect=source');
  assert.equal(
    surfaceHref({ name: 'pipeline', id: 'local-proof', tab: 'overview' }),
    '#/pipeline/local-proof/overview',
  );
  assert.notEqual(
    surfaceHref(route),
    surfaceHref({ name: 'pipeline', id: 'local-proof', tab: 'overview', inspect: 'load' }),
  );
});

test('unsupported pipeline surfaces are absent from routing', () => {
  assert.deepEqual(parseRoute('#/pipeline/dev-cntr/integrity'), { name: 'overview' });
  assert.deepEqual(parseRoute('#/pipeline/dev-cntr/runs'), { name: 'overview' });
  assert.deepEqual(parseRoute('#/pipeline/dev-cntr/audit'), { name: 'overview' });
});

test('invalid encoded route segments fall back to a safe route', () => {
  assert.deepEqual(parseRoute('#/pipeline/%ZZ/integrity'), { name: 'overview' });
});

test('status labels never reduce degraded to healthy', () => {
  assert.equal(statusCopy('degraded').label, 'Couverture partielle');
});

test('cockpit routes (home/connection/table with optional tab) round-trip through the product paths', () => {
  const routes = [
    { name: 'cockpit' as const },
    { name: 'cockpit-confirmations' as const },
    { name: 'cockpit-connection' as const, id: 'src_1' },
    { name: 'cockpit-table' as const, id: 'pl_1' },
    { name: 'cockpit-table' as const, id: 'pl_1', tab: 'logs' as const },
    { name: 'cockpit-table' as const, id: 'pl_1', tab: 'costs' as const },
  ];
  for (const route of routes) {
    assert.deepEqual(parseRoute(href(route)), route);
  }
});

test('an unknown cockpit table tab is dropped, never a fallback to the cockpit home', () => {
  assert.deepEqual(parseRoute('#/cockpit/table/pl_1/unknown-tab'), { name: 'cockpit-table', id: 'pl_1' });
});

test('a cockpit connection route with no id falls back to the overview, like every other malformed route', () => {
  assert.deepEqual(parseRoute('#/cockpit/connection/'), { name: 'overview' });
});

test('the login route round-trips, with or without a returnTo hash', () => {
  assert.deepEqual(parseRoute(href({ name: 'login' })), { name: 'login' });
  const withReturn = { name: 'login', returnTo: '#/cockpit/connection/src_1' } as const;
  assert.deepEqual(parseRoute(href(withReturn)), withReturn);
});

test('a 401 redirect hash built for the login screen parses back to the same returnTo', () => {
  // Forme produite par data/authRedirect.ts::loginRedirectHash — le routeur
  // doit la reconnaître telle quelle, sans double décodage.
  assert.deepEqual(parseRoute('#/login?returnTo=%23%2Fcockpit'), { name: 'login', returnTo: '#/cockpit' });
});

test('the login route carries a prefilled email (post-activation, auto-login failed)', () => {
  const route = { name: 'login', email: 'admin@example.com' } as const;
  assert.deepEqual(parseRoute(href(route)), route);
});
