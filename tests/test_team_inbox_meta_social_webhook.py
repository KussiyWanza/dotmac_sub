from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import meta_inbox_webhooks
from app.models.integration_platform import IntegrationInbox
from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxMediaAsset,
    InboxMessage,
)
from app.services import team_inbox_read
from app.services.integrations import installations
from app.services.integrations.meta_social_installation import (
    META_SOCIAL_CONFIGURATION_SCOPE,
    ConfigureMetaSocialInstallationCommand,
    configure_meta_social_installation,
)
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import CommandContext

META_TEST_SECRET = "meta-secret"  # pragma: allowlist secret


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
        "path": "/api/v1/webhooks/meta",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in (headers or {}).items()
        ],
    }
    return Request(scope, receive)


def _sign(body: bytes, secret: str = META_TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _install_meta_social(db_session) -> None:
    result = configure_meta_social_installation(
        db_session,
        ConfigureMetaSocialInstallationCommand(
            auth_mode="individual",
            app_id="app-1",
            facebook_page_id="page-1",
            instagram_account_id="ig-1",
            graph_version="v21.0",
            webhook_url="https://sub.example.test/api/v1/webhooks/meta",
            meta_oauth_access_token_ref="env://META_TEST_OAUTH_TOKEN",
            facebook_page_access_token_ref="env://META_TEST_FACEBOOK_TOKEN",
            instagram_login_access_token_ref="env://META_TEST_INSTAGRAM_TOKEN",
            webhook_signing_secret_ref="env://META_TEST_SIGNING_SECRET",
            webhook_verify_token_ref="env://META_TEST_VERIFY_TOKEN",
            environment="test",
        ),
        context=CommandContext.system(
            actor="test.meta",
            scope=META_SOCIAL_CONFIGURATION_SCOPE,
            reason="Install Meta social webhook test binding",
        ),
    )
    installations.enable_after_connection_validation(
        db_session,
        installation_id=result.installation_id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.commit()


def _disable_profile_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        meta_inbox_webhooks,
        "fetch_contact_profile",
        lambda *args, **kwargs: None,
    )


def test_meta_inbox_webhook_verify_returns_challenge(db_session, monkeypatch):
    monkeypatch.setattr(meta_inbox_webhooks, "_verify_token", lambda db: "verify-token")

    response = meta_inbox_webhooks.verify_meta_inbox_webhook(
        mode="subscribe",
        token="verify-token",
        challenge="challenge-123",
        db=db_session,
    )

    assert response.body == b"challenge-123"


def test_meta_inbox_webhook_rejects_bad_signature(db_session, monkeypatch):
    body = b'{"entry":[]}'
    request = _request(body, {"X-Hub-Signature-256": "sha256=bad"})

    monkeypatch.setattr(
        meta_inbox_webhooks,
        "_verify_meta_signature",
        lambda db, body, sig: (_ for _ in ()).throw(
            HTTPException(status_code=401, detail="bad")
        ),
    )

    with pytest.raises(HTTPException) as exc:
        _run_async(meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session))

    assert exc.value.status_code == 401


def test_meta_signature_accepts_whatsapp_secret_fallback(db_session, monkeypatch):
    body = b'{"entry":[]}'
    monkeypatch.setattr(
        meta_inbox_webhooks,
        "inbound_secret_material",
        lambda db: SimpleNamespace(
            signing_secret=META_TEST_SECRET,
            verify_token="verify-token",
        ),
    )
    monkeypatch.setattr(
        meta_inbox_webhooks,
        "_whatsapp_app_secret",
        lambda db: "whatsapp-secret",
    )

    meta_inbox_webhooks._verify_meta_signature(
        db_session,
        body,
        _sign(body, "whatsapp-secret"),
    )


def test_meta_inbox_webhook_creates_facebook_messenger_message(db_session, monkeypatch):
    _install_meta_social(db_session)
    monkeypatch.setattr(
        meta_inbox_webhooks,
        "fetch_contact_profile",
        lambda *args, **kwargs: SimpleNamespace(
            display_name="Jane Customer",
            username=None,
            profile_pic="https://example.test/jane.jpg",
        ),
    )
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "123456789012345"},
                        "recipient": {"id": "page-1"},
                        "timestamp": 1783670400000,
                        "message": {
                            "mid": "m_fb_1",
                            "text": "Hello support",
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["status"] == "ok"
    assert response["processed"] == 1
    assert response["items"][0]["resolution_status"] == "unmatched"
    assert conversation.channel_type == InboxChannelType.facebook_messenger.value
    assert conversation.contact_address == "123456789012345"
    assert conversation.subject == "Jane Customer"
    assert conversation.external_thread_id == "facebook_messenger:123456789012345"
    assert message.external_message_id == "m_fb_1"
    assert message.from_address == "123456789012345"
    assert message.body == "Hello support"
    assert message.metadata_["provider"] == "meta_social"
    assert message.metadata_["page_id"] == "page-1"
    assert message.metadata_["external_account_id"] == "page-1"
    assert message.metadata_["provider_account_id"] == "page-1"
    assert message.metadata_["contact_profile"]["display_name"] == "Jane Customer"
    assert "platform" not in message.metadata_


def test_meta_inbox_webhook_creates_instagram_dm_message(db_session, monkeypatch):
    _install_meta_social(db_session)
    _disable_profile_lookup(monkeypatch)
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-1",
                "messaging": [
                    {
                        "sender": {"id": "17841400000000000"},
                        "recipient": {"id": "ig-1"},
                        "timestamp": 1783670500000,
                        "message": {
                            "mid": "m_ig_1",
                            "text": "Please check my account",
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["processed"] == 1
    assert conversation.channel_type == InboxChannelType.instagram_dm.value
    assert conversation.contact_address == "17841400000000000"
    assert conversation.external_thread_id == "instagram_dm:17841400000000000"
    assert message.external_message_id == "m_ig_1"
    assert message.body == "Please check my account"
    assert message.metadata_["instagram_account_id"] == "ig-1"
    assert message.metadata_["external_account_id"] == "ig-1"
    assert message.metadata_["provider_account_id"] == "ig-1"


def test_meta_inbox_webhook_creates_facebook_post_comment_thread(
    db_session,
    monkeypatch,
):
    _install_meta_social(db_session)
    _disable_profile_lookup(monkeypatch)
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "post_id": "page-1_987",
                            "comment_id": "comment-1",
                            "parent_id": "page-1_987",
                            "message": "Please check this area",
                            "created_time": 1783670600,
                            "from": {
                                "id": "fb-user-1",
                                "name": "Public Customer",
                            },
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["processed"] == 1
    assert conversation.channel_type == InboxChannelType.facebook_comment.value
    assert conversation.contact_address == "fb-user-1"
    assert conversation.subject == "Facebook Comment post page-1_987"
    assert conversation.external_thread_id == "facebook_comment:page-1_987"
    assert message.external_message_id == "comment-1"
    assert message.body == "Please check this area"
    assert message.metadata_["provider_comment_id"] == "comment-1"
    assert message.metadata_["post_id"] == "page-1_987"
    assert message.metadata_["page_id"] == "page-1"
    assert message.metadata_["commenter_name"] == "Public Customer"
    assert message.metadata_["parent_provider_comment_id"] is None


def test_meta_inbox_webhook_groups_instagram_comment_reply_by_media(
    db_session,
    monkeypatch,
):
    _install_meta_social(db_session)
    _disable_profile_lookup(monkeypatch)
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-1",
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "ig-comment-2",
                            "text": "Replying under the post",
                            "parent_id": "ig-comment-1",
                            "media": {"id": "ig-media-1"},
                            "created_time": 1783670700,
                            "from": {
                                "id": "ig-user-1",
                                "username": "igcustomer",
                            },
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    response = _run_async(
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )

    conversation = db_session.query(InboxConversation).one()
    message = db_session.query(InboxMessage).one()
    assert response["processed"] == 1
    assert conversation.channel_type == InboxChannelType.instagram_comment.value
    assert conversation.external_thread_id == "instagram_comment:ig-media-1"
    assert message.external_message_id == "ig-comment-2"
    assert message.metadata_["instagram_account_id"] == "ig-1"
    assert message.metadata_["media_id"] == "ig-media-1"
    assert message.metadata_["parent_provider_comment_id"] == "ig-comment-1"


def test_meta_inbox_webhook_deduplicates_external_message_id(db_session, monkeypatch):
    _install_meta_social(db_session)
    _disable_profile_lookup(monkeypatch)
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "timestamp": 1783670400000,
                        "message": {"mid": "m_dup", "text": "Hello"},
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})
    first = _run_async(
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )
    request = _request(body, {"X-Hub-Signature-256": _sign(body)})

    second = _run_async(
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )

    assert first["items"][0]["kind"] == "received"
    assert second == first
    assert db_session.query(InboxConversation).count() == 1
    assert db_session.query(InboxMessage).count() == 1
    assert db_session.query(IntegrationInbox).count() == 1


def test_meta_inbox_webhook_preserves_attachment_messages(db_session, monkeypatch):
    _install_meta_social(db_session)
    _disable_profile_lookup(monkeypatch)
    monkeypatch.setattr(
        meta_inbox_webhooks, "_verify_meta_signature", lambda db, body, sig: None
    )
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "psid-1"},
                        "timestamp": 1783670400000,
                        "message": {
                            "mid": "m_img",
                            "attachments": [
                                {
                                    "type": "image",
                                    "payload": {"url": "https://example.test/i.jpg"},
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
        meta_inbox_webhooks.receive_meta_inbox_webhook(request, db_session)
    )

    message = db_session.query(InboxMessage).one()
    asset = db_session.query(InboxMediaAsset).one()
    timeline = team_inbox_read.get_conversation_timeline(
        db_session,
        conversation_id=asset.conversation_id,
    )
    assert response["processed"] == 1
    assert message.body == "[image]"
    assert message.metadata_["attachments"][0]["type"] == "image"
    assert message.metadata_["attachments"][0]["url"] == "https://example.test/i.jpg"
    assert asset.asset_type == "image"
    assert asset.source_url == "https://example.test/i.jpg"
    assert asset.download_status == "remote_available"
    assert timeline.messages[0].attachments[0]["url"].startswith("/admin/inbox/media/")
    assert "raw" not in message.metadata_
