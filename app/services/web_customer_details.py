"""Service helpers for web/admin customer detail pages."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.models.billing import (
    CreditNote,
    CreditNoteApplication,
    CreditNoteStatus,
    Invoice,
    Payment,
    PaymentStatus,
    TaxRate,
)
from app.models.catalog import (
    AccessCredential,
    AddOn,
    ConnectionType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.communication_log import CommunicationLog
from app.models.crm_sync_failure import CrmSyncFailure, CrmSyncFailureStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.enforcement_lock import EnforcementLock
from app.models.gis import (
    CustomerLocationChangeRequest,
    CustomerLocationChangeRequestStatus,
)
from app.models.network import (
    CPEDevice,
    IPAssignment,
    OntAssignment,
    SubscriberAdditionalRoute,
)
from app.models.payment_proof import PaymentProof, PaymentProofStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.service_extension import ServiceExtensionEntry
from app.models.subscriber import (
    ChannelType,
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberChannel,
    SubscriberStatus,
    UserType,
)
from app.models.support import Ticket
from app.schemas.geocoding import GeocodePreviewRequest
from app.services import account_status_commands
from app.services import catalog as catalog_service
from app.services import geocoding as geocoding_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services import subscriber_summary as subscriber_summary_service
from app.services import web_customer_user_access as web_customer_user_access_service
from app.services.access_resolution import resolve_customer_access
from app.services.audit_helpers import (
    extract_changes,
    format_changes,
    humanize_action,
    humanize_entity,
    list_audit_events_for_entities,
    load_audit_actor_subscribers,
    resolve_actor_name,
)
from app.services.billing_settings import resolve_payment_due_days
from app.services.collections import get_available_balance
from app.services.credential_crypto import decrypt_credential
from app.services.customer_support_links import ticket_customer_any_link_filter
from app.services.invoice_collectibility import (
    open_invoice_balance_for_accounts,
    overdue_debt_filters_for_accounts,
)
from app.services.network._common import decode_huawei_hex_serial
from app.services.network.access_path import (
    build_topology_trace,
    summarize_customer_path,
)
from app.services.network.radius_sessions import (
    SubscriptionSessionSnapshot,
    subscription_session_snapshots,
)
from app.services.nin_matching import mask_nin
from app.services.portal_account_health import build_portal_account_health
from app.services.status_presentation import (
    access_session_status_presentation,
    account_status_presentation,
    invoice_status_presentation,
    payment_status_presentation,
    subscription_status_presentation,
)
from app.services.subscription_lifecycle import is_subscription_restore_candidate
from app.services.subscription_lifecycle_policy import (
    is_customer_impact_service_status,
    is_mrr_countable_service_status,
)
from app.services.topology.customer_path import resolve_customer_path

logger = logging.getLogger(__name__)

RESOLVED_TICKET_STATUSES = {
    "resolved",
    "closed",
    "canceled",
    "merged",
}
ACTIVE_SERVICE_ORDER_STATUSES = {
    ServiceOrderStatus.submitted,
    ServiceOrderStatus.scheduled,
    ServiceOrderStatus.provisioning,
}


def _display_ont_serial(value: object) -> str:
    serial = str(value or "").strip()
    if not serial:
        return ""
    return decode_huawei_hex_serial(serial) or serial


def _ont_display_serial_by_id(assignments: list[OntAssignment]) -> dict[str, str]:
    display_by_id: dict[str, str] = {}
    for assignment in assignments:
        ont = getattr(assignment, "ont_unit", None)
        if ont is None:
            continue
        display = _display_ont_serial(getattr(ont, "vendor_serial_number", None))
        if not display:
            display = _display_ont_serial(getattr(ont, "serial_number", None))
        if display:
            display_by_id[str(ont.id)] = display
    return display_by_id


def _coerce_setting_int(value: object | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _coerce_setting_decimal(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _format_billing_value(key: str, value: object | None) -> str:
    if value is None:
        return "Not set"
    if key == "billing_enabled":
        return "Approved" if bool(value) else "Administratively disabled"
    if key in {"billing_day", "payment_due_days", "grace_period_days"}:
        if key == "billing_day":
            return f"Day {value}"
        if key == "payment_due_days":
            return f"{value} day(s)"
        return f"{value} day(s)"
    if key == "min_balance":
        try:
            return f"NGN {Decimal(str(value)):,.2f}"
        except Exception:
            return str(value)
    return str(value)


def _billing_global_defaults(db: Session) -> dict[str, object | None]:
    keys = {"billing_day", "minimum_balance"}
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.billing)
        .filter(DomainSetting.key.in_(keys))
        .all()
    )
    raw = {
        row.key: row.value_json if row.value_json is not None else row.value_text
        for row in rows
    }
    return {
        "billing_day": _coerce_setting_int(raw.get("billing_day")),
        "payment_due_days": resolve_payment_due_days(db),
        "min_balance": _coerce_setting_decimal(raw.get("minimum_balance")),
    }


def _resolve_tax_labels(db: Session, accounts: list[Subscriber]) -> dict[str, str]:
    tax_ids = {
        account.tax_rate_id
        for account in accounts
        if getattr(account, "tax_rate_id", None)
    }
    if not tax_ids:
        return {}
    rates = db.query(TaxRate).filter(TaxRate.id.in_(tax_ids)).all()
    return {str(rate.id): rate.name for rate in rates}


def _billing_policy_snapshot(
    db: Session, accounts: list[Subscriber]
) -> dict[str, object]:
    global_defaults = _billing_global_defaults(db)
    tax_labels = _resolve_tax_labels(db, accounts)
    fields = [
        ("billing_enabled", "Billing and Service Approval", False),
        ("billing_day", "Billing Day", True),
        ("payment_due_days", "Payment Due", True),
        ("grace_period_days", "Grace Period", False),
        ("min_balance", "Minimum Balance", True),
        ("tax_rate", "Tax Rate", False),
    ]
    rows: list[dict[str, object]] = []
    has_overrides = False
    has_mixed = False

    for key, label, uses_global in fields:
        override: object | None
        raw_values: list[object | None] = []
        effective_values: list[object | None] = []
        for account in accounts:
            if key == "tax_rate":
                raw = (
                    tax_labels.get(str(account.tax_rate_id))
                    if getattr(account, "tax_rate_id", None)
                    else None
                )
            else:
                raw = getattr(account, key, None)
            raw_values.append(raw)
            effective_values.append(
                raw
                if raw is not None
                else (global_defaults.get(key) if uses_global else None)
            )

        unique_effective = {
            str(value) for value in effective_values if value is not None
        }
        unique_raw = {str(value) for value in raw_values if value is not None}
        if not accounts:
            source = "Global default" if uses_global else "Not set"
            effective = global_defaults.get(key) if uses_global else None
            override = None
        elif len(unique_effective) > 1 or (len(unique_raw) > 1):
            source = "Mixed"
            effective = "Mixed"
            override = "Mixed"
            has_mixed = True
        else:
            resolved_raw: object | None = next(
                (value for value in raw_values if value is not None),
                None,
            )
            effective = effective_values[0] if effective_values else None
            override = resolved_raw
            if resolved_raw is None:
                source = "Global default" if uses_global else "Not set"
            else:
                source = "Customer override"
                has_overrides = True

        rows.append(
            {
                "key": key,
                "label": label,
                "effective": effective
                if effective == "Mixed"
                else _format_billing_value(key, effective),
                "source": source,
                "global": _format_billing_value(key, global_defaults.get(key))
                if uses_global
                else "Not set",
                "override": override
                if override == "Mixed"
                else _format_billing_value(key, override),
            }
        )

    return {
        "rows": rows,
        "has_overrides": has_overrides,
        "has_mixed": has_mixed,
        "account_count": len(accounts),
    }


def _dedupe_accounts(accounts):
    unique = {}
    for account in accounts:
        unique[str(account.id)] = account
    return list(unique.values())


def _list_subscriptions_for_accounts(db: Session, accounts):
    if not accounts:
        return []
    subscriptions = []
    for account in accounts:
        try:
            account_subs = catalog_service.subscriptions.list(
                db=db,
                subscriber_id=str(account.id),
                offer_id=None,
                status=None,
                order_by="created_at",
                order_dir="desc",
                limit=200,
                offset=0,
            )
            subscriptions.extend(account_subs)
        except Exception:
            logger.debug(
                "Skipping subscriptions for linked account %s",
                account.id,
                exc_info=True,
            )
            continue
    return subscriptions


def _format_contact_channel(
    subscriber: Subscriber, channel: SubscriberChannel
) -> dict[str, object]:
    return {
        "id": str(channel.id),
        "first_name": subscriber.first_name or "",
        "last_name": subscriber.last_name or "",
        "role": None,
        "title": channel.label,
        "is_primary": channel.is_primary,
        "email": channel.address if channel.channel_type == ChannelType.email else "",
        "phone": channel.address if channel.channel_type == ChannelType.phone else "",
    }


def _enum_label(value: object) -> str:
    raw_value = getattr(value, "value", value)
    if raw_value is None:
        return ""
    return str(raw_value).replace("_", " ").title()


def _event_timestamp(*values: datetime | None) -> datetime | None:
    for value in values:
        if value is not None:
            return value
    return None


def _timeline_sort_key(item: dict[str, object]) -> datetime:
    timestamp = item.get("timestamp")
    if isinstance(timestamp, datetime):
        return timestamp
    return datetime.min.replace(tzinfo=UTC)


def _audit_entity_link(entity_type: str | None, entity_id: str | None) -> str | None:
    if not entity_type or not entity_id:
        return None
    route_prefix = {
        "subscription": "/admin/catalog/subscriptions",
        "invoice": "/admin/billing/invoices",
        "payment": "/admin/billing/payments",
        "support_ticket": "/admin/support/tickets",
        "service_order": "/admin/provisioning/orders",
    }.get(entity_type)
    if not route_prefix:
        return None
    return f"{route_prefix}/{entity_id}"


def _build_audit_activity_items(
    db: Session,
    entity_refs: list[tuple[str, str]],
    limit: int = 16,
) -> list[dict[str, object]]:
    audit_events = list_audit_events_for_entities(db, entity_refs, limit=limit)
    if not audit_events:
        return []
    people = load_audit_actor_subscribers(db, audit_events)
    items: list[dict[str, object]] = []
    for event in audit_events:
        actor_name = resolve_actor_name(event, people)
        metadata = getattr(event, "metadata_", None) or {}
        comment_text = str(metadata.get("comment") or "").strip()
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        description = ""
        if comment_text:
            description = comment_text
        elif change_summary:
            description = change_summary
        items.append(
            {
                "type": "audit",
                "title": (
                    f"{humanize_entity(getattr(event, 'entity_type', None))} "
                    f"{humanize_action(getattr(event, 'action', None))}"
                ),
                "actor_name": actor_name,
                "description": description,
                "timestamp": getattr(event, "occurred_at", None),
                "link": _audit_entity_link(
                    getattr(event, "entity_type", None),
                    getattr(event, "entity_id", None),
                ),
            }
        )
    return items


def get_customer_audit_activity_items(
    db: Session, customer_id: str, limit: int = 5
) -> list[dict[str, object]]:
    """Return recent structured audit activity for a customer edit surface."""
    return _build_audit_activity_items(
        db,
        [("subscriber", str(customer_id))],
        limit=limit,
    )


def _build_activity_items(
    db: Session,
    entity_type: str,
    entity_id: str,
    account_ids: list[UUID],
    subscriptions: list[Subscription],
) -> list[dict[str, object]]:
    activity_items: list[dict[str, object]] = []

    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.is_active.is_(True))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .limit(8)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.is_active.is_(True))
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .limit(8)
            .all()
        )
        support_tickets = (
            db.query(Ticket)
            .filter(ticket_customer_any_link_filter(Ticket, account_ids))
            .order_by(Ticket.updated_at.desc())
            .limit(8)
            .all()
        )
        communication_logs = (
            db.query(CommunicationLog)
            .filter(CommunicationLog.subscriber_id.in_(account_ids))
            .order_by(
                func.coalesce(
                    CommunicationLog.sent_at, CommunicationLog.created_at
                ).desc()
            )
            .limit(8)
            .all()
        )
        service_orders = (
            db.query(ServiceOrder)
            .filter(ServiceOrder.subscriber_id.in_(account_ids))
            .order_by(ServiceOrder.updated_at.desc())
            .limit(8)
            .all()
        )
        dunning_cases = (
            db.query(DunningCase)
            .filter(DunningCase.account_id.in_(account_ids))
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
        support_tickets = []
        communication_logs = []
        service_orders = []
        dunning_cases = []

    for invoice in invoices:
        amount = invoice.total if invoice.total is not None else 0
        activity_items.append(
            {
                "type": "invoice",
                "title": f"Invoice {invoice.invoice_number or 'created'}",
                "description": _enum_label(invoice.status),
                "timestamp": _event_timestamp(invoice.issued_at, invoice.created_at),
                "amount": float(amount),
                "link": f"/admin/billing/invoices/{invoice.id}",
            }
        )

    for payment in payments:
        presentation = payment_status_presentation(payment.status)
        activity_items.append(
            {
                "type": "payment",
                "title": "Payment received"
                if payment.status == PaymentStatus.succeeded
                else "Payment update",
                "description": presentation.label,
                "timestamp": _event_timestamp(payment.paid_at, payment.created_at),
                "amount": float(payment.amount or 0),
            }
        )

    for subscription in subscriptions[:8]:
        account_label = (
            subscription.login or subscription.ipv4_address or subscription.ipv6_address
        )
        description = _enum_label(subscription.status)
        if account_label:
            description = (
                f"{description} · {account_label}" if description else account_label
            )
        activity_items.append(
            {
                "type": "subscription",
                "title": subscription.offer.name
                if subscription.offer
                else "Subscription updated",
                "description": description,
                "timestamp": _event_timestamp(
                    subscription.updated_at,
                    subscription.next_billing_at,
                    subscription.start_at,
                    subscription.created_at,
                ),
                "amount": float(subscription.unit_price or 0)
                if subscription.unit_price is not None
                else None,
                "link": f"/admin/catalog/subscriptions/{subscription.id}",
            }
        )

    for ticket in support_tickets:
        description = " · ".join(
            part
            for part in (_enum_label(ticket.status), _enum_label(ticket.priority))
            if part
        )
        activity_items.append(
            {
                "type": "ticket",
                "title": ticket.title or ticket.number or "Support ticket",
                "description": description,
                "timestamp": _event_timestamp(ticket.updated_at, ticket.created_at),
                "link": f"/admin/support/tickets/{ticket.id}",
            }
        )

    for log in communication_logs:
        subject = (
            log.subject
            or log.recipient
            or log.sender
            or _enum_label(log.channel)
            or "Communication"
        )
        description = " · ".join(
            part
            for part in (
                _enum_label(log.channel),
                _enum_label(log.direction),
                _enum_label(log.status),
            )
            if part
        )
        activity_items.append(
            {
                "type": "communication",
                "title": subject,
                "description": description,
                "timestamp": _event_timestamp(log.sent_at, log.created_at),
            }
        )

    for order in service_orders:
        title = f"{_enum_label(order.order_type) or 'Service'} order"
        description = _enum_label(order.status)
        activity_items.append(
            {
                "type": "service_order",
                "title": title,
                "description": description,
                "timestamp": _event_timestamp(order.updated_at, order.created_at),
                "link": f"/admin/provisioning/orders/{order.id}",
            }
        )

    for case in dunning_cases:
        description_parts = [_enum_label(case.status)]
        if case.current_step is not None:
            description_parts.append(f"Step {case.current_step}")
        activity_items.append(
            {
                "type": "dunning",
                "title": "Dunning case",
                "description": " · ".join(part for part in description_parts if part),
                "timestamp": _event_timestamp(
                    case.resolved_at, case.updated_at, case.started_at, case.created_at
                ),
            }
        )

    entity_refs = [(entity_type, entity_id)]
    entity_refs.extend(
        ("subscription", str(subscription.id)) for subscription in subscriptions[:8]
    )
    entity_refs.extend(("invoice", str(invoice.id)) for invoice in invoices)
    entity_refs.extend(("payment", str(payment.id)) for payment in payments)
    entity_refs.extend(("support_ticket", str(ticket.id)) for ticket in support_tickets)
    entity_refs.extend(("service_order", str(order.id)) for order in service_orders)
    activity_items.extend(_build_audit_activity_items(db, entity_refs))

    activity_items.sort(key=_timeline_sort_key, reverse=True)
    return activity_items[:20]


def _build_common_financials(db: Session, account_ids):
    invoices = []
    payments = []
    if account_ids:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.is_active.is_(True))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .limit(10)
            .all()
        )
        payments = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.is_active.is_(True))
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .limit(10)
            .all()
        )

    balance_due = float(open_invoice_balance_for_accounts(db, account_ids or []))

    current_balance = Decimal("0.00")
    for account_id in account_ids or []:
        try:
            current_balance += get_available_balance(db, str(account_id))
        except Exception:
            logger.warning(
                "Failed to resolve current balance for customer account %s",
                account_id,
                exc_info=True,
            )

    total_invoiced = 0
    total_paid = 0
    overdue_invoices = 0
    last_payment = None
    last_invoice = None
    if account_ids:
        total_invoiced = (
            db.query(func.coalesce(func.sum(Invoice.total), 0))
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.is_active.is_(True))
            .scalar()
            or 0
        )
        total_paid = (
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.is_active.is_(True))
            .filter(Payment.status == PaymentStatus.succeeded)
            .scalar()
            or 0
        )
        overdue_invoices = (
            db.query(func.count(Invoice.id))
            .filter(*overdue_debt_filters_for_accounts(account_ids))
            .scalar()
            or 0
        )
        last_payment = (
            db.query(Payment)
            .filter(Payment.account_id.in_(account_ids))
            .filter(Payment.is_active.is_(True))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .first()
        )
        last_invoice = (
            db.query(Invoice)
            .filter(Invoice.account_id.in_(account_ids))
            .filter(Invoice.is_active.is_(True))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .first()
        )

    return {
        "invoices": invoices,
        "invoice_status_presentations": {
            str(invoice.id): invoice_status_presentation(invoice.status)
            for invoice in invoices
        },
        "payments": payments,
        "payment_status_presentations": {
            str(payment.id): payment_status_presentation(payment.status)
            for payment in payments
        },
        "balance_due": balance_due,
        "financials": {
            "current_balance": current_balance,
            "total_invoiced": total_invoiced,
            "total_paid": total_paid,
            "overdue_invoices": overdue_invoices,
            "last_payment": last_payment,
            "last_invoice": last_invoice,
        },
    }


def _build_admin_billing_workspace(
    db: Session, account_ids: list[UUID]
) -> dict[str, object]:
    if not account_ids:
        return {
            "payment_proofs": [],
            "credit_notes": [],
            "credit_applications": [],
            "service_extensions": [],
            "billing_workspace_counts": {
                "pending_payment_proofs": 0,
                "open_credit_notes": 0,
                "open_credit_amount": Decimal("0.00"),
                "service_extensions": 0,
            },
        }

    payment_proofs = (
        db.query(PaymentProof)
        .filter(PaymentProof.account_id.in_(account_ids))
        .order_by(PaymentProof.created_at.desc())
        .limit(10)
        .all()
    )
    credit_notes = (
        db.query(CreditNote)
        .options(selectinload(CreditNote.applications))
        .filter(CreditNote.account_id.in_(account_ids))
        .filter(CreditNote.is_active.is_(True))
        .order_by(CreditNote.created_at.desc())
        .limit(10)
        .all()
    )
    credit_applications = (
        db.query(CreditNoteApplication)
        .options(
            selectinload(CreditNoteApplication.credit_note),
            selectinload(CreditNoteApplication.invoice),
        )
        .join(CreditNote, CreditNoteApplication.credit_note_id == CreditNote.id)
        .filter(CreditNote.account_id.in_(account_ids))
        .order_by(CreditNoteApplication.created_at.desc())
        .limit(10)
        .all()
    )
    service_extensions = (
        db.query(ServiceExtensionEntry)
        .options(selectinload(ServiceExtensionEntry.extension))
        .filter(ServiceExtensionEntry.subscriber_id.in_(account_ids))
        .order_by(ServiceExtensionEntry.created_at.desc())
        .limit(10)
        .all()
    )

    open_credit_notes = [
        note
        for note in credit_notes
        if note.status in (CreditNoteStatus.issued, CreditNoteStatus.partially_applied)
        and (note.total or Decimal("0.00")) > (note.applied_total or Decimal("0.00"))
    ]
    open_credit_amount = sum(
        (note.total or Decimal("0.00")) - (note.applied_total or Decimal("0.00"))
        for note in open_credit_notes
    )

    return {
        "payment_proofs": payment_proofs,
        "credit_notes": credit_notes,
        "credit_applications": credit_applications,
        "service_extensions": service_extensions,
        "billing_workspace_counts": {
            "pending_payment_proofs": sum(
                1
                for proof in payment_proofs
                if proof.status == PaymentProofStatus.submitted
            ),
            "open_credit_notes": len(open_credit_notes),
            "open_credit_amount": open_credit_amount,
            "service_extensions": len(service_extensions),
        },
    }


def _build_relationship_data(db: Session, account_ids: list[UUID]) -> dict[str, object]:
    if not account_ids:
        empty_summary: dict[str, int] = {
            "open_tickets": 0,
            "recent_communications": 0,
            "active_service_orders": 0,
            "open_dunning_cases": 0,
            "active_credentials": 0,
            "active_cpes": 0,
            "active_onts": 0,
            "active_ip_assignments": 0,
            "linked_resellers": 0,
        }
        return {
            "support_tickets": [],
            "communication_logs": [],
            "service_orders": [],
            "dunning_cases": [],
            "access_credentials": [],
            "cpe_devices": [],
            "ip_assignments": [],
            "active_additional_routes": [],
            "active_additional_route_rows": [],
            "ont_assignments": [],
            "linked_resellers": [],
            "relationship_summary": empty_summary,
        }

    support_tickets = (
        db.query(Ticket)
        .filter(ticket_customer_any_link_filter(Ticket, account_ids))
        .order_by(Ticket.updated_at.desc())
        .limit(10)
        .all()
    )
    communication_logs = (
        db.query(CommunicationLog)
        .filter(CommunicationLog.subscriber_id.in_(account_ids))
        .order_by(
            func.coalesce(CommunicationLog.sent_at, CommunicationLog.created_at).desc()
        )
        .limit(12)
        .all()
    )
    service_orders = (
        db.query(ServiceOrder)
        .filter(ServiceOrder.subscriber_id.in_(account_ids))
        .order_by(ServiceOrder.updated_at.desc())
        .limit(10)
        .all()
    )
    dunning_cases = (
        db.query(DunningCase)
        .filter(DunningCase.account_id.in_(account_ids))
        .order_by(DunningCase.updated_at.desc())
        .limit(10)
        .all()
    )
    access_credentials = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id.in_(account_ids))
        .order_by(AccessCredential.updated_at.desc())
        .limit(10)
        .all()
    )
    cpe_devices = (
        db.query(CPEDevice)
        .filter(CPEDevice.subscriber_id.in_(account_ids))
        .order_by(CPEDevice.updated_at.desc())
        .limit(10)
        .all()
    )
    ip_assignments = (
        db.query(IPAssignment)
        .options(
            selectinload(IPAssignment.ipv4_address),
            selectinload(IPAssignment.ipv6_address),
        )
        .filter(IPAssignment.subscriber_id.in_(account_ids))
        .order_by(IPAssignment.updated_at.desc())
        .limit(10)
        .all()
    )
    active_additional_routes = (
        db.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id.in_(account_ids))
        .filter(SubscriberAdditionalRoute.is_active.is_(True))
        .order_by(SubscriberAdditionalRoute.cidr.asc())
        .all()
    )
    active_public_ip_addons = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .join(Subscription, Subscription.id == SubscriptionAddOn.subscription_id)
        .filter(Subscription.subscriber_id.in_(account_ids))
        .filter(SubscriptionAddOn.end_at.is_(None))
        .filter(AddOn.ip_is_public.is_(True))
        .all()
    )
    addon_qty_by_prefix: dict[int, int] = {}
    for sub_addon, add_on in active_public_ip_addons:
        if add_on.ip_prefix_length is None:
            continue
        prefix = int(add_on.ip_prefix_length)
        addon_qty_by_prefix[prefix] = addon_qty_by_prefix.get(prefix, 0) + int(
            sub_addon.quantity or 0
        )
    route_counts_by_prefix: dict[int, int] = {}
    for route in active_additional_routes:
        prefix = int(route.prefix_length)
        route_counts_by_prefix[prefix] = route_counts_by_prefix.get(prefix, 0) + 1
    active_additional_route_rows = []
    for route in active_additional_routes:
        prefix = int(route.prefix_length)
        qty = addon_qty_by_prefix.get(prefix, 0)
        billing_ok = qty >= route_counts_by_prefix.get(prefix, 0)
        active_additional_route_rows.append(
            {
                "cidr": route.cidr,
                "prefix_length": prefix,
                "type_label": "Additional IP" if prefix == 32 else "Routed IP Block",
                "billing_ok": billing_ok,
                "billing_label": f"Billed /{prefix} IP x{qty}"
                if billing_ok
                else "Billing add-on missing",
            }
        )
    ont_assignments = (
        db.query(OntAssignment)
        .options(
            selectinload(OntAssignment.ont_unit),
            selectinload(OntAssignment.pon_port),
        )
        .filter(OntAssignment.subscriber_id.in_(account_ids))
        .order_by(OntAssignment.updated_at.desc())
        .limit(10)
        .all()
    )

    reseller_ids = {
        account.reseller_id
        for account in db.query(Subscriber)
        .options(selectinload(Subscriber.reseller))
        .filter(Subscriber.id.in_(account_ids))
        .all()
        if getattr(account, "reseller_id", None)
    }
    linked_resellers = (
        db.query(Reseller)
        .filter(Reseller.id.in_(list(reseller_ids)))
        .order_by(Reseller.name.asc())
        .all()
        if reseller_ids
        else []
    )

    relationship_summary: dict[str, int] = {
        "open_tickets": sum(
            1
            for ticket in support_tickets
            if ticket.status not in RESOLVED_TICKET_STATUSES
        ),
        "recent_communications": len(communication_logs),
        "active_service_orders": sum(
            1
            for order in service_orders
            if order.status in ACTIVE_SERVICE_ORDER_STATUSES
        ),
        "open_dunning_cases": sum(
            1
            for case in dunning_cases
            if case.status in {DunningCaseStatus.open, DunningCaseStatus.paused}
        ),
        "active_credentials": sum(
            1 for credential in access_credentials if credential.is_active
        ),
        "active_cpes": sum(
            1
            for cpe in cpe_devices
            if str(getattr(cpe, "status", "")) == "DeviceStatus.active"
            or getattr(getattr(cpe, "status", None), "value", None) == "active"
        ),
        "active_onts": sum(1 for assignment in ont_assignments if assignment.active),
        "active_ip_assignments": sum(
            1 for assignment in ip_assignments if assignment.is_active
        ),
        "linked_resellers": len(linked_resellers),
    }

    return {
        "support_tickets": support_tickets,
        "communication_logs": communication_logs,
        "service_orders": service_orders,
        "dunning_cases": dunning_cases,
        "access_credentials": access_credentials,
        "cpe_devices": cpe_devices,
        "ip_assignments": ip_assignments,
        "active_additional_routes": active_additional_routes,
        "active_additional_route_rows": active_additional_route_rows,
        "ont_assignments": ont_assignments,
        "ont_serial_display_by_id": _ont_display_serial_by_id(ont_assignments),
        "linked_resellers": linked_resellers,
        "relationship_summary": relationship_summary,
    }


def _build_person_fallback_address(db: Session, customer: Subscriber):
    if (customer.address_line1 or "").strip() == "":
        return []

    customer_meta = getattr(customer, "metadata_", None) or {}
    customer_lat = getattr(customer, "latitude", None)
    customer_lng = getattr(customer, "longitude", None)

    def _clean_value(value):
        if isinstance(value, str):
            trimmed = value.strip()
            return None if not trimmed or trimmed.lower() == "none" else trimmed
        return value

    address_line1 = (customer.address_line1 or "").strip()
    address_line2 = _clean_value(getattr(customer, "address_line2", None))
    city = _clean_value(getattr(customer, "city", None))
    region = _clean_value(getattr(customer, "region", None))
    postal_code = _clean_value(getattr(customer, "postal_code", None))
    country_code = _clean_value(getattr(customer, "country_code", None))
    if customer_lat is None:
        customer_lat = customer_meta.get("latitude")
    if customer_lng is None:
        customer_lng = customer_meta.get("longitude")

    if customer_lat is None or customer_lng is None:
        try:
            payload = GeocodePreviewRequest(
                address_line1=address_line1,
                address_line2=address_line2,
                city=city,
                region=region,
                postal_code=postal_code,
                country_code=country_code,
                limit=1,
            )
            results = geocoding_service.geocode_preview_from_request(db, payload)
            if results:
                first = results[0] or {}
                lat_value = first.get("latitude")
                lng_value = first.get("longitude")
                if lat_value is not None and lng_value is not None:
                    customer_lat = float(lat_value)
                    customer_lng = float(lng_value)
                    if getattr(customer, "metadata_", None) is None:
                        customer.metadata_ = {}
                    if isinstance(customer.metadata_, dict):
                        customer.metadata_["latitude"] = customer_lat
                        customer.metadata_["longitude"] = customer_lng
                        try:
                            db.add(customer)
                            db.commit()
                        except Exception:
                            db.rollback()
        except Exception:
            logger.debug(
                "Fallback geocoding preview failed for customer %s",
                customer.id,
                exc_info=True,
            )

    return [
        SimpleNamespace(
            id=None,
            is_primary=True,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            region=region,
            postal_code=postal_code,
            country_code=country_code,
            latitude=customer_lat,
            longitude=customer_lng,
            created_at=None,
        )
    ]


def _build_map_payload(primary_address, customer_name: str):
    map_data = None
    geocode_target = None
    if primary_address and (primary_address.address_line1 or "").strip():
        if (
            getattr(primary_address, "latitude", None) is not None
            and getattr(primary_address, "longitude", None) is not None
        ):
            map_data = {
                "center": [primary_address.latitude, primary_address.longitude],
                "geojson": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [
                                    primary_address.longitude,
                                    primary_address.latitude,
                                ],
                            },
                            "properties": {
                                "type": "customer",
                                "name": customer_name,
                                "address": primary_address.address_line1,
                            },
                        }
                    ],
                },
            }
        else:
            target_id = getattr(primary_address, "id", None)
            geocode_target = {
                "id": str(target_id) if target_id is not None else None,
                "address_line1": primary_address.address_line1,
                "address_line2": primary_address.address_line2,
                "city": primary_address.city,
                "region": primary_address.region,
                "postal_code": primary_address.postal_code,
                "country_code": primary_address.country_code,
                "payload": {
                    "address_line1": primary_address.address_line1,
                    "address_line2": primary_address.address_line2 or "",
                    "city": primary_address.city or "",
                    "region": primary_address.region or "",
                    "postal_code": primary_address.postal_code or "",
                    "country_code": primary_address.country_code or "",
                },
            }
            geocode_target["payload_json"] = json.dumps(geocode_target["payload"])
    return map_data, geocode_target


def _connection_status_for_session(
    subscription: Subscription,
    session: SubscriptionSessionSnapshot,
) -> dict[str, object]:
    state = session.state.value
    presentation = access_session_status_presentation(state)
    detail = {
        "connected": "Canonical live RADIUS session",
        "stale": "Live session mirror has stale updates",
        "offline": "No trustworthy live RADIUS session",
        "inactive": "Service is not active",
    }[state]
    return {
        "state": state,
        "label": presentation.label,
        "detail": detail,
        "last_seen_at": session.observed_at,
        "identifier": session.framed_ip_address or session.acct_session_id,
        "status_presentation": presentation.model_dump(mode="json"),
    }


def _build_network_connection_snapshot(
    db: Session, subscriptions: list[Subscription]
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    sessions_by_sub = subscription_session_snapshots(db, subscriptions)

    by_subscription: dict[str, dict[str, object]] = {}
    for sub in subscriptions:
        by_subscription[str(sub.id)] = _connection_status_for_session(
            sub,
            sessions_by_sub[sub.id],
        )

    connected = [
        status for status in by_subscription.values() if status["state"] == "connected"
    ]
    stale = [
        status for status in by_subscription.values() if status["state"] == "stale"
    ]
    active_count = sum(
        1 for sub in subscriptions if is_customer_impact_service_status(sub.status)
    )
    if connected:
        label = "Connected"
        state = "connected"
        detail = f"{len(connected)} active service session"
        if len(connected) != 1:
            detail += "s"
    elif stale:
        label = "Last seen"
        state = "stale"
        detail = "Open session has stale accounting updates"
    else:
        label = "Not connected"
        state = "offline"
        detail = "No open RADIUS accounting session"

    return (
        {
            "state": state,
            "label": label,
            "detail": detail,
            "status_presentation": access_session_status_presentation(state).model_dump(
                mode="json"
            ),
            "connected_count": len(connected),
            "active_count": active_count,
        },
        by_subscription,
    )


def _active_additional_routes_by_subscriber(
    db: Session, account_ids: list[UUID]
) -> dict[UUID, list[dict[str, object]]]:
    if not account_ids:
        return {}
    rows = (
        db.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id.in_(account_ids))
        .filter(SubscriberAdditionalRoute.is_active.is_(True))
        .order_by(SubscriberAdditionalRoute.cidr.asc())
        .all()
    )
    routes_by_subscriber: dict[UUID, list[dict[str, object]]] = {}
    for route in rows:
        routes_by_subscriber.setdefault(route.subscriber_id, []).append(
            {
                "cidr": route.cidr,
                "metric": route.metric,
            }
        )
    return routes_by_subscriber


def _build_access_endpoint_projection(
    db: Session, subscription, path=None
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Serving endpoint + active-path trace for one subscription.

    Accepts an already-resolved path so the card, the trace and the diagnosis
    share one resolution instead of paying for it each. Failures degrade to an
    unresolved endpoint rather than breaking the page: an unavailable trace
    must not take the customer record with it.
    """

    if path is None:
        try:
            path = resolve_customer_path(db, subscription)
        except Exception:
            logger.warning(
                "Access path resolution failed for subscription %s",
                getattr(subscription, "id", None),
                exc_info=True,
            )
            return ({"endpoint_source": "unresolved"}, None)

    summary = summarize_customer_path(subscription, path)
    trace = build_topology_trace(subscription, path)
    return (
        {
            "endpoint_display": summary.endpoint_display,
            "endpoint_source": summary.endpoint_source,
            "access_device_name": summary.access_device_name,
            "access_device_id": str(summary.access_device_id)
            if summary.access_device_id
            else None,
            "access_kind": summary.access_kind,
            "pon_port_label": summary.pon_port_label,
            "ont_serial": summary.ont_serial,
            "radio_label": summary.radio_label,
            "basestation_name": summary.basestation_name,
            "gap": summary.gap,
            "endpoint_complete": summary.endpoint_complete,
        },
        trace.to_dict(),
    )


def _build_access_state_facts(subscription) -> dict[str, object] | None:
    """Why RADIUS is allowing or blocking this service, from the canonical owner.

    Reports what access_resolution decided, with its own reason strings. It
    deliberately does not infer a cause of its own: an agent reading the state
    alongside the path draws a better conclusion than a guess would.
    """

    try:
        decision = resolve_customer_access(subscription)
    except Exception:
        logger.warning(
            "Access resolution failed for subscription %s",
            getattr(subscription, "id", None),
            exc_info=True,
        )
        return None

    state = decision.radius_access_state
    return {
        "radius_state": getattr(state, "value", None),
        "radius_allowed": decision.radius_allowed,
        "radius_blocked": decision.radius_blocked,
        "radius_mode": decision.radius_mode,
        "access_block_reason": decision.access_block_reason,
        "billing_block_reason": decision.billing_block_reason,
        "source": "access.subscription_lifecycle",
    }


def _build_known_incident(db: Session, subscription) -> dict[str, object] | None:
    """A detected incident already covering this customer, for the agent.

    ``customer_service_state`` computes this for the portal, and its
    ``support_context`` docstring names the subscriber detail page as a
    consumer, but nothing wired it up. The result was that the *customer* was
    told "we know, engineers are on it" while the *agent* looking at the same
    account saw nothing, and asked the channel.

    Returns None when there is no incident, so the panel stays absent rather
    than rendering an empty "no known issues" box on every customer.
    """

    from app.services.customer_service_state import get_customer_service_state

    try:
        state = get_customer_service_state(db, subscription)
    except Exception:
        logger.warning(
            "Service state resolution failed for subscription %s",
            getattr(subscription, "id", None),
            exc_info=True,
        )
        return None

    if not (
        state.area_outage
        or state.active_outage_id
        or state.open_infrastructure_ticket_id
    ):
        return None

    return {
        "area_outage": state.area_outage,
        "connection_state": state.connection_state,
        "incident_id": str(state.active_outage_id) if state.active_outage_id else None,
        "infrastructure_ticket_id": (
            str(state.open_infrastructure_ticket_id)
            if state.open_infrastructure_ticket_id
            else None
        ),
        "customer_message": state.customer_message,
        "checked_at": state.checked_at.isoformat() if state.checked_at else None,
    }


def _ticket_prefill_url(subscription, card: dict[str, object]) -> str:
    """Pre-filled ticket URL carrying the observed state as evidence.

    The agent chooses the ticket type. Sub does not guess it: a wrong type is
    worse than an empty one, and the create form already validates the choice.
    """

    endpoint = cast(dict, card.get("access_endpoint") or {})
    access_state = cast(dict, card.get("access_state") or {})
    connection = cast(dict, card.get("connection_status") or {})
    lines: list[str] = []
    if endpoint.get("endpoint_display"):
        lines.append(
            f"Serving endpoint: {endpoint['endpoint_display']} "
            f"({endpoint.get('endpoint_source')})"
        )
    elif endpoint.get("endpoint_source") == "unresolved":
        lines.append(
            f"Serving endpoint: unresolved ({endpoint.get('gap') or 'no path'})"
        )
    if connection.get("label"):
        lines.append(f"Session: {connection['label']} - {connection.get('detail')}")
    if access_state.get("radius_state"):
        lines.append(f"RADIUS access: {access_state['radius_state']}")
    for key in ("access_block_reason", "billing_block_reason"):
        if access_state.get(key):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {access_state[key]}")
    trace = cast(dict, card.get("topology_trace") or {})
    for node in cast(list, trace.get("nodes") or []):
        detail = node.get("detail") or {}
        rx = detail.get("onu_rx_signal_dbm")
        suffix = f" rx {rx} dBm" if rx is not None else ""
        lines.append(f"- {node['kind']}: {node['label']} [{node['state']}]{suffix}")
    for gap in cast(list, trace.get("breaks") or []):
        lines.append(f"- path break: {gap['code']} - {gap['message']}")

    params = {
        "subscriber_id": str(subscription.subscriber_id),
        "description": "\n".join(lines),
    }
    return f"/admin/support/tickets/new?{urlencode(params)}"


def _build_network_access_cards(
    subscriptions: list,
    connection_by_subscription: dict[str, dict[str, object]],
    additional_routes_by_subscriber: dict[UUID, list[dict[str, object]]] | None = None,
    endpoints_by_subscription: dict[str, dict[str, object]] | None = None,
    traces_by_subscription: dict[str, dict[str, object] | None] | None = None,
    access_state_by_subscription: dict[str, dict[str, object] | None] | None = None,
    incident_by_subscription: dict[str, dict[str, object] | None] | None = None,
    service_health_by_subscription: Mapping[str, object] | None = None,
) -> list[dict]:
    """Build network access info cards from subscriptions with live access."""
    cards = []
    additional_routes_by_subscriber = additional_routes_by_subscriber or {}
    endpoints_by_subscription = endpoints_by_subscription or {}
    traces_by_subscription = traces_by_subscription or {}
    access_state_by_subscription = access_state_by_subscription or {}
    incident_by_subscription = incident_by_subscription or {}
    service_health_by_subscription = service_health_by_subscription or {}
    for sub in subscriptions:
        raw_status = getattr(sub, "status", None)
        status_value = getattr(raw_status, "value", None)
        status = str(
            status_value if status_value is not None else raw_status or "unknown"
        )
        if not sub.login and not sub.ipv4_address:
            continue
        sub_id = str(sub.id)
        nas = getattr(sub, "provisioning_nas_device", None)
        pop_site = getattr(nas, "pop_site", None) if nas else None
        cards.append(
            {
                "subscription_id": sub_id,
                "offer_name": sub.offer.name if sub.offer else "Subscription",
                "status": status,
                "status_presentation": subscription_status_presentation(raw_status),
                "connection_status": connection_by_subscription.get(sub_id, {}),
                "login": sub.login,
                "ipv4_address": sub.ipv4_address,
                "additional_routes": additional_routes_by_subscriber.get(
                    sub.subscriber_id, []
                )
                if status == SubscriptionStatus.active.value
                else [],
                "ipv6_address": getattr(sub, "ipv6_address", None),
                "mac_address": getattr(sub, "mac_address", None),
                "nas_name": nas.name if nas else None,
                "nas_id": str(nas.id) if nas else None,
                # Where provisioning *intends* this subscription to sit. Kept,
                # but no longer presented as the serving location.
                "pop_site_name": pop_site.name if pop_site else None,
                # Where the subscription is actually served, resolved from the
                # access path rather than inferred from the provisioning NAS.
                "access_endpoint": endpoints_by_subscription.get(sub_id, {}),
                "topology_trace": traces_by_subscription.get(sub_id),
                "access_state": access_state_by_subscription.get(sub_id),
                "known_incident": incident_by_subscription.get(sub_id),
                "service_health": service_health_by_subscription.get(sub_id),
            }
        )
        cards[-1]["ticket_prefill_url"] = _ticket_prefill_url(sub, cards[-1])
    return cards


def _current_pppoe_access_credential(
    db: Session, account_ids: list[UUID]
) -> AccessCredential | None:
    if not account_ids:
        return None
    return (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id.in_(account_ids))
        .filter(AccessCredential.is_active.is_(True))
        .filter(
            or_(
                AccessCredential.connection_type == ConnectionType.pppoe,
                AccessCredential.connection_type.is_(None),
            )
        )
        .order_by(AccessCredential.updated_at.desc())
        .first()
    )


def _build_pppoe_access_snapshot(
    db: Session, account_ids: list[UUID]
) -> dict[str, object]:
    credential = _current_pppoe_access_credential(db, account_ids)
    if not credential:
        return {
            "has_credential": False,
            "credential_id": None,
            "login": None,
            "has_password": False,
        }
    return {
        "has_credential": True,
        "credential_id": str(credential.id),
        "login": credential.username,
        "has_password": bool(credential.secret_hash),
    }


def reveal_customer_pppoe_password(
    db: Session, customer_id: str, credential_id: str | None = None
) -> tuple[str, bool]:
    customer = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if customer.user_type != UserType.customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    account_ids = [customer.id]
    credential = _current_pppoe_access_credential(db, account_ids)
    if not credential or not credential.secret_hash:
        return "", False
    if credential_id and str(credential.id) != str(credential_id):
        return "", False
    try:
        password = decrypt_credential(credential.secret_hash)
    except Exception:
        logger.warning(
            "Failed to decrypt PPPoE credential for customer %s",
            customer_id,
            exc_info=True,
        )
        return "", False
    return password or "", bool(password)


def _build_crm_sync_status(db: Session, customer: Subscriber) -> dict[str, Any]:
    def _display_datetime(value: object) -> str | None:
        if isinstance(value, datetime):
            return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if value:
            return str(value)
        return None

    latest_failure = (
        db.query(CrmSyncFailure)
        .filter(CrmSyncFailure.entity == "subscriber")
        .filter(CrmSyncFailure.external_id == str(customer.id))
        .order_by(CrmSyncFailure.created_at.desc())
        .first()
    )
    unresolved_failure = (
        latest_failure
        if latest_failure and latest_failure.status == CrmSyncFailureStatus.unresolved
        else None
    )
    crm_meta = {}
    metadata = customer.metadata_ if isinstance(customer.metadata_, dict) else {}
    raw_crm_meta = metadata.get("crm_sync") if metadata else None
    if isinstance(raw_crm_meta, dict):
        crm_meta = raw_crm_meta

    crm_subscriber_id = (
        str(customer.crm_subscriber_id) if customer.crm_subscriber_id else None
    )
    last_success_at = crm_meta.get("last_success_at")
    last_activity_at: object | None
    if unresolved_failure:
        status = "failed"
        label = "Sync failed"
        last_activity_at = (
            unresolved_failure.updated_at or unresolved_failure.created_at
        )
    elif crm_subscriber_id:
        status = "linked"
        label = "Synced"
        last_activity_at = last_success_at
    else:
        status = "pending"
        label = "Pending sync"
        last_activity_at = None

    return {
        "status": status,
        "label": label,
        "crm_subscriber_id": crm_subscriber_id,
        "last_success_at": last_success_at,
        "last_activity_at": last_activity_at,
        "last_success_display": _display_datetime(last_success_at),
        "last_activity_display": _display_datetime(last_activity_at),
        "dead_letter_id": str(unresolved_failure.id) if unresolved_failure else None,
        "error": unresolved_failure.error if unresolved_failure else None,
        "attempts": unresolved_failure.attempts if unresolved_failure else 0,
    }


def _build_identity_profile(customer: Subscriber) -> dict[str, Any]:
    gender_value = getattr(customer.gender, "value", "unknown")
    nin_value = customer.nin or ""
    required = {
        "date_of_birth": bool(customer.date_of_birth),
        "gender": gender_value not in {"", "unknown"},
        "nin": bool(nin_value),
    }
    missing = [key for key, complete in required.items() if not complete]
    return {
        "nin_masked": mask_nin(nin_value) if nin_value else None,
        "nin_verified": bool((customer.metadata_ or {}).get("nin_verified")),
        "nin_last_checked_at": (customer.metadata_ or {}).get("nin_last_checked_at"),
        "date_of_birth": customer.date_of_birth.isoformat()
        if customer.date_of_birth
        else None,
        "gender": None if gender_value == "unknown" else gender_value,
        "complete": not missing,
        "missing": missing,
        "missing_labels": ", ".join(item.replace("_", " ") for item in missing),
        "completed_count": sum(1 for value in required.values() if value),
        "total_count": len(required),
    }


def _build_access_repair_state(
    db: Session,
    customer: Subscriber,
    subscriptions: list[Subscription],
) -> dict[str, Any]:
    active_subscriptions = [
        subscription
        for subscription in subscriptions
        if subscription.status == SubscriptionStatus.active
    ]
    if not active_subscriptions:
        return {
            "enabled": False,
            "needed": False,
            "reason": "No active subscriptions to repair.",
        }

    active_credentials_count = int(
        db.query(func.count(AccessCredential.id))
        .filter(AccessCredential.subscriber_id == customer.id)
        .filter(AccessCredential.is_active.is_(True))
        .scalar()
        or 0
    )
    if active_credentials_count <= 0:
        return {
            "enabled": False,
            "needed": False,
            "reason": "No active access credentials to refresh.",
        }

    active_lock_count = int(
        db.query(func.count(EnforcementLock.id))
        .filter(EnforcementLock.subscriber_id == customer.id)
        .filter(EnforcementLock.is_active.is_(True))
        .scalar()
        or 0
    )
    if active_lock_count > 0:
        return {
            "enabled": False,
            "needed": False,
            "reason": "Blocked by an active suspension lock.",
        }

    active_dunning_count = int(
        db.query(func.count(DunningCase.id))
        .filter(DunningCase.account_id == customer.id)
        .filter(
            DunningCase.status.in_([DunningCaseStatus.open, DunningCaseStatus.paused])
        )
        .scalar()
        or 0
    )
    if active_dunning_count > 0:
        return {
            "enabled": False,
            "needed": False,
            "reason": "Blocked by an active dunning case.",
        }

    if customer.lifecycle_override_status is not None:
        return {
            "enabled": False,
            "needed": False,
            "reason": "Blocked by a manual lifecycle override.",
        }

    stale_reasons: list[str] = []
    if customer.status != SubscriberStatus.active:
        stale_reasons.append(f"account status is {customer.status.value}")
    stale_access_count = sum(
        1
        for subscription in active_subscriptions
        if (subscription.access_state or "") != "active"
    )
    if stale_access_count:
        stale_reasons.append(
            f"{stale_access_count} active subscription access state(s) are stale"
        )

    if not stale_reasons:
        return {
            "enabled": False,
            "needed": False,
            "reason": "No repair needed; account and access state already look active.",
        }

    return {
        "enabled": True,
        "needed": True,
        "reason": "Repair needed: " + "; ".join(stale_reasons) + ".",
    }


def _build_account_unsuspend_action(
    db: Session,
    customer: Subscriber,
) -> dict[str, object]:
    """Project the reviewed single-account unsuspend action for the admin UI."""
    visible = customer.lifecycle_override_status == SubscriberStatus.suspended
    if not visible:
        return {
            "visible": False,
            "allowed": False,
            "reason": "The account is not explicitly suspended.",
        }

    preview = account_status_commands.preview_account_status_change(
        db,
        account_status_commands.PreviewAccountStatusRequest(
            account_id=customer.id,
            action=account_status_commands.AccountStatusAction.unsuspend,
        ),
    )
    affected_count = len(preview.affected_subscription_ids)
    preserved_disabled_count = len(preview.preserved_disabled_subscription_ids)
    blocker_names = tuple(item.replace("_", " ") for item in preview.remaining_blockers)
    return {
        "visible": True,
        "allowed": preview.allowed,
        "fingerprint": preview.fingerprint,
        "current_status": preview.current_status.value,
        "projected_status": preview.projected_status.value,
        "override_reason": customer.lifecycle_override_reason,
        "override_source": customer.lifecycle_override_source,
        "override_at": customer.lifecycle_override_at,
        "affected_subscription_count": affected_count,
        "matching_lock_count": len(preview.matching_lock_ids),
        "preserved_disabled_count": preserved_disabled_count,
        "remaining_blockers": blocker_names,
        "reason": preview.eligibility_reason,
        "confirmation": (
            "Unsuspend this customer account? This clears the explicit account "
            f"suspension, restores {affected_count} service(s) held by that "
            "suspension, preserves "
            f"{preserved_disabled_count} disabled service(s), and leaves "
            "unrelated enforcement locks in place."
        ),
    }


def build_customer_detail_snapshot(
    db: Session,
    customer_id: str,
    *,
    include_conversations: bool = False,
) -> dict[str, Any]:
    """Build unified customer detail snapshot.

    Every customer is a subscriber. Business accounts store their
    company identity directly on the subscriber row.

    ``include_conversations`` is decided by the caller's permission check. The
    read is skipped entirely when false, so conversation data never reaches the
    template context for a principal that may not see it.
    """
    customer = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if customer.user_type != UserType.customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    customer_name = (
        customer.company_name
        or customer.display_name
        or f"{customer.first_name or ''} {customer.last_name or ''}".strip()
    )
    organization = None
    if customer.category == SubscriberCategory.business:
        organization = SimpleNamespace(
            name=customer.company_name or customer_name,
            legal_name=customer.legal_name,
            tax_id=customer.tax_id,
            domain=customer.domain,
            website=customer.website,
            notes=customer.notes,
        )

    subscribers = [customer]

    # Load addresses and contacts for all subscriber accounts
    addresses: list[Any] = []
    contacts: list[dict[str, object]] = []
    accounts: list[Subscriber] = []
    for sub in subscribers:
        try:
            sub_addresses = subscriber_service.addresses.list(
                db=db,
                subscriber_id=str(sub.id),
                order_by="created_at",
                order_dir="desc",
                limit=50,
                offset=0,
            )
            addresses.extend(sub_addresses)
        except Exception:
            logger.debug(
                "Failed to load addresses for subscriber %s",
                sub.id,
                exc_info=True,
            )
        accounts.append(sub)
        channels = (
            db.query(SubscriberChannel)
            .filter(SubscriberChannel.subscriber_id == sub.id)
            .order_by(SubscriberChannel.created_at.desc())
            .limit(50)
            .all()
        )
        contacts.extend(_format_contact_channel(sub, channel) for channel in channels)

    accounts = _dedupe_accounts(accounts)
    subscriptions = _list_subscriptions_for_accounts(db, accounts)
    account_lookup = {str(account.id): account for account in accounts}
    account_ids = [account.id for account in accounts]

    # Financials & relationships
    finance_data = _build_common_financials(db, account_ids)
    invoices = finance_data["invoices"]
    payments = finance_data["payments"]
    invoice_status_presentations = finance_data["invoice_status_presentations"]
    payment_status_presentations = finance_data["payment_status_presentations"]
    balance_due = finance_data["balance_due"]
    financials = finance_data["financials"]
    active_subscriptions = sum(
        1 for sub in subscriptions if is_customer_impact_service_status(sub.status)
    )
    monthly_recurring = sum(
        float(getattr(sub, "unit_price", 0) or 0)
        for sub in subscriptions
        if is_mrr_countable_service_status(sub.status)
    )
    financials["monthly_recurring"] = monthly_recurring
    relationship_data = _build_relationship_data(db, account_ids)
    billing_workspace = _build_admin_billing_workspace(db, account_ids)

    # Address resolution with fallback
    if not addresses:
        addresses = _build_person_fallback_address(db, customer)

    primary_address = next(
        (
            a
            for a in addresses
            if getattr(a, "is_primary", False) and (a.address_line1 or "").strip()
        ),
        next(
            (a for a in addresses if getattr(a, "is_primary", False)),
            next(
                (a for a in addresses if (a.address_line1 or "").strip()),
                addresses[0] if addresses else None,
            ),
        ),
    )
    map_data, geocode_target = _build_map_payload(primary_address, customer_name)

    sub_filter = Subscriber.id == customer.id
    active_subscribers = (
        db.query(Subscriber).filter(sub_filter, Subscriber.is_active.is_(True)).count()
    )
    total_subscribers = db.query(Subscriber).filter(sub_filter).count()

    # Notifications — gather recipients from all accounts
    notifications: list[Any] = []
    try:
        recipients: list[str] = []
        for sub in subscribers:
            if sub.email:
                recipients.append(sub.email)
            if sub.phone:
                recipients.append(sub.phone)
        if recipients:
            all_notifications = notification_service.Notifications.list(
                db=db,
                channel=None,
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=100,
                offset=0,
            )
            notifications = [n for n in all_notifications if n.recipient in recipients][
                :5
            ]
    except Exception:
        db.rollback()
        notifications = []

    activity_items = _build_activity_items(
        db,
        "subscriber",
        str(customer_id),
        account_ids,
        subscriptions,
    )
    relationship_summary = cast(
        dict[str, int],
        relationship_data["relationship_summary"],
    )
    stats = {
        "total_subscribers": len(subscribers),
        "total_subscriptions": len(subscriptions),
        "active_subscriptions": active_subscriptions,
        "balance_due": balance_due,
        "total_addresses": len(addresses),
        "total_contacts": len(contacts),
        "open_tickets": relationship_summary["open_tickets"],
        "active_service_orders": relationship_summary["active_service_orders"],
        "service_orders": relationship_summary["active_service_orders"],
        "active_credentials": relationship_summary["active_credentials"],
        "active_network_assets": (
            relationship_summary["active_cpes"]
            + relationship_summary["active_onts"]
            + relationship_summary["active_ip_assignments"]
        ),
    }
    try:
        customer_user_access = (
            web_customer_user_access_service.build_customer_user_access_state(
                db,
                customer_type="person",
                customer_id=customer_id,
            )
        )
    except Exception as exc:
        customer_user_access = {"error": str(exc)}

    account_health = build_portal_account_health(db, customer.id).for_active_services()
    service_health_by_subscription = {
        str(service.subscription_id): service for service in account_health.services
    }
    pppoe_access = _build_pppoe_access_snapshot(db, account_ids)
    network_connection_status, connection_by_subscription = (
        _build_network_connection_snapshot(db, subscriptions)
    )
    additional_routes_by_subscriber = _active_additional_routes_by_subscriber(
        db, account_ids
    )
    endpoints_by_subscription: dict[str, dict[str, object]] = {}
    traces_by_subscription: dict[str, dict[str, object] | None] = {}
    access_state_by_subscription: dict[str, dict[str, object] | None] = {}
    incident_by_subscription: dict[str, dict[str, object] | None] = {}
    for sub in subscriptions:
        if not sub.login and not sub.ipv4_address:
            continue
        access_state_by_subscription[str(sub.id)] = _build_access_state_facts(sub)
        incident_by_subscription[str(sub.id)] = _build_known_incident(db, sub)
        # One path resolution feeds both the endpoint card and the trace.
        try:
            path = resolve_customer_path(db, sub)
        except Exception:
            logger.warning(
                "Access path resolution failed for subscription %s",
                sub.id,
                exc_info=True,
            )
            endpoints_by_subscription[str(sub.id)] = {"endpoint_source": "unresolved"}
            traces_by_subscription[str(sub.id)] = None
            continue
        endpoint, trace = _build_access_endpoint_projection(db, sub, path)
        endpoints_by_subscription[str(sub.id)] = endpoint
        traces_by_subscription[str(sub.id)] = trace
    network_access_cards = _build_network_access_cards(
        subscriptions,
        connection_by_subscription,
        additional_routes_by_subscriber,
        endpoints_by_subscription,
        traces_by_subscription,
        access_state_by_subscription,
        incident_by_subscription,
        service_health_by_subscription,
    )
    network_access_active_count = sum(
        1
        for card in network_access_cards
        if card["status"] == SubscriptionStatus.active.value
    )
    network_access_inactive_count = (
        len(network_access_cards) - network_access_active_count
    )
    pending_location_request = (
        db.query(CustomerLocationChangeRequest)
        .filter(CustomerLocationChangeRequest.subscriber_id == customer.id)
        .filter(
            CustomerLocationChangeRequest.status
            == CustomerLocationChangeRequestStatus.pending
        )
        .order_by(CustomerLocationChangeRequest.created_at.desc())
        .first()
    )

    # Communications projection. `communications.team_inbox` owns these rows;
    # the customer record reads its own scoped slice and links back to the
    # workspace to act on one.
    customer_conversations: Sequence[Any] = ()
    if include_conversations:
        from app.services import team_inbox_read

        customer_conversations = team_inbox_read.list_conversations(
            db,
            subscriber_id=customer.id,
            order_by="last_message_at",
            order_dir="desc",
            limit=5,
        ).items

    return {
        "customer": customer,
        "can_view_conversations": include_conversations,
        "customer_conversations": customer_conversations,
        "customer_status_presentation": account_status_presentation(
            customer.status,
            is_active=customer.is_active,
        ),
        "customer_type": "person",
        "customer_name": customer_name,
        "organization": organization,
        "subscribers": subscribers,
        "accounts": accounts,
        "account_status_presentations": {
            str(account.id): account_status_presentation(
                account.status,
                is_active=account.is_active,
            )
            for account in accounts
        },
        "subscriptions": subscriptions,
        "restorable_subscription_ids": {
            str(subscription.id)
            for subscription in subscriptions
            if is_subscription_restore_candidate(subscription.status)
        },
        "subscription_status_presentations": {
            str(subscription.id): subscription_status_presentation(subscription.status)
            for subscription in subscriptions
        },
        "account_lookup": account_lookup,
        "addresses": addresses,
        "primary_address": primary_address,
        "map_data": map_data,
        "geocode_target": geocode_target,
        "contacts": contacts,
        "invoices": invoices,
        "payments": payments,
        "invoice_status_presentations": invoice_status_presentations,
        "payment_status_presentations": payment_status_presentations,
        "notifications": notifications,
        "stats": stats,
        "financials": financials,
        "has_active_subscribers": active_subscribers > 0,
        "has_any_subscribers": total_subscribers > 0,
        "activity_items": activity_items,
        "customer_user_access": customer_user_access,
        "crm_sync_status": _build_crm_sync_status(db, customer),
        "identity_profile": _build_identity_profile(customer),
        "pppoe_access": pppoe_access,
        "billing_policy": _billing_policy_snapshot(db, accounts),
        "billing_workspace": billing_workspace,
        "network_connection_status": network_connection_status,
        "connection_by_subscription": connection_by_subscription,
        "network_access_cards": network_access_cards,
        "network_access_active_count": network_access_active_count,
        "network_access_inactive_count": network_access_inactive_count,
        "account_health": account_health,
        "service_health_by_subscription": service_health_by_subscription,
        "access_repair_state": _build_access_repair_state(
            db,
            customer,
            subscriptions,
        ),
        "account_unsuspend_action": _build_account_unsuspend_action(db, customer),
        "pending_location_request": pending_location_request,
        "subscriber_summary": subscriber_summary_service.subscriber_summary(
            db, customer_id
        ),
        **relationship_data,
    }


def build_person_detail_snapshot(db: Session, customer_id: str) -> dict[str, Any]:
    """Backwards-compatible wrapper — delegates to unified snapshot."""
    return build_customer_detail_snapshot(db, customer_id)


def build_business_detail_snapshot(db: Session, customer_id: str) -> dict[str, Any]:
    """Business-customer wrapper for business URLs."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if subscriber.category != SubscriberCategory.business:
        raise HTTPException(status_code=404, detail="Business customer not found")
    return build_customer_detail_snapshot(db, str(subscriber.id))
