"""Compose les deux adaptateurs d'observation réels (chantier « observabilité
v2 », suite) en un seul ``PipelineObservationProviderProtocol``.

``observed_state``/``lag_seconds``/``rows_source``/``rows_destination``
sont possédés par ``ProjectionRepositoryObservationAdapter`` (documents
console/projection v1) ; ``throughput_rows_per_second``/``last_arrival_at``
par ``StorageBackendObservationAdapter`` (checkpoint de capture). Chaque
champ vient strictement de son propriétaire désigné — pas de repli croisé
qui masquerait une raison d'absence réelle par une autre. La série de
métriques (``metrics()``) vient uniquement de l'adaptateur de projection
(le seul des deux à porter un historique, via ``lag_series``)."""

from __future__ import annotations

from .observation import MetricsSeries, MetricsWindow, PipelineObservation
from .observation_projection import ProjectionRepositoryObservationAdapter
from .observation_storage import StorageBackendObservationAdapter

_STATE_FIELDS = ("observed_state", "lag_seconds", "rows_source", "rows_destination")
_STORAGE_FIELDS = ("throughput_rows_per_second", "last_arrival_at")


class CompositeObservationProvider:
    def __init__(
        self,
        projection_adapter: ProjectionRepositoryObservationAdapter,
        storage_adapter: StorageBackendObservationAdapter,
    ) -> None:
        self._projection = projection_adapter
        self._storage = storage_adapter

    def observe(self, pipeline_id: str) -> PipelineObservation:
        from_projection = self._projection.observe(pipeline_id)
        from_storage = self._storage.observe(pipeline_id)
        reasons: dict[str, str] = {}
        for field in _STATE_FIELDS:
            reason = from_projection.absent_reasons.get(field)
            if reason is not None:
                reasons[field] = reason
        for field in _STORAGE_FIELDS:
            reason = from_storage.absent_reasons.get(field)
            if reason is not None:
                reasons[field] = reason
        collected_at = from_projection.collected_at or from_storage.collected_at
        return PipelineObservation(
            observed_state=from_projection.observed_state,
            lag_seconds=from_projection.lag_seconds,
            throughput_rows_per_second=from_storage.throughput_rows_per_second,
            rows_source=from_projection.rows_source,
            rows_destination=from_projection.rows_destination,
            last_arrival_at=from_storage.last_arrival_at,
            collected_at=collected_at,
            absent_reasons=reasons,
        )

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries:
        return self._projection.metrics(pipeline_id, window)
