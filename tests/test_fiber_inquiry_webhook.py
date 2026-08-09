"""Signed fiber inquiry ingestion, identity, and replay guarantees."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.fiber_inquiry_webhooks import receive_fiber_inquiry
from app.models.integration_platform import IntegrationInbox
from app.models.party import Party
from app.models.sales import Lead
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationLeadLink,
    InboxMessage,
    InboxProviderObservation,
)
from app.services.integrations.connectors.fiber_inquiry_http import (
    FIBER_INQUIRY_CAPABILITY,
)
from tests.integration_platform_helpers import enable_capability

SIGNING_SECRET = "test-fiber-inquiry-signing-secret"
SIGNATURE_HEADER = "x-test-fiber-signature"
DELIVERY_HEADER = "x-test-fiber-delivery"
SIGNATURE_PREFIX = "sha256="


def _binding(db_session, monkeypatch):
    monkeypatch.setenv("FIBER_INQUIRY_TEST_SIGNING_SECRET", SIGNING_SECRET)
    return enable_capability(
        db_session,
        connector_key="fiber.inquiry.http",
        capability_id=FIBER_INQUIRY_CAPABILITY,
        config={
            "signature_header": SIGNATURE_HEADER,
            "delivery_id_header": DELIVERY_HEADER,
            "signature_prefix": SIGNATURE_PREFIX,
            "site_id": "fiber.dotmac.ng",
        },
        secret_refs={
            "webhook_signing_secret": "env://FIBER_INQUIRY_TEST_SIGNING_SECRET"
        },
    )


def _payload(*, email: str = "prospect@example.com", phone: str = "08031234567"):
    return {
        "form_version": "fiber-contact-v1",
        "full_name": "Fiber Prospect",
        "phone": phone,
        "email": email,
        "interest": "new_connection",
        "message": "Please check my coverage.",
        "submitted_at": "2026-08-09T14:30:00Z",
    }


def _request(*, raw: bytes, headers: dict[str, str]) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/webhooks/fiber-inquiry/test-binding",
            "raw_path": b"/api/v1/webhooks/fiber-inquiry/test-binding",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


def _post(db_session, binding_id, payload: dict, delivery_id: str):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = (
        SIGNATURE_PREFIX
        + hmac.new(SIGNING_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    )
    request = _request(
        raw=raw,
        headers={
            SIGNATURE_HEADER: signature,
            DELIVERY_HEADER: delivery_id,
            "content-type": "application/json",
        },
    )
    return asyncio.run(receive_fiber_inquiry(binding_id, request, db_session))


def _subscriber(db_session, *, email: str, phone: str) -> Subscriber:
    row = Subscriber(
        first_name="Existing",
        last_name="Customer",
        email=email,
        phone=phone,
        status=SubscriberStatus.active,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_signed_unmatched_inquiry_creates_observation_inbox_and_prospect(
    db_session, monkeypatch
) -> None:
    binding = _binding(db_session, monkeypatch)

    response = _post(db_session, binding.id, _payload(), "fiber-delivery-1")

    conversation = db_session.get(InboxConversation, response.conversation_id)
    message = db_session.get(InboxMessage, response.message_id)
    assert conversation.channel_type == InboxChannelType.website_fiber.value
    assert message.channel_type == InboxChannelType.website_fiber.value
    assert conversation.subscriber_id is None
    assert response.resolution_status == "unmatched"
    assert db_session.query(InboxProviderObservation).count() == 1
    assert db_session.query(IntegrationInbox).count() == 1
    assert db_session.query(Party).count() == 1
    assert db_session.query(Lead).count() == 1
    assert db_session.query(InboxConversationLeadLink).count() == 1


def test_exact_replay_returns_same_conversation_and_message(
    db_session, monkeypatch
) -> None:
    binding = _binding(db_session, monkeypatch)
    payload = _payload()

    first = _post(db_session, binding.id, payload, "fiber-delivery-replay")
    replay = _post(db_session, binding.id, payload, "fiber-delivery-replay")

    assert replay.replayed is True
    assert replay.conversation_id == first.conversation_id
    assert replay.message_id == first.message_id
    assert db_session.query(InboxConversation).count() == 1
    assert db_session.query(InboxMessage).count() == 1
    assert db_session.query(Lead).count() == 1


def test_invalid_signature_creates_no_durable_fact(db_session, monkeypatch) -> None:
    binding = _binding(db_session, monkeypatch)
    raw = json.dumps(_payload(), separators=(",", ":")).encode()

    request = _request(
        raw=raw,
        headers={
            SIGNATURE_HEADER: f"{SIGNATURE_PREFIX}invalid",
            DELIVERY_HEADER: "fiber-invalid-signature",
            "content-type": "application/json",
        },
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(receive_fiber_inquiry(binding.id, request, db_session))

    assert exc.value.status_code == 401
    assert db_session.query(IntegrationInbox).count() == 0
    assert db_session.query(InboxProviderObservation).count() == 0


def test_existing_subscriber_match_creates_no_prospect(db_session, monkeypatch) -> None:
    subscriber = _subscriber(
        db_session,
        email="customer@example.com",
        phone="+2348031234567",
    )
    binding = _binding(db_session, monkeypatch)

    response = _post(
        db_session,
        binding.id,
        _payload(email="CUSTOMER@example.com", phone="08031234567"),
        "fiber-existing-subscriber",
    )

    conversation = db_session.get(InboxConversation, response.conversation_id)
    assert conversation.subscriber_id == subscriber.id
    assert response.resolution_status == "linked_subscriber"
    assert db_session.query(Lead).count() == 0
    assert db_session.query(Party).count() == 0


def test_conflicting_email_and_phone_fail_closed_for_identity(
    db_session, monkeypatch
) -> None:
    _subscriber(
        db_session,
        email="email-owner@example.com",
        phone="+2348030000001",
    )
    _subscriber(
        db_session,
        email="phone-owner@example.com",
        phone="+2348031234567",
    )
    binding = _binding(db_session, monkeypatch)

    response = _post(
        db_session,
        binding.id,
        _payload(email="email-owner@example.com", phone="08031234567"),
        "fiber-ambiguous-identity",
    )

    conversation = db_session.get(InboxConversation, response.conversation_id)
    assert conversation.subscriber_id is None
    assert response.resolution_status == "identity_review_required"
    assert conversation.metadata_["identity_review_required"] is True
    assert db_session.query(Lead).count() == 0
    assert db_session.query(Party).count() == 0
