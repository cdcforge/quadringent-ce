"""Adaptateur d'observation réel — documents console/projection v1 (chantier
« observabilité v2 », suite).

Reprend tel quel `repository.ProjectionRepository`/`model.PipelineProjection`
(v1, déjà conforme à la discipline « jamais de valeur inventée, `null` = non
mesuré ») pour fournir `observed_state`/`lag_seconds`/`rows_source`/
`rows_destination` à un pipeline v2. Un pipeline v2 (id opaque de la table
`pipelines`) n'a pas d'équivalent direct dans le modèle v1 (`fleet_id`/
`environment`/origine de document) : un résolveur injecté
(`pipeline_source_spec`) traduit l'id v2 vers une spécification de source v1
(`"evidence_kind:source_id:origin"`, syntaxe de `repository.parse_source_spec`)
— sans résolution déclarée pour un pipeline donné, l'observation reste
absente (jamais un document deviné ou une valeur par défaut).

``throughput_rows_per_second`` et ``last_arrival_at`` ne viennent **pas** de
ce module : la projection v1 ne porte aucun horodatage d'arrivée ni de
débit calculé (seulement des compteurs cumulés et un retard instantané) —
voir `services/observation_storage.py` pour ces deux champs, dérivés du
curseur de capture (checkpoint) dans le stockage objet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ... import repository as v1_repository
from .observation import (
    MetricPoint,
    MetricsSeries,
    MetricsWindow,
    PipelineObservation,
    absent_observation,
    empty_metrics_series,
)

NO_MAPPING_REASON = "aucune source de projection v1 déclarée pour ce pipeline"
UNREADABLE_REASON = "document de projection v1 introuvable ou illisible"
NO_THROUGHPUT_REASON = "la projection v1 ne porte pas de débit calculé (voir l'adaptateur de stockage objet)"
NO_ARRIVAL_REASON = "la projection v1 ne porte pas d'horodatage d'arrivée (voir l'adaptateur de stockage objet)"

PipelineSourceSpecResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class _Resolved:
    pipeline: v1_repository.PipelineProjection | None
    reason: str | None


class ProjectionRepositoryObservationAdapter:
    """Fournisseur d'observation adossé à un document console/projection v1
    par pipeline v2 — une nouvelle lecture (jamais de cache partagé entre
    pipelines) à chaque appel, cohérent avec le principe « toujours relire,
    jamais une valeur mémorisée indéfiniment » déjà en place côté SSE (§3.1
    du contrat : « le flux ne transporte jamais l'état complet »).

    ``PipelineProjection.counters`` ne retient que les clés de
    ``projection.PUBLIC_COUNTERS`` (liste fermée côté v1 — tout compteur
    hors liste est filtré avant d'atteindre ce module, jamais visible ici).
    Aucune de ces clés ne s'appelle littéralement « lignes source »/
    « lignes destination » : les valeurs par défaut retenues sont
    ``events_published`` (événements publiés depuis la source — la
    meilleure approximation v1 d'un compteur de lignes source) et
    ``events_in_target`` (événements visibles côté destination, alimenté
    uniquement quand une preuve de destination existe dans le document).
    ``rows_source_counter``/``rows_destination_counter`` restent
    surchargeables par déploiement (même liste fermée) — jamais de valeur
    par défaut inventée quand le document ne porte pas la clé choisie.
    """

    def __init__(
        self,
        pipeline_source_spec: PipelineSourceSpecResolver,
        *,
        rows_source_counter: str = "events_published",
        rows_destination_counter: str = "events_in_target",
    ) -> None:
        self._resolve = pipeline_source_spec
        self._rows_source_counter = rows_source_counter
        self._rows_destination_counter = rows_destination_counter

    def observe(self, pipeline_id: str) -> PipelineObservation:
        resolved = self._resolve_pipeline(pipeline_id)
        if resolved.pipeline is None:
            return absent_observation(pipeline_id, reason=resolved.reason or UNREADABLE_REASON)
        proj = resolved.pipeline
        counters = proj.counters
        rows_source = _as_int(counters.get(self._rows_source_counter))
        rows_destination = _as_int(counters.get(self._rows_destination_counter))
        reasons: dict[str, str] = {}
        if rows_source is None:
            reasons["rows_source"] = f"compteur « {self._rows_source_counter} » absent du document de projection"
        if rows_destination is None:
            reasons["rows_destination"] = (
                f"compteur « {self._rows_destination_counter} » absent du document de projection"
            )
        reasons["throughput_rows_per_second"] = NO_THROUGHPUT_REASON
        reasons["last_arrival_at"] = NO_ARRIVAL_REASON
        return PipelineObservation(
            observed_state=proj.status,
            lag_seconds=proj.lag_seconds,
            throughput_rows_per_second=None,
            rows_source=rows_source,
            rows_destination=rows_destination,
            last_arrival_at=None,
            collected_at=proj.observed_at,
            absent_reasons=reasons,
        )

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries:
        resolved = self._resolve_pipeline(pipeline_id)
        if resolved.pipeline is None:
            return empty_metrics_series(window, reason=resolved.reason or UNREADABLE_REASON)
        series = resolved.pipeline.lag_series
        if series is None:
            return empty_metrics_series(window, reason="aucune série de retard dans le document de projection")
        # ``start_s``/``end_s`` sont des décalages en secondes relatifs au
        # document (voir `model.LagBucketProjection`) : on ancre le dernier
        # panier sur ``observed_at`` (l'horodatage propre du document) et on
        # recule les paniers précédents du même décalage — décision
        # documentée (aucune autre ancre temporelle n'est portée par le
        # document v1).
        anchor = _parse_iso(resolved.pipeline.observed_at)
        last_end = series.buckets[-1].end_s if series.buckets else 0.0
        points = []
        for bucket in series.buckets:
            at = _offset_iso(anchor, last_end - bucket.end_s) if anchor is not None else resolved.pipeline.observed_at
            lag_value = bucket.last if isinstance(bucket.last, (int, float)) else None
            points.append(MetricPoint(at=at, lag_seconds=lag_value, throughput_rows_per_second=None))
        return MetricsSeries(
            window=window,
            points=tuple(points),
            provenance="lag_series_projection",
            freshness="fresh" if resolved.pipeline.observed_at else None,
            collected_at=resolved.pipeline.observed_at,
        )

    def _resolve_pipeline(self, pipeline_id: str) -> _Resolved:
        spec = self._resolve(pipeline_id)
        if spec is None:
            return _Resolved(None, NO_MAPPING_REASON)
        try:
            source = v1_repository.parse_source_spec(spec)
        except ValueError:
            return _Resolved(None, "spécification de source de projection v1 invalide")
        repo = v1_repository.ProjectionRepository([source])
        snapshot = repo.refresh()
        if not snapshot.pipelines:
            return _Resolved(None, UNREADABLE_REASON)
        return _Resolved(snapshot.pipelines[0], None)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _parse_iso(value: str | None):
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _offset_iso(anchor, seconds_before: float) -> str:
    from datetime import timedelta

    return (anchor - timedelta(seconds=seconds_before)).isoformat()
