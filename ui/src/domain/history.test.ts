import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import type { ActivityEntry } from './activityJournal.ts';
import type { Pipeline } from './controlPlane.ts';
import { activeAlerts, historyFrom, historyLine, isMeaningful } from './history.ts';

function entry(overrides: Partial<ActivityEntry> = {}): ActivityEntry {
  return {
    at: '2026-09-21T17:30:47+00:00',
    type: 'Étape',
    source: 'example-corp',
    verdict: 'constaté',
    label: 'Capture — Prête à reprendre',
    detail: null,
    origin: 'mesuré',
    tone: 'muted',
    ...overrides,
  };
}

test('les événements qui ne racontent que l’écran sont écartés', () => {
  // « Premier relevé de la session » date de l'ouverture de la page : rejouée
  // demain, elle produirait la même ligne à une autre heure.
  assert.equal(isMeaningful(entry({ type: 'Session' })), false);
  assert.equal(isMeaningful(entry({ type: 'Relevé' })), false);
  assert.equal(isMeaningful(entry({ type: 'Incident' })), true);
});

test('le préfixe de type disparaît du libellé', () => {
  assert.equal(historyLine(entry()).label, 'Prête à reprendre');
});

test('la taxonomie de provenance ne survit pas à la traduction', () => {
  const line = historyLine(entry());
  const surfaced = JSON.stringify(line).toLowerCase();
  for (const forbidden of ['mesuré', 'déduit', 'constaté', 'figé']) {
    assert.ok(!surfaced.includes(forbidden), `« ${forbidden} » ne doit pas rester : ${surfaced}`);
  }
});

test('le journal ne donne pas d’ordre — il raconte', () => {
  const line = historyLine(
    entry({
      type: 'Incident',
      label: 'Cause résolue',
      detail:
        'la source accepte de nouveau la connexion — la capture attend le relancement — Relancer la capture reprend le flux au point d’arrêt.',
      tone: 'attention',
    }),
  );
  assert.ok(line.detail !== null);
  assert.ok(!/relancer la capture/i.test(line.detail!), `l’injonction subsiste : ${line.detail}`);
  assert.match(line.detail!, /la lecture attend d’être relancée/);
});

test('le vocabulaire du service est redit dans celui du produit', () => {
  const line = historyLine(
    entry({
      type: 'Catalogue',
      label: 'Catalogue observé — 218 694 727 lignes sur 13 tables · continuité prouvée',
    }),
  );
  assert.match(line.label, /sans interruption détectée/);
  assert.ok(!/continuité prouvée/.test(line.label));
});

test('deux événements identiques au même instant ne font qu’une ligne', () => {
  const lines = historyFrom([entry(), entry()]);
  assert.equal(lines.length, 1);
});

test('le journal se lit du plus récent au plus ancien', () => {
  const lines = historyFrom([
    entry({ at: '2026-09-19T21:02:01+00:00', label: 'Ancien' }),
    entry({ at: '2026-09-21T17:30:47+00:00', label: 'Récent' }),
  ]);
  assert.deepEqual(lines.map((line) => line.label), ['Récent', 'Ancien']);
});

test('un détail vidé de ses consignes disparaît plutôt que de rester vide', () => {
  const line = historyLine(entry({ detail: 'Relancer la capture reprend le flux.' }));
  assert.equal(line.detail, null);
});

/* ------------------------------------------------------------------ */
/* Alertes de supervision                                              */

function alert(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    fingerprint: 'f1',
    checkId: 'lag_bounded',
    stage: 'capture',
    lifecycleState: 'firing',
    signalStatus: 'breach',
    severity: 'critical',
    reason: 'threshold',
    observed: 900,
    threshold: 600,
    unit: 'seconds',
    firstFiredAt: '2026-09-21T17:00:00+00:00',
    firingSince: '2026-09-21T17:00:00+00:00',
    lastObservedAt: '2026-09-21T17:30:00+00:00',
    resolvedAt: null,
    occurrenceCount: 3,
    evaluationCount: 10,
    ...overrides,
  };
}

function withObservability(overrides: Record<string, unknown>): Pipeline {
  return {
    id: 'example-corp',
    observability: {
      status: 'available',
      quality: { coverage: 'complete', freshness: 'fresh', evidenceKind: 'live' },
      observedAt: '2026-09-21T17:30:00+00:00',
      reason: 'ok',
      checks: [],
      alerts: [],
      ...overrides,
    },
  } as unknown as Pipeline;
}

test('une alerte en cours remonte au journal', () => {
  const lines = activeAlerts(withObservability({ alerts: [alert()] }));
  assert.equal(lines.length, 1);
  assert.equal(lines[0]?.tone, 'attention');
  assert.match(lines[0]!.label, /dépassement/);
});

test('une alerte résolue appartient à l’historique, pas à ce qui appelle une décision', () => {
  const lines = activeAlerts(
    withObservability({ alerts: [alert({ lifecycleState: 'resolved', resolvedAt: '2026-09-21T17:20:00+00:00' })] }),
  );
  assert.deepEqual(lines, []);
});

test('une supervision non rattachée ne fabrique aucune alerte', () => {
  assert.deepEqual(activeAlerts(withObservability({ status: 'unavailable', alerts: [alert()] })), []);
  assert.deepEqual(activeAlerts({ id: 'x' } as unknown as Pipeline), []);
});

test('un relevé de supervision périmé n’affirme pas un problème actuel', () => {
  // Une alerte tirée d'un relevé qui date affirmerait un dépassement qui a
  // peut-être cessé depuis.
  const stale = withObservability({
    quality: { coverage: 'complete', freshness: 'stale', evidenceKind: 'live' },
    alerts: [alert()],
  });
  assert.deepEqual(activeAlerts(stale), []);
});

test('l’alerte dit la mesure et le seuil, sans vocabulaire de supervision', () => {
  const line = activeAlerts(withObservability({ alerts: [alert()] }))[0]!;
  const surfaced = `${line.label} ${line.detail ?? ''}`.toLowerCase();
  for (const forbidden of ['slo', 'breach', 'firing', 'fingerprint', 'lag_bounded']) {
    assert.ok(!surfaced.includes(forbidden), `« ${forbidden} » ne doit pas atteindre l’écran : ${surfaced}`);
  }
  assert.match(line.detail!, /seuil/);
});
