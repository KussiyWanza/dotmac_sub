from __future__ import annotations

import enum
import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from app.models.billing import (
    BillingRun,
    BillingRunStatus,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    DiscountType,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.network import SubscriberAdditionalRoute
from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services import enforcement_window, settings_spec
from app.services.billing import _recalculate_invoice_totals
from app.services.billing._common import _calculate_tax_amount
from app.services.billing.invoices import InvoiceLines, Invoices, next_invoice_number
from app.services.billing.reconcile_unposted import settle_open_invoices_from_credit
from app.services.billing_prepaid_overlap_repair import apply_prepaid_overlap_hold
from app.services.billing_settings import (
    accounts_with_collectible_service,
    resolve_payment_due_days,
)
from app.services.billing_statuses import BILLABLE_SUBSCRIBER_STATUSES
from app.services.common import coerce_uuid, round_money
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.invoice_classification import (
    collectible_ar_invoice_filter,
    prepaid_non_ar_invoice_ids,
)
from app.services.service_entitlements import (
    ensure_prepaid_entitlements_for_paid_invoice,
    prepaid_entitlement_coverage_end,
)

logger = logging.getLogger(__name__)


class PostpaidChargePreviewError(DomainError):
    """Fail-closed current-owner preview used by migration verification."""


class PostpaidChargePreviewDisposition(enum.StrEnum):
    """Why the current postpaid owner produced one candidate charge."""

    comparable = "comparable"


class RecurringChargeComponentKind(enum.StrEnum):
    """Component identity exposed by the current postpaid invoice owner."""

    base_service = "base_service"
    recurring_addon = "recurring_addon"


class PostpaidChargePreviewIssueKind(enum.StrEnum):
    """Current add-on behavior that cannot support a cutover parity claim."""

    multiple_active_prices = "multiple_active_prices"
    currency_mismatch = "currency_mismatch"
    route_unavailable = "route_unavailable"
    route_quantity_capped = "route_quantity_capped"


@dataclass(frozen=True, slots=True)
class PostpaidChargeComponentPreview:
    """One exact component included by the current postpaid invoice formula."""

    kind: RecurringChargeComponentKind
    component_key: str
    quantity: Decimal
    unit_price: Decimal
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal
    subscription_add_on_id: UUID | None = None
    add_on_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PostpaidChargePreviewIssue:
    """Typed evidence for an add-on the current owner skips or resolves unsafely."""

    kind: PostpaidChargePreviewIssueKind
    subscription_add_on_id: UUID
    add_on_id: UUID
    expected_currency: str | None = None
    observed_currency: str | None = None
    configured_quantity: Decimal | None = None
    billable_quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PostpaidChargePreview:
    """Typed current-owner result for one complete recurring invoice cycle."""

    subscription_id: UUID
    account_id: UUID
    period_start: datetime
    period_end: datetime
    currency: str
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal
    billing_cycle: BillingCycle
    tax_application: TaxApplication
    tax_rate_percent: Decimal
    tax_rate_id: UUID | None
    disposition: PostpaidChargePreviewDisposition
    components: tuple[PostpaidChargeComponentPreview, ...]
    issues: tuple[PostpaidChargePreviewIssue, ...]


@dataclass(frozen=True, slots=True)
class _RecurringAddonPrice:
    amount: Decimal
    currency: str
    multiple_active_prices: bool


@dataclass(frozen=True, slots=True)
class _RecurringAddonCharge:
    subscription_add_on_id: UUID
    add_on_id: UUID
    name: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


def _postpaid_preview_error(
    suffix: str,
    message: str,
    *,
    subscription_id: UUID,
) -> PostpaidChargePreviewError:
    return PostpaidChargePreviewError(
        code=f"financial.billing_automation.{suffix}",
        message=message,
        details={"subscription_id": str(subscription_id)},
    )


def _billing_run_extra(
    *,
    run_uuid: object | None,
    run_at: datetime,
    billing_cycle: BillingCycle | None,
    dry_run: bool,
    include_pending: bool,
    auto_activate_pending: bool,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
    attempt: int | None = None,
    max_retries: int | None = None,
) -> dict[str, object]:
    extra: dict[str, object] = {
        "event": "billing_run",
        "billing_run_id": str(run_uuid) if run_uuid else None,
        "run_at": run_at.isoformat(),
        "billing_cycle": billing_cycle.value if billing_cycle else None,
        "dry_run": dry_run,
        "include_pending": include_pending,
        "auto_activate_pending": auto_activate_pending,
    }
    if summary is not None:
        for key in (
            "subscriptions_scanned",
            "subscriptions_billed",
            "invoices_created",
            "lines_created",
            "skipped",
            "currency_skipped",
            "pending_activated",
            "invoice_reminders_sent",
            "credit_applied",
            "credit_settled_invoices",
            "accounts_restored",
            "run_id",
        ):
            if key in summary:
                value = summary[key]
                extra[key] = str(value) if isinstance(value, Decimal) else value
    if error is not None:
        extra["error"] = error
    if attempt is not None:
        extra["attempt"] = attempt
    if max_retries is not None:
        extra["max_retries"] = max_retries
    return extra


def _setting_truthy(db: Session, key: str, *, default: bool) -> bool:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int_setting(value: object) -> int | None:
    # settings_spec.resolve_value() returns object | None.
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _parse_day_offsets(value: object) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):
        return [value] if value >= 0 else []
    if isinstance(value, str):
        offsets: set[int] = set()
        for raw_part in value.split(","):
            part = raw_part.strip()
            if not part:
                continue
            try:
                parsed = int(part)
            except ValueError:
                continue
            if parsed >= 0:
                offsets.add(parsed)
        return sorted(offsets, reverse=True)
    return []


def _add_months(value: datetime, months: int) -> datetime:
    total = value.month - 1 + months
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _period_end(start: datetime, cycle: BillingCycle) -> datetime:
    """Calculate the end of a billing period based on the cycle.

    Args:
        start: The period start date
        cycle: The billing cycle (daily, weekly, monthly, annual)

    Returns:
        The period end date
    """
    if cycle == BillingCycle.daily:
        return start + timedelta(days=1)
    if cycle == BillingCycle.weekly:
        return start + timedelta(weeks=1)
    if cycle == BillingCycle.monthly:
        return _add_months(start, 1)
    if cycle == BillingCycle.quarterly:
        return _add_months(start, 3)
    if cycle == BillingCycle.annual:
        return _add_months(start, 12)
    # Default to monthly
    return _add_months(start, 1)


def _resolve_price(db: Session, subscription: Subscription):
    if subscription.offer_version_id:
        version_prices = (
            db.query(OfferVersionPrice)
            .filter(OfferVersionPrice.offer_version_id == subscription.offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .order_by(OfferVersionPrice.created_at.desc(), OfferVersionPrice.id.desc())
            .limit(2)
            .all()
        )
        if len(version_prices) > 1:
            logger.warning(
                "Multiple active recurring offer-version prices for subscription %s "
                "(offer_version %s); using newest",
                subscription.id,
                subscription.offer_version_id,
            )
        if version_prices:
            version_price = version_prices[0]
            return (
                version_price.amount,
                version_price.currency,
                # SOT: subscription-owned cadence wins; price cadence is fallback.
                subscription.billing_cycle or version_price.billing_cycle,
            )
    offer_prices = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == subscription.offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .order_by(OfferPrice.created_at.desc(), OfferPrice.id.desc())
        .limit(2)
        .all()
    )
    if len(offer_prices) > 1:
        logger.warning(
            "Multiple active recurring offer prices for subscription %s (offer %s); "
            "using newest",
            subscription.id,
            subscription.offer_id,
        )
    if offer_prices:
        offer_price = offer_prices[0]
        return (
            offer_price.amount,
            offer_price.currency,
            # SOT: subscription-owned cadence wins; price cadence is fallback.
            subscription.billing_cycle or offer_price.billing_cycle,
        )
    # No price => no charge; cadence is irrelevant with nothing to bill.
    return None, None, None


def _effective_unit_price(
    subscription: Subscription,
    catalog_amount: Decimal,
    now: datetime,
    *,
    include_override: bool = True,
) -> Decimal:
    """Effective per-cycle price for a subscription.

    A positive subscription.unit_price (imported or admin-set
    negotiated price) overrides the catalog amount. Zero is treated as
    "no override" because the legacy importer stores 0 when the export
    carried no per-service price. An enabled discount inside its
    [discount_start_at, discount_end_at] window (open-ended where null,
    bounds inclusive) is then applied: percentage is percent-off, fixed
    is an absolute reduction. Never returns below 0.00.

    ``include_override=False`` skips the per-offer unit_price override (but still
    applies the subscription-level discount); plan-change proration uses this for
    the *new* plan's side, whose catalog price hasn't been snapshotted yet.
    """
    base = catalog_amount
    if (
        include_override
        and subscription.unit_price is not None
        and subscription.unit_price > 0
    ):
        base = subscription.unit_price
    price = Decimal(str(base))
    if subscription.discount and subscription.discount_value is not None:
        now_utc = _as_utc(now) or datetime.now(UTC)
        start = _as_utc(subscription.discount_start_at)
        end = _as_utc(subscription.discount_end_at)
        in_window = (start is None or now_utc >= start) and (
            end is None or now_utc <= end
        )
        if in_window:
            value = Decimal(str(subscription.discount_value))
            if subscription.discount_type in (
                DiscountType.percentage,
                DiscountType.percent,
            ):
                price -= price * value / Decimal("100")
            elif subscription.discount_type == DiscountType.fixed:
                price -= value
    if price < Decimal("0.00"):
        price = Decimal("0.00")
    return round_money(price)


def _active_tax_rate_id(db: Session, tax_rate_id) -> bool:
    rate = db.get(TaxRate, tax_rate_id)
    return rate is not None and bool(rate.is_active)


def _tax_rate_for_catalog_percent(db: Session, vat_percent: Decimal | None):
    if vat_percent is None:
        return None
    percent = Decimal(str(vat_percent))
    if percent <= Decimal("0.00"):
        return None
    candidates = {percent}
    if percent > Decimal("1.00"):
        candidates.add(percent / Decimal("100"))
    else:
        candidates.add(percent * Decimal("100"))
    rates = db.query(TaxRate).filter(TaxRate.is_active.is_(True)).all()
    for rate in rates:
        current = Decimal(str(rate.rate))
        if any(current == candidate for candidate in candidates):
            return rate.id
    return None


def _resolve_offer_tax_rate_id(db: Session, offer: CatalogOffer | None):
    """Resolve tax from the catalog offer's VAT flags.

    The catalog is the service-level source of truth: a positive ``vat_percent``
    means taxable even when older imported rows still have ``with_vat=false``;
    ``with_vat=false`` without a positive percent means VAT-exempt.
    """
    if offer is None:
        return _default_tax_rate_id(db)

    percent = Decimal(str(offer.vat_percent or "0"))
    if percent > Decimal("0.00"):
        return _tax_rate_for_catalog_percent(db, percent) or _default_tax_rate_id(db)
    if bool(offer.with_vat):
        return _default_tax_rate_id(db)
    return None


def _resolve_tax_rate_id(db: Session, subscription: Subscription):
    def _is_active(tax_rate_id) -> bool:
        return _active_tax_rate_id(db, tax_rate_id)

    if subscription.service_address_id:
        address = db.get(Address, subscription.service_address_id)
        if address and address.tax_rate_id and _is_active(address.tax_rate_id):
            return address.tax_rate_id
    subscriber = db.get(Subscriber, subscription.subscriber_id)
    if subscriber and subscriber.tax_rate_id and _is_active(subscriber.tax_rate_id):
        return subscriber.tax_rate_id
    return _resolve_offer_tax_rate_id(db, subscription.offer)


def _default_tax_rate_id(db: Session):
    """Configurable fallback VAT rate, applied when neither the service address
    nor the subscriber carries a tax_rate_id. Unset by default → returns None →
    no tax (current behaviour); set the ``billing.default_tax_rate_id`` setting
    to a TaxRate id to bill a default VAT."""
    raw = settings_spec.resolve_value(db, SettingDomain.billing, "default_tax_rate_id")
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        rate = db.get(TaxRate, coerce_uuid(value))
    except (ValueError, TypeError):
        return None
    if rate is not None and bool(rate.is_active):
        return rate.id
    return None


def _default_tax_application(db: Session) -> TaxApplication:
    """Whether default-VAT billing treats catalog prices as tax-exclusive (tax
    added on top — the default) or tax-inclusive (tax extracted from the price).
    Controlled by ``billing.default_tax_application`` (exclusive|inclusive)."""
    raw = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_tax_application"
    )
    value = str(raw or "").strip().lower()
    if value == "inclusive":
        return TaxApplication.inclusive
    if value == "exempt":
        return TaxApplication.exempt
    return TaxApplication.exclusive


def _prorated_amount(
    full_amount: Decimal,
    period_start: datetime,
    period_end: datetime,
    usage_start: datetime,
    usage_end: datetime,
) -> Decimal:
    period_seconds = (period_end - period_start).total_seconds()
    usage_seconds = (usage_end - usage_start).total_seconds()
    if period_seconds <= 0 or usage_seconds <= 0:
        return Decimal("0.00")
    ratio = min(
        Decimal(str(usage_seconds)) / Decimal(str(period_seconds)), Decimal("1.00")
    )
    return round_money(full_amount * ratio)


def _line_amounts(
    amount: Decimal,
    *,
    tax_rate_percent: Decimal,
    tax_application: TaxApplication,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return net, tax, and gross exactly as invoice total recalculation does."""

    tax_amount = _calculate_tax_amount(
        amount,
        tax_rate_percent,
        tax_application,
    )
    if tax_application is TaxApplication.inclusive:
        gross_amount = round_money(amount)
        net_amount = gross_amount - tax_amount
    else:
        net_amount = round_money(amount)
        gross_amount = net_amount + tax_amount
    return net_amount, tax_amount, gross_amount


def preview_postpaid_recurring_charge(
    db: Session,
    *,
    subscription_id: UUID,
    as_of: datetime,
) -> PostpaidChargePreview:
    """Resolve the current postpaid owner's next complete recurring charge.

    This is the typed, read-only comparison surface for ADR 0007 Phase 2. It
    deliberately reuses the exact period, negotiated-price, discount,
    proration, recurring add-on, route-cap, and tax helpers used by
    ``run_invoice_cycle``. It neither checks whether an invoice line already
    exists nor writes a BillingRun: the shadow verifier needs one candidate
    period for every active cohort member, not only the subset due in a
    particular batch.
    """

    if as_of.tzinfo is None:
        raise PostpaidChargePreviewError(
            code="financial.billing_automation.invalid_date",
            message="Postpaid charge preview requires a timezone-aware instant.",
            details={"field": "as_of"},
        )
    effective_at = _as_utc(as_of)
    assert effective_at is not None
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise _postpaid_preview_error(
            "subscription_not_found",
            "Postpaid charge preview requires an existing subscription.",
            subscription_id=subscription_id,
        )
    if subscription.billing_mode is BillingMode.prepaid:
        raise _postpaid_preview_error(
            "mode_not_postpaid",
            "The current postpaid owner cannot preview a prepaid subscription.",
            subscription_id=subscription_id,
        )
    if subscription.status is not SubscriptionStatus.active:
        raise _postpaid_preview_error(
            "subscription_not_billable",
            "The current postpaid preview requires an active subscription.",
            subscription_id=subscription_id,
        )
    account = db.get(Subscriber, subscription.subscriber_id)
    if account is None or account.status not in BILLABLE_SUBSCRIBER_STATUSES:
        raise _postpaid_preview_error(
            "account_not_billable",
            "The current postpaid owner excludes this account state.",
            subscription_id=subscription_id,
        )

    catalog_amount, currency, cycle = _resolve_price(db, subscription)
    if catalog_amount is None:
        raise _postpaid_preview_error(
            "missing_price",
            "The current postpaid owner cannot resolve a recurring price.",
            subscription_id=subscription_id,
        )
    amount = _effective_unit_price(subscription, catalog_amount, effective_at)
    effective_cycle = cycle or BillingCycle.monthly
    period_start = _as_utc(
        subscription.next_billing_at or subscription.start_at or effective_at
    )
    assert period_start is not None
    period_end = _period_end(period_start, effective_cycle)
    while period_end <= effective_at:
        period_start = period_end
        period_end = _period_end(period_start, effective_cycle)

    service_start = _as_utc(subscription.start_at) or period_start
    service_end = _as_utc(subscription.end_at)
    if service_end is not None and service_end <= period_start:
        raise _postpaid_preview_error(
            "service_ended",
            "The current postpaid owner has no charge after service end.",
            subscription_id=subscription_id,
        )
    covered_start = max(period_start, service_start)
    covered_end = min(period_end, service_end) if service_end else period_end
    net_or_gross = _prorated_amount(
        amount,
        period_start,
        period_end,
        covered_start,
        covered_end,
    )
    if net_or_gross <= Decimal("0.00"):
        raise _postpaid_preview_error(
            "zero_amount",
            "The current postpaid owner advances this period without a charge.",
            subscription_id=subscription_id,
        )

    tax_rate_id = _resolve_tax_rate_id(db, subscription)
    tax_rate = db.get(TaxRate, tax_rate_id) if tax_rate_id is not None else None
    tax_application = (
        _default_tax_application(db) if tax_rate is not None else TaxApplication.exempt
    )
    tax_rate_percent = (
        Decimal(str(tax_rate.rate)) if tax_rate is not None else Decimal("0")
    )
    base_net, base_tax, base_gross = _line_amounts(
        net_or_gross,
        tax_rate_percent=tax_rate_percent,
        tax_application=tax_application,
    )
    components: list[PostpaidChargeComponentPreview] = [
        PostpaidChargeComponentPreview(
            kind=RecurringChargeComponentKind.base_service,
            component_key="base_service",
            quantity=Decimal("1"),
            unit_price=round_money(net_or_gross),
            net_amount=base_net,
            tax_amount=base_tax,
            gross_amount=base_gross,
        )
    ]
    addon_charges, issues = _resolve_recurring_addon_charges(
        db,
        subscription=subscription,
        period_start=period_start,
        period_end=period_end,
        usage_start=covered_start,
        usage_end=covered_end,
        invoice_currency=str(currency or "NGN"),
    )
    for charge in addon_charges:
        net_amount, tax_amount, gross_amount = _line_amounts(
            charge.amount,
            tax_rate_percent=tax_rate_percent,
            tax_application=tax_application,
        )
        components.append(
            PostpaidChargeComponentPreview(
                kind=RecurringChargeComponentKind.recurring_addon,
                component_key=str(charge.subscription_add_on_id),
                quantity=charge.quantity,
                unit_price=charge.unit_price,
                net_amount=net_amount,
                tax_amount=tax_amount,
                gross_amount=gross_amount,
                subscription_add_on_id=charge.subscription_add_on_id,
                add_on_id=charge.add_on_id,
            )
        )

    return PostpaidChargePreview(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        period_start=period_start,
        period_end=period_end,
        currency=str(currency or "NGN").upper(),
        net_amount=sum(
            (component.net_amount for component in components),
            start=Decimal("0.00"),
        ),
        tax_amount=sum(
            (component.tax_amount for component in components),
            start=Decimal("0.00"),
        ),
        gross_amount=sum(
            (component.gross_amount for component in components),
            start=Decimal("0.00"),
        ),
        billing_cycle=effective_cycle,
        tax_application=tax_application,
        tax_rate_percent=tax_rate_percent,
        tax_rate_id=tax_rate_id,
        disposition=PostpaidChargePreviewDisposition.comparable,
        components=tuple(components),
        issues=issues,
    )


def _addon_recurring_price(db: Session, add_on_id: UUID) -> _RecurringAddonPrice | None:
    """Active recurring price for an add-on -> (amount, currency), or None when
    it has no recurring price (one-time/usage add-ons don't bill on the cycle)."""
    prices = (
        db.query(AddOnPrice)
        .filter(AddOnPrice.add_on_id == add_on_id)
        .filter(AddOnPrice.is_active.is_(True))
        .filter(AddOnPrice.price_type == PriceType.recurring)
        .limit(2)
        .all()
    )
    if not prices:
        return None
    price = prices[0]
    return _RecurringAddonPrice(
        amount=round_money(price.amount or 0),
        currency=str(price.currency or "NGN"),
        multiple_active_prices=len(prices) > 1,
    )


def _active_ip_route_count(db: Session, subscriber_id, prefix_length: int) -> int:
    """Count active additional routed blocks of a given prefix for a subscriber.

    The route↔add-on tie is convention-by-prefix (no FK) and is only reconciled
    when an admin re-saves the subscription form, so a block released or
    deactivated by any other lifecycle/IPAM path can leave the add-on billing
    forever. This lets the recurring-add-on biller keep public-IP billing
    coupled to whether the block is actually still routing.
    """
    return (
        db.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscriber_id == subscriber_id)
        .filter(SubscriberAdditionalRoute.prefix_length == prefix_length)
        .filter(SubscriberAdditionalRoute.is_active.is_(True))
        .count()
    )


def _resolve_recurring_addon_charges(
    db: Session,
    *,
    subscription: Subscription,
    period_start: datetime,
    period_end: datetime,
    usage_start: datetime,
    usage_end: datetime,
    invoice_currency: str,
) -> tuple[tuple[_RecurringAddonCharge, ...], tuple[PostpaidChargePreviewIssue, ...]]:
    """Resolve the exact recurring add-on inputs used by invoice execution.

    Each line is prorated to the add-on's own active overlap with the billing
    period (intersected with the subscription's service window): an add-on that
    started or ended mid-period is billed only for the portion it was active —
    not the full period, and not skipped outright. Add-ons that don't overlap the
    period at all (start in a later period, or already ended before it) are
    excluded.

    Public-IP add-ons can additionally be coupled to live routing: when the
    ``billing.bill_ip_addon_requires_active_route`` setting is on, the billed
    quantity is capped to the number of active matching ``SubscriberAdditionalRoute``
    blocks (and the line skipped entirely when none remain), so a released or
    deactivated block stops billing even if the add-on's ``end_at`` was never set.
    Default OFF keeps the deploy behaviour-neutral until route data is audited.
    """
    # Add-ons that OVERLAP [period_start, period_end): started before the period
    # ends and not yet ended when it begins. (Proration below handles the partial
    # overlap; a degenerate row that slips through bills 0 and is skipped.)
    rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .filter(
            (SubscriptionAddOn.start_at.is_(None))
            | (SubscriptionAddOn.start_at < period_end)
        )
        .filter(
            (SubscriptionAddOn.end_at.is_(None))
            | (SubscriptionAddOn.end_at > period_start)
        )
        .all()
    )
    charges: list[_RecurringAddonCharge] = []
    issues: list[PostpaidChargePreviewIssue] = []
    require_active_route = _setting_truthy(
        db, "bill_ip_addon_requires_active_route", default=False
    )
    for sub_addon, add_on in rows:
        priced = _addon_recurring_price(db, add_on.id)
        if priced is None:
            continue
        unit, currency = priced.amount, priced.currency
        qty = Decimal(str(sub_addon.quantity or 1))
        if priced.multiple_active_prices:
            issues.append(
                PostpaidChargePreviewIssue(
                    kind=PostpaidChargePreviewIssueKind.multiple_active_prices,
                    subscription_add_on_id=sub_addon.id,
                    add_on_id=add_on.id,
                    observed_currency=currency,
                    configured_quantity=qty,
                )
            )
        if currency != invoice_currency:
            issues.append(
                PostpaidChargePreviewIssue(
                    kind=PostpaidChargePreviewIssueKind.currency_mismatch,
                    subscription_add_on_id=sub_addon.id,
                    add_on_id=add_on.id,
                    expected_currency=invoice_currency,
                    observed_currency=currency,
                    configured_quantity=qty,
                )
            )
            continue

        # Keep public-IP add-on billing coupled to live routing: bill at most the
        # number of active matching routes, skipping entirely when none remain.
        # Only applies to public-IP add-ons that declare a prefix length.
        if (
            require_active_route
            and add_on.ip_is_public
            and add_on.ip_prefix_length is not None
        ):
            active_routes = _active_ip_route_count(
                db, subscription.subscriber_id, add_on.ip_prefix_length
            )
            billable_qty = min(int(qty), active_routes)
            if billable_qty <= 0:
                issues.append(
                    PostpaidChargePreviewIssue(
                        kind=PostpaidChargePreviewIssueKind.route_unavailable,
                        subscription_add_on_id=sub_addon.id,
                        add_on_id=add_on.id,
                        configured_quantity=qty,
                        billable_quantity=Decimal("0"),
                    )
                )
                continue
            if billable_qty != int(qty):
                issues.append(
                    PostpaidChargePreviewIssue(
                        kind=PostpaidChargePreviewIssueKind.route_quantity_capped,
                        subscription_add_on_id=sub_addon.id,
                        add_on_id=add_on.id,
                        configured_quantity=qty,
                        billable_quantity=Decimal(str(billable_qty)),
                    )
                )
                qty = Decimal(str(billable_qty))

        # Prorate to the add-on's own active overlap with the billing period,
        # clipped to the subscription's service window. In the steady case (add-on
        # active for the whole period) the ratio is 1, so amount == unit * qty;
        # a mid-period start/end bills only the active portion.
        addon_start = _as_utc(sub_addon.start_at) or period_start
        addon_end = _as_utc(sub_addon.end_at) or period_end
        addon_usage_start = max(usage_start, addon_start)
        addon_usage_end = min(usage_end, addon_end)
        if addon_usage_start >= addon_usage_end:
            continue
        amount = _prorated_amount(
            round_money(unit * qty),
            period_start,
            period_end,
            addon_usage_start,
            addon_usage_end,
        )
        if amount <= Decimal("0.00"):
            continue
        charges.append(
            _RecurringAddonCharge(
                subscription_add_on_id=sub_addon.id,
                add_on_id=add_on.id,
                name=add_on.name,
                quantity=qty,
                unit_price=unit,
                amount=amount,
            )
        )
    return tuple(charges), tuple(issues)


def _bill_recurring_addons(
    db: Session,
    invoice: Invoice,
    subscription: Subscription,
    period_start: datetime,
    period_end: datetime,
    usage_start: datetime,
    usage_end: datetime,
    tax_rate_id,
) -> int:
    """Stage the recurring add-on lines resolved by the current owner formula."""

    charges, issues = _resolve_recurring_addon_charges(
        db,
        subscription=subscription,
        period_start=period_start,
        period_end=period_end,
        usage_start=usage_start,
        usage_end=usage_end,
        invoice_currency=invoice.currency or "NGN",
    )
    for issue in issues:
        logger.warning(
            "recurring_addon_billing_issue",
            extra={
                "event": "recurring_addon_billing_issue",
                "subscription_id": str(subscription.id),
                "subscriber_id": str(subscription.subscriber_id),
                "subscription_add_on_id": str(issue.subscription_add_on_id),
                "add_on_id": str(issue.add_on_id),
                "issue": issue.kind.value,
            },
        )
    added = 0
    tax_application = (
        _default_tax_application(db) if tax_rate_id else TaxApplication.exempt
    )
    for charge in charges:
        billing_line_key = _billing_line_key(
            subscription.id,
            period_start,
            period_end,
            f"addon:{charge.subscription_add_on_id}",
        )
        if _billing_line_key_exists(
            db, billing_line_key
        ) or _recurring_addon_line_exists(
            db,
            subscription.id,
            charge.subscription_add_on_id,
            period_start,
            period_end,
        ):
            continue
        InvoiceLines.stage_system_line(
            db,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description=(
                    f"{charge.name} ({period_start.date()} - {period_end.date()})"
                ),
                quantity=charge.quantity,
                unit_price=charge.unit_price,
                amount=charge.amount,
                tax_rate_id=tax_rate_id,
                tax_application=tax_application,
                metadata_={
                    "kind": "recurring_addon",
                    "subscription_add_on_id": str(charge.subscription_add_on_id),
                    "add_on_id": str(charge.add_on_id),
                    "billing_period_start": period_start.isoformat(),
                    "billing_period_end": period_end.isoformat(),
                },
                billing_line_key=billing_line_key,
            ),
            reason="scheduled_recurring_addon",
        )
        added += 1
    return added


def _billing_line_key(
    subscription_id,
    period_start: datetime,
    period_end: datetime,
    component: str,
) -> str:
    return (
        f"subscription:{subscription_id}:"
        f"{period_start.isoformat()}:{period_end.isoformat()}:{component}"
    )


def _billing_line_key_exists(db: Session, billing_line_key: str) -> bool:
    return (
        db.query(InvoiceLine.id)
        .filter(InvoiceLine.billing_line_key == billing_line_key)
        .filter(InvoiceLine.is_active.is_(True))
        .first()
        is not None
    )


def _recurring_addon_line_exists(
    db: Session,
    subscription_id,
    subscription_add_on_id,
    period_start: datetime,
    period_end: datetime,
) -> bool:
    rows = (
        db.query(InvoiceLine)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(InvoiceLine.subscription_id == subscription_id)
        .filter(Invoice.billing_period_start == period_start)
        .filter(Invoice.billing_period_end == period_end)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.is_active.is_(True))
        .all()
    )
    wanted = str(subscription_add_on_id)
    for line in rows:
        metadata = line.metadata_ or {}
        if (
            metadata.get("kind") == "recurring_addon"
            and metadata.get("subscription_add_on_id") == wanted
        ):
            return True
    return False


def _paid_coverage_end_for_subscription(
    db: Session,
    subscription_id,
    account_id,
    period_start: datetime,
    period_end: datetime,
) -> datetime | None:
    """Return paid-through coverage overlapping a proposed recurring period.

    Imported Splynx invoices are the migration source of truth for prepaid
    service coverage during cutover. A locally generated prepaid invoice must
    not overlap a paid imported period just because ``next_billing_at`` drifted
    earlier than the actual paid-through date.
    """
    entitlement_end = prepaid_entitlement_coverage_end(
        db,
        subscription_id=subscription_id,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
    )
    row = (
        db.query(Invoice.billing_period_end)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .filter(Invoice.account_id == account_id)
        .filter(InvoiceLine.subscription_id == subscription_id)
        .filter(Invoice.is_active.is_(True))
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.status == InvoiceStatus.paid)
        .filter(Invoice.balance_due <= Decimal("0.00"))
        .filter(Invoice.billing_period_start.isnot(None))
        .filter(Invoice.billing_period_end.isnot(None))
        .filter(Invoice.billing_period_start <= period_start)
        .filter(Invoice.billing_period_end > period_start)
        .order_by(Invoice.billing_period_end.desc())
        .first()
    )
    invoice_end = _as_utc(row[0]) if row is not None else None
    entitlement_end = _as_utc(entitlement_end)
    candidates = [end for end in (entitlement_end, invoice_end) if end is not None]
    return max(candidates) if candidates else None


def _activate_pending_subscription(
    db: Session,
    subscription: Subscription,
    run_at: datetime,
) -> None:
    """Activate a pending subscription when its first invoice is generated.

    Delegates to the lifecycle module for status change and account status
    derivation. Emits the activation event manually with the
    ``auto_activated=True`` flag so other handlers (like proration) skip
    since billing already creates the invoice.
    """
    from app.services.account_lifecycle import activate_subscription

    try:
        activate_subscription(
            db,
            str(subscription.id),
            start_at=run_at,
            emit=False,  # Emit manually below with auto_activated flag
        )
    except ValueError as e:
        logger.warning(
            "Could not auto-activate subscription %s: %s", subscription.id, e
        )
        return

    logger.info("Auto-activated subscription %s (pending → active)", subscription.id)

    emit_event(
        db,
        EventType.subscription_activated,
        {
            "subscription_id": str(subscription.id),
            "offer_name": subscription.offer.name if subscription.offer else None,
            "from_status": "pending",
            "to_status": "active",
            "auto_activated": True,
        },
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
    )


def _emit_invoice_created_event(
    db: Session,
    invoice: Invoice,
    run_id: str | None,
) -> None:
    """Emit invoice.created event for webhook integrations."""
    emit_event(
        db,
        EventType.invoice_created,
        {
            "invoice_id": str(invoice.id),
            "account_id": str(invoice.account_id),
            # invoice_number / amount / due_date feed the invoice_created email
            # template — without them the notification was suppressed as having
            # unresolved variables.
            "invoice_number": invoice.invoice_number or "",
            "amount": str(invoice.total) if invoice.total else "0.00",
            "due_date": invoice.due_at.date().isoformat() if invoice.due_at else "",
            "status": invoice.status.value if invoice.status else None,
            "currency": invoice.currency,
            "subtotal": str(invoice.subtotal) if invoice.subtotal else "0.00",
            "total": str(invoice.total) if invoice.total else "0.00",
            "billing_period_start": invoice.billing_period_start.isoformat()
            if invoice.billing_period_start
            else None,
            "billing_period_end": invoice.billing_period_end.isoformat()
            if invoice.billing_period_end
            else None,
            "due_at": invoice.due_at.isoformat() if invoice.due_at else None,
            "billing_run_id": run_id,
        },
        invoice_id=invoice.id,
        account_id=invoice.account_id,
    )


def _mark_invoice_metadata_flag(invoice: Invoice, key: str) -> None:
    metadata = dict(invoice.metadata_ or {})
    metadata[key] = datetime.now(UTC).isoformat()
    invoice.metadata_ = metadata


def _billing_notification_flag_enabled(metadata: dict | None) -> bool:
    raw_value = (metadata or {}).get("send_billing_notifications")
    if raw_value is None:
        return True
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, str):
        return raw_value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw_value)


def _emit_invoice_reminders(
    db: Session,
    run_at: datetime,
) -> int:
    reminder_days = _parse_day_offsets(
        settings_spec.resolve_value(db, SettingDomain.billing, "invoice_reminder_days")
    )
    if not reminder_days:
        return 0

    sent = 0
    collectible_accounts = accounts_with_collectible_service(db)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(collectible_ar_invoice_filter())
        .filter(
            Invoice.status.in_([InvoiceStatus.issued, InvoiceStatus.partially_paid])
        )
        .all()
    )
    account_ids = {invoice.account_id for invoice in invoices if invoice.account_id}
    suppressed_account_ids = {
        account.id
        for account in db.query(Subscriber).filter(Subscriber.id.in_(account_ids)).all()
        if not _billing_notification_flag_enabled(account.metadata_)
    }
    for invoice in invoices:
        # Don't remind on balances for accounts whose services are all
        # paused or terminal (disabled/canceled/expired/…) — a service that is
        # not being billed should not keep receiving collection reminders.
        if invoice.account_id not in collectible_accounts:
            continue
        if invoice.account_id in suppressed_account_ids:
            continue
        if not invoice.due_at or (invoice.balance_due or Decimal("0.00")) <= Decimal(
            "0.00"
        ):
            continue
        if (invoice.metadata_ or {}).get("reconciliation_hold"):
            continue
        if apply_prepaid_overlap_hold(db, invoice):
            continue
        due_at = _as_utc(invoice.due_at)
        if due_at is None:
            continue
        days_until_due = (due_at.date() - run_at.date()).days
        if days_until_due not in reminder_days:
            continue
        metadata = dict(invoice.metadata_ or {})
        marker = f"invoice_reminder_sent_{days_until_due}"
        if metadata.get(marker):
            continue
        emit_event(
            db,
            EventType.invoice_sent,
            {
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number or "",
                "amount": str(invoice.balance_due or invoice.total or Decimal("0.00")),
                "due_date": due_at.date().isoformat(),
                "days_until_due": str(days_until_due),
            },
            invoice_id=invoice.id,
            account_id=invoice.account_id,
        )
        _mark_invoice_metadata_flag(invoice, marker)
        sent += 1
    return sent


def _log_billing_run_audit(
    db: Session,
    run: BillingRun | None,
    summary: dict[str, Any],
    status: str,
    error: str | None = None,
) -> None:
    """Log billing run results to audit log."""
    from app.models.audit import AuditActorType, AuditEvent
    from app.services.audit_adapter import stage_audit_event

    run_id = None
    if run:
        try:
            run_id = str(run.id)
        except ObjectDeletedError:
            run_id = None

    run_at_value = summary.get("run_at")
    run_at_iso = (
        run_at_value.isoformat() if isinstance(run_at_value, datetime) else None
    )
    metadata = {
        "run_id": run_id,
        "launch_kind": run.launch_kind if run else "scheduled",
        "source_run_id": str(run.source_run_id) if run and run.source_run_id else None,
        "preview_fingerprint": run.preview_fingerprint if run else None,
        "run_at": run_at_iso,
        "subscriptions_scanned": summary.get("subscriptions_scanned", 0),
        "subscriptions_billed": summary.get("subscriptions_billed", 0),
        "invoices_created": summary.get("invoices_created", 0),
        "lines_created": summary.get("lines_created", 0),
        "skipped": summary.get("skipped", 0),
        "currency_skipped": summary.get("currency_skipped", 0),
        "pending_activated": summary.get("pending_activated", 0),
        "invoice_reminders_sent": summary.get("invoice_reminders_sent", 0),
        "status": status,
    }
    if error:
        metadata["error"] = error

    existing = (
        db.query(AuditEvent)
        .filter(AuditEvent.action == "billing_run")
        .filter(AuditEvent.entity_type == "billing_run")
        .filter(AuditEvent.entity_id == run_id)
        .filter(AuditEvent.is_active.is_(True))
        .first()
        if run_id
        else None
    )
    if existing is not None:
        return

    stage_audit_event(
        db,
        actor_type=(
            AuditActorType.user if run and run.requested_by else AuditActorType.system
        ),
        actor_id=(
            run.requested_by if run and run.requested_by else "billing_automation"
        ),
        action="billing_run",
        entity_type="billing_run",
        entity_id=run_id,
        is_success=status == "success",
        metadata=metadata,
    )


def reconcile_billing_run_audit(db: Session, run_id: UUID) -> bool:
    """Idempotently rebuild the secondary audit projection for one terminal run."""
    run = db.get(BillingRun, run_id)
    if run is None or run.status is BillingRunStatus.running:
        return False
    summary: dict[str, Any] = {
        "run_at": run.run_at,
        "subscriptions_scanned": run.subscriptions_scanned,
        "subscriptions_billed": run.subscriptions_billed,
        "invoices_created": run.invoices_created,
        "lines_created": run.lines_created,
        "skipped": run.skipped,
    }
    from app.models.audit import AuditEvent

    existed = (
        db.query(AuditEvent)
        .filter(AuditEvent.action == "billing_run")
        .filter(AuditEvent.entity_type == "billing_run")
        .filter(AuditEvent.entity_id == str(run.id))
        .filter(AuditEvent.is_active.is_(True))
        .first()
        is not None
    )
    _log_billing_run_audit(
        db,
        run,
        summary,
        run.status.value,
        run.error,
    )
    if not existed:
        db.commit()
    return not existed


_ABANDONED_RUN_MAX_AGE_HOURS = 12


def _fail_abandoned_runs(db: Session) -> int:
    """Mark billing runs stuck in `running` for hours as failed.

    A run left `running` means the worker died mid-run (deploy restart, OOM,
    kill) before the failure handler could write; without this sweep those
    rows look in-flight forever.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=_ABANDONED_RUN_MAX_AGE_HOURS)
    stale = (
        db.query(BillingRun)
        .filter(BillingRun.status == BillingRunStatus.running)
        .filter(BillingRun.started_at < cutoff)
        .all()
    )
    for run in stale:
        run.status = BillingRunStatus.failed
        run.finished_at = datetime.now(UTC)
        run.error = "abandoned: worker died mid-run (swept by next run)"
    if stale:
        db.commit()
        logger.warning(
            "billing_runs_abandoned_swept",
            extra={"event": "billing_runs_abandoned_swept", "count": len(stale)},
        )
    return len(stale)


def subscription_invoice_eligible(
    subscription: Subscription, *, allow_prepaid: bool = False
) -> bool:
    """Whether a subscription may enter invoice generation.

    Production prepaid subscriptions are invoiced in advance only when the
    caller explicitly opts in (``allow_prepaid=True``). The default remains
    postpaid-only so ad-hoc invoice paths do not double-bill prepaid services.
    """
    if allow_prepaid:
        return True
    return subscription.billing_mode != BillingMode.prepaid


def run_billing_notifications(
    db: Session,
    run_at: datetime | None = None,
) -> dict[str, int | bool]:
    """Emit pre-due invoice reminders, gated by the send window.

    Intended to run hourly: the send-window gate (``billing_notif_send_hour``)
    restricts sends to the configured local hour. Overdue notifications and
    every service-access consequence are owned by policy dunning, not billing
    automation.
    """
    run_at = run_at or datetime.now(UTC)
    if not enforcement_window.within_send_window(db, run_at):
        return {
            "invoice_reminders_sent": 0,
            "skipped_outside_window": True,
        }
    reminders = _emit_invoice_reminders(db, run_at)
    db.commit()
    return {
        "invoice_reminders_sent": reminders,
        "skipped_outside_window": False,
    }


def run_invoice_cycle(
    db: Session,
    run_at: datetime | None = None,
    billing_cycle: BillingCycle | None = None,
    dry_run: bool = False,
    include_pending: bool = True,
    auto_activate_pending: bool = True,
    suppress_restore_notifications: bool = False,
    run_prepaid_renewals: bool = True,
    launch_kind: str = "scheduled",
    requested_by: str | None = None,
    preview_fingerprint: str | None = None,
    source_run_id: UUID | None = None,
) -> dict[str, Any]:
    """Run the billing cycle to generate invoices for subscriptions.

    Args:
        db: Database session
        run_at: The reference time for the billing run (defaults to now)
        billing_cycle: Optional filter to only process subscriptions with this cycle
        dry_run: If True, don't create any records, just return what would be done
        include_pending: If True, also bill pending subscriptions ready for activation
        auto_activate_pending: If True, auto-activate pending subscriptions when billed
        suppress_restore_notifications: If True, mute customer notifications from any
            service restore triggered when credit settles a suspended account's debt.
            Off by default (steady-state restores are a legitimate "service resumed"
            notice); set True for a bulk catch-up run to avoid a notification burst.
        run_prepaid_renewals: If True, run the independently owned prepaid renewal
            pass before postpaid invoice generation. Manual invoice-only commands
            disable this so their preview and execution have one stated scope.
        launch_kind: Durable provenance for scheduled, manual, or retry launches.
        requested_by: Staff principal for a confirmed manual or retry launch.
        preview_fingerprint: Exact staff preview evidence confirmed before launch.
        source_run_id: Failed run that a reviewed retry supersedes.
    """
    run_at = _as_utc(run_at) or datetime.now(UTC)

    from app.services.events.handlers.owner_session import owner_session
    from app.services.owner_commands import CommandContext
    from app.services.prepaid_service_renewals import (
        RunDuePrepaidServiceRenewalsCommand,
        execute_due_prepaid_service_renewals,
    )

    prepaid_renewal_summary: dict[str, int | str] = {}
    if run_prepaid_renewals:
        # This legacy billing coordinator owns a separate postpaid transaction
        # lifecycle.  The prepaid owner must therefore enter on a fresh clean
        # session; it cannot inherit a caller read transaction or be nested in
        # the postpaid run. The helper preserves the same bind in the external-
        # transaction test harness while production receives an independent
        # owner transaction.
        with owner_session(db) as renewal_db:
            prepaid_renewal_summary = execute_due_prepaid_service_renewals(
                renewal_db,
                RunDuePrepaidServiceRenewalsCommand(
                    context=CommandContext.system(
                        actor="system:billing_automation",
                        scope="prepaid_service_renewals",
                        reason=f"{launch_kind} billing-cycle renewal pass",
                        idempotency_key=(
                            "prepaid-renewal-pass:"
                            f"{run_at.isoformat()}:{'dry' if dry_run else 'apply'}"
                        ),
                    ),
                    run_at=run_at,
                    dry_run=dry_run,
                ),
            )

    global_due_days = resolve_payment_due_days(db)

    run = BillingRun(
        run_at=run_at,
        billing_cycle=billing_cycle.value if billing_cycle else None,
        launch_kind=launch_kind,
        requested_by=requested_by,
        preview_fingerprint=preview_fingerprint,
        source_run_id=source_run_id,
        status=BillingRunStatus.running,
        started_at=datetime.now(UTC),
    )
    run_uuid = None
    if not dry_run:
        _fail_abandoned_runs(db)
        db.add(run)
        db.commit()
        db.refresh(run)
        run_uuid = run.id
    logger.info(
        "billing_run_start",
        extra=_billing_run_extra(
            run_uuid=run_uuid,
            run_at=run_at,
            billing_cycle=billing_cycle,
            dry_run=dry_run,
            include_pending=include_pending,
            auto_activate_pending=auto_activate_pending,
        ),
    )

    # Query billable active subscriptions. Network/account enforcement states
    # like blocked/suspended must not suppress invoicing: those accounts still
    # owe for active service periods and may need the invoice to clear the block.
    # Postpaid subscriptions are invoiced here. The retired prepaid monthly
    # draft path was a competing owner for the same service period; all prepaid
    # periods now belong exclusively to ``financial.prepaid_service_renewals``.
    mode_filter = Subscription.billing_mode != BillingMode.prepaid

    active_subscriptions = (
        db.query(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
        .filter(mode_filter)
        .all()
    )

    # Optionally include pending subscriptions ready for billing
    pending_subscriptions = []
    if include_pending:
        pending_subscriptions = (
            db.query(Subscription)
            .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
            .filter(Subscription.status == SubscriptionStatus.pending)
            .filter(Subscriber.status == SubscriberStatus.active)
            .filter(mode_filter)
            .all()
        )

    prepaid_skipped_query = (
        db.query(Subscription)
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscriber.status.in_(BILLABLE_SUBSCRIBER_STATUSES))
        .filter(Subscription.billing_mode == BillingMode.prepaid)
    )
    prepaid_skipped = prepaid_skipped_query.count()
    if prepaid_skipped:
        logger.info(
            "Invoice cycle skipped %d prepaid subscription(s) (%s)",
            prepaid_skipped,
            "owned by canonical prepaid renewals",
        )

    subscriptions = active_subscriptions + pending_subscriptions

    from app.services.subscription_billing_grants import (
        SubscriptionBillingGrantError,
        stage_subscription_billing_grant,
    )
    from app.services.subscription_billing_treatments import (
        SubscriptionBillingTreatmentError,
        resolve_subscription_billing_treatments,
    )

    billing_treatments = resolve_subscription_billing_treatments(
        db, subscriptions, as_of=run_at
    )

    invoices: dict[tuple[str, datetime, datetime, BillingMode], Invoice] = {}
    newly_created_invoices: list[Invoice] = []
    summary: dict[str, Any] = {
        "run_at": run_at,
        "subscriptions_scanned": len(subscriptions),
        "subscriptions_billed": 0,
        "invoices_created": 0,
        "lines_created": 0,
        "skipped": 0,
        "prepaid_skipped": prepaid_skipped,
        "currency_skipped": 0,
        "pending_activated": 0,
        "invoice_reminders_sent": 0,
        "credit_applied": Decimal("0.00"),
        "credit_settled_invoices": 0,
        "accounts_restored": 0,
        "zero_amount_advanced": 0,
        "non_cash_service_grants": 0,
        "non_cash_service_grants_replayed": 0,
        "billing_treatment_blocked": 0,
        "prepaid_legacy_invoice_path_retired": True,
        **prepaid_renewal_summary,
    }
    preview_subscriptions: list[dict[str, object]] = []
    preview_invoice_currencies: dict[
        tuple[str, datetime, datetime, BillingMode], str
    ] = {}
    preview_accounts: set[str] = set()
    preview_totals: dict[str, Decimal] = {}

    for subscription in subscriptions:
        is_pending = subscription.status == SubscriptionStatus.pending
        amount, currency, cycle = _resolve_price(db, subscription)
        if amount is None:
            summary["skipped"] += 1
            continue
        amount = _effective_unit_price(subscription, amount, run_at)
        effective_cycle = cycle or BillingCycle.monthly
        if billing_cycle and effective_cycle != billing_cycle:
            continue

        # For pending subscriptions, use run_at as the period start if no start_at
        if is_pending:
            period_start = _as_utc(subscription.start_at) or run_at
        else:
            period_start = (
                _as_utc(subscription.next_billing_at or subscription.start_at or run_at)
                or run_at
            )

        if period_start > run_at:
            continue
        period_end = _period_end(period_start, effective_cycle)

        if subscription.billing_mode == BillingMode.prepaid:
            paid_through = _paid_coverage_end_for_subscription(
                db,
                subscription.id,
                subscription.subscriber_id,
                period_start,
                period_end,
            )
            if paid_through and paid_through > period_start:
                current_nb = _as_utc(subscription.next_billing_at)
                if not dry_run and (current_nb is None or current_nb < paid_through):
                    subscription.next_billing_at = paid_through
                logger.info(
                    "billing_prepaid_paid_coverage_skip",
                    extra={
                        "event": "billing_prepaid_paid_coverage_skip",
                        "run_id": str(run_uuid) if run_uuid else None,
                        "subscription_id": str(subscription.id),
                        "subscriber_id": str(subscription.subscriber_id),
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                        "paid_through": paid_through.isoformat(),
                    },
                )
                summary["skipped"] += 1
                continue

        # Skip wholly-past billing periods instead of invoicing every missed
        # month. Migrated subscribers carry next_billing_at/start_at at
        # their original signup date — without this, the runner generated a
        # backdated invoice per missed period per run, double-billing periods
        # already settled before local billing cutover.
        # We fast-forward next_billing_at to the current period and bill only
        # that. Historical arrears require an explicit reviewed repair rather
        # than a mutable fleet-wide billing mode.
        if not is_pending and period_end <= run_at:
            skipped_periods = 0
            while period_end <= run_at:
                period_start = period_end
                period_end = _period_end(period_start, effective_cycle)
                skipped_periods += 1
            if not dry_run:
                subscription.next_billing_at = period_start
            logger.info(
                "billing_fast_forward",
                extra={
                    "run_id": str(run_uuid) if run_uuid else None,
                    "subscription_id": str(subscription.id),
                    "skipped_periods": skipped_periods,
                    "new_period_start": period_start.isoformat(),
                },
            )
            if period_start > run_at:
                summary["skipped"] += 1
                continue
        end_at = _as_utc(subscription.end_at)
        start_at = _as_utc(subscription.start_at) or period_start
        if end_at and end_at <= period_start:
            continue
        usage_start = max(period_start, start_at)
        usage_end = min(period_end, end_at) if end_at else period_end
        line_amount = _prorated_amount(
            amount, period_start, period_end, usage_start, usage_end
        )
        treatment = billing_treatments[subscription.id]
        if treatment.suppress_customer_billing:
            if not treatment.grantable:
                summary["billing_treatment_blocked"] += 1
                summary["skipped"] += 1
                logger.error(
                    "billing_treatment_drift_blocked",
                    extra={
                        "event": "billing_treatment_drift_blocked",
                        "subscription_id": str(subscription.id),
                        "arrangement_id": (
                            str(treatment.arrangement_id)
                            if treatment.arrangement_id
                            else None
                        ),
                        "reason": treatment.drift_reason,
                    },
                )
                continue
            grant_start = max(
                period_start, _as_utc(treatment.starts_at) or period_start
            )
            grant_end = _period_end(grant_start, effective_cycle)
            if end_at is not None and end_at < grant_end:
                grant_end = end_at
            if dry_run:
                summary["non_cash_service_grants"] += 1
                summary["skipped"] += 1
                continue
            try:
                grant = stage_subscription_billing_grant(
                    db,
                    subscription=subscription,
                    decision=treatment,
                    starts_at=grant_start,
                    ends_at=grant_end,
                    actor="system:billing_automation",
                    correlation_id=run_uuid,
                    reference_amount=line_amount,
                )
            except (
                SubscriptionBillingGrantError,
                SubscriptionBillingTreatmentError,
            ) as exc:
                summary["billing_treatment_blocked"] += 1
                summary["skipped"] += 1
                logger.error(
                    "billing_treatment_grant_blocked",
                    extra={
                        "event": "billing_treatment_grant_blocked",
                        "subscription_id": str(subscription.id),
                        "arrangement_id": (
                            str(treatment.arrangement_id)
                            if treatment.arrangement_id
                            else None
                        ),
                        "code": exc.code,
                    },
                )
                continue
            summary[
                "non_cash_service_grants_replayed"
                if grant.replayed
                else "non_cash_service_grants"
            ] += 1
            summary["skipped"] += 1
            continue
        if line_amount <= Decimal("0.00"):
            if not dry_run:
                subscription.next_billing_at = period_end
            summary["zero_amount_advanced"] += 1
            summary["skipped"] += 1
            continue

        offer_name = (
            subscription.offer.name
            if subscription.offer
            else f"Subscription {subscription.id}"
        )
        description = f"{offer_name} ({period_start.date()} - {period_end.date()})"

        # Idempotency check 1: verify no base invoice line exists for this
        # subscription+period. Recurring add-ons have their own lines and must
        # not cause the base service line to be skipped after a partial failure.
        # This catches cases where the invoice was created but next_billing_at wasn't updated
        existing_line_for_period = (
            db.query(InvoiceLine)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .filter(InvoiceLine.description == description)
            .filter(Invoice.billing_period_start == period_start)
            .filter(Invoice.billing_period_end == period_end)
            .filter(InvoiceLine.is_active.is_(True))
            .filter(Invoice.is_active.is_(True))
            .first()
        )
        if existing_line_for_period:
            existing_invoice = existing_line_for_period.invoice
            # Prepaid drafts are not service proof. Only paid prepaid invoices
            # can advance service coverage; postpaid anchors keep the existing
            # invoice idempotency behavior.
            can_advance_anchor = subscription.billing_mode != BillingMode.prepaid or (
                existing_invoice is not None
                and existing_invoice.status == InvoiceStatus.paid
                and (existing_invoice.balance_due or Decimal("0.00")) <= Decimal("0.00")
            )
            if (
                not dry_run
                and can_advance_anchor
                and (
                    subscription.next_billing_at is None
                    or subscription.next_billing_at < period_end
                )
            ):
                subscription.next_billing_at = period_end
            if (
                not dry_run
                and subscription.billing_mode == BillingMode.prepaid
                and existing_invoice is not None
                and existing_invoice.status == InvoiceStatus.paid
            ):
                ensure_prepaid_entitlements_for_paid_invoice(db, existing_invoice)
            logger.debug(
                "Skipping subscription %s: already billed for period %s - %s",
                subscription.id,
                period_start.date(),
                period_end.date(),
            )
            summary["skipped"] += 1
            continue

        if dry_run:
            normalized_currency = (currency or "NGN").upper()
            preview_invoice_key = (
                str(subscription.subscriber_id),
                period_start,
                period_end,
                subscription.billing_mode,
            )
            invoice_currency = preview_invoice_currencies.get(preview_invoice_key)
            if invoice_currency is not None and invoice_currency != normalized_currency:
                summary["currency_skipped"] += 1
                summary["skipped"] += 1
                continue
            preview_invoice_currencies[preview_invoice_key] = normalized_currency
            preview_accounts.add(str(subscription.subscriber_id))
            preview_totals[normalized_currency] = preview_totals.get(
                normalized_currency, Decimal("0.00")
            ) + round_money(line_amount)
            preview_subscriptions.append(
                {
                    "id": str(subscription.id),
                    "account_id": str(subscription.subscriber_id),
                    "offer_name": offer_name,
                    "amount": round_money(line_amount),
                    "currency": normalized_currency,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "pending_activation": is_pending,
                }
            )
            summary["subscriptions_billed"] += 1
            summary["lines_created"] += 1
            if is_pending:
                summary["pending_activated"] += 1
            continue

        # Auto-activate pending subscription
        if is_pending and auto_activate_pending:
            _activate_pending_subscription(db, subscription, run_at)
            summary["pending_activated"] += 1

        account_id = str(subscription.subscriber_id)
        invoice_key = (
            account_id,
            period_start,
            period_end,
            subscription.billing_mode,
        )
        invoice = invoices.get(invoice_key)
        if not invoice:
            invoice_query = (
                db.query(Invoice)
                .filter(Invoice.account_id == subscription.subscriber_id)
                .filter(Invoice.billing_period_start == period_start)
                .filter(Invoice.billing_period_end == period_end)
                .filter(Invoice.is_active.is_(True))
            )
            invoice_query = invoice_query.filter(collectible_ar_invoice_filter())
            invoice = invoice_query.first()
            if invoice:
                invoices[invoice_key] = invoice
        if not invoice:
            # Use subscriber-level payment_due_days if set, else global
            account = db.get(Subscriber, subscription.subscriber_id)
            due_days = (
                resolve_payment_due_days(db, subscriber=account)
                if account
                else global_due_days
            )
            invoice = Invoices.stage_system_invoice(
                db,
                InvoiceCreate(
                    account_id=subscription.subscriber_id,
                    invoice_number=next_invoice_number(db),
                    status=InvoiceStatus.issued,
                    currency=currency or "NGN",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                    issued_at=run_at,
                    due_at=run_at + timedelta(days=due_days),
                ),
                reason="scheduled_billing_run",
            )
            invoices[invoice_key] = invoice
            newly_created_invoices.append(invoice)
            summary["invoices_created"] += 1
        elif currency and invoice.currency != currency:
            logger.warning(
                "billing_currency_mismatch_skip",
                extra={
                    "event": "billing_currency_mismatch_skip",
                    "subscription_id": str(subscription.id),
                    "subscriber_id": str(subscription.subscriber_id),
                    "subscription_currency": currency,
                    "invoice_currency": invoice.currency,
                },
            )
            summary["skipped"] += 1
            summary["currency_skipped"] += 1
            continue

        # Double-check for existing line on this specific invoice (belt and suspenders)
        existing_line = (
            db.query(InvoiceLine)
            .filter(InvoiceLine.invoice_id == invoice.id)
            .filter(InvoiceLine.subscription_id == subscription.id)
            .filter(InvoiceLine.description == description)
            .filter(InvoiceLine.is_active.is_(True))
            .first()
        )
        if existing_line:
            summary["skipped"] += 1
            continue

        billing_line_key = _billing_line_key(
            subscription.id, period_start, period_end, "base"
        )
        if _billing_line_key_exists(db, billing_line_key):
            summary["skipped"] += 1
            continue

        tax_rate_id = _resolve_tax_rate_id(db, subscription)
        InvoiceLines.stage_system_line(
            db,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description=description,
                quantity=Decimal("1.000"),
                unit_price=round_money(line_amount),
                amount=round_money(line_amount),
                tax_rate_id=tax_rate_id,
                tax_application=(
                    _default_tax_application(db)
                    if tax_rate_id
                    else TaxApplication.exempt
                ),
                metadata_={
                    "kind": "base_subscription",
                    "billing_period_start": period_start.isoformat(),
                    "billing_period_end": period_end.isoformat(),
                },
                billing_line_key=billing_line_key,
            ),
            reason="scheduled_base_subscription",
        )
        summary["subscriptions_billed"] += 1
        summary["lines_created"] += 1
        # Bill active recurring add-ons (e.g. extra IP blocks) on the same invoice.
        summary["lines_created"] += _bill_recurring_addons(
            db,
            invoice,
            subscription,
            period_start,
            period_end,
            usage_start,
            usage_end,
            tax_rate_id,
        )
        if subscription.billing_mode != BillingMode.prepaid:
            subscription.next_billing_at = period_end

    if dry_run:
        summary["invoices_created"] = len(preview_invoice_currencies)
        summary["accounts_affected"] = len(preview_accounts)
        summary["subscriptions"] = preview_subscriptions
        summary["totals_by_currency"] = preview_totals
        summary["total_amount"] = sum(
            preview_totals.values(),
            start=Decimal("0.00"),
        )
        summary["run_id"] = None
        logger.info(
            "billing_run_dry_run_complete",
            extra=_billing_run_extra(
                run_uuid=None,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
                summary=summary,
            ),
        )
        return summary

    try:
        # Persist the invoice lines just added in the loop before recalculating
        # totals and settling credit. _recalculate_invoice_totals and
        # settle_open_invoices_from_credit read rows back via queries, which only
        # see flushed rows when the session has autoflush disabled (the test
        # harness does); these flushes are harmless no-ops under default autoflush.
        db.flush()
        # Recalculate totals for all invoices
        for invoice in invoices.values():
            _recalculate_invoice_totals(db, invoice)
        # Recalc writes balance_due/status onto the invoice objects; flush so the
        # credit settlement below sees the open balance via its query.
        db.flush()

        # Canonical account credit is always offered to newly-created
        # receivables. The settlement owner validates current invoice state and
        # records structural allocation evidence; this is not a runtime option.
        if newly_created_invoices:
            from contextlib import nullcontext

            from app.services.notification_suppression import suppress_notifications

            touched_account_ids = {
                str(invoice.account_id) for invoice in newly_created_invoices
            }
            # Restore can emit "service resumed" notifications; suppress them for
            # a bulk catch-up run so we don't burst-mail a large suspended cohort.
            restore_notify_ctx = (
                suppress_notifications()
                if suppress_restore_notifications
                else nullcontext()
            )
            with restore_notify_ctx:
                for account_id in touched_account_ids:
                    account = db.get(Subscriber, coerce_uuid(account_id))
                    was_walled = account is not None and account.status in (
                        SubscriberStatus.suspended,
                        SubscriberStatus.blocked,
                    )
                    try:
                        settle_result = settle_open_invoices_from_credit(db, account_id)
                    except Exception:
                        logger.exception(
                            "invoice_credit_settlement_failed",
                            extra={
                                "event": "invoice_credit_settlement_failed",
                                "run_id": str(run_uuid) if run_uuid else None,
                                "account_id": account_id,
                            },
                        )
                        continue
                    if settle_result.changed:
                        summary["credit_applied"] = round_money(
                            summary["credit_applied"] + settle_result.applied
                        )
                        summary["credit_settled_invoices"] += len(
                            settle_result.invoices_settled
                        )
                    # PaymentAllocations owns the exact transfer and hands a paid
                    # invoice to the access-reconciliation owner. This automation
                    # adapter only reports that resulting state transition; it must
                    # not run a second restoration decision path.
                    if (
                        settle_result.changed
                        and was_walled
                        and account is not None
                        and account.status == SubscriberStatus.active
                    ):
                        summary["accounts_restored"] += 1

        db.commit()

        # Emit invoice.created events for newly created invoices
        run_id_str = str(run_uuid) if run_uuid else None
        for invoice in newly_created_invoices:
            try:
                _emit_invoice_created_event(db, invoice, run_id_str)
            except Exception as event_exc:
                logger.warning(
                    "Failed to emit invoice.created event for %s: %s",
                    invoice.id,
                    event_exc,
                )

        # The permanent hourly notification runner owns pre-due reminders so
        # they honour the configured send window. Policy dunning separately
        # owns every overdue notification and consequence.
        summary["invoice_reminders_sent"] = 0
        db.commit()

        summary["run_id"] = run_id_str
        run_db = db.get(BillingRun, run_uuid) if run_uuid else None
        if run_db:
            run_db.status = BillingRunStatus.success
            run_db.finished_at = datetime.now(UTC)
            run_db.subscriptions_scanned = summary["subscriptions_scanned"]
            run_db.subscriptions_billed = summary["subscriptions_billed"]
            run_db.invoices_created = summary["invoices_created"]
            run_db.lines_created = summary["lines_created"]
            run_db.skipped = summary["skipped"]
        db.commit()

        # Log the run to audit AFTER the success status is committed, and
        # never let it fail the run: this exact write failing (a NOT NULL
        # violation on audit_events) marked every 2026 billing run "failed"
        # while the invoices it had created survived.
        try:
            _log_billing_run_audit(db, run_db, summary, "success")
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "billing_run_audit_log_failed", extra={"run_id": run_id_str}
            )

        logger.info(
            "Billing run completed: %d invoices, %d lines, %d activated",
            summary["invoices_created"],
            summary["lines_created"],
            summary["pending_activated"],
            extra=_billing_run_extra(
                run_uuid=run_uuid,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
                summary=summary,
            ),
        )
        return summary

    except Exception as exc:
        db.rollback()
        error_msg = str(exc)
        logger.error(
            "Billing run failed: %s",
            error_msg,
            extra=_billing_run_extra(
                run_uuid=run_uuid,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
                summary=summary,
                error=error_msg,
            ),
        )

        run_db = db.get(BillingRun, run_uuid) if run_uuid else None
        if run_db:
            run_db.status = BillingRunStatus.failed
            run_db.finished_at = datetime.now(UTC)
            run_db.error = error_msg
            db.commit()

        # Log failed billing run to audit
        try:
            _log_billing_run_audit(db, run_db, summary, "failed", error_msg)
            db.commit()
        except Exception as audit_exc:
            logger.warning("Failed to log billing run audit: %s", audit_exc)

        raise


def generate_prorated_invoice(
    db: Session,
    subscription: Subscription,
    activation_date: datetime | None = None,
) -> Invoice | None:
    """Generate a prorated invoice for a subscription that starts mid-cycle.

    This should be called when a subscription is activated mid-billing-cycle
    to charge for the partial period until the next regular billing date.

    Args:
        db: Database session
        subscription: The subscription to generate prorated invoice for
        activation_date: The activation date (defaults to now)

    Returns:
        The created invoice or None if no proration is needed
    """
    # Prepaid subscriptions draw down a deposit and are never invoiced; a
    # mid-cycle activation must not mint a balance-due proration invoice.
    if not subscription_invoice_eligible(subscription):
        logger.info(
            "Skipping prorated invoice for prepaid subscription %s",
            subscription.id,
        )
        return None
    activation_date = _as_utc(activation_date) or datetime.now(UTC)

    # Get price info
    amount, currency, cycle = _resolve_price(db, subscription)
    if amount is None:
        logger.warning(
            "No price found for subscription %s, skipping proration", subscription.id
        )
        return None
    amount = _effective_unit_price(subscription, amount, activation_date)

    effective_cycle = cycle or BillingCycle.monthly

    # Calculate billing period start based on activation date
    # Use the activation date as the period start for proration
    # The period end will be calculated based on the billing cycle
    period_start = activation_date.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = _period_end(period_start, effective_cycle)

    # For monthly billing, check if we should align to month boundaries
    # If activation is on the 1st, no proration needed (full month)
    if effective_cycle == BillingCycle.monthly and period_start.day == 1:
        return None

    # For annual billing, if activation is on Jan 1st, no proration needed
    if (
        effective_cycle == BillingCycle.annual
        and period_start.month == 1
        and period_start.day == 1
    ):
        return None

    # Calculate prorated amount
    line_amount = _prorated_amount(
        amount, period_start, period_end, activation_date, period_end
    )

    if line_amount <= Decimal("0.00"):
        return None

    # Check for existing prorated invoice for this period. Key on the
    # day-floored period_start (not the raw activation timestamp): two
    # activations on the same day would otherwise carry sub-second-different
    # billing_period_start values, miss this dedupe, and mint a second proration.
    existing = (
        db.query(InvoiceLine)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(InvoiceLine.subscription_id == subscription.id)
        .filter(Invoice.billing_period_start == period_start)
        .filter(Invoice.billing_period_end == period_end)
        .filter(InvoiceLine.is_active.is_(True))
        .filter(Invoice.is_active.is_(True))
        .first()
    )
    if existing:
        logger.debug(
            "Prorated invoice already exists for subscription %s", subscription.id
        )
        return None

    # Get due days — subscriber override > global setting
    account = db.get(Subscriber, subscription.subscriber_id)
    due_days = resolve_payment_due_days(db, subscriber=account)

    # Create prorated invoice
    invoice = Invoices.stage_system_invoice(
        db,
        InvoiceCreate(
            account_id=subscription.subscriber_id,
            invoice_number=next_invoice_number(db),
            status=InvoiceStatus.issued,
            currency=currency or "NGN",
            # Store the day-floored period_start so the dedupe above is stable
            # across re-activations on the same day (issued_at keeps the exact
            # instant).
            billing_period_start=period_start,
            billing_period_end=period_end,
            issued_at=activation_date,
            due_at=activation_date + timedelta(days=due_days),
        ),
        reason="prorated_subscription_activation",
    )

    tax_rate_id = _resolve_tax_rate_id(db, subscription)
    offer_name = (
        subscription.offer.name
        if subscription.offer
        else f"Subscription {subscription.id}"
    )
    description = (
        f"{offer_name} (Prorated: {activation_date.date()} - {period_end.date()})"
    )

    InvoiceLines.stage_system_line(
        db,
        SystemInvoiceLineCreate(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description=description,
            quantity=Decimal("1.000"),
            unit_price=round_money(line_amount),
            amount=round_money(line_amount),
            tax_rate_id=tax_rate_id,
            tax_application=_default_tax_application(db),
        ),
        reason="prorated_subscription_activation",
    )

    # Set next billing date to the end of this prorated period
    subscription.next_billing_at = period_end

    _recalculate_invoice_totals(db, invoice)
    db.commit()
    db.refresh(invoice)

    # Emit event
    _emit_invoice_created_event(db, invoice, None)

    logger.info(
        "Generated prorated invoice %s for subscription %s: %s %s",
        invoice.id,
        subscription.id,
        line_amount,
        currency,
    )

    return invoice


def run_invoice_cycle_with_retry(
    db: Session,
    run_at: datetime | None = None,
    billing_cycle: BillingCycle | None = None,
    dry_run: bool = False,
    include_pending: bool = True,
    auto_activate_pending: bool = True,
    max_retries: int = 3,
    retry_delay_seconds: int = 5,
) -> dict[str, Any]:
    """Run the billing cycle with automatic retry on transient failures.

    Args:
        db: Database session
        run_at: The reference time for the billing run
        billing_cycle: Optional filter for billing cycle
        dry_run: If True, don't create records
        include_pending: If True, include pending subscriptions
        auto_activate_pending: If True, auto-activate pending subscriptions
        max_retries: Maximum number of retry attempts
        retry_delay_seconds: Delay between retries

    Returns:
        Summary dict of the billing run results
    """
    import time

    from sqlalchemy.exc import IntegrityError, OperationalError

    last_error: BaseException | None = None
    for attempt in range(max_retries):
        try:
            logger.info(
                "billing_run_attempt_start",
                extra=_billing_run_extra(
                    run_uuid=None,
                    run_at=_as_utc(run_at) or datetime.now(UTC),
                    billing_cycle=billing_cycle,
                    dry_run=dry_run,
                    include_pending=include_pending,
                    auto_activate_pending=auto_activate_pending,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                ),
            )
            return run_invoice_cycle(
                db=db,
                run_at=run_at,
                billing_cycle=billing_cycle,
                dry_run=dry_run,
                include_pending=include_pending,
                auto_activate_pending=auto_activate_pending,
            )
        except (OperationalError, IntegrityError) as exc:
            last_error = exc
            logger.warning(
                "Billing run attempt %d/%d failed: %s",
                attempt + 1,
                max_retries,
                exc,
                extra=_billing_run_extra(
                    run_uuid=None,
                    run_at=_as_utc(run_at) or datetime.now(UTC),
                    billing_cycle=billing_cycle,
                    dry_run=dry_run,
                    include_pending=include_pending,
                    auto_activate_pending=auto_activate_pending,
                    error=str(exc),
                    attempt=attempt + 1,
                    max_retries=max_retries,
                ),
            )
            if attempt < max_retries - 1:
                db.rollback()
                time.sleep(retry_delay_seconds)
                continue
            raise
        except Exception:
            # Don't retry non-transient errors
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("Billing run failed but no exception was captured")


def mark_overdue_invoices(db: Session) -> dict[str, int]:
    """Mark past-due invoices as overdue and emit events.

    Runs independently of the billing cycle. Finds invoices where
    ``due_at <= now``, ``balance_due > 0``, and status is ``issued``
    or ``partially_paid``, then transitions them to ``overdue`` and
    emits one observational ``invoice_overdue`` event. Policy dunning alone
    decides overdue notifications and service-access consequences.
    """
    now = datetime.now(UTC)
    invoices = (
        db.query(Invoice)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            )
        )
        .filter(Invoice.due_at.is_not(None))
        .filter(Invoice.due_at <= now)
        .filter(Invoice.balance_due > Decimal("0.00"))
        .all()
    )
    prepaid_non_ar_ids = set(db.scalars(prepaid_non_ar_invoice_ids()))

    marked = 0
    errors = 0
    skipped_on_hold = 0
    for invoice in invoices:
        # Check idempotency flag before changing status
        metadata = dict(invoice.metadata_ or {})
        # Reconciliation hold: invoices flagged as under reconciliation (e.g. the
        # phantom duplicate-billing cleanup) must not be marked overdue or
        # drive suspension/dunning. Setting this flag immediately stops dunning
        # before the invoices are voided.
        if metadata.get("reconciliation_hold") or apply_prepaid_overlap_hold(
            db, invoice
        ):
            skipped_on_hold += 1
            continue
        if invoice.id in prepaid_non_ar_ids:
            continue
        try:
            transition = Invoices.mark_overdue_system(
                db,
                str(invoice.id),
                as_of=now,
                reason="scheduled_overdue_detection",
            )
            if transition.changed or transition.event_emitted:
                marked += 1
        except Exception as exc:
            logger.error("Failed to process overdue invoice %s: %s", invoice.id, exc)
            errors += 1

    if marked or skipped_on_hold:
        db.commit()

    logger.info(
        "Overdue detection: %d marked, %d errors, %d scanned",
        marked,
        errors,
        len(invoices),
    )
    return {
        "marked_overdue": marked,
        "errors": errors,
        "scanned": len(invoices),
        "skipped_on_hold": skipped_on_hold,
    }


def generate_cancellation_credit(
    db: Session,
    subscription: Subscription,
) -> None:
    """Generate a credit note for unused days when a subscription is canceled mid-cycle.

    Generates when the subscription has been billed (has at least one invoice
    line). The credit covers the unused portion from cancellation date to
    next_billing_at.
    """
    from app.schemas.billing import CreditNoteIssuePreviewRequest
    from app.services.billing.credit_notes import CreditNotes

    if not subscription.next_billing_at:
        return

    now = datetime.now(UTC)
    next_billing = _as_utc(subscription.next_billing_at)
    if not next_billing or next_billing <= now:
        return  # No unused future period

    # Find the most recent invoice line for this subscription
    last_line = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.subscription_id == subscription.id)
        .filter(InvoiceLine.is_active.is_(True))
        .order_by(InvoiceLine.created_at.desc())
        .first()
    )
    if not last_line or not last_line.amount:
        return  # Never billed

    # Calculate unused portion. Resolve the cycle the same way the invoice
    # runner and proration do — from the active recurring price (version-aware) —
    # so the credit window matches how the subscription was actually billed,
    # falling back to the offer's cycle then monthly.
    _, _, resolved_cycle = _resolve_price(db, subscription)
    cycle = resolved_cycle or (
        subscription.offer.billing_cycle
        if subscription.offer and subscription.offer.billing_cycle
        else BillingCycle.monthly
    )

    period_start = _as_utc(subscription.next_billing_at)
    if period_start:
        # Work backwards: period_start = next_billing_at - cycle
        if cycle == BillingCycle.daily:
            period_start = period_start - timedelta(days=1)
        elif cycle == BillingCycle.weekly:
            period_start = period_start - timedelta(weeks=1)
        elif cycle == BillingCycle.annual:
            period_start = period_start.replace(year=period_start.year - 1)
        else:  # monthly
            month = period_start.month - 1 or 12
            year = (
                period_start.year if period_start.month > 1 else period_start.year - 1
            )
            day = min(period_start.day, monthrange(year, month)[1])
            period_start = period_start.replace(year=year, month=month, day=day)

    if not period_start:
        return

    total_seconds = max((next_billing - period_start).total_seconds(), 1)
    unused_seconds = max((next_billing - now).total_seconds(), 0)
    ratio = Decimal(str(unused_seconds)) / Decimal(str(total_seconds))
    credit_amount = (last_line.amount * ratio).quantize(Decimal("0.01"))

    if credit_amount <= Decimal("0.00"):
        return

    tax_rate = last_line.tax_rate
    tax_amount = _calculate_tax_amount(
        credit_amount,
        tax_rate.rate if tax_rate else Decimal("0.00"),
        last_line.tax_application,
    )
    if last_line.tax_application == TaxApplication.inclusive:
        credit_subtotal = credit_amount - tax_amount
        credit_total = credit_amount
    else:
        credit_subtotal = credit_amount
        credit_total = credit_amount + tax_amount

    offer_name = subscription.offer.name if subscription.offer else "Subscription"
    result = CreditNotes.issue_system(
        db,
        CreditNoteIssuePreviewRequest(
            account_id=subscription.subscriber_id,
            invoice_id=last_line.invoice_id,
            currency=last_line.invoice.currency,
            subtotal=credit_subtotal,
            tax_total=tax_amount,
            total=credit_total,
            memo=(
                f"Cancellation credit: {offer_name} "
                f"(unused {int(unused_seconds / 86400)} days)"
            ),
            line_description=f"Prorated credit for {offer_name}",
            line_tax_rate_id=last_line.tax_rate_id,
            line_tax_application=last_line.tax_application,
        ),
        idempotency_key=f"cancel-credit-{subscription.id}-{last_line.id}",
        commit=False,
    )

    logger.info(
        "Generated cancellation credit %s for subscription %s: %s",
        result.credit_note.credit_number or result.credit_note.id,
        subscription.id,
        credit_amount,
    )
