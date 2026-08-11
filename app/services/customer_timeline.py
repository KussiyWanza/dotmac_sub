"""Typed customer timeline projection for admin customer detail surfaces."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypedDict
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.auth import ApiKey
from app.models.billing import Invoice, Payment, PaymentStatus
from app.models.catalog import Subscription
from app.models.collections import DunningCase
from app.models.communication_log import CommunicationLog
from app.models.provisioning import ServiceOrder
from app.models.subscriber import Subscriber
from app.models.support import Ticket, canonical_ticket_status_value
from app.models.system_user import SystemUser
from app.services.audit_helpers import (
    extract_changes,
    format_changes,
    humanize_action,
    humanize_entity,
    list_audit_events_for_entities,
    load_audit_actor_subscribers,
    resolve_actor_name,
)
from app.services.customer_support_links import ticket_customer_any_link_filter
from app.services.status_presentation import payment_status_presentation


class CustomerTimelineActorKind(StrEnum):
    STAFF = "staff"
    CUSTOMER = "customer"
    SYSTEM = "system"
    SERVICE = "service"
    API_KEY = "api_key"
    UNKNOWN = "unknown_actor"


class CustomerTimelineResult(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    RECORDED = "recorded"


class CustomerTimelineDetail(TypedDict):
    label: str
    value: str


class CustomerTimelineItem(TypedDict):
    key: str
    type: str
    title: str
    action_label: str
    object_label: str
    actor_kind: CustomerTimelineActorKind
    actor_label: str
    actor_name: str
    description: str
    timestamp: datetime | None
    result: CustomerTimelineResult
    amount: float | None
    link: str | None
    security_sensitive: bool
    details: tuple[CustomerTimelineDetail, ...]


_ACTION_LABELS = {
    "customer.pppoe_password_reveal": "Viewed the customer's PPPoE password",
    "customer_user_reset_link": "Sent a customer password reset link",
    "impersonate": "Started customer impersonation",
    "customer.impersonate": "Started customer impersonation",
    "unsuspend": "Removed the account suspension",
    "deactivate": "Deactivated the customer account",
    "status_change": "Changed status",
    "priority_change": "Changed priority",
}

_SECURITY_ACTION_TOKENS = (
    "credential",
    "deactivate",
    "export",
    "impersonat",
    "password",
    "pppoe",
    "reversal",
    "suspend",
)


def _enum_label(value: object) -> str:
    raw_value = getattr(value, "value", value)
    if raw_value is None:
        return ""
    return str(raw_value).replace("_", " ").title()


def _event_timestamp(*values: datetime | None) -> datetime | None:
    return next((value for value in values if value is not None), None)


def _timeline_sort_key(item: CustomerTimelineItem) -> tuple[datetime, str]:
    timestamp = item["timestamp"]
    if not isinstance(timestamp, datetime):
        timestamp = datetime.min.replace(tzinfo=UTC)
    elif timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp, item["key"]


def _looks_like_uuid(value: object) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _readable_service_label(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or _looks_like_uuid(text):
        return None
    for prefix in ("system:", "service:", "task:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.replace("_", " ").replace("-", " ").replace(".", " ").title()


def _actor_projection(
    event: AuditEvent,
    actors: dict[str, object],
) -> tuple[CustomerTimelineActorKind, str]:
    actor_type = event.actor_type.value
    actor_id = str(event.actor_id or "").strip()
    actor_object = actors.get(actor_id)
    resolved_name = str(resolve_actor_name(event, actors) or "").strip()
    stored_label = str(event.actor_label or "").strip()

    if actor_type == AuditActorType.user.value:
        if isinstance(actor_object, SystemUser):
            return CustomerTimelineActorKind.STAFF, resolved_name or "Staff member"
        if isinstance(actor_object, Subscriber):
            return CustomerTimelineActorKind.CUSTOMER, resolved_name or "Customer"
        if resolved_name and not _looks_like_uuid(resolved_name):
            return CustomerTimelineActorKind.UNKNOWN, resolved_name
        return CustomerTimelineActorKind.UNKNOWN, "Unknown Actor"

    if actor_type == AuditActorType.api_key.value:
        if isinstance(actor_object, ApiKey) and resolved_name:
            return CustomerTimelineActorKind.API_KEY, resolved_name
        return (
            CustomerTimelineActorKind.API_KEY,
            (
                resolved_name
                if resolved_name and not _looks_like_uuid(resolved_name)
                else "API Key"
            ),
        )

    if actor_type == AuditActorType.service.value:
        return (
            CustomerTimelineActorKind.SERVICE,
            stored_label
            or _readable_service_label(actor_id)
            or _readable_service_label(resolved_name)
            or "System Service",
        )

    if actor_type == AuditActorType.system.value:
        return (
            CustomerTimelineActorKind.SYSTEM,
            stored_label
            or _readable_service_label(actor_id)
            or _readable_service_label(resolved_name)
            or "System",
        )

    return CustomerTimelineActorKind.UNKNOWN, "Unknown Actor"


def _action_label(action: object) -> str:
    value = str(action or "").strip()
    if not value:
        return "Recorded activity"
    if value in _ACTION_LABELS:
        return _ACTION_LABELS[value]
    return humanize_action(value.replace(".", "_"))


def _is_security_sensitive(action: object) -> bool:
    normalized = str(action or "").lower()
    return any(token in normalized for token in _SECURITY_ACTION_TOKENS)


def _audit_entity_link(entity_type: str | None, entity_id: str | None) -> str | None:
    if not entity_type or not entity_id:
        return None
    if entity_type in {"subscriber", "subscriber_account"}:
        return f"/admin/customers/person/{entity_id}"
    route_prefix = {
        "subscription": "/admin/catalog/subscriptions",
        "invoice": "/admin/billing/invoices",
        "payment": "/admin/billing/payments",
        "support_ticket": "/admin/support/tickets",
        "service_order": "/admin/provisioning/orders",
    }.get(entity_type)
    return f"{route_prefix}/{entity_id}" if route_prefix else None


def _audit_activity_items(
    db: Session,
    entity_refs: Sequence[tuple[str, str]],
    *,
    limit: int,
) -> list[CustomerTimelineItem]:
    events: list[AuditEvent] = list_audit_events_for_entities(
        db,
        list(entity_refs),
        limit=limit,
    )
    if not events:
        return []
    actors = load_audit_actor_subscribers(db, events)
    items: list[CustomerTimelineItem] = []
    for event in events:
        actor_kind, actor_label = _actor_projection(event, actors)
        metadata = event.metadata_ or {}
        action = event.action
        comment_text = str(metadata.get("comment") or "").strip()
        changes = extract_changes(metadata, action)
        change_summary = format_changes(changes, max_items=3) or ""
        description = comment_text or change_summary
        entity_type = str(event.entity_type or "")
        entity_id = str(event.entity_id or "")
        action_label = _action_label(action)
        object_label = humanize_entity(entity_type, entity_id or None)
        is_success = event.is_success
        details: list[CustomerTimelineDetail] = [
            {"label": "Source", "value": "Audit log"},
            {"label": "Result", "value": "Successful" if is_success else "Failed"},
        ]
        if change_summary:
            details.append({"label": "Changes", "value": change_summary})
        request_id = str(event.request_id or "").strip()
        if request_id:
            details.append({"label": "Request ID", "value": request_id})
        items.append(
            {
                "key": f"audit:{event.id}",
                "type": "audit",
                "title": f"{humanize_entity(entity_type)} {humanize_action(action)}",
                "action_label": action_label,
                "object_label": object_label,
                "actor_kind": actor_kind,
                "actor_label": actor_label,
                "actor_name": actor_label,
                "description": description,
                "timestamp": event.occurred_at,
                "result": (
                    CustomerTimelineResult.SUCCESS
                    if is_success
                    else CustomerTimelineResult.FAILED
                ),
                "amount": None,
                "link": _audit_entity_link(entity_type, entity_id or None),
                "security_sensitive": _is_security_sensitive(action),
                "details": tuple(details),
            }
        )
    return items


def _record_item(
    *,
    key: str,
    item_type: str,
    title: str,
    action_label: str,
    object_label: str,
    description: str,
    timestamp: datetime | None,
    amount: float | None = None,
    link: str | None = None,
    result: CustomerTimelineResult = CustomerTimelineResult.RECORDED,
) -> CustomerTimelineItem:
    return {
        "key": key,
        "type": item_type,
        "title": title,
        "action_label": action_label,
        "object_label": object_label,
        "actor_kind": CustomerTimelineActorKind.UNKNOWN,
        "actor_label": "Actor not recorded",
        "actor_name": "Actor not recorded",
        "description": description,
        "timestamp": timestamp,
        "result": result,
        "amount": amount,
        "link": link,
        "security_sensitive": False,
        "details": (
            {"label": "Source", "value": f"{object_label} record"},
            {
                "label": "Attribution",
                "value": "No audit actor is attached to this record activity.",
            },
        ),
    }


def get_customer_audit_activity_items(
    db: Session,
    customer_id: str,
    *,
    limit: int = 5,
) -> list[CustomerTimelineItem]:
    """Return customer-profile audit items through the timeline projection."""
    return _audit_activity_items(
        db,
        (("subscriber", str(customer_id)),),
        limit=limit,
    )


def build_customer_timeline(
    db: Session,
    *,
    customer_id: str,
    account_ids: Sequence[UUID],
    subscriptions: Sequence[Subscription],
    limit: int = 20,
) -> list[CustomerTimelineItem]:
    """Compose recent customer activity without inventing missing actors."""
    items: list[CustomerTimelineItem] = []
    normalized_account_ids = list(dict.fromkeys(account_ids))

    if normalized_account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(normalized_account_ids))
            .filter(Invoice.is_active.is_(True))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .limit(8)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(normalized_account_ids))
            .filter(Payment.is_active.is_(True))
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .limit(8)
            .all()
        )
        tickets = (
            db.query(Ticket)
            .filter(ticket_customer_any_link_filter(Ticket, normalized_account_ids))
            .order_by(Ticket.updated_at.desc())
            .limit(8)
            .all()
        )
        communications = (
            db.query(CommunicationLog)
            .filter(CommunicationLog.subscriber_id.in_(normalized_account_ids))
            .order_by(
                func.coalesce(
                    CommunicationLog.sent_at,
                    CommunicationLog.created_at,
                ).desc()
            )
            .limit(8)
            .all()
        )
        orders = (
            db.query(ServiceOrder)
            .filter(ServiceOrder.subscriber_id.in_(normalized_account_ids))
            .order_by(ServiceOrder.updated_at.desc())
            .limit(8)
            .all()
        )
        dunning_cases = (
            db.query(DunningCase)
            .filter(DunningCase.account_id.in_(normalized_account_ids))
            .order_by(
                func.coalesce(
                    DunningCase.resolved_at,
                    DunningCase.updated_at,
                    DunningCase.started_at,
                ).desc()
            )
            .limit(8)
            .all()
        )
    else:
        invoices = []
        payments = []
        tickets = []
        communications = []
        orders = []
        dunning_cases = []

    for invoice in invoices:
        number = invoice.invoice_number or str(invoice.id)[:8]
        items.append(
            _record_item(
                key=f"invoice:{invoice.id}:record",
                item_type="invoice",
                title=f"Invoice {number}",
                action_label="Recorded invoice activity",
                object_label=f"Invoice {number}",
                description=_enum_label(invoice.status),
                timestamp=_event_timestamp(invoice.issued_at, invoice.created_at),
                amount=float(invoice.total or 0),
                link=f"/admin/billing/invoices/{invoice.id}",
            )
        )

    for payment in payments:
        presentation = payment_status_presentation(payment.status)
        items.append(
            _record_item(
                key=f"payment:{payment.id}:record",
                item_type="payment",
                title="Payment received"
                if payment.status == PaymentStatus.succeeded
                else "Payment update",
                action_label="Recorded payment activity",
                object_label=f"Payment #{str(payment.id)[:8]}",
                description=presentation.label,
                timestamp=_event_timestamp(payment.paid_at, payment.created_at),
                amount=float(payment.amount or 0),
                link=f"/admin/billing/payments/{payment.id}",
                result=(
                    CustomerTimelineResult.FAILED
                    if str(getattr(payment.status, "value", payment.status)).lower()
                    == "failed"
                    else CustomerTimelineResult.RECORDED
                ),
            )
        )

    for subscription in list(subscriptions)[:8]:
        account_label = (
            subscription.login or subscription.ipv4_address or subscription.ipv6_address
        )
        description = _enum_label(subscription.status)
        if account_label:
            description = (
                f"{description} · {account_label}" if description else account_label
            )
        object_label = subscription.offer.name if subscription.offer else "Subscription"
        items.append(
            _record_item(
                key=f"subscription:{subscription.id}:record",
                item_type="subscription",
                title=object_label,
                action_label="Recorded subscription activity",
                object_label=object_label,
                description=description,
                timestamp=_event_timestamp(
                    subscription.updated_at,
                    subscription.next_billing_at,
                    subscription.start_at,
                    subscription.created_at,
                ),
                amount=(
                    float(subscription.unit_price)
                    if subscription.unit_price is not None
                    else None
                ),
                link=f"/admin/catalog/subscriptions/{subscription.id}",
            )
        )

    for ticket in tickets:
        number = ticket.number or str(ticket.id)[:8]
        items.append(
            _record_item(
                key=f"support_ticket:{ticket.id}:record",
                item_type="ticket",
                title=ticket.title or number,
                action_label="Recorded support-ticket activity",
                object_label=f"Ticket {number}",
                description=" · ".join(
                    part
                    for part in (
                        _enum_label(canonical_ticket_status_value(ticket.status)),
                        _enum_label(ticket.priority),
                    )
                    if part
                ),
                timestamp=_event_timestamp(ticket.updated_at, ticket.created_at),
                link=f"/admin/support/tickets/{ticket.id}",
            )
        )

    for log in communications:
        channel = _enum_label(log.channel) or "Communication"
        description = " · ".join(
            part
            for part in (
                channel,
                _enum_label(log.direction),
                _enum_label(log.status),
            )
            if part
        )
        items.append(
            _record_item(
                key=f"communication_log:{log.id}:record",
                item_type="communication",
                title=log.subject or channel,
                action_label="Recorded communication activity",
                object_label=channel,
                description=description,
                timestamp=_event_timestamp(log.sent_at, log.created_at),
                result=(
                    CustomerTimelineResult.FAILED
                    if str(getattr(log.status, "value", log.status)).lower()
                    in {"failed", "bounced"}
                    else CustomerTimelineResult.RECORDED
                ),
            )
        )

    for order in orders:
        object_label = f"{_enum_label(order.order_type) or 'Service'} order"
        items.append(
            _record_item(
                key=f"service_order:{order.id}:record",
                item_type="service_order",
                title=object_label,
                action_label="Recorded service-order activity",
                object_label=object_label,
                description=_enum_label(order.status),
                timestamp=_event_timestamp(order.updated_at, order.created_at),
                link=f"/admin/provisioning/orders/{order.id}",
            )
        )

    for case in dunning_cases:
        description_parts = [_enum_label(case.status)]
        if case.current_step is not None:
            description_parts.append(f"Step {case.current_step}")
        items.append(
            _record_item(
                key=f"dunning_case:{case.id}:record",
                item_type="dunning",
                title="Dunning case",
                action_label="Recorded dunning activity",
                object_label=f"Dunning case #{str(case.id)[:8]}",
                description=" · ".join(part for part in description_parts if part),
                timestamp=_event_timestamp(
                    case.resolved_at,
                    case.updated_at,
                    case.started_at,
                    case.created_at,
                ),
            )
        )

    entity_refs: list[tuple[str, str]] = [
        ("subscriber", str(customer_id)),
        ("subscriber_account", str(customer_id)),
    ]
    entity_refs.extend(("subscription", str(row.id)) for row in subscriptions)
    entity_refs.extend(("invoice", str(row.id)) for row in invoices)
    entity_refs.extend(("payment", str(row.id)) for row in payments)
    entity_refs.extend(("support_ticket", str(row.id)) for row in tickets)
    entity_refs.extend(("communication_log", str(row.id)) for row in communications)
    entity_refs.extend(("service_order", str(row.id)) for row in orders)
    entity_refs.extend(("dunning_case", str(row.id)) for row in dunning_cases)
    items.extend(_audit_activity_items(db, entity_refs, limit=max(limit * 2, 40)))

    unique_items = {item["key"]: item for item in items}
    return sorted(unique_items.values(), key=_timeline_sort_key, reverse=True)[:limit]
