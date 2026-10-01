"""Route ``GET /v2/events`` — SSE étendu avec types nommés (tâche 12, contrat §3.1).

Reprend le contrat ``Last-Event-ID`` de ``/v1/events``
(``ProjectionRepository.events_after``) : le curseur est un entier opaque
(id de la table ``events``), repris depuis l'en-tête ``Last-Event-ID`` ou
le paramètre ``?last_event_id=`` (utile aux clients qui ne peuvent pas
poser d'en-tête, ex. ``EventSource`` du navigateur). Interrogation par
sondage léger (pas de ``LISTEN/NOTIFY`` dans ce chantier — décision
documentée dans le contrat §8, acceptable pour un seul control plane par
organisation).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..auth import require_scope
from ..services.events import EventsService

router = APIRouter(prefix="/v2/events", tags=["events"])

DEFAULT_POLL_INTERVAL_SECONDS = 0.05


def _service(request: Request) -> EventsService:
    return EventsService(request.app.state.engine, org_id=request.app.state.org_id)


def _cursor(request: Request, last_event_id: int | None) -> int:
    header = request.headers.get("last-event-id")
    if header is not None:
        try:
            return int(header)
        except ValueError:
            return 0
    return last_event_id or 0


async def _stream(
    service: EventsService, *, cursor: int, poll_interval: float, max_iterations: int | None
) -> AsyncIterator[str]:
    yield "retry: 3000\n\n"
    position = cursor
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        for record in service.events_after(position):
            yield record.to_sse()
            position = record.id
        iterations += 1
        await asyncio.sleep(poll_interval)


@router.get("")
async def stream_events(
    request: Request,
    identity=Depends(require_scope("read")),
    last_event_id: int | None = None,
) -> StreamingResponse:
    service = _service(request)
    cursor = _cursor(request, last_event_id)
    max_iterations = getattr(request.app.state, "sse_max_iterations", None)
    poll_interval = getattr(request.app.state, "sse_poll_interval", DEFAULT_POLL_INTERVAL_SECONDS)
    return StreamingResponse(
        _stream(service, cursor=cursor, poll_interval=poll_interval, max_iterations=max_iterations),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
