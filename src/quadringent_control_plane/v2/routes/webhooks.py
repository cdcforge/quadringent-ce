"""Routes ``/v2/webhooks`` — CRUD + rejeu manuel (tâche 13).

Scope ``admin`` en écriture (enregistrer un point de terminaison qui
recevra des événements est sensible), ``read`` pour la liste/détail. La
livraison effective (``WebhookDeliveryWorker``) tourne hors requête HTTP
(tâche d'arrière-plan injectable) ; ce module se limite au CRUD et au
rejeu manuel d'un événement précis.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..auth import require_scope
from ..errors import invalid_request, not_found
from ..http import AuditContext, envelope, idempotent_write
from ..services.webhooks import WebhookNotFoundError, WebhooksService, WebhookValidationError

router = APIRouter(prefix="/v2/webhooks", tags=["webhooks"])


def _service(request: Request) -> WebhooksService:
    return WebhooksService(request.app.state.engine, org_id=request.app.state.org_id, secret_box=request.app.state.secret_box)


@router.get("")
async def list_webhooks(request: Request, identity=Depends(require_scope("read"))) -> dict[str, object]:
    service = _service(request)
    return {"items": [record.to_dict() for record in service.list()], "next_cursor": None}


@router.get("/{webhook_id}")
async def get_webhook(
    webhook_id: str, request: Request, identity=Depends(require_scope("read"))
) -> dict[str, object]:
    service = _service(request)
    try:
        record = service.get(webhook_id)
    except WebhookNotFoundError as error:
        raise not_found("webhook introuvable") from error
    return record.to_dict()


@router.post("", status_code=201)
async def create_webhook(
    request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            record, secret = service.create(url=payload.get("url"), events=payload.get("events"))
        except WebhookValidationError as error:
            raise invalid_request(str(error)) from error
        body = record.to_dict()
        body["secret"] = secret
        return 201, envelope(before=None, after=body, verify_method="GET", verify_path=f"/v2/webhooks/{record.id}")

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path="/v2/webhooks",
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="webhook.create", resource_type="webhook"),
        identity=identity,
    )


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: str, request: Request, response: Response, identity=Depends(require_scope("admin"))
) -> dict[str, object]:
    service = _service(request)
    path = f"/v2/webhooks/{webhook_id}"

    def build(_payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            before = service.get(webhook_id)
            service.delete(webhook_id)
        except WebhookNotFoundError as error:
            raise not_found("webhook introuvable") from error
        return 200, envelope(before=before.to_dict(), after=None, verify_method="GET", verify_path="/v2/webhooks")

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="DELETE",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="webhook.delete", resource_type="webhook", resource_id=webhook_id),
        identity=identity,
    )


@router.post("/{webhook_id}/redeliver/{event_id}")
async def redeliver_event(
    webhook_id: str,
    event_id: str,
    request: Request,
    response: Response,
    identity=Depends(require_scope("operate")),
) -> dict[str, object]:
    """Force une nouvelle tentative pour ``(webhook_id, event_id)`` — jamais automatique."""

    service = _service(request)
    path = f"/v2/webhooks/{webhook_id}/redeliver/{event_id}"

    def build(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        try:
            service.get(webhook_id)
        except WebhookNotFoundError as error:
            raise not_found("webhook introuvable") from error
        event_type = payload.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            raise invalid_request("event_type requis pour rejouer une livraison")
        delivery = service.enqueue_delivery(
            webhook_id, event_id=event_id, event_type=event_type, payload=payload.get("payload", {})
        )
        if delivery is None:
            # Anti-rejeu : déjà enfilé — la route redeliver reste
            # idempotente sans dupliquer la livraison existante.
            existing = service.get_delivery_by_event(webhook_id, event_id)
            if existing is None:
                raise not_found("aucune livraison existante à rejouer pour cet événement")
            body = existing.to_dict()
        else:
            body = delivery.to_dict()
        return 202, envelope(
            before=None, after=body, verify_method="GET", verify_path=f"/v2/webhooks/{webhook_id}"
        )

    return await idempotent_write(
        request=request,
        response=response,
        actor_id=identity.subject,
        method="POST",
        path=path,
        store=request.app.state.idempotency_store,
        build=build,
        audit=AuditContext(action="webhook.redeliver", resource_type="webhook", resource_id=webhook_id),
        identity=identity,
    )
