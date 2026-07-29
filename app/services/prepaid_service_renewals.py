"""Owner for funding one due prepaid service period from customer position.

Payment settlement records confirmed money and emits a funding-change event;
it never creates service debit or entitlement evidence. This owner handles both
payment-triggered and scheduled renewal decisions. It posts one preview-bound
account adjustment, links one active service entitlement to that exact debit,
and advances the subscription anchor in the caller's transaction.
"""

from __future__ import annotations

import enum
import hashlib
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    AddOnPrice,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.idempotency import IdempotencyKey
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionStatus,
)
from app.models.subscriber import Address, Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import AccountAdjustmentPreviewRequest
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.billing.adjustments import (
    ACCOUNT_ADJUSTMENT_SCOPE,
    AccountAdjustmentError,
    AccountAdjustmentOrigin,
    PreviewAccountAdjustmentQuery,
    StageSystemAccountAdjustmentCommand,
    preview_account_adjustment,
    stage_system_account_adjustment,
)
from app.services.common import coerce_uuid, round_money
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_wallet_debit,
    prepaid_entitlement_coverage_end,
)

logger = logging.getLogger(__name__)

_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
    }
)
_MAX_AUTOMATIC_LAG = timedelta(days=2)


class PrepaidServiceRenewalError(DomainError):
    """Transport-neutral renewal failure."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidServiceRenewalError(
        code=f"financial.prepaid_service_renewals.{suffix}",
        message=message,
        details=details,
    )


def _adjustment_error(exc: AccountAdjustmentError) -> NoReturn:
    _error(
        "adjustment_rejected",
        exc.message,
        account_adjustment_code=exc.code,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _origin_ref(subscription_id: object, starts_at: datetime, ends_at: datetime) -> str:
    return f"{subscription_id}:{starts_at.isoformat()}:{ends_at.isoformat()}"


def _idempotency_key(origin_ref: str) -> str:
    return "prepaid-renewal-" + hashlib.sha256(origin_ref.encode("utf-8")).hexdigest()


def resolve_prepaid_monthly_charge(
    db: Session,
    subscription: Subscription,
    effective_at: datetime,
) -> tuple[Decimal, str, BillingCycle] | None:
    """Resolve one canonical taxed monthly renewal amount."""
    return resolve_prepaid_monthly_charges(
        db,
        [subscription],
        effective_at,
    )[subscription.id]


def _newest_price(rows: Sequence[OfferPrice | OfferVersionPrice]):
    return max(rows, key=lambda row: (row.created_at, str(row.id))) if rows else None


def _matching_catalog_tax_rate_id(
    rates: Sequence[TaxRate], vat_percent: Decimal | None
) -> UUID | None:
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
    for rate in rates:
        if Decimal(str(rate.rate)) in candidates:
            return rate.id
    return None


def resolve_prepaid_monthly_charges(
    db: Session,
    subscriptions: Sequence[Subscription],
    effective_at: datetime,
) -> dict[UUID, tuple[Decimal, str, BillingCycle] | None]:
    """Resolve exact contracted renewal charges with bounded query cost.

    Both renewal and enforcement consume this owner. Contract amount lives on
    ``Subscription.unit_price``; catalog rows provide currency/cadence metadata
    only. Tax precedence exactly matches recurring invoice billing: service
    address, account, then offer/default.
    """
    from app.services.billing._common import _calculate_tax_amount
    from app.services.billing_automation import (
        _default_tax_application,
        _default_tax_rate_id,
        _effective_unit_price,
    )

    rows = list(subscriptions)
    result: dict[UUID, tuple[Decimal, str, BillingCycle] | None] = {
        subscription.id: None for subscription in rows
    }
    eligible = [
        subscription
        for subscription in rows
        if subscription.unit_price is not None and subscription.unit_price > 0
    ]
    if not eligible:
        return result

    version_ids = {
        subscription.offer_version_id
        for subscription in eligible
        if subscription.offer_version_id is not None
    }
    offer_ids = {subscription.offer_id for subscription in eligible}
    version_prices: dict[UUID, list[OfferVersionPrice]] = defaultdict(list)
    if version_ids:
        for version_price in db.scalars(
            select(OfferVersionPrice).where(
                OfferVersionPrice.offer_version_id.in_(version_ids),
                OfferVersionPrice.price_type == PriceType.recurring,
                OfferVersionPrice.is_active.is_(True),
            )
        ).all():
            version_prices[version_price.offer_version_id].append(version_price)
    offer_prices: dict[UUID, list[OfferPrice]] = defaultdict(list)
    if offer_ids:
        for offer_price in db.scalars(
            select(OfferPrice).where(
                OfferPrice.offer_id.in_(offer_ids),
                OfferPrice.price_type == PriceType.recurring,
                OfferPrice.is_active.is_(True),
            )
        ).all():
            offer_prices[offer_price.offer_id].append(offer_price)

    offers = {
        offer.id: offer
        for offer in db.scalars(
            select(CatalogOffer).where(CatalogOffer.id.in_(offer_ids))
        ).all()
    }
    account_ids = {subscription.subscriber_id for subscription in eligible}
    account_tax_ids: dict[UUID, UUID | None] = {
        account_id: tax_rate_id
        for account_id, tax_rate_id in db.execute(
            select(Subscriber.id, Subscriber.tax_rate_id).where(
                Subscriber.id.in_(account_ids)
            )
        ).all()
    }
    address_ids = {
        subscription.service_address_id
        for subscription in eligible
        if subscription.service_address_id is not None
    }
    address_tax_ids: dict[UUID, UUID | None] = (
        {
            address_id: tax_rate_id
            for address_id, tax_rate_id in db.execute(
                select(Address.id, Address.tax_rate_id).where(
                    Address.id.in_(address_ids)
                )
            ).all()
        }
        if address_ids
        else {}
    )
    active_rates = list(
        db.scalars(select(TaxRate).where(TaxRate.is_active.is_(True))).all()
    )
    rates_by_id = {rate.id: rate for rate in active_rates}
    default_tax_rate_id = _default_tax_rate_id(db)
    tax_application = _default_tax_application(db)

    for subscription in eligible:
        price: OfferPrice | OfferVersionPrice | None = None
        if subscription.offer_version_id is not None:
            price = _newest_price(version_prices.get(subscription.offer_version_id, []))
        if price is None:
            price = _newest_price(offer_prices.get(subscription.offer_id, []))
        if price is None:
            continue
        cycle = (
            subscription.billing_cycle or price.billing_cycle or BillingCycle.monthly
        )
        if cycle != BillingCycle.monthly:
            continue
        base = _effective_unit_price(subscription, price.amount, effective_at)
        tax_rate_id = (
            address_tax_ids.get(subscription.service_address_id)
            if subscription.service_address_id is not None
            else None
        )
        if tax_rate_id not in rates_by_id:
            tax_rate_id = account_tax_ids.get(subscription.subscriber_id)
        if tax_rate_id not in rates_by_id:
            offer = offers.get(subscription.offer_id)
            tax_rate_id = None
            if offer is not None:
                tax_rate_id = _matching_catalog_tax_rate_id(
                    active_rates, offer.vat_percent
                )
                if tax_rate_id is None and (
                    bool(offer.with_vat)
                    or Decimal(str(offer.vat_percent or "0")) > Decimal("0.00")
                ):
                    tax_rate_id = default_tax_rate_id
        tax_rate = rates_by_id.get(tax_rate_id) if tax_rate_id is not None else None
        if tax_rate is None or tax_application == TaxApplication.exempt:
            total = base
        else:
            tax_amount = _calculate_tax_amount(
                base,
                Decimal(str(tax_rate.rate)),
                tax_application,
            )
            total = (
                base
                if tax_application == TaxApplication.inclusive
                else round_money(base + tax_amount)
            )
        result[subscription.id] = (total, price.currency or "NGN", cycle)
    return result


@dataclass(frozen=True)
class PrepaidServiceRenewalPreview:
    account_id: UUID
    subscription_id: UUID
    starts_at: datetime
    ends_at: datetime
    amount: Decimal
    currency: str
    funding_before: Decimal
    funding_after: Decimal
    shortfall: Decimal
    allowed: bool
    fingerprint: str
    idempotency_key: str
    origin_ref: str
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PrepaidRecurringChargePreview:
    """Typed current-owner result for one complete candidate prepaid period."""

    subscription_id: UUID
    account_id: UUID
    period_start: datetime
    period_end: datetime
    gross_amount: Decimal
    currency: str
    billing_cycle: BillingCycle
    excluded_recurring_addon_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class PrepaidServiceRenewalResult:
    preview: PrepaidServiceRenewalPreview
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    entitlement: ServiceEntitlement
    replayed: bool


class PrepaidServiceRenewalSource(enum.StrEnum):
    direct_payment = "direct_payment"
    account_credit = "account_credit"
    scheduled = "scheduled"


@dataclass(frozen=True)
class PrepaidServiceRenewedOutcome:
    """Exact customer-visible result of one forward prepaid renewal."""

    event_id: UUID
    account_id: UUID
    subscription_id: UUID
    entitlement_id: UUID
    ledger_entry_id: UUID
    period_start: datetime
    renewed_through: datetime
    amount: Decimal
    currency: str
    source: PrepaidServiceRenewalSource
    trigger_payment_id: UUID | None = None


class FundingChangeRenewalDisposition(enum.StrEnum):
    no_due_service = "no_due_service"
    payable_invoice_remaining = "payable_invoice_remaining"
    draft_invoice_settled = "draft_invoice_settled"
    draft_invoice_pending = "draft_invoice_pending"
    draft_invoice_review_required = "draft_invoice_review_required"
    funded = "funded"
    unfunded = "unfunded"
    already_covered = "already_covered"
    missing_price = "missing_price"
    currency_mismatch = "currency_mismatch"
    non_cash_granted = "non_cash_granted"
    treatment_blocked = "treatment_blocked"


class FundingChangeEvaluationDisposition(enum.StrEnum):
    """Terminal result of validating one settlement-triggered renewal request."""

    evaluated = "evaluated"
    consolidated_invoice_allocation = "consolidated_invoice_allocation"


@dataclass(frozen=True)
class FundingChangeEvaluation:
    """Durable-handler result for one confirmed funding event."""

    payment_id: UUID
    disposition: FundingChangeEvaluationDisposition
    renewal: FundingChangeRenewalResult | None = None


@dataclass(frozen=True)
class FundingChangeRenewalResult:
    account_id: UUID
    scanned: int
    funded: int
    unfunded: int
    already_covered: int
    missing_price: int
    currency_mismatch: int
    disposition: FundingChangeRenewalDisposition
    renewals: tuple[PrepaidServiceRenewedOutcome, ...] = ()
    non_cash_granted: int = 0
    treatment_blocked: int = 0
    draft_invoices_settled: int = 0
    draft_invoices_pending: int = 0
    draft_review_exceptions: int = 0


def preview_prepaid_recurring_charge(
    db: Session,
    *,
    subscription_id: UUID,
    as_of: datetime,
) -> PrepaidRecurringChargePreview:
    """Resolve the current prepaid owner's next base-service charge.

    The result is read-only migration evidence. It deliberately preserves the
    current monthly-only and stale-anchor constraints so ADR 0007 Phase 2 can
    distinguish parity from newly supported cadence and unresolved legacy
    policy instead of silently treating them as equal.
    """

    if as_of.tzinfo is None:
        _error(
            "invalid_effective_at",
            "Prepaid charge preview requires a timezone-aware instant.",
        )
    effective_at = _utc(as_of)
    subscription = _subscription_for_request(db, subscription_id)
    if subscription.billing_mode is not BillingMode.prepaid:
        _error(
            "mode_not_prepaid",
            "The current prepaid owner cannot preview a postpaid subscription.",
        )
    if subscription.status not in PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES:
        _error(
            "subscription_not_eligible",
            "The current prepaid owner excludes this subscription state.",
        )
    period_start_value = subscription.next_billing_at or subscription.start_at
    if period_start_value is None:
        _error(
            "missing_anchor",
            "The current prepaid owner requires a billing-period anchor.",
        )
    period_start = _utc(period_start_value)
    if (
        period_start <= effective_at
        and effective_at - period_start > _MAX_AUTOMATIC_LAG
    ):
        _error(
            "stale_anchor",
            "The current prepaid owner quarantines a stale billing anchor.",
        )
    resolved = resolve_prepaid_monthly_charge(db, subscription, effective_at)
    if resolved is None:
        effective_cycle = subscription.billing_cycle or subscription.offer.billing_cycle
        if effective_cycle is not BillingCycle.monthly:
            _error(
                "unsupported_cadence",
                "The current prepaid owner supports monthly renewal only.",
            )
        _error(
            "missing_price",
            "The current prepaid owner cannot resolve a recurring price.",
        )
    amount, currency, cycle = resolved
    from app.services.billing_automation import _period_end

    period_end = _period_end(period_start, cycle)
    excluded_recurring_addon_ids = tuple(
        sorted(
            set(
                db.execute(
                    select(SubscriptionAddOn.id)
                    .join(
                        AddOnPrice,
                        AddOnPrice.add_on_id == SubscriptionAddOn.add_on_id,
                    )
                    .where(
                        SubscriptionAddOn.subscription_id == subscription.id,
                        (SubscriptionAddOn.start_at.is_(None))
                        | (SubscriptionAddOn.start_at < period_end),
                        (SubscriptionAddOn.end_at.is_(None))
                        | (SubscriptionAddOn.end_at > period_start),
                        AddOnPrice.price_type == PriceType.recurring,
                        AddOnPrice.is_active.is_(True),
                    )
                ).scalars()
            ),
            key=str,
        )
    )
    return PrepaidRecurringChargePreview(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        period_start=period_start,
        period_end=period_end,
        gross_amount=round_money(amount),
        currency=str(currency).upper(),
        billing_cycle=cycle,
        excluded_recurring_addon_ids=excluded_recurring_addon_ids,
    )


def evaluate_prepaid_service_after_settlement(
    db: Session,
    *,
    account_id: UUID,
    payment_id: UUID,
    evidence_ref: str,
) -> FundingChangeEvaluation:
    """Validate settlement evidence and request its prepaid consequence.

    The event adapter must not silently accept incomplete money evidence. A
    failure raised here leaves the durable event handler attempt retryable. A
    consolidated payment is a terminal non-prepaid outcome because its money
    belongs to invoice allocations rather than one customer funding position.
    """

    payment = db.get(Payment, payment_id)
    if payment is None:
        _error(
            "payment_not_found",
            "Funding-change payment was not found.",
            payment_id=str(payment_id),
        )
    if payment.account_id is None:
        return FundingChangeEvaluation(
            payment_id=payment.id,
            disposition=(
                FundingChangeEvaluationDisposition.consolidated_invoice_allocation
            ),
        )
    if payment.account_id != account_id:
        _error(
            "payment_account_mismatch",
            "Funding-change payment belongs to a different account.",
            payment_id=str(payment.id),
            event_account_id=str(account_id),
            payment_account_id=str(payment.account_id),
        )
    if payment.status != PaymentStatus.succeeded or not payment.is_active:
        _error(
            "payment_not_settled",
            "Funding-change payment is not an active succeeded payment.",
            payment_id=str(payment.id),
            payment_status=payment.status.value,
            payment_is_active=payment.is_active,
        )
    settlement_id = db.scalar(
        select(PaymentSettlement.id).where(
            PaymentSettlement.payment_id == payment.id,
        )
    )
    if settlement_id is None:
        _error(
            "settlement_missing",
            "Funding-change payment has no settlement evidence.",
            payment_id=str(payment.id),
        )
    effective_at = payment.paid_at or payment.created_at
    if effective_at is None:
        _error(
            "settlement_time_missing",
            "Funding-change payment has no effective settlement time.",
            payment_id=str(payment.id),
        )
    # Project the anchor from the entitlement evidence this payment already
    # committed, before deciding whether any further period is due. Doing it
    # here rather than inside the renewal branch keeps the anchor exact even
    # when another payable invoice defers the invoice-less renewal path.
    for funded_invoice_id in _invoice_ids_touched_by_payment(db, payment.id):
        funded_invoice = db.get(Invoice, funded_invoice_id)
        if funded_invoice is None or funded_invoice.account_id != account_id:
            continue
        project_prepaid_billing_anchor_for_invoice(
            db,
            funded_invoice,
            evidence_ref=evidence_ref,
        )

    renewal = apply_due_prepaid_service_after_funding_change(
        db,
        account_id=account_id,
        effective_at=effective_at,
        funding_currency=payment.currency,
        evidence_ref=evidence_ref,
        trigger_payment_id=payment.id,
    )
    return FundingChangeEvaluation(
        payment_id=payment.id,
        disposition=FundingChangeEvaluationDisposition.evaluated,
        renewal=renewal,
    )


def _subscription_for_request(
    db: Session,
    subscription_id: object,
) -> Subscription:
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    if subscription.billing_mode != BillingMode.prepaid:
        _error(
            "ineligible_billing_mode",
            "Only a prepaid subscription can receive a funded service cycle.",
        )
    if subscription.status not in PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES:
        _error(
            "ineligible_status",
            "Subscription is not eligible for prepaid service renewal.",
        )
    return subscription


def _existing_period_entitlement(
    db: Session,
    *,
    subscription_id: object,
    starts_at: datetime,
    ends_at: datetime,
) -> ServiceEntitlement | None:
    return db.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.subscription_id == subscription_id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at < ends_at,
            ServiceEntitlement.ends_at > starts_at,
        )
    )


def preview_prepaid_service_renewal(
    db: Session,
    *,
    subscription_id: object,
    starts_at: datetime,
    ends_at: datetime,
    amount: Decimal,
    currency: str = "NGN",
) -> PrepaidServiceRenewalPreview:
    subscription = _subscription_for_request(db, subscription_id)
    period_start = _utc(starts_at)
    period_end = _utc(ends_at)
    if period_end <= period_start:
        _error("invalid_period", "Renewal period must be positive.")
    charge = round_money(amount)
    if charge <= Decimal("0.00"):
        _error("invalid_amount", "Renewal amount must be positive.")
    unit = str(currency).strip().upper()
    if len(unit) != 3:
        _error("invalid_currency", "Renewal currency is invalid.")

    origin_ref = _origin_ref(subscription.id, period_start, period_end)
    idempotency_key = _idempotency_key(origin_ref)
    overlap = _existing_period_entitlement(
        db,
        subscription_id=subscription.id,
        starts_at=period_start,
        ends_at=period_end,
    )
    if overlap is not None:
        existing_adjustment = db.scalar(
            select(AccountAdjustment).where(
                AccountAdjustment.origin == _ORIGIN,
                AccountAdjustment.idempotency_key == idempotency_key,
            )
        )
        if (
            existing_adjustment is not None
            and overlap.source_ledger_entry_id == existing_adjustment.ledger_entry_id
            and overlap.account_id == subscription.subscriber_id
            and _utc(overlap.starts_at) == period_start
            and _utc(overlap.ends_at) == period_end
            and round_money(overlap.amount_funded) == charge
            and overlap.currency == unit
        ):
            return PrepaidServiceRenewalPreview(
                account_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                starts_at=period_start,
                ends_at=period_end,
                amount=charge,
                currency=unit,
                funding_before=round_money(existing_adjustment.prepaid_funding_before),
                funding_after=round_money(existing_adjustment.prepaid_funding_after),
                shortfall=Decimal("0.00"),
                allowed=True,
                fingerprint=existing_adjustment.preview_fingerprint,
                idempotency_key=idempotency_key,
                origin_ref=origin_ref,
                replayed=True,
            )
        _error(
            "period_already_funded",
            "Prepaid service period already has active funding evidence.",
        )

    try:
        adjustment_preview = preview_account_adjustment(
            db,
            PreviewAccountAdjustmentQuery(
                request=AccountAdjustmentPreviewRequest(
                    account_id=subscription.subscriber_id,
                    category=LedgerCategory.internet_service,
                    amount=charge,
                    currency=unit,
                    memo=(
                        "Prepaid service renewal "
                        f"{period_start.date()} - {period_end.date()}"
                    ),
                    reason="Funded prepaid service period",
                ),
                origin=_ORIGIN,
                origin_ref=origin_ref,
            ),
        )
    except AccountAdjustmentError as exc:
        _adjustment_error(exc)
    return PrepaidServiceRenewalPreview(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        starts_at=period_start,
        ends_at=period_end,
        amount=charge,
        currency=unit,
        funding_before=adjustment_preview.prepaid_funding_before,
        funding_after=adjustment_preview.prepaid_funding_after,
        shortfall=adjustment_preview.shortfall,
        allowed=adjustment_preview.allowed,
        fingerprint=adjustment_preview.fingerprint,
        idempotency_key=idempotency_key,
        origin_ref=origin_ref,
    )


def confirm_prepaid_service_renewal(
    db: Session,
    preview: PrepaidServiceRenewalPreview,
    *,
    evidence_ref: str,
) -> PrepaidServiceRenewalResult:
    """Lock, re-preview, and atomically stage debit + entitlement + anchor."""
    evidence = evidence_ref.strip()
    if not evidence:
        _error("missing_evidence_ref", "An evidence reference is required.")

    # Serialize the idempotency lookup with the funding re-preview and write.
    # Looking up the adjustment before this lock let two concurrent callers
    # both observe "missing"; the second caller then re-previewed after the
    # first committed and failed with a stale fingerprint instead of returning
    # the already-recorded renewal.
    lock_account(db, str(preview.account_id))
    existing_adjustment = db.scalar(
        select(AccountAdjustment).where(
            AccountAdjustment.origin == _ORIGIN,
            AccountAdjustment.idempotency_key == preview.idempotency_key,
        )
    )
    if existing_adjustment is not None:
        entitlement = db.scalar(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_ledger_entry_id
                == existing_adjustment.ledger_entry_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        )
        if (
            existing_adjustment.preview_fingerprint != preview.fingerprint
            or existing_adjustment.account_id != preview.account_id
            or entitlement is None
            or entitlement.subscription_id != preview.subscription_id
            or _utc(entitlement.starts_at) != preview.starts_at
            or _utc(entitlement.ends_at) != preview.ends_at
            or round_money(entitlement.amount_funded) != preview.amount
            or entitlement.currency != preview.currency
        ):
            _error(
                "idempotency_conflict",
                "Prepaid renewal idempotency evidence does not match the request.",
            )
        return PrepaidServiceRenewalResult(
            preview=preview,
            adjustment=existing_adjustment,
            ledger_entry=existing_adjustment.ledger_entry,
            entitlement=entitlement,
            replayed=True,
        )

    subscription = _subscription_for_request(db, preview.subscription_id)
    db.refresh(subscription)
    current = preview_prepaid_service_renewal(
        db,
        subscription_id=subscription.id,
        starts_at=preview.starts_at,
        ends_at=preview.ends_at,
        amount=preview.amount,
        currency=preview.currency,
    )
    if current.fingerprint != preview.fingerprint:
        _error(
            "stale_preview",
            "Prepaid funding changed after preview; preview again.",
        )
    if not current.allowed:
        _error(
            "insufficient_funding",
            "Insufficient prepaid funding for service renewal.",
        )

    try:
        adjustment_result = stage_system_account_adjustment(
            db,
            StageSystemAccountAdjustmentCommand(
                context=CommandContext.system(
                    actor="system:prepaid_service_renewals",
                    scope=ACCOUNT_ADJUSTMENT_SCOPE,
                    reason="Stage one funded prepaid service-period debit",
                    idempotency_key=current.idempotency_key,
                ),
                request=AccountAdjustmentPreviewRequest(
                    account_id=current.account_id,
                    category=LedgerCategory.internet_service,
                    amount=current.amount,
                    currency=current.currency,
                    memo=(
                        "Prepaid service renewal "
                        f"{current.starts_at.date()} - {current.ends_at.date()}"
                    ),
                    reason="Funded prepaid service period",
                ),
                origin=_ORIGIN,
                origin_ref=current.origin_ref,
                idempotency_key=current.idempotency_key,
                ledger_effective_date=current.starts_at,
            ),
        )
    except AccountAdjustmentError as exc:
        _adjustment_error(exc)
    entitlement = ensure_prepaid_entitlement_for_wallet_debit(
        db,
        subscription=subscription,
        ledger_entry=adjustment_result.ledger_entry,
        starts_at=current.starts_at,
        ends_at=current.ends_at,
    )
    if entitlement is None:
        _error(
            "incomplete_entitlement",
            "Prepaid renewal did not produce exact entitlement evidence.",
        )
    metadata = dict(entitlement.metadata_ or {})
    metadata.update(
        {
            "evidence_ref": evidence,
            "preview_fingerprint": current.fingerprint,
            "idempotency_key": current.idempotency_key,
        }
    )
    entitlement.metadata_ = metadata
    if (
        subscription.next_billing_at is None
        or _utc(subscription.next_billing_at) < current.ends_at
    ):
        subscription.next_billing_at = current.ends_at
    db.flush()
    return PrepaidServiceRenewalResult(
        preview=current,
        adjustment=adjustment_result.adjustment,
        ledger_entry=adjustment_result.ledger_entry,
        entitlement=entitlement,
        replayed=adjustment_result.replayed,
    )


def stage_prepaid_service_renewed_outcome(
    db: Session,
    *,
    account_id: UUID,
    subscription_id: UUID,
    entitlement_id: UUID,
    ledger_entry_id: UUID,
    period_start: datetime,
    renewed_through: datetime,
    amount: Decimal,
    currency: str,
    source: PrepaidServiceRenewalSource,
    trigger_payment_id: UUID | None = None,
) -> PrepaidServiceRenewedOutcome:
    """Stage the exact forward-renewal outcome beside its financial writes."""
    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType

    starts_at = _utc(period_start)
    ends_at = _utc(renewed_through)
    charge = round_money(amount)
    event = emit_event(
        db,
        EventType.prepaid_service_renewed,
        {
            "schema_version": 1,
            "subscription_id": str(subscription_id),
            "entitlement_id": str(entitlement_id),
            "ledger_entry_id": str(ledger_entry_id),
            "trigger_payment_id": (
                str(trigger_payment_id) if trigger_payment_id else None
            ),
            "amount": str(charge),
            "currency": currency,
            "period_start": starts_at.isoformat(),
            "renewed_through": ends_at.isoformat(),
            "source": source.value,
        },
        actor="system:prepaid_service_renewals",
        account_id=account_id,
        subscription_id=subscription_id,
    )
    return PrepaidServiceRenewedOutcome(
        event_id=event.event_id,
        account_id=account_id,
        subscription_id=subscription_id,
        entitlement_id=entitlement_id,
        ledger_entry_id=ledger_entry_id,
        period_start=starts_at,
        renewed_through=ends_at,
        amount=charge,
        currency=currency,
        source=source,
        trigger_payment_id=trigger_payment_id,
    )


def renewal_outcomes_for_payment(
    db: Session,
    payment_id: UUID,
) -> tuple[PrepaidServiceRenewedOutcome, ...]:
    """Return canonical renewal outcomes explicitly linked to one payment."""
    from app.models.event_store import EventStore
    from app.services.events.types import EventType

    rows = list(
        db.scalars(
            select(EventStore)
            .where(
                EventStore.event_type == EventType.prepaid_service_renewed.value,
                EventStore.is_active.is_(True),
                EventStore.payload["trigger_payment_id"].as_string() == str(payment_id),
            )
            .order_by(EventStore.created_at, EventStore.id)
        ).all()
    )
    outcomes: list[PrepaidServiceRenewedOutcome] = []
    for row in rows:
        payload = row.payload or {}
        if row.account_id is None or row.subscription_id is None:
            continue
        try:
            outcomes.append(
                PrepaidServiceRenewedOutcome(
                    event_id=row.event_id,
                    account_id=row.account_id,
                    subscription_id=row.subscription_id,
                    entitlement_id=UUID(str(payload["entitlement_id"])),
                    ledger_entry_id=UUID(str(payload["ledger_entry_id"])),
                    period_start=_utc(datetime.fromisoformat(payload["period_start"])),
                    renewed_through=_utc(
                        datetime.fromisoformat(payload["renewed_through"])
                    ),
                    amount=round_money(Decimal(str(payload["amount"]))),
                    currency=str(payload["currency"]),
                    source=PrepaidServiceRenewalSource(str(payload["source"])),
                    trigger_payment_id=payment_id,
                )
            )
        except (KeyError, TypeError, ValueError):
            # Malformed historical events are not a basis for a customer claim.
            continue
    return tuple(outcomes)


class BillingAnchorAuthority(enum.StrEnum):
    """How much authority the caller has to move an anchor backwards.

    Before this owner existed, `financial.payments` ran two different anchor
    policies from two finalizers, and both are load-bearing:

    * ``_finalize_invoice_payment_effects`` (payment creation, allocation,
      refund, reversal) re-anchored a lapsed prepaid invoice and deliberately
      carried its *inferred* extension delta forward, never writing the anchor
      backwards.
    * ``finalize_invoice_application_for_owner`` (reviewed prepaid-draft
      reconciliation) additionally projected the anchor unconditionally from
      the exact entitlements, overriding that inferred delta.

    Collapsing them into one policy is what made this projection alternately
    claw back granted service or strand a lapsed invoice at a stale anchor, so
    authority is an explicit input rather than something guessed from state.
    """

    #: A payment settled, was allocated, or was reversed. The trigger observes
    #: that funding changed; it says nothing about why the anchor is ahead.
    #: That lead may be a `financial.service_extensions` grant or the payment
    #: owner's own preserved delta, so it is never overwritten downwards.
    funding_observation = "funding_observation"

    #: A named owner is deliberately correcting the record from a reviewed,
    #: fingerprint-bound, operator-confirmed preview, having just rewritten the
    #: invoice's documentary period. It may set the anchor onto exact projected
    #: coverage even when that is earlier than the current anchor.
    reviewed_reconciliation = "reviewed_reconciliation"


@dataclass(frozen=True, slots=True)
class BillingAnchorProjection:
    """One owner-computed anchor decision for a single subscription."""

    subscription_id: UUID
    previous_next_billing_at: datetime | None
    next_billing_at: datetime | None
    coverage_end: datetime | None
    changed: bool
    retracted: bool
    authority: BillingAnchorAuthority = BillingAnchorAuthority.funding_observation


def project_prepaid_billing_anchor_for_invoice(
    db: Session,
    invoice: Invoice,
    *,
    evidence_ref: str,
    authority: BillingAnchorAuthority = BillingAnchorAuthority.funding_observation,
) -> tuple[BillingAnchorProjection, ...]:
    """Recompute affected billing anchors from canonical entitlement evidence.

    ``financial.prepaid_service_renewals`` is the sole owner of billing-anchor
    advancement. Payment allocation, invoice application, and draft
    reconciliation are participants: they commit exact entitlement evidence and
    then ask this owner to project it. They never write ``next_billing_at``
    themselves, so there is exactly one writer for the projection.

    The result is a pure function of current coverage state and the caller's
    declared authority, which makes it idempotent under replay:

    ``coverage`` is the union of active ``ServiceEntitlement`` intervals and
    applied ``ServiceExtensionEntry`` grant intervals — exactly what
    ``financial.prepaid_service_coverage`` treats as evidence. The anchor never
    lands below that union, and never below the start of the period this
    invoice funded.

    On top of that floor, ``authority`` decides one question: may the anchor
    move BACKWARDS past an unexplained lead?

    * ``funding_observation`` — no. A payment settling is an observation that
      funding changed; it carries no statement about why the anchor is ahead.
      That lead may be a ``financial.service_extensions`` grant, a
      ``financial.subscription_billing_grants`` grant, or the extension delta
      the payment owner deliberately preserved in the same transaction while
      re-anchoring a lapsed renewal. Overwriting it would silently claw back
      service another owner granted, so advancement is monotonic while this
      invoice's own entitlements survive.
    * ``reviewed_reconciliation`` — yes. A named owner has just rewritten this
      invoice's documentary period from an operator-confirmed, fingerprint-
      bound preview and holds exact entitlement evidence for it. A stale anchor
      left behind by a long-lapsed period carries no grant, and is precisely
      what ``financial.prepaid_service_coverage`` classifies as an unresolved
      projection: never restoration or suspension authority. A reviewed
      correction may resolve it downwards. This stays sound because the floor
      above still applies — reviewed authority can only pull the anchor down
      ONTO existing coverage, never below it, so it can delete an evidence-free
      lead but can never cancel granted service.

    Retraction after a refund, chargeback, reversal, or reallocation needs no
    special authority: once this invoice's entitlements are revoked they leave
    the coverage union, and the anchor follows the evidence down on its own.
    """

    rows = db.execute(
        select(
            ServiceEntitlement.subscription_id,
            ServiceEntitlement.starts_at,
            ServiceEntitlement.status,
        )
        .where(ServiceEntitlement.source_invoice_id == invoice.id)
        .order_by(ServiceEntitlement.subscription_id, ServiceEntitlement.starts_at)
    ).all()
    if not rows:
        return ()

    unfunded_start_by_subscription: dict[UUID, datetime] = {}
    # Whether THIS invoice still funds the subscription. Losing its entitlement
    # is what authorizes a retraction; an untouched invoice never may.
    invoice_still_funds: dict[UUID, bool] = {}
    for subscription_id, starts_at, status in rows:
        current = unfunded_start_by_subscription.get(subscription_id)
        candidate = _utc(starts_at)
        if current is None or candidate < current:
            unfunded_start_by_subscription[subscription_id] = candidate
        if status == ServiceEntitlementStatus.active:
            invoice_still_funds[subscription_id] = True
        else:
            invoice_still_funds.setdefault(subscription_id, False)

    subscription_ids = list(unfunded_start_by_subscription)
    coverage_rows = db.execute(
        select(
            ServiceEntitlement.subscription_id,
            ServiceEntitlement.ends_at,
        ).where(
            ServiceEntitlement.subscription_id.in_(subscription_ids),
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
        )
    ).all()
    coverage_end_by_subscription: dict[UUID, datetime] = {}
    for subscription_id, ends_at in coverage_rows:
        current = coverage_end_by_subscription.get(subscription_id)
        candidate = _utc(ends_at)
        if current is None or candidate > current:
            coverage_end_by_subscription[subscription_id] = candidate

    # `financial.service_extensions` owns its own billing-anchor projection and
    # records one immutable grant interval per subscription. Those intervals are
    # coverage evidence exactly as `financial.prepaid_service_coverage` reads
    # them, so they must be visible here too — otherwise a retraction would
    # silently undo another owner's anchor projection.
    for subscription_id, grant_ends_at in db.execute(
        select(
            ServiceExtensionEntry.subscription_id,
            ServiceExtensionEntry.grant_ends_at,
        )
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscription_id.in_(subscription_ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
        )
    ).all():
        current = coverage_end_by_subscription.get(subscription_id)
        candidate = _utc(grant_ends_at)
        if current is None or candidate > current:
            coverage_end_by_subscription[subscription_id] = candidate

    projections: list[BillingAnchorProjection] = []
    changed_any = False
    for subscription_id, unfunded_start in unfunded_start_by_subscription.items():
        subscription = db.get(Subscription, subscription_id)
        if subscription is None or subscription.subscriber_id != invoice.account_id:
            continue
        previous = (
            _utc(subscription.next_billing_at)
            if subscription.next_billing_at is not None
            else None
        )
        coverage_end = coverage_end_by_subscription.get(subscription_id)
        # The floor every authority shares: surviving coverage, but never
        # leaving the period this invoice funded looking covered when it is not
        # (an extension that already expired cannot vouch for it).
        floor = (
            max(coverage_end, unfunded_start)
            if coverage_end is not None
            else unfunded_start
        )
        target: datetime
        if coverage_end is None and previous is not None and previous < unfunded_start:
            # Nothing survives and an earlier unpaid period is already due.
            # Never push a due anchor later.
            target = previous
        elif (
            authority is BillingAnchorAuthority.funding_observation
            and invoice_still_funds.get(subscription_id)
            and previous is not None
        ):
            # Observational trigger, nothing revoked: monotonic. An anchor
            # ahead of coverage may be a grant this owner cannot see.
            target = max(previous, floor)
        else:
            # Reviewed correction, or a retraction the evidence already forces.
            target = floor
        retracted = previous is not None and target < previous
        changed = target != previous
        if changed:
            subscription.next_billing_at = target
            changed_any = True
        projections.append(
            BillingAnchorProjection(
                subscription_id=subscription_id,
                previous_next_billing_at=previous,
                next_billing_at=target,
                coverage_end=coverage_end,
                changed=changed,
                retracted=retracted and changed,
                authority=authority,
            )
        )
    if changed_any:
        db.flush()
    if projections:
        logger.info(
            "prepaid_billing_anchor_projected",
            extra={
                "event": "prepaid_billing_anchor_projected",
                "invoice_id": str(invoice.id),
                "account_id": str(invoice.account_id),
                "evidence_ref": evidence_ref,
                "authority": authority.value,
                "projections": [
                    {
                        "subscription_id": str(item.subscription_id),
                        "previous_next_billing_at": (
                            item.previous_next_billing_at.isoformat()
                            if item.previous_next_billing_at
                            else None
                        ),
                        "next_billing_at": (
                            item.next_billing_at.isoformat()
                            if item.next_billing_at
                            else None
                        ),
                        "coverage_end": (
                            item.coverage_end.isoformat() if item.coverage_end else None
                        ),
                        "changed": item.changed,
                        "retracted": item.retracted,
                    }
                    for item in projections
                ],
            },
        )
    return tuple(projections)


def _invoice_ids_touched_by_payment(db: Session, payment_id: UUID) -> tuple[UUID, ...]:
    """Return every invoice this payment ever allocated to, retired included."""

    return tuple(
        dict.fromkeys(
            db.scalars(
                select(PaymentAllocation.invoice_id)
                .where(PaymentAllocation.payment_id == payment_id)
                .order_by(PaymentAllocation.invoice_id)
            ).all()
        )
    )


def retract_prepaid_billing_anchors_after_funding_reversal(
    db: Session,
    *,
    account_id: UUID,
    payment_id: UUID,
    invoice_ids: Sequence[UUID] = (),
    evidence_ref: str,
) -> tuple[BillingAnchorProjection, ...]:
    """Re-project anchors after a refund, chargeback, or reversal.

    The payment owner revokes the entitlements its money had funded and then
    emits the durable reversal event. This owner — the only writer of
    ``next_billing_at`` — re-derives the anchor from what evidence survives, so
    a reversed period can never keep a stale advanced anchor claiming service
    the customer no longer paid for. Recomputation makes replay idempotent.
    """

    targets = tuple(invoice_ids) or _invoice_ids_touched_by_payment(db, payment_id)
    projections: list[BillingAnchorProjection] = []
    for invoice_id in targets:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None or invoice.account_id != account_id:
            continue
        projections.extend(
            project_prepaid_billing_anchor_for_invoice(
                db,
                invoice,
                evidence_ref=evidence_ref,
            )
        )
    return tuple(projections)


STALE_BILLING_ANCHOR_REPAIR_SCOPE = "prepaid_billing_anchor_repair"
_STALE_BILLING_ANCHOR_REPAIR_ACTION = "repair_stale_prepaid_billing_anchor"


@dataclass(frozen=True, slots=True)
class StaleBillingAnchorCandidate:
    """One subscription whose anchor understates its exact funded coverage."""

    subscription_id: UUID
    account_id: UUID
    current_next_billing_at: datetime
    coverage_end: datetime

    @property
    def drift(self) -> timedelta:
        return self.coverage_end - self.current_next_billing_at


@dataclass(frozen=True, slots=True)
class StaleBillingAnchorRepairPreview:
    """Fingerprint-bound view of the outstanding anchor-drift cohort."""

    as_of: datetime
    candidates: tuple[StaleBillingAnchorCandidate, ...]
    fingerprint: str
    truncated: bool

    @property
    def cohort_size(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True, slots=True)
class StaleBillingAnchorRepairResult:
    """Exact outcome of one repair pass."""

    scanned: int
    repaired: int
    already_correct: int
    skipped_changed: int
    replayed: int
    repaired_subscription_ids: tuple[UUID, ...]


def _stale_billing_anchor_candidates(
    db: Session,
    *,
    limit: int,
    subscription_ids: Sequence[UUID] = (),
) -> tuple[tuple[StaleBillingAnchorCandidate, ...], bool]:
    coverage = (
        select(
            ServiceEntitlement.subscription_id.label("subscription_id"),
            func.max(ServiceEntitlement.ends_at).label("coverage_end"),
        )
        .where(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .group_by(ServiceEntitlement.subscription_id)
        .subquery()
    )
    query = (
        select(
            Subscription.id,
            Subscription.subscriber_id,
            Subscription.next_billing_at,
            coverage.c.coverage_end,
        )
        .join(coverage, coverage.c.subscription_id == Subscription.id)
        .where(
            Subscription.next_billing_at.isnot(None),
            coverage.c.coverage_end > Subscription.next_billing_at,
        )
        .order_by(Subscription.next_billing_at, Subscription.id)
    )
    if subscription_ids:
        query = query.where(Subscription.id.in_(list(subscription_ids)))
    rows = db.execute(query.limit(limit + 1)).all()
    truncated = len(rows) > limit
    candidates = tuple(
        StaleBillingAnchorCandidate(
            subscription_id=row[0],
            account_id=row[1],
            current_next_billing_at=_utc(row[2]),
            coverage_end=_utc(row[3]),
        )
        for row in rows[:limit]
    )
    return candidates, truncated


def _stale_billing_anchor_fingerprint(
    candidates: Sequence[StaleBillingAnchorCandidate],
) -> str:
    material = "|".join(
        f"{item.subscription_id}:"
        f"{item.current_next_billing_at.isoformat()}:"
        f"{item.coverage_end.isoformat()}"
        for item in candidates
    )
    return hashlib.sha256(
        f"prepaid-billing-anchor-repair:{material}".encode()
    ).hexdigest()


def preview_stale_prepaid_billing_anchor_repair(
    db: Session,
    *,
    limit: int = 500,
    subscription_ids: Sequence[UUID] = (),
) -> StaleBillingAnchorRepairPreview:
    """Report subscriptions whose anchor lags their exact funded coverage.

    This is the pre-existing drift cohort created while the payment-allocation
    path committed entitlements without ever reaching this owner: an active
    ``ServiceEntitlement`` ends after ``Subscription.next_billing_at``, so the
    customer has paid for service the billing anchor says is already due. It is
    a different cohort from ``scripts/one_off/backfill_next_billing_at.py``,
    which repairs NULL or historically-past anchors with no coverage evidence.

    Read-only. No money is posted, moved, or forgiven.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    candidates, truncated = _stale_billing_anchor_candidates(
        db, limit=limit, subscription_ids=subscription_ids
    )
    return StaleBillingAnchorRepairPreview(
        as_of=datetime.now(UTC),
        candidates=candidates,
        fingerprint=_stale_billing_anchor_fingerprint(candidates),
        truncated=truncated,
    )


def apply_stale_prepaid_billing_anchor_repair(
    db: Session,
    preview: StaleBillingAnchorRepairPreview,
    *,
    actor: str,
    reason: str,
    commit: bool = True,
) -> StaleBillingAnchorRepairResult:
    """Advance every previewed anchor to its exact funded coverage end.

    Idempotent by construction and by reservation. The write is a pure
    recomputation from surviving entitlement evidence, so a repaired row leaves
    the cohort permanently and a replay of the same candidate is a no-op that
    reuses its existing idempotency reservation and audit evidence. A candidate
    whose coverage changed between preview and apply is skipped, never guessed
    at, and shows up in the next preview.
    """

    if not actor.strip() or not reason.strip():
        raise ValueError("actor and reason are required repair evidence")

    scanned = 0
    repaired = 0
    already_correct = 0
    skipped_changed = 0
    replayed = 0
    repaired_ids: list[UUID] = []
    for candidate in preview.candidates:
        scanned += 1
        lock_account(db, str(candidate.account_id))
        subscription = db.get(Subscription, candidate.subscription_id)
        if subscription is None:
            skipped_changed += 1
            continue
        current, truncated_scan = _stale_billing_anchor_candidates(
            db, limit=1, subscription_ids=(candidate.subscription_id,)
        )
        del truncated_scan
        if not current:
            already_correct += 1
            continue
        fresh = current[0]
        if (
            fresh.current_next_billing_at != candidate.current_next_billing_at
            or fresh.coverage_end != candidate.coverage_end
        ):
            skipped_changed += 1
            continue

        material = f"{candidate.subscription_id}:{candidate.coverage_end.isoformat()}"
        key = (
            "prepaid-billing-anchor-repair-"
            + hashlib.sha256(material.encode("utf-8")).hexdigest()
        )
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == STALE_BILLING_ANCHOR_REPAIR_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is not None:
            replayed += 1
            continue
        db.add(
            IdempotencyKey(
                scope=STALE_BILLING_ANCHOR_REPAIR_SCOPE,
                key=key,
                account_id=candidate.account_id,
                ref_id=str(candidate.subscription_id),
            )
        )
        subscription.next_billing_at = candidate.coverage_end
        db.flush()
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action=_STALE_BILLING_ANCHOR_REPAIR_ACTION,
                entity_type="subscription",
                entity_id=str(candidate.subscription_id),
                metadata_={
                    "owner": "financial.prepaid_service_renewals",
                    "account_id": str(candidate.account_id),
                    "actor": actor,
                    "reason": reason,
                    "preview_fingerprint": preview.fingerprint,
                    "previous_next_billing_at": (
                        candidate.current_next_billing_at.isoformat()
                    ),
                    "repaired_next_billing_at": candidate.coverage_end.isoformat(),
                    "drift_seconds": str(int(candidate.drift.total_seconds())),
                },
            ),
        )
        repaired += 1
        repaired_ids.append(candidate.subscription_id)

    db.flush()
    if commit:
        db.commit()
    logger.info(
        "prepaid_billing_anchor_repair_applied",
        extra={
            "event": "prepaid_billing_anchor_repair_applied",
            "preview_fingerprint": preview.fingerprint,
            "actor": actor,
            "reason": reason,
            "scanned": scanned,
            "repaired": repaired,
            "already_correct": already_correct,
            "skipped_changed": skipped_changed,
            "replayed": replayed,
        },
    )
    return StaleBillingAnchorRepairResult(
        scanned=scanned,
        repaired=repaired,
        already_correct=already_correct,
        skipped_changed=skipped_changed,
        replayed=replayed,
        repaired_subscription_ids=tuple(repaired_ids),
    )


def _payable_invoice_exists(
    db: Session,
    *,
    account_id: UUID,
    currency: str,
) -> bool:
    return (
        db.scalar(
            select(Invoice.id)
            .where(
                Invoice.account_id == account_id,
                Invoice.is_active.is_(True),
                Invoice.status.in_(
                    {
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    }
                ),
                Invoice.currency == currency,
                Invoice.balance_due > Decimal("0.00"),
            )
            .limit(1)
        )
        is not None
    )


def apply_due_prepaid_service_after_funding_change(
    db: Session,
    *,
    account_id: UUID,
    effective_at: datetime,
    funding_currency: str,
    evidence_ref: str,
    trigger_payment_id: UUID | None = None,
) -> FundingChangeRenewalResult:
    """Consume newly available funding for currently due prepaid service.

    Payment settlement, account-credit settlement and invoice allocation remain
    separate owners. Their completed funding-change event invokes this owner
    only after ordinary payable invoices have had first claim on the credit. A
    lapsed service starts a new period on the payment day; missed inactive
    periods are never back-billed.
    """
    evaluated_at = _utc(effective_at)
    currency = str(funding_currency or "").strip().upper()
    if len(currency) != 3:
        raise ValueError("funding_currency must be a three-letter code")
    evidence = evidence_ref.strip()
    if not evidence:
        raise ValueError("evidence_ref is required")

    # Invoice-first invariant: an existing prepaid draft owns the documentary
    # service-period boundary. Exact verified funding settles that draft; a
    # shortfall (including NGN 0.50), unbacked credit, overlap, or ambiguity
    # leaves it unchanged and blocks the parallel invoice-less renewal path.
    from app.services.prepaid_draft_reconciliation import (
        stage_prepaid_draft_after_funding_change,
    )

    draft_result = stage_prepaid_draft_after_funding_change(
        db,
        account_id=account_id,
        currency=currency,
        effective_at=evaluated_at,
    )
    if draft_result.drafts_found:
        settled = draft_result.drafts_settled
        pending = draft_result.drafts_blocked
        return FundingChangeRenewalResult(
            account_id=account_id,
            scanned=draft_result.drafts_found,
            funded=settled,
            unfunded=pending,
            already_covered=0,
            missing_price=0,
            currency_mismatch=0,
            disposition=(
                FundingChangeRenewalDisposition.draft_invoice_settled
                if settled
                else (
                    FundingChangeRenewalDisposition.draft_invoice_review_required
                    if draft_result.review_exceptions
                    else FundingChangeRenewalDisposition.draft_invoice_pending
                )
            ),
            draft_invoices_settled=settled,
            draft_invoices_pending=pending,
            draft_review_exceptions=draft_result.review_exceptions,
        )

    due_subscriptions = list(
        db.scalars(
            select(Subscription)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.subscriber_id == account_id,
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES),
                Subscription.next_billing_at.isnot(None),
                Subscription.next_billing_at <= evaluated_at,
                CatalogOffer.billing_cycle == BillingCycle.monthly,
                CatalogOffer.is_active.is_(True),
            )
            .order_by(Subscription.next_billing_at, Subscription.id)
        ).all()
    )
    if not due_subscriptions:
        return FundingChangeRenewalResult(
            account_id=account_id,
            scanned=0,
            funded=0,
            unfunded=0,
            already_covered=0,
            missing_price=0,
            currency_mismatch=0,
            disposition=FundingChangeRenewalDisposition.no_due_service,
        )

    if _payable_invoice_exists(db, account_id=account_id, currency=currency):
        return FundingChangeRenewalResult(
            account_id=account_id,
            scanned=len(due_subscriptions),
            funded=0,
            unfunded=0,
            already_covered=0,
            missing_price=0,
            currency_mismatch=0,
            disposition=FundingChangeRenewalDisposition.payable_invoice_remaining,
        )

    from app.services.billing_automation import _period_end
    from app.services.subscription_billing_grants import (
        SubscriptionBillingGrantError,
        stage_subscription_billing_grant,
    )
    from app.services.subscription_billing_treatments import (
        SubscriptionBillingTreatmentError,
        resolve_subscription_billing_treatments,
    )

    funded = 0
    unfunded = 0
    already_covered = 0
    missing_price = 0
    currency_mismatch = 0
    renewals: list[PrepaidServiceRenewedOutcome] = []
    non_cash_granted = 0
    treatment_blocked = 0
    paid_day = evaluated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    treatment_decisions = resolve_subscription_billing_treatments(
        db, due_subscriptions, as_of=evaluated_at
    )
    charges = resolve_prepaid_monthly_charges(
        db,
        [
            subscription
            for subscription in due_subscriptions
            if not treatment_decisions[subscription.id].suppress_customer_billing
        ],
        evaluated_at,
    )
    for subscription in due_subscriptions:
        treatment = treatment_decisions[subscription.id]
        if treatment.suppress_customer_billing:
            if not treatment.grantable:
                treatment_blocked += 1
                continue
            anchor = _utc(subscription.next_billing_at or paid_day)
            period_start = max(anchor, paid_day, _utc(treatment.starts_at or paid_day))
            period_end = _period_end(period_start, BillingCycle.monthly)
            try:
                stage_subscription_billing_grant(
                    db,
                    subscription=subscription,
                    decision=treatment,
                    starts_at=period_start,
                    ends_at=period_end,
                    actor="system:prepaid_service_renewals",
                    correlation_id=trigger_payment_id,
                )
            except (
                SubscriptionBillingGrantError,
                SubscriptionBillingTreatmentError,
            ):
                treatment_blocked += 1
                continue
            non_cash_granted += 1
            continue
        charge = charges[subscription.id]
        if charge is None:
            missing_price += 1
            continue
        amount, charge_currency, cycle = charge
        if charge_currency != currency:
            currency_mismatch += 1
            continue
        anchor = _utc(subscription.next_billing_at or paid_day)
        period_start = max(anchor, paid_day)
        period_end = _period_end(period_start, cycle)
        paid_through = prepaid_entitlement_coverage_end(
            db,
            subscription_id=subscription.id,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
        if paid_through is not None and _utc(paid_through) > period_start:
            if anchor < _utc(paid_through):
                subscription.next_billing_at = paid_through
            already_covered += 1
            continue
        preview = preview_prepaid_service_renewal(
            db,
            subscription_id=subscription.id,
            starts_at=period_start,
            ends_at=period_end,
            amount=amount,
            currency=charge_currency,
        )
        if not preview.allowed:
            unfunded += 1
            continue
        renewal = confirm_prepaid_service_renewal(
            db,
            preview,
            evidence_ref=evidence,
        )
        if not renewal.replayed:
            renewals.append(
                stage_prepaid_service_renewed_outcome(
                    db,
                    account_id=renewal.preview.account_id,
                    subscription_id=renewal.preview.subscription_id,
                    entitlement_id=renewal.entitlement.id,
                    ledger_entry_id=renewal.ledger_entry.id,
                    period_start=renewal.preview.starts_at,
                    renewed_through=renewal.preview.ends_at,
                    amount=renewal.preview.amount,
                    currency=renewal.preview.currency,
                    source=PrepaidServiceRenewalSource.account_credit,
                    trigger_payment_id=trigger_payment_id,
                )
            )
        funded += 1

    db.flush()
    disposition = (
        FundingChangeRenewalDisposition.non_cash_granted
        if non_cash_granted
        else FundingChangeRenewalDisposition.treatment_blocked
        if treatment_blocked
        else FundingChangeRenewalDisposition.funded
        if funded
        else FundingChangeRenewalDisposition.already_covered
        if already_covered
        else FundingChangeRenewalDisposition.unfunded
        if unfunded
        else FundingChangeRenewalDisposition.missing_price
        if missing_price
        else FundingChangeRenewalDisposition.currency_mismatch
    )
    return FundingChangeRenewalResult(
        account_id=account_id,
        scanned=len(due_subscriptions),
        funded=funded,
        unfunded=unfunded,
        already_covered=already_covered,
        missing_price=missing_price,
        currency_mismatch=currency_mismatch,
        disposition=disposition,
        renewals=tuple(renewals),
        non_cash_granted=non_cash_granted,
        treatment_blocked=treatment_blocked,
    )


def run_due_prepaid_service_renewals(
    db: Session,
    *,
    run_at: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int | str]:
    """Fund currently due monthly periods without historical catch-up.

    The daily billing runner calls this only behind the canonical control. A
    stale anchor older than two days is reported for reviewed reconciliation,
    never silently back-billed. Global missing authority fails closed for the
    pass. Accounts excluded from the materialized authority cohort and an
    unexpected account-level missing baseline are reported and skipped so one
    unavailable position cannot block unrelated verified renewals.
    """
    from app.services.billing_automation import _period_end
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
        authority_cutover_batch,
        prepaid_funding_quarantined_account_ids,
    )

    effective_at = _utc(run_at or datetime.now(UTC))
    subscriptions = list(
        db.scalars(
            select(Subscription)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES),
                Subscription.next_billing_at.isnot(None),
                Subscription.next_billing_at <= effective_at,
                CatalogOffer.billing_cycle == BillingCycle.monthly,
                CatalogOffer.is_active.is_(True),
            )
            .order_by(Subscription.next_billing_at, Subscription.id)
        ).all()
    )
    from app.services.subscription_billing_grants import (
        SubscriptionBillingGrantError,
        stage_subscription_billing_grant,
    )
    from app.services.subscription_billing_treatments import (
        SubscriptionBillingTreatmentError,
        resolve_subscription_billing_treatments,
    )

    treatment_decisions = resolve_subscription_billing_treatments(
        db, subscriptions, as_of=effective_at
    )
    summary: dict[str, int | str] = {
        "prepaid_renewals_scanned": len(subscriptions),
        "prepaid_renewals_funded": 0,
        "prepaid_renewals_unfunded": 0,
        "prepaid_renewals_already_covered": 0,
        "prepaid_renewals_stale_anchor": 0,
        "prepaid_renewals_missing_price": 0,
        "prepaid_renewals_quarantined": 0,
        "prepaid_renewals_missing_baseline": 0,
        "prepaid_renewals_restored": 0,
        "prepaid_renewals_non_cash_granted": 0,
        "prepaid_renewals_treatment_blocked": 0,
    }
    chargeable_subscriptions: list[Subscription] = []
    for subscription in subscriptions:
        next_billing_at = subscription.next_billing_at
        if next_billing_at is None:
            continue
        treatment = treatment_decisions[subscription.id]
        if not treatment.suppress_customer_billing:
            chargeable_subscriptions.append(subscription)
            continue
        if not treatment.grantable:
            summary["prepaid_renewals_treatment_blocked"] = (
                int(summary["prepaid_renewals_treatment_blocked"]) + 1
            )
            continue
        period_start = max(
            _utc(next_billing_at), _utc(treatment.starts_at or next_billing_at)
        )
        period_end = _period_end(period_start, BillingCycle.monthly)
        if dry_run:
            summary["prepaid_renewals_non_cash_granted"] = (
                int(summary["prepaid_renewals_non_cash_granted"]) + 1
            )
            continue
        try:
            stage_subscription_billing_grant(
                db,
                subscription=subscription,
                decision=treatment,
                starts_at=period_start,
                ends_at=period_end,
                actor="system:prepaid_service_renewals",
            )
        except (
            SubscriptionBillingGrantError,
            SubscriptionBillingTreatmentError,
        ):
            summary["prepaid_renewals_treatment_blocked"] = (
                int(summary["prepaid_renewals_treatment_blocked"]) + 1
            )
            continue
        summary["prepaid_renewals_non_cash_granted"] = (
            int(summary["prepaid_renewals_non_cash_granted"]) + 1
        )

    authority = authority_cutover_batch(db)
    if authority is None:
        summary["prepaid_renewals_skipped"] = "authority_not_materialized"
        db.flush()
        return summary

    quarantined_account_ids = prepaid_funding_quarantined_account_ids(
        db,
        {subscription.subscriber_id for subscription in chargeable_subscriptions},
    )
    authority_at = _utc(authority.position_at)
    charges = resolve_prepaid_monthly_charges(
        db,
        chargeable_subscriptions,
        effective_at,
    )
    for subscription in chargeable_subscriptions:
        if subscription.subscriber_id in quarantined_account_ids:
            summary["prepaid_renewals_quarantined"] = (
                int(summary["prepaid_renewals_quarantined"]) + 1
            )
            continue
        next_billing_at = subscription.next_billing_at
        if next_billing_at is None:
            continue
        period_start = _utc(next_billing_at)
        lag = effective_at - period_start
        if period_start <= authority_at or lag > _MAX_AUTOMATIC_LAG:
            summary["prepaid_renewals_stale_anchor"] = (
                int(summary["prepaid_renewals_stale_anchor"]) + 1
            )
            continue
        charge = charges[subscription.id]
        if charge is None:
            summary["prepaid_renewals_missing_price"] = (
                int(summary["prepaid_renewals_missing_price"]) + 1
            )
            continue
        amount, currency, cycle = charge
        period_end = _period_end(period_start, cycle)
        paid_through = prepaid_entitlement_coverage_end(
            db,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            period_start=period_start,
            period_end=period_end,
        )
        if paid_through is not None and _utc(paid_through) > period_start:
            if not dry_run and period_start < _utc(paid_through):
                subscription.next_billing_at = paid_through
            summary["prepaid_renewals_already_covered"] = (
                int(summary["prepaid_renewals_already_covered"]) + 1
            )
            continue
        try:
            preview = preview_prepaid_service_renewal(
                db,
                subscription_id=subscription.id,
                starts_at=period_start,
                ends_at=period_end,
                amount=amount,
                currency=currency,
            )
        except PrepaidFundingBaselineMissingError:
            # A baseline may become unavailable after the quarantine snapshot
            # above. Preview is read-only, so isolating this account cannot
            # retain a partial renewal write.
            summary["prepaid_renewals_missing_baseline"] = (
                int(summary["prepaid_renewals_missing_baseline"]) + 1
            )
            continue
        if not preview.allowed:
            summary["prepaid_renewals_unfunded"] = (
                int(summary["prepaid_renewals_unfunded"]) + 1
            )
            continue
        if not dry_run:
            renewal = confirm_prepaid_service_renewal(
                db,
                preview,
                evidence_ref=(
                    "scheduled-billing-run:"
                    f"{effective_at.isoformat().replace('+00:00', 'Z')}"
                ),
            )
            if not renewal.replayed:
                stage_prepaid_service_renewed_outcome(
                    db,
                    account_id=renewal.preview.account_id,
                    subscription_id=renewal.preview.subscription_id,
                    entitlement_id=renewal.entitlement.id,
                    ledger_entry_id=renewal.ledger_entry.id,
                    period_start=renewal.preview.starts_at,
                    renewed_through=renewal.preview.ends_at,
                    amount=renewal.preview.amount,
                    currency=renewal.preview.currency,
                    source=PrepaidServiceRenewalSource.scheduled,
                )
            from app.models.collections import FinancialAccessOrigin
            from app.services.collections._core import restore_account_services

            restored = restore_account_services(
                db,
                str(subscription.subscriber_id),
                origin=FinancialAccessOrigin.prepaid_enforcement,
                resolved_by=(
                    "prepaid_service_renewal:"
                    f"{subscription.id}:{period_start.isoformat()}"
                ),
            )
            summary["prepaid_renewals_restored"] = (
                int(summary["prepaid_renewals_restored"]) + restored
            )
        summary["prepaid_renewals_funded"] = int(summary["prepaid_renewals_funded"]) + 1
    db.flush()
    return summary


__all__ = [
    "STALE_BILLING_ANCHOR_REPAIR_SCOPE",
    "BillingAnchorAuthority",
    "BillingAnchorProjection",
    "FundingChangeEvaluation",
    "FundingChangeEvaluationDisposition",
    "FundingChangeRenewalDisposition",
    "FundingChangeRenewalResult",
    "PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES",
    "PrepaidRecurringChargePreview",
    "PrepaidServiceRenewalPreview",
    "PrepaidServiceRenewalError",
    "PrepaidServiceRenewalResult",
    "PrepaidServiceRenewalSource",
    "PrepaidServiceRenewedOutcome",
    "StaleBillingAnchorCandidate",
    "StaleBillingAnchorRepairPreview",
    "StaleBillingAnchorRepairResult",
    "apply_due_prepaid_service_after_funding_change",
    "apply_stale_prepaid_billing_anchor_repair",
    "confirm_prepaid_service_renewal",
    "evaluate_prepaid_service_after_settlement",
    "preview_prepaid_service_renewal",
    "preview_prepaid_recurring_charge",
    "preview_stale_prepaid_billing_anchor_repair",
    "project_prepaid_billing_anchor_for_invoice",
    "renewal_outcomes_for_payment",
    "retract_prepaid_billing_anchors_after_funding_reversal",
    "resolve_prepaid_monthly_charge",
    "resolve_prepaid_monthly_charges",
    "run_due_prepaid_service_renewals",
    "stage_prepaid_service_renewed_outcome",
]
