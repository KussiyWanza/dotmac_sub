"""Owner for funding one due prepaid service period from customer position.

Payment settlement records confirmed money and emits a funding-change event;
it never creates service-consumption or entitlement evidence. This owner handles
both payment-triggered and scheduled renewal decisions. For a fully funded
period it coordinates one invoice, exact credit application, service entitlement,
and subscription-anchor advancement in the caller's transaction.
"""

from __future__ import annotations

import enum
import hashlib
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
    TaxApplication,
)
from app.models.billing_contract import (
    CadenceAlignment,
    CollectionTiming,
    EndOfMonthRule,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
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
from app.models.prepaid_funding import PrepaidOpeningFundingConsumption
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionStatus,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    AccountAdjustmentPreviewRequest,
    AccountAdjustmentReversalConfirm,
    AccountAdjustmentReversalPreviewRequest,
    InvoiceCreate,
    SystemInvoiceLineCreate,
)
from app.services.account_lifecycle import (
    BillingAnchorProjectionCommand,
    BillingAnchorProjectionSource,
    stage_subscription_billing_anchor,
)
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.adjustments import (
    AccountAdjustmentError,
    AccountAdjustmentOrigin,
    PreviewAccountAdjustmentQuery,
    PreviewAccountAdjustmentReversalQuery,
    ReverseAccountAdjustmentCommand,
    preview_account_adjustment,
    preview_account_adjustment_reversal,
    stage_account_adjustment_reversal_for_renewal_owner,
)
from app.services.billing.cadence import BillingCadence, service_period
from app.services.billing.invoices import (
    InvoiceIssuanceInput,
    InvoiceLines,
    InvoiceOwnerError,
    Invoices,
)
from app.services.billing_tax_resolution import resolve_subscription_taxes
from app.services.common import coerce_uuid, round_money
from app.services.domain_errors import DomainError
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
    execute_owner_savepoint,
)
from app.services.service_entitlements import prepaid_entitlement_coverage_end
from app.timezone import APP_TIMEZONE, APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.prepaid_draft_reconciliation import (
        ProspectiveFundingClassification,
    )

_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
_OWNER = "financial.prepaid_service_renewals"
_EXECUTION_CONCERN = "prepaid service renewal execution"
_REVIEWED_RENEWAL_CONCERN = "fingerprint-approved missed renewal execution"
_EVALUATE_SETTLEMENT_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_EXECUTION_CONCERN,
    name="execute_prepaid_service_after_settlement",
)
_RUN_DUE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_EXECUTION_CONCERN,
    name="execute_due_prepaid_service_renewals",
)
_EXECUTE_REVIEWED_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_REVIEWED_RENEWAL_CONCERN,
    name="execute_reviewed_prepaid_service_renewal",
)
_LEGACY_TAX_CORRECTION_CONCERN = (
    "reviewed legacy prepaid renewal tax-invoice correction"
)
_LEGACY_TAX_CORRECTION_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LEGACY_TAX_CORRECTION_CONCERN,
    name="correct_legacy_prepaid_renewal_tax_invoice",
)
_LEGACY_TAX_CORRECTION_SCOPE = "prepaid:legacy-renewal-tax-invoice-correction"
_LEGACY_TAX_CORRECTION_AUTH_SCOPE = "billing:ledger:write"
_LEGACY_TAX_CORRECTION_METADATA_KEY = "legacy_renewal_tax_invoice_correction"
PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
    }
)
_MAX_AUTOMATIC_LAG = timedelta(days=2)

# The scheduled-renewal pass summary carries plain counters plus (round-3
# nightly-isolation correction) a list of isolated-account entries and an
# honest overall status string -- never a bare `dict[str, int | str]`, which
# cannot represent the isolated-accounts list at all.
PrepaidRenewalSummaryValue = int | str | list[dict[str, str]]


def _bump_summary(
    summary: dict[str, PrepaidRenewalSummaryValue], key: str, by: int = 1
) -> None:
    """Increment one integer counter in the scheduled-renewal summary.

    A tiny helper rather than inline ``int(summary[key]) + by`` at every call
    site: `PrepaidRenewalSummaryValue` is a real union (counters, the status
    string, and the isolated-accounts list all share one dict), so a bare
    ``int(...)`` on that union does not type-check at every counter site --
    this asserts the narrower type once, here.
    """
    current = summary[key]
    assert isinstance(current, int)
    summary[key] = current + by


class PrepaidServiceRenewalError(DomainError):
    """Transport-neutral renewal failure."""


class PrepaidRenewalAmbiguousEvidenceError(PrepaidServiceRenewalError):
    """One due subscription's funding evidence is ambiguous, PRE-mutation.

    Raised only when classification (zero database writes so far) cannot
    cleanly resolve to settle-an-exact-draft, create-canonical, or the
    reviewed-opening-funding lane. This is one of exactly three named,
    isolatable account-scoped types (see also
    :class:`PrepaidOpeningLaneUnavailableError`,
    :class:`PrepaidTriggerExecutionConflictError`) that the automatic
    funding-event path (:func:`apply_due_prepaid_service_after_funding_change`)
    and the nightly scheduled pass (:func:`run_due_prepaid_service_renewals`)
    catch by an explicit, closed `isinstance` allowlist — never a bare
    ``retryable=False`` check or a generic ``DomainError``/``Exception``
    catch, which would silently isolate an unrelated, genuinely
    pass-aborting failure (a posting-owner failure, a DB/infrastructure
    error, a programming error). Each makes no financial/service-state
    change for the affected subscription, records a durable review item out
    of band, and lets processing continue for every OTHER due
    subscription/account in the same batch. Every OTHER caller of
    :func:`confirm_prepaid_service_renewal` (the operator-reviewed
    missed-renewal path) does not catch these and sees them propagate as an
    ordinary :class:`PrepaidServiceRenewalError` — unchanged, fail-closed
    behavior there. Always ``retryable=False``: none of the three resolve
    themselves on a bare retry.

    This is DISTINCT from a POST-mutation integrity failure (settlement
    already moved money and the evidence still doesn't match) — that case
    raises a plain :class:`PrepaidServiceRenewalError` instead, which is
    never caught here and always rolls back the whole transaction.
    """

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(
            code="financial.prepaid_service_renewals.ambiguous_evidence",
            message=message,
            details=details,
            retryable=False,
        )


class PrepaidOpeningLaneUnavailableError(PrepaidServiceRenewalError):
    """The reviewed-opening-funding lane could not classify, PRE-mutation.

    Raised specifically when a baseline-less account (or any other pure
    classification failure inside the opening-funding lookup,
    :func:`app.services.prepaid_draft_reconciliation.classify_prospective_prepaid_funding`)
    prevents even determining whether the reviewed-opening lane applies.
    Deliberately its OWN type rather than caught via the generic
    ``PrepaidDraftReconciliationError`` parent — the isolation allowlist
    must never accidentally isolate some future, unrelated
    ``PrepaidDraftReconciliationError`` subtype that is NOT safe to skip
    past.
    """

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(
            code="financial.prepaid_service_renewals.opening_lane_unavailable",
            message=message,
            details=details,
            retryable=False,
        )


class PrepaidTriggerExecutionConflictError(PrepaidServiceRenewalError):
    """The SAME durable event was already processed with different evidence.

    The permanent, non-retryable mismatched-replay conflict
    (``PrepaidFundingTriggerExecution.request_fingerprint`` disagrees with
    what this call now computes for the same ``event_store_id``). Its own
    named type for the identical reason as
    :class:`PrepaidOpeningLaneUnavailableError`: an isolation allowlist keyed
    on this exact type can never accidentally widen to catch an unrelated
    failure that happens to also be a plain, unmarked
    :class:`PrepaidServiceRenewalError`.
    """

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(
            code="financial.prepaid_service_renewals.trigger_execution_conflict",
            message=message,
            details=details,
            retryable=False,
        )


# The exact, closed set of account-scoped failures that are safe to isolate
# (roll back just this account/subscription's work and continue the batch)
# rather than abort the whole pass. Deliberately a tuple of specific named
# types, not `retryable=False` or a generic `DomainError`/`Exception` catch
# -- see each type's own docstring for why. Shared by the funding-event loop
# and the nightly scheduled pass so both isolate on exactly the same set.
PREPAID_RENEWAL_ISOLATABLE_ERRORS: tuple[type[PrepaidServiceRenewalError], ...] = (
    PrepaidRenewalAmbiguousEvidenceError,
    PrepaidOpeningLaneUnavailableError,
    PrepaidTriggerExecutionConflictError,
)


@dataclass(frozen=True, slots=True)
class EvaluatePrepaidServiceAfterSettlementCommand:
    """Typed public command for one durable funding-change event."""

    context: CommandContext
    account_id: UUID
    payment_id: UUID
    evidence_ref: str
    # The domain event's own id (``Event.event_id`` / ``EventStore.event_id``,
    # NOT the ``EventStore`` surrogate primary key). The durable dispatch path
    # always supplies this. A caller with no event identity at all (the
    # repair CLI -- see ``skip_receipt_for_repair`` below) must say so
    # explicitly; omitting ``event_id`` without that flag is a hard failure
    # (2026-09, round 7) rather than a silent unreceipted fallthrough.
    event_id: UUID | None = None
    # Narrows the due-subscription scan to exactly one subscription. Real
    # dispatch never sets this; the repair CLI does, so what actually gets
    # applied cannot exceed the one subscription/period its fingerprint-bound
    # preview committed to.
    only_subscription_id: UUID | None = None
    # Explicit, named opt-out of the trigger-receipt model for a call that
    # has no durable event to receipt against -- exactly the repair CLI's
    # case (``scripts/billing/repair_prepaid_funding_consequences.py``): a
    # repair is a fresh, current-state evaluation, not a replay of the
    # original event, and deliberately does not touch that event's
    # ``PrepaidFundingTriggerExecution`` receipt. Must be set ``True``
    # together with ``event_id=None``; any other caller omitting
    # ``event_id`` is refused rather than silently proceeding unreceipted.
    skip_receipt_for_repair: bool = False


@dataclass(frozen=True, slots=True)
class RunDuePrepaidServiceRenewalsCommand:
    """Typed public command for one bounded scheduled renewal pass."""

    context: CommandContext
    run_at: datetime
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PrepaidSettlementPeriodQuery:
    """Resolve the service period begun by one lapsed prepaid settlement."""

    effective_at: datetime
    billing_cycle: BillingCycle
    timezone_name: str = APP_TIMEZONE_NAME


@dataclass(frozen=True, slots=True)
class PrepaidSettlementPeriod:
    """UTC interval plus the business-calendar dates shown to operators."""

    starts_at: datetime
    ends_at: datetime
    starts_on: date
    ends_on: date
    timezone_name: str


@dataclass(frozen=True, slots=True)
class PrepaidSubscriptionSettlementPeriodQuery:
    """Resolve a paid period from exact current coverage or settlement day."""

    subscription_id: UUID
    account_id: UUID
    effective_at: datetime
    billing_cycle: BillingCycle
    timezone_name: str = APP_TIMEZONE_NAME
    exclude_source_invoice_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class PrepaidSubscriptionSettlementPeriod:
    """Selected paid period with the exact coverage boundary that selected it."""

    period: PrepaidSettlementPeriod
    covered_through: datetime | None


_SETTLEMENT_CYCLE_INTERVAL: dict[BillingCycle, tuple[IntervalUnit, int]] = {
    BillingCycle.daily: (IntervalUnit.day, 1),
    BillingCycle.weekly: (IntervalUnit.week, 1),
    BillingCycle.monthly: (IntervalUnit.month, 1),
    BillingCycle.quarterly: (IntervalUnit.month, 3),
    BillingCycle.annual: (IntervalUnit.year, 1),
}


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


def resolve_prepaid_settlement_period(
    query: PrepaidSettlementPeriodQuery,
) -> PrepaidSettlementPeriod:
    """Resolve a lapsed renewal from the settlement's local calendar day.

    Settlement timestamps are instants and remain UTC at persistence boundaries.
    The service anniversary is a business-calendar decision: the payment first
    crosses into the declared billing timezone, starts at that local midnight,
    and advances through the typed calendar cadence. This prevents a payment in
    the first hour of a Lagos day from being assigned to the previous UTC day.
    """

    interval_spec = _SETTLEMENT_CYCLE_INTERVAL.get(query.billing_cycle)
    if interval_spec is None:
        _error(
            "unsupported_cadence",
            "The prepaid settlement billing cadence is unsupported.",
            billing_cycle=str(query.billing_cycle),
        )
    interval_unit, interval_count = interval_spec
    cadence = BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=interval_unit,
        rate_quantity=Decimal("1"),
        service_interval_unit=interval_unit,
        service_interval_count=interval_count,
        invoice_interval_unit=interval_unit,
        invoice_interval_count=interval_count,
        collection_timing=CollectionTiming.advance,
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name=query.timezone_name,
        end_of_month_rule=EndOfMonthRule.clamp_to_month_end,
        proration_policy=ProrationPolicy.none,
    )
    zone = cadence.zone()
    local_effective_at = _utc(query.effective_at).astimezone(zone)
    local_start = datetime.combine(local_effective_at.date(), time.min, tzinfo=zone)
    interval = service_period(
        cadence=cadence,
        contract_start=local_start.astimezone(UTC),
    )
    return PrepaidSettlementPeriod(
        starts_at=interval.starts_at.astimezone(UTC),
        ends_at=interval.ends_at.astimezone(UTC),
        starts_on=interval.starts_at.astimezone(zone).date(),
        ends_on=interval.ends_at.astimezone(zone).date(),
        timezone_name=query.timezone_name,
    )


def _contiguous_prepaid_coverage_end(
    db: Session,
    *,
    subscription_id: UUID,
    account_id: UUID,
    as_of: datetime,
    exclude_source_invoice_id: UUID | None,
) -> datetime | None:
    """Resolve uninterrupted exact coverage containing ``as_of``."""

    observed_at = _utc(as_of)
    entitlement_query = select(
        ServiceEntitlement.id,
        ServiceEntitlement.starts_at,
        ServiceEntitlement.ends_at,
    ).where(
        ServiceEntitlement.subscription_id == subscription_id,
        ServiceEntitlement.account_id == account_id,
        ServiceEntitlement.status == ServiceEntitlementStatus.active,
        ServiceEntitlement.ends_at > observed_at,
    )
    if exclude_source_invoice_id is not None:
        entitlement_query = entitlement_query.where(
            (ServiceEntitlement.source_invoice_id.is_(None))
            | (ServiceEntitlement.source_invoice_id != exclude_source_invoice_id)
        )
    intervals: list[tuple[datetime, datetime, UUID]] = [
        (_utc(row.starts_at), _utc(row.ends_at), row.id)
        for row in db.execute(entitlement_query).all()
    ]
    extension_rows = db.execute(
        select(
            ServiceExtensionEntry.id,
            ServiceExtensionEntry.grant_starts_at,
            ServiceExtensionEntry.grant_ends_at,
        )
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscription_id == subscription_id,
            ServiceExtensionEntry.subscriber_id == account_id,
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > observed_at,
        )
    ).all()
    for row in extension_rows:
        assert row.grant_starts_at is not None
        assert row.grant_ends_at is not None
        intervals.append((_utc(row.grant_starts_at), _utc(row.grant_ends_at), row.id))

    cursor = observed_at
    found = False
    for starts_at, ends_at, _source_id in sorted(
        intervals,
        key=lambda item: (item[0], item[1], item[2]),
    ):
        if ends_at <= cursor:
            continue
        if starts_at > cursor:
            break
        found = True
        cursor = max(cursor, ends_at)
    return cursor if found else None


def resolve_prepaid_subscription_settlement_period(
    db: Session,
    query: PrepaidSubscriptionSettlementPeriodQuery,
) -> PrepaidSubscriptionSettlementPeriod:
    """Start a paid period after exact live coverage, otherwise on payment day.

    This deliberately ignores ``Subscription.next_billing_at``. That mutable
    projection cannot prove service coverage and previously caused an inferred
    extension delta to be added twice. Only active entitlement intervals and
    applied, non-reversed extension grants can defer the new paid period.
    """

    covered_through = _contiguous_prepaid_coverage_end(
        db,
        subscription_id=query.subscription_id,
        account_id=query.account_id,
        as_of=query.effective_at,
        exclude_source_invoice_id=query.exclude_source_invoice_id,
    )
    if covered_through is None:
        period = resolve_prepaid_settlement_period(
            PrepaidSettlementPeriodQuery(
                effective_at=query.effective_at,
                billing_cycle=query.billing_cycle,
                timezone_name=query.timezone_name,
            )
        )
        return PrepaidSubscriptionSettlementPeriod(
            period=period,
            covered_through=None,
        )

    interval_spec = _SETTLEMENT_CYCLE_INTERVAL.get(query.billing_cycle)
    if interval_spec is None:
        _error(
            "unsupported_cadence",
            "The prepaid settlement billing cadence is unsupported.",
            billing_cycle=str(query.billing_cycle),
        )
    interval_unit, interval_count = interval_spec
    cadence = BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=interval_unit,
        rate_quantity=Decimal("1"),
        service_interval_unit=interval_unit,
        service_interval_count=interval_count,
        invoice_interval_unit=interval_unit,
        invoice_interval_count=interval_count,
        collection_timing=CollectionTiming.advance,
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name=query.timezone_name,
        end_of_month_rule=EndOfMonthRule.clamp_to_month_end,
        proration_policy=ProrationPolicy.none,
    )
    interval = service_period(
        cadence=cadence,
        contract_start=covered_through,
    )
    zone = cadence.zone()
    starts_at = interval.starts_at.astimezone(UTC)
    ends_at = interval.ends_at.astimezone(UTC)
    return PrepaidSubscriptionSettlementPeriod(
        period=PrepaidSettlementPeriod(
            starts_at=starts_at,
            ends_at=ends_at,
            starts_on=starts_at.astimezone(zone).date(),
            ends_on=ends_at.astimezone(zone).date(),
            timezone_name=query.timezone_name,
        ),
        covered_through=covered_through,
    )


def _origin_ref(subscription_id: object, starts_at: datetime, ends_at: datetime) -> str:
    return f"{subscription_id}:{starts_at.isoformat()}:{ends_at.isoformat()}"


def _idempotency_key(origin_ref: str) -> str:
    return "prepaid-renewal-" + hashlib.sha256(origin_ref.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PrepaidMonthlyChargeDetail:
    """Exact invoice-ready components of one canonical prepaid charge."""

    subscription_id: UUID
    unit_price: Decimal
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    currency: str
    billing_cycle: BillingCycle
    tax_rate_id: UUID | None
    tax_application: TaxApplication


class LegacyRenewalTaxInvoiceCorrectionDisposition(enum.StrEnum):
    """Closed preview outcomes for one historical base-only renewal."""

    exact_base_only_legacy_renewal = "exact_base_only_legacy_renewal"
    already_corrected = "already_corrected"
    manual_review = "manual_review"


@dataclass(frozen=True, slots=True)
class LegacyRenewalTaxInvoiceCorrectionQuery:
    """Expected evidence and Finance-approved amounts for one correction."""

    account_id: UUID
    subscription_id: UUID
    adjustment_id: UUID
    entitlement_id: UUID
    expected_invoice_total: Decimal
    expected_remaining_credit: Decimal


@dataclass(frozen=True, slots=True)
class LegacyRenewalTaxInvoiceCorrectionPreview:
    """Fingerprint-bound proof of the exact proposed documentary correction."""

    account_id: UUID
    subscription_id: UUID
    adjustment_id: UUID
    entitlement_id: UUID
    disposition: LegacyRenewalTaxInvoiceCorrectionDisposition
    period_start: datetime | None
    period_end: datetime | None
    currency: str
    original_debit: Decimal
    subtotal: Decimal
    tax_total: Decimal
    tax_rate_id: UUID | None
    tax_application: TaxApplication
    invoice_total: Decimal
    credit_before: Decimal
    credit_after: Decimal
    reversal_preview_fingerprint: str | None
    reason: str
    fingerprint: str
    existing_invoice_id: UUID | None = None

    @property
    def actionable(self) -> bool:
        return (
            self.disposition
            is LegacyRenewalTaxInvoiceCorrectionDisposition.exact_base_only_legacy_renewal
        )


@dataclass(frozen=True, slots=True)
class CorrectLegacyRenewalTaxInvoiceCommand:
    """Execute one reviewed correction only while its preview remains exact."""

    context: CommandContext
    query: LegacyRenewalTaxInvoiceCorrectionQuery
    expected_preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class LegacyRenewalTaxInvoiceCorrectionResult:
    """Exact financial and entitlement evidence committed by the correction."""

    invoice_id: UUID
    invoice_number: str
    invoice_line_id: UUID
    replacement_entitlement_id: UUID
    replaced_entitlement_id: UUID
    adjustment_id: UUID
    reversal_ledger_entry_id: UUID
    payment_allocation_ids: tuple[UUID, ...]
    invoice_total: Decimal
    tax_total: Decimal
    remaining_credit: Decimal
    preview_fingerprint: str
    replayed: bool


def resolve_prepaid_monthly_charge_detail(
    db: Session,
    subscription: Subscription,
    effective_at: datetime,
) -> PrepaidMonthlyChargeDetail | None:
    """Resolve one canonical prepaid charge without losing tax provenance."""

    return _resolve_prepaid_monthly_charge_details(
        db,
        [subscription],
        effective_at,
    )[subscription.id]


def resolve_prepaid_monthly_charge(
    db: Session,
    subscription: Subscription,
    effective_at: datetime,
) -> tuple[Decimal, str, BillingCycle] | None:
    """Resolve one canonical taxed monthly renewal amount."""

    detail = resolve_prepaid_monthly_charge_detail(db, subscription, effective_at)
    if detail is None:
        return None
    return detail.total, detail.currency, detail.billing_cycle


def _newest_price(rows: Sequence[OfferPrice | OfferVersionPrice]):
    return max(rows, key=lambda row: (row.created_at, str(row.id))) if rows else None


def _resolve_prepaid_monthly_charge_details(
    db: Session,
    subscriptions: Sequence[Subscription],
    effective_at: datetime,
) -> dict[UUID, PrepaidMonthlyChargeDetail | None]:
    """Resolve exact contracted renewal charges with bounded query cost.

    Both renewal and enforcement consume this owner. Contract amount lives on
    ``Subscription.unit_price``; catalog rows provide currency/cadence metadata
    only. Tax precedence exactly matches recurring invoice billing: service
    customer exemption, service address, account, then offer/default.
    """
    from app.services.billing._common import _calculate_tax_amount
    from app.services.billing_automation import _effective_unit_price

    rows = list(subscriptions)
    result: dict[UUID, PrepaidMonthlyChargeDetail | None] = {
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

    tax_resolutions = resolve_subscription_taxes(db, eligible)

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
        tax_resolution = tax_resolutions[subscription.id]
        tax_rate_percent = tax_resolution.tax_rate_percent
        tax_application = tax_resolution.tax_application
        if (
            tax_resolution.tax_rate_id is None
            or tax_rate_percent is None
            or tax_application == TaxApplication.exempt
        ):
            effective_tax_application = TaxApplication.exempt
            tax_amount = Decimal("0.00")
            total = base
        else:
            effective_tax_application = tax_application
            tax_amount = _calculate_tax_amount(
                base,
                tax_rate_percent,
                tax_application,
            )
            total = (
                base
                if tax_application == TaxApplication.inclusive
                else round_money(base + tax_amount)
            )
        subtotal = (
            round_money(base - tax_amount)
            if effective_tax_application == TaxApplication.inclusive
            else round_money(base)
        )
        result[subscription.id] = PrepaidMonthlyChargeDetail(
            subscription_id=subscription.id,
            unit_price=round_money(base),
            subtotal=subtotal,
            tax_total=round_money(tax_amount),
            total=round_money(total),
            currency=(price.currency or "NGN").upper(),
            billing_cycle=cycle,
            tax_rate_id=tax_resolution.tax_rate_id,
            tax_application=effective_tax_application,
        )
    return result


def resolve_prepaid_monthly_charges(
    db: Session,
    subscriptions: Sequence[Subscription],
    effective_at: datetime,
) -> dict[UUID, tuple[Decimal, str, BillingCycle] | None]:
    """Resolve canonical taxed monthly renewal amounts in bounded queries."""

    details = _resolve_prepaid_monthly_charge_details(
        db,
        subscriptions,
        effective_at,
    )
    return {
        subscription_id: (
            (detail.total, detail.currency, detail.billing_cycle)
            if detail is not None
            else None
        )
        for subscription_id, detail in details.items()
    }


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
    invoice: Invoice | None
    invoice_line: InvoiceLine | None
    payment_allocation_ids: tuple[UUID, ...]
    entitlement: ServiceEntitlement
    adjustment: AccountAdjustment | None
    ledger_entry: LedgerEntry | None
    replayed: bool
    # The reviewed-opening-funding lane's own consumption evidence id
    # (2026-09, round 7). Previously discarded at the call site, which left
    # the receipt able to declare an `opening_funding` disposition with an
    # EMPTY evidence list -- the exact gap this closes. `None` for the
    # payment-only lane and for the pre-invoice-backed-renewal-cutover
    # adjustment replay path (neither ever consumes opening funding).
    opening_funding_consumption_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ExecuteReviewedPrepaidServiceRenewalCommand:
    """Fingerprint-bound execution of one explicitly reviewed missed cycle."""

    context: CommandContext
    subscription_id: UUID
    starts_at: datetime
    ends_at: datetime
    amount: Decimal
    currency: str
    expected_preview_fingerprint: str
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class ReviewedPrepaidServiceRenewalResult:
    """Canonical financial and service effects of the reviewed execution."""

    renewal: PrepaidServiceRenewalResult
    outcome: PrepaidServiceRenewedOutcome | None
    restored_service_count: int


class PrepaidServiceRenewalSource(enum.StrEnum):
    direct_payment = "direct_payment"
    account_credit = "account_credit"
    scheduled = "scheduled"
    reviewed_repair = "reviewed_repair"


@dataclass(frozen=True)
class PrepaidServiceRenewedOutcome:
    """Exact customer-visible result of one forward prepaid renewal."""

    event_id: UUID
    account_id: UUID
    subscription_id: UUID
    entitlement_id: UUID
    invoice_id: UUID | None
    ledger_entry_id: UUID | None
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
    renewal_review_required = "renewal_review_required"
    funded = "funded"
    unfunded = "unfunded"
    already_covered = "already_covered"
    missing_price = "missing_price"
    currency_mismatch = "currency_mismatch"
    non_cash_granted = "non_cash_granted"
    treatment_blocked = "treatment_blocked"


#: A disposition that claims a real settlement happened. A receipt written
#: with one of these MUST carry at least one subscription decision -- the
#: exact defect this whole round targets (2026-09, round 7/8). Mirrored in
#: `scripts/billing/census_prepaid_funding_consequence_gaps.py`'s
#: `_SUCCESSFUL_TRIGGER_DISPOSITIONS` (a separate, string-keyed constant
#: there since that script reads the persisted `disposition` column rather
#: than this enum) -- keep both in sync if this set ever changes.
SUCCESSFUL_FUNDING_CHANGE_RENEWAL_DISPOSITIONS = frozenset(
    {
        FundingChangeRenewalDisposition.draft_invoice_settled,
        FundingChangeRenewalDisposition.funded,
    }
)


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
    draft_invoices_voided: int = 0
    draft_invoices_pending: int = 0
    draft_review_exceptions: int = 0
    renewal_review_exceptions: int = 0
    restored_service_count: int = 0
    subscription_decisions: tuple[PrepaidFundingSubscriptionDecision, ...] = ()


@dataclass(frozen=True, slots=True)
class PrepaidFundingSubscriptionDecision:
    """One subscription's outcome within one funding-consequence execution.

    The in-memory shape ``evaluate_prepaid_service_after_settlement`` persists
    as one ``PrepaidFundingTriggerSubscriptionOutcome`` child row per
    receipt. Kept independent of the ORM row so callers that only need the
    in-transaction result (not a durable read) never touch the model layer.
    """

    subscription_id: UUID
    period_start: datetime
    period_end: datetime
    disposition: str
    funding_source: str | None
    invoice_id: UUID | None
    invoice_line_id: UUID | None
    entitlement_id: UUID | None
    funding_evidence_ids: list[str]
    amount: Decimal
    currency: str
    evidence_fingerprint: str


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


def _record_review_item_out_of_band(
    *,
    account_id: UUID,
    invoice_id: UUID | None,
    currency: str,
    required_amount: Decimal,
    payment_backed_amount: Decimal,
    opening_funding_amount: Decimal,
    preview_fingerprint: str,
    reason: str,
    invoice_number: str | None = None,
    subscription_id: UUID | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    detail: str | None = None,
) -> None:
    """Write the review item on a genuinely independent session/connection.

    Every call site that writes this review item does so IMMEDIATELY before
    raising -- and the caller's own transaction (an owner-command root, or a
    nightly-batch savepoint) rolls back on that raise. Writing through the
    caller's own ``db`` would roll the review item back right along with it,
    destroying the one piece of evidence the failure needs most (this was a
    confirmed round-2 defect: the mismatched-replay test only appeared to
    pass because it bypassed ``execute_owner_command`` entirely).

    This opens a brand-new session on a brand-new connection
    (``db_session_adapter.create_session()`` -- the same primitive
    ``advisory_lock`` uses to guarantee a in-process-independent backend) so
    the write commits 100% independently of whatever happens to the
    transaction that is about to unwind.

    A failure to write the review item is logged AND RE-RAISED (round-3
    correction, per Michael's decision): a permanent conflict/ambiguity that
    cannot be durably recorded is not safe to silently skip past. The
    caller's own specific isolatable-error type (e.g.
    :class:`PrepaidRenewalAmbiguousEvidenceError`) is never even constructed
    in that case -- the write failure itself propagates instead, so nightly
    isolation's closed `isinstance` allowlist does NOT match it and the
    whole pass aborts, exactly as an unclassified failure should.
    """

    from app.services.db_session_adapter import db_session_adapter
    from app.services.prepaid_draft_reconciliation import (
        record_prepaid_draft_reconciliation_exception,
    )

    out_of_band_db = db_session_adapter.create_session()
    try:
        record_prepaid_draft_reconciliation_exception(
            out_of_band_db,
            account_id=account_id,
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            currency=currency,
            required_amount=required_amount,
            payment_backed_amount=payment_backed_amount,
            opening_funding_amount=opening_funding_amount,
            preview_fingerprint=preview_fingerprint,
            reason=reason,
            subscription_id=subscription_id,
            period_start=period_start,
            period_end=period_end,
            detail=detail,
        )
        out_of_band_db.commit()
    except Exception:
        out_of_band_db.rollback()
        logger.exception(
            "prepaid_funding_review_item_out_of_band_write_failed",
            extra={
                "event": "prepaid_funding_review_item_out_of_band_write_failed",
                "account_id": str(account_id),
                "subscription_id": str(subscription_id) if subscription_id else None,
                "reason": reason,
            },
        )
        raise
    finally:
        out_of_band_db.close()


def _prepaid_funding_request_fingerprint(
    *,
    event_id: UUID | None,
    account_id: UUID,
    currency: str,
    effective_at: datetime,
    payment_id: UUID,
) -> str:
    """Stable hash of the inputs that determine what one execution was asked to do."""

    raw = (
        f"{event_id}:{account_id}:{currency.upper()}:"
        f"{_utc(effective_at).isoformat()}:{payment_id}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prepaid_funding_subscription_decision_evidence_fingerprint(
    *,
    subscription_id: UUID,
    period_start: datetime,
    period_end: datetime,
    disposition: str,
    funding_source: str | None,
    invoice_id: UUID | None,
    invoice_line_id: UUID | None,
    entitlement_id: UUID | None,
    funding_evidence_ids: list[str],
    amount: Decimal,
    currency: str,
) -> str:
    """Stable hash of one subscription's full settlement/allocation shape.

    Covers settlement/allocation shape and every resulting invoice/line/
    entitlement/funding-evidence identity (2026-09, round 7) -- not just
    disposition/amount/currency, so two outcomes reaching the same
    disposition through different evidence (e.g. a different invoice, a
    different set of payment allocations, or opening funding vs. none)
    hash differently instead of colliding.
    """

    raw = (
        f"{subscription_id}:{period_start.isoformat()}:{period_end.isoformat()}:"
        f"{disposition}:{funding_source or ''}:{invoice_id or ''}:"
        f"{invoice_line_id or ''}:{entitlement_id or ''}:"
        f"{','.join(sorted(funding_evidence_ids))}:{amount}:{currency.upper()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prepaid_funding_outcome_fingerprint(
    renewal: FundingChangeRenewalResult,
) -> str:
    """Stable hash of what actually happened, for the receipt's audit trail."""

    raw = "|".join(
        sorted(
            f"{decision.subscription_id}:{decision.period_start.isoformat()}:"
            f"{decision.period_end.isoformat()}:{decision.disposition}:"
            f"{decision.funding_source or ''}:{decision.invoice_id or ''}:"
            f"{decision.invoice_line_id or ''}:{decision.entitlement_id or ''}:"
            f"{','.join(sorted(decision.funding_evidence_ids))}:"
            f"{decision.amount}:{decision.currency}"
            for decision in renewal.subscription_decisions
        )
    )
    raw = f"{renewal.disposition.value}|{renewal.funded}|{renewal.renewal_review_exceptions}|{raw}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_prepaid_funding_trigger_execution(
    db: Session,
    *,
    event_store_id: UUID,
    event_id: UUID,
    event_type: str,
    payment: Payment,
    account_id: UUID,
    effective_at: datetime,
    request_fingerprint: str,
    renewal: FundingChangeRenewalResult,
) -> None:
    """Write the receipt + child decision rows, once, at commit time.

    Called exactly once per real (non-replayed) execution, in the SAME
    transaction as every consequence it describes -- never a pre-work
    "started" placeholder.
    """
    from app.models.prepaid_funding import (
        PrepaidFundingTriggerExecution,
        PrepaidFundingTriggerSubscriptionOutcome,
    )

    # The inverse of the per-decision `funding_evidence_missing` invariant
    # above, and the actual defect class this whole round targets (2026-09,
    # round 8): a receipt claiming a real settlement (`draft_invoice_settled`/
    # `funded`) must never commit with zero children. This is the write-site
    # gate `find_successful_receipts_missing_child_evidence` exists to make
    # unnecessary in the first place -- the census check is a detective
    # control for anything that slips past THIS gate, not a substitute for it.
    if (
        renewal.disposition in SUCCESSFUL_FUNDING_CHANGE_RENEWAL_DISPOSITIONS
        and not renewal.subscription_decisions
    ):
        _error(
            "successful_receipt_missing_child_evidence",
            "A receipt claiming a real settlement cannot commit with zero "
            "subscription-level child outcomes.",
            account_id=str(account_id),
            disposition=renewal.disposition.value,
        )

    receipt = PrepaidFundingTriggerExecution(
        event_store_id=event_store_id,
        event_id=event_id,
        event_type=event_type,
        payment_id=payment.id,
        settlement_reference=str(payment.id),
        account_id=account_id,
        currency=(payment.currency or "NGN").upper(),
        effective_at=_utc(effective_at),
        request_fingerprint=request_fingerprint,
        outcome_fingerprint=_prepaid_funding_outcome_fingerprint(renewal),
        disposition=renewal.disposition.value,
    )
    db.add(receipt)
    db.flush()
    for decision in renewal.subscription_decisions:
        db.add(
            PrepaidFundingTriggerSubscriptionOutcome(
                trigger_execution_id=receipt.id,
                subscription_id=decision.subscription_id,
                period_start=decision.period_start,
                period_end=decision.period_end,
                disposition=decision.disposition,
                funding_source=decision.funding_source,
                invoice_id=decision.invoice_id,
                invoice_line_id=decision.invoice_line_id,
                entitlement_id=decision.entitlement_id,
                funding_evidence_ids=decision.funding_evidence_ids,
                amount=decision.amount,
                currency=decision.currency,
                evidence_fingerprint=decision.evidence_fingerprint,
            )
        )
    db.flush()


def _replay_prepaid_funding_trigger_execution(
    receipt: object,
    *,
    payment_id: UUID,
) -> FundingChangeEvaluation:
    """Reconstruct the durable-handler result from an existing exact-match receipt.

    No work is redone -- the receipt's own recorded disposition IS the
    answer. Aggregate counters beyond ``disposition``/``scanned`` are not
    reconstructed field-by-field from the child rows here; a caller that
    needs the exact original per-subscription breakdown can read
    ``PrepaidFundingTriggerSubscriptionOutcome`` rows directly by
    ``trigger_execution_id``.
    """
    from app.models.prepaid_funding import PrepaidFundingTriggerExecution

    assert isinstance(receipt, PrepaidFundingTriggerExecution)
    renewal = FundingChangeRenewalResult(
        account_id=receipt.account_id,
        scanned=len(receipt.subscription_outcomes),
        funded=sum(
            1
            for outcome in receipt.subscription_outcomes
            if outcome.disposition == "created_canonical_renewal"
        ),
        unfunded=0,
        already_covered=0,
        missing_price=0,
        currency_mismatch=0,
        disposition=FundingChangeRenewalDisposition(receipt.disposition)
        if receipt.disposition in FundingChangeRenewalDisposition._value2member_map_
        else FundingChangeRenewalDisposition.funded,
    )
    return FundingChangeEvaluation(
        payment_id=payment_id,
        disposition=FundingChangeEvaluationDisposition.evaluated,
        renewal=renewal,
    )


def evaluate_prepaid_service_after_settlement(
    db: Session,
    *,
    account_id: UUID,
    payment_id: UUID,
    evidence_ref: str,
    event_id: UUID | None = None,
    only_subscription_id: UUID | None = None,
    skip_receipt_for_repair: bool = False,
) -> FundingChangeEvaluation:
    """Validate settlement evidence and request its prepaid consequence.

    The event adapter must not silently accept incomplete money evidence. A
    failure raised here leaves the durable event handler attempt retryable. A
    consolidated payment is a terminal non-prepaid outcome because its money
    belongs to invoice allocations rather than one customer funding position.

    ``event_id`` (the domain event's own id) is how this owner answers "have
    we already processed this EXACT durable event" via the
    ``PrepaidFundingTriggerExecution`` receipt, unique on ``event_store_id``
    -- a DIFFERENT question from the period-level protection
    (``billing_line_key``/``_invoice_backed_renewal_evidence``), which
    answers "is this exact subscription+period already funded, regardless
    of which trigger did it." Both continue to exist.
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

    # Receipt replay check: "have we already processed this EXACT durable
    # event." A receipt-eligible call must actually be able to write a
    # receipt (2026-09, round 7): a fail-open here would leave a real
    # settlement consequence permanently un-receipted with no trace of why.
    if event_id is None and not skip_receipt_for_repair:
        _error(
            "receipt_required",
            "This call has no event identity and did not explicitly opt "
            "out of receipting. Supply `event_id` (the durable dispatch "
            "path always does), or pass `skip_receipt_for_repair=True` if "
            "this is genuinely a repair re-evaluation with no original "
            "event to receipt against.",
            account_id=str(account_id),
            payment_id=str(payment_id),
        )
    event_store_id: UUID | None = None
    request_fingerprint: str | None = None
    if event_id is not None:
        from app.models.event_store import EventStore
        from app.models.prepaid_funding import PrepaidFundingTriggerExecution

        event_store_row = db.scalar(
            select(EventStore).where(EventStore.event_id == event_id)
        )
        if event_store_row is None:
            # Should never happen on the real dispatch path (`dispatch()`
            # persists the `EventStore` row before any handler runs, in the
            # same transaction) -- if it ever does, a receipt-eligible call
            # with no way to write a receipt is not safe to let proceed
            # unreceipted, so this is now a hard failure (2026-09, round 7)
            # rather than a warn-and-continue.
            _error(
                "event_store_row_missing",
                "Funding-change event has an event_id with no matching "
                "EventStore row; refusing to proceed unreceipted.",
                event_id=str(event_id),
                account_id=str(account_id),
            )
        if event_store_row is not None:
            event_store_id = event_store_row.id
            request_fingerprint = _prepaid_funding_request_fingerprint(
                event_id=event_id,
                account_id=account_id,
                currency=payment.currency,
                effective_at=effective_at,
                payment_id=payment.id,
            )
            existing_receipt = db.scalar(
                select(PrepaidFundingTriggerExecution).where(
                    PrepaidFundingTriggerExecution.event_store_id == event_store_id
                )
            )
            if existing_receipt is not None:
                if existing_receipt.request_fingerprint == request_fingerprint:
                    # Exact replay: return the stored result, redo nothing.
                    return _replay_prepaid_funding_trigger_execution(
                        existing_receipt, payment_id=payment.id
                    )
                # Same durable event, different computed inputs than last
                # time -- a genuine data/logic inconsistency, not a routine
                # case. Permanent, non-retryable, and surfaced as a durable
                # review item rather than silently reprocessed. Written
                # OUT OF BAND: `db` belongs to `execute_owner_command`'s
                # transaction, which rolls back the instant the exception
                # below propagates -- writing through `db` would destroy
                # this evidence at the exact moment it's needed most.
                _record_review_item_out_of_band(
                    account_id=account_id,
                    invoice_id=None,
                    currency=(payment.currency or "NGN").upper(),
                    required_amount=Decimal("0.01"),
                    payment_backed_amount=Decimal("0.00"),
                    opening_funding_amount=Decimal("0.00"),
                    preview_fingerprint=hashlib.sha256(
                        f"{existing_receipt.request_fingerprint}:"
                        f"{request_fingerprint}".encode()
                    ).hexdigest(),
                    reason="trigger_execution_fingerprint_mismatch",
                    detail=(
                        f"event_store_id={event_store_id} previously recorded "
                        f"fingerprint {existing_receipt.request_fingerprint}, "
                        f"now computed {request_fingerprint}"
                    ),
                )
                raise PrepaidTriggerExecutionConflictError(
                    "This durable event was already processed with "
                    "different evidence than it presents now.",
                    event_store_id=str(event_store_id),
                    event_id=str(event_id),
                )

    # Project the anchor from the entitlement evidence this payment already
    # committed, before deciding whether any further period is due. Doing it
    # here rather than inside the renewal branch keeps the anchor exact even
    # when another payable invoice defers the funded renewal path.
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
        only_subscription_id=only_subscription_id,
    )
    if event_store_id is not None and event_id is not None and request_fingerprint:
        _record_prepaid_funding_trigger_execution(
            db,
            event_store_id=event_store_id,
            event_id=event_id,
            event_type=event_store_row.event_type if event_store_row else "",
            payment=payment,
            account_id=account_id,
            effective_at=effective_at,
            request_fingerprint=request_fingerprint,
            renewal=renewal,
        )
    return FundingChangeEvaluation(
        payment_id=payment.id,
        disposition=FundingChangeEvaluationDisposition.evaluated,
        renewal=renewal,
    )


def execute_prepaid_service_after_settlement(
    db: Session,
    command: EvaluatePrepaidServiceAfterSettlementCommand,
) -> FundingChangeEvaluation:
    """Execute one settlement-triggered consequence under this owner's root.

    Durable event handlers call this public boundary from a fresh session.
    Settlement validation, draft reconciliation, paid invoice, entitlement,
    anchor, posting group, and outcome therefore commit or roll back together.
    """

    return execute_owner_command(
        db,
        definition=_EVALUATE_SETTLEMENT_COMMAND,
        context=command.context,
        operation=lambda: evaluate_prepaid_service_after_settlement(
            db,
            account_id=command.account_id,
            payment_id=command.payment_id,
            evidence_ref=command.evidence_ref,
            event_id=command.event_id,
            only_subscription_id=command.only_subscription_id,
            skip_receipt_for_repair=command.skip_receipt_for_repair,
        ),
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


def _renewal_billing_line_key(origin_ref: str) -> str:
    return "prepaid-renewal:" + hashlib.sha256(origin_ref.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _InvoiceBackedRenewalEvidence:
    invoice: Invoice
    line: InvoiceLine
    entitlement: ServiceEntitlement
    payment_allocation_ids: tuple[UUID, ...]
    preview_fingerprint: str
    funding_before: Decimal
    funding_after: Decimal


def _invoice_backed_renewal_evidence(
    db: Session,
    *,
    subscription: Subscription,
    starts_at: datetime,
    ends_at: datetime,
    amount: Decimal,
    currency: str,
    origin_ref: str,
) -> _InvoiceBackedRenewalEvidence | None:
    """Return one exact paid renewal document, or fail closed on drift."""

    line = db.scalar(
        select(InvoiceLine).where(
            InvoiceLine.billing_line_key == _renewal_billing_line_key(origin_ref),
            InvoiceLine.is_active.is_(True),
        )
    )
    if line is None:
        return None
    invoice = db.get(Invoice, line.invoice_id)
    entitlement = db.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.source_invoice_line_id == line.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
        )
    )
    metadata = line.metadata_ if isinstance(line.metadata_, dict) else {}
    fingerprint = str(metadata.get("renewal_preview_fingerprint") or "")
    try:
        funding_before = round_money(Decimal(str(metadata["renewal_funding_before"])))
        funding_after = round_money(Decimal(str(metadata["renewal_funding_after"])))
    except (KeyError, ArithmeticError, ValueError):
        funding_before = Decimal("0.00")
        funding_after = Decimal("0.00")
    allocations = tuple(
        db.scalars(
            select(PaymentAllocation)
            .where(
                PaymentAllocation.invoice_id == line.invoice_id,
                PaymentAllocation.is_active.is_(True),
            )
            .order_by(PaymentAllocation.id)
        ).all()
    )
    opening_consumption = db.scalar(
        select(PrepaidOpeningFundingConsumption).where(
            PrepaidOpeningFundingConsumption.invoice_id == line.invoice_id
        )
    )
    applied_amount = round_money(
        sum(
            (Decimal(str(allocation.amount)) for allocation in allocations),
            Decimal("0.00"),
        )
        + (
            Decimal(str(opening_consumption.amount))
            if opening_consumption is not None
            else Decimal("0.00")
        )
    )
    active_line_count = db.scalar(
        select(func.count(InvoiceLine.id)).where(
            InvoiceLine.invoice_id == line.invoice_id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
        )
    )
    if (
        invoice is None
        or not invoice.is_active
        or invoice.status is not InvoiceStatus.paid
        or round_money(invoice.balance_due) != Decimal("0.00")
        or invoice.issued_at is None
        or invoice.due_at is None
        or invoice.paid_at is None
        or invoice.due_date_basis is not InvoiceDueDateBasis.prepaid_service_period
        or invoice.account_id != subscription.subscriber_id
        or (invoice.currency or "NGN").upper() != currency
        or _utc(invoice.billing_period_start or starts_at) != starts_at
        or _utc(invoice.billing_period_end or ends_at) != ends_at
        or round_money(invoice.total) != amount
        or round_money(invoice.subtotal) + round_money(invoice.tax_total) != amount
        or active_line_count != 1
        or line.subscription_id != subscription.id
        or round_money(line.quantity) != Decimal("1.00")
        or round_money(line.unit_price) != round_money(line.amount)
        or round_money(line.amount)
        != (
            amount
            if line.tax_application is TaxApplication.inclusive
            else round_money(invoice.subtotal)
        )
        or metadata.get("kind") != "base_subscription"
        or metadata.get("billing_period_start") != starts_at.isoformat()
        or metadata.get("billing_period_end") != ends_at.isoformat()
        or metadata.get("renewal_idempotency_key") != _idempotency_key(origin_ref)
        or entitlement is None
        or entitlement.account_id != subscription.subscriber_id
        or entitlement.subscription_id != subscription.id
        or _utc(entitlement.starts_at) != starts_at
        or _utc(entitlement.ends_at) != ends_at
        or round_money(entitlement.amount_funded) != round_money(line.amount)
        or entitlement.currency.upper() != currency
        or len(fingerprint) != 64
        or round_money(funding_before - amount) != funding_after
        or applied_amount != amount
    ):
        _error(
            "idempotency_conflict",
            "Prepaid renewal invoice evidence does not match the funded period.",
            invoice_id=str(invoice.id) if invoice is not None else None,
            invoice_line_id=str(line.id),
        )
    return _InvoiceBackedRenewalEvidence(
        invoice=invoice,
        line=line,
        entitlement=entitlement,
        payment_allocation_ids=tuple(allocation.id for allocation in allocations),
        preview_fingerprint=fingerprint,
        funding_before=funding_before,
        funding_after=funding_after,
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
    invoice_evidence = _invoice_backed_renewal_evidence(
        db,
        subscription=subscription,
        starts_at=period_start,
        ends_at=period_end,
        amount=charge,
        currency=unit,
        origin_ref=origin_ref,
    )
    if invoice_evidence is not None:
        return PrepaidServiceRenewalPreview(
            account_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            starts_at=period_start,
            ends_at=period_end,
            amount=charge,
            currency=unit,
            funding_before=invoice_evidence.funding_before,
            funding_after=invoice_evidence.funding_after,
            shortfall=Decimal("0.00"),
            allowed=True,
            fingerprint=invoice_evidence.preview_fingerprint,
            idempotency_key=idempotency_key,
            origin_ref=origin_ref,
            replayed=True,
        )
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
    effective_at: datetime,
    evidence_ref: str,
) -> PrepaidServiceRenewalResult:
    """Lock, re-preview, and atomically settle invoice + entitlement + anchor."""
    evidence = evidence_ref.strip()
    if not evidence:
        _error("missing_evidence_ref", "An evidence reference is required.")
    decision_at = _utc(effective_at)

    # Serialize the idempotency lookup with the funding re-preview and write.
    # Looking up the adjustment before this lock let two concurrent callers
    # both observe "missing"; the second caller then re-previewed after the
    # first committed and failed with a stale fingerprint instead of returning
    # the already-recorded renewal.
    lock_account(db, str(preview.account_id))
    subscription = _subscription_for_request(db, preview.subscription_id)
    invoice_evidence = _invoice_backed_renewal_evidence(
        db,
        subscription=subscription,
        starts_at=preview.starts_at,
        ends_at=preview.ends_at,
        amount=preview.amount,
        currency=preview.currency,
        origin_ref=preview.origin_ref,
    )
    if invoice_evidence is not None:
        if invoice_evidence.preview_fingerprint != preview.fingerprint:
            _error(
                "idempotency_conflict",
                "Prepaid renewal idempotency evidence does not match the request.",
            )
        return PrepaidServiceRenewalResult(
            preview=preview,
            invoice=invoice_evidence.invoice,
            invoice_line=invoice_evidence.line,
            payment_allocation_ids=invoice_evidence.payment_allocation_ids,
            entitlement=invoice_evidence.entitlement,
            adjustment=None,
            ledger_entry=None,
            replayed=True,
            # Re-derived, not just discarded on replay (2026-09, round 7):
            # without this, the SAME renewal would report real opening-funding
            # evidence on its first execution and none on a replay, which
            # would make the receipt's evidence fingerprint disagree with
            # itself across an exact replay of the same durable event.
            opening_funding_consumption_id=db.scalar(
                select(PrepaidOpeningFundingConsumption.id)
                .where(
                    PrepaidOpeningFundingConsumption.invoice_id
                    == invoice_evidence.invoice.id
                )
                .limit(1)
            ),
        )

    # Preserve replay compatibility for periods funded before invoice-backed
    # renewal cutover. New renewals never write this adjustment form.
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
        _stage_prepaid_consumption_posting(
            db,
            renewal=preview,
            adjustment=existing_adjustment,
            entitlement=entitlement,
            require_existing=True,
        )
        return PrepaidServiceRenewalResult(
            preview=preview,
            invoice=None,
            invoice_line=None,
            payment_allocation_ids=(),
            entitlement=entitlement,
            adjustment=existing_adjustment,
            ledger_entry=existing_adjustment.ledger_entry,
            replayed=True,
        )

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

    charge = resolve_prepaid_monthly_charge_detail(db, subscription, decision_at)
    if (
        charge is None
        or charge.total != current.amount
        or charge.currency != current.currency
    ):
        _error(
            "stale_preview",
            "Prepaid renewal charge details changed after preview.",
        )

    # Classify FIRST, with ZERO writes. This is the single-owner
    # funding-consequence design (2026-09, round 2): the ORIGINAL production
    # defect was a re-entrant call into `stage_prepaid_draft_after_funding_change`
    # for a document this owner had already created — round 1 removed that
    # re-entrant call but still created the Invoice/InvoiceLine BEFORE
    # classifying, so an ambiguous outcome left an orphaned draft committed by
    # the caller. Nothing is written to the database above this point in this
    # branch; `classify_prospective_prepaid_funding` runs against an
    # ephemeral, never-persisted invoice shell.
    from app.services.prepaid_draft_reconciliation import (
        PrepaidDraftAction,
        PrepaidDraftDisposition,
        PrepaidDraftReconciliationError,
        classify_prospective_prepaid_funding,
    )

    try:
        classification = classify_prospective_prepaid_funding(
            db,
            account_id=current.account_id,
            currency=current.currency,
            amount=current.amount,
        )
    except PrepaidDraftReconciliationError as exc:
        # A baseline-less account (`opening_funding_unavailable`) or any
        # other pure-classification failure is exactly as ambiguous as a
        # disposition the classifier returns normally -- it must not abort
        # the whole funding event for every OTHER subscription on this
        # account. Zero mutations have happened above this point either way.
        _record_review_item_out_of_band(
            account_id=current.account_id,
            invoice_id=None,
            currency=current.currency,
            required_amount=current.amount,
            payment_backed_amount=Decimal("0.00"),
            opening_funding_amount=Decimal("0.00"),
            preview_fingerprint=current.fingerprint,
            reason=f"renewal_classification_failed_{exc.code.rsplit('.', 1)[-1]}",
            subscription_id=subscription.id,
            period_start=current.starts_at,
            period_end=current.ends_at,
            detail=exc.message,
        )
        raise PrepaidOpeningLaneUnavailableError(
            "Due prepaid renewal funding could not be classified.",
            subscription_id=str(subscription.id),
            reason=exc.message,
        ) from exc
    if classification.recommended_action is not PrepaidDraftAction.settle_paid:
        # Case (c): ambiguous or insufficient evidence, decided BEFORE any
        # mutation — there is no invoice yet, so the review item is keyed on
        # (account, subscription, period) instead. Written OUT OF BAND: this
        # raise unwinds either `execute_owner_command`'s whole transaction
        # (nightly cron, wrapped in its own savepoint) or is caught by the
        # funding-event loop without a savepoint -- writing through `db`
        # would lose the evidence in the first case and is inconsistent
        # with the second, so both call sites use the same mechanism.
        _record_review_item_out_of_band(
            account_id=current.account_id,
            invoice_id=None,
            currency=current.currency,
            required_amount=current.amount,
            payment_backed_amount=classification.funding.payment_backed_credit,
            opening_funding_amount=classification.opening.available_amount,
            preview_fingerprint=current.fingerprint,
            reason=f"renewal_{classification.disposition.value}",
            subscription_id=subscription.id,
            period_start=current.starts_at,
            period_end=current.ends_at,
            detail=classification.reason,
        )
        raise PrepaidRenewalAmbiguousEvidenceError(
            "Due prepaid renewal does not have clean, exact settlement evidence.",
            subscription_id=str(subscription.id),
            disposition=classification.disposition.value,
            reason=classification.reason,
        )

    # Decision made. Mutate now, and only now.
    local_start = current.starts_at.astimezone(APP_TIMEZONE).date()
    local_end = current.ends_at.astimezone(APP_TIMEZONE).date()
    line_key = _renewal_billing_line_key(current.origin_ref)
    try:
        invoice = Invoices.stage_system_invoice_for_owner(
            db,
            InvoiceCreate(
                account_id=current.account_id,
                status=InvoiceStatus.draft,
                currency=current.currency,
                subtotal=charge.subtotal,
                tax_total=charge.tax_total,
                total=charge.total,
                balance_due=charge.total,
                billing_period_start=current.starts_at,
                billing_period_end=current.ends_at,
                memo=(
                    "Funded prepaid service renewal "
                    f"{local_start.isoformat()} - {local_end.isoformat()}"
                ),
            ),
            reason="funded_prepaid_service_renewal",
        )
        invoice_line = InvoiceLines.stage_system_line_for_owner(
            db,
            SystemInvoiceLineCreate(
                invoice_id=invoice.id,
                subscription_id=subscription.id,
                description=(
                    f"{subscription.offer.name if subscription.offer else 'Service'} "
                    f"({local_start.isoformat()} - {local_end.isoformat()})"
                ),
                quantity=Decimal("1.000"),
                unit_price=charge.unit_price,
                amount=charge.unit_price,
                tax_rate_id=charge.tax_rate_id,
                tax_application=charge.tax_application,
                metadata_={
                    "kind": "base_subscription",
                    "billing_period_start": current.starts_at.isoformat(),
                    "billing_period_end": current.ends_at.isoformat(),
                    "renewal_preview_fingerprint": current.fingerprint,
                    "renewal_idempotency_key": current.idempotency_key,
                    "renewal_evidence_ref": evidence,
                    "renewal_funding_before": str(current.funding_before),
                    "renewal_funding_after": str(current.funding_after),
                    "renewal_account_id": str(current.account_id),
                    "renewal_subscription_id": str(subscription.id),
                    "renewal_amount": str(current.amount),
                    "renewal_currency": current.currency,
                },
                billing_line_key=line_key,
            ),
            reason="funded_prepaid_service_renewal",
        )
    except InvoiceOwnerError as exc:
        _error(
            "invoice_rejected",
            "Invoice owner rejected the funded prepaid renewal document.",
            participant_error=exc.code,
        )
    # Fix-by-construction for the double-computation defect (not a numeric
    # coincidence): this owner has already computed this invoice's exact
    # period AND totals. Marking both authoritative makes the shared
    # payment-finalization pipeline (`_reanchor_paid_prepaid_invoice_if_lapsed`
    # for the period; `_recalculate_invoice_totals` for
    # subtotal/tax_total/total) skip its own independent re-derivation of
    # either fact rather than risk disagreeing with what this owner already
    # computed from the exact same `charge` this invoice was built from.
    invoice.metadata_ = {
        **(invoice.metadata_ or {}),
        "renewal_period_authoritative": True,
        "renewal_totals_authoritative": True,
    }
    db.flush()

    opening_funding_consumption_id: UUID | None = None
    if classification.disposition is PrepaidDraftDisposition.exact_payment_fundable:
        _settle_exact_payment_fundable_renewal(
            db,
            invoice=invoice,
            decision_at=decision_at,
        )
    else:
        opening_funding_consumption_id = _settle_reviewed_opening_fundable_renewal(
            db,
            invoice=invoice,
            classification=classification,
            current=current,
            decision_at=decision_at,
            idempotency_key=current.idempotency_key,
        )

    project_prepaid_billing_anchor_for_invoice(
        db,
        invoice,
        evidence_ref=f"prepaid_service_renewals:{invoice.id}",
        authority=BillingAnchorAuthority.funding_observation,
    )

    # Post-mutation integrity check. Money has already moved by this point
    # (the invoice is settled), so a mismatch here is a genuine, permanent
    # integrity failure — NOT ambiguous evidence — and per the corrected
    # design must roll back everything this transaction did, exactly like
    # `main` did before round 1 (fail closed, `retryable=False`, propagates
    # rather than being caught as a review-item-and-continue case).
    evidence_result = _invoice_backed_renewal_evidence(
        db,
        subscription=subscription,
        starts_at=current.starts_at,
        ends_at=current.ends_at,
        amount=current.amount,
        currency=current.currency,
        origin_ref=current.origin_ref,
    )
    if evidence_result is None or evidence_result.line.id != invoice_line.id:
        raise PrepaidServiceRenewalError(
            code="financial.prepaid_service_renewals.incomplete_entitlement",
            message=(
                "Paid prepaid renewal invoice did not produce exact "
                "entitlement evidence."
            ),
            details={"invoice_id": str(invoice.id)},
            retryable=False,
        )
    return PrepaidServiceRenewalResult(
        preview=current,
        invoice=evidence_result.invoice,
        invoice_line=evidence_result.line,
        payment_allocation_ids=evidence_result.payment_allocation_ids,
        entitlement=evidence_result.entitlement,
        adjustment=None,
        ledger_entry=None,
        replayed=False,
        opening_funding_consumption_id=opening_funding_consumption_id,
    )


def _settle_exact_payment_fundable_renewal(
    db: Session,
    *,
    invoice: Invoice,
    decision_at: datetime,
) -> None:
    """Settle a canonical renewal invoice this owner just created, directly.

    A faithful, inlined port of ``_stage_action``'s ``exact_payment_fundable``
    branch (``app/services/prepaid_draft_reconciliation.py``) — verified
    correct in round 1's review — but this owner never re-enters that
    function's generic write path for a document it constructs itself.
    """
    from app.services.billing.account_credit import (
        AccountCreditApplicationError,
        AccountCreditApplications,
    )
    from app.services.prepaid_draft_reconciliation import (
        preview_payment_funding_for_owner,
    )

    decision_at = _utc(decision_at)
    try:
        Invoices.issue_draft_for_owner(
            db,
            str(invoice.id),
            issuance=InvoiceIssuanceInput(
                issued_at=decision_at,
                due_at=decision_at,
                due_date_basis=InvoiceDueDateBasis.prepaid_service_period,
                due_date_basis_ref=f"prepaid_service_renewals:{invoice.id}",
                due_date_policy_version="prepaid-service-renewals-v1",
                reason="funded_prepaid_service_renewal",
            ),
            apply_available_credit=False,
        )
        # Re-preview AFTER issuing, not before: issuing changes
        # `invoice.status` (draft -> issued), which the funding preview's own
        # fingerprint covers — previewing before issuing would bind a
        # fingerprint to a status the invoice no longer has by the time
        # `apply_invoice_fully` re-derives and checks it.
        funding_preview = preview_payment_funding_for_owner(
            db,
            invoice=invoice,
        )
        AccountCreditApplications.apply_invoice_fully(
            db,
            invoice,
            preview_fingerprint=funding_preview.fingerprint,
            funding_position_at=funding_preview.funding_position_at,
        )
    except (InvoiceOwnerError, AccountCreditApplicationError) as exc:
        # Money may already have partially moved inside this try block (e.g.
        # `issue_draft_for_owner` succeeded, `apply_invoice_fully` then
        # failed) -- this is a genuine post-mutation integrity failure, not
        # ambiguous evidence, so it rolls back the whole transaction.
        raise PrepaidServiceRenewalError(
            code="financial.prepaid_service_renewals.invoice_settlement_rejected",
            message=(
                "Verified funding did not produce one exactly paid renewal invoice."
            ),
            details={
                "invoice_id": str(invoice.id),
                "participant_error": getattr(exc, "code", type(exc).__name__),
            },
            retryable=False,
        ) from exc
    db.refresh(invoice)


def _settle_reviewed_opening_fundable_renewal(
    db: Session,
    *,
    invoice: Invoice,
    classification: ProspectiveFundingClassification,
    current: PrepaidServiceRenewalPreview,
    decision_at: datetime,
    idempotency_key: str,
) -> UUID | None:
    """Full 8-step reviewed-opening-funding lane, atomically, for one invoice.

    Returns the consumption row's id when opening funding was actually
    consumed (``opening_amount > 0``), so the caller can carry it as this
    renewal's funding evidence -- never silently discarded (2026-09, round 7).

    1. Lock — delegated to the already-locked account
       (:func:`confirm_prepaid_service_renewal` locks it at entry) plus
       ``_stage_opening_funding_consumption``'s own row lock on the
       baseline/opening-position it consumes.
    2. Validate — re-derive funding against the REAL now-created invoice
       (not the ephemeral classification shell) and require it still
       matches exactly; anything else is a permanent integrity failure
       (money has not moved yet at this point, but the decision to use this
       lane already has — treat drift here the same as any other
       post-classification mismatch: fail closed, not silently ambiguous).
    3. Consume the opening funding.
    4. Settle/finalize the invoice.
    5. Create the entitlement — inside step 4's finalize.
    6. Project the anchor — by the caller, immediately after this function
       returns (shared with the exact-payment-fundable branch).
    7. Record the trigger receipt/child outcome — by the caller
       (:func:`evaluate_prepaid_service_after_settlement`), after this
       function returns, in the same transaction.
    8. Emit the renewal event — by the caller
       (:func:`stage_prepaid_service_renewed_outcome`).

    Never calls ``stage_prepaid_draft_after_funding_change``/``_stage_action``.
    """
    from app.services.billing.account_credit import (
        AccountCreditApplicationError,
        AccountCreditApplications,
    )
    from app.services.billing.payments import finalize_invoice_application_for_owner
    from app.services.prepaid_draft_reconciliation import (
        preview_payment_funding_for_owner,
        preview_reviewed_opening_funding_for_owner,
        stage_reviewed_opening_funding_consumption_for_owner,
    )

    try:
        Invoices.issue_draft_for_owner(
            db,
            str(invoice.id),
            issuance=InvoiceIssuanceInput(
                issued_at=decision_at,
                due_at=decision_at,
                due_date_basis=InvoiceDueDateBasis.prepaid_service_period,
                due_date_basis_ref=f"prepaid_service_renewals:{invoice.id}",
                due_date_policy_version="prepaid-service-renewals-v1",
                reason="funded_prepaid_service_renewal",
            ),
            apply_available_credit=False,
        )
        funding = preview_payment_funding_for_owner(db, invoice=invoice)
        opening = preview_reviewed_opening_funding_for_owner(
            db, invoice=invoice, payment_funding=funding
        )
        if (
            opening.baseline_id is None
            and opening.opening_position_id is None
            or opening.available_amount < funding.shortfall
            or opening.authoritative_funding < funding.invoice_remaining
            or funding.unbacked_credit != Decimal("0.00")
        ):
            # Step 2 failed: the reviewed-opening evidence no longer lines up
            # exactly against the real invoice. No settlement has happened
            # yet (issuing a draft moves no money) -- fail closed as a
            # permanent integrity failure rather than silently downgrading
            # to ambiguous, since the classification decision itself is now
            # provably wrong, not merely unclear.
            raise PrepaidServiceRenewalError(
                code=(
                    "financial.prepaid_service_renewals"
                    ".reviewed_opening_lane_evidence_changed"
                ),
                message=(
                    "Reviewed opening funding no longer matches this exact "
                    "invoice; the funding-consequence owner will not guess."
                ),
                details={"invoice_id": str(invoice.id)},
                retryable=False,
            )
        result = AccountCreditApplications.apply_invoice_available(
            db,
            invoice,
            preview_fingerprint=funding.fingerprint,
            funding_position_at=funding.funding_position_at,
        )
        opening_amount = result.invoice_remaining
        consumption_id: UUID | None = None
        if opening_amount > Decimal("0.00"):
            consumption = stage_reviewed_opening_funding_consumption_for_owner(
                db,
                invoice=invoice,
                opening=opening,
                fingerprint=current.fingerprint,
                currency=current.currency,
                amount=opening_amount,
                effective_at=decision_at,
                idempotency_key=f"funding-consequence:{invoice.id}:{idempotency_key}",
            )
            consumption_id = consumption.id
        finalize_invoice_application_for_owner(db, invoice, effective_at=decision_at)
    except (InvoiceOwnerError, AccountCreditApplicationError) as exc:
        raise PrepaidServiceRenewalError(
            code="financial.prepaid_service_renewals.invoice_settlement_rejected",
            message=(
                "Reviewed opening funding did not produce one exactly paid "
                "renewal invoice."
            ),
            details={
                "invoice_id": str(invoice.id),
                "participant_error": getattr(exc, "code", type(exc).__name__),
            },
            retryable=False,
        ) from exc
    db.refresh(invoice)
    return consumption_id


def execute_reviewed_prepaid_service_renewal(
    db: Session,
    command: ExecuteReviewedPrepaidServiceRenewalCommand,
) -> ReviewedPrepaidServiceRenewalResult:
    """Execute one reviewed missed period through the canonical renewal owner."""

    return execute_owner_command(
        db,
        definition=_EXECUTE_REVIEWED_COMMAND,
        context=command.context,
        operation=lambda: _execute_reviewed_prepaid_service_renewal(db, command),
    )


def _execute_reviewed_prepaid_service_renewal(
    db: Session,
    command: ExecuteReviewedPrepaidServiceRenewalCommand,
) -> ReviewedPrepaidServiceRenewalResult:
    if not command.context.idempotency_key:
        _error(
            "missing_idempotency_key",
            "Reviewed prepaid renewal requires an idempotency key.",
        )
    expected = command.expected_preview_fingerprint.strip().lower()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        _error(
            "invalid_preview_fingerprint",
            "Reviewed prepaid renewal requires a SHA-256 preview fingerprint.",
        )
    preview = preview_prepaid_service_renewal(
        db,
        subscription_id=command.subscription_id,
        starts_at=command.starts_at,
        ends_at=command.ends_at,
        amount=command.amount,
        currency=command.currency,
    )
    if preview.fingerprint != expected:
        _error(
            "stale_preview",
            "Prepaid funding or reviewed renewal terms changed after preview.",
        )
    if not preview.allowed:
        _error(
            "insufficient_funding",
            "Insufficient prepaid funding for the reviewed service renewal.",
        )
    renewal = confirm_prepaid_service_renewal(
        db,
        preview,
        effective_at=command.starts_at,
        evidence_ref=command.evidence_ref,
    )
    outcome = None
    if not renewal.replayed:
        outcome = stage_prepaid_service_renewed_outcome(
            db,
            account_id=renewal.preview.account_id,
            subscription_id=renewal.preview.subscription_id,
            entitlement_id=renewal.entitlement.id,
            invoice_id=(renewal.invoice.id if renewal.invoice is not None else None),
            ledger_entry_id=(
                renewal.ledger_entry.id if renewal.ledger_entry is not None else None
            ),
            period_start=renewal.preview.starts_at,
            renewed_through=renewal.preview.ends_at,
            amount=renewal.preview.amount,
            currency=renewal.preview.currency,
            source=PrepaidServiceRenewalSource.reviewed_repair,
        )
    from app.models.collections import FinancialAccessOrigin
    from app.services.collections._core import restore_account_services

    restored = restore_account_services(
        db,
        str(renewal.preview.account_id),
        origin=FinancialAccessOrigin.prepaid_enforcement,
        resolved_by=(
            "reviewed_prepaid_service_renewal:"
            f"{renewal.preview.subscription_id}:"
            f"{renewal.preview.starts_at.isoformat()}"
        ),
    )
    return ReviewedPrepaidServiceRenewalResult(
        renewal=renewal,
        outcome=outcome,
        restored_service_count=restored,
    )


def _legacy_tax_correction_fingerprint(
    *,
    query: LegacyRenewalTaxInvoiceCorrectionQuery,
    disposition: LegacyRenewalTaxInvoiceCorrectionDisposition,
    period_start: datetime | None,
    period_end: datetime | None,
    currency: str,
    original_debit: Decimal,
    subtotal: Decimal,
    tax_total: Decimal,
    tax_rate_id: UUID | None,
    tax_application: TaxApplication,
    invoice_total: Decimal,
    credit_before: Decimal,
    credit_after: Decimal,
    reversal_preview_fingerprint: str | None,
    existing_invoice_id: UUID | None,
    reason: str,
) -> str:
    values = (
        str(query.account_id),
        str(query.subscription_id),
        str(query.adjustment_id),
        str(query.entitlement_id),
        str(round_money(query.expected_invoice_total)),
        str(round_money(query.expected_remaining_credit)),
        disposition.value,
        period_start.isoformat() if period_start else "",
        period_end.isoformat() if period_end else "",
        currency,
        str(round_money(original_debit)),
        str(round_money(subtotal)),
        str(round_money(tax_total)),
        str(tax_rate_id or ""),
        tax_application.value,
        str(round_money(invoice_total)),
        str(round_money(credit_before)),
        str(round_money(credit_after)),
        reversal_preview_fingerprint or "",
        str(existing_invoice_id or ""),
        reason,
    )
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def _legacy_tax_correction_actor(
    context: CommandContext,
) -> tuple[AuditActorType, str]:
    """Require attributable Finance-operator evidence, including on replay."""

    if context.scope != _LEGACY_TAX_CORRECTION_AUTH_SCOPE:
        _error(
            "invalid_command_context",
            "Legacy renewal tax correction requires ledger-write authorization.",
            field="scope",
        )
    actor_type_value, separator, actor_id_value = context.actor.partition(":")
    try:
        actor_type = AuditActorType(actor_type_value)
        actor_id = UUID(actor_id_value.strip())
    except (ValueError, AttributeError) as exc:
        raise PrepaidServiceRenewalError(
            code="financial.prepaid_service_renewals.invalid_command_context",
            message=(
                "Legacy renewal tax correction requires a user:<uuid> actor identity."
            ),
            details={"field": "actor"},
        ) from exc
    if not separator or actor_type is not AuditActorType.user:
        _error(
            "invalid_command_context",
            "Legacy renewal tax correction requires a user:<uuid> actor identity.",
            field="actor",
        )
    return actor_type, str(actor_id)


def _legacy_tax_correction_preview(
    *,
    query: LegacyRenewalTaxInvoiceCorrectionQuery,
    disposition: LegacyRenewalTaxInvoiceCorrectionDisposition,
    reason: str,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    currency: str = "NGN",
    original_debit: Decimal = Decimal("0.00"),
    subtotal: Decimal = Decimal("0.00"),
    tax_total: Decimal = Decimal("0.00"),
    tax_rate_id: UUID | None = None,
    tax_application: TaxApplication = TaxApplication.exempt,
    invoice_total: Decimal = Decimal("0.00"),
    credit_before: Decimal = Decimal("0.00"),
    credit_after: Decimal = Decimal("0.00"),
    reversal_preview_fingerprint: str | None = None,
    existing_invoice_id: UUID | None = None,
    fingerprint_override: str | None = None,
) -> LegacyRenewalTaxInvoiceCorrectionPreview:
    fingerprint = fingerprint_override or _legacy_tax_correction_fingerprint(
        query=query,
        disposition=disposition,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        original_debit=original_debit,
        subtotal=subtotal,
        tax_total=tax_total,
        tax_rate_id=tax_rate_id,
        tax_application=tax_application,
        invoice_total=invoice_total,
        credit_before=credit_before,
        credit_after=credit_after,
        reversal_preview_fingerprint=reversal_preview_fingerprint,
        existing_invoice_id=existing_invoice_id,
        reason=reason,
    )
    return LegacyRenewalTaxInvoiceCorrectionPreview(
        account_id=query.account_id,
        subscription_id=query.subscription_id,
        adjustment_id=query.adjustment_id,
        entitlement_id=query.entitlement_id,
        disposition=disposition,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        original_debit=round_money(original_debit),
        subtotal=round_money(subtotal),
        tax_total=round_money(tax_total),
        tax_rate_id=tax_rate_id,
        tax_application=tax_application,
        invoice_total=round_money(invoice_total),
        credit_before=round_money(credit_before),
        credit_after=round_money(credit_after),
        reversal_preview_fingerprint=reversal_preview_fingerprint,
        reason=reason,
        fingerprint=fingerprint,
        existing_invoice_id=existing_invoice_id,
    )


def _existing_legacy_tax_correction_invoice(
    db: Session,
    query: LegacyRenewalTaxInvoiceCorrectionQuery,
) -> tuple[Invoice, InvoiceLine, ServiceEntitlement, dict[str, object]] | None:
    rows = db.execute(
        select(Invoice, InvoiceLine)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.account_id == query.account_id,
            InvoiceLine.subscription_id == query.subscription_id,
            InvoiceLine.is_active.is_(True),
        )
        .order_by(Invoice.created_at, Invoice.id)
    ).all()
    for invoice, line in rows:
        metadata = dict(invoice.metadata_ or {}).get(
            _LEGACY_TAX_CORRECTION_METADATA_KEY
        )
        if not isinstance(metadata, dict):
            continue
        if metadata.get("adjustment_id") != str(query.adjustment_id) or metadata.get(
            "replaced_entitlement_id"
        ) != str(query.entitlement_id):
            continue
        entitlement = db.scalar(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_invoice_line_id == line.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        )
        if entitlement is None:
            _error(
                "legacy_tax_correction_drift",
                "Corrected invoice has no active replacement entitlement.",
                invoice_id=str(invoice.id),
            )
        return invoice, line, entitlement, metadata
    return None


def preview_legacy_prepaid_renewal_tax_invoice_correction(
    db: Session,
    query: LegacyRenewalTaxInvoiceCorrectionQuery,
) -> LegacyRenewalTaxInvoiceCorrectionPreview:
    """Prove one base-only legacy renewal can become a taxed paid invoice."""

    existing = _existing_legacy_tax_correction_invoice(db, query)
    if existing is not None:
        invoice, line, _entitlement, metadata = existing
        fingerprint = str(metadata.get("preview_fingerprint") or "")
        if (
            invoice.status is not InvoiceStatus.paid
            or round_money(invoice.balance_due) != Decimal("0.00")
            or round_money(invoice.total) != round_money(query.expected_invoice_total)
            or len(fingerprint) != 64
        ):
            _error(
                "legacy_tax_correction_drift",
                "Corrected historical renewal invoice evidence has drifted.",
                invoice_id=str(invoice.id),
            )
        currency = (invoice.currency or "NGN").upper()
        return _legacy_tax_correction_preview(
            query=query,
            disposition=LegacyRenewalTaxInvoiceCorrectionDisposition.already_corrected,
            reason="invoice carries exact legacy renewal tax-correction evidence",
            period_start=(
                _utc(invoice.billing_period_start)
                if invoice.billing_period_start is not None
                else None
            ),
            period_end=(
                _utc(invoice.billing_period_end)
                if invoice.billing_period_end is not None
                else None
            ),
            currency=currency,
            subtotal=invoice.subtotal,
            tax_total=invoice.tax_total,
            tax_rate_id=line.tax_rate_id,
            tax_application=line.tax_application,
            invoice_total=invoice.total,
            credit_after=get_account_credit_balance(
                db, str(query.account_id), currency=currency
            ),
            existing_invoice_id=invoice.id,
            fingerprint_override=fingerprint,
        )

    subscription = db.get(Subscription, query.subscription_id)
    adjustment = db.get(AccountAdjustment, query.adjustment_id)
    entitlement = db.get(ServiceEntitlement, query.entitlement_id)
    ledger_entry = (
        db.get(LedgerEntry, adjustment.ledger_entry_id)
        if adjustment is not None
        else None
    )
    if (
        subscription is None
        or adjustment is None
        or entitlement is None
        or ledger_entry is None
        or subscription.subscriber_id != query.account_id
        or subscription.billing_mode is not BillingMode.prepaid
        or subscription.status not in PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES
        or adjustment.account_id != query.account_id
        or adjustment.category is not LedgerCategory.internet_service
        or adjustment.origin != _ORIGIN.value
        or adjustment.reversed_at is not None
        or adjustment.reversal_ledger_entry_id is not None
        or ledger_entry.account_id != query.account_id
        or ledger_entry.id != adjustment.ledger_entry_id
        or ledger_entry.entry_type is not LedgerEntryType.debit
        or ledger_entry.source is not LedgerSource.adjustment
        or ledger_entry.category is not LedgerCategory.internet_service
        or not ledger_entry.is_active
        or not ledger_entry.affects_customer_position
        or ledger_entry.reversal_of_entry_id is not None
        or ledger_entry.invoice_id is not None
        or ledger_entry.payment_id is not None
        or entitlement.account_id != query.account_id
        or entitlement.subscription_id != query.subscription_id
        or entitlement.status is not ServiceEntitlementStatus.active
        or entitlement.source_ledger_entry_id != ledger_entry.id
        or entitlement.source_invoice_id is not None
        or entitlement.source_invoice_line_id is not None
        or entitlement.source_billing_grant_id is not None
        or _utc(entitlement.ends_at) <= _utc(entitlement.starts_at)
        or subscription.next_billing_at is None
        or _utc(subscription.next_billing_at) != _utc(entitlement.ends_at)
        or adjustment.origin_ref
        != _origin_ref(
            subscription.id,
            _utc(entitlement.starts_at),
            _utc(entitlement.ends_at),
        )
        or round_money(entitlement.amount_funded) != round_money(adjustment.amount)
        or round_money(ledger_entry.amount) != round_money(adjustment.amount)
        or round_money(adjustment.prepaid_funding_before)
        - round_money(adjustment.prepaid_funding_after)
        != round_money(adjustment.amount)
        or round_money(adjustment.prepaid_funding_after) < Decimal("0.00")
        or entitlement.currency.upper() != adjustment.currency.upper()
        or (ledger_entry.currency or "NGN").upper() != adjustment.currency.upper()
    ):
        return _legacy_tax_correction_preview(
            query=query,
            disposition=LegacyRenewalTaxInvoiceCorrectionDisposition.manual_review,
            reason="selected records are not one exact active legacy renewal chain",
        )

    period_start = _utc(entitlement.starts_at)
    period_end = _utc(entitlement.ends_at)
    charge = resolve_prepaid_monthly_charge_detail(db, subscription, period_start)
    credit_before = round_money(
        get_account_credit_balance(
            db,
            str(query.account_id),
            currency=adjustment.currency.upper(),
        )
    )
    expected_total = round_money(query.expected_invoice_total)
    expected_remaining = round_money(query.expected_remaining_credit)
    if (
        charge is None
        or charge.currency != adjustment.currency.upper()
        or charge.tax_application is not TaxApplication.exclusive
        or charge.tax_rate_id is None
        or charge.tax_total <= Decimal("0.00")
        or round_money(charge.unit_price) != round_money(adjustment.amount)
        or round_money(charge.subtotal) != round_money(adjustment.amount)
        or charge.total != expected_total
        or expected_remaining < Decimal("0.00")
        or round_money(adjustment.prepaid_funding_after) != credit_before
        or credit_before != round_money(charge.tax_total + expected_remaining)
    ):
        return _legacy_tax_correction_preview(
            query=query,
            disposition=LegacyRenewalTaxInvoiceCorrectionDisposition.manual_review,
            reason=(
                "current contract tax or approved balance does not exactly match "
                "the base-only legacy renewal"
            ),
            period_start=period_start,
            period_end=period_end,
            currency=adjustment.currency.upper(),
            original_debit=adjustment.amount,
            subtotal=charge.subtotal if charge is not None else Decimal("0.00"),
            tax_total=charge.tax_total if charge is not None else Decimal("0.00"),
            tax_rate_id=charge.tax_rate_id if charge is not None else None,
            tax_application=(
                charge.tax_application if charge is not None else TaxApplication.exempt
            ),
            invoice_total=charge.total if charge is not None else Decimal("0.00"),
            credit_before=credit_before,
            credit_after=expected_remaining,
        )

    competing_invoice_id = db.scalar(
        select(Invoice.id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.account_id == query.account_id,
            Invoice.is_active.is_(True),
            Invoice.status.notin_({InvoiceStatus.void, InvoiceStatus.written_off}),
            InvoiceLine.subscription_id == query.subscription_id,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.amount > Decimal("0.00"),
            or_(
                Invoice.billing_period_start.is_(None),
                Invoice.billing_period_end.is_(None),
                (
                    (Invoice.billing_period_start < period_end)
                    & (Invoice.billing_period_end > period_start)
                ),
            ),
        )
        .limit(1)
    )
    if competing_invoice_id is not None:
        return _legacy_tax_correction_preview(
            query=query,
            disposition=LegacyRenewalTaxInvoiceCorrectionDisposition.manual_review,
            reason="a competing invoice already overlaps the legacy renewal period",
            period_start=period_start,
            period_end=period_end,
            currency=charge.currency,
            original_debit=adjustment.amount,
            subtotal=charge.subtotal,
            tax_total=charge.tax_total,
            tax_rate_id=charge.tax_rate_id,
            tax_application=charge.tax_application,
            invoice_total=charge.total,
            credit_before=credit_before,
            credit_after=expected_remaining,
            existing_invoice_id=competing_invoice_id,
        )

    reversal = preview_account_adjustment_reversal(
        db,
        PreviewAccountAdjustmentReversalQuery(
            adjustment_id=adjustment.id,
            request=AccountAdjustmentReversalPreviewRequest(
                reason=(
                    "Replace base-only legacy prepaid renewal with the approved "
                    "tax-inclusive paid invoice"
                )
            ),
        ),
    )
    if (
        reversal.prepaid_funding_before != credit_before
        or reversal.prepaid_funding_after
        != round_money(expected_total + expected_remaining)
    ):
        return _legacy_tax_correction_preview(
            query=query,
            disposition=LegacyRenewalTaxInvoiceCorrectionDisposition.manual_review,
            reason="adjustment reversal does not restore the exact invoice funding",
            period_start=period_start,
            period_end=period_end,
            currency=charge.currency,
            original_debit=adjustment.amount,
            subtotal=charge.subtotal,
            tax_total=charge.tax_total,
            tax_rate_id=charge.tax_rate_id,
            tax_application=charge.tax_application,
            invoice_total=charge.total,
            credit_before=credit_before,
            credit_after=expected_remaining,
            reversal_preview_fingerprint=reversal.fingerprint,
        )

    return _legacy_tax_correction_preview(
        query=query,
        disposition=(
            LegacyRenewalTaxInvoiceCorrectionDisposition.exact_base_only_legacy_renewal
        ),
        reason=(
            "one atomic correction can reverse the base-only debit and settle "
            "the exact tax-inclusive renewal invoice"
        ),
        period_start=period_start,
        period_end=period_end,
        currency=charge.currency,
        original_debit=adjustment.amount,
        subtotal=charge.subtotal,
        tax_total=charge.tax_total,
        tax_rate_id=charge.tax_rate_id,
        tax_application=charge.tax_application,
        invoice_total=charge.total,
        credit_before=credit_before,
        credit_after=expected_remaining,
        reversal_preview_fingerprint=reversal.fingerprint,
    )


def _legacy_tax_correction_result(
    db: Session,
    *,
    invoice: Invoice,
    query: LegacyRenewalTaxInvoiceCorrectionQuery,
    preview_fingerprint: str,
    replayed: bool,
) -> LegacyRenewalTaxInvoiceCorrectionResult:
    line = db.scalar(
        select(InvoiceLine).where(
            InvoiceLine.invoice_id == invoice.id,
            InvoiceLine.subscription_id == query.subscription_id,
            InvoiceLine.is_active.is_(True),
        )
    )
    replacement = (
        db.scalar(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_invoice_line_id == line.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        )
        if line is not None
        else None
    )
    replaced = db.get(ServiceEntitlement, query.entitlement_id)
    adjustment = db.get(AccountAdjustment, query.adjustment_id)
    allocations = tuple(
        db.scalars(
            select(PaymentAllocation)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
            )
            .order_by(PaymentAllocation.id)
        ).all()
    )
    remaining = round_money(
        get_account_credit_balance(
            db, str(query.account_id), currency=(invoice.currency or "NGN").upper()
        )
    )
    invoice_number = (invoice.invoice_number or "").strip()
    reversal_ledger_entry_id = (
        adjustment.reversal_ledger_entry_id if adjustment is not None else None
    )
    if (
        line is None
        or replacement is None
        or replaced is None
        or replaced.status is not ServiceEntitlementStatus.reversed
        or adjustment is None
        or adjustment.reversed_at is None
        or reversal_ledger_entry_id is None
        or not invoice_number
        or invoice.status is not InvoiceStatus.paid
        or round_money(invoice.balance_due) != Decimal("0.00")
        or round_money(invoice.total) != round_money(query.expected_invoice_total)
        or remaining != round_money(query.expected_remaining_credit)
        or round_money(
            sum((allocation.amount for allocation in allocations), Decimal("0.00"))
        )
        != round_money(invoice.total)
    ):
        _error(
            "legacy_tax_correction_incomplete",
            "Legacy renewal tax-invoice correction did not close exactly.",
            invoice_id=str(invoice.id),
        )
    return LegacyRenewalTaxInvoiceCorrectionResult(
        invoice_id=invoice.id,
        invoice_number=invoice_number,
        invoice_line_id=line.id,
        replacement_entitlement_id=replacement.id,
        replaced_entitlement_id=replaced.id,
        adjustment_id=adjustment.id,
        reversal_ledger_entry_id=reversal_ledger_entry_id,
        payment_allocation_ids=tuple(allocation.id for allocation in allocations),
        invoice_total=round_money(invoice.total),
        tax_total=round_money(invoice.tax_total),
        remaining_credit=remaining,
        preview_fingerprint=preview_fingerprint,
        replayed=replayed,
    )


def correct_legacy_prepaid_renewal_tax_invoice(
    db: Session,
    command: CorrectLegacyRenewalTaxInvoiceCommand,
) -> LegacyRenewalTaxInvoiceCorrectionResult:
    """Atomically replace one base-only legacy renewal with its taxed invoice."""

    def operation() -> LegacyRenewalTaxInvoiceCorrectionResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error(
                "missing_idempotency_key",
                "Legacy renewal tax correction requires a bounded idempotency key.",
            )
        expected = command.expected_preview_fingerprint.strip().lower()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            _error(
                "invalid_preview_fingerprint",
                "Legacy renewal tax correction requires a SHA-256 preview.",
            )
        actor_type, actor_id = _legacy_tax_correction_actor(command.context)

        lock_account(db, str(command.query.account_id))
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _LEGACY_TAX_CORRECTION_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            if (
                reservation.account_id != command.query.account_id
                or not reservation.ref_id
            ):
                _error(
                    "idempotency_conflict",
                    "Legacy renewal tax-correction key belongs to other evidence.",
                )
            try:
                invoice_id = UUID(reservation.ref_id)
            except ValueError:
                _error(
                    "legacy_tax_correction_incomplete",
                    "Correction reservation contains an invalid invoice identity.",
                )
            invoice = db.get(Invoice, invoice_id)
            if invoice is None:
                _error(
                    "legacy_tax_correction_incomplete",
                    "Correction reservation points to a missing invoice.",
                )
            metadata = dict(invoice.metadata_ or {}).get(
                _LEGACY_TAX_CORRECTION_METADATA_KEY
            )
            if (
                not isinstance(metadata, dict)
                or metadata.get("preview_fingerprint") != expected
            ):
                _error(
                    "idempotency_conflict",
                    "Legacy renewal tax-correction key was reused with other evidence.",
                )
            return _legacy_tax_correction_result(
                db,
                invoice=invoice,
                query=command.query,
                preview_fingerprint=expected,
                replayed=True,
            )

        subscription = lock_for_update(db, Subscription, command.query.subscription_id)
        adjustment = lock_for_update(db, AccountAdjustment, command.query.adjustment_id)
        entitlement = lock_for_update(
            db, ServiceEntitlement, command.query.entitlement_id
        )
        if subscription is None or adjustment is None or entitlement is None:
            _error(
                "legacy_tax_correction_not_found",
                "Selected subscription or legacy renewal evidence was not found.",
            )

        current = preview_legacy_prepaid_renewal_tax_invoice_correction(
            db, command.query
        )
        if current.fingerprint != expected:
            _error(
                "stale_preview",
                "Legacy renewal tax-correction evidence changed; preview again.",
            )
        if (
            not current.actionable
            or current.period_start is None
            or current.period_end is None
            or current.reversal_preview_fingerprint is None
        ):
            _error(
                "legacy_tax_correction_not_actionable",
                "Legacy renewal requires additional review.",
                disposition=current.disposition.value,
                reason=current.reason,
            )

        reservation = IdempotencyKey(
            scope=_LEGACY_TAX_CORRECTION_SCOPE,
            key=key,
            account_id=current.account_id,
        )
        db.add(reservation)
        db.flush()

        reversal = stage_account_adjustment_reversal_for_renewal_owner(
            db,
            ReverseAccountAdjustmentCommand(
                context=command.context,
                adjustment_id=adjustment.id,
                confirmation=AccountAdjustmentReversalConfirm(
                    reason=(
                        "Replace base-only legacy prepaid renewal with the approved "
                        "tax-inclusive paid invoice"
                    ),
                    preview_fingerprint=current.reversal_preview_fingerprint,
                    idempotency_key=key,
                ),
            ),
        )

        entitlement.status = ServiceEntitlementStatus.reversed
        entitlement.metadata_ = {
            **(entitlement.metadata_ or {}),
            "revoked_reason": "replaced_by_tax_inclusive_paid_invoice",
            "tax_correction_preview_fingerprint": current.fingerprint,
        }
        db.flush()

        charge = resolve_prepaid_monthly_charge_detail(
            db, subscription, current.period_start
        )
        if (
            charge is None
            or charge.unit_price != current.subtotal
            or charge.subtotal != current.subtotal
            or charge.tax_total != current.tax_total
            or charge.tax_rate_id != current.tax_rate_id
            or charge.tax_application is not current.tax_application
            or charge.total != current.invoice_total
        ):
            _error(
                "stale_preview",
                "Contract tax changed after the correction preview.",
            )
        origin_ref = _origin_ref(
            subscription.id, current.period_start, current.period_end
        )
        local_start = current.period_start.astimezone(APP_TIMEZONE).date()
        local_end = current.period_end.astimezone(APP_TIMEZONE).date()
        try:
            invoice = Invoices.stage_system_invoice_for_owner(
                db,
                InvoiceCreate(
                    account_id=current.account_id,
                    status=InvoiceStatus.draft,
                    currency=current.currency,
                    subtotal=current.subtotal,
                    tax_total=current.tax_total,
                    total=current.invoice_total,
                    balance_due=current.invoice_total,
                    billing_period_start=current.period_start,
                    billing_period_end=current.period_end,
                    memo=(
                        "Reviewed correction of legacy prepaid renewal "
                        f"{local_start.isoformat()} - {local_end.isoformat()}"
                    ),
                ),
                reason="legacy_prepaid_renewal_tax_invoice_correction",
            )
            line = InvoiceLines.stage_system_line_for_owner(
                db,
                SystemInvoiceLineCreate(
                    invoice_id=invoice.id,
                    subscription_id=subscription.id,
                    description=(
                        f"{subscription.offer.name if subscription.offer else 'Service'} "
                        f"({local_start.isoformat()} - {local_end.isoformat()})"
                    ),
                    quantity=Decimal("1.000"),
                    unit_price=charge.unit_price,
                    amount=charge.unit_price,
                    tax_rate_id=charge.tax_rate_id,
                    tax_application=charge.tax_application,
                    metadata_={
                        "kind": "base_subscription",
                        "billing_period_start": current.period_start.isoformat(),
                        "billing_period_end": current.period_end.isoformat(),
                        "renewal_preview_fingerprint": current.fingerprint,
                        "renewal_idempotency_key": _idempotency_key(origin_ref),
                        "renewal_evidence_ref": command.context.reason,
                        "renewal_funding_before": str(
                            reversal.preview.prepaid_funding_after
                        ),
                        "renewal_funding_after": str(current.credit_after),
                        "renewal_account_id": str(current.account_id),
                        "renewal_subscription_id": str(subscription.id),
                        "renewal_amount": str(current.invoice_total),
                        "renewal_currency": current.currency,
                        "legacy_adjustment_id": str(adjustment.id),
                        "replaced_entitlement_id": str(entitlement.id),
                    },
                    billing_line_key=_renewal_billing_line_key(origin_ref),
                ),
                reason="legacy_prepaid_renewal_tax_invoice_correction",
            )
        except InvoiceOwnerError as exc:
            _error(
                "invoice_rejected",
                "Invoice owner rejected the legacy renewal tax correction.",
                participant_error=exc.code,
            )

        invoice.metadata_ = {
            **(invoice.metadata_ or {}),
            "renewal_period_authoritative": True,
            "renewal_totals_authoritative": True,
            _LEGACY_TAX_CORRECTION_METADATA_KEY: {
                "adjustment_id": str(adjustment.id),
                "replaced_entitlement_id": str(entitlement.id),
                "reversal_ledger_entry_id": str(reversal.ledger_entry.id),
                "preview_fingerprint": current.fingerprint,
                "actor": command.context.actor,
                "reason": command.context.reason,
            },
        }
        db.flush()
        _settle_exact_payment_fundable_renewal(
            db,
            invoice=invoice,
            decision_at=adjustment.created_at,
        )
        project_prepaid_billing_anchor_for_invoice(
            db,
            invoice,
            evidence_ref=f"legacy-renewal-tax-correction:{invoice.id}",
            authority=BillingAnchorAuthority.reviewed_reconciliation,
        )
        evidence = _invoice_backed_renewal_evidence(
            db,
            subscription=subscription,
            starts_at=current.period_start,
            ends_at=current.period_end,
            amount=current.invoice_total,
            currency=current.currency,
            origin_ref=origin_ref,
        )
        if evidence is None or evidence.line.id != line.id:
            _error(
                "legacy_tax_correction_incomplete",
                "Corrected paid invoice did not produce exact renewal evidence.",
            )
        reservation.ref_id = str(invoice.id)
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=actor_type,
                actor_id=actor_id,
                action="correct_legacy_prepaid_renewal_tax_invoice",
                entity_type="invoice",
                entity_id=str(invoice.id),
                request_id=str(command.context.correlation_id),
                metadata_={
                    "owner": _OWNER,
                    "account_id": str(current.account_id),
                    "subscription_id": str(subscription.id),
                    "adjustment_id": str(adjustment.id),
                    "replaced_entitlement_id": str(entitlement.id),
                    "replacement_entitlement_id": str(evidence.entitlement.id),
                    "reversal_ledger_entry_id": str(reversal.ledger_entry.id),
                    "payment_allocation_ids": [
                        str(allocation_id)
                        for allocation_id in evidence.payment_allocation_ids
                    ],
                    "subtotal": str(current.subtotal),
                    "tax_total": str(current.tax_total),
                    "invoice_total": str(current.invoice_total),
                    "credit_before": str(current.credit_before),
                    "credit_after": str(current.credit_after),
                    "preview_fingerprint": current.fingerprint,
                    "scope": command.context.scope,
                    "reason": command.context.reason,
                },
            ),
        )
        from app.services.events.dispatcher import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.prepaid_service_renewal_document_corrected,
            {
                "schema_version": 2,
                "account_id": str(current.account_id),
                "subscription_id": str(subscription.id),
                "invoice_id": str(invoice.id),
                "adjustment_id": str(adjustment.id),
                "replaced_entitlement_id": str(entitlement.id),
                "replacement_entitlement_id": str(evidence.entitlement.id),
                "reversal_ledger_entry_id": str(reversal.ledger_entry.id),
                "period_start": current.period_start.isoformat(),
                "period_end": current.period_end.isoformat(),
                "invoice_total": str(current.invoice_total),
                "tax_total": str(current.tax_total),
                "remaining_credit": str(current.credit_after),
                "preview_fingerprint": current.fingerprint,
            },
            actor=command.context.actor,
            account_id=current.account_id,
            subscription_id=subscription.id,
            invoice_id=invoice.id,
        )
        db.flush()
        return _legacy_tax_correction_result(
            db,
            invoice=invoice,
            query=command.query,
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    try:
        return execute_owner_command(
            db,
            definition=_LEGACY_TAX_CORRECTION_COMMAND,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise PrepaidServiceRenewalError(
            code="financial.prepaid_service_renewals.legacy_tax_correction_write_conflict",
            message="Legacy renewal correction conflicted with another command; retry.",
            details={"account_id": str(command.query.account_id)},
            retryable=True,
        ) from exc


def _stage_prepaid_consumption_posting(
    db: Session,
    *,
    renewal: PrepaidServiceRenewalPreview,
    adjustment: AccountAdjustment,
    entitlement: ServiceEntitlement,
    require_existing: bool = False,
) -> None:
    """Stage the exact renewal consumption at this owner's command root."""

    from app.services.owner_commands import (
        current_command_context,
        owner_command_active,
    )

    if not owner_command_active(db, owner=_OWNER):
        return

    from app.models.customer_subledger import (
        CustomerPostingGroup,
        PositionEffectKind,
        PostingCommandKind,
        PostingProducer,
        PostingSourceKind,
    )
    from app.services.billing.customer_subledger import (
        EffectInput,
        StagePostingGroupCommand,
        stage_posting_group,
    )

    if require_existing:
        existing_group_id = db.scalar(
            select(CustomerPostingGroup.id).where(
                CustomerPostingGroup.producer_owner
                == PostingProducer.prepaid_service_renewals.value,
                CustomerPostingGroup.source_kind
                == PostingSourceKind.account_adjustment.value,
                CustomerPostingGroup.source_id == adjustment.id,
            )
        )
        if existing_group_id is None:
            # A replay of a pre-forward-shadow renewal is evidence debt, not
            # permission to manufacture a historical posting after the fact.
            return

    # Historical invoice-less renewal consumed pooled customer credit without a
    # separately persisted reservation decision. Preserve its replay posting as
    # an immutable legacy fact; new renewals use paid invoice evidence instead.
    # Express the instantaneous
    # transfer and consumption in one immutable group: credit is consumed,
    # the same amount is reserved, then that reservation funds the exact
    # entitlement. The reserved lane nets to zero while consumption evidence
    # remains explicit.
    stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=renewal.account_id,
            currency=renewal.currency,
            command_kind=PostingCommandKind.prepaid_consumption,
            producer_owner=PostingProducer.prepaid_service_renewals,
            source_kind=PostingSourceKind.account_adjustment,
            source_id=adjustment.id,
            occurred_at=renewal.starts_at,
            effects=(
                EffectInput(
                    effect=PositionEffectKind.customer_credit_consumed,
                    amount=renewal.amount,
                    entitlement_id=entitlement.id,
                ),
                EffectInput(
                    effect=PositionEffectKind.prepaid_funding_reserved,
                    amount=renewal.amount,
                    entitlement_id=entitlement.id,
                ),
                EffectInput(
                    effect=PositionEffectKind.prepaid_funding_consumed,
                    amount=renewal.amount,
                    entitlement_id=entitlement.id,
                ),
            ),
            idempotency_key=f"posting:prepaid_service_renewal:{adjustment.id}",
        ),
        context=current_command_context(db),
    )


def stage_prepaid_service_renewed_outcome(
    db: Session,
    *,
    account_id: UUID,
    subscription_id: UUID,
    entitlement_id: UUID,
    invoice_id: UUID | None,
    ledger_entry_id: UUID | None,
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
    if (invoice_id is None) == (ledger_entry_id is None):
        _error(
            "incomplete_funding_evidence",
            "Renewed service requires exactly one invoice or legacy debit source.",
        )
    event = emit_event(
        db,
        EventType.prepaid_service_renewed,
        {
            "schema_version": 2,
            "subscription_id": str(subscription_id),
            "entitlement_id": str(entitlement_id),
            "invoice_id": str(invoice_id) if invoice_id is not None else None,
            "ledger_entry_id": (
                str(ledger_entry_id) if ledger_entry_id is not None else None
            ),
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
        invoice_id=invoice_id,
    )
    return PrepaidServiceRenewedOutcome(
        event_id=event.event_id,
        account_id=account_id,
        subscription_id=subscription_id,
        entitlement_id=entitlement_id,
        invoice_id=invoice_id,
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
            invoice_id = (
                UUID(str(payload["invoice_id"])) if payload.get("invoice_id") else None
            )
            ledger_entry_id = (
                UUID(str(payload["ledger_entry_id"]))
                if payload.get("ledger_entry_id")
                else None
            )
            if invoice_id is None and ledger_entry_id is None:
                continue
            outcomes.append(
                PrepaidServiceRenewedOutcome(
                    event_id=row.event_id,
                    account_id=row.account_id,
                    subscription_id=row.subscription_id,
                    entitlement_id=UUID(str(payload["entitlement_id"])),
                    invoice_id=invoice_id,
                    ledger_entry_id=ledger_entry_id,
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
      refund, reversal) re-anchors a lapsed prepaid invoice after exact current
      coverage and never writes the anchor backwards.
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
      ``financial.subscription_billing_grants`` grant, or another unresolved
      projection. Exact extension coverage is already part of the coverage
      floor; payment re-anchoring never infers a second extension delta.
      Overwriting the lead would silently claw back service another owner
      granted, so advancement is monotonic while this invoice's own
      entitlements survive.
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
            stage_subscription_billing_anchor(
                db,
                subscription,
                BillingAnchorProjectionCommand(
                    subscription_id=subscription.id,
                    expected_previous=subscription.next_billing_at,
                    target=target,
                    source=BillingAnchorProjectionSource.prepaid_coverage,
                    evidence_ref=evidence_ref,
                ),
            )
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
    """One entitlement-backed anchor that diverges from exact coverage."""

    subscription_id: UUID
    account_id: UUID
    current_next_billing_at: datetime | None
    entitlement_coverage_end: datetime
    extension_coverage_end: datetime | None
    coverage_end: datetime

    @property
    def drift(self) -> timedelta | None:
        if self.current_next_billing_at is None:
            return None
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
    include_unsupported_leads: bool = False,
) -> tuple[tuple[StaleBillingAnchorCandidate, ...], bool]:
    entitlement_coverage = (
        select(
            ServiceEntitlement.subscription_id.label("subscription_id"),
            func.max(ServiceEntitlement.ends_at).label("entitlement_end"),
        )
        .where(ServiceEntitlement.status == ServiceEntitlementStatus.active)
        .group_by(ServiceEntitlement.subscription_id)
        .subquery()
    )
    extension_coverage = (
        select(
            ServiceExtensionEntry.subscription_id.label("subscription_id"),
            func.max(ServiceExtensionEntry.grant_ends_at).label("extension_end"),
        )
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
        )
        .group_by(ServiceExtensionEntry.subscription_id)
        .subquery()
    )
    # Discovery remains entitlement-backed: extension-only projection drift is
    # reconciled by financial.service_extensions. Once this owner repairs a
    # funded-entitlement candidate, however, the target must respect the full
    # authoritative coverage union and may not stop below a later applied grant.
    coverage_end = case(
        (
            extension_coverage.c.extension_end > entitlement_coverage.c.entitlement_end,
            extension_coverage.c.extension_end,
        ),
        else_=entitlement_coverage.c.entitlement_end,
    ).label("coverage_end")
    lagging_or_absent = or_(
        Subscription.next_billing_at.is_(None),
        coverage_end > Subscription.next_billing_at,
    )
    # Pulling an anchor backwards is intentionally narrower than advancing it.
    # An applied service extension is exact coverage owned by another service;
    # this repair must never erase that grant. Unsupported leads are therefore
    # eligible only in an explicitly selected, reviewed cohort with no applied
    # extension evidence at all.
    applied_extension_exists = (
        select(ServiceExtensionEntry.id)
        .join(
            ServiceExtension,
            ServiceExtension.id == ServiceExtensionEntry.extension_id,
        )
        .where(
            ServiceExtensionEntry.subscription_id == Subscription.id,
            ServiceExtension.status == ServiceExtensionStatus.applied,
        )
        .exists()
    )
    unsupported_lead = (coverage_end < Subscription.next_billing_at) & (
        ~applied_extension_exists
    )
    candidate_predicate = (
        or_(lagging_or_absent, unsupported_lead)
        if include_unsupported_leads
        else lagging_or_absent
    )
    query = (
        select(
            Subscription.id,
            Subscription.subscriber_id,
            Subscription.next_billing_at,
            entitlement_coverage.c.entitlement_end,
            extension_coverage.c.extension_end,
            coverage_end,
        )
        .join(
            entitlement_coverage,
            entitlement_coverage.c.subscription_id == Subscription.id,
        )
        .outerjoin(
            extension_coverage,
            extension_coverage.c.subscription_id == Subscription.id,
        )
        .where(
            Subscription.status == SubscriptionStatus.active,
            Subscription.billing_mode == BillingMode.prepaid,
            candidate_predicate,
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
            current_next_billing_at=_utc(row[2]) if row[2] is not None else None,
            entitlement_coverage_end=_utc(row[3]),
            extension_coverage_end=_utc(row[4]) if row[4] is not None else None,
            coverage_end=_utc(row[5]),
        )
        for row in rows[:limit]
    )
    return candidates, truncated


def _stale_billing_anchor_fingerprint(
    candidates: Sequence[StaleBillingAnchorCandidate],
) -> str:
    material = "|".join(
        f"{item.subscription_id}:"
        f"{item.current_next_billing_at.isoformat() if item.current_next_billing_at else 'NULL'}:"
        f"{item.entitlement_coverage_end.isoformat()}:"
        f"{item.extension_coverage_end.isoformat() if item.extension_coverage_end else 'NULL'}:"
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
    include_unsupported_leads: bool = False,
) -> StaleBillingAnchorRepairPreview:
    """Report subscriptions whose anchor diverges from authoritative coverage.

    This includes the pre-existing drift cohort created while the
    payment-allocation path committed entitlements without ever reaching this
    owner, plus active prepaid subscriptions whose anchor is NULL while an
    active entitlement proves the exact paid-through boundary. For every
    entitlement-backed candidate, the target is the later of active funded
    entitlement coverage and any applied service-extension grant. No anchor is
    inferred from mutable catalog cadence, subscription creation, or current
    time; NULL rows without exact entitlement evidence remain review stock.

    Leads are excluded by default because they may represent coverage owned by
    another service. ``include_unsupported_leads`` is accepted only for an
    explicitly selected subscription cohort; an applied service extension
    still quarantines the row. This makes backwards repair a deliberate,
    fingerprint-bound operator action rather than a bulk inference.

    Read-only. No money is posted, moved, or forgiven.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    if include_unsupported_leads and not subscription_ids:
        raise ValueError(
            "unsupported billing-anchor leads require explicit subscription_ids"
        )
    candidates, truncated = _stale_billing_anchor_candidates(
        db,
        limit=limit,
        subscription_ids=subscription_ids,
        include_unsupported_leads=include_unsupported_leads,
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
    """Align every previewed anchor to its authoritative coverage-union end.

    Idempotent by construction and by reservation. The write is a pure
    recomputation from surviving entitlement and applied service-extension
    evidence, so a repaired row leaves the cohort permanently and a replay of
    the same candidate is a no-op that reuses its existing idempotency
    reservation and audit evidence. A candidate whose coverage changed between
    preview and apply is skipped, never guessed at, and shows up in the next
    preview.
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
            db,
            limit=1,
            subscription_ids=(candidate.subscription_id,),
            include_unsupported_leads=(
                candidate.current_next_billing_at is not None
                and candidate.current_next_billing_at > candidate.coverage_end
            ),
        )
        del truncated_scan
        if not current:
            already_correct += 1
            continue
        fresh = current[0]
        if (
            fresh.current_next_billing_at != candidate.current_next_billing_at
            or fresh.entitlement_coverage_end != candidate.entitlement_coverage_end
            or fresh.extension_coverage_end != candidate.extension_coverage_end
            or fresh.coverage_end != candidate.coverage_end
        ):
            skipped_changed += 1
            continue

        material = (
            f"{candidate.subscription_id}:"
            f"{preview.as_of.isoformat()}:"
            f"{candidate.coverage_end.isoformat()}"
        )
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
        stage_subscription_billing_anchor(
            db,
            subscription,
            BillingAnchorProjectionCommand(
                subscription_id=subscription.id,
                expected_previous=subscription.next_billing_at,
                target=candidate.coverage_end,
                source=BillingAnchorProjectionSource.reviewed_reconciliation,
                evidence_ref=key,
            ),
        )
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
                        if candidate.current_next_billing_at
                        else None
                    ),
                    "repaired_next_billing_at": candidate.coverage_end.isoformat(),
                    "entitlement_coverage_end": (
                        candidate.entitlement_coverage_end.isoformat()
                    ),
                    "extension_coverage_end": (
                        candidate.extension_coverage_end.isoformat()
                        if candidate.extension_coverage_end
                        else None
                    ),
                    "drift_seconds": (
                        str(int(candidate.drift.total_seconds()))
                        if candidate.drift is not None
                        else None
                    ),
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
    only_subscription_id: UUID | None = None,
) -> FundingChangeRenewalResult:
    """Consume newly available funding for currently due prepaid service.

    Payment settlement, account-credit settlement and invoice allocation remain
    separate owners. Their completed funding-change event invokes this owner
    only after ordinary payable invoices have had first claim on the credit. A
    lapsed service starts a new period on the payment day; missed inactive
    periods are never back-billed.

    ``only_subscription_id``, when set, narrows the due-subscription scan to
    exactly that one subscription. Every real-time caller leaves this unset
    (an account's funding event legitimately funds every due subscription on
    it) -- it exists for the repair CLI
    (`scripts/billing/repair_prepaid_funding_consequences.py`), whose
    fingerprint-bound preview commits to one exact subscription/period and
    must not silently apply a broader scope than what was previewed and
    gated.
    """
    evaluated_at = _utc(effective_at)
    currency = str(funding_currency or "").strip().upper()
    if len(currency) != 3:
        raise ValueError("funding_currency must be a three-letter code")
    evidence = evidence_ref.strip()
    if not evidence:
        raise ValueError("evidence_ref is required")

    # Invoice-first invariant: an existing prepaid draft owns the documentary
    # service-period boundary. Exact verified funding settles that draft. One
    # strictly proven duplicate is voided before the current funding continues
    # to the new invoice-backed renewal path; shortfall, unbacked credit, or ambiguous
    # overlap leaves the draft unchanged and blocks that path.
    from app.services.prepaid_draft_reconciliation import (
        FundingChangeDraftCommand,
        stage_prepaid_draft_after_funding_change,
    )

    draft_result = stage_prepaid_draft_after_funding_change(
        db,
        FundingChangeDraftCommand(
            account_id=account_id,
            currency=currency,
            effective_at=evaluated_at,
            evidence_ref=evidence,
        ),
    )
    duplicate_drafts_voided = draft_result.drafts_voided
    if draft_result.drafts_found and not duplicate_drafts_voided:
        settled = draft_result.drafts_settled
        pending = draft_result.drafts_blocked
        # A real settlement must never produce a receipt with zero
        # subscription-level children (2026-09, round 7): the existing-draft
        # reconciliation owner (`prepaid_draft_reconciliation`) returns typed
        # per-subscription evidence for its own settle/void outcomes, which
        # this owner converts into the same receipt-child shape the
        # new-renewal lanes populate, so `execute_prepaid_service_after_settlement`
        # writes a non-empty `PrepaidFundingTriggerSubscriptionOutcome` row
        # whenever a real financial consequence occurred here.
        subscription_decisions = tuple(
            PrepaidFundingSubscriptionDecision(
                subscription_id=outcome.subscription_id,
                period_start=outcome.period_start,
                period_end=outcome.period_end,
                disposition=outcome.disposition,
                funding_source=outcome.funding_source,
                invoice_id=outcome.invoice_id,
                invoice_line_id=outcome.invoice_line_id,
                entitlement_id=None,
                funding_evidence_ids=outcome.funding_evidence_ids,
                amount=outcome.amount,
                currency=outcome.currency,
                evidence_fingerprint=(
                    _prepaid_funding_subscription_decision_evidence_fingerprint(
                        subscription_id=outcome.subscription_id,
                        period_start=outcome.period_start,
                        period_end=outcome.period_end,
                        disposition=outcome.disposition,
                        funding_source=outcome.funding_source,
                        invoice_id=outcome.invoice_id,
                        invoice_line_id=outcome.invoice_line_id,
                        entitlement_id=None,
                        funding_evidence_ids=outcome.funding_evidence_ids,
                        amount=outcome.amount,
                        currency=outcome.currency,
                    )
                ),
            )
            for outcome in draft_result.subscription_outcomes
        )
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
            draft_invoices_voided=0,
            draft_invoices_pending=pending,
            draft_review_exceptions=draft_result.review_exceptions,
            subscription_decisions=subscription_decisions,
        )

    due_subscriptions_query = (
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
    )
    if only_subscription_id is not None:
        due_subscriptions_query = due_subscriptions_query.where(
            Subscription.id == only_subscription_id
        )
    due_subscriptions = list(db.scalars(due_subscriptions_query).all())
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
            draft_invoices_voided=duplicate_drafts_voided,
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
            draft_invoices_voided=duplicate_drafts_voided,
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
    review_exceptions = 0
    renewals: list[PrepaidServiceRenewedOutcome] = []
    subscription_outcomes: list[PrepaidFundingSubscriptionDecision] = []
    non_cash_granted = 0
    treatment_blocked = 0
    settlement_period = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=evaluated_at,
            billing_cycle=BillingCycle.monthly,
        )
    )
    paid_day = settlement_period.starts_at
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
            period_end = (
                settlement_period.ends_at
                if period_start == paid_day
                else _period_end(period_start, BillingCycle.monthly)
            )
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
        period_end = (
            settlement_period.ends_at
            if period_start == paid_day
            else _period_end(period_start, cycle)
        )
        paid_through = prepaid_entitlement_coverage_end(
            db,
            subscription_id=subscription.id,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
        if paid_through is not None and _utc(paid_through) > period_start:
            if anchor < _utc(paid_through):
                stage_subscription_billing_anchor(
                    db,
                    subscription,
                    BillingAnchorProjectionCommand(
                        subscription_id=subscription.id,
                        expected_previous=subscription.next_billing_at,
                        target=_utc(paid_through),
                        source=BillingAnchorProjectionSource.prepaid_coverage,
                        evidence_ref=evidence,
                    ),
                )
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
        try:
            renewal = confirm_prepaid_service_renewal(
                db,
                preview,
                effective_at=evaluated_at,
                evidence_ref=evidence,
            )
        except PREPAID_RENEWAL_ISOLATABLE_ERRORS:
            # Same closed, named-type allowlist the nightly pass uses (see
            # `PREPAID_RENEWAL_ISOLATABLE_ERRORS`): ambiguous or
            # insufficient evidence for THIS subscription, decided with zero
            # mutation. `confirm_prepaid_service_renewal` already made no
            # financial/service-state change and wrote the durable review
            # item out of band -- continue to the next due subscription in
            # the same batch (a different subscription on the same account
            # may still be cleanly fundable) rather than failing the whole
            # funding event. Anything NOT in this allowlist (a posting-owner
            # failure, a post-mutation integrity failure, an unexpected
            # error) is not caught here and rolls back this event's whole
            # transaction, per the corrected design.
            review_exceptions += 1
            continue
        if not renewal.replayed:
            outcome = stage_prepaid_service_renewed_outcome(
                db,
                account_id=renewal.preview.account_id,
                subscription_id=renewal.preview.subscription_id,
                entitlement_id=renewal.entitlement.id,
                invoice_id=(
                    renewal.invoice.id if renewal.invoice is not None else None
                ),
                ledger_entry_id=(
                    renewal.ledger_entry.id
                    if renewal.ledger_entry is not None
                    else None
                ),
                period_start=renewal.preview.starts_at,
                renewed_through=renewal.preview.ends_at,
                amount=renewal.preview.amount,
                currency=renewal.preview.currency,
                source=PrepaidServiceRenewalSource.account_credit,
                trigger_payment_id=trigger_payment_id,
            )
            renewals.append(outcome)
        # Receipt-child evidence is built for BOTH the fresh and the
        # period-level-replayed case (2026-09, round 8) -- `renewal.replayed`
        # means THIS subscription/period was already fully settled by
        # earlier evidence (`_invoice_backed_renewal_evidence`/
        # `existing_adjustment`), a period-level idempotency concept
        # completely independent of whether the DURABLE EVENT itself is
        # being replayed (that's guarded much earlier, in
        # `evaluate_prepaid_service_after_settlement`, before this loop ever
        # runs). `confirm_prepaid_service_renewal`'s replay branches return
        # the exact same invoice/line/entitlement/payment-allocation
        # evidence shape as a fresh settlement, so this receipt still gets
        # real, non-empty children -- only `stage_prepaid_service_renewed_
        # outcome` (which would emit a duplicate "renewed" event/outcome
        # record) stays guarded above. Previously `funded += 1` below ran
        # unconditionally while this whole block was skipped on replay,
        # so a purely-replayed renewal could produce a `funded`-disposition
        # receipt with ZERO children -- invisible to
        # `find_successful_receipts_missing_child_evidence`.
        #
        # The reviewed-opening-funding lane applies payment-backed credit
        # fully before falling back to opening funding for the remainder
        # (`AccountCreditApplications.apply_invoice_available`), so a
        # single renewal can legitimately carry BOTH kinds of evidence at
        # once -- this is no longer treated as one-or-the-other
        # (2026-09, round 7); previously the opening-funding lane could
        # declare `opening_funding` while recording an empty evidence
        # list, because the consumption id was computed and discarded.
        funding_sources = []
        funding_evidence_ids = [str(value) for value in renewal.payment_allocation_ids]
        if renewal.payment_allocation_ids:
            funding_sources.append("payment")
        if renewal.opening_funding_consumption_id is not None:
            funding_sources.append("opening_funding")
            funding_evidence_ids.append(str(renewal.opening_funding_consumption_id))
        # Hard invariant, not just a natural consequence of the fix above
        # (2026-09, round 7): a disposition naming a funding source with
        # no matching evidence id is not a successful disposition -- this
        # must never regress silently if a future lane forgets to thread
        # its own evidence id through.
        if funding_sources and not funding_evidence_ids:
            _error(
                "funding_evidence_missing",
                "A funded renewal declared a funding source with no evidence id.",
                subscription_id=str(subscription.id),
                funding_sources=funding_sources,
            )
        decision_invoice_id = (
            renewal.invoice.id if renewal.invoice is not None else None
        )
        decision_invoice_line_id = (
            renewal.invoice_line.id if renewal.invoice_line is not None else None
        )
        decision_funding_source = "+".join(funding_sources) or None
        subscription_outcomes.append(
            PrepaidFundingSubscriptionDecision(
                subscription_id=subscription.id,
                period_start=renewal.preview.starts_at,
                period_end=renewal.preview.ends_at,
                disposition="created_canonical_renewal",
                funding_source=decision_funding_source,
                invoice_id=decision_invoice_id,
                invoice_line_id=decision_invoice_line_id,
                entitlement_id=renewal.entitlement.id,
                funding_evidence_ids=funding_evidence_ids,
                amount=renewal.preview.amount,
                currency=renewal.preview.currency,
                evidence_fingerprint=_prepaid_funding_subscription_decision_evidence_fingerprint(
                    subscription_id=subscription.id,
                    period_start=renewal.preview.starts_at,
                    period_end=renewal.preview.ends_at,
                    disposition="created_canonical_renewal",
                    funding_source=decision_funding_source,
                    invoice_id=decision_invoice_id,
                    invoice_line_id=decision_invoice_line_id,
                    entitlement_id=renewal.entitlement.id,
                    funding_evidence_ids=funding_evidence_ids,
                    amount=renewal.preview.amount,
                    currency=renewal.preview.currency,
                ),
            )
        )
        funded += 1

    db.flush()
    restored_service_count = 0
    if funded or already_covered or non_cash_granted:
        from app.models.collections import FinancialAccessOrigin
        from app.services.collections._core import restore_account_services

        restored_service_count = restore_account_services(
            db,
            str(account_id),
            origin=FinancialAccessOrigin.prepaid_enforcement,
            resolved_by=(
                "prepaid_service_after_funding_change:"
                f"{trigger_payment_id or hashlib.sha256(evidence.encode()).hexdigest()[:24]}"
            ),
        )
    disposition = (
        FundingChangeRenewalDisposition.non_cash_granted
        if non_cash_granted
        else FundingChangeRenewalDisposition.treatment_blocked
        if treatment_blocked
        else FundingChangeRenewalDisposition.funded
        if funded
        else FundingChangeRenewalDisposition.already_covered
        if already_covered
        else FundingChangeRenewalDisposition.renewal_review_required
        if review_exceptions
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
        draft_invoices_voided=duplicate_drafts_voided,
        renewal_review_exceptions=review_exceptions,
        restored_service_count=restored_service_count,
        subscription_decisions=tuple(subscription_outcomes),
    )


def _confirm_and_stage_scheduled_renewal(
    db: Session,
    *,
    preview: PrepaidServiceRenewalPreview,
    effective_at: datetime,
) -> None:
    """One subscription's scheduled-renewal confirm+stage, as one savepoint body.

    Factored out so `run_due_prepaid_service_renewals` can pass it to
    `execute_owner_savepoint` as a single callable -- the savepoint helper
    itself owns the begin/commit/rollback around whatever this raises.
    """

    renewal = confirm_prepaid_service_renewal(
        db,
        preview,
        effective_at=effective_at,
        evidence_ref=(
            f"scheduled-billing-run:{effective_at.isoformat().replace('+00:00', 'Z')}"
        ),
    )
    if not renewal.replayed:
        stage_prepaid_service_renewed_outcome(
            db,
            account_id=renewal.preview.account_id,
            subscription_id=renewal.preview.subscription_id,
            entitlement_id=renewal.entitlement.id,
            invoice_id=(renewal.invoice.id if renewal.invoice is not None else None),
            ledger_entry_id=(
                renewal.ledger_entry.id if renewal.ledger_entry is not None else None
            ),
            period_start=renewal.preview.starts_at,
            renewed_through=renewal.preview.ends_at,
            amount=renewal.preview.amount,
            currency=renewal.preview.currency,
            source=PrepaidServiceRenewalSource.scheduled,
        )


def run_due_prepaid_service_renewals(
    db: Session,
    *,
    run_at: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, PrepaidRenewalSummaryValue]:
    """Fund currently due monthly periods without historical catch-up.

    The daily billing runner calls this only behind the canonical control. A
    stale anchor older than two days is reported for reviewed reconciliation,
    never silently back-billed. Global missing authority fails closed for the
    pass. An incomplete source-batch account is reported and skipped until the
    complete history artifact materializes it; this is migration debt, not a
    permanent renewal disposition.
    """
    from app.services.billing_automation import _period_end
    from app.services.prepaid_funding_reconstruction import (
        authority_cutover_batch,
        prepaid_funding_incomplete_source_account_ids,
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
    summary: dict[str, PrepaidRenewalSummaryValue] = {
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
        # Always present, even on a fully clean pass, so a caller never has
        # to guess whether the key's absence means "isolated nothing" or
        # "this summary predates the isolation feature."
        "prepaid_renewals_isolated": [],
        # Never "ok" when `prepaid_renewals_isolated` is non-empty -- set at
        # the very end, after every subscription has been processed.
        "prepaid_renewals_status": "ok",
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
            _bump_summary(summary, "prepaid_renewals_treatment_blocked", 1)
            continue
        period_start = max(
            _utc(next_billing_at), _utc(treatment.starts_at or next_billing_at)
        )
        period_end = _period_end(period_start, BillingCycle.monthly)
        if dry_run:
            _bump_summary(summary, "prepaid_renewals_non_cash_granted", 1)
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
            _bump_summary(summary, "prepaid_renewals_treatment_blocked", 1)
            continue
        _bump_summary(summary, "prepaid_renewals_non_cash_granted", 1)

    authority = authority_cutover_batch(db)
    if authority is None:
        summary["prepaid_renewals_skipped"] = "authority_not_materialized"
        db.flush()
        return summary

    incomplete_source_account_ids = prepaid_funding_incomplete_source_account_ids(
        db,
        {subscription.subscriber_id for subscription in chargeable_subscriptions},
    )
    authority_at = _utc(authority.position_at)
    charges = resolve_prepaid_monthly_charges(
        db,
        chargeable_subscriptions,
        effective_at,
    )
    # Grouped by account, preserving each account's first-due-subscription
    # position in the original `next_billing_at, id` order (2026-09,
    # round 7: the isolation boundary moved UP from one subscription to one
    # ACCOUNT -- see `_process_one_due_prepaid_subscription` and the
    # per-account savepoint below for why).
    subscriptions_by_account: dict[UUID, list[Subscription]] = {}
    account_order: list[UUID] = []
    for subscription in chargeable_subscriptions:
        account_id = subscription.subscriber_id
        if account_id not in subscriptions_by_account:
            subscriptions_by_account[account_id] = []
            account_order.append(account_id)
        subscriptions_by_account[account_id].append(subscription)

    for account_id in account_order:
        account_subscriptions = subscriptions_by_account[account_id]
        if account_id in incomplete_source_account_ids:
            _bump_summary(
                summary, "prepaid_renewals_quarantined", len(account_subscriptions)
            )
            continue
        if dry_run:
            # Nothing mutates in a dry run, so nothing to isolate: no
            # savepoint needed, write straight to the real summary.
            for subscription in account_subscriptions:
                _process_one_due_prepaid_subscription(
                    db,
                    subscription,
                    effective_at=effective_at,
                    authority_at=authority_at,
                    charges=charges,
                    dry_run=True,
                    summary=summary,
                )
            continue

        # Nightly isolation (Michael's exact decided shape, extended
        # 2026-09 round 7 to account granularity): isolate and continue
        # ONLY for the three named, typed, account-scoped failures in
        # `PREPAID_RENEWAL_ISOLATABLE_ERRORS` -- checked by an explicit
        # closed `isinstance` allowlist, never `retryable=False`, never a
        # generic `DomainError`, never a bare `except Exception`. A
        # posting-owner failure, a DB/infrastructure failure, an unexpected
        # integrity/atomicity violation, or a programming error is NOT in
        # that allowlist and propagates naturally, aborting the whole pass
        # -- there is no catch-all here.
        #
        # The savepoint now wraps this WHOLE account's due subscriptions,
        # not one subscription: an ambiguous/isolatable conflict on ANY
        # subscription in this account rolls back every sibling
        # subscription this same pass had already funded on this account,
        # so a partial, inconsistent account-level consequence can never
        # survive this run. Counters are staged in a local scratch summary
        # for the same reason -- a Python-level counter bump is not part of
        # the DB transaction the savepoint rolls back, so it must not be
        # applied to the real summary until the whole account's savepoint
        # body has actually succeeded.
        #
        # Rollback uses `execute_owner_savepoint` (the established
        # repository helper), never a raw `db.begin_nested()` -- see
        # `tests/test_owner_commands.py:185`, which proves a raw nested
        # transaction is the wrong primitive inside an active owner command.
        account_scratch_summary = _new_scheduled_renewal_scratch_summary()

        def _run_this_account(
            _subscriptions: list[Subscription] = account_subscriptions,
            _scratch: dict[str, PrepaidRenewalSummaryValue] = account_scratch_summary,
        ) -> None:
            for subscription in _subscriptions:
                _process_one_due_prepaid_subscription(
                    db,
                    subscription,
                    effective_at=effective_at,
                    authority_at=authority_at,
                    charges=charges,
                    dry_run=False,
                    summary=_scratch,
                )

        try:
            execute_owner_savepoint(db, _run_this_account)
        except PREPAID_RENEWAL_ISOLATABLE_ERRORS as exc:
            # The savepoint above has already rolled back every mutation
            # this account's subscriptions made in this pass, including any
            # sibling subscription this SAME call had already funded before
            # the isolating subscription was reached. The finance work item
            # for the specific subscription that raised was already
            # persisted OUT OF BAND (a separate connection/transaction,
            # `_record_review_item_out_of_band`) by the code that raised
            # this -- and if THAT persistence itself had failed, it would
            # have raised a DIFFERENT (non-allowlisted) exception, which is
            # deliberately NOT caught here and aborts the whole pass instead
            # of silently isolating past an unrecorded permanent conflict.
            isolated_entry = {
                "account_id": str(account_id),
                "subscription_ids": ",".join(
                    str(subscription.id) for subscription in account_subscriptions
                ),
                "error_type": type(exc).__name__,
                "reason": exc.message,
            }
            isolated_accounts = summary.setdefault("prepaid_renewals_isolated", [])
            assert isinstance(isolated_accounts, list)
            isolated_accounts.append(isolated_entry)
            logger.warning(
                "prepaid_scheduled_renewal_account_isolated",
                extra={
                    "event": "prepaid_scheduled_renewal_account_isolated",
                    **isolated_entry,
                },
            )
            continue
        for key, value in account_scratch_summary.items():
            assert isinstance(value, int)
            _bump_summary(summary, key, value)
    db.flush()
    _finalize_scheduled_renewal_summary(db, summary)
    return summary


_SCHEDULED_RENEWAL_SCRATCH_SUMMARY_KEYS = (
    "prepaid_renewals_already_covered",
    "prepaid_renewals_stale_anchor",
    "prepaid_renewals_missing_price",
    "prepaid_renewals_missing_baseline",
    "prepaid_renewals_unfunded",
    "prepaid_renewals_funded",
    "prepaid_renewals_restored",
)


def _new_scheduled_renewal_scratch_summary() -> dict[str, PrepaidRenewalSummaryValue]:
    """One account's provisional counters, merged into the real summary only
    after that account's savepoint has actually succeeded (see the
    per-account loop above)."""

    return dict.fromkeys(_SCHEDULED_RENEWAL_SCRATCH_SUMMARY_KEYS, 0)


def _process_one_due_prepaid_subscription(
    db: Session,
    subscription: Subscription,
    *,
    effective_at: datetime,
    authority_at: datetime,
    charges: dict[UUID, tuple[Decimal, str, BillingCycle] | None],
    dry_run: bool,
    summary: dict[str, PrepaidRenewalSummaryValue],
) -> None:
    """One subscription's scheduled-renewal decision, and its mutation if due.

    Raises one of `PREPAID_RENEWAL_ISOLATABLE_ERRORS` for ambiguous or
    insufficient evidence when `dry_run` is false. The caller wraps a whole
    ACCOUNT's due subscriptions in one savepoint (2026-09, round 7) -- this
    function itself carries no savepoint, so that one raise unwinds every
    sibling subscription call this same account's loop already made in this
    pass, not just this one.
    """
    from app.services.billing_automation import _period_end
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
    )

    next_billing_at = subscription.next_billing_at
    if next_billing_at is None:
        return
    period_start = _utc(next_billing_at)
    lag = effective_at - period_start
    if period_start <= authority_at or lag > _MAX_AUTOMATIC_LAG:
        _bump_summary(summary, "prepaid_renewals_stale_anchor", 1)
        return
    charge = charges[subscription.id]
    if charge is None:
        _bump_summary(summary, "prepaid_renewals_missing_price", 1)
        return
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
            stage_subscription_billing_anchor(
                db,
                subscription,
                BillingAnchorProjectionCommand(
                    subscription_id=subscription.id,
                    expected_previous=subscription.next_billing_at,
                    target=_utc(paid_through),
                    source=BillingAnchorProjectionSource.prepaid_coverage,
                    evidence_ref=(
                        f"scheduled-coverage:{subscription.id}:"
                        f"{_utc(paid_through).isoformat()}"
                    ),
                ),
            )
        _bump_summary(summary, "prepaid_renewals_already_covered", 1)
        return
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
        # taken before this account's loop started. Preview is read-only, so
        # isolating this account cannot retain a partial renewal write.
        _bump_summary(summary, "prepaid_renewals_missing_baseline", 1)
        return
    if not preview.allowed:
        _bump_summary(summary, "prepaid_renewals_unfunded", 1)
        return
    if not dry_run:
        _confirm_and_stage_scheduled_renewal(
            db,
            preview=preview,
            effective_at=effective_at,
        )
        from app.models.collections import FinancialAccessOrigin
        from app.services.collections._core import restore_account_services

        restored = restore_account_services(
            db,
            str(subscription.subscriber_id),
            origin=FinancialAccessOrigin.prepaid_enforcement,
            resolved_by=(
                f"prepaid_service_renewal:{subscription.id}:{period_start.isoformat()}"
            ),
        )
        _bump_summary(summary, "prepaid_renewals_restored", restored)
    _bump_summary(summary, "prepaid_renewals_funded", 1)


def _finalize_scheduled_renewal_summary(
    db: Session, summary: dict[str, PrepaidRenewalSummaryValue]
) -> None:
    """Mark partial failure honestly and raise operational visibility.

    Never let the summary's status read "ok" when any account was isolated
    -- and never let a partial-failure pass go unnoticed the way the
    original silent-swallow defect did.
    """
    isolated = summary.get("prepaid_renewals_isolated")
    if not isolated:
        return
    assert isinstance(isolated, list)
    summary["prepaid_renewals_status"] = "partial_failure"
    from app.services import staff_notifications
    from app.services.db_session_adapter import db_session_adapter

    fingerprint = (
        "prepaid-scheduled-renewal-partial-failure:"
        + hashlib.sha256(
            ",".join(
                # Account-level grouping (2026-09, round 7) renamed this
                # entry's key from `subscription_id` to `subscription_ids`
                # (one account can isolate more than one due subscription
                # at once); this lookup must track that key or every batch
                # would hash to the same fingerprint regardless of which
                # accounts were actually isolated.
                sorted(str(entry.get("subscription_ids")) for entry in isolated)
            ).encode("utf-8")
        ).hexdigest()[:32]
    )
    # Guaranteed, not best-effort (2026-09, round 6/7): queued on a genuinely
    # independent connection, the same technique `_record_review_item_out_of_
    # band` uses, so this alert's fate never depends on the main pass's own
    # session/transaction state. The durable evidence for each isolated case
    # already lives in `prepaid_draft_reconciliation_exceptions` regardless
    # of whether this alert is ever delivered, so a failure here logs loudly
    # but does not abort the pass (unlike a lost review item, a lost or
    # delayed staff PAGE is not itself a loss of financial evidence) -- one
    # retry on a fresh connection before giving up, so a single transient
    # connection blip cannot silently drop the only attempt.
    out_of_band_db = db_session_adapter.create_session()
    try:
        staff_notifications.queue_permission_review_request(
            out_of_band_db,
            permission_key="billing:write",
            fingerprint=fingerprint,
            event_type="prepaid_scheduled_renewal_partial_failure",
            title=(
                f"Nightly prepaid renewal isolated {len(isolated)} "
                "subscription(s) -- review required"
            ),
            body=(
                "The scheduled prepaid renewal pass completed but isolated "
                f"{len(isolated)} subscription(s) rather than aborting the "
                "whole run. Each isolated case has its own durable review "
                "item; see prepaid_draft_reconciliation_exceptions."
            ),
            target_url="/admin/billing/prepaid-review",
            category="billing",
            source="prepaid_service_renewals.run_due_prepaid_service_renewals",
        )
        out_of_band_db.commit()
    except Exception:
        out_of_band_db.rollback()
        logger.exception(
            "prepaid_scheduled_renewal_partial_failure_alert_failed",
            extra={
                "event": "prepaid_scheduled_renewal_partial_failure_alert_failed",
                "isolated_count": len(isolated),
                "attempt": "first",
            },
        )
        retry_db = db_session_adapter.create_session()
        try:
            staff_notifications.queue_permission_review_request(
                retry_db,
                permission_key="billing:write",
                fingerprint=fingerprint,
                event_type="prepaid_scheduled_renewal_partial_failure",
                title=(
                    f"Nightly prepaid renewal isolated {len(isolated)} "
                    "subscription(s) -- review required"
                ),
                body=(
                    "The scheduled prepaid renewal pass completed but "
                    f"isolated {len(isolated)} subscription(s) rather than "
                    "aborting the whole run. Each isolated case has its own "
                    "durable review item; see "
                    "prepaid_draft_reconciliation_exceptions."
                ),
                target_url="/admin/billing/prepaid-review",
                category="billing",
                source="prepaid_service_renewals.run_due_prepaid_service_renewals",
            )
            retry_db.commit()
        except Exception:
            # The isolated-accounts list and "partial_failure" status are
            # already set on `summary` above regardless -- an alert-delivery
            # failure must not hide the honest status from whatever already
            # consumes this return value (logs, the caller's own reporting).
            # The financial evidence itself is durable independently of this
            # alert (see the module docstring above), so the pass is not
            # aborted for a second failed delivery attempt.
            retry_db.rollback()
            logger.exception(
                "prepaid_scheduled_renewal_partial_failure_alert_failed",
                extra={
                    "event": "prepaid_scheduled_renewal_partial_failure_alert_failed",
                    "isolated_count": len(isolated),
                    "attempt": "retry",
                },
            )
        finally:
            retry_db.close()
    finally:
        out_of_band_db.close()


def execute_due_prepaid_service_renewals(
    db: Session,
    command: RunDuePrepaidServiceRenewalsCommand,
) -> dict[str, PrepaidRenewalSummaryValue]:
    """Execute the scheduled renewal pass under its named owner command."""

    return execute_owner_command(
        db,
        definition=_RUN_DUE_COMMAND,
        context=command.context,
        operation=lambda: run_due_prepaid_service_renewals(
            db,
            run_at=command.run_at,
            dry_run=command.dry_run,
        ),
    )


__all__ = [
    "STALE_BILLING_ANCHOR_REPAIR_SCOPE",
    "BillingAnchorAuthority",
    "BillingAnchorProjection",
    "CorrectLegacyRenewalTaxInvoiceCommand",
    "EvaluatePrepaidServiceAfterSettlementCommand",
    "ExecuteReviewedPrepaidServiceRenewalCommand",
    "FundingChangeEvaluation",
    "FundingChangeEvaluationDisposition",
    "FundingChangeRenewalDisposition",
    "FundingChangeRenewalResult",
    "LegacyRenewalTaxInvoiceCorrectionDisposition",
    "LegacyRenewalTaxInvoiceCorrectionPreview",
    "LegacyRenewalTaxInvoiceCorrectionQuery",
    "LegacyRenewalTaxInvoiceCorrectionResult",
    "PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES",
    "PREPAID_RENEWAL_ISOLATABLE_ERRORS",
    "PrepaidFundingSubscriptionDecision",
    "PrepaidMonthlyChargeDetail",
    "PrepaidOpeningLaneUnavailableError",
    "PrepaidRecurringChargePreview",
    "PrepaidRenewalAmbiguousEvidenceError",
    "PrepaidTriggerExecutionConflictError",
    "PrepaidSettlementPeriod",
    "PrepaidSettlementPeriodQuery",
    "PrepaidSubscriptionSettlementPeriod",
    "PrepaidSubscriptionSettlementPeriodQuery",
    "PrepaidServiceRenewalPreview",
    "PrepaidServiceRenewalError",
    "PrepaidServiceRenewalResult",
    "PrepaidServiceRenewalSource",
    "PrepaidServiceRenewedOutcome",
    "RunDuePrepaidServiceRenewalsCommand",
    "ReviewedPrepaidServiceRenewalResult",
    "StaleBillingAnchorCandidate",
    "StaleBillingAnchorRepairPreview",
    "StaleBillingAnchorRepairResult",
    "apply_due_prepaid_service_after_funding_change",
    "apply_stale_prepaid_billing_anchor_repair",
    "confirm_prepaid_service_renewal",
    "correct_legacy_prepaid_renewal_tax_invoice",
    "evaluate_prepaid_service_after_settlement",
    "execute_due_prepaid_service_renewals",
    "execute_reviewed_prepaid_service_renewal",
    "execute_prepaid_service_after_settlement",
    "preview_prepaid_service_renewal",
    "preview_legacy_prepaid_renewal_tax_invoice_correction",
    "preview_prepaid_recurring_charge",
    "preview_stale_prepaid_billing_anchor_repair",
    "project_prepaid_billing_anchor_for_invoice",
    "renewal_outcomes_for_payment",
    "retract_prepaid_billing_anchors_after_funding_reversal",
    "resolve_prepaid_monthly_charge",
    "resolve_prepaid_monthly_charge_detail",
    "resolve_prepaid_monthly_charges",
    "resolve_prepaid_settlement_period",
    "resolve_prepaid_subscription_settlement_period",
    "run_due_prepaid_service_renewals",
    "stage_prepaid_service_renewed_outcome",
]
