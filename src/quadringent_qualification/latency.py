"""Statistiques de latence (fraîcheur écriture -> lot brut durable)."""

from __future__ import annotations

from dataclasses import dataclass
import statistics


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
