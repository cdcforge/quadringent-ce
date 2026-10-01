"""Rafraîchissement périodique d'observation, en tâche de fond (chantier
« observabilité v2 », suite).

``ObservationRefreshScheduler`` appelle
``ObservationRefreshService.refresh_all`` à intervalle régulier, protégé
par un ``SchedulerLock`` (bail — voir ``scheduler_lock.py``) : un seul
réplica exécute effectivement le rafraîchissement à un instant donné,
même si le control plane tourne en plusieurs réplicas (StatefulSet/
Deployment). ``run_once`` est la primitive testable (pas de thread, pas de
sommeil) ; ``start``/``stop`` pilotent un thread démon pour la production.
Désactivé par défaut partout (``create_v2_app`` ne le démarre jamais tout
seul) — même discipline que les autres briques injectables de ce chantier."""

from __future__ import annotations

import logging
import threading

from .observation_refresh import ObservationRefreshService
from .scheduler_lock import SchedulerLock

logger = logging.getLogger(__name__)


class ObservationRefreshScheduler:
    def __init__(
        self,
        refresh_service: ObservationRefreshService,
        lock: SchedulerLock,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("l'intervalle doit être positif")
        self._refresh_service = refresh_service
        self._lock = lock
        self._interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> bool:
        """Une itération : tente le bail, rafraîchit si acquis.

        Retourne ``True`` si ce réplica a effectivement rafraîchi (bail
        acquis), ``False`` si un autre réplica détient déjà le bail — dans
        ce cas, aucune lecture d'observation n'est faite ici (le réplica
        qui détient le bail s'en charge).
        """

        if not self._lock.try_acquire():
            return False
        try:
            self._refresh_service.refresh_all()
        except Exception:  # noqa: BLE001 — ne jamais arrêter la boucle sur une erreur d'un cycle
            logger.exception("échec d'un cycle de rafraîchissement d'observation")
        return True

    def start(self) -> None:
        """Démarre la boucle en tâche de fond (thread démon) — idempotent."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def loop() -> None:
            while not self._stop_event.is_set():
                self.run_once()
                self._stop_event.wait(self._interval_seconds)

        self._thread = threading.Thread(target=loop, name="observation-refresh-scheduler", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
            self._thread = None
