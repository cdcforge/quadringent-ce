"""Service d'événements SSE persistés (tâche 12, contrat §3.1).

Table ``events`` : chaque écriture pertinente (transition de pipeline,
confirmation en attente, action terminée, alerte) y insère une ligne
typée. ``id`` (autoincrément) sert de curseur ``Last-Event-ID`` — même
sémantique que ``ProjectionRepository.events_after`` en v1 (reprise après
coupure sans perte, sans rejeu de ce qui a déjà été vu). Le flux SSE n'est
jamais la source de vérité : un client relit la ressource concernée par
HTTP, il ne consomme le payload que comme notification.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .. import schema as v2_schema

KNOWN_EVENT_TYPES = frozenset(
    {
        "pipeline.state_changed",
        "action.pending_confirmation",
        "action.completed",
        "alert.fired",
        "alert.resolved",
    }
)


@dataclass(frozen=True)
class EventRecord:
    id: int
    event_type: str
    payload: object
    at: str

    def to_sse(self) -> str:
        import json

        return f"id: {self.id}\nevent: {self.event_type}\ndata: {json.dumps(self.payload, ensure_ascii=True)}\n\n"


class EventsService:
    def __init__(self, engine: Engine, *, org_id: str) -> None:
        self._engine = engine
        self._org_id = org_id

    def publish(self, event_type: str, payload: dict[str, object]) -> EventRecord:
        with self._engine.begin() as connection:
            result = connection.execute(
                v2_schema.events.insert(),
                {"org_id": self._org_id, "event_type": event_type, "payload": payload},
            )
            event_id = result.inserted_primary_key[0]
        return self.get(event_id)

    def get(self, event_id: int) -> EventRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(select(v2_schema.events).where(v2_schema.events.c.id == event_id))
                .mappings()
                .one()
            )
        return _to_record(row)

    def events_after(self, cursor: int, *, limit: int = 200) -> tuple[EventRecord, ...]:
        """Tous les événements d'``id`` strictement supérieur à ``cursor``, dans l'ordre.

        ``cursor <= 0`` renvoie tout l'historique retenu (borné à
        ``limit``) — équivalent d'un client qui se connecte pour la
        première fois.
        """

        statement = (
            select(v2_schema.events)
            .where(v2_schema.events.c.id > cursor)
            .order_by(v2_schema.events.c.id)
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_to_record(row) for row in rows)

    def latest_id(self) -> int:
        with self._engine.connect() as connection:
            row = connection.execute(select(v2_schema.events.c.id).order_by(v2_schema.events.c.id.desc())).first()
        return row[0] if row is not None else 0


def _to_record(row: object) -> EventRecord:
    return EventRecord(id=row["id"], event_type=row["event_type"], payload=row["payload"], at=_iso(row["at"]))


def _iso(value: object) -> str:
    if isinstance(value, str):
        return value
    return value.isoformat()
