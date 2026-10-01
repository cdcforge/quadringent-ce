"""Protocole de bascule journal (chantier 4) — `executor/boundary.py`.

Couvre : lecture de position valide, jamais de régression enregistrée
(même receiver avec séquence en arrière, retour à un receiver antérieur),
et rotation de receiver acceptée tant que l'horloge avance.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2.executor.boundary import (
    BoundaryError,
    BoundaryRegressionError,
    JournalBoundary,
    assert_no_regression,
    plan_bootstrap,
)

T0 = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)


def _boundary(*, receiver="RCV0001", library="QGPL", sequence=100, at=T0) -> JournalBoundary:
    return JournalBoundary(
        receiver_library=library, receiver_name=receiver, last_sequence=sequence, observed_at=at
    )


def test_boundary_rejects_naive_datetime() -> None:
    with pytest.raises(BoundaryError):
        JournalBoundary(
            receiver_library="QGPL",
            receiver_name="RCV0001",
            last_sequence=1,
            observed_at=datetime(2026, 9, 23),
        )


def test_boundary_rejects_negative_sequence() -> None:
    with pytest.raises(BoundaryError):
        _boundary(sequence=-1)


def test_first_bootstrap_has_no_previous_to_regress_against() -> None:
    boundary = _boundary()
    assert plan_bootstrap(boundary, previous=None) is boundary


def test_same_receiver_non_decreasing_sequence_is_accepted() -> None:
    previous = _boundary(sequence=100)
    candidate = _boundary(sequence=150, at=T0 + timedelta(minutes=5))
    assert plan_bootstrap(candidate, previous=previous) is candidate


def test_same_receiver_equal_sequence_is_accepted() -> None:
    previous = _boundary(sequence=100)
    candidate = _boundary(sequence=100, at=T0 + timedelta(minutes=1))
    assert_no_regression(previous, candidate)  # ne lève pas


def test_same_receiver_decreasing_sequence_is_a_regression() -> None:
    previous = _boundary(sequence=100)
    candidate = _boundary(sequence=50, at=T0 + timedelta(minutes=5))
    with pytest.raises(BoundaryRegressionError):
        plan_bootstrap(candidate, previous=previous)


def test_receiver_rotation_forward_in_time_is_accepted() -> None:
    previous = _boundary(receiver="RCV0001", sequence=9999)
    candidate = _boundary(receiver="RCV0002", sequence=1, at=T0 + timedelta(hours=1))
    assert plan_bootstrap(candidate, previous=previous) is candidate


def test_receiver_change_backward_in_time_is_a_regression() -> None:
    previous = _boundary(receiver="RCV0002", sequence=1, at=T0 + timedelta(hours=1))
    candidate = _boundary(receiver="RCV0001", sequence=9999, at=T0)
    with pytest.raises(BoundaryRegressionError):
        plan_bootstrap(candidate, previous=previous)


def test_round_trip_through_dict() -> None:
    boundary = _boundary()
    restored = JournalBoundary.from_dict(boundary.to_dict())
    assert restored == boundary
