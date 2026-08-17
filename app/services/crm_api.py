from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.models.catalog import (
    CatalogOffer,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.collections import DunningCase
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.lifecycle import SubscriptionLifecycleEvent
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.service_extension import ServiceExtension, ServiceExtensionEntry
from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.models.system_user import SystemUser
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services import service_address as service_address_service
from app.services.account_lifecycle import (
    cancel_subscription,
    transition_account_status,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import round_money
from app.services.invoice_collectibility import (
    open_invoice_balance,
    open_invoice_filters_for_accounts,
)

SUCCESSFUL_PAYMENT_STATUSES = (PaymentStatus.succeeded,)
ONLINE_FRESH_SECONDS = 24 * 60 * 60
LOCATIONS_CACHE_TTL_SECONDS = 30
_MISSING = object()
_SUSPENDED_STATUSES = {
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
}
_TERMINATED_STATUSES = {
    SubscriptionStatus.disabled,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.hidden,
    SubscriptionStatus.archived,
}
_locations_cache: tuple[float, list[dict[str, Any]]] | None = None


def coerce_subscriber_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def money(value: Any) -> float:
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def enum_value(value: Any) -> str | None:
    return getattr(value, "value", value)


def _uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def subscriber_name(subscriber: Subscriber) -> str:
    return (
        subscriber.name
        or subscriber.display_name
        or subscriber.email
        or str(subscriber.id)
    )


def address_text(
    subscriber: Subscriber, addresses: list[Address] | None = None
) -> str | None:
    address = service_address_service.pick_service_address(addresses)
    if address is not None:
        parts = [
            address.address_line1,
            address.address_line2,
            address.city,
            address.region,
            address.postal_code,
            address.country_code,
        ]
    else:
        parts = [
            subscriber.address_line1,
            subscriber.address_line2,
            subscriber.city,
            subscriber.region,
            subscriber.postal_code,
            subscriber.country_code,
        ]
    rendered = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
    return rendered or None


def location_label(db: Session, subscriber: Subscriber) -> str | None:
    if subscriber.pop_site_id:
        pop = db.get(PopSite, subscriber.pop_site_id)
        if pop:
            return pop.name
    return subscriber.city or subscriber.region or address_text(subscriber)


def location_id(db: Session, subscriber: Subscriber) -> str | None:
    if subscriber.pop_site_id:
        return str(subscriber.pop_site_id)
    label = location_label(db, subscriber)
    if not label:
        return None
    return f"address:{label.strip().lower()}"


def service_price(subscription: Subscription) -> float:
    if subscription.unit_price is not None:
        return money(subscription.unit_price)
    offer = subscription.offer
    if offer:
        recurring = next(
            (
                price
                for price in offer.prices
                if price.is_active and price.price_type == PriceType.recurring
            ),
            None,
        )
        if recurring:
            return money(recurring.amount)
    return 0.0


def service_speed(subscription: Subscription) -> str | None:
    offer = subscription.offer
    down = getattr(offer, "speed_download_mbps", None) if offer else None
    up = getattr(offer, "speed_upload_mbps", None) if offer else None
    if down and up:
        return f"{down}/{up} Mbps"
    if down:
        return f"{down} Mbps"
    profile = subscription.radius_profile
    if profile and (profile.download_speed or profile.upload_speed):
        profile_down = profile.download_speed
        profile_up = profile.upload_speed
        if profile_down and profile_up:
            return f"{profile_down}/{profile_up} Kbps"
        return f"{profile_down or profile_up} Kbps"
    return None


def service_record(subscription: Subscription) -> dict[str, Any]:
    offer = subscription.offer
    return {
        "service_id": str(subscription.id),
        "plan_name": offer.name if offer else subscription.service_description,
        "speed": service_speed(subscription),
        "status": enum_value(subscription.status),
        "activated_at": utc_iso(subscription.start_at),
        "price": service_price(subscription),
    }


def subscriber_services(db: Session, subscriber_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
            .options(
                joinedload(Subscription.offer).selectinload(CatalogOffer.prices),
                joinedload(Subscription.radius_profile),
            )
            .order_by(Subscription.created_at.desc())
        ).all()
    )
    return [service_record(row) for row in rows]


def services_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not subscriber_ids:
        return {}
    rows = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .options(
                joinedload(Subscription.offer).selectinload(CatalogOffer.prices),
                joinedload(Subscription.radius_profile),
            )
            .order_by(Subscription.created_at.desc())
        ).all()
    )
    mapped: dict[uuid.UUID, list[dict[str, Any]]] = {sid: [] for sid in subscriber_ids}
    for row in rows:
        mapped.setdefault(row.subscriber_id, []).append(service_record(row))
    return mapped


def _total_paid_query(db: Session, subscriber_id: uuid.UUID) -> Decimal:
    return Decimal(
        str(
            db.query(func.coalesce(func.sum(Payment.amount), 0))
            .filter(Payment.account_id == subscriber_id)
            .filter(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
            .filter(Payment.is_active.is_(True))
            .scalar()
            or 0
        )
    )


def _balance_query(db: Session, subscriber_id: uuid.UUID) -> Decimal:
    return open_invoice_balance(db, subscriber_id)


def _invoiced_until_query(db: Session, subscriber_id: uuid.UUID) -> datetime | None:
    return (
        db.query(func.max(Invoice.billing_period_end))
        .filter(Invoice.account_id == subscriber_id)
        .filter(Invoice.is_active.is_(True))
        .scalar()
    )


def _next_bill_date_query(db: Session, subscriber_id: uuid.UUID) -> datetime | None:
    return (
        db.query(func.min(Subscription.next_billing_at))
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.pending]
            )
        )
        .scalar()
    )


def _billing_start_query(db: Session, subscriber: Subscriber) -> datetime | None:
    if subscriber.account_start_date:
        return subscriber.account_start_date
    return (
        db.query(func.min(Subscription.start_at))
        .filter(Subscription.subscriber_id == subscriber.id)
        .scalar()
    )


def _blocked_date_query(db: Session, subscriber_id: uuid.UUID) -> datetime | None:
    lock_at = (
        db.query(func.max(EnforcementLock.created_at))
        .filter(EnforcementLock.subscriber_id == subscriber_id)
        .filter(
            EnforcementLock.reason.in_(
                [EnforcementReason.overdue, EnforcementReason.prepaid]
            )
        )
        .scalar()
    )
    dunning_at = (
        db.query(func.max(DunningCase.started_at))
        .filter(DunningCase.account_id == subscriber_id)
        .scalar()
    )
    values = [value for value in (lock_at, dunning_at) if value is not None]
    return max(values) if values else None


def billing_summary(db: Session, subscriber: Subscriber) -> dict[str, Any]:
    return {
        "balance": money(_balance_query(db, subscriber.id)),
        "next_bill_date": utc_iso(_next_bill_date_query(db, subscriber.id)),
        "billing_start_date": utc_iso(_billing_start_query(db, subscriber)),
        "invoiced_until": utc_iso(_invoiced_until_query(db, subscriber.id)),
        "blocked_date": utc_iso(_blocked_date_query(db, subscriber.id)),
        "total_paid": money(_total_paid_query(db, subscriber.id)),
    }


def _system_user_name(user: SystemUser) -> str:
    rendered = (
        user.display_name
        or f"{user.first_name or ''} {user.last_name or ''}".strip()
        or user.email
    )
    return rendered or str(user.id)


def _actor_map(db: Session, actor_ids: list[str | None]) -> dict[str, dict[str, Any]]:
    parsed_ids = sorted(
        {
            actor_id
            for actor_id in (_uuid_or_none(value) for value in actor_ids)
            if actor_id is not None
        }
    )
    if not parsed_ids:
        return {}
    rows = list(
        db.scalars(select(SystemUser).where(SystemUser.id.in_(parsed_ids))).all()
    )
    return {
        str(row.id): {
            "id": str(row.id),
            "name": _system_user_name(row),
            "email": row.email,
        }
        for row in rows
    }


def _actor_payload(
    actor_id: str | None, actors: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if not actor_id:
        return None
    resolved = actors.get(str(actor_id))
    if resolved:
        return resolved
    return {"id": str(actor_id), "name": None, "email": None}


def _service_extension_entry_payload(
    entry: ServiceExtensionEntry,
    *,
    subscriber: Subscriber | None = None,
) -> dict[str, Any]:
    return {
        "entry_id": str(entry.id),
        "subscriber_id": str(entry.subscriber_id),
        "customer_id": subscriber.splynx_customer_id if subscriber else None,
        "subscriber_number": subscriber.subscriber_number if subscriber else None,
        "name": subscriber_name(subscriber) if subscriber else None,
        "subscription_id": str(entry.subscription_id),
        "previous_next_billing_at": utc_iso(entry.previous_next_billing_at),
        "grant_starts_at": utc_iso(entry.grant_starts_at),
        "grant_ends_at": utc_iso(entry.grant_ends_at),
        "anchor_basis": enum_value(entry.anchor_basis),
        "new_next_billing_at": utc_iso(entry.new_next_billing_at),
        "created_at": utc_iso(entry.created_at),
    }


def _service_extension_payload(
    extension: ServiceExtension,
    *,
    actors: dict[str, dict[str, Any]] | None = None,
    entries: list[ServiceExtensionEntry] | None = None,
) -> dict[str, Any]:
    actor_lookup = actors or {}
    payload: dict[str, Any] = {
        "id": str(extension.id),
        "reason": extension.reason,
        "window_start": utc_iso(extension.window_start),
        "window_end": utc_iso(extension.window_end),
        "days": extension.days,
        "scope_type": enum_value(extension.scope_type),
        "scope_id": str(extension.scope_id) if extension.scope_id else None,
        "scope_subscriber_ids": extension.scope_subscriber_ids or [],
        "status": enum_value(extension.status),
        "affected_count": extension.affected_count,
        "skipped_count": extension.skipped_count,
        "created_by": _actor_payload(extension.created_by, actor_lookup),
        "applied_by": _actor_payload(extension.applied_by, actor_lookup),
        "applied_at": utc_iso(extension.applied_at),
        "created_at": utc_iso(extension.created_at),
    }
    if entries is not None:
        payload["affected_customers"] = [
            _service_extension_entry_payload(entry) for entry in entries
        ]
    return payload


def service_extension_rows(
    db: Session, *, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    total = db.scalar(select(func.count(ServiceExtension.id))) or 0
    rows = list(
        db.scalars(
            select(ServiceExtension)
            .order_by(
                func.coalesce(
                    ServiceExtension.applied_at,
                    ServiceExtension.created_at,
                ).desc(),
                ServiceExtension.id.desc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    actors = _actor_map(
        db,
        [actor for row in rows for actor in (row.created_by, row.applied_by)],
    )
    return [_service_extension_payload(row, actors=actors) for row in rows], int(total)


def service_extension_detail(db: Session, extension_id: str) -> dict[str, Any] | None:
    parsed = _uuid_or_none(extension_id)
    if parsed is None:
        return None
    extension = db.get(ServiceExtension, parsed)
    if extension is None:
        return None
    entries = list(
        db.scalars(
            select(ServiceExtensionEntry)
            .where(ServiceExtensionEntry.extension_id == extension.id)
            .order_by(ServiceExtensionEntry.created_at.desc())
        ).all()
    )
    subscribers = {
        row.id: row
        for row in db.scalars(
            select(Subscriber).where(
                Subscriber.id.in_([entry.subscriber_id for entry in entries])
            )
        ).all()
    }
    actors = _actor_map(db, [extension.created_by, extension.applied_by])
    payload = _service_extension_payload(extension, actors=actors)
    payload["affected_customers"] = [
        _service_extension_entry_payload(
            entry, subscriber=subscribers.get(entry.subscriber_id)
        )
        for entry in entries
    ]
    return payload


def service_extensions_for_subscriber(
    db: Session, subscriber_id: uuid.UUID
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(ServiceExtensionEntry)
            .where(ServiceExtensionEntry.subscriber_id == subscriber_id)
            .options(
                joinedload(ServiceExtensionEntry.extension),
            )
            .order_by(ServiceExtensionEntry.created_at.desc())
        ).all()
    )
    subscriber = db.get(Subscriber, subscriber_id)
    actors = _actor_map(
        db,
        [
            actor
            for row in rows
            for actor in (row.extension.created_by, row.extension.applied_by)
            if row.extension is not None
        ],
    )
    data = []
    for row in rows:
        item = _service_extension_payload(row.extension, actors=actors)
        item["entry"] = _service_extension_entry_payload(row, subscriber=subscriber)
        data.append(item)
    return data


def billing_by_subscriber(
    db: Session, subscribers: list[Subscriber]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not subscribers:
        return {}

    subscriber_ids = [subscriber.id for subscriber in subscribers]
    subscriber_by_id = {subscriber.id: subscriber for subscriber in subscribers}

    balances = {
        account_id: Decimal(str(value or 0))
        for account_id, value in db.execute(
            select(
                Invoice.account_id,
                func.coalesce(func.sum(Invoice.balance_due), 0),
            )
            .where(*open_invoice_filters_for_accounts(subscriber_ids))
            .group_by(Invoice.account_id)
        ).all()
    }
    invoiced_until = {
        account_id: value
        for account_id, value in db.execute(
            select(Invoice.account_id, func.max(Invoice.billing_period_end))
            .where(Invoice.account_id.in_(subscriber_ids))
            .where(Invoice.is_active.is_(True))
            .group_by(Invoice.account_id)
        ).all()
    }
    total_paid = {
        account_id: Decimal(str(value or 0))
        for account_id, value in db.execute(
            select(Payment.account_id, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.account_id.in_(subscriber_ids))
            .where(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
            .where(Payment.is_active.is_(True))
            .group_by(Payment.account_id)
        ).all()
    }
    next_bill_dates = {
        subscriber_id: value
        for subscriber_id, value in db.execute(
            select(Subscription.subscriber_id, func.min(Subscription.next_billing_at))
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .where(
                Subscription.status.in_(
                    [SubscriptionStatus.active, SubscriptionStatus.pending]
                )
            )
            .group_by(Subscription.subscriber_id)
        ).all()
    }
    subscription_starts = {
        subscriber_id: value
        for subscriber_id, value in db.execute(
            select(Subscription.subscriber_id, func.min(Subscription.start_at))
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .group_by(Subscription.subscriber_id)
        ).all()
    }
    lock_dates = {
        subscriber_id: value
        for subscriber_id, value in db.execute(
            select(EnforcementLock.subscriber_id, func.max(EnforcementLock.created_at))
            .where(EnforcementLock.subscriber_id.in_(subscriber_ids))
            .where(
                EnforcementLock.reason.in_(
                    [EnforcementReason.overdue, EnforcementReason.prepaid]
                )
            )
            .group_by(EnforcementLock.subscriber_id)
        ).all()
    }
    dunning_dates = {
        account_id: value
        for account_id, value in db.execute(
            select(DunningCase.account_id, func.max(DunningCase.started_at))
            .where(DunningCase.account_id.in_(subscriber_ids))
            .group_by(DunningCase.account_id)
        ).all()
    }

    summaries: dict[uuid.UUID, dict[str, Any]] = {}
    for subscriber_id in subscriber_ids:
        subscriber = subscriber_by_id[subscriber_id]
        blocked_values = [
            value
            for value in (
                lock_dates.get(subscriber_id),
                dunning_dates.get(subscriber_id),
            )
            if value is not None
        ]
        billing_start = subscriber.account_start_date or subscription_starts.get(
            subscriber_id
        )
        summaries[subscriber_id] = {
            "balance": money(balances.get(subscriber_id)),
            "next_bill_date": utc_iso(next_bill_dates.get(subscriber_id)),
            "billing_start_date": utc_iso(billing_start),
            "invoiced_until": utc_iso(invoiced_until.get(subscriber_id)),
            "blocked_date": utc_iso(max(blocked_values) if blocked_values else None),
            "total_paid": money(total_paid.get(subscriber_id)),
        }
    return summaries


def latest_payment(db: Session, subscriber_id: uuid.UUID) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.account_id == subscriber_id)
        .filter(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
        .filter(Payment.is_active.is_(True))
        .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
        .first()
    )


def latest_payments_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not subscriber_ids:
        return {}
    ranked = (
        select(
            Payment.account_id.label("account_id"),
            Payment.amount.label("amount"),
            Payment.paid_at.label("paid_at"),
            Payment.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=Payment.account_id,
                order_by=func.coalesce(Payment.paid_at, Payment.created_at).desc(),
            )
            .label("rank"),
        )
        .where(Payment.account_id.in_(subscriber_ids))
        .where(Payment.status.in_(SUCCESSFUL_PAYMENT_STATUSES))
        .where(Payment.is_active.is_(True))
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.account_id,
            ranked.c.amount,
            ranked.c.paid_at,
            ranked.c.created_at,
        ).where(ranked.c.rank == 1)
    ).all()
    return {
        account_id: {
            "last_payment_date": utc_iso(paid_at or created_at),
            "last_payment_amount": money(amount),
        }
        for account_id, amount, paid_at, created_at in rows
    }


def _latest_session_by_subscription(
    db: Session, subscription_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RadiusAccountingSession]:
    if not subscription_ids:
        return {}
    observed_at = func.coalesce(
        RadiusAccountingSession.last_update_at,
        RadiusAccountingSession.session_start,
        RadiusAccountingSession.created_at,
    )
    ranked = (
        select(
            RadiusAccountingSession.id.label("session_id"),
            func.row_number()
            .over(
                partition_by=RadiusAccountingSession.subscription_id,
                order_by=(
                    observed_at.desc(),
                    RadiusAccountingSession.id.desc(),
                ),
            )
            .label("rank"),
        )
        .where(RadiusAccountingSession.subscription_id.in_(subscription_ids))
        .subquery()
    )
    rows = list(
        db.scalars(
            select(RadiusAccountingSession)
            .join(ranked, ranked.c.session_id == RadiusAccountingSession.id)
            .where(ranked.c.rank == 1)
        ).all()
    )
    return {row.subscription_id: row for row in rows if row.subscription_id}


def latest_session_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RadiusAccountingSession]:
    if not subscriber_ids:
        return {}
    subs = list(
        db.execute(
            select(Subscription.id, Subscription.subscriber_id).where(
                Subscription.subscriber_id.in_(subscriber_ids)
            )
        ).all()
    )
    subscription_ids = [row[0] for row in subs]
    sessions = _latest_session_by_subscription(db, subscription_ids)
    mapped: dict[uuid.UUID, RadiusAccountingSession] = {}
    for subscription_id, subscriber_id in subs:
        session = sessions.get(subscription_id)
        if not session:
            continue
        current = mapped.get(subscriber_id)
        session_seen = (
            session.last_update_at or session.session_start or session.created_at
        )
        current_seen = (
            current.last_update_at or current.session_start or current.created_at
            if current
            else None
        )
        if current is None or (
            session_seen and (current_seen is None or session_seen > current_seen)
        ):
            mapped[subscriber_id] = session
    return mapped


def session_last_seen(session: RadiusAccountingSession | None) -> datetime | None:
    if session is None:
        return None
    return session.last_update_at or session.session_start or session.created_at


def session_state(session: RadiusAccountingSession | None) -> str:
    if session is None:
        return "offline"
    if session.status_type == AccountingStatus.stop or session.session_end is not None:
        return "offline"
    seen = session_last_seen(session)
    if seen is None:
        return "offline"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return "online" if seen >= cutoff_ago(ONLINE_FRESH_SECONDS) else "stale"


def cutoff_ago(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _first_status_event(
    db: Session, subscriber_id: uuid.UUID, statuses: set[SubscriptionStatus]
) -> datetime | None:
    subscription_ids = list(
        db.scalars(
            select(Subscription.id).where(Subscription.subscriber_id == subscriber_id)
        ).all()
    )
    if not subscription_ids:
        return None
    return (
        db.query(func.min(SubscriptionLifecycleEvent.created_at))
        .filter(SubscriptionLifecycleEvent.subscription_id.in_(subscription_ids))
        .filter(SubscriptionLifecycleEvent.to_status.in_(list(statuses)))
        .scalar()
    )


def _first_status_events_by_subscriber(
    db: Session, subscriber_ids: list[uuid.UUID], statuses: set[SubscriptionStatus]
) -> dict[uuid.UUID, datetime]:
    if not subscriber_ids:
        return {}
    return {
        subscriber_id: created_at
        for subscriber_id, created_at in db.execute(
            select(
                Subscription.subscriber_id,
                func.min(SubscriptionLifecycleEvent.created_at),
            )
            .join(
                Subscription,
                Subscription.id == SubscriptionLifecycleEvent.subscription_id,
            )
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .where(SubscriptionLifecycleEvent.to_status.in_(list(statuses)))
            .group_by(Subscription.subscriber_id)
        ).all()
    }


def subscriber_payload_metadata(
    db: Session, subscribers: list[Subscriber]
) -> dict[uuid.UUID, dict[str, Any]]:
    if not subscribers:
        return {}
    subscriber_ids = [subscriber.id for subscriber in subscribers]
    subscription_starts = {
        subscriber_id: start_at
        for subscriber_id, start_at in db.execute(
            select(Subscription.subscriber_id, func.min(Subscription.start_at))
            .where(Subscription.subscriber_id.in_(subscriber_ids))
            .group_by(Subscription.subscriber_id)
        ).all()
    }
    suspended_dates = _first_status_events_by_subscriber(
        db, subscriber_ids, _SUSPENDED_STATUSES
    )
    terminated_dates = _first_status_events_by_subscriber(
        db, subscriber_ids, _TERMINATED_STATUSES
    )
    pop_site_ids = sorted(
        {subscriber.pop_site_id for subscriber in subscribers if subscriber.pop_site_id}
    )
    pop_names = (
        {
            pop_id: name
            for pop_id, name in db.execute(
                select(PopSite.id, PopSite.name).where(PopSite.id.in_(pop_site_ids))
            ).all()
        }
        if pop_site_ids
        else {}
    )
    metadata: dict[uuid.UUID, dict[str, Any]] = {}
    for subscriber in subscribers:
        pop_name = pop_names.get(subscriber.pop_site_id)
        metadata[subscriber.id] = {
            "activated_at": (
                subscriber.account_start_date
                or subscription_starts.get(subscriber.id)
                or subscriber.created_at
            ),
            "suspended_at": suspended_dates.get(subscriber.id),
            "terminated_at": terminated_dates.get(subscriber.id),
            "location": (
                pop_name
                if pop_name
                else subscriber.city or subscriber.region or address_text(subscriber)
            ),
        }
    return metadata


def subscriber_payload(
    db: Session,
    subscriber: Subscriber,
    *,
    services: list[dict[str, Any]] | object = _MISSING,
    billing: dict[str, Any] | object = _MISSING,
    session: RadiusAccountingSession | None = None,
    include_session_state: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if metadata is None:
        activated_at = (
            subscriber.account_start_date
            or db.query(func.min(Subscription.start_at))
            .filter(Subscription.subscriber_id == subscriber.id)
            .scalar()
            or subscriber.created_at
        )
        suspended_at = _first_status_event(db, subscriber.id, _SUSPENDED_STATUSES)
        terminated_at = _first_status_event(db, subscriber.id, _TERMINATED_STATUSES)
        location = location_label(db, subscriber)
    else:
        activated_at = metadata.get("activated_at")
        suspended_at = metadata.get("suspended_at")
        terminated_at = metadata.get("terminated_at")
        location = metadata.get("location")
    row: dict[str, Any] = {
        "id": str(subscriber.id),
        "subscriber_number": subscriber.subscriber_number or subscriber.account_number,
        "name": subscriber_name(subscriber),
        "email": subscriber.email,
        "phone": subscriber.phone,
        "status": enum_value(subscriber.status),
        "billing_mode": enum_value(subscriber.billing_mode),
        "billing_day": subscriber.billing_day,
        "address": address_text(subscriber, list(subscriber.addresses or [])),
        "location": location,
        "created_at": utc_iso(subscriber.created_at),
        "activated_at": utc_iso(activated_at),
        "suspended_at": utc_iso(suspended_at),
        "terminated_at": utc_iso(terminated_at),
        "last_seen": utc_iso(session_last_seen(session)),
    }
    if services is not _MISSING:
        row["services"] = services
    if billing is not _MISSING:
        row["billing"] = billing
    if include_session_state:
        row["session_state"] = session_state(session)
    return row


def get_subscriber_or_none(db: Session, subscriber_id: str) -> Subscriber | None:
    parsed = coerce_subscriber_id(subscriber_id)
    if parsed is None:
        return None
    return db.get(Subscriber, parsed)


def list_subscribers(
    db: Session, *, page: int, per_page: int
) -> tuple[list[Subscriber], int]:
    stmt = (
        select(Subscriber)
        .options(selectinload(Subscriber.addresses))
        .order_by(Subscriber.created_at.desc(), Subscriber.id.desc())
    )
    total = db.query(func.count(Subscriber.id)).scalar() or 0
    rows = list(db.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all())
    return rows, int(total)


def search_subscribers(
    db: Session, q: str, *, page: int, per_page: int
) -> tuple[list[Subscriber], int]:
    like = f"%{q.strip()}%"
    predicate = or_(
        Subscriber.first_name.ilike(like),
        Subscriber.last_name.ilike(like),
        Subscriber.display_name.ilike(like),
        Subscriber.company_name.ilike(like),
        Subscriber.email.ilike(like),
        Subscriber.subscriber_number.ilike(like),
        Subscriber.account_number.ilike(like),
    )
    total = db.query(func.count(Subscriber.id)).filter(predicate).scalar() or 0
    rows = list(
        db.scalars(
            select(Subscriber)
            .where(predicate)
            .options(selectinload(Subscriber.addresses))
            .order_by(Subscriber.created_at.desc(), Subscriber.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    return rows, int(total)


def locations(
    db: Session, *, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    global _locations_cache
    now = monotonic()
    if _locations_cache is not None:
        cached_at, cached = _locations_cache
        if now - cached_at < LOCATIONS_CACHE_TTL_SECONDS:
            start = (page - 1) * per_page
            return [dict(item) for item in cached[start : start + per_page]], len(
                cached
            )

    result: dict[str, dict[str, str]] = {}
    for pop in db.scalars(
        select(PopSite).where(PopSite.is_active.is_(True)).order_by(PopSite.name)
    ).all():
        result[str(pop.id)] = {"id": str(pop.id), "name": pop.name}
    rows = db.query(Subscriber.city, Subscriber.region).distinct().all()
    for city, region in rows:
        label = city or region
        if not label:
            continue
        key = f"address:{str(label).strip().lower()}"
        result.setdefault(key, {"id": key, "name": str(label)})
    ordered = sorted(result.values(), key=lambda item: item["name"].lower())
    _locations_cache = (now, [dict(item) for item in ordered])
    start = (page - 1) * per_page
    return ordered[start : start + per_page], len(ordered)


def billing_risk_rows(
    db: Session, *, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    subscribers, total = list_subscribers(db, page=page, per_page=per_page)
    subscriber_ids = [item.id for item in subscribers]
    services = services_by_subscriber(db, subscriber_ids)
    billing = billing_by_subscriber(db, subscribers)
    payments = latest_payments_by_subscriber(db, subscriber_ids)
    pop_site_ids = {
        subscriber.pop_site_id for subscriber in subscribers if subscriber.pop_site_id
    }
    pop_names = (
        {
            pop_id: name
            for pop_id, name in db.execute(
                select(PopSite.id, PopSite.name).where(PopSite.id.in_(pop_site_ids))
            ).all()
        }
        if pop_site_ids
        else {}
    )
    rows: list[dict[str, Any]] = []
    for subscriber in subscribers:
        primary_service = next(iter(services.get(subscriber.id, [])), {})
        summary = billing[subscriber.id]
        payment = payments.get(subscriber.id, {})
        location = (
            pop_names.get(subscriber.pop_site_id)
            if subscriber.pop_site_id
            else subscriber.city or subscriber.region or address_text(subscriber)
        )
        rows.append(
            {
                "id": str(subscriber.id),
                "name": subscriber_name(subscriber),
                "email": subscriber.email,
                "phone": subscriber.phone,
                "status": enum_value(subscriber.status),
                "location": location,
                "service_plan": primary_service.get("plan_name"),
                "speed": primary_service.get("speed"),
                # Primary-service monthly price so bulk consumers (e.g. omni's
                # billing-risk cache) can read MRR here instead of an N+1
                # per-subscriber GET /subscribers/{id}/services.
                "service_mrr": primary_service.get("price"),
                "balance": summary["balance"],
                "next_bill_date": summary["next_bill_date"],
                "billing_start_date": summary["billing_start_date"],
                "invoiced_until": summary["invoiced_until"],
                "blocked_date": summary["blocked_date"],
                "total_paid": summary["total_paid"],
                "last_payment_date": payment.get("last_payment_date"),
                "last_payment_amount": payment.get("last_payment_amount", 0.0),
            }
        )
    return rows, total


def online_subscribers(
    db: Session, *, page: int, per_page: int
) -> tuple[list[dict[str, Any]], int]:
    cutoff = cutoff_ago(ONLINE_FRESH_SECONDS)
    last_seen = func.coalesce(
        RadiusAccountingSession.last_update_at,
        RadiusAccountingSession.session_start,
        RadiusAccountingSession.created_at,
    )
    ranked = (
        select(
            Subscriber.id.label("subscriber_id"),
            Subscriber.subscriber_number,
            Subscriber.account_number,
            Subscriber.status,
            last_seen.label("last_seen"),
            func.row_number()
            .over(partition_by=Subscriber.id, order_by=last_seen.desc())
            .label("session_rank"),
        )
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .join(
            RadiusAccountingSession,
            RadiusAccountingSession.subscription_id == Subscription.id,
        )
        .where(
            Subscription.status == SubscriptionStatus.active,
            RadiusAccountingSession.status_type != AccountingStatus.stop,
            RadiusAccountingSession.session_end.is_(None),
            last_seen >= cutoff,
        )
        .subquery()
    )
    latest = select(ranked).where(ranked.c.session_rank == 1).subquery()
    rows = db.execute(
        select(latest, func.count().over().label("total"))
        .order_by(latest.c.subscriber_id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    total = (
        int(rows[0].total)
        if rows
        else int(db.scalar(select(func.count()).select_from(latest)) or 0)
    )
    return (
        [
            {
                "id": str(row.subscriber_id),
                "subscriber_number": row.subscriber_number or row.account_number,
                "status": enum_value(row.status),
                "last_seen": utc_iso(row.last_seen),
            }
            for row in rows
        ],
        total,
    )


def transaction_rows(
    db: Session,
    *,
    customer_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(InvoiceLine).join(Invoice, InvoiceLine.invoice_id == Invoice.id)
    count_stmt = select(func.count(InvoiceLine.id)).join(
        Invoice, InvoiceLine.invoice_id == Invoice.id
    )
    predicates: list[ColumnElement[bool]] = [
        InvoiceLine.is_active.is_(True),
        Invoice.is_active.is_(True),
    ]
    if customer_id:
        predicates.append(Invoice.account_id == customer_id)
    if date_from:
        predicates.append(
            func.coalesce(Invoice.issued_at, Invoice.created_at) >= date_from
        )
    if date_to:
        predicates.append(
            func.coalesce(Invoice.issued_at, Invoice.created_at) <= date_to
        )
    for predicate in predicates:
        stmt = stmt.where(predicate)
        count_stmt = count_stmt.where(predicate)
    total = db.scalar(count_stmt) or 0
    lines = list(
        db.scalars(
            stmt.options(joinedload(InvoiceLine.invoice))
            .order_by(func.coalesce(Invoice.issued_at, Invoice.created_at).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    rows = []
    for line in lines:
        invoice = line.invoice
        rows.append(
            {
                "id": str(line.id),
                "customer_id": str(invoice.account_id),
                "service_id": str(line.subscription_id)
                if line.subscription_id
                else None,
                "date": utc_iso(invoice.issued_at or invoice.created_at),
                "description": line.description,
                "price": money(line.amount),
                "period_from": utc_iso(invoice.billing_period_start),
                "period_to": utc_iso(invoice.billing_period_end),
            }
        )
    return rows, int(total)


def payment_rows(
    db: Session,
    *,
    customer_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Payment).where(Payment.is_active.is_(True))
    count_stmt = select(func.count(Payment.id)).where(Payment.is_active.is_(True))
    predicates = []
    if customer_id:
        predicates.append(Payment.account_id == customer_id)
    if date_from:
        predicates.append(
            func.coalesce(Payment.paid_at, Payment.created_at) >= date_from
        )
    if date_to:
        predicates.append(func.coalesce(Payment.paid_at, Payment.created_at) <= date_to)
    for predicate in predicates:
        stmt = stmt.where(predicate)
        count_stmt = count_stmt.where(predicate)
    total = db.scalar(count_stmt) or 0
    payments = list(
        db.scalars(
            stmt.options(
                joinedload(Payment.payment_method), joinedload(Payment.payment_channel)
            )
            .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        ).all()
    )
    rows = []
    for payment in payments:
        method = None
        if payment.payment_channel:
            method = payment.payment_channel.name
        elif payment.payment_method:
            method = enum_value(payment.payment_method.method_type)
        rows.append(
            {
                "id": str(payment.id),
                "customer_id": str(payment.account_id) if payment.account_id else None,
                "amount": money(payment.amount),
                "date": utc_iso(payment.paid_at or payment.created_at),
                "method": method or "unknown",
            }
        )
    return rows, int(total)


def session_rows(db: Session, subscriber_id: uuid.UUID) -> list[dict[str, Any]]:
    subscription_ids = list(
        db.scalars(
            select(Subscription.id).where(Subscription.subscriber_id == subscriber_id)
        ).all()
    )
    if not subscription_ids:
        return []
    sessions = list(
        db.scalars(
            select(RadiusAccountingSession)
            .where(RadiusAccountingSession.subscription_id.in_(subscription_ids))
            .order_by(
                func.coalesce(
                    RadiusAccountingSession.session_start,
                    RadiusAccountingSession.created_at,
                ).desc()
            )
        ).all()
    )
    rows = []
    for session in sessions:
        start = session.session_start or session.created_at
        end = session.session_end
        duration = None
        if start and end:
            duration = max(int((end - start).total_seconds()), 0)
        elif start and session.last_update_at:
            duration = max(int((session.last_update_at - start).total_seconds()), 0)
        rows.append(
            {
                "session_id": session.session_id,
                "start_time": utc_iso(start),
                "end_time": utc_iso(end),
                "duration_seconds": duration,
                "bytes_downloaded": int(session.output_octets or 0),
                "bytes_uploaded": int(session.input_octets or 0),
            }
        )
    return rows


def log_status_writeback(
    db: Session,
    *,
    subscriber_id: uuid.UUID,
    actor: str | None,
    source: str | None,
    reason: str | None,
    requested_status: str | None,
    previous_status: str | None,
    result: str,
    status_code: int,
) -> None:
    stage_audit_event(
        db,
        actor_type=AuditActorType.service,
        actor_id=actor or "crm",
        action="crm.subscriber_status_writeback",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        status_code=status_code,
        is_success=status_code < 400,
        metadata={
            "source": source,
            "reason": reason,
            "requested_status": requested_status,
            "previous_status": previous_status,
            "result": result,
        },
    )


def disable_subscriber_from_crm(
    db: Session,
    subscriber: Subscriber,
    *,
    actor: str | None,
    source: str | None,
    reason: str | None,
) -> dict[str, Any]:
    previous_status = enum_value(subscriber.status)
    transition_account_status(
        db,
        str(subscriber.id),
        SubscriberStatus.disabled,
        reason=reason or "Disabled from CRM ingress",
        source=actor or source or "crm",
        emit=False,
    )
    log_status_writeback(
        db,
        subscriber_id=subscriber.id,
        actor=actor,
        source=source,
        reason=reason,
        requested_status="disabled",
        previous_status=previous_status,
        result="updated",
        status_code=200,
    )
    db.commit()
    db.refresh(subscriber)
    return {"id": str(subscriber.id), "status": enum_value(subscriber.status)}


def update_subscriber_status_from_crm(
    db: Session,
    subscriber: Subscriber,
    *,
    requested_status: str,
    actor: str | None,
    source: str | None,
    reason: str | None,
) -> dict[str, Any]:
    current = subscriber.status
    current_value = current.value if current else None
    if current in {SubscriberStatus.disabled, SubscriberStatus.canceled}:
        log_status_writeback(
            db,
            subscriber_id=subscriber.id,
            actor=actor,
            source=source,
            reason=reason,
            requested_status=requested_status,
            previous_status=current_value,
            result="already_terminal",
            status_code=200,
        )
        db.commit()
        return {"id": str(subscriber.id), "status": current_value}

    if current not in {SubscriberStatus.blocked, SubscriberStatus.suspended}:
        log_status_writeback(
            db,
            subscriber_id=subscriber.id,
            actor=actor,
            source=source,
            reason=reason,
            requested_status=requested_status,
            previous_status=current_value,
            result="rejected_transition",
            status_code=status.HTTP_409_CONFLICT,
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Subscriber can only be disabled from blocked, suspended, "
                "or nonpayment_suspended state."
            ),
        )

    return disable_subscriber_from_crm(
        db,
        subscriber,
        actor=actor,
        source=source,
        reason=reason,
    )


def create_account_credit(
    db: Session,
    *,
    subscriber_id: str,
    amount: Decimal,
    reason: str | None = None,
    external_ref: str | None = None,
    currency: str = "NGN",
):
    """Issue an account credit (an *issued* credit note) on a subscriber's
    billing account. Used by the CRM to pay out referral rewards.

    Idempotent on ``external_ref`` (embedded in the memo): a repeat call returns
    the existing credit note rather than double-crediting. Raises ``LookupError``
    when the subscriber does not exist.
    """
    from app.models.billing import CreditNote, CreditNoteStatus
    from app.schemas.billing import CreditNoteIssuePreviewRequest
    from app.services.billing.credit_notes import CreditNotes

    sub_uuid = coerce_subscriber_id(str(subscriber_id))
    if sub_uuid is None:
        raise LookupError("subscriber_not_found")
    subscriber = db.get(Subscriber, sub_uuid)
    if subscriber is None or not subscriber.is_active:
        raise LookupError("subscriber_not_found")

    ref_marker = f"[ref:{external_ref}]" if external_ref else ""
    if external_ref:
        existing = (
            db.query(CreditNote)
            .filter(CreditNote.account_id == sub_uuid)
            .filter(CreditNote.is_active.is_(True))
            .filter(CreditNote.status == CreditNoteStatus.issued)
            .filter(CreditNote.memo.ilike(f"%{ref_marker}%"))
            .order_by(CreditNote.created_at.desc())
            .first()
        )
        if existing is not None:
            if (
                round_money(existing.total) != round_money(amount)
                or existing.currency.upper() != currency.upper()
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "external_ref was already used for a different account "
                        "credit confirmation"
                    ),
                )
            return existing

    memo = (reason or "Referral reward").strip()
    if ref_marker:
        memo = f"{memo} {ref_marker}"

    operation_id = (
        uuid.uuid5(uuid.NAMESPACE_URL, f"crm-account-credit:{external_ref}")
        if external_ref
        else uuid.uuid4()
    )
    return CreditNotes.issue_system(
        db,
        CreditNoteIssuePreviewRequest(
            account_id=sub_uuid,
            currency=currency,
            subtotal=amount,
            total=amount,
            memo=memo,
            line_description=reason or "Referral reward",
        ),
        idempotency_key=f"crm-credit-{operation_id}",
        commit=True,
    ).credit_note


def create_installation_invoice(
    db: Session,
    *,
    subscriber_id: str,
    amount: Decimal,
    description: str,
    external_ref: str | None = None,
    currency: str = "NGN",
    commit: bool = True,
) -> Invoice:
    """Create a one-time installation invoice (header + single line) for a
    CRM-driven subscriber. Replaces the old Splynx installation-invoice path.

    Idempotent on ``external_ref``: a repeat call with the same ref returns the
    existing invoice rather than creating a duplicate. Raises ``LookupError``
    when the subscriber does not exist.
    """
    from app.services.billing.invoices import next_invoice_number
    from app.services.billing_adapter import (
        BillingAdapter,
        InvoiceIntent,
        InvoiceLineIntent,
    )

    sub_uuid = coerce_subscriber_id(str(subscriber_id))
    subscriber = db.get(Subscriber, sub_uuid) if sub_uuid else None
    if subscriber is None or not subscriber.is_active:
        raise LookupError("subscriber_not_found")

    if external_ref:
        existing = _find_invoice_by_crm_ref(db, external_ref)
        if existing is not None:
            return existing

    from app.services.billing_settings import resolve_payment_due_days

    issued_at = datetime.now(UTC)
    due_days = resolve_payment_due_days(db, subscriber=subscriber)
    intent = InvoiceIntent(
        account_id=subscriber.id,
        invoice_number=next_invoice_number(db),
        currency=currency,
        total=amount,
        memo=description,
        status=InvoiceStatus.issued,
        issued_at=issued_at,
        due_at=issued_at + timedelta(days=due_days),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref=f"account:{subscriber.id}:installation",
        due_date_policy_version="billing-payment-terms-v1",
    )
    invoice = BillingAdapter().create_invoice_with_lines(
        db,
        intent,
        [
            InvoiceLineIntent(
                description=description, quantity=Decimal("1"), unit_price=amount
            )
        ],
        commit=commit,
    )
    metadata = dict(invoice.metadata_ or {})
    metadata["source"] = "dotmac_crm"
    if external_ref:
        metadata["crm_external_ref"] = str(external_ref)
        invoice.crm_external_ref = str(external_ref)
    invoice.metadata_ = metadata
    db.add(invoice)
    from sqlalchemy.exc import IntegrityError

    if commit:
        try:
            db.commit()
        except IntegrityError:
            # A concurrent duplicate lost the race on
            # uq_invoices_active_crm_external_ref — the create is idempotent,
            # so return the invoice the winner wrote.
            db.rollback()
            if external_ref:
                existing = _find_invoice_by_crm_ref(db, external_ref)
                if existing is not None:
                    return existing
            raise
        db.refresh(invoice)
    else:
        db.flush()
    return invoice


def _find_invoice_by_crm_ref(db: Session, external_ref: str) -> Invoice | None:
    """Locate the active CRM-created invoice for an external_ref (the
    uq_invoices_active_crm_external_ref key)."""
    return (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.crm_external_ref == str(external_ref))
        .order_by(Invoice.created_at.desc())
        .first()
    )


def outage_impact(
    session: Session,
    *,
    node: NetworkDevice | None = None,
    basestation: PopSite | None = None,
    olt_id: str | uuid.UUID | None = None,
    pon_port_id: str | uuid.UUID | None = None,
    fdh_id: str | uuid.UUID | None = None,
) -> dict:
    """Subscribers affected by a failed asset, with a coverage report.

    Handles five asset granularities, deduped into one subscriber set:
      - ``node``/``basestation`` — the LLDP topology (``affected_customers``),
        which expands a switch/router/cabinet failure downstream.
      - ``olt_id`` — every active ONT on the OLT (all PON ports).
      - ``pon_port_id`` — only the ONTs on that PON port (a subset of the OLT).
      - ``fdh_id`` — active subscriptions behind an FDH cabinet.

    OLT and PON-port resolution read straight off ``OntAssignment`` and so are
    vendor-agnostic (Huawei and Ubiquiti alike). The coverage block is
    deliberate: impact is only as complete as the e2e topology, and the
    resolvers under-report rather than over-report when links are missing — so
    we surface where the chain dead-ends (an asset that yields zero subscribers)
    to let the caller trust the list or fall back to manual selection.
    """
    from app.models.network import FdhCabinet, OntAssignment, OntUnit
    from app.services.topology.affected import (
        affected_customers,
        fdh_impact_rows,
    )

    subscribers: dict = {}
    gaps: list[dict] = []
    detailed_rows: list[dict] = []
    resolved_node_count = 0

    def _add(s) -> None:
        if s is None:
            return
        subscribers[s.id] = {
            "id": str(s.id),
            "subscriber_number": s.subscriber_number or s.account_number,
            "name": subscriber_name(s),
            "email": s.email,
            "phone": s.phone,
        }

    def _add_by_subscriber_ids(rows) -> None:
        for (sub_id,) in rows:
            if sub_id is not None:
                _add(session.get(Subscriber, sub_id))

    if node is not None or basestation is not None:
        result = affected_customers(session, node=node, basestation=basestation)
        for sub in result["subscriptions"]:
            _add(getattr(sub, "subscriber", None))
        node_ids = result.get("node_ids") or set()
        resolved_node_count = len(node_ids)
        # Reuse the per-node resolution affected_customers already computed
        # instead of re-running subscriptions_for_node once per node. A node
        # that resolves subscribers through ANY arm (nas/olt/wireless) is not
        # a coverage gap: bad-edge detection (e.g. a stray CPE -> AP edge
        # masking a broken nas/olt mapping) belongs to the reconciler/drift
        # layer, not this outage report.
        subscriptions_by_node = result.get("subscriptions_by_node") or {}
        for nid in node_ids:
            n = session.get(NetworkDevice, nid)
            if (
                n is not None
                and n.matched_device_type in ("olt", "nas")
                and not subscriptions_by_node.get(nid)
            ):
                gaps.append(
                    {
                        "node_id": str(nid),
                        "name": n.name,
                        "matched_type": n.matched_device_type,
                    }
                )

    if olt_id is not None:
        before = len(subscribers)
        ont_ids = [
            r[0]
            for r in session.query(OntUnit.id)
            .filter(OntUnit.olt_device_id == olt_id)
            .all()
        ]
        if ont_ids:
            _add_by_subscriber_ids(
                session.query(OntAssignment.subscriber_id)
                .filter(
                    OntAssignment.ont_unit_id.in_(ont_ids),
                    OntAssignment.active.is_(True),
                    OntAssignment.subscriber_id.isnot(None),
                )
                .all()
            )
        if len(subscribers) == before:
            gaps.append(
                {
                    "olt_id": str(olt_id),
                    "reason": "no active ONT assignments on this OLT",
                }
            )

    if pon_port_id is not None:
        before = len(subscribers)
        _add_by_subscriber_ids(
            session.query(OntAssignment.subscriber_id)
            .filter(
                OntAssignment.pon_port_id == pon_port_id,
                OntAssignment.active.is_(True),
                OntAssignment.subscriber_id.isnot(None),
            )
            .all()
        )
        if len(subscribers) == before:
            gaps.append(
                {
                    "pon_port_id": str(pon_port_id),
                    "reason": "no active ONT assignments on this PON port",
                }
            )

    if fdh_id is not None:
        before = len(subscribers)
        fdh = session.get(FdhCabinet, fdh_id)
        if fdh is not None:
            detailed_rows = fdh_impact_rows(session, fdh)
            for row in detailed_rows:
                subscriber_id = row.get("subscriber_id")
                if subscriber_id is None:
                    continue
                subscribers[subscriber_id] = {
                    "id": str(subscriber_id),
                    "subscriber_number": row.get("subscriber_number"),
                    "name": row.get("subscriber_name"),
                    "email": row.get("email"),
                    "phone": row.get("phone"),
                }
        if len(subscribers) == before:
            gaps.append(
                {
                    "fdh_id": str(fdh_id),
                    "reason": "no active subscriptions mapped to this FDH",
                }
            )

    payload = {
        "subscribers": list(subscribers.values()),
        "count": len(subscribers),
        "coverage": {
            "resolved_node_count": resolved_node_count,
            "nodes_without_subscribers": gaps,
            "has_topology_gaps": bool(gaps),
        },
    }
    if detailed_rows:
        payload["impact_rows"] = detailed_rows
    return payload


def list_infrastructure_assets(
    session: Session, *, q: str | None = None, limit: int = 1000
) -> list[dict]:
    """Pickable infrastructure items for raising an outage/infrastructure ticket:
    OLTs (Huawei/Ubiquiti), their PON ports, and basestations. Each item's id +
    type map onto the outage-impact resolver."""
    from app.models.network import OLTDevice, PonPort

    like = f"%{q.strip()}%" if q and q.strip() else None
    assets: list[dict] = []

    olt_query = session.query(OLTDevice)
    if like is not None:
        olt_query = olt_query.filter(OLTDevice.name.ilike(like))
    olts = olt_query.order_by(OLTDevice.name).limit(limit).all()
    olt_by_id = {o.id: o for o in olts}
    for olt in olts:
        vendor = (olt.vendor or "").strip()
        assets.append(
            {
                "id": str(olt.id),
                "type": "olt",
                "label": f"{olt.name} ({vendor})" if vendor else olt.name,
                "vendor": vendor or None,
            }
        )

    # Only list PON ports for OLTs we can attribute (search mode = all matching
    # OLTs; otherwise the OLTs we already returned) — avoids dumping thousands.
    ports: list = []
    if olt_by_id:
        ports = (
            session.query(PonPort)
            .filter(PonPort.olt_id.in_(list(olt_by_id.keys())))
            .order_by(PonPort.name)
            .limit(limit)
            .all()
        )
    for port in ports:
        parent: OLTDevice | None = olt_by_id.get(port.olt_id) or session.get(
            OLTDevice, port.olt_id
        )
        olt_name = parent.name if parent else "OLT"
        vendor = ((parent.vendor if parent else None) or "").strip()
        assets.append(
            {
                "id": str(port.id),
                "type": "pon_port",
                "label": f"{olt_name} - {port.name}",
                "olt_id": str(port.olt_id),
                "vendor": vendor or None,
            }
        )

    bs_query = session.query(PopSite).filter(PopSite.zabbix_group_id.isnot(None))
    if like is not None:
        bs_query = bs_query.filter(PopSite.name.ilike(like))
    for pop in bs_query.order_by(PopSite.name).limit(limit).all():
        assets.append({"id": str(pop.id), "type": "basestation", "label": pop.name})

    return assets


def record_external_payment(
    session: Session,
    *,
    subscriber_id: str,
    amount: Any,
    external_ref: str,
    paid_at: datetime | None = None,
    memo: str | None = None,
    invoice_external_ref: str | None = None,
    currency: str = "NGN",
    commit: bool = True,
) -> Any:
    """Record a payment the customer made in the CRM (installation / subscription)
    into this app's ledger, so it settles the matching invoice and shows in the
    customer portal. Idempotent on ``external_ref`` — a repeat push returns the
    already-recorded payment. When ``invoice_external_ref`` matches a CRM-created
    invoice it's allocated there; otherwise it auto-allocates to oldest unpaid.
    """
    from decimal import Decimal, InvalidOperation

    from app.models.billing import Invoice, Payment, PaymentStatus
    from app.schemas.billing import PaymentAllocationApply, PaymentCreate
    from app.services import billing as billing_service

    sub_uuid = coerce_subscriber_id(str(subscriber_id))
    subscriber = session.get(Subscriber, sub_uuid) if sub_uuid else None
    if subscriber is None:
        raise LookupError("subscriber not found")

    try:
        amt = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("amount must be a number") from exc
    if amt <= 0:
        raise ValueError("amount must be greater than 0")

    ext_id = f"crm:{external_ref}"
    existing = session.query(Payment).filter(Payment.external_id == ext_id).first()
    if existing is not None:
        return existing

    allocations: list[PaymentAllocationApply] | None = None
    if invoice_external_ref:
        for inv in (
            session.query(Invoice)
            .filter(Invoice.account_id == subscriber.id, Invoice.is_active.is_(True))
            .order_by(Invoice.created_at.desc())
            .all()
        ):
            if (inv.metadata_ or {}).get("crm_external_ref") == str(
                invoice_external_ref
            ):
                due = inv.balance_due if inv.balance_due is not None else amt
                if due and due > 0:
                    allocations = [
                        PaymentAllocationApply(invoice_id=inv.id, amount=min(amt, due))
                    ]
                break

    payload = PaymentCreate(
        account_id=subscriber.id,
        amount=amt,
        currency=(currency or "NGN").upper(),
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        external_id=ext_id,
        memo=memo or "CRM sales payment",
        allocations=allocations,
    )
    from sqlalchemy.exc import IntegrityError

    try:
        return billing_service.payments.create(
            session,
            payload,
            auto_allocate=(allocations is None),
            commit=commit,
        )
    except IntegrityError:
        if not commit:
            raise
        # A concurrent /crm/payments push won the race on
        # uq_payments_active_crm_external_id. The write is idempotent — roll back
        # our losing insert and return the already-recorded payment.
        session.rollback()
        existing = session.query(Payment).filter(Payment.external_id == ext_id).first()
        if existing is not None:
            return existing
        raise


def list_catalog_offers(
    session: Session,
    *,
    q: str | None = None,
    active_only: bool = True,
    limit: int = 500,
) -> list[dict]:
    """The subscriber-facing plan catalog (offers + their recurring price) for the
    CRM to read, so a sales quote can pick a real offer instead of the CRM
    maintaining a parallel plan list. sub is the source of truth for plans."""
    from app.models.catalog import CatalogOffer, OfferPrice, PriceType

    query = session.query(CatalogOffer)
    if active_only:
        query = query.filter(CatalogOffer.is_active.is_(True))
    if q and q.strip():
        query = query.filter(CatalogOffer.name.ilike(f"%{q.strip()}%"))
    offers = query.order_by(CatalogOffer.name).limit(limit).all()
    if not offers:
        return []

    prices: dict = {}
    for price in (
        session.query(OfferPrice)
        .filter(
            OfferPrice.offer_id.in_([o.id for o in offers]),
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
        .all()
    ):
        prices.setdefault(price.offer_id, price)  # first active recurring price wins

    out: list[dict] = []
    for offer in offers:
        offer_price = prices.get(offer.id)
        out.append(
            {
                "id": str(offer.id),
                "code": offer.code,
                "name": offer.name,
                "recurring_price": str(offer_price.amount)
                if offer_price is not None
                else None,
                "currency": offer_price.currency if offer_price is not None else "NGN",
                "billing_cycle": offer.billing_cycle.value
                if offer.billing_cycle
                else None,
                "speed_download_mbps": offer.speed_download_mbps,
                "speed_upload_mbps": offer.speed_upload_mbps,
            }
        )
    return out


def _resolve_offer(session: Session, offer_ref: str):
    from app.models.catalog import CatalogOffer

    oid = coerce_subscriber_id(str(offer_ref))
    if oid is not None:
        offer = session.get(CatalogOffer, oid)
        if offer is not None:
            return offer
    return (
        session.query(CatalogOffer)
        .filter(CatalogOffer.code == str(offer_ref), CatalogOffer.is_active.is_(True))
        .order_by(CatalogOffer.created_at.desc())
        .first()
    )


def create_subscription(
    session: Session,
    *,
    subscriber_id: str,
    offer_ref: str,
    external_ref: str,
    unit_price: Any = None,
    start_at: datetime | None = None,
    service_address_id: Any = None,
    billing_cycle: Any = None,
    commit: bool = True,
) -> dict:
    """Create a subscription for a subscriber from a CRM sale and generate its
    first (subscription-tagged) invoice, so the plan + its charge show in the
    customer portal. ``offer_ref`` is a sub CatalogOffer id or code — the CRM
    picks a real offer, so no fuzzy matching. Idempotent on ``external_ref``
    (recorded on the first invoice's metadata).
    """
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    from app.models.catalog import BillingCycle, SubscriptionStatus
    from app.schemas.catalog import SubscriptionCreate
    from app.services.billing.invoices import Invoices
    from app.services.catalog.subscriptions import Subscriptions

    # Contracted cadence captured on the sales-order line (SOT: the contract is
    # the fact-of-record). Invalid/absent => None, so the subscription inherits
    # the offer price cadence at creation.
    contracted_cycle: BillingCycle | None = None
    if billing_cycle is not None:
        try:
            contracted_cycle = (
                billing_cycle
                if isinstance(billing_cycle, BillingCycle)
                else BillingCycle(str(billing_cycle))
            )
        except ValueError:
            contracted_cycle = None

    subscriber = session.get(Subscriber, coerce_subscriber_id(str(subscriber_id)))
    if subscriber is None:
        raise LookupError("subscriber not found")
    offer = _resolve_offer(session, offer_ref)
    if offer is None:
        raise LookupError("offer not found")

    # Idempotent: a first invoice tagged with this crm_external_ref means the
    # subscription was synced before — return it rather than duplicating.
    existing = _find_crm_subscription(session, subscriber.id, external_ref)
    if existing is not None:
        return existing

    price_override = None
    if unit_price is not None:
        try:
            price_override = Decimal(str(unit_price))
        except (InvalidOperation, TypeError, ValueError):
            price_override = None

    try:
        subscription = Subscriptions.create(
            session,
            SubscriptionCreate(
                subscriber_id=subscriber.id,
                offer_id=offer.id,
                service_address_id=service_address_id,
                status=SubscriptionStatus.pending,
                start_at=start_at or datetime.now(UTC),
                unit_price=price_override,
                billing_cycle=contracted_cycle,
            ),
            # The sales-fulfillment coordinator creates the structurally linked
            # ServiceOrder.  The generic subscription helper must not create a
            # second, context-free order for the same service.
            create_service_order=False,
            commit=commit,
        )
    except HTTPException:
        # enforce_single_active_subscription treats pending as active and rejects
        # a second one — so a concurrent/previous create already won. It raises
        # before adding anything (no state to roll back); if that winner is this
        # same CRM sale, return it idempotently, else surface the rejection.
        existing = _find_crm_subscription(session, subscriber.id, external_ref)
        if existing is not None:
            return existing
        raise

    invoice = Invoices.create_for_subscription(
        session,
        str(subscriber.id),
        str(subscription.id),
        allow_prepaid=True,
        commit=commit,
    )
    meta = dict(invoice.metadata_ or {})
    meta["source"] = "dotmac_crm"
    meta["crm_external_ref"] = str(external_ref)
    meta["crm_subscription_id"] = str(subscription.id)
    invoice.metadata_ = meta
    # DB backstop: uq_invoices_active_crm_external_ref (migration 212). Legacy
    # root mode commits each creator, so on a true-concurrency collision the
    # sub + invoice are already persisted — cancel the orphan and return the
    # winner. Owner-participant mode flushes only and lets its outer transaction
    # roll back and retry as one unit.
    invoice.crm_external_ref = str(external_ref)
    if commit:
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            cancel_subscription(
                session,
                str(subscription.id),
                "Duplicate CRM external reference",
                "crm_subscription_create",
                emit=False,
                generate_credit=False,
            )
            invoice.is_active = False
            session.commit()
            existing = _find_crm_subscription(session, subscriber.id, external_ref)
            if existing is not None:
                return existing
            raise
    else:
        session.flush()
    return {"subscription": subscription, "invoice": invoice, "created": True}


def _outage_incident_scope(session: Session, incident) -> dict[str, Any]:
    """Human-usable scope block for an incident: what failed, by name."""
    if incident.root_node_id is not None:
        node = session.get(NetworkDevice, incident.root_node_id)
        return {
            "type": "node",
            "id": str(incident.root_node_id),
            "name": getattr(node, "name", None),
            "basestation_id": str(node.pop_site_id)
            if node is not None and node.pop_site_id is not None
            else None,
        }
    if incident.basestation_id is not None:
        pop = session.get(PopSite, incident.basestation_id)
        return {
            "type": "basestation",
            "id": str(incident.basestation_id),
            "name": getattr(pop, "name", None),
            "basestation_id": str(incident.basestation_id),
        }
    if incident.fdh_cabinet_id is not None:
        from app.models.network import FdhCabinet

        fdh = session.get(FdhCabinet, incident.fdh_cabinet_id)
        return {
            "type": "fdh",
            "id": str(incident.fdh_cabinet_id),
            "name": getattr(fdh, "code", None) or getattr(fdh, "name", None),
            "basestation_id": None,
        }
    return {"type": None, "id": None, "name": None, "basestation_id": None}


def outage_incident_row(session: Session, incident) -> dict[str, Any]:
    """One incident, serialized for the CRM (list + detail header).

    ``detection_source`` is the legacy ``auto``/``manual`` field, left UNCHANGED
    for backward compatibility (``auto`` = scanner-detected, ``manual`` =
    hand-declared; classifier incidents report ``manual`` under this legacy
    field). ``provenance`` is the NEW ``operator``/``classifier`` discriminator
    (the model column) so agents can tell a debounced classifier outage from an
    operator-declared one — added additively rather than repurposing the
    existing field, so any external reader of ``detection_source`` keeps working.
    ``state`` mirrors the lifecycle ``status`` (open/resolved for operator;
    suspected/confirmed/clearing/resolved/discarded for classifier).
    ``status_presentation`` adds the server-owned UI label/tone/icon projection.
    ``mttr_seconds`` is ``resolved_at - confirmed_at`` once an incident is resolved.
    """
    from app.services.status_presentation import outage_status_presentation
    from app.services.topology.outage import detection_source, mttr_seconds

    status_presentation = outage_status_presentation(incident.status)

    return {
        "id": str(incident.id),
        "status": incident.status,
        "state": incident.status,
        "status_presentation": status_presentation.model_dump(mode="json"),
        "detection_source": detection_source(incident),
        "provenance": incident.detection_source,
        "scope": _outage_incident_scope(session, incident),
        "severity": incident.severity,
        "affected_count": incident.affected_count,
        "classification": incident.classification,
        "confidence": incident.confidence,
        "note": incident.note,
        "declared_by": incident.declared_by,
        "started_at": utc_iso(incident.started_at),
        "suspected_at": utc_iso(incident.suspected_at),
        "confirmed_at": utc_iso(incident.confirmed_at),
        "cleared_at": utc_iso(incident.cleared_at),
        "resolved_at": utc_iso(incident.resolved_at),
        "mttr_seconds": mttr_seconds(incident),
    }


def list_outage_incidents(
    session: Session,
    *,
    status: str | None = None,
    basestation_id: str | uuid.UUID | None = None,
    node_id: str | uuid.UUID | None = None,
    resolved_within_hours: int = 24,
    page: int = 1,
    per_page: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    """Active + recently-resolved incidents for the CRM, newest first.

    Default "active" view (§7.6 finding 4): operator ``open`` PLUS the debounced
    real classifier states ``confirmed``/``clearing`` (resolved_at NULL), PLUS
    anything resolved within ``resolved_within_hours`` (so a just-cleared outage
    is still visible). ``suspected`` (not yet debounced — noise) and ``discarded``
    (false positive) are deliberately excluded from the default. ``status``
    narrows to any single lifecycle state (open/resolved plus the classifier
    states suspected/confirmed/clearing/discarded); ``basestation_id`` /
    ``node_id`` narrow the scope. Returns ``(rows, total)``.
    """
    from app.models.network_monitoring import OutageIncident
    from app.services.topology.outage import (
        CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
        OUTAGE_STATUS_VALUES,
        OutageStatus,
    )

    # The debounced-real active set: operator open + classifier confirmed/clearing.
    _ACTIVE_STATUSES = (
        OutageStatus.open.value,
        *CLASSIFIER_CUSTOMER_VISIBLE_STATUSES,
    )
    query = session.query(OutageIncident)
    if status in OUTAGE_STATUS_VALUES:
        query = query.filter(OutageIncident.status == status)
    else:
        cutoff = datetime.now(UTC) - timedelta(hours=max(resolved_within_hours, 0))
        query = query.filter(
            or_(
                OutageIncident.status.in_(_ACTIVE_STATUSES),
                OutageIncident.resolved_at >= cutoff,
            )
        )
    if basestation_id:
        query = query.filter(
            OutageIncident.basestation_id == _uuid_or_none(basestation_id)
        )
    if node_id:
        query = query.filter(OutageIncident.root_node_id == _uuid_or_none(node_id))
    total = query.count()
    incidents = (
        query.order_by(OutageIncident.started_at.desc())
        .offset(max(page - 1, 0) * per_page)
        .limit(per_page)
        .all()
    )
    return [outage_incident_row(session, i) for i in incidents], total


def outage_incident_detail(
    session: Session, incident_id: str, *, limit: int = 200
) -> dict[str, Any] | None:
    """One incident plus the subscriptions inside its blast radius.

    Membership is re-derived through the SAME resolvers the declare path used
    (``affected_customers`` over node/basestation/FDH), never re-implemented,
    so the detail list always matches the snapshotted count's semantics.
    Capped at ``limit`` entries (``affected_total`` carries the real size).
    """
    from app.models.network import FdhCabinet
    from app.models.network_monitoring import OutageIncident
    from app.services.topology.affected import affected_customers

    incident_uuid = _uuid_or_none(incident_id)
    incident = (
        session.get(OutageIncident, incident_uuid)
        if incident_uuid is not None
        else None
    )
    if incident is None:
        return None

    node = (
        session.get(NetworkDevice, incident.root_node_id)
        if incident.root_node_id is not None
        else None
    )
    basestation = (
        session.get(PopSite, incident.basestation_id)
        if incident.basestation_id is not None
        else None
    )
    fdh = (
        session.get(FdhCabinet, incident.fdh_cabinet_id)
        if incident.fdh_cabinet_id is not None
        else None
    )
    subscriptions: list = []
    if node is not None or basestation is not None or fdh is not None:
        subscriptions = affected_customers(
            session, node=node, basestation=basestation, fdh=fdh
        )["subscriptions"]

    # Slice BEFORE hydrating: a big outage can cover thousands of
    # subscriptions, and touching .subscriber/.service_address on each would
    # lazy-load per row. Total/truncated come from the id list; only the
    # capped page (deterministic id order) is re-fetched, with both
    # relationships eager-loaded in one pass.
    affected_total = len(subscriptions)
    page_ids = [s.id for s in sorted(subscriptions, key=lambda s: str(s.id))][:limit]
    entries = []
    if page_ids:
        page = (
            session.query(Subscription)
            .options(
                selectinload(Subscription.subscriber),
                selectinload(Subscription.service_address),
            )
            .filter(Subscription.id.in_(page_ids))
            .all()
        )
        for sub in page:
            subscriber = sub.subscriber
            service_address = sub.service_address
            entries.append(
                {
                    "subscription_id": str(sub.id),
                    "status": enum_value(sub.status),
                    "subscriber_id": str(sub.subscriber_id)
                    if sub.subscriber_id
                    else None,
                    "subscriber_name": subscriber_name(subscriber)
                    if subscriber is not None
                    else None,
                    "service_address": address_text(
                        subscriber, [service_address] if service_address else None
                    )
                    if subscriber is not None
                    else None,
                }
            )
        entries.sort(key=lambda e: (e["subscriber_name"] or "", e["subscription_id"]))

    row = outage_incident_row(session, incident)
    row["affected_total"] = affected_total
    row["affected_truncated"] = affected_total > limit
    row["affected_subscriptions"] = entries
    return row


def _find_crm_subscription(
    session: Session, subscriber_id: uuid.UUID, external_ref: str
) -> dict | None:
    """The existing CRM-synced subscription for an external_ref, as the
    ``{subscription, invoice, created: False}`` shape. Keyed on the first
    invoice's ``crm_external_ref`` column (+ its ``crm_subscription_id`` tag)."""
    inv = (
        session.query(Invoice)
        .filter(Invoice.account_id == subscriber_id, Invoice.is_active.is_(True))
        .filter(Invoice.crm_external_ref == str(external_ref))
        .order_by(Invoice.created_at.desc())
        .first()
    )
    if inv is None:
        return None
    sub_id = (inv.metadata_ or {}).get("crm_subscription_id")
    if not sub_id:
        return None
    subscription = session.get(Subscription, coerce_subscriber_id(str(sub_id)))
    return {"subscription": subscription, "invoice": inv, "created": False}
