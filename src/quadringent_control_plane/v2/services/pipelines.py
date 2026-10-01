"""Service Pipeline v2 : applique une transition ``declared_state`` (tâches 5+6).

Ne câble aucun Kubernetes ici : l'exécution réelle passe par un exécuteur
injecté (``PipelineExecutorProtocol``), agnostique du fournisseur — un faux
exécuteur dans les tests, à l'image de ``FleetActionExecutor``
(``actions.py`` v1). La création de pipelines (table/source/destination
existantes requises) est hors périmètre de ce chantier (découverte de
tables, tâche 4) : ce service part d'un pipeline déjà persisté.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from .observation import NullObservationProvider, PipelineObservation, PipelineObservationProviderProtocol
from .state_machine import DECLARED_STATES, ForbiddenTransitionError, transition

ACTIONS = frozenset({"pause", "resume", "remove", "restart_initial_copy", "replay"})

# Sous-ensemble d'ACTIONS qui exige une confirmation (tâche 7, contrat §2.4) :
# retrait de table et relance de copie complète sont toujours confirmés ;
# ``replay`` l'est aussi par défaut ici, faute de seuil de coût configurable
# dans ce chantier (§2.4 : « oui si coût > seuil » — décision conservatrice
# documentée : toujours confirmer en l'absence de calcul de coût).
CONFIRMATION_REQUIRED_ACTIONS = frozenset({"remove", "restart_initial_copy", "replay"})


class PipelineNotFoundError(LookupError):
    """Aucun pipeline pour cet identifiant — 404 ``not_found``."""


class TableNotFoundError(LookupError):
    """Aucune table pour cet identifiant — 404 ``not_found``."""


class DestinationNotFoundError(LookupError):
    """L'identifiant de destination fourni n'existe pas — 404 ``not_found``."""


class AmbiguousDestinationError(ValueError):
    """``destination_id`` omis et plusieurs destinations existent pour l'organisation.

    Décision conservatrice documentée (contrat §2.3 ne tranche pas ce cas) :
    jamais de choix implicite silencieux — l'appelant doit préciser
    ``destination_id`` dès qu'il y a plus d'une destination.
    """


class PipelineAlreadyExistsError(ValueError):
    """Une table ne peut avoir qu'un seul pipeline (``pipelines.table_id`` unique)."""


class PipelineExecutorUnavailableError(RuntimeError):
    """Aucun exécuteur injecté — 503 ``executor_unavailable``."""


class UnknownActionError(ValueError):
    """Le verbe d'action n'est pas dans le périmètre de ce chantier."""


class InvalidListFilterError(ValueError):
    """Filtre de liste hors périmètre (état inconnu, curseur illisible) — 400."""


class PipelineExecutorProtocol(Protocol):
    """Contrat minimal d'un exécuteur de transition — jamais Kubernetes ici."""

    def execute(self, *, pipeline_id: str, event: str) -> None: ...


@dataclass(frozen=True)
class PipelineRecord:
    id: str
    declared_state: str
    # ``declared_state`` mémorisé au moment de la transition ``pause``
    # (``copying`` ou ``live``) — voir ``0014_pipeline_state_before_pause`` et
    # ``_event_for_action``. ``None`` hors pause, ou pour un pipeline pausé
    # avant ce correctif.
    state_before_pause: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "declared_state": self.declared_state}


@dataclass(frozen=True)
class PipelineListRecord:
    """Ligne de ``GET /v2/pipelines`` : état déclaré (base v2) + figures en
    direct (fournisseur d'observation injecté) — contrat §2.4.

    Les deux couches restent distinctes dans le JSON (jamais fusionnées à
    l'aveugle) : ``declared_state`` vient de la base transactionnelle,
    ``observed_state``/``lag_seconds``/… viennent de la projection
    d'observation et peuvent être absents (``None`` + raison) sans jamais
    invalider ``declared_state``.
    """

    id: str
    table_id: str
    source_id: str
    destination_id: str
    declared_state: str
    observation: PipelineObservation

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "table_id": self.table_id,
            "source_id": self.source_id,
            "destination_id": self.destination_id,
            "declared_state": self.declared_state,
        }
        payload.update(self.observation.to_dict())
        return payload


class PipelinesService:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def list(
        self,
        *,
        state: str | None = None,
        source_id: str | None = None,
        destination_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        observation_provider: PipelineObservationProviderProtocol | None = None,
    ) -> tuple[tuple[PipelineListRecord, ...], str | None]:
        """Liste paginée (contrat §2, conventions transverses) : ``?limit=&cursor=``
        -> ``{"items":[...],"next_cursor":str|null}``. Filtres fermés — un
        ``state`` hors de ``DECLARED_STATES`` échoue fermé (400), jamais un
        filtre silencieusement ignoré.
        """

        if state is not None and state not in DECLARED_STATES:
            raise InvalidListFilterError(f"état de filtre inconnu : {state!r}")
        if not 1 <= limit <= 500:
            raise InvalidListFilterError("limit doit être un entier entre 1 et 500")
        provider = observation_provider or NullObservationProvider()

        pipelines = v2_schema.pipelines
        tables = v2_schema.tables
        statement = (
            select(
                pipelines.c.id,
                pipelines.c.table_id,
                pipelines.c.destination_id,
                pipelines.c.declared_state,
                tables.c.source_id,
            )
            .select_from(pipelines.join(tables, pipelines.c.table_id == tables.c.id))
            .order_by(pipelines.c.id)
            .limit(limit + 1)
        )
        if state is not None:
            statement = statement.where(pipelines.c.declared_state == state)
        if source_id is not None:
            statement = statement.where(tables.c.source_id == source_id)
        if destination_id is not None:
            statement = statement.where(pipelines.c.destination_id == destination_id)
        if cursor is not None:
            statement = statement.where(pipelines.c.id > cursor)

        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = rows[-1]["id"]

        records = tuple(
            PipelineListRecord(
                id=row["id"],
                table_id=row["table_id"],
                source_id=row["source_id"],
                destination_id=row["destination_id"],
                declared_state=row["declared_state"],
                observation=provider.observe(row["id"]),
            )
            for row in rows
        )
        return records, next_cursor

    def get(self, pipeline_id: str) -> PipelineRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.pipelines).where(v2_schema.pipelines.c.id == pipeline_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise PipelineNotFoundError(pipeline_id)
        return PipelineRecord(
            id=row["id"], declared_state=row["declared_state"], state_before_pause=row["state_before_pause"]
        )

    # -- Démarrage (``POST /v2/tables/{id}/pipeline``, contrat §2.3) -----------

    def _existing_pipeline_for_table(self, table_id: str) -> PipelineRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.pipelines).where(v2_schema.pipelines.c.table_id == table_id)
                )
                .mappings()
                .first()
            )
        return None if row is None else PipelineRecord(id=row["id"], declared_state=row["declared_state"])

    def _resolve_destination_id(self, destination_id: str | None) -> str:
        with self._engine.connect() as connection:
            if destination_id is not None:
                row = connection.execute(
                    select(v2_schema.destinations.c.id).where(v2_schema.destinations.c.id == destination_id)
                ).first()
                if row is None:
                    raise DestinationNotFoundError(destination_id)
                return destination_id
            rows = connection.execute(select(v2_schema.destinations.c.id)).all()
        if not rows:
            raise DestinationNotFoundError("aucune destination configurée")
        if len(rows) > 1:
            raise AmbiguousDestinationError(
                "plusieurs destinations existent : préciser destination_id"
            )
        return rows[0][0]

    def _require_table(self, table_id: str) -> None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(v2_schema.tables.c.id).where(v2_schema.tables.c.id == table_id)
            ).first()
        if row is None:
            raise TableNotFoundError(table_id)

    def plan_start(self, table_id: str, *, destination_id: str | None = None) -> dict[str, object]:
        """Calcule le démarrage sans effet de bord — support de ``dry_run``."""

        self._require_table(table_id)
        existing = self._existing_pipeline_for_table(table_id)
        if existing is not None:
            transition(existing.declared_state, "start")  # lève si déjà démarré
        resolved_destination_id = self._resolve_destination_id(destination_id)
        return {
            "would_create": existing is None,
            "would_transition": {"from": "not_started", "to": "copying"},
            "destination_id": resolved_destination_id,
        }

    def start_table_pipeline(
        self,
        table_id: str,
        *,
        destination_id: str | None,
        executor: PipelineExecutorProtocol | None,
        now: datetime | None = None,
    ) -> tuple[PipelineRecord | None, PipelineRecord]:
        """Crée le pipeline si nécessaire, puis déclenche l'évènement ``start``.

        ``before`` est ``None`` quand le pipeline n'existait pas encore
        (comme les créations de source/destination), sinon l'état courant —
        utile pour rejouer un ``start`` sur un pipeline déjà ``not_started``
        (créé par une migration, jamais démarré).
        """

        self._require_table(table_id)
        if executor is None:
            raise PipelineExecutorUnavailableError("aucun exécuteur de pipeline injecté")
        existing = self._existing_pipeline_for_table(table_id)
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if existing is None:
            resolved_destination_id = self._resolve_destination_id(destination_id)
            pipeline_id = uuid.uuid4().hex
            with self._engine.begin() as connection:
                connection.execute(
                    insert(v2_schema.pipelines),
                    {
                        "id": pipeline_id,
                        "table_id": table_id,
                        "destination_id": resolved_destination_id,
                        "declared_state": "not_started",
                        "created_at": moment,
                        "updated_at": moment,
                    },
                )
            before = None
        else:
            pipeline_id = existing.id
            before = existing
        next_state = transition(
            "not_started" if before is None else before.declared_state, "start"
        )
        executor.execute(pipeline_id=pipeline_id, event="start")
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == pipeline_id)
                .values(declared_state=next_state, updated_at=moment)
            )
        return before, self.get(pipeline_id)

    def plan_action(self, pipeline_id: str, action: str) -> dict[str, object]:
        """Calcule la transition sans effet de bord — support de ``dry_run``."""

        record = self.get(pipeline_id)
        if action == "replay":
            return {"would_transition": {"from": record.declared_state, "to": record.declared_state}}
        event = _event_for_action(action, record.declared_state, record.state_before_pause)
        next_state = transition(record.declared_state, event)
        return {"would_transition": {"from": record.declared_state, "to": next_state}}

    def apply_action(
        self,
        pipeline_id: str,
        action: str,
        *,
        executor: PipelineExecutorProtocol | None,
        now: datetime | None = None,
    ) -> tuple[PipelineRecord, PipelineRecord]:
        """Applique l'action ; retourne ``(before, after)``.

        Valide d'abord la transition (échoue fermé si interdite) *avant*
        d'invoquer l'exécuteur — un exécuteur ne voit jamais une transition
        que la machine à états refuserait. ``replay`` ne change jamais
        ``declared_state`` (c'est une opération sur les données déjà
        répliquées, pas une transition de cycle de vie) : seul l'exécuteur
        est invoqué.
        """

        before = self.get(pipeline_id)
        if executor is None:
            raise PipelineExecutorUnavailableError("aucun exécuteur de pipeline injecté")
        if action == "replay":
            executor.execute(pipeline_id=pipeline_id, event="replay")
            return before, self.get(pipeline_id)
        event = _event_for_action(action, before.declared_state, before.state_before_pause)
        next_state = transition(before.declared_state, event)
        executor.execute(pipeline_id=pipeline_id, event=event)
        updated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        # ``pause`` mémorise l'état déclaré d'avant pause (``copying``/``live``)
        # pour que ``resume`` sache où reprendre — voir
        # ``0014_pipeline_state_before_pause``/``_event_for_action``. Tout
        # événement de reprise (``resume``/``resume_copying``/``resume_live``)
        # efface la mémoire : elle a été consommée. ``restart_initial_copy``
        # remet le pipeline en ``copying`` (nouvelle copie explicite) : un
        # ``pause`` ultérieur mémorisera de nouveau ``copying``, jamais
        # l'ancienne valeur.
        state_before_pause_update: dict[str, object] = {}
        if event == "pause":
            state_before_pause_update["state_before_pause"] = before.declared_state
        elif event in ("resume", "resume_copying", "resume_live"):
            state_before_pause_update["state_before_pause"] = None
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == pipeline_id)
                .values(declared_state=next_state, updated_at=updated_at, **state_before_pause_update)
            )
        return before, self.get(pipeline_id)

    # -- Actions de portée (source/destination/tout — design §3) ---------------

    def list_pipeline_ids_for_source(self, source_id: str) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(v2_schema.pipelines.c.id)
                .select_from(
                    v2_schema.pipelines.join(
                        v2_schema.tables, v2_schema.tables.c.id == v2_schema.pipelines.c.table_id
                    )
                )
                .where(v2_schema.tables.c.source_id == source_id)
            ).all()
        return tuple(row[0] for row in rows)

    def list_pipeline_ids_for_destination(self, destination_id: str) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(v2_schema.pipelines.c.id).where(
                    v2_schema.pipelines.c.destination_id == destination_id
                )
            ).all()
        return tuple(row[0] for row in rows)

    def list_all_pipeline_ids(self) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(select(v2_schema.pipelines.c.id)).all()
        return tuple(row[0] for row in rows)

    def _is_scope_paused(self, pipeline_id: str) -> bool:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(v2_schema.pipelines.c.paused_by_scope_action).where(
                    v2_schema.pipelines.c.id == pipeline_id
                )
            ).first()
        return bool(row and row[0])

    def _set_scope_paused(self, pipeline_id: str, value: bool, *, now: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == pipeline_id)
                .values(paused_by_scope_action=value, updated_at=now)
            )

    def _would_skip_scope_action(self, pipeline_id: str, action: str) -> str | None:
        """Raison de ``skip`` pour une action de portée, ou ``None`` si applicable.

        ``resume`` de portée ne relance jamais une table que l'utilisateur
        avait mise en pause individuellement (``paused_by_scope_action`` à
        faux) — distinct d'une transition interdite par la machine à états :
        c'est une décision produit, pas une contrainte du modèle.
        """

        record = self.get(pipeline_id)
        if action == "resume" and record.declared_state == "paused" and not self._is_scope_paused(pipeline_id):
            return "pausé individuellement — non relancé par une reprise de portée"
        try:
            event = _event_for_action(action, record.declared_state, record.state_before_pause)
            transition(record.declared_state, event)
        except (UnknownActionError, ForbiddenTransitionError) as error:
            return str(error)
        return None

    def plan_scope_action(self, pipeline_ids: tuple[str, ...], action: str) -> dict[str, object]:
        """Dry-run d'une action de portée (pause/resume) — jamais d'effet de bord.

        Une table dont la transition n'est pas permise dans son état
        courant (ex. ``pause`` sur une table déjà ``paused``/``stopped``),
        ou dont la pause était individuelle (jamais relancée par un
        ``resume`` de portée), est listée comme ``skipped`` — jamais une
        erreur qui bloquerait toute la portée : la pause/reprise de masse
        reste best-effort (design §3 : « pause et reprise ... de toutes,
        d'une connexion, d'une destination »).
        """

        would_transition: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for pipeline_id in pipeline_ids:
            reason = self._would_skip_scope_action(pipeline_id, action)
            if reason is not None:
                skipped.append({"pipeline_id": pipeline_id, "reason": reason})
                continue
            record = self.get(pipeline_id)
            event = _event_for_action(action, record.declared_state, record.state_before_pause)
            next_state = transition(record.declared_state, event)
            would_transition.append(
                {"pipeline_id": pipeline_id, "from": record.declared_state, "to": next_state}
            )
        return {"would_transition": would_transition, "skipped": skipped}

    def apply_scope_action(
        self,
        pipeline_ids: tuple[str, ...],
        action: str,
        *,
        executor: PipelineExecutorProtocol | None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Applique ``pause``/``resume`` de portée, best-effort, marqueur inclus.

        Une pause de portée marque chaque pipeline effectivement pausé
        ``paused_by_scope_action=true`` ; une reprise de portée ne relance
        que les pipelines qui portent ce marqueur (jamais une table pausée
        individuellement par l'utilisateur — cf. ``_would_skip_scope_action``)
        et l'efface au passage. ``executor`` absent échoue fermé pour toute
        la portée (503, jamais un pilotage partiel silencieux).
        """

        if action not in ("pause", "resume"):
            raise UnknownActionError(f"action de portée inconnue : {action!r}")
        if not pipeline_ids:
            return {"applied": [], "skipped": []}  # rien à piloter : jamais besoin d'exécuteur
        if executor is None:
            raise PipelineExecutorUnavailableError("aucun exécuteur de pipeline injecté")
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        applied: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for pipeline_id in pipeline_ids:
            reason = self._would_skip_scope_action(pipeline_id, action)
            if reason is not None:
                skipped.append({"pipeline_id": pipeline_id, "reason": reason})
                continue
            before, after = self.apply_action(pipeline_id, action, executor=executor, now=moment)
            self._set_scope_paused(pipeline_id, action == "pause", now=moment)
            applied.append({"pipeline_id": pipeline_id, "before": before.to_dict(), "after": after.to_dict()})
        return {"applied": applied, "skipped": skipped}


def _event_for_action(action: str, current_state: str, state_before_pause: str | None = None) -> str:
    """Traduit un verbe d'action HTTP en événement de la machine à états.

    Correctif du 24 septembre 2026 (premier pipeline réel sur GKE) : un
    ``resume`` sur un pipeline ``paused`` reprend désormais au ``declared_
    state`` mémorisé juste avant la pause (``state_before_pause`` —
    ``0014_pipeline_state_before_pause``) : ``live`` -> ``resume_live``
    (reprise de la capture au checkpoint, jamais de nouvelle copie),
    ``copying`` (ou inconnu, pipeline pausé avant ce correctif) ->
    ``resume_copying``, comportement conservateur inchangé pour ces lignes
    historiques. Une nouvelle copie initiale complète reste une action
    explicite et distincte (``restart_initial_copy``), jamais un effet de
    bord de la reprise.
    """

    if action == "pause":
        return "pause"
    if action == "remove":
        return "remove"
    if action == "restart_initial_copy":
        return "restart_initial_copy"
    if action == "resume":
        if current_state == "attention":
            return "resume"
        return "resume_live" if state_before_pause == "live" else "resume_copying"
    raise UnknownActionError(f"action inconnue : {action!r}")
