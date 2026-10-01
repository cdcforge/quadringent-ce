"""Rafraîchissement d'observation -> événements SSE (contrat §3.1).

Ce service ne tourne pas tout seul (pas d'ordonnanceur ici, hors périmètre)
: un futur processus l'appelle périodiquement (``refresh_all``) ou à la
demande (``refresh``) — même principe que les autres briques injectables de
ce chantier (``pipeline_executor``, ``log_source``, ``costs_provider``).

Il compare l'``observed_state`` renvoyé par le fournisseur d'observation au
dernier état observé connu, persisté dans
``pipelines.last_observed_state`` (migration ``0005_pipeline_last_observed``)
— jamais l'état *déclaré*, qui reste piloté uniquement par
``PipelinesService.apply_action``. Sur un vrai changement (y compris
première observation, ``None`` -> un état), il publie
``pipeline.state_changed`` via ``EventsService`` (réutilisé tel quel, tâche
12) ; une entrée/sortie de l'état ``incident`` publie en plus
``alert.fired``/``alert.resolved`` — modélisation volontairement minimale
(un « pipeline en incident » est la seule alerte connue ici, faute d'un
modèle d'alertes v2 dédié dans ce chantier) : ``alert_id`` vaut
``f"pipeline:{pipeline_id}"`` par convention, à remplacer par un vrai
identifiant d'alerte quand ce modèle existera.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from .events import EventsService
from .observation import NullObservationProvider, PipelineObservation, PipelineObservationProviderProtocol

INCIDENT_STATE = "incident"


class PipelineNotFoundError(LookupError):
    """Aucun pipeline pour cet identifiant — mêmes sémantiques que
    ``services.pipelines.PipelineNotFoundError`` (pas réutilisée directement
    pour ne pas coupler ce service au module d'actions)."""


class ObservationRefreshService:
    def __init__(
        self,
        engine: Engine,
        *,
        org_id: str,
        observation_provider: PipelineObservationProviderProtocol | None = None,
        events_service: EventsService | None = None,
    ) -> None:
        self._engine = engine
        self._provider = observation_provider or NullObservationProvider()
        self._events = events_service or EventsService(engine, org_id=org_id)

    def refresh(self, pipeline_id: str, *, now: datetime | None = None) -> PipelineObservation:
        observation = self._provider.observe(pipeline_id)
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            row = connection.execute(
                select(v2_schema.pipelines.c.last_observed_state).where(
                    v2_schema.pipelines.c.id == pipeline_id
                )
            ).first()
            if row is None:
                raise PipelineNotFoundError(pipeline_id)
            previous_state = row[0]
            if observation.observed_state == previous_state:
                return observation
            connection.execute(
                update(v2_schema.pipelines)
                .where(v2_schema.pipelines.c.id == pipeline_id)
                .values(last_observed_state=observation.observed_state, last_observed_at=moment)
            )

        self._events.publish(
            "pipeline.state_changed",
            {
                "pipeline_id": pipeline_id,
                "from": previous_state,
                "to": observation.observed_state,
            },
        )
        if observation.observed_state == INCIDENT_STATE and previous_state != INCIDENT_STATE:
            self._events.publish(
                "alert.fired",
                {"alert_id": f"pipeline:{pipeline_id}", "pipeline_id": pipeline_id, "severity": "critical"},
            )
        elif previous_state == INCIDENT_STATE and observation.observed_state != INCIDENT_STATE:
            self._events.publish(
                "alert.resolved",
                {"alert_id": f"pipeline:{pipeline_id}", "pipeline_id": pipeline_id},
            )
        return observation

    def refresh_all(self, *, now: datetime | None = None) -> tuple[PipelineObservation, ...]:
        with self._engine.connect() as connection:
            ids = [
                row[0]
                for row in connection.execute(select(v2_schema.pipelines.c.id).order_by(v2_schema.pipelines.c.id))
            ]
        return tuple(self.refresh(pipeline_id, now=now) for pipeline_id in ids)
