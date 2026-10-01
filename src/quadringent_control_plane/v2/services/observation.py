"""Fournisseur d'observation injectable pour les routes de lecture v2 (chantier
« observabilité v2 », contrat §2.4/§2.5).

Ce module ne connecte rien de réel : comme ``PipelineExecutorProtocol``
(``services/pipelines.py``) ou ``TableDiscoveryClientProtocol``
(``services/tables.py``), il définit un contrat minimal que les routes de
lecture appellent via ``request.app.state``. La brique v1 à réutiliser pour
une implémentation réelle est ``repository.ProjectionRepository`` +
``model.PipelineProjection``/``LagSeriesProjection`` (déjà conformes à la
discipline « jamais de valeur inventée, ``null`` = non mesuré ») : un futur
chantier écrira un adaptateur qui traduit un identifiant de pipeline v2 vers
la clé de projection v1 et retourne des ``PipelineObservation``/
``MetricsWindow`` construits à partir de ``PipelineProjection.lag_seconds``,
``.counters`` et ``.lag_series``. Tant qu'aucun adaptateur n'est injecté, le
fournisseur par défaut (``NullObservationProvider``) répond « absent, aucun
fournisseur configuré » pour chaque champ — jamais 0, jamais une valeur
inventée (même discipline que l'exécuteur/le client de découverte absents).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

# Raison par défaut quand aucun fournisseur d'observation n'est câblé —
# distincte d'une raison d'absence *mesurée* (ex. « pipeline jamais démarré »).
NO_PROVIDER_REASON = "aucun fournisseur d'observation configuré pour ce control plane"

MetricsWindow = Literal["1h", "24h"]


@dataclass(frozen=True)
class PipelineObservation:
    """Etat observé + figures en direct d'un pipeline, jamais mutées ici.

    Chaque champ nullable porte sa raison d'absence dans ``absent_reasons``
    (clé = nom du champ) dès qu'il vaut ``None`` — jamais de 0 par défaut
    (même discipline que ``costs.py``/``infrastructure_costs.py`` v1).
    """

    observed_state: str | None
    lag_seconds: float | None
    throughput_rows_per_second: float | None
    rows_source: int | None
    rows_destination: int | None
    last_arrival_at: str | None
    collected_at: str | None
    # Retards de la voie Snowpipe Streaming (docs/decisions/2026-09-23-miroir-
    # snowflake.md) : history_lag_seconds mesure l'écart entre le commit_timestamp
    # IBM i le plus récent chargé dans l'historique et l'instant de mesure ;
    # mirror_lag_seconds fait de même côté miroir (MIRROR_UPDATED_AT le plus
    # récent). None avec une raison en copy_merge (voie non mesurée) — jamais
    # dérivés de lag_seconds ni d'une autre figure.
    history_lag_seconds: float | None = None
    mirror_lag_seconds: float | None = None
    absent_reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "observed_state": self.observed_state,
            "lag_seconds": self.lag_seconds,
            "throughput_rows_per_second": self.throughput_rows_per_second,
            "rows_source": self.rows_source,
            "rows_destination": self.rows_destination,
            "last_arrival_at": self.last_arrival_at,
            "collected_at": self.collected_at,
            "history_lag_seconds": self.history_lag_seconds,
            "mirror_lag_seconds": self.mirror_lag_seconds,
            "absent_reasons": dict(self.absent_reasons),
        }


def absent_observation(pipeline_id: str, *, reason: str = NO_PROVIDER_REASON) -> PipelineObservation:
    """Observation entièrement absente, avec la même raison pour chaque champ.

    Utilitaire pour les fournisseurs qui échouent fermé (pas de preuve
    disponible) plutôt que d'inventer une valeur.
    """

    fields = (
        "observed_state",
        "lag_seconds",
        "throughput_rows_per_second",
        "rows_source",
        "rows_destination",
        "last_arrival_at",
        "history_lag_seconds",
        "mirror_lag_seconds",
    )
    return PipelineObservation(
        observed_state=None,
        lag_seconds=None,
        throughput_rows_per_second=None,
        rows_source=None,
        rows_destination=None,
        last_arrival_at=None,
        collected_at=None,
        history_lag_seconds=None,
        mirror_lag_seconds=None,
        absent_reasons={name: reason for name in fields},
    )


@dataclass(frozen=True)
class MetricPoint:
    at: str
    lag_seconds: float | None
    throughput_rows_per_second: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "lag_seconds": self.lag_seconds,
            "throughput_rows_per_second": self.throughput_rows_per_second,
        }


@dataclass(frozen=True)
class MetricsSeries:
    """Série retard/débit d'un pipeline — reprend l'esprit de
    ``model.LagSeriesProjection`` (résolution fixe, échantillons inconnus
    comptés séparément) sans dépendre de son type, incompatible avec la clé
    de pipeline v2 (id opaque de la table ``pipelines``, pas un
    ``fleet_id``/``environment`` v1).
    """

    window: MetricsWindow
    points: tuple[MetricPoint, ...]
    provenance: str
    freshness: str | None
    collected_at: str | None
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "window": self.window,
            "points": [point.to_dict() for point in self.points],
            "provenance": self.provenance,
            "freshness": self.freshness,
            "collected_at": self.collected_at,
            "reason": self.reason,
        }


def empty_metrics_series(window: MetricsWindow, *, reason: str = NO_PROVIDER_REASON) -> MetricsSeries:
    return MetricsSeries(window=window, points=(), provenance="absent", freshness=None, collected_at=None, reason=reason)


class PipelineObservationProviderProtocol(Protocol):
    """Contrat minimal d'un fournisseur d'observation — jamais de connexion
    à un vrai backend (Kubernetes/Snowflake/repository v1) ici."""

    def observe(self, pipeline_id: str) -> PipelineObservation: ...

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries: ...


class NullObservationProvider:
    """Fournisseur par défaut : échoue fermé, ne connaît aucun pipeline."""

    def observe(self, pipeline_id: str) -> PipelineObservation:
        return absent_observation(pipeline_id)

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries:
        return empty_metrics_series(window)
