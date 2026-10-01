"""Tâche 13 — webhooks signés, anti-rejeu, retry/backoff, désactivation."""

from __future__ import annotations

import pytest

from quadringent_control_plane.v2 import db as v2_db, schema as v2_schema
from quadringent_control_plane.v2.crypto import SecretBox
from quadringent_control_plane.v2.services.webhooks import (
    MAX_CONSECUTIVE_FAILURES_BEFORE_DISABLE,
    WebhookDeliveryWorker,
    WebhooksService,
    WebhookValidationError,
    sign_delivery,
    verify_delivery_signature,
)


@pytest.fixture()
def webhooks_service(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'webhooks.sqlite3'}"
    v2_db.run_migrations(dsn)
    engine = v2_db.create_engine_for(dsn)
    with engine.begin() as connection:
        connection.execute(v2_schema.organizations.insert(), {"id": "org1", "name": "Client unique"})
    service = WebhooksService(engine, org_id="org1", secret_box=SecretBox(SecretBox.generate_key()))
    try:
        yield engine, service
    finally:
        engine.dispose()


class _RecordingHttpClient:
    def __init__(self, *, status_code: int = 200, raises: bool = False) -> None:
        self.status_code = status_code
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str], str]] = []

    def post(self, url: str, *, headers: dict[str, str], body: str) -> int:
        self.calls.append((url, dict(headers), body))
        if self.raises:
            raise ConnectionError("réseau injoignable")
        return self.status_code


# --- Signature ----------------------------------------------------------


def test_sign_delivery_then_verify_round_trip() -> None:
    header = sign_delivery('{"a":1}', "un-secret", timestamp=1_700_000_000)
    assert header == f"t=1700000000,v1={header.split('v1=')[1]}"
    assert verify_delivery_signature('{"a":1}', "un-secret", header, now=1_700_000_010) is True


def test_verify_delivery_signature_rejects_wrong_secret() -> None:
    header = sign_delivery('{"a":1}', "un-secret", timestamp=1_700_000_000)
    assert verify_delivery_signature('{"a":1}', "un-autre-secret", header, now=1_700_000_010) is False


def test_verify_delivery_signature_rejects_outside_tolerance() -> None:
    header = sign_delivery('{"a":1}', "un-secret", timestamp=1_700_000_000)
    assert verify_delivery_signature('{"a":1}', "un-secret", header, now=1_700_000_400) is False


def test_verify_delivery_signature_rejects_malformed_header() -> None:
    assert verify_delivery_signature('{"a":1}', "un-secret", "n'importe quoi") is False


# --- CRUD -----------------------------------------------------------------


def test_create_returns_secret_once_never_again(webhooks_service) -> None:
    _engine, service = webhooks_service
    record, secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    assert secret
    assert secret not in str(record.to_dict())
    fetched = service.get(record.id)
    assert secret not in str(fetched.to_dict())


def test_create_rejects_invalid_url_scheme(webhooks_service) -> None:
    _engine, service = webhooks_service
    with pytest.raises(WebhookValidationError):
        service.create(url="ftp://example.com", events=["alert.fired"])


def test_create_rejects_empty_events(webhooks_service) -> None:
    _engine, service = webhooks_service
    with pytest.raises(WebhookValidationError):
        service.create(url="https://example.com", events=[])


def test_secret_for_decrypts_to_the_original_value(webhooks_service) -> None:
    _engine, service = webhooks_service
    record, secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    assert service.secret_for(record.id) == secret


# --- Anti-rejeu et livraison -----------------------------------------------


def test_enqueue_delivery_is_idempotent_per_event_and_webhook(webhooks_service) -> None:
    _engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    first = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.fired", payload={"a": 1})
    second = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.fired", payload={"a": 1})
    assert first is not None
    assert second is None  # déjà enfilé pour ce (webhook, event_id)


def test_enqueue_delivery_ignores_event_type_not_subscribed(webhooks_service) -> None:
    _engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    result = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.resolved", payload={})
    assert result is None


def test_deliver_one_success_marks_delivered_and_signs_correctly(webhooks_service) -> None:
    engine, service = webhooks_service
    record, secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    delivery = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.fired", payload={"a": 1})
    http_client = _RecordingHttpClient(status_code=200)
    worker = WebhookDeliveryWorker(engine, service=service, http_client=http_client)

    delivered = worker.deliver_one(delivery.id)

    assert delivered.status == "delivered"
    assert delivered.delivered_at is not None
    url, headers, body = http_client.calls[0]
    assert url == "https://example.com/hook"
    assert verify_delivery_signature(body, secret, headers["X-Quadringent-Signature"]) is True


def test_deliver_one_failure_schedules_backoff_retry(webhooks_service) -> None:
    engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    delivery = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.fired", payload={})
    http_client = _RecordingHttpClient(status_code=500)
    worker = WebhookDeliveryWorker(engine, service=service, http_client=http_client)

    failed = worker.deliver_one(delivery.id)

    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert failed.next_attempt_at is not None


def test_deliver_one_network_error_counts_as_failure(webhooks_service) -> None:
    engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    delivery = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.fired", payload={})
    http_client = _RecordingHttpClient(raises=True)
    worker = WebhookDeliveryWorker(engine, service=service, http_client=http_client)

    failed = worker.deliver_one(delivery.id)
    assert failed.status == "failed"


def test_webhook_disables_after_consecutive_failures(webhooks_service) -> None:
    engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    http_client = _RecordingHttpClient(status_code=500)
    worker = WebhookDeliveryWorker(engine, service=service, http_client=http_client)

    for index in range(MAX_CONSECUTIVE_FAILURES_BEFORE_DISABLE):
        delivery = service.enqueue_delivery(
            record.id, event_id=f"evt-{index}", event_type="alert.fired", payload={}
        )
        worker.deliver_one(delivery.id)

    assert service.get(record.id).state == "disabled"


def test_a_successful_delivery_resets_consecutive_failures(webhooks_service) -> None:
    engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    failing_client = _RecordingHttpClient(status_code=500)
    worker = WebhookDeliveryWorker(engine, service=service, http_client=failing_client)
    delivery = service.enqueue_delivery(record.id, event_id="evt-1", event_type="alert.fired", payload={})
    worker.deliver_one(delivery.id)
    assert service.get(record.id).consecutive_failures == 1

    ok_client = _RecordingHttpClient(status_code=200)
    worker_ok = WebhookDeliveryWorker(engine, service=service, http_client=ok_client)
    delivery2 = service.enqueue_delivery(record.id, event_id="evt-2", event_type="alert.fired", payload={})
    worker_ok.deliver_one(delivery2.id)
    assert service.get(record.id).consecutive_failures == 0


def test_delete_removes_the_webhook(webhooks_service) -> None:
    _engine, service = webhooks_service
    record, _secret = service.create(url="https://example.com/hook", events=["alert.fired"])
    service.delete(record.id)
    assert record.id not in [item.id for item in service.list()]
