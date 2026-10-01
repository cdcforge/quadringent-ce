"""Contrat du fournisseur d'observation injectable v2 (chantier observabilité).

``NullObservationProvider`` est le défaut fail-closed (comme
``pipeline_executor``/``table_discovery_client`` ailleurs dans v2) : sans
fournisseur réel câblé, chaque champ observé reste ``None`` avec une raison
explicite — jamais 0, jamais une valeur inventée.
"""

from __future__ import annotations

from quadringent_control_plane.v2.services.observation import (
    NO_PROVIDER_REASON,
    NullObservationProvider,
    absent_observation,
    empty_metrics_series,
)


def test_null_provider_observe_returns_absent_fields_with_reason() -> None:
    provider = NullObservationProvider()
    observation = provider.observe("pipeline-1")
    assert observation.observed_state is None
    assert observation.lag_seconds is None
    assert observation.throughput_rows_per_second is None
    assert observation.rows_source is None
    assert observation.rows_destination is None
    assert observation.last_arrival_at is None
    assert observation.history_lag_seconds is None
    assert observation.mirror_lag_seconds is None
    for field_name in (
        "observed_state",
        "lag_seconds",
        "throughput_rows_per_second",
        "rows_source",
        "rows_destination",
        "last_arrival_at",
        "history_lag_seconds",
        "mirror_lag_seconds",
    ):
        assert observation.absent_reasons[field_name] == NO_PROVIDER_REASON


def test_observation_to_dict_carries_streaming_lag_fields() -> None:
    from quadringent_control_plane.v2.services.observation import PipelineObservation

    observation = PipelineObservation(
        observed_state="healthy",
        lag_seconds=1.2,
        throughput_rows_per_second=3.4,
        rows_source=10,
        rows_destination=10,
        last_arrival_at="2026-09-23T08:00:00Z",
        collected_at="2026-09-23T08:00:05Z",
        history_lag_seconds=5.2,
        mirror_lag_seconds=6.3,
    )
    payload = observation.to_dict()
    assert payload["history_lag_seconds"] == 5.2
    assert payload["mirror_lag_seconds"] == 6.3


def test_null_provider_metrics_returns_empty_series() -> None:
    provider = NullObservationProvider()
    series = provider.metrics("pipeline-1", "1h")
    assert series.window == "1h"
    assert series.points == ()
    assert series.provenance == "absent"
    assert series.freshness is None


def test_absent_observation_never_defaults_to_zero() -> None:
    observation = absent_observation("pipeline-1", reason="pipeline jamais démarré")
    payload = observation.to_dict()
    assert payload["rows_source"] is None
    assert payload["rows_destination"] is None
    assert payload["lag_seconds"] is None
    assert payload["absent_reasons"]["rows_source"] == "pipeline jamais démarré"


def test_empty_metrics_series_to_dict_shape() -> None:
    series = empty_metrics_series("24h")
    payload = series.to_dict()
    assert payload == {
        "window": "24h",
        "points": [],
        "provenance": "absent",
        "freshness": None,
        "collected_at": None,
        "reason": NO_PROVIDER_REASON,
    }
