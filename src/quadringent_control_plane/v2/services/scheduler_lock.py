"""Verrou à bail (« lease »), portable SQLite/Postgres, pour les tâches
planifiées en tâche de fond (chantier « observabilité v2 », suite).

Historique : ce module portait sa propre table (``scheduler_locks``), en
parallèle de la table jumelle ``reconciler_leases`` de la boucle de
réconciliation (``services/reconciler.py``) — deux implémentations du même
mécanisme, introduites indépendamment. Depuis la migration
0010_unify_leases, les deux tables sont remplacées par une table unique
``leases`` portée par ``services/lease.py`` ; ``SchedulerLock`` n'est plus
qu'une fine enveloppe autour de ``lease.Lease``, conservée pour ne pas
casser ``services/scheduler.py`` (même nom de classe, même API
``try_acquire``/``release``).

Bail plutôt que verrou de session (``pg_advisory_lock``, propriétaire
Postgres, indisponible sur SQLite en test) : une ligne par nom de tâche
porte ``holder``/``expires_at`` ; l'acquisition est une mise à jour SQL
conditionnelle atomique (« si personne ne le détient, ou si le bail a
expiré, ou si c'est déjà nous »). Un réplica mort libère donc son verrou de
lui-même à l'expiration du bail — jamais de blocage permanent."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.engine import Engine

from .lease import Lease


class SchedulerLock:
    """Un bail nommé, détenu par ``holder`` pendant ``lease_seconds``."""

    def __init__(self, engine: Engine, *, name: str, holder: str, lease_seconds: float = 30.0) -> None:
        # ``Lease`` valide déjà ``ttl_seconds > 0`` et lève le même
        # ``ValueError`` — pas de duplication de ce contrôle ici.
        self._lease = Lease(engine, name=name, holder=holder, ttl_seconds=lease_seconds)

    def try_acquire(self, *, now: datetime | None = None) -> bool:
        """Acquiert ou renouvelle le bail ; ``True`` seulement si ce
        ``holder`` le détient après l'appel."""

        return self._lease.acquire(now=now)

    def release(self) -> None:
        """Libère le bail par anticipation — best-effort, jamais requis pour
        la sûreté (le bail expire de lui-même)."""

        self._lease.release()
