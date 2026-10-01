"""Webhooks signés + anti-rejeu + retry (tâche 13, contrat §3.2).

``WebhooksService`` gère le CRUD (le secret en clair n'est affiché qu'à la
création, chiffré ensuite — voir ``schema.py::webhooks``). ``sign_delivery``
calcule ``X-Quadringent-Signature: t=<unix>,v1=<hex>`` sur
``f"{timestamp}.{body}"``. ``WebhookDeliveryWorker`` enfile une livraison
par ``(webhook_id, event_id)`` (contrainte unique en base — un événement
déjà livré à un point de terminaison n'est jamais rejoué automatiquement),
retente avec un backoff exponentiel borné, et désactive le webhook après
des échecs consécutifs trop nombreux.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets as _secrets
import time
from typing import Protocol
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .. import schema as v2_schema
from ..crypto import SecretBox

SIGNATURE_TOLERANCE_SECONDS = 300  # 5 minutes
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 30
MAX_CONSECUTIVE_FAILURES_BEFORE_DISABLE = 5


class WebhookValidationError(ValueError):
    """Corps hors contrat (url/events invalides)."""


class WebhookNotFoundError(LookupError):
    """Aucun webhook pour cet identifiant — 404 ``not_found``."""


class WebhookDeliveryNotFoundError(LookupError):
    """Aucune livraison pour cet ``(webhook_id, event_id)`` — 404."""


@dataclass(frozen=True)
class WebhookRecord:
    id: str
    url: str
    events: tuple[str, ...]
    state: str
    consecutive_failures: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "url": self.url,
            "events": list(self.events),
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class DeliveryRecord:
    id: str
    webhook_id: str
    event_id: str
    event_type: str
    payload: object
    status: str
    attempt_count: int
    next_attempt_at: str | None
    delivered_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "webhook_id": self.webhook_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "next_attempt_at": self.next_attempt_at,
            "delivered_at": self.delivered_at,
        }


def sign_delivery(body: str, secret: str, *, timestamp: int | None = None) -> str:
    """Calcule l'en-tête ``X-Quadringent-Signature: t=<unix>,v1=<hex>``."""

    ts = timestamp if timestamp is not None else int(time.time())
    digest = hmac.new(secret.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_delivery_signature(
    body: str, secret: str, header_value: str, *, now: int | None = None, tolerance: int = SIGNATURE_TOLERANCE_SECONDS
) -> bool:
    """Vérifie une signature reçue — fermé sur tout format inattendu."""

    parts = dict(item.split("=", 1) for item in header_value.split(",") if "=" in item)
    if "t" not in parts or "v1" not in parts:
        return False
    try:
        ts = int(parts["t"])
    except ValueError:
        return False
    reference = now if now is not None else int(time.time())
    if abs(reference - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), f"{ts}.{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


class WebhookHttpClient(Protocol):
    """Contrat minimal d'un client HTTP injecté — jamais de réseau réel en test."""

    def post(self, url: str, *, headers: dict[str, str], body: str) -> int: ...


class WebhooksService:
    def __init__(self, engine: Engine, *, org_id: str, secret_box: SecretBox) -> None:
        self._engine = engine
        self._org_id = org_id
        self._secret_box = secret_box

    def create(self, *, url: object, events: object, now: datetime | None = None) -> tuple[WebhookRecord, str]:
        if not isinstance(url, str) or not (url.startswith("https://") or url.startswith("http://")):
            raise WebhookValidationError("url invalide (http(s):// requis)")
        if not isinstance(events, (list, tuple)) or not events or not all(isinstance(item, str) for item in events):
            raise WebhookValidationError("events doit être une liste non vide de chaînes")
        secret_value = _secrets.token_urlsafe(32)
        webhook_id = uuid.uuid4().hex
        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._engine.begin() as connection:
            connection.execute(
                v2_schema.webhooks.insert(),
                {
                    "id": webhook_id,
                    "org_id": self._org_id,
                    "url": url,
                    "secret_ciphertext": self._secret_box.encrypt(secret_value),
                    "events": list(events),
                    "state": "active",
                    "consecutive_failures": 0,
                    "created_at": created_at,
                },
            )
        return self.get(webhook_id), secret_value

    def get(self, webhook_id: str) -> WebhookRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(select(v2_schema.webhooks).where(v2_schema.webhooks.c.id == webhook_id))
                .mappings()
                .first()
            )
        if row is None:
            raise WebhookNotFoundError(webhook_id)
        return _to_record(row)

    def list(self) -> tuple[WebhookRecord, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(select(v2_schema.webhooks).order_by(v2_schema.webhooks.c.created_at))
                .mappings()
                .all()
            )
        return tuple(_to_record(row) for row in rows)

    def delete(self, webhook_id: str) -> None:
        self.get(webhook_id)  # 404 si absent
        with self._engine.begin() as connection:
            connection.execute(v2_schema.webhooks.delete().where(v2_schema.webhooks.c.id == webhook_id))

    def secret_for(self, webhook_id: str) -> str:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.webhooks.c.secret_ciphertext).where(v2_schema.webhooks.c.id == webhook_id)
                )
                .mappings()
                .one()
            )
        return self._secret_box.decrypt(row["secret_ciphertext"])

    def enqueue_delivery(
        self, webhook_id: str, *, event_id: str, event_type: str, payload: dict[str, object]
    ) -> DeliveryRecord | None:
        """Enfile une livraison — ``None`` si ``(webhook_id, event_id)`` existe déjà (anti-rejeu)."""

        webhook = self.get(webhook_id)
        if webhook.state != "active" or event_type not in webhook.events:
            return None
        with self._engine.connect() as connection:
            existing = (
                connection.execute(
                    select(v2_schema.webhook_deliveries.c.id)
                    .where(v2_schema.webhook_deliveries.c.webhook_id == webhook_id)
                    .where(v2_schema.webhook_deliveries.c.event_id == event_id)
                )
                .mappings()
                .first()
            )
        if existing is not None:
            return None
        delivery_id = uuid.uuid4().hex
        with self._engine.begin() as connection:
            connection.execute(
                v2_schema.webhook_deliveries.insert(),
                {
                    "id": delivery_id,
                    "webhook_id": webhook_id,
                    "event_id": event_id,
                    "event_type": event_type,
                    "payload": payload,
                    "status": "pending",
                    "attempt_count": 0,
                    "next_attempt_at": None,
                    "delivered_at": None,
                },
            )
        return self.get_delivery(delivery_id)

    def get_delivery(self, delivery_id: str) -> DeliveryRecord:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.webhook_deliveries).where(v2_schema.webhook_deliveries.c.id == delivery_id)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise WebhookDeliveryNotFoundError(delivery_id)
        return _to_delivery_record(row)

    def get_delivery_by_event(self, webhook_id: str, event_id: str) -> DeliveryRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(v2_schema.webhook_deliveries)
                    .where(v2_schema.webhook_deliveries.c.webhook_id == webhook_id)
                    .where(v2_schema.webhook_deliveries.c.event_id == event_id)
                )
                .mappings()
                .first()
            )
        return _to_delivery_record(row) if row is not None else None

    def pending_deliveries(self, webhook_id: str | None = None) -> tuple[DeliveryRecord, ...]:
        statement = select(v2_schema.webhook_deliveries).where(
            v2_schema.webhook_deliveries.c.status.in_(("pending", "failed"))
        )
        if webhook_id is not None:
            statement = statement.where(v2_schema.webhook_deliveries.c.webhook_id == webhook_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement.order_by(v2_schema.webhook_deliveries.c.created_at)).mappings().all()
        return tuple(_to_delivery_record(row) for row in rows)


class WebhookDeliveryWorker:
    """Livre les livraisons ``pending``/``failed`` via un client HTTP injecté.

    Backoff exponentiel borné (``BASE_BACKOFF_SECONDS * 2**attempt``,
    ``MAX_ATTEMPTS`` tentatives) ; après ``MAX_CONSECUTIVE_FAILURES_BEFORE_DISABLE``
    échecs consécutifs sur un webhook, celui-ci passe ``disabled`` (visible
    via ``GET /v2/webhooks/{id}``) — aucune nouvelle tentative automatique
    ensuite, seul un redeliver manuel (``POST .../redeliver/{event_id}``)
    peut relivrer un événement précis.
    """

    def __init__(self, engine: Engine, *, service: WebhooksService, http_client: WebhookHttpClient) -> None:
        self._engine = engine
        self._service = service
        self._http_client = http_client

    def deliver_one(self, delivery_id: str, *, now: datetime | None = None) -> DeliveryRecord:
        delivery = self._service.get_delivery(delivery_id)
        webhook = self._service.get(delivery.webhook_id)
        secret = self._service.secret_for(delivery.webhook_id)
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

        body = json.dumps(
            {"event_id": delivery.event_id, "event_type": delivery.event_type, "payload": delivery.payload},
            ensure_ascii=True,
            sort_keys=True,
        )
        signature = sign_delivery(body, secret, timestamp=int(reference.timestamp()))
        attempt_count = delivery.attempt_count + 1
        try:
            status_code = self._http_client.post(
                webhook.url, headers={"X-Quadringent-Signature": signature}, body=body
            )
            success = 200 <= status_code < 300
        except Exception:  # noqa: BLE001 - toute erreur réseau/du client est un échec de livraison
            success = False

        with self._engine.begin() as connection:
            if success:
                connection.execute(
                    update(v2_schema.webhook_deliveries)
                    .where(v2_schema.webhook_deliveries.c.id == delivery_id)
                    .values(status="delivered", attempt_count=attempt_count, delivered_at=reference, next_attempt_at=None)
                )
                connection.execute(
                    update(v2_schema.webhooks)
                    .where(v2_schema.webhooks.c.id == webhook.id)
                    .values(consecutive_failures=0)
                )
            else:
                exhausted = attempt_count >= MAX_ATTEMPTS
                next_attempt = None
                if not exhausted:
                    backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt_count - 1))
                    next_attempt = reference + timedelta(seconds=backoff)
                connection.execute(
                    update(v2_schema.webhook_deliveries)
                    .where(v2_schema.webhook_deliveries.c.id == delivery_id)
                    .values(
                        status="failed" if not exhausted else "failed",
                        attempt_count=attempt_count,
                        next_attempt_at=next_attempt,
                    )
                )
                new_failures = webhook.consecutive_failures + 1
                new_state = webhook.state
                if new_failures >= MAX_CONSECUTIVE_FAILURES_BEFORE_DISABLE:
                    new_state = "disabled"
                connection.execute(
                    update(v2_schema.webhooks)
                    .where(v2_schema.webhooks.c.id == webhook.id)
                    .values(consecutive_failures=new_failures, state=new_state)
                )
        return self._service.get_delivery(delivery_id)


def _to_record(row: object) -> WebhookRecord:
    return WebhookRecord(
        id=row["id"],
        url=row["url"],
        events=tuple(row["events"] or ()),
        state=row["state"],
        consecutive_failures=row["consecutive_failures"],
        created_at=_iso(row["created_at"]),
    )


def _to_delivery_record(row: object) -> DeliveryRecord:
    return DeliveryRecord(
        id=row["id"],
        webhook_id=row["webhook_id"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        payload=row["payload"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        next_attempt_at=_iso(row["next_attempt_at"]),
        delivered_at=_iso(row["delivered_at"]),
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()
