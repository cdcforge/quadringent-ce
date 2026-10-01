"""Historique de retard borné en mémoire, écrit pour être tracé.

Le worker ne gardait que ``last_lag_sequences``. La console demande le retard
*dans le temps* — c'est sa métrique reine — et cette série n'existait nulle
part : le script pilote accumulait bien une liste d'entiers, mais sans
horodatage, uniquement pour calculer un verdict de tendance en fin de run.

Trois contraintes commandent la structure.

**La mémoire est bornée.** Un soak de 24 h à un poll par seconde produit 86 400
échantillons. On garde un nombre fixe de seaux et on double leur largeur quand
ils débordent, comme une base de données de séries temporelles : la résolution
se dégrade avec l'âge du run, jamais la mémoire.

**Le plancher survit à la décimation.** C'est le point non négociable. Un seau
qui ne retiendrait que le dernier échantillon effacerait les retours au tail,
c'est-à-dire exactement le signal qui distingue un gros retard sain d'un petit
retard qui part. Chaque seau retient donc son minimum et son maximum, et la
fusion de deux seaux est exacte sur ces deux grandeurs.

**Un retard inconnu reste inconnu.** ``_effective_lag`` rend ``None`` quand le
curseur et le tail sont sur des receivers disjoints. Ces échantillons sont
comptés, pas ignorés : un trou dans la série doit se voir comme un trou, pas
se refermer en silence sur une droite qui n'a jamais été mesurée.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class LagBucket:
    """Un seau de la série. Les bornes sont en secondes depuis le début du run."""

    start_s: float
    end_s: float
    minimum: int | None
    maximum: int | None
    last: int | None
    samples: int
    unknown_samples: int

    @property
    def known_samples(self) -> int:
        return self.samples - self.unknown_samples

    def payload(self) -> dict[str, object]:
        return {
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "min": self.minimum,
            "max": self.maximum,
            "last": self.last,
            "samples": self.samples,
            "unknown_samples": self.unknown_samples,
        }


def _merge(left: LagBucket, right: LagBucket) -> LagBucket:
    knowns = [value for value in (left.minimum, right.minimum) if value is not None]
    peaks = [value for value in (left.maximum, right.maximum) if value is not None]
    return LagBucket(
        start_s=left.start_s,
        end_s=right.end_s,
        minimum=min(knowns) if knowns else None,
        maximum=max(peaks) if peaks else None,
        # Le dernier connu, en remontant : un seau récent tout en inconnus ne
        # doit pas effacer la dernière valeur réellement mesurée.
        last=right.last if right.last is not None else left.last,
        samples=left.samples + right.samples,
        unknown_samples=left.unknown_samples + right.unknown_samples,
    )


class LagHistory:
    """Série de retard à mémoire fixe et résolution dégradante.

    ``capacity`` est le nombre de seaux conservés : c'est aussi, à peu de chose
    près, le nombre de points que la console tracera. 240 tient dans un graphe
    sans le saturer.
    """

    def __init__(self, *, capacity: int = 240, initial_width_s: float = 5.0) -> None:
        if capacity < 4:
            raise ValueError("capacity must leave room for at least four buckets")
        if capacity % 2:
            raise ValueError("capacity must be even so buckets merge in pairs")
        if initial_width_s <= 0:
            raise ValueError("initial bucket width must be positive")
        self.capacity = capacity
        self.width_s = initial_width_s
        self._buckets: list[LagBucket] = []
        self._total = 0
        self._unknown_total = 0

    # -- écriture ---------------------------------------------------------

    def observe(self, *, elapsed_s: float, lag: int | None) -> None:
        """Enregistre un échantillon. ``lag`` vaut ``None`` s'il est incalculable."""

        if elapsed_s < 0:
            raise ValueError("elapsed_s must be non-negative")
        self._total += 1
        if lag is None:
            self._unknown_total += 1

        current = self._buckets[-1] if self._buckets else None
        if current is None or elapsed_s >= current.start_s + self.width_s:
            start = self.width_s * (elapsed_s // self.width_s)
            if current is not None:
                start = max(start, current.start_s + self.width_s)
            self._buckets.append(
                LagBucket(
                    start_s=start,
                    end_s=elapsed_s,
                    minimum=lag,
                    maximum=lag,
                    last=lag,
                    samples=1,
                    unknown_samples=1 if lag is None else 0,
                )
            )
            self._compact()
            return

        self._buckets[-1] = replace(
            current,
            end_s=max(current.end_s, elapsed_s),
            minimum=_least(current.minimum, lag),
            maximum=_greatest(current.maximum, lag),
            last=lag if lag is not None else current.last,
            samples=current.samples + 1,
            unknown_samples=current.unknown_samples + (1 if lag is None else 0),
        )

    def _compact(self) -> None:
        """Double la largeur des seaux et fusionne par paires quand ça déborde."""

        while len(self._buckets) > self.capacity:
            self.width_s *= 2
            merged: list[LagBucket] = []
            for index in range(0, len(self._buckets) - 1, 2):
                merged.append(_merge(self._buckets[index], self._buckets[index + 1]))
            if len(self._buckets) % 2:
                merged.append(self._buckets[-1])
            self._buckets = merged

    # -- lecture ----------------------------------------------------------

    @property
    def buckets(self) -> tuple[LagBucket, ...]:
        return tuple(self._buckets)

    @property
    def sample_count(self) -> int:
        return self._total

    @property
    def unknown_sample_count(self) -> int:
        return self._unknown_total

    def known_series(self) -> list[int]:
        """Les derniers retards connus, seau par seau.

        C'est l'entrée de ``lag_trend`` quand on veut un verdict sur la série
        décimée plutôt que sur les échantillons bruts. Les seaux entièrement
        inconnus sont écartés : ``lag_trend`` classe sur des entiers, et
        inventer une valeur pour combler un trou serait précisément la faute
        que cette structure existe pour éviter.
        """

        return [bucket.last for bucket in self._buckets if bucket.last is not None]

    def peak(self) -> int | None:
        """Le plus grand retard jamais observé, exact malgré la décimation.

        Il ne se lit pas sur ``known_series`` : cette série ne retient que le
        dernier échantillon de chaque seau, et un pic tombé en milieu de seau y
        disparaît. Un pic absorbé est une preuve de solidité — le perdre en
        chemin reviendrait à n'afficher que le régime nominal.
        """

        peaks = [bucket.maximum for bucket in self._buckets if bucket.maximum is not None]
        return max(peaks) if peaks else None

    def floor_thirds(self) -> tuple[int | None, int | None]:
        """Plancher du premier et du dernier tiers de la série.

        La même règle de tiers que ``lag_trend``, mais lue sur les minima des
        seaux et non sur les derniers échantillons : après décimation, c'est la
        seule lecture du plancher qui reste exacte.
        """

        floors = [bucket.minimum for bucket in self._buckets if bucket.minimum is not None]
        if len(floors) < 2:
            return (None, None)
        third = max(1, len(floors) // 3)
        return (min(floors[:third]), min(floors[-third:]))

    def payload(self) -> dict[str, object]:
        return {
            "resolution_s": self.width_s,
            "capacity": self.capacity,
            "sample_count": self._total,
            "unknown_sample_count": self._unknown_total,
            "buckets": [bucket.payload() for bucket in self._buckets],
        }


def _least(current: int | None, value: int | None) -> int | None:
    if value is None:
        return current
    return value if current is None else min(current, value)


def _greatest(current: int | None, value: int | None) -> int | None:
    if value is None:
        return current
    return value if current is None else max(current, value)
