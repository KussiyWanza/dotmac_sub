from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.inbox_webhooks import _app_secret as _whatsapp_app_secret
from app.api.webhook_observation import webhook_observation
from app.db import finish_read_transaction, get_db
from app.models.team_inbox import InboxChannelType
from app.services import team_inbox_channel_receive
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.connectors.meta_social_runtime import (
    META_SOCIAL_RECEIVE_CAPABILITY,
)
from app.services.integrations.meta_social_capability import (
    fetch_contact_profile,
    inbound_secret_material,
    require_binding,
)
from app.services.integrations.meta_social_contracts import MetaSocialChannel

router = APIRouter(prefix="/webhooks/meta", tags=["meta-inbox-webhook"])
SIGNATURE_HEADER = "X-Hub-Signature-256"


def _verify_token(db: Session) -> str:
    try:
        return inbound_secret_material(db).verify_token.strip()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook verification is not configured.",
        ) from exc


def _verify_meta_signature(db: Session, raw_body: bytes, presented: str | None) -> None:
    try:
        secret = inbound_secret_material(db).signing_secret.strip()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook signature verification is not configured.",
        ) from exc
    if _signature_matches(raw_body, presented, secret):
        return
    whatsapp_secret = _whatsapp_signature_fallback_secret(db)
    if whatsapp_secret and whatsapp_secret != secret:
        if _signature_matches(raw_body, presented, whatsapp_secret):
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing Meta webhook signature.",
    )


def _signature_matches(
    raw_body: bytes,
    presented: str | None,
    secret: str,
) -> bool:
    if not presented:
        return False
    expected = (
        "sha256="
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(presented, expected)


def _whatsapp_signature_fallback_secret(db: Session) -> str | None:
    try:
        value = _whatsapp_app_secret(db)
    except Exception:
        return None
    value = value.strip()
    if not value:
        return None
    return value


def _event_timestamp(value: object) -> datetime | None:
    try:
        timestamp = int(str(value))
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp / 1000, tz=UTC)


def _comment_event_timestamp(value: object) -> datetime | None:
    try:
        timestamp = int(str(value))
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp < 100_000_000_000:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    return datetime.fromtimestamp(timestamp / 1000, tz=UTC)


def _message_text(message: dict[str, Any]) -> str:
    text = str(message.get("text") or "").strip()
    if text:
        return text
    attachments = message.get("attachments")
    if isinstance(attachments, list) and attachments:
        first = attachments[0]
        attachment_type = (
            str(first.get("type") or "attachment").strip()
            if isinstance(first, dict)
            else "attachment"
        )
        return f"[{attachment_type}]"
    if "quick_reply" in message:
        return "[quick reply]"
    return ""


def _message_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        raw_payload = item.get("payload")
        payload: dict[str, Any] = (
            dict(raw_payload) if isinstance(raw_payload, dict) else {}
        )
        normalized.append(
            {
                "type": str(item.get("type") or "attachment"),
                "provider": "meta_social",
                "provider_media_id": payload.get("id"),
                "url": payload.get("url"),
                "source_url": payload.get("url"),
                "title": item.get("title"),
                "file_name": payload.get("filename") or item.get("title"),
                "mime_type": payload.get("mime_type"),
                "file_size": payload.get("size"),
                "caption": payload.get("caption"),
                "download_status": "remote_available"
                if payload.get("url")
                else "metadata_only",
            }
        )
    return normalized


def _text(value: object) -> str | None:
    candidate = str(value or "").strip()
    return candidate or None


def _channel_for_object(payload_object: object) -> str | None:
    object_name = str(payload_object or "").strip().lower()
    if object_name == "page":
        return InboxChannelType.facebook_messenger.value
    if object_name == "instagram":
        return InboxChannelType.instagram_dm.value
    return None


def _comment_channel_for_object(payload_object: object) -> str | None:
    object_name = str(payload_object or "").strip().lower()
    if object_name == "page":
        return InboxChannelType.facebook_comment.value
    if object_name == "instagram":
        return InboxChannelType.instagram_comment.value
    return None


def _comment_body(value: dict[str, Any]) -> str:
    message = _text(value.get("message")) or _text(value.get("text"))
    if message:
        return message
    if _text(value.get("photo")) or _text(value.get("media_url")):
        return "[media]"
    return ""


def _comment_author(value: dict[str, Any]) -> tuple[str | None, str | None]:
    author = value.get("from")
    if isinstance(author, dict):
        return (
            _text(author.get("id")) or _text(author.get("username")),
            _text(author.get("name")) or _text(author.get("username")),
        )
    return (
        _text(value.get("sender_id")) or _text(value.get("user_id")),
        _text(value.get("username")) or _text(value.get("sender_name")),
    )


def _iter_meta_social_comments(payload: dict[str, Any]):
    channel_type = _comment_channel_for_object(payload.get("object"))
    if not channel_type:
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        page_or_account_id = _text(entry.get("id"))
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field = str(change.get("field") or "").strip().lower()
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            item_type = str(value.get("item") or value.get("object") or "").lower()
            if field not in {"feed", "comments", "live_comments"} and (
                "comment" not in item_type
            ):
                continue
            body = _comment_body(value)
            if not body:
                continue
            comment_id = (
                _text(value.get("comment_id"))
                or _text(value.get("id"))
                or _text(value.get("instagram_comment_id"))
            )
            media = value.get("media")
            media_id = _text(media.get("id")) if isinstance(media, dict) else None
            post_id = (
                _text(value.get("post_id"))
                or _text(value.get("media_id"))
                or media_id
                or _text(value.get("parent_id"))
                or _text(value.get("video_id"))
            )
            if not comment_id or not post_id:
                continue
            author_id, author_name = _comment_author(value)
            if not author_id:
                continue
            parent_comment_id = _text(value.get("parent_id"))
            if parent_comment_id == post_id:
                parent_comment_id = None
            received_at = (
                _comment_event_timestamp(value.get("created_time"))
                or _comment_event_timestamp(change.get("time"))
                or _comment_event_timestamp(entry.get("time"))
            )
            media_url = _text(value.get("media_url")) or _text(value.get("photo"))
            attachments = (
                [
                    {
                        "type": "image",
                        "provider": "meta_social",
                        "url": media_url,
                        "source_url": media_url,
                        "download_status": "remote_available",
                    }
                ]
                if media_url
                else []
            )
            metadata: dict[str, Any] = {
                "provider": "meta_social",
                "provider_account_id": page_or_account_id,
                "provider_account_scope": page_or_account_id,
                "external_account_id": page_or_account_id,
                "surface": "post_comment",
                "provider_comment_id": comment_id,
                "comment_id": comment_id,
                "post_id": post_id,
                "parent_provider_comment_id": parent_comment_id,
                "commenter_id": author_id,
                "commenter_name": author_name,
                "verb": _text(value.get("verb")),
                "field": field,
                "permalink_url": _text(value.get("permalink_url")),
                "media_url": media_url,
                "attachments": attachments,
            }
            if channel_type == InboxChannelType.facebook_comment.value:
                metadata["page_id"] = page_or_account_id
            if channel_type == InboxChannelType.instagram_comment.value:
                metadata["instagram_account_id"] = page_or_account_id
                metadata["media_id"] = post_id
            yield {
                "channel_type": channel_type,
                "sender_id": author_id,
                "contact_name": author_name,
                "body": body,
                "external_message_id": comment_id,
                "external_thread_id": f"{channel_type}:{post_id}",
                "subject": f"{channel_type.replace('_', ' ').title()} post {post_id}",
                "received_at": received_at,
                "metadata": metadata,
            }


def _iter_meta_social_messages(payload: dict[str, Any]):
    channel_type = _channel_for_object(payload.get("object"))
    if not channel_type:
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        page_or_account_id = str(entry.get("id") or "").strip() or None
        for event in entry.get("messaging") or []:
            if not isinstance(event, dict):
                continue
            sender = event.get("sender")
            sender_id = (
                str(sender.get("id") or "").strip() if isinstance(sender, dict) else ""
            )
            message = event.get("message")
            if not sender_id or not isinstance(message, dict):
                continue
            body = _message_text(message)
            if not body:
                continue
            external_message_id = str(message.get("mid") or "").strip() or None
            metadata: dict[str, Any] = {
                "provider": "meta_social",
                "provider_account_id": page_or_account_id,
                "provider_account_scope": page_or_account_id,
                "external_account_id": page_or_account_id,
                "attachments": _message_attachments(message),
            }
            if channel_type == InboxChannelType.facebook_messenger.value:
                metadata["page_id"] = page_or_account_id
            if channel_type == InboxChannelType.instagram_dm.value:
                metadata["instagram_account_id"] = page_or_account_id
            yield {
                "channel_type": channel_type,
                "sender_id": sender_id,
                "body": body,
                "external_message_id": external_message_id,
                "received_at": _event_timestamp(event.get("timestamp")),
                "page_or_account_id": page_or_account_id,
                "metadata": metadata,
            }


@router.get("")
def verify_meta_inbox_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
    db: Session = Depends(get_db),
):
    expected = _verify_token(db)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook verify token is not configured.",
        )
    if mode != "subscribe" or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return PlainTextResponse(challenge or "")


@router.post("")
async def receive_meta_inbox_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    with webhook_observation(provider="meta_cloud_api", event="social"):
        raw_body = await request.body()
        _verify_meta_signature(db, raw_body, request.headers.get(SIGNATURE_HEADER))
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON payload.",
            ) from None
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON payload.",
            )

        inbound_payloads: list[team_inbox_channel_receive.InboundChannelPayload] = []
        for item in (
            *_iter_meta_social_messages(payload),
            *_iter_meta_social_comments(payload),
        ):
            metadata = item.get("metadata")
            metadata_dict = metadata if isinstance(metadata, dict) else {}
            contact_name = item.get("contact_name")
            try:
                profile = (
                    fetch_contact_profile(
                        db,
                        channel=MetaSocialChannel(str(item["channel_type"])),
                        contact_id=str(item["sender_id"]),
                    )
                    if item["channel_type"]
                    in {
                        InboxChannelType.facebook_messenger.value,
                        InboxChannelType.instagram_dm.value,
                    }
                    else None
                )
            except Exception:
                profile = None
            if profile is not None:
                contact_name = profile.display_name or profile.username
                metadata_dict = {
                    **metadata_dict,
                    "contact_profile": {
                        "display_name": profile.display_name,
                        "username": profile.username,
                        "profile_pic": profile.profile_pic,
                    },
                }
            inbound_payloads.append(
                team_inbox_channel_receive.InboundChannelPayload(
                    channel_type=str(item["channel_type"]),
                    contact_address=str(item["sender_id"]),
                    body=str(item["body"]),
                    contact_name=contact_name,
                    external_message_id=item.get("external_message_id"),
                    external_thread_id=item.get("external_thread_id"),
                    subject=item.get("subject"),
                    received_at=item.get("received_at"),
                    metadata=metadata_dict,
                ),
            )
        finish_read_transaction(db)
        binding = require_binding(
            db,
            capability_id=META_SOCIAL_RECEIVE_CAPABILITY,
        )
        receipt, should_process = integration_inbox.receive_and_claim_verified(
            db,
            capability_binding_id=binding.id,
            provider_event_id=f"meta-social:{hashlib.sha256(raw_body).hexdigest()}",
            event_type="meta.social.webhook.v1",
            payload=payload,
            headers={
                key: value
                for key, value in {
                    "content-type": request.headers.get("content-type"),
                    "user-agent": request.headers.get("user-agent"),
                }.items()
                if value
            },
        )
        if not should_process:
            return dict(receipt.consequence_json)
        try:
            results = (
                team_inbox_channel_receive.receive_inbound_channel_batch_committed(
                    db,
                    inbound_payloads,
                )
            )
            consequence: dict[str, object] = {
                "status": "ok",
                "processed": len(results),
                "items": results,
            }
            integration_inbox.complete_consequence(
                db,
                receipt=receipt,
                consequence=consequence,
            )
        except Exception as exc:
            integration_inbox.fail_consequence(
                db,
                receipt=receipt,
                error_code="meta_social_consequence_failed",
                error_detail=type(exc).__name__,
            )
            raise
        return consequence
