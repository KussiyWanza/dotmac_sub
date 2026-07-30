"""Dashboard and shared context helpers for customer portal."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    CatalogOffer,
    OfferStatus,
    PriceType,
    Subscription,
)
from app.models.provisioning import AppointmentStatus, InstallAppointment, ServiceOrder
from app.models.subscriber import (
    Subscriber,
    SubscriberCategory,
)
from app.models.support import Ticket
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services.bandwidth import bandwidth_samples
from app.services.collections import get_available_balance
from app.services.common import coerce_uuid
from app.services.customer_context import (
    RESTRICTED_CUSTOMER_STATUSES,
    allowed_customer_account_ids,
    allowed_customer_subscriber_ids,
    customer_is_restricted,
    optional_customer_subscriber_id,
    resolve_customer_account_ids,
)
from app.services.customer_financial_position import get_customer_financial_position
from app.services.customer_support_links import ticket_customer_link_filter
from app.services.invoice_classification import collectible_ar_invoice_filter
from app.services.prepaid_funding_reconstruction import (
    PrepaidFundingBaselineMissingError,
)
from app.services.status_presentation import account_status_presentation

logger = logging.getLogger(__name__)


def _parse_allowed_change_plan_ids(raw_value: str | None) -> set[str]:
    raw = str(raw_value or "").strip()
    if not raw:
        return set()
    normalized = raw.replace("\n", ",").replace("\r", ",").replace(" ", ",")
    return {item.strip() for item in normalized.split(",") if item.strip()}


def _same_region_compatibility(
    current_offer: CatalogOffer,
    candidate_offer: CatalogOffer,
) -> bool:
    return str(current_offer.region_zone_id or "") == str(
        candidate_offer.region_zone_id or ""
    )


def offer_has_positive_recurring_price(offer: CatalogOffer) -> bool:
    """Fail closed unless one active customer-billable recurring price exists."""
    recurring_prices = [
        price
        for price in offer.prices or []
        if price.is_active and price.price_type == PriceType.recurring
    ]
    return len(recurring_prices) == 1 and Decimal(recurring_prices[0].amount) > Decimal(
        "0.00"
    )


def emit_customer_event(db: Session, event_name: str, payload: dict) -> None:
    """Emit a customer portal event without letting telemetry break the request."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        event_type = getattr(EventType, event_name, None)
        if event_type:
            emit_event(db, event_type, payload, actor="customer")
    except Exception as exc:
        logger.warning("Failed to emit customer event %s: %s", event_name, exc)


def resolve_subscriber_id(session: dict) -> str:
    """Extract subscriber_id from a customer session for CRM lookups."""
    return str(
        session.get("subscriber_id")
        or session.get("session", {}).get("subscriber_id", "")
    )


def resolve_allowed_subscriber_ids(session: dict, db: Session) -> list[str]:
    """Resolve all customer-visible subscriber/account IDs for access checks."""
    return allowed_customer_subscriber_ids(db, session)


def resolve_customer_subscription(db: Session, session: dict) -> Subscription | None:
    """Resolve the active subscription visible to the current customer session."""
    account_id, session_subscription_id = resolve_customer_account(session, db)
    account_id_str = str(account_id) if account_id else None

    if session_subscription_id:
        subscription = db.get(Subscription, session_subscription_id)
        if subscription and (
            not account_id_str or str(subscription.subscriber_id) == account_id_str
        ):
            return subscription

    if not account_id_str:
        return None

    try:
        return bandwidth_samples.get_user_active_subscription(
            db, {"account_id": account_id_str}
        )
    except HTTPException:
        return None


def get_dashboard_template_context(db: Session, session: dict) -> tuple[str, dict]:
    """Return the dashboard template and context for full or restricted access."""
    subscriber_id = session.get("subscriber_id")
    if subscriber_id and is_subscriber_restricted(db, subscriber_id):
        return (
            "customer/dashboard/restricted.html",
            {
                "customer": session,
                **get_restricted_dashboard_context(db, session),
                "active_page": "dashboard",
            },
        )
    return (
        "customer/dashboard/index.html",
        {
            "customer": session,
            **get_dashboard_context(db, session),
            "active_page": "dashboard",
        },
    )


def _format_address(address) -> str:
    if not address:
        return "No address on file"
    parts = [address.address_line1]
    if address.city:
        parts.append(address.city)
    if address.region:
        parts.append(address.region)
    if address.postal_code:
        parts.append(address.postal_code)
    return ", ".join([part for part in parts if part])


def _get_subscriber_devices(db: Session, subscriber_id: str) -> list:
    """Get subscriber's ONT devices using the subscriber-ONT adapter.

    Returns a list of SimpleNamespace objects with device info for the portal.
    """
    try:
        from app.services.network.subscriber_ont_adapter import get_subscriber_onts

        onts = get_subscriber_onts(db, subscriber_id)
        devices = []
        for ont_info in onts:
            # Map OLT status to user-friendly display
            status_display = {
                "online": "Online",
                "offline": "Offline",
            }.get(ont_info.olt_status or "offline", "Offline")

            devices.append(
                SimpleNamespace(
                    serial_number=ont_info.serial_number or "Unknown",
                    model=ont_info.model or "ONT Device",
                    status=ont_info.olt_status or "offline",
                    status_display=status_display,
                    location=ont_info.service_address or "Service address",
                )
            )
        return devices
    except Exception as exc:
        logger.warning(
            "Failed to get devices for subscriber %s: %s",
            subscriber_id,
            exc,
        )
        return []


def get_dashboard_context(db: Session, session: dict) -> dict:
    account_id = session.get("account_id")
    subscriber_id = session.get("subscriber_id")

    account_obj = None
    if account_id:
        try:
            account_obj = subscriber_service.accounts.get(db, account_id)
        except Exception:
            account_obj = None

    subscriber = None
    if subscriber_id:
        subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber and account_obj:
        subscriber = account_obj.subscriber

    user_name = session.get("username") or "Customer"
    user = {"first_name": user_name}
    if subscriber:
        if subscriber.category == SubscriberCategory.business:
            user = {
                "first_name": subscriber.company_name
                or subscriber.display_name
                or subscriber.first_name
            }
        elif subscriber.first_name:
            user = {"first_name": subscriber.first_name}

    open_count = 0
    if account_id:
        try:
            open_count = (
                db.scalar(
                    select(func.count(Ticket.id))
                    .where(Ticket.is_active.is_(True))
                    .where(ticket_customer_link_filter(Ticket, account_id))
                    .where(
                        Ticket.status.notin_(
                            (
                                "closed",
                                "canceled",
                                "merged",
                            )
                        )
                    )
                )
                or 0
            )
        except Exception:
            open_count = 0

    from app.services.portal_account_health import build_portal_account_health

    health_account_id = account_id or subscriber_id
    account_health = (
        build_portal_account_health(db, coerce_uuid(health_account_id))
        if health_account_id
        else None
    )

    return {
        "user": SimpleNamespace(**user),
        "account_health": account_health,
        "tickets": SimpleNamespace(open_count=open_count),
        "stats_error": account_health is None or account_health.has_partial_data,
    }


_RESTRICTED_STATUSES = RESTRICTED_CUSTOMER_STATUSES


def get_restricted_since(subscriber: Subscriber) -> datetime | None:
    """Return when the subscriber most recently entered a restricted status."""
    metadata = subscriber.metadata_ or {}
    return subscriber_service._metadata_datetime(metadata, "restricted_since")


def get_total_outstanding_balance(db: Session, account_id: object) -> float:
    """Active positive account balance owed by the customer.

    This uses the same available-balance calculation as enforcement and
    statements, so customer portal totals cannot diverge from the canonical
    customer ledger.
    """
    available = float(get_available_balance(db, str(account_id)) or 0)
    return max(0.0, -available)


def is_subscriber_restricted(db: Session, subscriber_id: object) -> bool:
    """Check if a subscriber should see the restricted portal view."""
    return customer_is_restricted(db, subscriber_id)


def get_restricted_dashboard_context(db: Session, session: dict) -> dict:
    """Build context for the restricted/expired subscriber portal view."""
    subscriber_id = session.get("subscriber_id")
    account_id = session.get("account_id")

    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
    if not subscriber:
        status_presentation = account_status_presentation(None)
        return {
            "restricted": True,
            "account_status": status_presentation.value,
            "account_status_display": status_presentation.label,
            "account_status_presentation": status_presentation,
            "outstanding_balance": None,
            "balance_unavailable": True,
            "recent_invoices": [],
        }

    user_name = session.get("username") or "Customer"
    if subscriber.category == SubscriberCategory.business:
        user_name = subscriber.company_name or subscriber.display_name or user_name
    elif subscriber.first_name:
        user_name = f"{subscriber.first_name} {subscriber.last_name or ''}".strip()

    # Outstanding balance from invoices
    balance: float | None = 0.0
    balance_unavailable = False
    recent_invoices = []
    if account_id:
        try:
            balance = get_total_outstanding_balance(db, account_id)
        except PrepaidFundingBaselineMissingError:
            logger.warning(
                "Restricted dashboard balance unavailable for account %s",
                account_id,
                exc_info=True,
            )
            balance = None
            balance_unavailable = True
        invoices = billing_service.invoices.list(
            db=db,
            account_id=account_id,
            status=None,
            is_active=None,
            order_by="issued_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )
        recent_invoices = [inv for inv in invoices if float(inv.balance_due or 0) > 0][
            :3
        ]

    # Subscriptions (all, not just active)
    subscriptions = []
    if account_id:
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=account_id,
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )

    plan_name = None
    if subscriptions and subscriptions[0].offer:
        plan_name = subscriptions[0].offer.name

    status_value = subscriber.status.value if subscriber.status else "unknown"
    status_presentation = account_status_presentation(
        subscriber.status,
        is_active=subscriber.is_active,
    )

    return {
        "restricted": True,
        "user_name": user_name,
        "subscriber_number": subscriber.subscriber_number or subscriber.account_number,
        "account_status": status_value,
        "account_status_display": status_presentation.label,
        "account_status_presentation": status_presentation,
        "plan_name": plan_name,
        "outstanding_balance": balance,
        "balance_unavailable": balance_unavailable,
        "recent_invoices": recent_invoices,
        "account_start_date": subscriber.account_start_date,
        "blocked_since": get_restricted_since(subscriber),
        "email": subscriber.email,
        "phone": subscriber.phone,
    }


def resolve_customer_account(
    customer: dict, db: Session
) -> tuple[str | None, str | None]:
    """Resolve account_id and subscription_id from customer session.

    Args:
        customer: Customer session dict
        db: Database session

    Returns:
        Tuple of (account_id_str, subscription_id_str)
    """
    return resolve_customer_account_ids(db, customer)


def get_allowed_account_ids(customer: dict, db: Session) -> list[str]:
    """Get list of account IDs the customer has access to.

    Args:
        customer: Customer session dict
        db: Database session

    Returns:
        List of account ID strings
    """
    return allowed_customer_account_ids(db, customer)


def get_invoice_billing_contact(db: Session, invoice, customer: dict) -> dict:
    """Resolve billing name and email for an invoice.

    Args:
        db: Database session
        invoice: Invoice model instance
        customer: Customer session dict

    Returns:
        Dict with 'billing_name' and 'billing_email' keys
    """
    billing_name = None
    billing_email = None
    account = None

    if invoice.account_id:
        try:
            account = subscriber_service.accounts.get(
                db=db, account_id=str(invoice.account_id)
            )
        except Exception:
            account = None

    if account:
        billing_name = (
            account.company_name
            if account.category == SubscriberCategory.business
            else account.display_name
            or f"{account.first_name} {account.last_name}".strip()
        )
        billing_email = account.email

    subscriber_id = optional_customer_subscriber_id(db, customer)
    subscriber = (
        db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None
    )

    if not billing_name and subscriber:
        billing_name = (
            subscriber.company_name
            if subscriber.category == SubscriberCategory.business
            else subscriber.display_name
            or f"{subscriber.first_name} {subscriber.last_name}".strip()
        )
        billing_email = billing_email or subscriber.email

    current_user = customer.get("current_user") if isinstance(customer, dict) else None
    if current_user:
        billing_name = billing_name or current_user.get("name")
        billing_email = billing_email or current_user.get("email")

    return {"billing_name": billing_name, "billing_email": billing_email}


def get_customer_appointments(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get installation appointments for the customer with pagination.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'appointments', 'total', 'total_pages' keys
    """
    account_id, subscription_id = resolve_customer_account_ids(db, customer)
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    if not account_id_str and not subscription_id_str:
        return {"appointments": [], "total": 0, "total_pages": 1}

    filters = []
    if account_id_str:
        filters.append(ServiceOrder.subscriber_id == coerce_uuid(account_id_str))
    if subscription_id_str:
        filters.append(ServiceOrder.subscription_id == coerce_uuid(subscription_id_str))
    if status:
        normalized_status = str(status).strip().lower()
        valid_statuses = {item.value for item in AppointmentStatus}
        if normalized_status in valid_statuses:
            filters.append(InstallAppointment.status == normalized_status)

    count_stmt = (
        select(func.count(InstallAppointment.id))
        .join(ServiceOrder, InstallAppointment.service_order_id == ServiceOrder.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    start = (page - 1) * per_page

    list_stmt = (
        select(InstallAppointment)
        .join(ServiceOrder, InstallAppointment.service_order_id == ServiceOrder.id)
        .where(*filters)
        .order_by(InstallAppointment.scheduled_start.desc())
        .offset(start)
        .limit(per_page)
    )
    appointments = db.scalars(list_stmt).all()

    return {"appointments": appointments, "total": total, "total_pages": total_pages}


def _reseller_default_catalog_open(db: Session) -> bool:
    """Global default: True = resellers see all unrestricted offers (open)."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    raw = settings_spec.resolve_value(
        db, SettingDomain.catalog, "reseller_default_catalog_open"
    )
    return str(raw).lower() in {"true", "1", "on", "yes"}


def _filter_by_reseller_availability(
    db: Session,
    offers: list[CatalogOffer],
    subscriber_id: object | None,
) -> list[CatalogOffer]:
    """Hide reseller-restricted offers from non-members.

    An offer with active offer_reseller_availability rows is visible only to
    subscribers whose reseller is listed; offers with no rows stay
    unrestricted. Callers without subscriber context never see restricted
    offers (fail closed).

    A reseller in "restrict to assigned offers" mode (its own
    ``restrict_to_assigned_offers`` flag, or the inverse of the global
    ``reseller_default_catalog_open`` default when the flag is NULL) sees ONLY
    offers explicitly assigned to it — unrestricted offers are hidden too.
    """
    from app.models.offer_availability import OfferResellerAvailability
    from app.models.subscriber import Reseller

    if not offers:
        return offers
    rows = db.execute(
        select(
            OfferResellerAvailability.offer_id,
            OfferResellerAvailability.reseller_id,
        ).where(
            OfferResellerAvailability.is_active.is_(True),
            OfferResellerAvailability.offer_id.in_([offer.id for offer in offers]),
        )
    ).all()
    restricted: dict[object, set[object]] = {}
    for offer_id, reseller_id in rows:
        restricted.setdefault(offer_id, set()).add(reseller_id)

    # Resolve the caller's reseller + its restrict flag in one query.
    caller_reseller_id = None
    caller_restrict_flag: bool | None = None
    if subscriber_id:
        row = db.execute(
            select(Subscriber.reseller_id, Reseller.restrict_to_assigned_offers)
            .join(Reseller, Reseller.id == Subscriber.reseller_id, isouter=True)
            .where(Subscriber.id == subscriber_id)
        ).first()
        if row:
            caller_reseller_id, caller_restrict_flag = row

    # Restrict mode only applies to a caller with a resolved reseller.
    restrict_mode = False
    if caller_reseller_id is not None:
        restrict_mode = (
            bool(caller_restrict_flag)
            if caller_restrict_flag is not None
            else not _reseller_default_catalog_open(db)
        )

    if restrict_mode:
        return [
            offer
            for offer in offers
            if offer.id in restricted and caller_reseller_id in restricted[offer.id]
        ]

    if not restricted:
        return offers

    return [
        offer
        for offer in offers
        if offer.id not in restricted
        or (caller_reseller_id and caller_reseller_id in restricted[offer.id])
    ]


def get_available_portal_offers(
    db: Session,
    subscription: Subscription | None = None,
    *,
    subscriber_id: object | None = None,
) -> list[CatalogOffer]:
    """Get catalog offers available on the customer portal.

    Args:
        db: Database session
        subscription: Optional current subscription used to scope compatible offers
        subscriber_id: Caller's subscriber id for reseller-availability scoping
            (derived from the subscription when not given)

    Returns:
        List of CatalogOffer instances
    """
    if subscription is not None and not subscriber_id:
        subscriber_id = subscription.subscriber_id

    if subscription is not None:
        from app.services.subscription_billing_treatments import (
            subscription_has_open_billing_treatment,
        )

        if subscription_has_open_billing_treatment(db, subscription.id):
            return []

    if not subscription or not subscription.offer_id:
        return _filter_by_reseller_availability(
            db,
            cast(
                list[CatalogOffer],
                db.scalars(
                    select(CatalogOffer)
                    .options(selectinload(CatalogOffer.prices))
                    .where(CatalogOffer.is_active.is_(True))
                    .where(CatalogOffer.status == OfferStatus.active)
                    .where(CatalogOffer.show_on_customer_portal.is_(True))
                    .order_by(CatalogOffer.name.asc())
                ).all(),
            ),
            subscriber_id,
        )

    current_offer = db.get(CatalogOffer, subscription.offer_id)
    if not current_offer:
        return []

    offers = cast(
        list[CatalogOffer],
        db.scalars(
            select(CatalogOffer)
            .options(selectinload(CatalogOffer.prices))
            .where(CatalogOffer.is_active.is_(True))
            .where(CatalogOffer.status == OfferStatus.active)
            .where(CatalogOffer.show_on_customer_portal.is_(True))
            .where(CatalogOffer.service_type == current_offer.service_type)
            .where(CatalogOffer.billing_mode == current_offer.billing_mode)
            .order_by(CatalogOffer.name.asc())
        ).all(),
    )
    offers = _filter_by_reseller_availability(db, offers, subscriber_id)

    allowed_ids = _parse_allowed_change_plan_ids(current_offer.allowed_change_plan_ids)

    # Plan family is merchandising metadata, not delivery evidence. The
    # subscription lifecycle owner classifies each compatible offer from access
    # and provisionable network intent after the customer selects it.
    return cast(
        list[CatalogOffer],
        [
            offer
            for offer in offers
            if _same_region_compatibility(current_offer, offer)
            and (not allowed_ids or str(offer.id) in allowed_ids)
            and offer_has_positive_recurring_price(offer)
        ],
    )


def get_outstanding_balance(db: Session, account_id: str) -> dict:
    """Get outstanding balance and overdue invoices for an account.

    Args:
        db: Database session
        account_id: Account ID string

    Returns:
        Dict with 'invoices' and 'outstanding_balance' keys
    """
    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.status == InvoiceStatus.overdue)
        .filter(Invoice.is_active.is_(True))
        .filter(collectible_ar_invoice_filter())
        .order_by(Invoice.due_at.asc())
        .limit(50)
        .all()
    )
    position = get_customer_financial_position(db, account_id)
    outstanding_balance = position.collection_blocking_balance

    return {
        "invoices": invoices,
        "outstanding_balance": outstanding_balance,
        "invoices_truncated": len(invoices) == 50,
    }
