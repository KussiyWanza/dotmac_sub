from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import inbox_webhooks
from app.models.integration_platform import IntegrationInbox
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxConversation,
    InboxMediaAsset,
    InboxMessage,
    InboxMessageDirection,
    InboxProviderObservation,
)
from app.services import team_inbox_maintenance, team_inbox_read
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult
from app.services.integrations.whatsapp_capability import (
    WHATSAPP_RECEIVE_CAPABILITY,
    WHATSAPP_SEND_CAPABILITY,
)
from app.services.integrations.whatsapp_installation import (
    VerifyWhatsAppWebhookChallengeQuery,
)
from app.services.owner_commands import CommandContext

META_TEST_SECRET = "meta-secret"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _enabled_whatsapp_installation(db_session):
    installation = installations.create_draft(
        db_session,
        connector_key="whatsapp",
        name="WhatsApp webhook test",
        environment="test",
    )
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={"provider": "meta_cloud_api", "phone_number": "phone-1"},
        secret_refs={
            "service_credentials": "env://WHATSAPP_TEST_TOKEN",
            "webhook_signing_secret": "env://WHATSAPP_TEST_SIGNING_SECRET",
            "webhook_verify_token": "env://WHATSAPP_TEST_VERIFY_TOKEN",
        },
    )
    for capability_id in (WHATSAPP_SEND_CAPABILITY, WHATSAPP_RECEIVE_CAPABILITY):
        installations.bind_capability(
            db_session,
            installation_id=installation.id,
            capability_id=capability_id,
            policy={"default": True},
        )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["exc"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "exc" in result:
        raise result["exc"]  # type: ignore[misc]
    return result.get("value")


def _request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/webhooks/whatsapp/meta",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
    }
    return Request(scope, receive)


def _sign(body: bytes, secret: str = META_TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Ada",
        last_name="Nwosu",
        email="ada@example.com",
        phone="0803 555 0114",
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def test_meta_webhook_verify_returns_challenge(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEST_VERIFY_TOKEN", "verify-token")
    installation = installations.list_installations(
        db_session, connector_key="whatsapp"
    )[0]
    installations.disable_installation(
        db_session,
        installation_id=installation.id,
        reason="webhook_setup",
    )

    response = inbox_webhooks.verify_meta_webhook(
        mode="subscribe",
        token="verify-token",
        challenge="challenge-123",
        db=db_session,
    )

    assert response.body == b"challenge-123"


def test_meta_webhook_verification_query_repr_redacts_presented_token():
    query = VerifyWhatsAppWebhookChallengeQuery(
        presented_token="provider-presented-token"
    )

    assert "provider-presented-token" not in repr(query)


def test_meta_webhook_verify_rejects_bad_token(db_session, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TEST_VERIFY_TOKEN", "verify-token")
    installation = installations.list_installations(
        db_session, connector_key="whatsapp"
    )[0]
    installations.disable_installation(
        db_session,
        installation_id=installation.id,
        reason="webhook_setup",
    )

    with pytest.raises(HTTPException) as exc:
        inbox_webhooks.verify_meta_webhook(
            mode="subscribe",
            token="wrong",
            challenge="challenge-123",
            db=db_session,
        )

    assert exc.value.status_code == 403


def test_meta_webhook_verify_reports_unavailable_secret(db_session, monkeypatch):
    monkeypatch.delenv("WHATSAPP_TEST_VERIFY_TOKEN", raising=False)
    installation = installations.list_installations(
        db_session, connector_key="whatsapp"
    )[0]
    installations.disable_installation(
        db_session,
        installation_id=installation.id,
        reason="webhook_setup",
    )

    with pytest.raises(HTTPException) as exc:
        inbox_webhooks.verify_meta_webhook(
            mode="subscribe",
            token="verify-token",
            challenge="challenge-123",
            db=db_session,
        )

    assert exc.value.status_code == 503


def test_meta_whatsapp_post_remains_disabled_during_webhook_setup(
    db_session, monkeypatch
):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    installation = installations.list_installations(
        db_session, connector_key="whatsapp"
    )[0]
    installations.disable_installation(
        db_session,
        installation_id=installation.id,
        reason="webhook_setup",
    )
    body = b'{"entry":[]}'
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    with pytest.raises(installations.InstallationError, match="no enabled binding"):
        _run_async(inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session))

    assert db_session.query(IntegrationInbox).count() == 0


def test_meta_whatsapp_webhook_rejects_bad_signature(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    body = b'{"entry":[]}'
    request = _request(body, {"X-Hub-Signature-256": "sha256=bad"})

    with pytest.raises(HTTPException) as exc:
        _run_async(inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session))

    assert exc.value.status_code == 401


def test_meta_whatsapp_webhook_creates_native_inbox_message(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    subscriber = _subscriber(db_session)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "08000000000",
                                "phone_number_id": "phone-number-id",
                            },
                            "contacts": [
                                {
                                    "wa_id": "2348035550114",
                                    "profile": {"name": "Ada Nwosu"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": "2348035550114",
                                    "id": "wamid.meta-1",
                                    "timestamp": "1783670400",
                                    "type": "text",
                                    "text": {"body": "My internet is down"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["status"] == "ok"
    assert response["processed"] == 1
    assert response["items"][0]["subscriber_id"] == str(subscriber.id)
    assert conversation.subscriber_id == subscriber.id
    assert conversation.contact_address == "+2348035550114"
    assert message.external_message_id == "wamid.meta-1"
    assert message.body == "My internet is down"
    receipt = db_session.query(IntegrationInbox).one()
    assert receipt.state == "processed"
    assert receipt.consequence_json["processed"] == 1

    replay = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(
            _request(body, {"X-Hub-Signature-256": _sign(body)}), db_session
        )
    )
    assert replay == receipt.consequence_json
    assert db_session.query(InboxMessage).count() == 1
    assert db_session.query(IntegrationInbox).count() == 1


def test_meta_whatsapp_webhook_preserves_media_message(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": "2348035550114"}],
                            "messages": [
                                {
                                    "from": "2348035550114",
                                    "id": "wamid.media-1",
                                    "timestamp": "1783670400",
                                    "type": "image",
                                    "image": {
                                        "id": "media-1",
                                        "mime_type": "image/jpeg",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    message = db_session.query(InboxMessage).one()
    assert response["processed"] == 1
    assert message.external_message_id == "wamid.media-1"
    assert message.body == "[image]"
    assert message.metadata_["attachments"][0]["type"] == "image"
    assert message.metadata_["attachments"][0]["id"] == "media-1"
    assert "raw" not in message.metadata_


def test_meta_whatsapp_webhook_renders_location_as_google_maps_link(
    db_session, monkeypatch
):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": "2348035550114"}],
                            "messages": [
                                {
                                    "from": "2348035550114",
                                    "id": "wamid.location-1",
                                    "timestamp": "1783670400",
                                    "type": "location",
                                    "location": {
                                        "latitude": 6.5243793,
                                        "longitude": 3.3792057,
                                        "name": "Customer location",
                                        "address": "Lagos, Nigeria",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(
            _request(body, {"X-Hub-Signature-256": _sign(body)}), db_session
        )
    )

    message = db_session.query(InboxMessage).one()
    asset = db_session.query(InboxMediaAsset).one()
    message_id = message.id
    asset_id = asset.id
    conversation_id = message.conversation_id
    location = message.metadata_["attachments"][0]["location"]
    timeline = team_inbox_read.get_conversation_timeline(
        db_session, message.conversation_id
    )

    assert response["processed"] == 1
    assert location == {
        "latitude": 6.5243793,
        "longitude": 3.3792057,
        "name": "Customer location",
        "address": "Lagos, Nigeria",
    }
    assert asset.asset_type == "location"
    assert asset.metadata_["location"] == location
    assert timeline is not None
    attachment = timeline.messages[0].attachments[0]
    assert attachment.location is not None
    assert attachment.location.name == "Customer location"
    assert attachment.url == (
        "https://www.google.com/maps/search/?api=1&query=6.5243793%2C3.3792057"
    )
    assert attachment.url is not None
    assert "/admin/inbox/media/" not in attachment.url

    message_metadata = dict(message.metadata_)
    legacy_attachment = dict(message_metadata["attachments"][0])
    legacy_attachment.pop("location")
    message_metadata["attachments"] = [legacy_attachment]
    message.metadata_ = message_metadata
    asset_metadata = dict(asset.metadata_)
    asset_metadata.pop("location")
    asset.metadata_ = asset_metadata
    db_session.commit()

    repaired = team_inbox_maintenance.repair_whatsapp_locations(
        db_session,
        team_inbox_maintenance.RepairWhatsAppLocationsCommand(
            context=CommandContext.system(
                actor="test:team-inbox-location-repair",
                scope="team-inbox:maintenance",
                reason="repair historical WhatsApp location test fixture",
                idempotency_key="repair-location-1",
            ),
            conversation_ids=(conversation_id,),
        ),
    )

    assert repaired.repaired == 1
    assert repaired.missing_evidence == 0
    db_session.expire_all()
    repaired_message = db_session.get(InboxMessage, message_id)
    repaired_asset = db_session.get(InboxMediaAsset, asset_id)
    assert repaired_message is not None
    assert repaired_asset is not None
    assert repaired_message.metadata_["attachments"][0]["location"] == location
    assert repaired_asset.metadata_["location"] == location
    db_session.commit()

    replay = team_inbox_maintenance.repair_whatsapp_locations(
        db_session,
        team_inbox_maintenance.RepairWhatsAppLocationsCommand(
            context=CommandContext.system(
                actor="test:team-inbox-location-repair",
                scope="team-inbox:maintenance",
                reason="verify historical WhatsApp location repair idempotency",
                idempotency_key="repair-location-2",
            ),
            conversation_ids=(conversation_id,),
        ),
    )
    assert replay.repaired == 0
    assert replay.already_complete == 1


def test_meta_whatsapp_webhook_updates_outbound_delivery_status(
    db_session, monkeypatch
):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    conversation = InboxConversation(
        channel_type="whatsapp",
        contact_address="+2348035550114",
        external_thread_id="whatsapp:+2348035550114",
    )
    db_session.add(conversation)
    db_session.flush()
    message = InboxMessage(
        conversation_id=conversation.id,
        channel_type="whatsapp",
        direction=InboxMessageDirection.outbound.value,
        body="We are checking this.",
        external_message_id="wamid.outbound-1",
        to_addresses=["+2348035550114"],
        metadata_={"provider_result": {"provider_message_id": "wamid.outbound-1"}},
    )
    db_session.add(message)
    db_session.commit()
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "phone-number-id"},
                            "statuses": [
                                {
                                    "id": "wamid.outbound-1",
                                    "status": "delivered",
                                    "timestamp": "1783670500",
                                    "recipient_id": "2348035550114",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    db_session.refresh(message)
    assert response["processed"] == 0
    assert response["status_processed"] == 1
    assert response["status_items"][0]["kind"] == "updated"
    assert message.metadata_["delivery_status"] == "delivered"
    assert message.metadata_["delivery_status_at"] == "2026-07-10T08:01:40+00:00"
    assert message.metadata_["delivery_recipient_id"] == "2348035550114"
    assert message.metadata_["delivery_status_history"][-1]["status"] == "delivered"
    observation = db_session.query(InboxProviderObservation).one()
    assert observation.provider_account_scope == "phone-number-id"


def test_meta_whatsapp_webhook_acknowledges_unknown_status(db_session, monkeypatch):
    monkeypatch.setattr(inbox_webhooks, "_app_secret", lambda db: META_TEST_SECRET)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.missing",
                                    "status": "failed",
                                    "timestamp": "1783670600",
                                    "recipient_id": "2348035550114",
                                    "errors": [{"code": 131026}],
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        inbox_webhooks.receive_meta_whatsapp_webhook(request, db_session)
    )

    assert response["processed"] == 0
    assert response["status_processed"] == 1
    assert response["status_items"][0] == {
        "kind": "not_found",
        "provider_message_id": "wamid.missing",
        "status": "failed",
    }
    assert db_session.query(InboxMessage).count() == 0
