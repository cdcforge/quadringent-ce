import assert from 'node:assert/strict';
import test from 'node:test';

import { observabilityFixture } from './observability.testFixtures.ts';
import {
  observabilityBoundary,
  observabilityIsCurrent,
  orderedSloChecks,
  sloCheckLabel,
  sloReasonLabel,
  sloValueLabel,
} from './observability.ts';

test('SLO copy stays public, localized and does not expose unknown raw identifiers', () => {
  assert.match(sloReasonLabel('metering_window_1788156000_1788242400_2'), /UTC.*2 relevés/);
  assert.equal(sloReasonLabel('metering_window_1788156000_1788242401_2'), 'Motif non publié');
  assert.equal(sloCheckLabel('snowpipe_queue'), 'File Snowpipe en attente');
  assert.equal(sloCheckLabel('secret_internal_probe'), 'Contrôle retiré');
  assert.equal(sloReasonLabel('threshold_exceeded'), 'Limite dépassée');
  assert.equal(sloReasonLabel('jdbc_operator_secret'), 'Motif non publié');
  assert.equal(sloValueLabel('OPERATOR_SECRET', 'state'), 'État non publié');
  assert.equal(
    sloValueLabel(['RUNNING', 'OPERATOR_SECRET'], 'state'),
    'En cours ou État non publié',
  );
});

test('SLO values preserve exact measurements with readable singular units', () => {
  assert.equal(sloValueLabel(1, 'warehousecredits/delayed24h'), '1 crédit warehouse / 24 h différées');
  assert.equal(sloValueLabel(1, 'sequences'), '1 position');
  assert.equal(sloValueLabel(0, 'errors'), '0 erreurs');
  assert.equal(sloValueLabel('RUNNING', 'state'), 'En cours');
  assert.equal(sloValueLabel(['RUNNING', 'STOPPED_BUDGET'], 'state'), 'En cours ou Arrêt planifié');
  assert.equal(sloValueLabel(null, 'files'), 'Non mesuré');
});

test('controls follow the operator path and current eligibility is non-compensatory', () => {
  const live = observabilityFixture({ status: 'pass' });
  const ordered = orderedSloChecks([...live.checks].reverse());

  assert.equal(ordered[0]?.id, 'capture_freshness');
  assert.equal(ordered.at(-1)?.id, 'observability_freshness');
  assert.equal(observabilityIsCurrent(live, true), true);
  assert.equal(observabilityIsCurrent({ ...live, quality: { ...live.quality, freshness: 'stale' } }, true), false);
  assert.equal(observabilityIsCurrent(live, false), false);
  assert.equal(observabilityBoundary({ ...live, quality: { ...live.quality, freshness: 'stale' } }), 'Observation des contrôles périmée');
});
