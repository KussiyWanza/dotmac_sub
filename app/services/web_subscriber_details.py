"""Helpers for admin subscriber detail page."""

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.models.billing import CreditNoteStatus, Invoice, Payment
from app.models.catalog import (
    ContractTerm,
    OfferStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import FdhCabinet, FiberSpliceClosure
from app.models.network_monitoring import SpeedTestResult
from app.models.provisioning import ServiceOrder
from app.models.subscriber import (
    Address,
    SubscriberCategory,
    SubscriberChannel,
)
from app.models.support import Ticket
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import notification as notification_service
from app.services import subscriber as subscriber_service
from app.services import web_customer_user_access as web_customer_user_access_service
from app.services.audit_helpers import (
    extract_changes,
    format_changes,
    humanize_action,
    humanize_entity,
    list_audit_events_for_entities,
    load_audit_actor_subscribers,
    resolve_actor_name,
)
from app.services.customer_network_context import get_customer_network_context
from app.services.customer_support_links import ticket_customer_link_filter
from app.services.invoice_collectibility import open_invoice_balance
from app.services.subscription_lifecycle_policy import is_customer_impact_service_status

logger = logging.getLogger(__name__)


def _enum_value(value) -> str:
    return str(getattr(value, "value", value) or "")


def _format_attachment_size(size_bytes: object) -> str:
    try:
        size = int(str(size_bytes))
    except (TypeError, ValueError):
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _timeline_time(value: datetime | None) -> str:
    return value.strftime("%b %d, %Y %H:%M") if value else "Just now"


def _timeline_sort_key(entry: dict[str, object]) -> datetime:
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, datetime):
        return timestamp
    return datetime.min.replace(tzinfo=UTC)


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two lat/lon points in meters."""
    radius_m = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _resolve_subscriber_addresses(db: Session, subscriber_id):
    addresses = db.query(Address).filter(Address.subscriber_id == subscriber_id).all()
    primary_address = next(
        (addr for addr in addresses if addr.is_primary),
        addresses[0] if addresses else None,
    )
    return addresses, primary_address


def build_subscriber_map_data(db: Session, subscriber, primary_address):
    """Build mini-map GeoJSON payload for subscriber detail page."""
    if not (
        primary_address
        and primary_address.latitude is not None
        and primary_address.longitude is not None
    ):
        return None
    customer_lat = float(primary_address.latitude)
    customer_lon = float(primary_address.longitude)

    customer_name = str(getattr(subscriber, "name", "") or "").strip() or "Customer"

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [primary_address.longitude, primary_address.latitude],
            },
            "properties": {
                "type": "customer",
                "name": customer_name,
                "address": primary_address.address_line1,
            },
        }
    ]

    fdh_cabinets = (
        db.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .filter(FdhCabinet.latitude.isnot(None))
        .filter(FdhCabinet.longitude.isnot(None))
        .all()
    )
    for fdh in fdh_cabinets:
        if fdh.latitude is None or fdh.longitude is None:
            continue
        distance = _haversine_distance(
            customer_lat,
            customer_lon,
            float(fdh.latitude),
            float(fdh.longitude),
        )
        if distance <= 2000:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [fdh.longitude, fdh.latitude],
                    },
                    "properties": {
                        "type": "fdh_cabinet",
                        "name": fdh.name,
                        "code": fdh.code,
                        "distance_m": round(distance),
                    },
                }
            )

    closures = (
        db.query(FiberSpliceClosure)
        .filter(FiberSpliceClosure.is_active.is_(True))
        .filter(FiberSpliceClosure.latitude.isnot(None))
        .filter(FiberSpliceClosure.longitude.isnot(None))
        .all()
    )
    for closure in closures:
        if closure.latitude is None or closure.longitude is None:
            continue
        distance = _haversine_distance(
            customer_lat,
            customer_lon,
            float(closure.latitude),
            float(closure.longitude),
        )
        if distance <= 1000:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [closure.longitude, closure.latitude],
                    },
                    "properties": {
                        "type": "splice_closure",
                        "name": closure.name,
                        "distance_m": round(distance),
                    },
                }
            )

    return {
        "center": [primary_address.latitude, primary_address.longitude],
        "geojson": {"type": "FeatureCollection", "features": features},
    }


def build_subscriber_geocode_target(primary_address):
    """Build geocode payload when address exists without coordinates."""
    if not primary_address or not (primary_address.address_line1 or "").strip():
        return None
    if (
        getattr(primary_address, "latitude", None) is not None
        and getattr(primary_address, "longitude", None) is not None
    ):
        return None
    target_id = getattr(primary_address, "id", None)
    return {
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


def _build_equipment_snapshot(db: Session, subscriber_id) -> dict[str, object]:
    """Collect ONT/CPE devices and direct management links for subscriber detail."""
    equipment: list[dict[str, object]] = []
    context = get_customer_network_context(db, subscriber_id)
    from app.services.network.ont_status import resolve_effective_ont_status

    try:
        for assignment in context.ont_assignments:
            ont = assignment.ont_unit
            if not ont:
                continue
            equipment.append(
                {
                    "type": "ONT",
                    "model": ont.model or ont.name or "ONT",
                    "serial": ont.serial_number or "-",
                    "online": resolve_effective_ont_status(ont).is_online,
                    "detail_url": f"/admin/network/onts/{ont.id}",
                    "tr069_url": f"/admin/network/onts/{ont.id}?tab=diagnostics",
                }
            )
    except Exception:
        logger.exception(
            "Failed to load subscriber ONT equipment snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()

    try:
        for cpe in context.cpe_devices:
            cpe_type = _enum_value(getattr(cpe, "device_type", None)) or "CPE"
            status_value = _enum_value(getattr(cpe, "status", None)).lower()
            equipment.append(
                {
                    "type": cpe_type.upper(),
                    "model": (cpe.model or cpe.vendor or cpe_type.upper()),
                    "serial": (cpe.serial_number or cpe.mac_address or "-"),
                    "online": status_value == "active",
                    "detail_url": f"/admin/network/cpes/{cpe.id}",
                }
            )
    except Exception:
        logger.exception(
            "Failed to load subscriber CPE equipment snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()

    primary_ont = next((item for item in equipment if item.get("type") == "ONT"), None)
    return {
        "equipment": equipment,
        "primary_ont_url": (primary_ont or {}).get("detail_url"),
        "primary_ont_tr069_url": (primary_ont or {}).get("tr069_url"),
    }


def build_subscriber_detail_snapshot(db: Session, subscriber, subscriber_id):
    """Collect subscriber detail page data from multiple services."""
    subscriptions = []
    all_subscriptions = []
    olt_status = {}
    try:
        all_subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(subscriber.id),
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=50,
            offset=0,
        )
        all_subscriptions = sorted(
            all_subscriptions,
            key=lambda item: item.created_at or item.id,
            reverse=True,
        )
        subscriptions = [
            s
            for s in all_subscriptions
            if is_customer_impact_service_status(getattr(s, "status", None))
        ][:10]
        for sub in subscriptions:
            latest_session = (
                db.query(RadiusAccountingSession)
                .filter(RadiusAccountingSession.subscription_id == sub.id)
                .order_by(RadiusAccountingSession.created_at.desc())
                .first()
            )
            if latest_session:
                olt_status[str(sub.id)] = (
                    latest_session.status_type
                    in (AccountingStatus.start, AccountingStatus.interim)
                    and latest_session.session_end is None
                )
            else:
                olt_status[str(sub.id)] = False
    except Exception:
        logger.exception(
            "Failed to load subscriber subscriptions/OLT status for detail snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        subscriptions = []
        all_subscriptions = []
        olt_status = {}

    accounts = []
    invoices = []
    balance_due = Decimal("0.00")
    available_credit = Decimal("0.00")
    current_balance = Decimal("0.00")
    try:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=str(subscriber_id),
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=25,
            offset=0,
        )
        if accounts:
            account = accounts[0]
            invoices = billing_service.invoices.list(
                db=db,
                account_id=account.id,
                status=None,
                is_active=None,
                order_by="created_at",
                order_dir="desc",
                limit=5,
                offset=0,
            )
            balance_due = open_invoice_balance(db, account.id)
            credit_notes = billing_service.credit_notes.list(
                db=db,
                account_id=account.id,
                invoice_id=None,
                status=None,
                is_active=True,
                order_by="created_at",
                order_dir="desc",
                limit=100,
                offset=0,
            )
            available_credit = sum(
                (
                    Decimal(str(note.total or 0))
                    - Decimal(str(note.applied_total or 0))
                    for note in credit_notes
                    if note.status
                    in (CreditNoteStatus.issued, CreditNoteStatus.partially_applied)
                ),
                Decimal("0.00"),
            )
            current_balance = balance_due + available_credit
    except Exception:
        logger.exception(
            "Failed to load subscriber billing snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        accounts = []
        invoices = []
        balance_due = Decimal("0.00")
        available_credit = Decimal("0.00")
        current_balance = Decimal("0.00")

    # Dunning cases
    dunning_cases = []
    try:
        from app.models.collections import DunningCase

        dunning_cases = (
            db.query(DunningCase)
            .filter(DunningCase.account_id == subscriber.id)
            .order_by(DunningCase.created_at.desc())
            .limit(10)
            .all()
        )
    except Exception:
        logger.exception(
            "Failed to load dunning cases for subscriber %s", subscriber_id
        )
        db.rollback()

    # Payments
    payments = []
    try:
        from app.models.billing import Payment

        payments = (
            db.query(Payment)
            .filter(
                Payment.account_id == subscriber.id,
                Payment.is_active.is_(True),
            )
            .order_by(Payment.created_at.desc())
            .limit(20)
            .all()
        )
    except Exception:
        logger.exception("Failed to load payments for subscriber %s", subscriber_id)
        db.rollback()

    # Service orders
    service_orders = []
    try:
        from app.models.provisioning import ServiceOrder

        service_orders = (
            db.query(ServiceOrder)
            .filter(ServiceOrder.subscriber_id == subscriber.id)
            .order_by(ServiceOrder.created_at.desc())
            .limit(10)
            .all()
        )
    except Exception:
        logger.exception(
            "Failed to load service orders for subscriber %s", subscriber_id
        )
        db.rollback()

    notifications = []
    try:
        recipients = []
        if getattr(subscriber, "email", None):
            recipients.append(subscriber.email)
        if getattr(subscriber, "phone", None):
            recipients.append(subscriber.phone)
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
            notifications = [
                item for item in all_notifications if item.recipient in recipients
            ][:5]
    except Exception:
        logger.exception(
            "Failed to load subscriber notifications snapshot",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        notifications = []

    monthly_bill = 0.0
    if subscriptions:
        for sub in subscriptions:
            # Try unit_price override first, then fall back to offer's recurring price
            if sub.unit_price:
                monthly_bill += float(sub.unit_price)
            elif sub.offer and hasattr(sub.offer, "prices") and sub.offer.prices:
                from app.models.catalog import PriceType

                recurring = [
                    p
                    for p in sub.offer.prices
                    if p.price_type == PriceType.recurring and p.is_active
                ]
                if recurring:
                    monthly_bill += float(recurring[0].amount)
    stats = {
        "monthly_bill": monthly_bill,
        "balance_due": float(balance_due),
        "credit_issued": float(available_credit),
        "current_balance": float(current_balance),
        "has_credit_adjustment": available_credit > 0,
        "data_usage": "0",
    }

    addresses, primary_address = _resolve_subscriber_addresses(db, subscriber_id)
    channels = (
        db.query(SubscriberChannel)
        .filter(SubscriberChannel.subscriber_id == subscriber_id)
        .order_by(
            SubscriberChannel.is_primary.desc(), SubscriberChannel.created_at.asc()
        )
        .all()
    )
    contacts: list[dict[str, object]] = []
    for channel in channels:
        contacts.append(
            {
                "id": str(channel.id),
                "type": channel.channel_type.value if channel.channel_type else "email",
                "label": channel.label or "",
                "value": channel.address,
                "is_primary": bool(channel.is_primary),
            }
        )

    organization_members = []
    if subscriber.category == SubscriberCategory.business:
        organization_members.append(
            {
                "id": str(subscriber.id),
                "name": subscriber.company_name
                or subscriber.display_name
                or f"{subscriber.first_name} {subscriber.last_name}".strip()
                or subscriber.email,
                "email": subscriber.email,
                "is_active": bool(subscriber.is_active),
                "is_current": True,
            }
        )

    map_data = build_subscriber_map_data(db, subscriber, primary_address)
    geocode_target = build_subscriber_geocode_target(primary_address)
    speedtest_snapshot = _build_speedtest_snapshot(db, subscriber_id, subscriptions)
    equipment_snapshot = _build_equipment_snapshot(db, subscriber_id)

    return {
        "accounts": accounts,
        "subscriptions": subscriptions,
        "all_subscriptions": all_subscriptions,
        "olt_status": olt_status,
        "invoices": invoices,
        "payments": payments,
        "dunning_cases": dunning_cases,
        "service_orders": service_orders,
        "notifications": notifications,
        "stats": stats,
        "addresses": addresses,
        "contacts": contacts,
        "primary_address": primary_address,
        "organization_members": organization_members,
        "map_data": map_data,
        "geocode_target": geocode_target,
        **speedtest_snapshot,
        **equipment_snapshot,
    }


def _build_speedtest_snapshot(
    db: Session, subscriber_id, subscriptions: list
) -> dict[str, object]:
    plan_download = 0.0
    plan_upload = 0.0
    for subscription in subscriptions:
        offer = getattr(subscription, "offer", None)
        if not offer:
            continue
        plan_download = float(getattr(offer, "speed_download_mbps", 0) or 0)
        plan_upload = float(getattr(offer, "speed_upload_mbps", 0) or 0)
        if plan_download > 0 or plan_upload > 0:
            break

    try:
        tests = (
            db.query(SpeedTestResult)
            .filter(SpeedTestResult.subscriber_id == subscriber_id)
            .order_by(SpeedTestResult.tested_at.desc())
            .limit(30)
            .all()
        )
    except ProgrammingError as exc:
        # Some environments are behind on migrations and may not have speed_test_results yet.
        # Fail open so subscriber detail still renders.
        db.rollback()
        if "speed_test_results" in str(exc).lower():
            return {
                "speedtests": [],
                "speedtest_performance_rows": [],
                "speedtest_chart": {"labels": [], "download": [], "upload": []},
                "speedtest_plan": {
                    "download_mbps": plan_download,
                    "upload_mbps": plan_upload,
                },
                "speedtest_underperforming_count": 0,
            }
        raise

    performance_rows = []
    underperforming_count = 0
    for test in tests:
        down = float(test.download_mbps or 0)
        up = float(test.upload_mbps or 0)
        down_ratio = (down / plan_download) if plan_download > 0 else None
        up_ratio = (up / plan_upload) if plan_upload > 0 else None
        performance_ratio = min(
            [ratio for ratio in (down_ratio, up_ratio) if ratio is not None] or [1.0]
        )
        is_underperforming = performance_ratio < 0.8
        if is_underperforming:
            underperforming_count += 1
        performance_rows.append(
            {
                "test": test,
                "down_ratio_pct": round((down_ratio or 0) * 100, 1)
                if down_ratio is not None
                else None,
                "up_ratio_pct": round((up_ratio or 0) * 100, 1)
                if up_ratio is not None
                else None,
                "is_underperforming": is_underperforming,
            }
        )

    chart_source = list(reversed(tests[:12]))
    chart = {
        "labels": [
            item.tested_at.strftime("%m-%d %H:%M") if item.tested_at else ""
            for item in chart_source
        ],
        "download": [round(float(item.download_mbps or 0), 2) for item in chart_source],
        "upload": [round(float(item.upload_mbps or 0), 2) for item in chart_source],
    }
    return {
        "speedtests": tests,
        "speedtest_performance_rows": performance_rows,
        "speedtest_chart": chart,
        "speedtest_plan": {
            "download_mbps": plan_download,
            "upload_mbps": plan_upload,
        },
        "speedtest_underperforming_count": underperforming_count,
    }


def build_subscriber_timeline(db: Session, subscriber_id):
    """Build audit timeline for subscriber detail page."""
    subscriber_id_str = str(subscriber_id)
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber_id)
        .order_by(
            func.coalesce(
                Subscription.updated_at,
                Subscription.start_at,
                Subscription.created_at,
            ).desc()
        )
        .limit(8)
        .all()
    )
    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == subscriber_id)
        .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
        .limit(8)
        .all()
    )
    payments = (
        db.query(Payment)
        .filter(Payment.account_id == subscriber_id)
        .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
        .limit(8)
        .all()
    )
    service_orders = (
        db.query(ServiceOrder)
        .filter(ServiceOrder.subscriber_id == subscriber_id)
        .order_by(
            func.coalesce(ServiceOrder.updated_at, ServiceOrder.created_at).desc()
        )
        .limit(8)
        .all()
    )
    tickets = (
        db.query(Ticket)
        .filter(ticket_customer_link_filter(Ticket, subscriber_id))
        .order_by(func.coalesce(Ticket.updated_at, Ticket.created_at).desc())
        .limit(8)
        .all()
    )
    entity_refs = [("subscriber", subscriber_id_str)]
    entity_refs.extend(
        ("subscription", str(subscription.id)) for subscription in subscriptions
    )
    entity_refs.extend(("invoice", str(invoice.id)) for invoice in invoices)
    entity_refs.extend(("payment", str(payment.id)) for payment in payments)
    entity_refs.extend(("service_order", str(order.id)) for order in service_orders)
    entity_refs.extend(("support_ticket", str(ticket.id)) for ticket in tickets)
    audit_events = list_audit_events_for_entities(db, entity_refs, limit=20)
    people = load_audit_actor_subscribers(db, audit_events)
    timeline = []
    for event in audit_events:
        actor_name = resolve_actor_name(event, people)
        metadata = getattr(event, "metadata_", None) or {}
        comment_text = str(metadata.get("comment") or "").strip()
        is_todo = bool(metadata.get("is_todo"))
        is_completed = bool(metadata.get("is_completed"))
        attachments: list[dict[str, str]] = []
        raw_attachments = metadata.get("attachments")
        if isinstance(raw_attachments, list):
            for item in raw_attachments:
                if not isinstance(item, dict):
                    continue
                attachment_id = str(item.get("id") or "").strip()
                if not attachment_id:
                    continue
                filename = (
                    str(item.get("filename") or "Attachment").strip() or "Attachment"
                )
                attachments.append(
                    {
                        "id": attachment_id,
                        "filename": filename,
                        "size_label": _format_attachment_size(item.get("size")),
                    }
                )
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        if comment_text:
            detail = f"{actor_name} · {comment_text}"
        else:
            detail = (
                actor_name if not change_summary else f"{actor_name} · {change_summary}"
            )
        timeline.append(
            {
                "id": str(event.id),
                "type": "audit",
                "title": (
                    f"{humanize_entity(getattr(event, 'entity_type', None))} "
                    f"{humanize_action(getattr(event, 'action', None))}"
                ),
                "detail": detail,
                "is_comment": bool(comment_text),
                "is_todo": is_todo,
                "is_completed": is_completed,
                "attachments": attachments,
                "time": _timeline_time(getattr(event, "occurred_at", None)),
                "timestamp": getattr(event, "occurred_at", None),
            }
        )
    # Also include domain events from event_store (lifecycle events like
    # subscription.suspended, subscription.upgraded, etc.)
    try:
        from app.models.event_store import EventStore

        domain_events = (
            db.query(EventStore)
            .filter(EventStore.account_id == subscriber_id)
            .order_by(EventStore.created_at.desc())
            .limit(20)
            .all()
        )
        for event in domain_events:
            event_type = getattr(event, "event_type", "") or ""
            payload = getattr(event, "payload", {}) or {}
            title = event_type.replace(".", " ").replace("_", " ").title()
            detail_parts = []
            if payload.get("offer_name"):
                detail_parts.append(payload["offer_name"])
            if payload.get("direction"):
                detail_parts.append(f"({payload['direction']})")
            if payload.get("from_status") and payload.get("to_status"):
                detail_parts.append(
                    f"{payload['from_status']} → {payload['to_status']}"
                )
            if payload.get("new_offer_name") and payload.get("previous_offer_name"):
                detail_parts.append(
                    f"{payload['previous_offer_name']} → {payload['new_offer_name']}"
                )
            detail = " · ".join(detail_parts) if detail_parts else "System"
            timeline.append(
                {
                    "id": str(event.id),
                    "type": "event",
                    "title": title,
                    "detail": detail,
                    "is_comment": False,
                    "is_todo": False,
                    "is_completed": False,
                    "attachments": [],
                    "time": _timeline_time(getattr(event, "created_at", None)),
                    "timestamp": getattr(event, "created_at", None),
                }
            )
    except Exception:
        logger.debug("Could not load domain events for timeline", exc_info=True)

    # Sort combined timeline by time (newest first) and limit
    timeline.sort(key=_timeline_sort_key, reverse=True)
    return timeline[:30]


def build_subscriber_detail_page_context(db: Session, subscriber_id):
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found")

    detail_snapshot = build_subscriber_detail_snapshot(
        db=db,
        subscriber=subscriber,
        subscriber_id=subscriber_id,
    )
    try:
        timeline = build_subscriber_timeline(db=db, subscriber_id=subscriber_id)
    except Exception:
        logger.exception(
            "Failed to load subscriber timeline for detail page",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        timeline = []
    try:
        offers = catalog_service.offers.list(
            db=db,
            service_type=None,
            access_type=None,
            status=OfferStatus.active.value,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
    except Exception:
        logger.exception(
            "Failed to load active offers for subscriber detail page",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        offers = []
    try:
        subscriber_user_access = (
            web_customer_user_access_service.build_subscriber_user_access_state(
                db,
                subscriber_id=str(subscriber_id),
            )
        )
    except Exception as exc:
        logger.exception(
            "Failed to load subscriber user access state for detail page",
            extra={"subscriber_id": str(subscriber_id)},
        )
        db.rollback()
        subscriber_user_access = {"error": str(exc)}

    # Enrich with reseller, last_online, credentials, communication stats
    enrichment = _build_subscriber_enrichment(db, subscriber)

    return {
        "subscriber": subscriber,
        **detail_snapshot,
        **enrichment,
        "billing_config": _build_billing_config(subscriber),
        "subscriber_user_access": subscriber_user_access,
        "timeline": timeline,
        "offers": offers,
        "subscription_statuses": [s.value for s in SubscriptionStatus],
        "contract_terms": [t.value for t in ContractTerm],
    }


def _build_subscriber_enrichment(db: Session, subscriber) -> dict:
    """Build enrichment data: reseller, last_online, credentials, comms."""
    from app.models.catalog import AccessCredential, NasDevice, Subscription
    from app.models.subscriber import Reseller

    metadata = dict(getattr(subscriber, "metadata_", None) or {})
    enrichment: dict = {}

    # Reseller name
    if subscriber.reseller_id:
        reseller = db.get(Reseller, subscriber.reseller_id)
        enrichment["reseller_name"] = reseller.name if reseller else None
    else:
        enrichment["reseller_name"] = None

    # Last online from imported metadata.
    enrichment["last_online"] = metadata.get("splynx_last_online") or metadata.get(
        "last_online"
    )

    # Billing email
    enrichment["billing_email"] = metadata.get("billing_email") or metadata.get(
        "splynx_billing_email"
    )

    # GPS coordinates
    gps_raw = metadata.get("splynx_gps") or metadata.get("gps")
    enrichment["gps_coordinates"] = gps_raw

    # Imported location metadata.
    enrichment["splynx_location_id"] = metadata.get("splynx_location_id")

    # RADIUS credentials per subscription
    credentials = (
        db.query(AccessCredential)
        .filter(
            AccessCredential.subscriber_id == subscriber.id,
            AccessCredential.is_active.is_(True),
        )
        .all()
    )
    enrichment["access_credentials"] = credentials

    # NAS device names for active subscriptions
    subscriptions = (
        db.query(Subscription).filter(Subscription.subscriber_id == subscriber.id).all()
    )
    nas_names: dict = {}
    for sub in subscriptions:
        if sub.provisioning_nas_device_id:
            nas = db.get(NasDevice, sub.provisioning_nas_device_id)
            if nas:
                nas_names[str(sub.id)] = nas.name
    enrichment["nas_device_names"] = nas_names

    # Communication log counts
    try:
        from sqlalchemy import func

        from app.models.communication_log import CommunicationChannel, CommunicationLog

        email_count = (
            db.query(func.count(CommunicationLog.id))
            .filter(
                CommunicationLog.subscriber_id == subscriber.id,
                CommunicationLog.channel == CommunicationChannel.email,
            )
            .scalar()
        ) or 0
        sms_count = (
            db.query(func.count(CommunicationLog.id))
            .filter(
                CommunicationLog.subscriber_id == subscriber.id,
                CommunicationLog.channel == CommunicationChannel.sms,
            )
            .scalar()
        ) or 0
        enrichment["comms_email_count"] = email_count
        enrichment["comms_sms_count"] = sms_count
    except Exception:
        enrichment["comms_email_count"] = 0
        enrichment["comms_sms_count"] = 0

    return enrichment


def _build_billing_config(subscriber) -> dict[str, object]:
    metadata = dict(getattr(subscriber, "metadata_", None) or {})
    auto_create = bool(metadata.get("auto_create_invoices", True))
    send_notifications = _billing_notification_flag_enabled(metadata)

    return {
        "category": getattr(subscriber, "category", None),
        "billing_day": getattr(subscriber, "billing_day", None),
        "payment_due_days": getattr(subscriber, "payment_due_days", None),
        "grace_period_days": getattr(subscriber, "grace_period_days", None),
        "min_balance": getattr(subscriber, "min_balance", None),
        "billing_enabled": bool(getattr(subscriber, "billing_enabled", True)),
        "auto_create_invoices": auto_create,
        "send_billing_notifications": send_notifications,
    }


def _billing_notification_flag_enabled(metadata: dict | None) -> bool:
    raw_value = (metadata or {}).get("send_billing_notifications")
    if raw_value is None:
        return True
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw_value)


def list_communication_logs(
    db: Session,
    subscriber_id: str,
    *,
    limit: int = 25,
    offset: int = 0,
) -> list:
    """Return communication logs for a subscriber, newest first."""
    from app.models.communication_log import CommunicationLog

    return (
        db.query(CommunicationLog)
        .filter(CommunicationLog.subscriber_id == subscriber_id)
        .order_by(CommunicationLog.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
