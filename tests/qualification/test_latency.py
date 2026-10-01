"""Tests des statistiques de latence (latency.py)."""

from __future__ import annotations

from quadringent_qualification.latency import percentile, summarize


def test_percentile_empty_list_is_none():
    assert percentile([], 0.5) is None


def test_percentile_p50_odd_count():
    assert percentile([1, 2, 3], 0.5) == 2


def test_percentile_p95_matches_reference_dataset():
    # 12 mesures, comme la campagne de fraîcheur du harnais privé.
    values = [4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 7.9, 8.0, 8.02, 8.03]
    assert percentile(values, 0.95) == 8.02


def test_percentile_rejects_out_of_range():
    try:
        percentile([1.0], 1.5)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_summarize_empty():
    result = summarize([])
    assert result.count == 0
    assert result.p50 is None
    assert result.as_dict() == {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}


def test_summarize_basic():
    result = summarize([1.0, 2.0, 3.0])
    assert result.count == 3
    assert result.p50 == 2.0
    assert result.maximum == 3.0
    assert result.mean == 2.0
