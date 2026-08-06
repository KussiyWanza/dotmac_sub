from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.webhook_observation import webhook_observation
from app.db import get_db
from app.services import team_inbox_channel_receive
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.whatsapp_capability import (
    WHATSAPP_RECEIVE_CAPABILITY,
    inbound_secret_material,
    require_binding,
)
from app.services.integrations.whatsapp_installation import (
    VerifyWhatsAppWebhookChallengeQuery,
    WhatsAppWebhookVerificationError,
    verify_whatsapp_webhook_challenge,
)

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp-webhook"])

SIGNATURE_HEADER = "X-Hub-Signature-256"


def _app_secret(db: Session) -> str:
    return str(inbound_secret_material(db).get("webhook_signing_secret") or "").strip()


def _verify_meta_signature(db: Session, raw_body: bytes, presented: str | None) -> None:
    secret = _app_secret(db)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook signature verification is not configured.",
        )
    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Meta webhook signature.",
        )


def _text_body(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, dict):
        body = str(text.get("body") or "").strip()
        if body:
            return body
    elif str(text or "").strip():
        return str(text or "").strip()

    message_type = str(message.get("type") or "").strip().lower()
    if message_type in {
        "image",
        "document",
        "audio",
        "video",
        "sticker",
        "location",
        "contacts",
        "button",
        "interactive",
    }:
        return f"[{message_type}]"
    return ""


def _whatsapp_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    message_type = str(message.get("type") or "").strip().lower()
    if message_type in {"text", ""}:
        return []
    raw_payload = message.get(message_type)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    attachment: dict[str, Any] = {
        "type": message_type,
        "id": payload.get("id"),
        "mime_type": payload.get("mime_type"),
        "caption": payload.get("caption"),
        "filename": payload.get("filename"),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "name": payload.get("name"),
        "address": payload.get("address"),
    }
    return [{key: value for key, value in attachment.items() if value is not None}]


def _iter_meta_whatsapp_messages(payload: dict[str, Any]):
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            raw_metadata = value.get("metadata")
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            metadata.setdefault("account_scope", entry.get("id"))
            raw_contacts = value.get("contacts")
            contacts = raw_contacts if isinstance(raw_contacts, list) else []
            names_by_wa_id: dict[str, object] = {}
            for contact in contacts:
                if not isinstance(contact, dict) or not contact.get("wa_id"):
                    continue
                profile = contact.get("profile")
                names_by_wa_id[str(contact["wa_id"])] = (
                    profile.get("name") if isinstance(profile, dict) else None
                )
            raw_messages = value.get("messages")
            messages = raw_messages if isinstance(raw_messages, list) else []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                body = _text_body(message)
                sender = str(message.get("from") or "").strip()
                if not sender or not body:
                    continue
                yield {
                    "message": {
                        "from": sender,
                        "text": body,
                        "id": str(message.get("id") or "").strip() or None,
                    },
                    "contact_name": names_by_wa_id.get(sender),
                    "metadata": metadata,
                    "attachments": _whatsapp_attachments(message),
                    "observed_at": (
                        datetime.fromtimestamp(
                            float(str(message.get("timestamp"))), tz=UTC
                        )
                        if str(message.get("timestamp") or "").isdigit()
                        else datetime.now(UTC)
                    ),
                }


def _iter_meta_whatsapp_statuses(payload: dict[str, Any]):
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            raw_metadata = value.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_statuses = value.get("statuses")
            statuses = raw_statuses if isinstance(raw_statuses, list) else []
            for status_item in statuses:
                if not isinstance(status_item, dict):
                    continue
                message_id = str(status_item.get("id") or "").strip()
                status_text = str(status_item.get("status") or "").strip()
                if not message_id or not status_text:
                    continue
                yield {
                    "message_id": message_id,
                    "status": status_text,
                    "timestamp": status_item.get("timestamp"),
                    "recipient_id": status_item.get("recipient_id"),
                    "errors": status_item.get("errors"),
                    "provider_account_scope": metadata.get("phone_number_id")
                    or metadata.get("display_phone_number")
                    or entry.get("id"),
                }


@router.get("/meta")
def verify_meta_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
    db: Session = Depends(get_db),
):
    if mode != "subscribe" or not token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    try:
        result = verify_whatsapp_webhook_challenge(
            db=db,
            query=VerifyWhatsAppWebhookChallengeQuery(presented_token=token),
        )
    except WhatsAppWebhookVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook verify token is not configured.",
        ) from exc
    if not result.accepted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return PlainTextResponse(challenge or "")


@router.post("/meta")
async def receive_meta_whatsapp_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    with webhook_observation(provider="meta_cloud_api", event="whatsapp"):
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

        inbound_payloads: list[dict[str, Any]] = []
        status_items: list[dict[str, Any]] = []
        for item in _iter_meta_whatsapp_messages(payload):
            inbound_payloads.append(
                {
                    "message": item["message"],
                    "contact_name": item.get("contact_name"),
                    "metadata": item.get("metadata"),
                    "attachments": item.get("attachments"),
                    "observed_at": item.get("observed_at"),
                }
            )
        for item in _iter_meta_whatsapp_statuses(payload):
            status_items.append(item)
        binding = require_binding(db, capability_id=WHATSAPP_RECEIVE_CAPABILITY)
        receipt, should_process = integration_inbox.receive_and_claim_verified(
            db,
            capability_binding_id=binding.id,
            provider_event_id=f"meta:{hashlib.sha256(raw_body).hexdigest()}",
            event_type="whatsapp.meta.webhook.v1",
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
            results, status_results = (
                team_inbox_channel_receive.receive_whatsapp_webhook_batch_committed(
                    db,
                    provider="meta_cloud_api",
                    payloads=inbound_payloads,
                    status_items=status_items,
                )
            )
            consequence: dict[str, object] = {
                "status": "ok",
                "processed": len(results),
                "status_processed": len(status_results),
                "items": results,
                "status_items": status_results,
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
                error_code="whatsapp_consequence_failed",
                error_detail=type(exc).__name__,
            )
            raise
        return consequence
