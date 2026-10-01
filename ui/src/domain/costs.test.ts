import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import type { Pipeline } from './controlPlane.ts';
import { snowflakeCostFor } from './operator.ts';
import { observabilityFixture } from './observability.testFixtures.ts';

const now = new Date('2026-09-22T12:00:00Z');
const end = now.getTime() / 1000 - 21600;
function pipeline(value: number | null = 2.5, price: string | null = null): Pipeline {
  const obs = observabilityFixture();
  return {
    quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
    observability: { ...obs, observedAt: now.toISOString(), checks: [{
      id: 'snowflake_credits', stage: 'cost', status: value === null ? 'unobserved' : 'pass',
      observed: value, threshold: 10, unit: 'warehousecredits/delayed24h',
      reason: `metering_window_${end - 86400}_${end}_24`,
    }] },
    costs: { scope: 'warehouse', warehouse: 'EXAMPLE_TEST_WH', pricePerCredit: price, currency: price === null ? null : 'EUR', amount: price === null ? null : '7.750' },
  } as unknown as Pipeline;
}

test('des crédits mesurés restent visibles sans prix déclaré', () => {
  const view = snowflakeCostFor(pipeline(), now);
  assert.equal(view.credits, '2,5');
  assert.equal(view.amount, 'Prix non déclaré');
  assert.match(view.window, /24 h.*6 h/);
  assert.match(view.scope, /warehouse/);
});

test('un zéro mesuré se distingue d’une absence', () => {
  assert.equal(snowflakeCostFor(pipeline(0), now).credits, '0');
  assert.equal(snowflakeCostFor(pipeline(null), now).credits, null);
});

test('un prix déclaré rend le montant calculé et sa nature explicite', () => {
  const view = snowflakeCostFor(pipeline(2.5, '3.10'), now);
  assert.match(view.amount, /7,75/);
  assert.match(view.amountLabel, /calculé/);
});

test('une mesure ancienne ou simulée ne porte jamais un montant actuel', () => {
  const item = pipeline(2.5, '3.10');
  for (const quality of [{ ...item.observability!.quality, freshness: 'stale' as const }, { ...item.observability!.quality, evidenceKind: 'simulation' as const }]) {
    const view = snowflakeCostFor({ ...item, observability: { ...item.observability!, quality } }, now);
    assert.equal(view.amount, 'Non mesuré');
    assert.ok(view.caveat);
  }
  assert.match(snowflakeCostFor(item, new Date('2026-09-23T12:00:00Z')).window, /30 h/);
});

test('une liaison simulée ne devient pas facturée par une observation incohérente', () => {
  const item = pipeline(2.5, '3.10');
  const view = snowflakeCostFor({ ...item, quality: { ...item.quality, evidenceKind: 'simulation' } }, now);
  assert.equal(view.amount, 'Non mesuré');
  assert.equal(view.caveat, 'Données simulées');
});
