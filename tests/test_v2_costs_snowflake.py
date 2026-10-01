"""``SnowflakeWarehouseCostsAdapter`` (``v2/services/costs_snowflake.py``,
chantier observabilité v2 suite) — crédits mesurés via une requête
injectée, jamais de montant sans prix déclaré."""

from __future__ import annotations

from quadringent_control_plane.v2.services.costs_snowflake import (
    NO_PRICE_REASON,
    NO_SAMPLE_REASON,
    SnowflakeCreditsSample,
    SnowflakeWarehouseCostsAdapter,
)


class _FakeQuery:
    def __init__(self, sample: SnowflakeCreditsSample | None) -> None:
        self.sample = sample
        self.calls: list[tuple[str, str | None]] = []

    def query_credits(self, warehouse: str, window: str | None) -> SnowflakeCreditsSample | None:
        self.calls.append((warehouse, window))
        return self.sample


def test_no_sample_returns_absent_with_reason() -> None:
    query = _FakeQuery(None)
    adapter = SnowflakeWarehouseCostsAdapter(query, warehouse="COCKPIT_WH", price_per_credit=3.0, currency="USD")
    snapshot = adapter.get("connection", "conn1", window="24h")
    assert snapshot.status == "absent"
    assert snapshot.reason == NO_SAMPLE_REASON
    assert query.calls == [("COCKPIT_WH", "24h")]


def test_sample_without_declared_price_stays_absent() -> None:
    query = _FakeQuery(SnowflakeCreditsSample(credits=12.0, collected_at="2026-09-23T10:00:00Z", provisional=False))
    adapter = SnowflakeWarehouseCostsAdapter(query, warehouse="COCKPIT_WH", price_per_credit=None, currency="USD")
    snapshot = adapter.get("connection", "conn1", window="24h")
    assert snapshot.status == "absent"
    assert snapshot.amount is None
    assert snapshot.reason == NO_PRICE_REASON
    assert "credits=12.0" in snapshot.basis


def test_finalized_sample_with_price_is_measured() -> None:
    query = _FakeQuery(SnowflakeCreditsSample(credits=10.0, collected_at="2026-09-23T10:00:00Z", provisional=False))
    adapter = SnowflakeWarehouseCostsAdapter(query, warehouse="COCKPIT_WH", price_per_credit=2.5, currency="USD")
    snapshot = adapter.get("connection", "conn1", window="24h")
    assert snapshot.status == "measured"
    assert snapshot.amount == 25.0
    assert snapshot.currency == "USD"
    assert snapshot.collected_at == "2026-09-23T10:00:00Z"


def test_provisional_sample_with_price_is_estimated_not_measured() -> None:
    query = _FakeQuery(SnowflakeCreditsSample(credits=10.0, collected_at="2026-09-23T10:00:00Z", provisional=True))
    adapter = SnowflakeWarehouseCostsAdapter(query, warehouse="COCKPIT_WH", price_per_credit=2.5, currency="USD")
    snapshot = adapter.get("connection", "conn1", window="24h")
    assert snapshot.status == "estimated"
    assert snapshot.amount == 25.0
