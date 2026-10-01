import assert from 'node:assert/strict';
import test from 'node:test';

import { configuredSource } from './source.ts';

test('uses the fixture only when development explicitly opts in', () => {
  const source = configuredSource({
    DEV: true,
    VITE_USE_FIXTURE: 'true',
  });

  assert.equal(source.kind, 'fixture');
});

test('production without a control plane URL fails instead of using the fixture', () => {
  assert.throws(
    () => configuredSource({ DEV: false }),
    /VITE_CONTROL_PLANE_URL is required/,
  );
});

test('production with a control plane URL still does not fall back to the fixture', () => {
  assert.throws(
    () => configuredSource({ DEV: false, VITE_CONTROL_PLANE_URL: 'https://control-plane/' }),
    /Adaptateur temps réel non implémenté/,
  );
});


test('the opt-in fixture contains no historical runtime measurements', async () => {
  const source = configuredSource({ DEV: true, VITE_USE_FIXTURE: 'true' });
  const snapshot = await source.read();
  assert.match(source.label, /synthétique/);
  for (const flux of snapshot.flux) {
    assert.equal(flux.runState.value, 'UNKNOWN');
    assert.equal(flux.timeline.length, 0);
    assert.equal(flux.lag.series.points.length, 0);
    for (const counter of Object.values(flux.counters)) {
      assert.equal(counter.value, null);
      assert.match(counter.source, /synthétique/);
      assert.ok(counter.unknownBecause);
    }
  }
});
