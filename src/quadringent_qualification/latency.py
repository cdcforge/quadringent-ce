"""Statistiques et bornes observées : miroir et objet brut restent distincts."""

from __future__ import annotations

from dataclasses import dataclass
import statistics
import math
from datetime import datetime


def percentile(values: list[float], p: float) -> float | None:
    """Percentile ``p`` (0..1) par interpolation au rang le plus proche (« nearest-rank »).

    Renvoie ``None`` sur une liste vide plutôt que de lever, pour rester
    composable dans un résumé où certaines mesures peuvent manquer.
    """
    if not values:
        return None
    if not 0 <= p <= 1:
        raise ValueError("p doit être compris entre 0 et 1")
    ordered = sorted(values)
    index = min(len(ordered) - 1, round(p * (len(ordered) - 1)))
    return ordered[index]


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50: float | None
    p95: float | None
    maximum: float | None
    mean: float | None

    def as_dict(self) -> dict[str, object]:
        return {"count": self.count, "p50": self.p50, "p95": self.p95, "max": self.maximum, "mean": self.mean}


def summarize(values: list[float]) -> LatencySummary:
    """Résumé p50/p95/max/moyenne d'une série de latences (en secondes)."""
    if not values:
        return LatencySummary(0, None, None, None, None)
    return LatencySummary(
        count=len(values),
        p50=percentile(values, 0.5),
        p95=percentile(values, 0.95),
        maximum=max(values),
        mean=round(statistics.mean(values), 6),
    )


def observed_mirror_latency(start: datetime, end: datetime, monotonic_seconds: float) -> float:
    """Borne haute jusqu'à la fin du SELECT, avec deux horloges cohérentes.

    Le passage exact de la valeur dans Snowflake n'est pas connu : son premier
    SELECT réussi borne la disponibilité. Zéro, temps absent ou saut d'horloge
    ne produisent jamais un échantillon accepté.
    """
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.tzinfo is None or start.utcoffset() is None
            or end.tzinfo is None or end.utcoffset() is None
            or not math.isfinite(monotonic_seconds) or monotonic_seconds <= 0):
        raise ValueError("horloges de fraîcheur invalides")
    wall_seconds = (end - start).total_seconds()
    if wall_seconds <= 0 or abs(wall_seconds - monotonic_seconds) > 1:
        raise ValueError("horloges de fraîcheur incohérentes")
    # Conserver la précision avant admission : arrondir pourrait masquer un dépassement du SLO.
    return max(wall_seconds, monotonic_seconds)
