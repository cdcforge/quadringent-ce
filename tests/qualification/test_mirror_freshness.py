"""Trois probes relues indépendamment dans le miroir, avec budget borné."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from quadringent_qualification.adapters import CaptureBoundary
from quadringent_qualification.generator import generated_row
from quadringent_qualification.published_probes import PublishedProbe

from .fakes import FakeCaptureRunner, FakeSourceDriver, FakeWarehouseLoader
from .test_orchestrator import make_orchestrator


def mirror_orchestrator(monkeypatch, *, missing=False, delay=0.2, error=False):
    base = datetime(2026, 10, 2, tzinfo=timezone.utc)
    timing = SimpleNamespace(seconds=0.0)
    order = []

    class Source(FakeSourceDriver):
        def execute(self, statements):
            order.append('write')
            timing.seconds += 0.1
            return super().execute(statements)

    class Capture(FakeCaptureRunner):
        def run(self, **kwargs):
            order.append('capture')
            timing.seconds += delay
            if error:
                raise RuntimeError('cleanup secret')
            return super().run(**kwargs)

    source = Source(rows={1: generated_row(1)}, primary_key='ORDER_ID')

    class Warehouse(FakeWarehouseLoader):
        def load(self, **kwargs):
            order.append('load')
            timing.seconds += 0.1
            return super().load(**kwargs)

        def fetch_mirror_value(self, *, schema, row_key, column, timeout_seconds):
            assert row_key == 1 and column == 'LABEL' and 1 <= timeout_seconds <= 10
            order.append('read')
            timing.seconds += 0.1
            return None if missing else source.rows[1]['LABEL']

    warehouse = Warehouse()
    capture = Capture()
    orch = make_orchestrator(('freshness',), source=source, capture=capture, warehouse=warehouse)
    orch.oracle[1] = generated_row(1)
    orch._capture_bootstrap = CaptureBoundary('QUALIF_LIB', 'R1', 1, base)
    orch._last_reconciliation = SimpleNamespace(status='PASS', as_dict=lambda: {'status': 'PASS'})
    orch._clock = lambda: base + timedelta(seconds=timing.seconds)
    orch._monotonic = lambda: round(timing.seconds, 9)
    orch._sleep = lambda duration: setattr(timing, 'seconds', timing.seconds + duration)

    def publications(_storage, *, markers, **kwargs):
        return {marker: PublishedProbe(
            event_id=marker, object_key='raw', created_at=orch._clock(),
        ) for marker in markers}

    monkeypatch.setattr('quadringent_qualification.orchestrator.find_receipted_probes', publications)
    monkeypatch.setattr(orch, '_reconcile', lambda **kwargs: SimpleNamespace(
        status='PASS', as_dict=lambda: {'status': 'PASS'},
    ))
    return orch, order, timing


def test_trois_marqueurs_relus_avant_ecriture_suivante(monkeypatch):
    orch, order, _ = mirror_orchestrator(monkeypatch)
    report = orch.run(('freshness',))
    assert report.status == 'PASS'
    assert order == ['write', 'capture', 'load', 'read'] * 3
    evidence = report.steps[0].details['mirror_measurement']
    assert evidence['count'] == 3 and evidence['accepted'] is True
    assert len({probe['marker'] for probe in evidence['probes']}) == 3
    assert report.freshness.maximum == pytest.approx(0.5)
    assert evidence['steady_state_streaming'] is False


@pytest.mark.parametrize('options,reason', [
    ({'missing': True}, 'freshness_mirror_missing'),
    ({'delay': 10.1}, 'freshness_mirror_slo_exceeded'),
    ({'error': True}, 'freshness_capture_failed'),
])
def test_absence_retard_et_erreur_cleanup_refusent_le_succes(monkeypatch, options, reason):
    orch, order, _ = mirror_orchestrator(monkeypatch, **options)
    report = orch.run(('freshness',))
    assert report.status == 'FAIL'
    assert report.steps[0].details['reason'] == reason
    assert report.steps[0].details['mirror_measurement']['accepted'] is False
    assert order.count('write') == 1
    assert order.count('read') <= 20
    assert report.reconciliation is None
    assert 'cleanup secret' not in str(report.as_dict())


@pytest.mark.parametrize('mode', ['naive', 'backwards'])
def test_horloge_invalide_ne_produit_pas_de_latence(monkeypatch, mode):
    orch, _, timing = mirror_orchestrator(monkeypatch)
    base = datetime(2026, 10, 2, tzinfo=timezone.utc)
    orch._clock = (lambda: datetime(2026, 10, 2)) if mode == 'naive' else (
        lambda: base - timedelta(seconds=timing.seconds)
    )
    report = orch.run(('freshness',))
    assert report.status == 'FAIL'
    assert report.steps[0].details['reason'] == 'freshness_clock_invalid'
    assert report.steps[0].details['mirror_measurement']['accepted'] is False


def test_polling_accepte_uniquement_la_valeur_reellement_relue(monkeypatch):
    orch, order, _ = mirror_orchestrator(monkeypatch)
    original = orch.warehouse.fetch_mirror_value
    calls = 0

    def delayed(**kwargs):
        nonlocal calls
        calls += 1
        value = original(**kwargs)
        return 'ancienne-valeur' if calls % 2 else value

    orch.warehouse.fetch_mirror_value = delayed
    report = orch.run(('freshness',))
    assert report.status == 'PASS'
    assert [probe['poll_count'] for probe in report.steps[0].details['mirror_measurement']['probes']] == [2, 2, 2]
    assert order.count('read') == 6


def test_erreur_select_ne_se_confond_pas_avec_horloge_invalide(monkeypatch):
    orch, _, _ = mirror_orchestrator(monkeypatch)

    def fail(**kwargs):
        raise ValueError('connection secret')

    orch.warehouse.fetch_mirror_value = fail
    report = orch.run(('freshness',))
    assert report.status == 'FAIL'
    assert report.steps[0].details['reason'] == 'freshness_mirror_read_failed'
    assert 'connection secret' not in str(report.as_dict())


def test_budget_configurable_et_nombre_maximal_de_lectures(monkeypatch):
    from quadringent_qualification.config import FreshnessConfig

    orch, order, _ = mirror_orchestrator(monkeypatch, missing=True)
    orch.config = replace(orch.config, freshness=FreshnessConfig(max_seconds=1, max_polls=2))
    report = orch.run(('freshness',))
    assert report.status == 'FAIL'
    assert order.count('read') == 2
    assert report.steps[0].details['mirror_measurement']['slo_seconds'] == 1


@pytest.mark.parametrize('configuration', [
    {'max_seconds': 0}, {'max_seconds': 301}, {'max_seconds': float('nan')},
    {'max_seconds': True}, {'max_polls': 0}, {'max_polls': 41}, {'max_polls': True},
    {'poll_interval_seconds': 0}, {'poll_interval_seconds': 2},
])
def test_budgets_non_bornes_refuses(configuration):
    from quadringent_qualification.config import ConfigError, FreshnessConfig

    with pytest.raises(ConfigError):
        FreshnessConfig(**configuration)


@pytest.mark.parametrize('value', [None, [], 'not-an-object', {'unknown_key': 1}])
def test_configuration_freshness_malformee_leve_configerror(value):
    import json
    from quadringent_qualification.config import ConfigError, parse_config
    from .test_config import JSON_DOC, ENV

    document = json.loads(JSON_DOC)
    document['freshness'] = value
    with pytest.raises(ConfigError, match='freshness'):
        parse_config(json.dumps(document), fmt='json', env=ENV)


def test_seuil_exact_accepte_et_duree_configuree_depassee_refuse(monkeypatch):
    orch, _, _ = mirror_orchestrator(monkeypatch, delay=9.7)
    report = orch.run(('freshness',))
    assert report.status == 'PASS'
    assert report.freshness.maximum == 10.0


def test_probe_sans_duree_mesurable_ne_compte_pas_comme_succes(monkeypatch):
    orch, _, _ = mirror_orchestrator(monkeypatch)
    orch._clock = lambda: datetime(2026, 10, 2, tzinfo=timezone.utc)
    orch._monotonic = lambda: 0.0
    report = orch.run(('freshness',))
    assert report.status == 'FAIL'
    evidence = report.steps[0].details['mirror_measurement']
    assert evidence['accepted'] is False and evidence['count'] == 0
