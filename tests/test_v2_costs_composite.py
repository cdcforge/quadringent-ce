"""``FallbackCostsProvider`` (``v2/services/costs_composite.py``, chantier
observabilité v2 suite)."""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2.services.costs import CostSnapshot
from quadringent_control_plane.v2.services.costs_composite import FallbackCostsProvider


class _FixedProvider:
    def __init__(self, snapshot: CostSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def get(self, scope, id_, *, window):
        self.calls += 1
        return self.snapshot


def _absent(reason: str) -> CostSnapshot:
    return CostSnapshot(
        scope="connection", id="c1", window=None, status="absent", amount=None, currency=None, basis=None,
        collected_at=None, reason=reason,
    )


def _measured() -> CostSnapshot:
    return CostSnapshot(
        scope="connection", id="c1", window=None, status="measured", amount=10.0, currency="USD",
        basis="warehouse:x", collected_at="2026-09-23T10:00:00Z", reason=None,
    )


def test_returns_first_non_absent_result() -> None:
    first = _FixedProvider(_absent("premier absent"))
    second = _FixedProvider(_measured())
    provider = FallbackCostsProvider(first, second)
    snapshot = provider.get("connection", "c1", window=None)
    assert snapshot.status == "measured"
    assert first.calls == 1
    assert second.calls == 1


def test_short_circuits_when_first_provider_succeeds() -> None:
    first = _FixedProvider(_measured())
    second = _FixedProvider(_absent("jamais atteint"))
    provider = FallbackCostsProvider(first, second)
    snapshot = provider.get("connection", "c1", window=None)
    assert snapshot.status == "measured"
    assert second.calls == 0


def test_returns_last_absent_reason_when_all_absent() -> None:
    first = _FixedProvider(_absent("raison 1"))
    second = _FixedProvider(_absent("raison 2"))
    provider = FallbackCostsProvider(first, second)
    snapshot = provider.get("connection", "c1", window=None)
    assert snapshot.status == "absent"
    assert snapshot.reason == "raison 2"


def test_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError):
        FallbackCostsProvider()
