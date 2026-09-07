"""Customer-work lifecycle communication intents.

Domain owners request a named outcome here. Channel selection, recipient
selection, preference/suppression policy, delivery state and transport remain
owned by the communications control plane.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel
from app.models.work_order import WorkOrder
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.communication_intents import CommunicationIntent, submit
from app.services.domain_settings import DomainSettings

DOCUMENT_CHANGE_NOTIFICATION_EVENTS: tuple[str, ...] = (
    "project_status_changed",
    "project_task_status_changed",
    "project_updated",
    "project_task_updated",
    "project_completed",
    "project_task_completed",
    "support_ticket_created_admin",
    "support_ticket_comment_added",
    "support_ticket_status_changed",
    "support_ticket_updated",
    "support_ticket_resolution_confirmation",
    "support_csat_request",
    "work_order_en_route",
    "work_order_arrived",
    "work_order_complete",
    "work_order_unable_to_complete",
)


def document_change_notification_enabled(
    db: Session,
    event_type: str,
    *,
    default: bool = True,
) -> bool:
    """Return whether a document-change customer notification is enabled.

    Shape is owned by the notification-domain setting
    ``document_change_notification_events_enabled``. Operators may store a JSON
    object such as ``{"default": true, "support_ticket_comment_added": false}``
    or a list/CSV of enabled event names.
    """
    configured = settings_spec.resolve_value(
        db,
        SettingDomain.notification,
        "document_change_notification_events_enabled",
    )
    normalized_event = event_type.strip()
    if isinstance(configured, dict):
        if normalized_event in configured:
            return _truthy(configured[normalized_event], default=default)
        if "*" in configured:
            return _truthy(configured["*"], default=default)
        if "default" in configured:
            return _truthy(configured["default"], default=default)
        return default
    if isinstance(configured, (list, tuple, set)):
        return normalized_event in {str(item).strip() for item in configured}
    if isinstance(configured, str):
        text = configured.strip()
        if not text:
            return default
        if text.lower() in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text.lower() in {"0", "false", "no", "off", "disabled"}:
            return False
        return normalized_event in {item.strip() for item in text.split(",")}
    return default


def document_change_notification_policy(db: Session) -> dict[str, bool]:
    return {
        event_type: document_change_notification_enabled(
            db,
            event_type,
            default=not event_type.endswith("_updated"),
        )
        for event_type in DOCUMENT_CHANGE_NOTIFICATION_EVENTS
    }


def set_document_change_notification_policy(
    db: Session,
    enabled_events: dict[str, bool],
) -> dict[str, bool]:
    payload = {
        event_type: bool(enabled_events.get(event_type, False))
        for event_type in DOCUMENT_CHANGE_NOTIFICATION_EVENTS
    }
    spec = settings_spec.get_spec(
        SettingDomain.notification,
        "document_change_notification_events_enabled",
    )
    if spec is None:
        raise RuntimeError("document change notification setting is not registered")
    DomainSettings(SettingDomain.notification).upsert_by_key(
        db,
        "document_change_notification_events_enabled",
        DomainSettingUpdate(
            value_type=spec.value_type,
            value_text=None,
            value_json=payload,
            is_secret=False,
            is_active=True,
        ),
    )
    db.commit()
    return document_change_notification_policy(db)


def _truthy(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def request_update(
    db: Session,
    *,
    subscriber_id: UUID,
    event_type: str,
    subject: str,
    body: str,
    metadata: dict[str, object],
    dedupe_key: str,
    default_channels: Iterable[NotificationChannel],
) -> None:
    if not document_change_notification_enabled(db, event_type):
        return
    submit(
        db,
        CommunicationIntent(
            subscriber_id=subscriber_id,
            event_type=event_type,
            category="service",
            template_code=event_type,
            subject=subject,
            body=body,
            default_channels=tuple(default_channels),
            include_reseller=False,
            persist_policy_suppressions=True,
            metadata={"customer_experience": True, **metadata},
            dedupe_key=dedupe_key,
        ),
    )


def request_field_event(
    db: Session,
    *,
    work_order: WorkOrder,
    event: str,
    field_event_id: UUID,
) -> None:
    messages = {
        "en_route": (
            "Your technician is on the way",
            f"The technician for {work_order.title} is on the way.",
        ),
        "arrived": (
            "Your technician has arrived",
            f"The technician for {work_order.title} has arrived at the site.",
        ),
        "complete": (
            "Technician visit completed",
            f"Field work for {work_order.title} is complete. You can review the visit in self-care.",
        ),
        "unable_to_complete": (
            "Technician visit needs follow-up",
            f"Field work for {work_order.title} could not be completed. The responsible team will review the outcome.",
        ),
    }
    message = messages.get(event)
    if message is None:
        return
    terminal = event in {"complete", "unable_to_complete"}
    request_update(
        db,
        subscriber_id=work_order.subscriber_id,
        event_type=f"work_order_{event}",
        subject=message[0],
        body=message[1],
        metadata={
            "type": "work_order",
            "work_order_id": work_order.public_id,
            "work_order_pk": str(work_order.id),
            "project_id": str(work_order.project_id) if work_order.project_id else None,
            "project_task_id": str(work_order.project_task_id)
            if work_order.project_task_id
            else None,
            "ticket_id": str(work_order.origin_ticket_id)
            if work_order.origin_ticket_id
            else None,
            "field_event_id": str(field_event_id),
        },
        dedupe_key=f"field-event:{field_event_id}",
        default_channels=(
            (
                NotificationChannel.email,
                NotificationChannel.whatsapp,
                NotificationChannel.push,
            )
            if terminal
            else (NotificationChannel.whatsapp, NotificationChannel.push)
        ),
    )
