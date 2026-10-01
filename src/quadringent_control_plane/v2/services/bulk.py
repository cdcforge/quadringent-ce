"""Service ``pause_all``/``resume_all`` (tâche 18 MCP/CLI, complétée chantier 4).

Portée organisation entière : met en pause/reprend chaque source et chaque
destination déclarées (``sources.paused_at``/``destinations.paused_at``,
migration 0006), marque ``organizations.paused_at`` comme repère global
pour ``quadringent://state/overview``, **et** pause/reprend réellement
chaque pipeline `copying`/`live` via l'exécuteur injecté — une pause qui ne
posait qu'un marqueur d'intention sans arrêter les flux ne correspond pas
au design (§3 : « pause et reprise ... de toutes »). Une table pausée
individuellement par l'utilisateur n'est jamais relancée par ``resume_all``
(``PipelinesService.apply_scope_action`` — même discipline que
``routes/sources.py``/``routes/destinations.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from .destinations import DestinationsService
from .pipelines import PipelineExecutorProtocol, PipelinesService
from .sources import SourcesService


@dataclass(frozen=True)
class BulkPauseResult:
    paused_sources: tuple[str, ...]
    paused_destinations: tuple[str, ...]
    pipelines: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "paused_sources": list(self.paused_sources),
            "paused_destinations": list(self.paused_destinations),
            "pipelines": self.pipelines,
        }


class BulkActionsService:
    def __init__(
        self,
        engine: Engine,
        secret_box,
        *,
        org_id: str,
        executor: PipelineExecutorProtocol | None = None,
    ) -> None:
        self._engine = engine
        self._org_id = org_id
        self._sources = SourcesService(engine, secret_box, org_id=org_id)
        self._destinations = DestinationsService(engine, secret_box, org_id=org_id)
        self._pipelines = PipelinesService(engine)
        self._executor = executor

    def pause_all(self, *, now: datetime | None = None) -> BulkPauseResult:
        paused_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        source_ids = tuple(record.id for record in self._sources.list())
        destination_ids = tuple(record.id for record in self._destinations.list())
        for source_id in source_ids:
            self._sources.pause(source_id, now=paused_at)
        for destination_id in destination_ids:
            self._destinations.pause(destination_id, now=paused_at)
        pipeline_ids = self._pipelines.list_all_pipeline_ids()
        pipeline_result = self._pipelines.apply_scope_action(
            pipeline_ids, "pause", executor=self._executor, now=paused_at
        )
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.organizations)
                .where(v2_schema.organizations.c.id == self._org_id)
                .values(paused_at=paused_at)
            )
        return BulkPauseResult(
            paused_sources=source_ids, paused_destinations=destination_ids, pipelines=pipeline_result
        )

    def resume_all(self, *, now: datetime | None = None) -> BulkPauseResult:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        source_ids = tuple(record.id for record in self._sources.list())
        destination_ids = tuple(record.id for record in self._destinations.list())
        for source_id in source_ids:
            self._sources.resume(source_id)
        for destination_id in destination_ids:
            self._destinations.resume(destination_id)
        pipeline_ids = self._pipelines.list_all_pipeline_ids()
        pipeline_result = self._pipelines.apply_scope_action(
            pipeline_ids, "resume", executor=self._executor, now=moment
        )
        with self._engine.begin() as connection:
            connection.execute(
                update(v2_schema.organizations)
                .where(v2_schema.organizations.c.id == self._org_id)
                .values(paused_at=None)
            )
        return BulkPauseResult(
            paused_sources=source_ids, paused_destinations=destination_ids, pipelines=pipeline_result
        )
