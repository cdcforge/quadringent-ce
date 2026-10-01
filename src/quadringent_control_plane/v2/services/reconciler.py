"""Boucle de réconciliation en arrière-plan (chantier 4, tâche 2).

Trois responsabilités, jamais mélangées avec la persistance de
``declared_state`` sur écriture HTTP (``PipelinesService.apply_action``,
synchrone) :

1. **Copie terminée -> live** : pour chaque pipeline ``copying`` avec un
   ``active_run_id``, lit la preuve durable
   (``executor.evidence.InitialCopyEvidence``, jamais écrite par le control
   plane) via ``PipelineExecutorProtocol``-like ``copying_evidence`` ; si
   elle existe, applique la transition ``bootstrap_completed`` et émet
   ``pipeline.state_changed``.
2. **Job en échec -> attention** : si le Job de copie associé au même
   ``active_run_id`` est terminé en échec (``copy_job_outcome``), bascule le
   pipeline en ``attention`` avec une action suivante explicite
   (``attention_reason``), jamais une correction automatique.
3. **Réconciliation de dérive** (chantier 2026-09-24, tâche 3) : jusqu'ici, les
   Deployments lecteur/chargeur déjà en place n'étaient réappliqués qu'aux
   transitions de ``declared_state`` pilotées par ``KubernetesPipelineExecutor.
   execute`` (pause, resume, start...) — jamais après une simple mise à jour
   du produit (nouvelle image, manifests changés) pendant qu'un pipeline
   reste ``live``/``copying``/``paused`` sans transition. À chaque tour, pour
   toute source et toute destination portant au moins un pipeline actif
   (``live``, ``copying`` ou ``paused`` — ``ACTIVE_PIPELINE_STATES``), le tour
   réapplique l'état désiré via ``ReconcilerExecutorProtocol.reconcile_source``/
   ``reconcile_destination`` : idempotent (aucune écriture Kubernetes si
   l'empreinte de ``spec`` est identique, cf. ``executor/reconcile.py``), et
   une erreur sur une source/destination est journalisée puis ignorée — elle
   ne bloque jamais la réconciliation des autres (best-effort, comme la
   pause/reprise de portée). Les pipelines ``paused`` sont inclus : leurs
   tables restent de toute façon exclues du manifeste désiré (``_desired_
   reader_manifests``/``_desired_loader_manifest`` ne retiennent que
   ``copying``/``live``), donc les inclure ne fait jamais réapparaître une
   charge déjà pausée — cela permet seulement de retirer un Deployment
   devenu orphelin (plus aucune table active sur son journal/destination)
   même sans transition explicite entre-temps.

``ReconciliationLoop`` ne câble jamais Kubernetes elle-même : elle ne
connaît que ``ReconcilerExecutorProtocol`` (le sous-ensemble de
``KubernetesPipelineExecutor`` dont elle a besoin), injecté — un faux
exécuteur suffit en tests, jamais de cluster réel.

## Sûreté à plusieurs réplicas

Un bail (lease) applicatif portable (table ``leases``, ``acquire_lease`` —
voir ``services/lease.py``, module partagé avec l'ordonnanceur
d'observation depuis la migration 0010_unify_leases) remplace un verrou
spécifique Postgres (``pg_advisory_lock``, incompatible avec les tests
SQLite de ce dépôt) : un seul réplica détient le bail à la fois, revalidé
à chaque tour avec un TTL court — un tour manqué (réplica qui redémarre
pendant qu'il détient le bail) est sans conséquence, le bail expire et un
autre réplica reprend au tour suivant. Une transition manquée n'est jamais
perdue : elle sera retentée au prochain tour tant que la preuve/le Job
existe toujours.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Protocol
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from .events import EventsService
from .lease import acquire_lease as _acquire_lease
from .state_machine import ForbiddenTransitionError, transition

DEFAULT_LEASE_NAME = "reconciliation-loop"
DEFAULT_LEASE_TTL_SECONDS = 30

# États dont un pipeline est considéré « actif » pour la réconciliation de
# dérive : sa source/destination doit voir son état désiré réappliqué à
# chaque tour. ``paused`` est inclus — voir la docstring du module — sans
# jamais rouvrir une charge pausée (ses tables restent filtrées côté
# manifeste désiré).
ACTIVE_PIPELINE_STATES = ("live", "copying", "paused")

_logger = logging.getLogger(__name__)
ATTENTION_REASON_JOB_FAILED = (
    "la copie initiale a échoué (Job en erreur) — action suivante : relancer "
    "restart_initial_copy"
)


class EvidenceLike(Protocol):
    boundary: object
    rows_copied: int


class ReconcilerExecutorProtocol(Protocol):
    """Sous-ensemble de ``KubernetesPipelineExecutor`` requis par la boucle."""

    def copying_evidence(self, pipeline_id: str, run_id: str) -> EvidenceLike | None: ...

    def copy_job_outcome(self, pipeline_id: str, run_id: str) -> str: ...

    def reconcile_source(self, source_id: str) -> None: ...

    def reconcile_destination(self, destination_id: str) -> None: ...


def acquire_lease(engine: Engine, *, name: str, holder: str, ttl_seconds: int, now: datetime) -> bool:
    """Tente d'obtenir (ou de renouveler) le bail ``name`` pour ``holder``.

    Vrai si ``holder`` détient le bail après l'appel. Enveloppe de
    compatibilité — l'implémentation vit désormais dans
    ``services/lease.py`` (table ``leases`` unique, partagée avec
    l'ordonnanceur d'observation) ; signature et sémantique booléenne
    inchangées."""

    return _acquire_lease(engine, name=name, holder=holder, ttl_seconds=ttl_seconds, now=now)


@dataclass(frozen=True)
class ReconcileTick:
    """Résumé d'un tour — utile pour les tests et l'observabilité."""

    lease_acquired: bool
    promoted_to_live: tuple[str, ...] = ()
    marked_attention: tuple[str, ...] = ()
    # Réconciliation de dérive (tâche 3) : sources/destinations dont l'état
    # désiré a été réappliqué (avec ou sans écriture Kubernetes — idempotent),
    # et erreurs par ressource, jamais bloquantes pour les autres.
    reconciled_sources: tuple[str, ...] = ()
    reconciled_destinations: tuple[str, ...] = ()
    reconcile_errors: tuple[dict[str, str], ...] = ()


class ReconciliationLoop:
    def __init__(
        self,
        engine: Engine,
        *,
        executor: ReconcilerExecutorProtocol,
        org_id: str,
        holder_id: str | None = None,
        lease_name: str = DEFAULT_LEASE_NAME,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self._engine = engine
        self._executor = executor
        self._events = EventsService(engine, org_id=org_id)
        self._holder_id = holder_id or uuid.uuid4().hex
        self._lease_name = lease_name
        self._lease_ttl_seconds = lease_ttl_seconds
        self._now = now

    def tick(self) -> ReconcileTick:
        """Un tour de réconciliation. Sûr à appeler depuis plusieurs réplicas."""

        moment = self._now()
        if not acquire_lease(
            self._engine,
            name=self._lease_name,
            holder=self._holder_id,
            ttl_seconds=self._lease_ttl_seconds,
            now=moment,
        ):
            return ReconcileTick(lease_acquired=False)

        promoted: list[str] = []
        attention: list[str] = []
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        v2_schema.pipelines.c.id,
                        v2_schema.pipelines.c.declared_state,
                        v2_schema.pipelines.c.active_run_id,
                    ).where(
                        v2_schema.pipelines.c.declared_state == "copying",
                        v2_schema.pipelines.c.active_run_id.is_not(None),
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            pipeline_id, run_id = row["id"], row["active_run_id"]
            evidence = self._executor.copying_evidence(pipeline_id, run_id)
            if evidence is not None:
                self._promote_to_live(pipeline_id, moment)
                promoted.append(pipeline_id)
                continue
            outcome = self._executor.copy_job_outcome(pipeline_id, run_id)
            if outcome == "failed":
                self._mark_attention(pipeline_id, ATTENTION_REASON_JOB_FAILED, moment)
                attention.append(pipeline_id)

        reconciled_sources, reconciled_destinations, reconcile_errors = self._reconcile_drift()

        return ReconcileTick(
            lease_acquired=True,
            promoted_to_live=tuple(promoted),
            marked_attention=tuple(attention),
            reconciled_sources=reconciled_sources,
            reconciled_destinations=reconciled_destinations,
            reconcile_errors=reconcile_errors,
        )

    def _reconcile_drift(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[dict[str, str], ...]]:
        """Réapplique l'état désiré pour chaque source/destination active.

        Sources et destinations sont collectées séparément (une source peut
        porter des tables vers plusieurs destinations, et réciproquement) ;
        chacune est réconciliée indépendamment, une erreur n'empêchant jamais
        les suivantes (best-effort, cf. la docstring du module).
        """

        with self._engine.connect() as connection:
            source_ids = [
                row[0]
                for row in connection.execute(
                    select(v2_schema.tables.c.source_id)
                    .select_from(
                        v2_schema.pipelines.join(
                            v2_schema.tables, v2_schema.tables.c.id == v2_schema.pipelines.c.table_id
                        )
                    )
                    .where(v2_schema.pipelines.c.declared_state.in_(ACTIVE_PIPELINE_STATES))
                    .distinct()
                ).all()
            ]
            destination_ids = [
                row[0]
                for row in connection.execute(
                    select(v2_schema.pipelines.c.destination_id)
                    .where(v2_schema.pipelines.c.declared_state.in_(ACTIVE_PIPELINE_STATES))
                    .distinct()
                ).all()
            ]

        reconciled_sources: list[str] = []
        reconciled_destinations: list[str] = []
        errors: list[dict[str, str]] = []

        for source_id in source_ids:
            try:
                self._executor.reconcile_source(source_id)
            except Exception as error:  # noqa: BLE001 — une source en échec ne bloque pas les autres
                _logger.exception("réconciliation de dérive en échec pour la source %s", source_id)
                errors.append({"source_id": source_id, "error": str(error)})
            else:
                reconciled_sources.append(source_id)

        for destination_id in destination_ids:
            try:
                self._executor.reconcile_destination(destination_id)
            except Exception as error:  # noqa: BLE001 — idem, par destination
                _logger.exception(
                    "réconciliation de dérive en échec pour la destination %s", destination_id
                )
                errors.append({"destination_id": destination_id, "error": str(error)})
            else:
                reconciled_destinations.append(destination_id)

        return tuple(reconciled_sources), tuple(reconciled_destinations), tuple(errors)

    def _promote_to_live(self, pipeline_id: str, moment: datetime) -> None:
        try:
            next_state = transition("copying", "bootstrap_completed")
        except ForbiddenTransitionError:
            return  # état changé entre-temps (ex. déjà pausé) — rien à faire
        with self._engine.begin() as connection:
            result = connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == pipeline_id, v2_schema.pipelines.c.declared_state == "copying")
                .values(declared_state=next_state, updated_at=moment)
            )
        if result.rowcount == 0:
            return  # concurrence : un autre acteur a déjà transitionné
        self._events.publish(
            "pipeline.state_changed",
            {"pipeline_id": pipeline_id, "from": "copying", "to": next_state},
        )

    def _mark_attention(self, pipeline_id: str, reason: str, moment: datetime) -> None:
        try:
            next_state = transition("copying", "attention")
        except ForbiddenTransitionError:
            return
        with self._engine.begin() as connection:
            result = connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == pipeline_id, v2_schema.pipelines.c.declared_state == "copying")
                .values(declared_state=next_state, attention_reason=reason, updated_at=moment)
            )
        if result.rowcount == 0:
            return
        self._events.publish(
            "pipeline.state_changed",
            {"pipeline_id": pipeline_id, "from": "copying", "to": next_state, "reason": reason},
        )


async def run_forever(
    loop: ReconciliationLoop, *, interval_seconds: float, stop_event: asyncio.Event
) -> None:
    """Boucle asyncio de production — un tour par intervalle, jusqu'à l'arrêt.

    ``tick()`` est synchrone (SQLAlchemy Core, comme le reste du control
    plane) ; ``asyncio.to_thread`` évite de bloquer la boucle d'évènements
    pendant les appels base de données/Kubernetes d'un tour. Une exception
    d'un tour est journalisée et n'arrête jamais la boucle — un tour raté
    est retenté au suivant (la boucle est idempotente, cf. le bail).
    """

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(loop.tick)
        except Exception:  # noqa: BLE001 — jamais interrompre la boucle de fond
            _logger.exception("tour de réconciliation en échec, nouvelle tentative au prochain intervalle")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
