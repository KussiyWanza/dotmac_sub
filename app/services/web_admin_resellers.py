"""Service helpers for admin reseller management routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.billing import Invoice, Payment, PaymentStatus
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.offer_availability import OfferResellerAvailability
from app.models.rbac import Role
from app.models.subscriber import Reseller, ResellerUser, Subscriber, UserType
from app.models.support import Ticket
from app.schemas.subscriber import ResellerCreate, ResellerUpdate
from app.services import catalog as catalog_service
from app.services import rbac_catalog, reseller_onboarding
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid
from app.services.customer_support_links import ticket_customer_any_link_filter
from app.services.invoice_collectibility import (
    invoice_balance_sum_by_currency,
    open_invoice_balance_for_accounts,
    open_invoice_filters_for_accounts,
    overdue_debt_filters_for_accounts,
)
from app.services.list_query import ListDefinition, ListFieldDefinition, ListQuery
from app.services.owner_commands import CommandContext
from app.services.status_presentation import (
    invoice_status_presentation,
    payment_status_presentation,
)

RESOLVED_TICKET_STATUSES = {
    "resolved",
    "closed",
    "canceled",
    "merged",
}


def _roles_for_form(db: Session) -> list[Role]:
    return list(
        rbac_catalog.list_roles(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
    )


def _portal_invite_form_context(db: Session) -> dict[str, object]:
    policy = reseller_onboarding.get_reseller_portal_invite_policy()
    return {
        "portal_invite_policy": policy,
        "roles": (
            _roles_for_form(db) if policy.subscriber_role_assignment_supported else ()
        ),
    }


def _policy_sets_for_form(db: Session) -> list:
    return list(
        catalog_service.policy_sets.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        )
    )


RESELLER_LIST_DEFINITION = ListDefinition(
    key="resellers",
    fields=(
        ListFieldDefinition("name", "Reseller", sortable=True),
        ListFieldDefinition("status", "Status", filterable=True),
        ListFieldDefinition("created_at", "Created", sortable=True),
    ),
    default_sort="name",
    default_sort_dir="asc",
    per_page_options=(10, 25, 50, 100, 200),
)

_RESELLER_STATUS_FILTERS = {"active", "inactive", "all"}


def build_reseller_list_query(
    *,
    status: str | None = None,
    page: int = 1,
    per_page: int | None = None,
) -> ListQuery:
    """Normalize the admin reseller list through its declared capabilities.

    ui.reseller_list_projection owns the filterable/sortable fields and pagination;
    the route submits raw request values and does not rebuild those rules.
    """
    normalized_status = (status or "active").strip().lower()
    if normalized_status not in _RESELLER_STATUS_FILTERS:
        normalized_status = "active"
    effective_per_page = per_page or RESELLER_LIST_DEFINITION.default_per_page
    if effective_per_page not in RESELLER_LIST_DEFINITION.per_page_options:
        effective_per_page = RESELLER_LIST_DEFINITION.default_per_page
    return RESELLER_LIST_DEFINITION.build_query(
        search=None,
        filters={"status": normalized_status},
        page=max(1, page),
        per_page=effective_per_page,
    )


def list_page_context(
    db: Session,
    *,
    page: int,
    per_page: int,
    status_filter: str = "active",
) -> dict[str, object]:
    list_query = build_reseller_list_query(
        status=status_filter, page=page, per_page=per_page
    )
    normalized_status = list_query.filter_value("status") or "active"
    active_filter = (
        True
        if normalized_status == "active"
        else False
        if normalized_status == "inactive"
        else None
    )
    per_page = list_query.per_page
    query = db.query(Reseller)
    if active_filter is not None:
        query = query.filter(Reseller.is_active.is_(active_filter))
    total = int(query.with_entities(func.count(Reseller.id)).scalar() or 0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    safe_page = min(list_query.page, total_pages)
    offset = (safe_page - 1) * per_page
    resellers = query.order_by(Reseller.name.asc()).limit(per_page).offset(offset).all()
    return {
        "resellers": resellers,
        "reseller_subscriber_counts": count_subscribers_by_reseller_ids(
            db,
            [str(item.id) for item in resellers],
        ),
        "page": safe_page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status_filter": normalized_status,
    }


def new_form_context(db: Session) -> dict[str, object]:
    return {
        "reseller": None,
        "action_url": "/admin/resellers",
        "policy_sets": _policy_sets_for_form(db),
        **_portal_invite_form_context(db),
    }


def edit_form_context(db: Session, *, reseller_id: str) -> dict[str, object]:
    reseller = subscriber_service.resellers.get(db=db, reseller_id=reseller_id)
    return {
        "reseller": reseller,
        "action_url": f"/admin/resellers/{reseller.id}",
        "policy_sets": _policy_sets_for_form(db),
    }


def create_form_error_context(
    db: Session,
    *,
    payload: dict[str, object],
    error: str,
) -> dict[str, object]:
    return {
        "reseller": payload,
        "action_url": "/admin/resellers",
        "policy_sets": _policy_sets_for_form(db),
        "error": error,
        **_portal_invite_form_context(db),
    }


def update_form_error_context(
    db: Session,
    *,
    reseller_id: str,
    payload: dict[str, object],
    error: str,
) -> dict[str, object]:
    payload.update({"id": reseller_id})
    return {
        "reseller": payload,
        "action_url": f"/admin/resellers/{reseller_id}",
        "policy_sets": _policy_sets_for_form(db),
        "error": error,
    }


def parse_reseller_payload(form) -> dict[str, object]:
    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "name": form_str("name").strip(),
        "code": form_str("code").strip() or None,
        "contact_email": form_str("contact_email").strip() or None,
        "contact_phone": form_str("contact_phone").strip() or None,
        "policy_set_id": form_str("policy_set_id").strip() or None,
        "notes": form_str("notes").strip() or None,
        "is_active": bool(form.get("is_active")),
        # Checked = opt into assigned-only (True); unchecked = inherit the
        # global reseller_default_catalog_open setting (NULL).
        "restrict_to_assigned_offers": (
            True if form.get("restrict_to_assigned_offers") else None
        ),
    }


def parse_create_user_payload(form) -> dict[str, str | None] | None:
    if not bool(form.get("create_user")):
        return None

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "first_name": form_str("user_first_name").strip(),
        "last_name": form_str("user_last_name").strip(),
        "email": form_str("user_email").strip(),
        "username": form_str("user_email").strip() or None,
        "role": form_str("user_role").strip() or None,
    }


def validate_create_user_payload(
    user_payload: dict[str, str | None] | None,
) -> str | None:
    if not user_payload:
        return None
    missing = [
        key
        for key, value in user_payload.items()
        if key not in {"role", "username"} and not value
    ]
    if missing:
        return (
            "Provide first name, last name, and email to create a reseller portal user."
        )
    return None


def create_reseller_from_form(
    db: Session,
    form,
    *,
    context: CommandContext,
    assignment_context: CommandContext | None = None,
) -> tuple[reseller_onboarding.ResellerOnboardingOutcome, str | None]:
    payload = parse_reseller_payload(form)
    try:
        data = ResellerCreate.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            exc.errors()[0].get("msg", "Invalid reseller details.")
        ) from exc
    user_payload = parse_create_user_payload(form)
    user_spec = (
        reseller_onboarding.ResellerPortalUserSpec(
            first_name=user_payload["first_name"] or "",
            last_name=user_payload["last_name"] or "",
            email=user_payload["email"] or "",
            username=user_payload.get("username"),
            role_name=user_payload.get("role"),
            send_invite=True,
        )
        if user_payload
        else None
    )
    outcome = reseller_onboarding.create_reseller(
        db,
        reseller_onboarding.CreateResellerCommand(
            context=context,
            reseller=data,
            portal_user=user_spec,
            assignment_context=assignment_context,
        ),
    )
    return outcome, "Invitation queued." if outcome.invite_requested else None


def update_reseller_from_form(db: Session, *, reseller_id: str, form) -> None:
    payload = parse_reseller_payload(form)
    try:
        data = ResellerUpdate.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            exc.errors()[0].get("msg", "Invalid reseller details.")
        ) from exc
    subscriber_service.resellers.update(db=db, reseller_id=reseller_id, payload=data)


def update_reseller_active_status(
    db: Session, *, reseller_id: str, is_active: bool
) -> Reseller:
    reseller = get_reseller_by_id(db, reseller_id)
    if not reseller:
        raise ValueError("Reseller not found.")
    reseller.is_active = is_active
    db.commit()
    db.refresh(reseller)
    return reseller


def list_reseller_subscribers(
    db: Session,
    reseller_id: str,
) -> list[ResellerUser]:
    """List managed subscriber accounts linked to the reseller."""

    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
            .where(Subscriber.user_type != UserType.reseller)
            .where(Subscriber.user_type != UserType.system_user)
            .order_by(Subscriber.created_at.desc())
        ).all()
    )
    links: list[ResellerUser] = []
    for subscriber in subscribers:
        link = cast(
            ResellerUser,
            SimpleNamespace(
                id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                subscriber_id=subscriber.id,
                person_id=subscriber.id,
                is_active=subscriber.is_active,
                created_at=subscriber.created_at,
                person=subscriber,
            ),
        )
        links.append(link)
    return links


def count_reseller_subscribers(db: Session, reseller_id: str) -> int:
    """Count managed subscriber accounts linked to the reseller."""
    count = db.scalar(
        select(func.count(Subscriber.id))
        .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .where(Subscriber.user_type != UserType.reseller)
        .where(Subscriber.user_type != UserType.system_user)
    )
    return int(count or 0)


def count_subscribers_by_reseller_ids(
    db: Session,
    reseller_ids: list[str],
) -> dict[str, int]:
    """Return subscriber counts keyed by reseller_id for list pages."""
    if not reseller_ids:
        return {}
    reseller_uuids = [coerce_uuid(item) for item in reseller_ids]
    rows = db.execute(
        select(
            Subscriber.reseller_id,
            func.count(Subscriber.id),
        )
        .where(Subscriber.reseller_id.in_(reseller_uuids))
        .where(Subscriber.user_type != UserType.reseller)
        .where(Subscriber.user_type != UserType.system_user)
        .group_by(Subscriber.reseller_id)
    ).all()
    counts: dict[str, int] = {str(row[0]): int(row[1]) for row in rows if row[0]}
    return counts


def list_reseller_subscribers_page(
    db: Session,
    reseller_id: str,
    *,
    limit: int,
    offset: int,
) -> list[ResellerUser]:
    """List a page of managed subscriber accounts linked to the reseller."""
    subscribers = list(
        db.scalars(
            select(Subscriber)
            .where(Subscriber.reseller_id == coerce_uuid(reseller_id))
            .where(Subscriber.user_type != UserType.reseller)
            .where(Subscriber.user_type != UserType.system_user)
            .order_by(Subscriber.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    links: list[ResellerUser] = []
    for subscriber in subscribers:
        link = cast(
            ResellerUser,
            SimpleNamespace(
                id=subscriber.id,
                reseller_id=subscriber.reseller_id,
                subscriber_id=subscriber.id,
                person_id=subscriber.id,
                is_active=subscriber.is_active,
                created_at=subscriber.created_at,
                person=subscriber,
            ),
        )
        links.append(link)
    return links


def list_reseller_people(db: Session) -> list[Subscriber]:
    """List person-type subscribers for linking to resellers."""
    return cast(
        list[Subscriber],
        subscriber_service.subscribers.list(
            db=db,
            business_account_id=None,
            subscriber_type="person",
            order_by="created_at",
            order_dir="asc",
            limit=500,
            offset=0,
        ),
    )


def get_reseller_detail_context(
    db: Session,
    reseller_id: str,
    *,
    page: int = 1,
    per_page: int = 50,
) -> dict | None:
    """Load reseller detail page data: reseller with linked subscribers list.

    Returns None if the reseller is not found.
    """
    reseller = get_reseller_by_id(db, reseller_id)
    if not reseller:
        return None
    reseller_uuid = coerce_uuid(reseller_id)
    linked_subscriber_query = (
        select(Subscriber.id, Subscriber.status)
        .where(Subscriber.reseller_id == reseller_uuid)
        .where(Subscriber.user_type != UserType.reseller)
        .where(Subscriber.user_type != UserType.system_user)
    )
    linked_subscriber_rows = list(db.execute(linked_subscriber_query).all())
    linked_subscriber_ids = [row[0] for row in linked_subscriber_rows]
    total_subscribers = count_reseller_subscribers(db, reseller_id)
    safe_per_page = max(10, min(per_page, 200))
    safe_page = max(1, page)
    total_pages = max(1, (total_subscribers + safe_per_page - 1) // safe_per_page)
    if safe_page > total_pages:
        safe_page = total_pages
    offset = (safe_page - 1) * safe_per_page
    reseller_subscribers = list_reseller_subscribers_page(
        db,
        reseller_id,
        limit=safe_per_page,
        offset=offset,
    )
    subscriber_status_counts: dict[str, int] = {}
    for _, status in linked_subscriber_rows:
        key = getattr(status, "value", str(status))
        subscriber_status_counts[key] = subscriber_status_counts.get(key, 0) + 1

    reseller_portal_users = int(
        db.scalar(
            select(func.count(Subscriber.id))
            .where(Subscriber.reseller_id == reseller_uuid)
            .where(Subscriber.user_type == UserType.reseller)
        )
        or 0
    )

    active_services = 0
    pending_services = 0
    suspended_services = 0
    subscriptions_total = 0
    outstanding_balance = Decimal("0.00")
    outstanding_balance_by_currency: list[dict[str, object]] = []
    overdue_invoices = 0
    recent_invoices: list[Invoice] = []
    recent_payments: list[Payment] = []
    recent_tickets: list[Ticket] = []
    recent_subscriptions: list[Subscription] = []
    explicit_available_offers: list[CatalogOffer] = []
    explicit_available_offers_total = 0
    payments_30d_total = Decimal("0.00")
    payments_30d_count = 0
    open_tickets = 0

    if linked_subscriber_ids:
        subscriptions_total = int(
            db.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.subscriber_id.in_(linked_subscriber_ids)
                )
            )
            or 0
        )
        active_services = int(
            db.scalar(
                select(func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(linked_subscriber_ids))
                .where(Subscription.status == SubscriptionStatus.active)
            )
            or 0
        )
        pending_services = int(
            db.scalar(
                select(func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(linked_subscriber_ids))
                .where(Subscription.status == SubscriptionStatus.pending)
            )
            or 0
        )
        suspended_services = int(
            db.scalar(
                select(func.count(Subscription.id))
                .where(Subscription.subscriber_id.in_(linked_subscriber_ids))
                .where(Subscription.status == SubscriptionStatus.suspended)
            )
            or 0
        )
        recent_subscriptions = list(
            db.query(Subscription)
            .options(
                joinedload(Subscription.offer), joinedload(Subscription.subscriber)
            )
            .filter(Subscription.subscriber_id.in_(linked_subscriber_ids))
            .order_by(Subscription.created_at.desc())
            .limit(5)
            .all()
        )

        outstanding_balance = open_invoice_balance_for_accounts(
            db, linked_subscriber_ids
        )
        outstanding_balance_by_currency = [
            {"currency": str(currency or ""), "amount": amount}
            for currency, amount in invoice_balance_sum_by_currency(
                db, open_invoice_filters_for_accounts(linked_subscriber_ids)
            )
        ]
        overdue_invoices = int(
            db.scalar(
                select(func.count(Invoice.id)).where(
                    *overdue_debt_filters_for_accounts(linked_subscriber_ids)
                )
            )
            or 0
        )
        recent_invoices = list(
            db.query(Invoice)
            .options(joinedload(Invoice.account))
            .filter(Invoice.account_id.in_(linked_subscriber_ids))
            .order_by(Invoice.created_at.desc())
            .limit(5)
            .all()
        )

        payments_since = datetime.now(UTC) - timedelta(days=30)
        payments_30d_total = db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.account_id.in_(linked_subscriber_ids))
            .where(Payment.status == PaymentStatus.succeeded)
            .where(Payment.created_at >= payments_since)
        ) or Decimal("0.00")
        payments_30d_count = int(
            db.scalar(
                select(func.count(Payment.id))
                .where(Payment.account_id.in_(linked_subscriber_ids))
                .where(Payment.status == PaymentStatus.succeeded)
                .where(Payment.created_at >= payments_since)
            )
            or 0
        )
        recent_payments = list(
            db.query(Payment)
            .options(joinedload(Payment.account))
            .filter(Payment.account_id.in_(linked_subscriber_ids))
            .order_by(Payment.created_at.desc())
            .limit(5)
            .all()
        )

        ticket_scope = ticket_customer_any_link_filter(Ticket, linked_subscriber_ids)
        open_tickets = int(
            db.scalar(
                select(func.count(Ticket.id))
                .where(ticket_scope)
                .where(Ticket.status.notin_(list(RESOLVED_TICKET_STATUSES)))
            )
            or 0
        )
        recent_tickets = list(
            db.query(Ticket)
            .filter(ticket_scope)
            .order_by(Ticket.updated_at.desc())
            .limit(5)
            .all()
        )

    explicit_available_offers_total = int(
        db.scalar(
            select(func.count(OfferResellerAvailability.id))
            .where(OfferResellerAvailability.reseller_id == reseller_uuid)
            .where(OfferResellerAvailability.is_active.is_(True))
        )
        or 0
    )
    explicit_available_offers = list(
        db.query(CatalogOffer)
        .join(
            OfferResellerAvailability,
            OfferResellerAvailability.offer_id == CatalogOffer.id,
        )
        .filter(OfferResellerAvailability.reseller_id == reseller_uuid)
        .filter(OfferResellerAvailability.is_active.is_(True))
        .filter(CatalogOffer.is_active.is_(True))
        .order_by(CatalogOffer.name.asc())
        .limit(8)
        .all()
    )
    return {
        "reseller": reseller,
        "reseller_subscribers": reseller_subscribers,
        "reseller_subscribers_total": total_subscribers,
        "reseller_portal_users": reseller_portal_users,
        "subscriber_status_counts": subscriber_status_counts,
        "active_services": active_services,
        "pending_services": pending_services,
        "suspended_services": suspended_services,
        "subscriptions_total": subscriptions_total,
        "outstanding_balance": outstanding_balance,
        "outstanding_balance_by_currency": outstanding_balance_by_currency,
        "overdue_invoices": overdue_invoices,
        "payments_30d_total": payments_30d_total,
        "payments_30d_count": payments_30d_count,
        "open_tickets": open_tickets,
        "recent_invoices": recent_invoices,
        "invoice_status_presentations": {
            str(invoice.id): invoice_status_presentation(invoice.status)
            for invoice in recent_invoices
        },
        "recent_payments": recent_payments,
        "payment_status_presentations": {
            str(payment.id): payment_status_presentation(payment.status)
            for payment in recent_payments
        },
        "recent_tickets": recent_tickets,
        "recent_subscriptions": recent_subscriptions,
        "explicit_available_offers": explicit_available_offers,
        "explicit_available_offers_total": explicit_available_offers_total,
        "policy_sets": _policy_sets_for_form(db),
        **_portal_invite_form_context(db),
        "reseller_urls": {
            "billing_overview": f"/admin/billing?partner_id={reseller.id}",
            "invoices": f"/admin/billing/invoices?partner_id={reseller.id}",
            "payments": f"/admin/billing/payments?partner_id={reseller.id}",
            "accounts": f"/admin/billing/accounts?reseller_id={reseller.id}",
            "provisioning": f"/admin/provisioning/migrate?reseller_id={reseller.id}",
            "settings": f"/admin/resellers/{reseller.id}#reseller-details",
            "subscribers": f"/admin/resellers/{reseller.id}#linked-subscribers",
            "support": f"/admin/resellers/{reseller.id}#support-activity",
            "catalog": f"/admin/resellers/{reseller.id}#catalog-access",
            "services": f"/admin/resellers/{reseller.id}#service-activity",
        },
        "page": safe_page,
        "per_page": safe_per_page,
        "total_pages": total_pages,
    }


def link_existing_subscriber_to_reseller(
    db: Session,
    *,
    reseller_id: str,
    subscriber_id: str,
) -> bool:
    """Link an existing subscriber account to a reseller."""
    r_uuid = coerce_uuid(reseller_id)
    s_uuid = coerce_uuid(subscriber_id)

    subscriber = db.get(Subscriber, s_uuid)
    if not subscriber:
        raise ValueError("Subscriber not found.")
    user_type_value = str(getattr(subscriber.user_type, "value", subscriber.user_type))
    if user_type_value in {"system_user", "reseller"}:
        raise ValueError("Only customer subscribers can be linked to a reseller.")
    if subscriber.reseller_id == r_uuid:
        raise ValueError("Subscriber is already linked to this reseller.")
    subscriber.reseller_id = r_uuid
    db.commit()
    return True


def create_and_link_reseller_user(
    db: Session,
    *,
    reseller_id: str,
    first_name: str,
    last_name: str,
    email: str,
    username: str | None = None,
    password: str | None = None,
    role: str | None = None,
    context: CommandContext,
    assignment_context: CommandContext | None = None,
) -> reseller_onboarding.ResellerOnboardingOutcome:
    """Translate the admin form into the canonical onboarding command."""

    return reseller_onboarding.provision_reseller_user(
        db,
        reseller_onboarding.ProvisionResellerUserCommand(
            context=context,
            reseller_id=coerce_uuid(reseller_id),
            portal_user=reseller_onboarding.ResellerPortalUserSpec(
                first_name=first_name,
                last_name=last_name,
                email=email,
                username=username,
                password=password,
                role_name=role,
                send_invite=True,
            ),
            assignment_context=assignment_context,
        ),
    )


def get_reseller_by_id(db: Session, reseller_id: str) -> Reseller | None:
    """Fetch a reseller by ID, returning None if not found."""
    return db.get(Reseller, coerce_uuid(reseller_id))
