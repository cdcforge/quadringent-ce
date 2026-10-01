"""Tests du rendu de rapport (report.py)."""

from __future__ import annotations

import json

from quadringent_qualification.orchestrator import RunReport, StepResult
from quadringent_qualification.reconcile import (
    Boundary,
    JournalContinuity,
    HistoryIdentityCheck,
    ReconciliationReport,
    ReplayCheck,
    SetDiff,
)
from quadringent_qualification.report import render_markdown, to_json, to_markdown


def make_report(status_step: str = "PASS") -> RunReport:
    report = RunReport(run_id="qual-test-1")
    report.steps.append(StepResult(name="seed", status=status_step, details={"statements": 100}))
    return report


def test_to_json_round_trips_through_json_loads():
    report = make_report()
    data = json.loads(to_json(report))
    assert data["run_id"] == "qual-test-1"
    assert data["status"] == "PASS"
    assert data["steps"][0]["name"] == "seed"


def test_to_json_fail_status_when_a_step_failed():
    report = make_report(status_step="FAIL")
    data = json.loads(to_json(report))
    assert data["status"] == "FAIL"


def test_empty_or_skipped_run_does_not_report_pass():
    assert RunReport(run_id="empty").status == "FAIL"
    report = RunReport(run_id="skipped", steps=[StepResult(name="freshness", status="SKIPPED")])
    assert report.status == "FAIL"


def test_to_markdown_contains_run_id_and_step_table():
    report = make_report()
    md = to_markdown(report)
    assert "qual-test-1" in md
    assert "| seed | PASS" in md


def test_to_markdown_without_reconciliation_says_not_executed():
    report = make_report()
    md = to_markdown(report)
    assert "non exécutée dans ce run" in md


def test_to_markdown_cost_placeholder_marked_absent_not_zero():
    report = make_report()
    md = to_markdown(report)
    assert "Absent" in md
    assert "0 €" not in md


def _full_reconciliation() -> ReconciliationReport:
    empty = SetDiff((), (), (), 5)
    return ReconciliationReport(
        counts={"oracle_keys": 5, "source_keys": 5, "destination_keys": 5, "snapshot_events": 5,
                "journal_events": 0, "raw_rows": 5, "raw_distinct_events": 5},
        boundary=Boundary(1, 5, None, None, 0),
        journal_continuity=JournalContinuity(0, 0, (), (), ()),
        replay=ReplayCheck(0, 0, 0),
        before_image_mismatches=(),
        oracle_vs_destination=empty, oracle_vs_source=empty, source_vs_destination=empty,
        deleted_keys_absent=True,
        oracle_vs_mirror=empty, destination_vs_mirror=empty,
        history=HistoryIdentityCheck(5, 5, 0, ()),
    )


def test_to_markdown_with_passing_reconciliation():
    report = make_report()
    report.reconciliation = _full_reconciliation()
    md = to_markdown(report)
    assert "**PASS**" in md
    assert "Oracle vs destination : égal" in md
    assert "Oracle vs miroir : égal" in md
    assert "Historique Snowflake : 5 ligne(s) physique(s), 5 EVENT_ID distinct(s), 0 doublon(s)" in md


def test_render_markdown_from_plain_dict_round_trip():
    report = make_report()
    report.reconciliation = _full_reconciliation()
    data = json.loads(to_json(report))
    md = render_markdown(data)
    assert "qual-test-1" in md
    assert "Oracle vs destination : égal" in md


def test_render_markdown_accepts_previous_sequence_only_report():
    report = make_report()
    report.reconciliation = _full_reconciliation()
    data = json.loads(to_json(report))
    continuity = data["reconciliation"]["journal_continuity"]
    continuity.pop("missing_positions")
    continuity.pop("unexpected_positions")
    continuity.pop("duplicate_positions")
    continuity.update({"missing_sequences": [], "unexpected_sequences": [], "duplicate_sequences": 0})
    assert "Continuité de journal : OK" in render_markdown(data)


def test_historical_report_does_not_invent_provenance_or_mirror_proof():
    data = json.loads(to_json(make_report()))
    data.pop("execution_mode")
    data["reconciliation"] = _full_reconciliation().as_dict()
    data["reconciliation"].pop("mirror")
    data["reconciliation"].pop("history")
    markdown = render_markdown(data)
    assert "Provenance : absente" in markdown
    assert "Miroir Snowflake : absent" in markdown
    assert "Historique Snowflake : absent" in markdown


def test_report_keeps_physical_history_duplicates_visible():
    report = make_report()
    report.reconciliation = _full_reconciliation()
    from dataclasses import replace
    report.reconciliation = replace(report.reconciliation, history=HistoryIdentityCheck(7, 5, 2, ("observed-id",)))
    assert report.status == "FAIL"
    data = json.loads(to_json(report))
    assert data["reconciliation"]["history"]["duplicate_event_ids"] == ["observed-id"]
    assert "7 ligne(s) physique(s), 5 EVENT_ID distinct(s), 2 doublon(s), snapshots compris" in to_markdown(report)


def test_partial_real_pass_is_only_selected_steps_success():
    report = make_report()
    report.execution_mode = 'real'
    data = json.loads(to_json(report))
    assert data['coverage']['missing_required_steps'] == ['snapshot', 'capture', 'reconcile', 'freshness', 'changes1', 'changes2', 'rotate', 'changes3']
    assert data['coverage']['complete_product_status'] == 'NOT_VALIDATED'
    assert 'Statut des étapes sélectionnées : PASS' in to_markdown(report)
    assert 'Statut global' not in to_markdown(report)


def test_fake_and_old_reports_never_claim_complete_qualification():
    report = make_report()
    report.execution_mode = 'offline_fake'
    assert json.loads(to_json(report))['coverage']['complete_product_status'] == 'NOT_VALIDATED'
    old = report.as_dict()
    old.pop('execution_mode')
    assert 'Qualification produit complète : NOT_VALIDATED' in render_markdown(old)
    assert 'unknown' in render_markdown(old)


def test_raw_latency_is_not_mirror_freshness_evidence():
    data = make_report().as_dict()
    data['freshness'] = {'count': 3, 'p50': 1, 'p95': 2, 'max': 2}
    assert 'Fraîcheur du miroir : unknown' in render_markdown(data)


def test_mirror_freshness_requires_measured_threshold_not_raw_latency():
    report, measurement = _mirror_report()
    assert "Fraîcheur du miroir : PASS" in to_markdown(report)
    measurement["max_seconds"] = measurement["p95_seconds"] = 11
    measurement["probes"][2]["observed_upper_bound_seconds"] = 11
    assert "Fraîcheur du miroir : FAIL" in to_markdown(report)
    assert "Qualification produit complète : NOT_VALIDATED" in to_markdown(report)
    measurement["max_seconds"] = float("nan")
    assert "Fraîcheur du miroir : unknown" in to_markdown(report)


def _mirror_report():
    report = make_report()
    report.execution_mode = 'real'
    measurement = {'target': 'snowflake_mirror', 'metric': 'write_to_mirror_observed_upper_bound',
                   'scope': 'sql_loader_bounded_docker_capture', 'steady_state_streaming': False,
                   'count': 3, 'p95_seconds': 9, 'max_seconds': 9, 'slo_seconds': 10,
                   'accepted': True, 'status': 'PASS',
                   'probes': [{'marker': str(i), 'observed_upper_bound_seconds': i + 7,
                               'poll_count': 1} for i in range(3)]}
    report.steps.append(StepResult(name='freshness', status='PASS', details={'mirror_measurement': measurement}))
    return report, measurement


def test_seed_is_required_even_when_other_steps_pass():
    report = make_report()
    report.steps.clear()
    assert 'seed' in json.loads(to_json(report))['coverage']['missing_required_steps']


def test_mirror_rejects_fabricated_probe_evidence():
    from copy import deepcopy
    report, measurement = _mirror_report()
    for changes in [{'probes': []}, {'p95_seconds': 8}, {'scope': 'unknown'},
                    {'steady_state_streaming': True}, {'slo_seconds': True}]:
        changed = deepcopy(measurement)
        changed.update(changes)
        report.steps[-1].details['mirror_measurement'] = changed
        assert 'Fraîcheur du miroir : PASS' not in to_markdown(report)
    for field, value in [('marker', ''), ('marker', '1'), ('observed_upper_bound_seconds', 11),
                         ('observed_upper_bound_seconds', float('nan')), ('poll_count', True),
                         ('poll_count', 0), ('poll_count', 1000001)]:
        changed = deepcopy(measurement)
        changed['probes'][0][field] = value
        report.steps[-1].details['mirror_measurement'] = changed
        assert 'Fraîcheur du miroir : PASS' not in to_markdown(report)


def test_offline_mirror_verdict_explicitly_simulated():
    report, _ = _mirror_report()
    report.execution_mode = 'offline_fake'
    assert 'Fraîcheur du miroir : PASS (simulation)' in to_markdown(report)


def test_mirror_rejects_runtime_impossible_polls_and_zero_duration():
    report, measurement = _mirror_report()
    measurement['probes'][0]['poll_count'] = 41
    assert 'Fraîcheur du miroir : PASS' not in to_markdown(report)


def test_mirror_rejects_three_zero_durations():
    report, measurement = _mirror_report()
    for probe in measurement['probes']:
        probe['observed_upper_bound_seconds'] = 0
    measurement['max_seconds'] = measurement['p95_seconds'] = 0
    assert 'Fraîcheur du miroir : PASS' not in to_markdown(report)
