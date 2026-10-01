"""Adaptateur d'observation réel — curseur de capture dans le stockage objet
(chantier « observabilité v2 », suite).

Reprend ``quadringent.storage_backend.StorageBackend.checkpoint_store`` (déjà
en place pour AWS/GCS, section « Constat de départ » du contrat : « Coûts et
télémétrie […] sont déjà conformes en esprit au design ») pour lire le
``JournalPosition`` (receveur + séquence) réellement publié par la capture —
jamais une valeur calculée ailleurs. Un résolveur injecté
(``pipeline_stream_key``) traduit l'id de pipeline v2 vers la clé de flux de
checkpoint (``stream_key`` — propre au déploiement, pas connue de ce
module) ; sans résolution, l'observation reste absente.

``débit`` et ``dernière arrivée`` exigent deux échantillons : ce module
mémorise, en mémoire par instance, le dernier ``(horodatage, position)`` vu
par pipeline et calcule un débit **réel** (delta de séquence / delta de
temps) seulement à partir du second appel — jamais une estimation sur un
seul point. ``rows_source``/``rows_destination`` et ``observed_state``
restent hors périmètre de cet adaptateur : un ``JournalPosition`` est une
position de curseur (receveur + séquence), pas un dénombrement de lignes
absolu (une séquence redémarre à chaque rotation de receveur — voir
``observation_projection.py`` pour ces trois champs, dérivés du document
console v1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .observation import MetricsSeries, MetricsWindow, PipelineObservation, empty_metrics_series

NO_STREAM_REASON = "aucun flux de checkpoint déclaré pour ce pipeline"
NO_CHECKPOINT_REASON = "aucun checkpoint publié pour ce flux — capture jamais démarrée"
FIRST_SAMPLE_REASON = "premier échantillon depuis le démarrage de ce fournisseur — débit non calculable avant un second relevé"
RECEIVER_ROTATED_REASON = "rotation de receveur entre deux relevés — débit non calculable au travers de la rotation"
ROLLBACK_REASON = "le curseur de checkpoint a reculé entre deux relevés — anomalie, aucune valeur retenue"
NOT_OWNED_REASON = "hors périmètre de l'adaptateur de stockage objet — voir observation_projection.py"
NO_HISTORY_REASON = "l'adaptateur de stockage objet ne conserve pas d'historique — voir observation_projection.py pour la série de retard"

PipelineStreamKeyResolver = Callable[[str], str | None]


@dataclass
class _Sample:
    at: datetime
    receiver: str
    sequence: int
    last_arrival_at: str | None


class StorageBackendObservationAdapter:
    """Fournisseur d'observation adossé au checkpoint de capture réel."""

    def __init__(self, storage_backend: Any, *, pipeline_stream_key: PipelineStreamKeyResolver) -> None:
        self._storage_backend = storage_backend
        self._resolve = pipeline_stream_key
        self._last_sample: dict[str, _Sample] = {}

    def observe(self, pipeline_id: str) -> PipelineObservation:
        stream_key = self._resolve(pipeline_id)
        if stream_key is None:
            return _absent(pipeline_id, reason=NO_STREAM_REASON)
        position = self._storage_backend.checkpoint_store(stream_key).load()
        if position is None:
            return _absent(pipeline_id, reason=NO_CHECKPOINT_REASON)

        now = datetime.now(timezone.utc)
        previous = self._last_sample.get(pipeline_id)
        throughput: float | None = None
        last_arrival_at: str | None = None
        reasons: dict[str, str] = {
            "observed_state": NOT_OWNED_REASON,
            "rows_source": NOT_OWNED_REASON,
            "rows_destination": NOT_OWNED_REASON,
        }

        if previous is None:
            reasons["throughput_rows_per_second"] = FIRST_SAMPLE_REASON
            reasons["last_arrival_at"] = FIRST_SAMPLE_REASON
        elif position.receiver != previous.receiver:
            reasons["throughput_rows_per_second"] = RECEIVER_ROTATED_REASON
            reasons["last_arrival_at"] = RECEIVER_ROTATED_REASON
        elif position.sequence < previous.sequence:
            reasons["throughput_rows_per_second"] = ROLLBACK_REASON
            reasons["last_arrival_at"] = ROLLBACK_REASON
        else:
            delta_sequence = position.sequence - previous.sequence
            elapsed_s = (now - previous.at).total_seconds()
            if elapsed_s > 0:
                throughput = delta_sequence / elapsed_s
            else:
                reasons["throughput_rows_per_second"] = "intervalle nul entre deux relevés — débit non calculable"
            if delta_sequence > 0:
                last_arrival_at = now.isoformat()
            else:
                last_arrival_at = previous.last_arrival_at
                if last_arrival_at is None:
                    reasons["last_arrival_at"] = "aucune progression du curseur observée depuis le premier relevé"

        self._last_sample[pipeline_id] = _Sample(
            at=now, receiver=position.receiver, sequence=position.sequence, last_arrival_at=last_arrival_at
        )
        if throughput is None and "throughput_rows_per_second" not in reasons:
            reasons["throughput_rows_per_second"] = "débit non calculable"
        if last_arrival_at is None and "last_arrival_at" not in reasons:
            reasons["last_arrival_at"] = "aucune arrivée mesurée"

        return PipelineObservation(
            observed_state=None,
            lag_seconds=None,
            throughput_rows_per_second=throughput,
            rows_source=None,
            rows_destination=None,
            last_arrival_at=last_arrival_at,
            collected_at=now.isoformat(),
            absent_reasons=reasons,
        )

    def metrics(self, pipeline_id: str, window: MetricsWindow) -> MetricsSeries:
        return empty_metrics_series(window, reason=NO_HISTORY_REASON)


def _absent(pipeline_id: str, *, reason: str) -> PipelineObservation:
    from .observation import absent_observation

    return absent_observation(pipeline_id, reason=reason)
