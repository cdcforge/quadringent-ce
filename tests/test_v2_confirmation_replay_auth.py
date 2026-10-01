"""L'authentification et le lien signé sont vérifiés avant tout rejeu."""

from datetime import datetime, timedelta, timezone

import pytest

from quadringent_control_plane.v2 import schema
from quadringent_control_plane.v2.services.confirmations import ConfirmationsService
from test_v2_one_time_secrets import native_app  # noqa: F401 — fixture native partagée


def _confirmation(client, engine):
    service = ConfirmationsService(engine, org_id="default", pepper=client.app.state.token_pepper)
    record, token = service.create(
        action_ref="pipeline.remove",
        resource_type="pipeline",
        resource_id="synthetic",
        reason="Test",
        requested_by_kind="human",
        requested_by_id="test",
    )
    return f"/v2/confirmations/{record.id}/approve", token, record.id


def _agent(client, label):
    record, token = client.app.state.agent_tokens_service.create(
        name=label,
        scope="operate",
        created_by="test",
        never_expires=True,
        pre_authorized_actions=["pipeline.remove"],
    )
    return {"Authorization": f"Bearer {token}"}, record.id


@pytest.mark.parametrize("attack", ["anonymous", "other-actor", "spoofed-header", "anonymous-spoof"])
def test_authenticated_approval_cannot_be_replayed_by_another_identity(native_app, attack):
    client, engine = native_app
    route, _token, _id = _confirmation(client, engine)
    owner, owner_id = _agent(client, "Owner")
    key = {"Idempotency-Key": "approval"}
    first = client.post(route, json={}, headers={**owner, **key})
    assert first.status_code == 200
    headers = {**key}
    if attack not in {"anonymous", "anonymous-spoof"}:
        other, _ = _agent(client, "Other")
        headers.update(other)
    if attack in {"spoofed-header", "anonymous-spoof"}:
        headers["x-request-actor"] = f"agent:{owner_id}"
    denied = client.post(route, json={}, headers=headers)
    assert denied.status_code == (401 if attack in {"anonymous", "anonymous-spoof"} else 409)
    replay = client.post(route, json={}, headers={**owner, **key, "x-request-actor": "spoof"})
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_signed_link_replay_has_stable_identity_without_actor_header(native_app):
    client, engine = native_app
    route, token, _id = _confirmation(client, engine)
    key = {"Idempotency-Key": "signed-link"}
    first = client.post(route, json={"token": token}, headers={**key, "x-request-actor": "first"})
    replay = client.post(route, json={"token": token}, headers={**key, "x-request-actor": "second"})
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    repeated_action = client.post(route, json={"token": token}, headers={"Idempotency-Key": "new-link-key"})
    assert repeated_action.status_code == 409


def test_expired_signed_link_cannot_replay_cached_approval(native_app):
    client, engine = native_app
    route, token, confirmation_id = _confirmation(client, engine)
    key = {"Idempotency-Key": "signed-link-expiry"}
    first = client.post(route, json={"token": token}, headers=key)
    assert first.status_code == 200
    with engine.begin() as connection:
        connection.execute(
            schema.confirmations.update()
            .where(schema.confirmations.c.id == confirmation_id)
            .values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
    replay = client.post(route, json={"token": token}, headers=key)
    assert replay.status_code == 400
