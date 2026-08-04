"""Read-only official activity projection for one Quote detail page."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditEvent
from app.models.notification import NotificationStatus
from app.models.sales import Quote, QuoteDeliveryRequest
from app.services.audit_helpers import (
    load_audit_actor_subscribers,
    resolve_actor_name,
)


class QuoteActivityItem(TypedDict):
    title: str
    description: str
    occurred_at: datetime
    tone: str


_ACTION_LABELS = {
    "quote.created": "Quote created",
    "quote.updated": "Quote updated",
    "quote.line_added": "Line item added",
    "quote.line_updated": "Line item updated",
    "quote.line_removed": "Line item removed",
    "quote.status_changed": "Status changed",
    "quote.accepted": "Quote accepted",
    "quote.deactivated": "Quote deactivated",
    "quote.pdf_exported": "PDF exported",
    "quote.email_queued": "Email queued",
    "quote.email_suppressed": "Email suppressed",
}


def _audit_items(db: Session, quote_id: UUID, limit: int) -> list[QuoteActivityItem]:
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "quote",
                AuditEvent.entity_id == str(quote_id),
                AuditEvent.is_active.is_(True),
            )
            .order_by(AuditEvent.occurred_at.desc())
            .limit(limit)
        ).all()
    )
    actors = load_audit_actor_subscribers(db, events)
    items: list[QuoteActivityItem] = []
    for event in events:
        metadata = event.metadata_ or {}
        actor = resolve_actor_name(event, actors)
        detail = ""
        changes = metadata.get("changes")
        if isinstance(changes, dict) and changes:
            fields = ", ".join(str(key).replace("_", " ") for key in list(changes)[:3])
            detail = f" · {fields}"
        recipient = str(metadata.get("recipient_masked") or "").strip()
        if recipient:
            detail = f" · {recipient}"
        tone = (
            "negative"
            if event.action in {"quote.deactivated", "quote.email_suppressed"}
            else "positive"
            if event.action in {"quote.accepted", "quote.email_queued"}
            else "info"
        )
        items.append(
            {
                "title": _ACTION_LABELS.get(
                    event.action, event.action.replace(".", " ").title()
                ),
                "description": f"{actor}{detail}",
                "occurred_at": event.occurred_at,
                "tone": tone,
            }
        )
    return items


def _delivery_items(db: Session, quote_id: UUID, limit: int) -> list[QuoteActivityItem]:
    requests = list(
        db.scalars(
            select(QuoteDeliveryRequest)
            .where(QuoteDeliveryRequest.quote_id == quote_id)
            .options(
                selectinload(QuoteDeliveryRequest.communication_intent),
            )
            .order_by(QuoteDeliveryRequest.created_at.desc())
            .limit(limit)
        ).all()
    )
    items: list[QuoteActivityItem] = []
    for request in requests:
        for notification in request.communication_intent.notifications:
            if notification.status not in {
                NotificationStatus.delivered,
                NotificationStatus.failed,
                NotificationStatus.canceled,
            }:
                continue
            delivered = notification.status == NotificationStatus.delivered
            occurred_at = notification.sent_at or notification.updated_at
            items.append(
                {
                    "title": (
                        "Email delivered" if delivered else "Email delivery failed"
                    ),
                    "description": (
                        "Customer email accepted by the configured mail transport"
                        if delivered
                        else "The delivery queue recorded a terminal failure"
                    ),
                    "occurred_at": occurred_at,
                    "tone": "positive" if delivered else "negative",
                }
            )
    return items


def list_quote_activity(
    db: Session,
    *,
    quote: Quote,
    limit: int = 40,
) -> list[QuoteActivityItem]:
    items = [
        *_audit_items(db, quote.id, limit),
        *_delivery_items(db, quote.id, limit),
    ]
    if not any(item["title"] == "Quote created" for item in items):
        items.append(
            {
                "title": "Quote record created",
                "description": "Creation actor was not recorded by the legacy writer",
                "occurred_at": quote.created_at,
                "tone": "neutral",
            }
        )
    return sorted(items, key=lambda item: item["occurred_at"], reverse=True)[:limit]
