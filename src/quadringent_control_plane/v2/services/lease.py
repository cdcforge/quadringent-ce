"""Bail (lease) applicatif unifié, portable SQLite/Postgres — sûreté à
plusieurs réplicas (migration 0010_unify_leases, table ``leases``).

Ce module remplace deux implémentations jumelles introduites indépendamment
par deux chantiers menés en parallèle : la boucle de réconciliation
(``services/reconciler.py``, table ``reconciler_leases`` — 0007) et
l'ordonnanceur de rafraîchissement d'observation (``services/scheduler.py``,
table ``scheduler_locks`` — 0009). Même mécanisme dupliqué à deux endroits :
une ligne par nom de bail, ``holder``/``expires_at``, acquise/renouvelée par
une mise à jour SQL conditionnelle atomique (« si personne ne le détient, ou
si le bail a expiré, ou si c'est déjà nous »). Ni l'un ni l'autre n'utilisait
``pg_advisory_lock`` (spécifique Postgres, indisponible en test SQLite) — ce
choix est repris ici.

Nouveauté par rapport aux deux implémentations d'origine : ``generation``,
un compteur de fencing qui avance à chaque nouvelle attribution du bail
(jamais sur un simple renouvellement par le même titulaire). Un titulaire
qui a perdu le bail sans avoir lui-même observé son expiration (ex. long GC
pause, thread suspendu puis réveillé) peut ainsi détecter qu'il n'est plus
à jour via ``Lease.is_current()`` avant d'agir sur la ressource protégée,
plutôt que de se fier uniquement à l'horloge locale.

Deux niveaux d'API, pour ne rien casser des deux appelants existants :

- les fonctions ``acquire_lease``/``release_lease`` sont sans état (chaque
  appel est indépendant) — c'est ce qu'utilisait déjà
  ``reconciler.acquire_lease`` (même signature, même sémantique booléenne),
  conservé tel quel comme fine enveloppe autour de ce module ;
- la classe ``Lease`` retient le dernier bail obtenu (nom, titulaire, TTL)
  pour offrir une API à l'instance (``try_acquire``/``release``, comme
  ``SchedulerLock``) et le fencing (``generation``, ``is_current``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .. import schema as v2_schema

DEFAULT_LEASE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class LeaseHandle:
    """Bail détenu par ``holder`` après un ``acquire`` réussi."""

    name: str
    holder: str
    generation: int
    expires_at: datetime


def _as_aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _row_expires_at(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    assert isinstance(value, datetime)
    return _as_aware_utc(value)


def _acquire(engine: Engine, *, name: str, holder: str, ttl_seconds: float, now: datetime) -> LeaseHandle | None:
    """Tente d'obtenir (ou de renouveler) le bail ``name`` pour ``holder``.

    Retourne le ``LeaseHandle`` détenu par ``holder`` après l'appel, ou
    ``None`` si le bail est tenu par un tiers non expiré. Une ligne absente
    est créée (génération 1) ; une ligne expirée ou déjà tenue par
    ``holder`` est renouvelée (génération inchangée pour ``holder``,
    incrémentée pour une reprise après expiration par un autre titulaire).
    """

    moment = _as_aware_utc(now)
    expires_at = moment + timedelta(seconds=ttl_seconds)
    table = v2_schema.leases
    with engine.begin() as connection:
        row = connection.execute(
            select(table.c.holder, table.c.generation, table.c.expires_at).where(table.c.name == name)
        ).mappings().first()
        if row is None:
            try:
                connection.execute(
                    table.insert(),
                    {"name": name, "holder": holder, "generation": 1, "expires_at": expires_at},
                )
            except IntegrityError:
                return None  # un autre réplica a inséré entre-temps
            return LeaseHandle(name=name, holder=holder, generation=1, expires_at=expires_at)

        row_expires_at = _row_expires_at(row["expires_at"])
        same_holder = row["holder"] == holder
        expired = row_expires_at <= moment
        if not same_holder and not expired:
            return None  # tenu par un tiers, bail non expiré

        # Nouvelle attribution (première prise par ce titulaire, ou reprise
        # après expiration) : la génération avance — un jeton de fencing émis
        # à l'ancien titulaire ne correspondra plus à la ligne après cette
        # mise à jour. Un simple renouvellement par le même titulaire ne
        # change pas la génération : il ne perd jamais le bail entre les deux
        # appels.
        next_generation = row["generation"] if same_holder else row["generation"] + 1
        result = connection.execute(
            update(table)
            .where(table.c.name == name)
            .where(table.c.holder == row["holder"])
            .values(holder=holder, generation=next_generation, expires_at=expires_at)
        )
        if result.rowcount != 1:
            return None  # course perdue entre le SELECT et l'UPDATE
        return LeaseHandle(name=name, holder=holder, generation=next_generation, expires_at=expires_at)


def acquire_lease(engine: Engine, *, name: str, holder: str, ttl_seconds: float, now: datetime) -> bool:
    """Enveloppe sans état, booléenne — API historique de
    ``reconciler.acquire_lease``, conservée à l'identique."""

    return _acquire(engine, name=name, holder=holder, ttl_seconds=ttl_seconds, now=now) is not None


def release_lease(engine: Engine, *, name: str, holder: str) -> None:
    """Libère le bail ``name`` par anticipation, si détenu par ``holder`` —
    best-effort, jamais requis pour la sûreté (le bail expire de lui-même)."""

    table = v2_schema.leases
    with engine.begin() as connection:
        connection.execute(table.delete().where(table.c.name == name).where(table.c.holder == holder))


def current_generation(engine: Engine, *, name: str, now: datetime | None = None) -> int | None:
    """Génération actuellement en base pour ``name``, ou ``None`` si le bail
    n'existe pas ou est expiré (personne ne le détient effectivement)."""

    moment = _as_aware_utc(now or datetime.now(timezone.utc))
    table = v2_schema.leases
    with engine.connect() as connection:
        row = connection.execute(
            select(table.c.generation, table.c.expires_at).where(table.c.name == name)
        ).mappings().first()
    if row is None:
        return None
    if _row_expires_at(row["expires_at"]) <= moment:
        return None
    return row["generation"]


class Lease:
    """Bail nommé, détenu par ``holder`` pendant ``ttl_seconds`` — API à
    l'instance (utilisée par la boucle de réconciliation et l'ordonnanceur
    d'observation), avec fencing.

    Retient la ``generation`` du dernier bail obtenu par cette instance, pour
    permettre ``is_current()`` : un contrôle explicite, à faire juste avant
    d'agir sur la ressource protégée, qui détecte qu'un autre titulaire a
    repris le bail même si celui-ci n'a pas observé l'expiration lui-même
    (ex. long GC pause, thread suspendu puis réveillé après coup)."""

    def __init__(self, engine: Engine, *, name: str, holder: str, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("le bail doit avoir une durée positive")
        self._engine = engine
        self._name = name
        self._holder = holder
        self._ttl_seconds = ttl_seconds
        self._generation: int | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def holder(self) -> str:
        return self._holder

    @property
    def generation(self) -> int | None:
        """Génération du bail détenu par cette instance après le dernier
        ``acquire``/``try_acquire`` réussi ; ``None`` si jamais acquis ou
        après ``release``."""

        return self._generation

    def acquire(self, *, now: datetime | None = None) -> bool:
        """Acquiert ou renouvelle le bail ; ``True`` seulement si ce
        titulaire le détient après l'appel."""

        moment = now or datetime.now(timezone.utc)
        handle = _acquire(self._engine, name=self._name, holder=self._holder, ttl_seconds=self._ttl_seconds, now=moment)
        self._generation = handle.generation if handle is not None else None
        return handle is not None

    # Alias historique — même nom que ``SchedulerLock.try_acquire``.
    try_acquire = acquire

    def release(self) -> None:
        """Libère le bail par anticipation — best-effort, jamais requis pour
        la sûreté (le bail expire de lui-même)."""

        release_lease(self._engine, name=self._name, holder=self._holder)
        self._generation = None

    def is_current(self, *, now: datetime | None = None) -> bool:
        """Contrôle de fencing : ce titulaire détient-il toujours le bail ?

        Faux si jamais acquis, si libéré, si le bail a expiré, ou si un
        autre titulaire l'a repris entre-temps (génération en base
        différente de celle retenue par cette instance) — à appeler juste
        avant toute action sur la ressource protégée par ce bail, pas
        seulement en confiance dans le TTL écoulé."""

        if self._generation is None:
            return False
        moment = now or datetime.now(timezone.utc)
        return current_generation(self._engine, name=self._name, now=moment) == self._generation
