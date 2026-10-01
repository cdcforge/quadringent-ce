"""Route ``GET /v2/audit`` — journal interrogeable (tâche 11).

Scope ``admin`` : le journal d'audit peut révéler des informations
sensibles sur l'activité d'autres utilisateurs/agents (§2.5 du contrat le
liste avec les routes ``users``/``agent-tokens``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_scope
from ..services.audit import AuditService

router = APIRouter(prefix="/v2/audit", tags=["audit"])


def _service(request: Request) -> AuditService:
    return AuditService(request.app.state.engine, org_id=request.app.state.org_id)


@router.get("")
async def list_audit_records(
    request: Request,
    identity=Depends(require_scope("admin")),
    actor_kind: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    service = _service(request)
    records = service.query(
        actor_kind=actor_kind,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        limit=limit,
    )
    return {"items": [record.to_dict() for record in records], "next_cursor": None}
