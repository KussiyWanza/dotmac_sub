"""Reviewed repair owner for the historical UTC prepaid-calendar defect.

The forward settlement owner now resolves prepaid anniversaries in the declared
business timezone.  This reconciler repairs only historical rows that exactly
match the retired UTC-midnight calculation and whose invoice, entitlement,
payment settlement, and subscription anchor still form one unambiguous chain.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentRefund,
    PaymentReversal,
    PaymentSettlement,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, Subscription
from app.models.idempotency import IdempotencyKey
from app.models.service_extension import ServiceExtensionEntry
from app.models.usage import QuotaBucket
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.billing._common import lock_account
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_renewals import (
    PrepaidSettlementPeriod,
    PrepaidSettlementPeriodQuery,
    resolve_prepaid_settlement_period,
)
from app.timezone import APP_TIMEZONE_NAME

_OWNER = "financial.prepaid_billing_calendar_reconciliation"
_CONCERN = "historical prepaid billing calendar reconciliation"
_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_CONCERN,
    name="reconcile_prepaid_billing_calendar",
)
_IDEMPOTENCY_SCOPE = "prepaid_billing_calendar_reconciliation"
_METADATA_KEY = "prepaid_billing_calendar_reconciliation"
_MAX_REASON_LENGTH = 500


class PrepaidBillingCalendarDisposition(enum.Enum):
    """One deterministic review outcome for a historical invoice."""

    eligible = "eligible"
    invoice_not_found = "invoice_not_found"
    invoice_not_paid = "invoice_not_paid"
    unsupported_invoice_lines = "unsupported_invoice_lines"
    subscription_not_prepaid = "subscription_not_prepaid"
    unsupported_cadence = "unsupported_cadence"
    ambiguous_payment = "ambiguous_payment"
    settlement_missing = "settlement_missing"
    payment_returned = "payment_returned"
    period_signature_mismatch = "period_signature_mismatch"
    entitlement_mismatch = "entitlement_mismatch"
    anchor_changed = "anchor_changed"
    service_extension_present = "service_extension_present"
    usage_period_present = "usage_period_present"
    overlapping_entitlement = "overlapping_entitlement"
    overlapping_invoice = "overlapping_invoice"


_REASONS: dict[PrepaidBillingCalendarDisposition, str] = {
    PrepaidBillingCalendarDisposition.eligible: (
        "The invoice exactly matches the retired UTC-midnight calculation and "
        "has one safe, internally consistent correction path."
    ),
    PrepaidBillingCalendarDisposition.invoice_not_found: "Invoice was not found.",
    PrepaidBillingCalendarDisposition.invoice_not_paid: (
        "Only active, fully paid, non-proforma invoices can be reconciled."
    ),
    PrepaidBillingCalendarDisposition.unsupported_invoice_lines: (
        "The invoice does not have exactly one active base-subscription line."
    ),
    PrepaidBillingCalendarDisposition.subscription_not_prepaid: (
        "The linked subscription is not prepaid."
    ),
    PrepaidBillingCalendarDisposition.unsupported_cadence: (
        "The subscription has no explicit supported billing cadence."
    ),
    PrepaidBillingCalendarDisposition.ambiguous_payment: (
        "The invoice does not have exactly one active succeeded payment allocation."
    ),
    PrepaidBillingCalendarDisposition.settlement_missing: (
        "The allocated payment has no canonical settlement evidence."
    ),
    PrepaidBillingCalendarDisposition.payment_returned: (
        "The allocated payment has refund or reversal evidence."
    ),
    PrepaidBillingCalendarDisposition.period_signature_mismatch: (
        "The current invoice dates do not exactly match the retired UTC calculation."
    ),
    PrepaidBillingCalendarDisposition.entitlement_mismatch: (
        "There is not exactly one active matching entitlement sourced from this invoice."
    ),
    PrepaidBillingCalendarDisposition.anchor_changed: (
        "The subscription billing anchor no longer exactly matches this invoice end."
    ),
    PrepaidBillingCalendarDisposition.service_extension_present: (
        "A service extension exists for this subscription, so calendar intent is ambiguous."
    ),
    PrepaidBillingCalendarDisposition.usage_period_present: (
        "A usage quota period overlaps this correction and requires coordinated review."
    ),
    PrepaidBillingCalendarDisposition.overlapping_entitlement: (
        "Another active entitlement overlaps the proposed corrected period."
    ),
    PrepaidBillingCalendarDisposition.overlapping_invoice: (
        "Another active subscription invoice overlaps the proposed corrected period."
    ),
}


class PrepaidBillingCalendarReconciliationError(DomainError):
    """Transport-neutral rejection from the calendar repair owner."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidBillingCalendarReconciliationError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    return left is not None and right is not None and _utc(left) == _utc(right)


@dataclass(frozen=True, slots=True)
class PrepaidBillingCalendarPreview:
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None
    subscription_id: UUID | None
    invoice_line_id: UUID | None
    entitlement_id: UUID | None
    payment_id: UUID | None
    payment_effective_at: datetime | None
    current_starts_at: datetime | None
    current_ends_at: datetime | None
    proposed_starts_at: datetime | None
    proposed_ends_at: datetime | None
    proposed_starts_on: str | None
    proposed_ends_on: str | None
    timezone_name: str
    disposition: PrepaidBillingCalendarDisposition
    reason: str
    fingerprint: str

    @property
    def actionable(self) -> bool:
        return self.disposition is PrepaidBillingCalendarDisposition.eligible


@dataclass(frozen=True, slots=True)
class PrepaidBillingCalendarCohort:
    previews: tuple[PrepaidBillingCalendarPreview, ...]
    scanned_count: int
    actionable_count: int
    blocked_count: int
    offset: int
    limit: int
    has_previous: bool
    has_more: bool


@dataclass(frozen=True, slots=True)
class ReconcilePrepaidBillingCalendarCommand:
    context: CommandContext
    invoice_id: UUID
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class PrepaidBillingCalendarReconciliationResult:
    invoice_id: UUID
    subscription_id: UUID
    entitlement_id: UUID
    previous_starts_at: datetime
    previous_ends_at: datetime
    corrected_starts_at: datetime
    corrected_ends_at: datetime
    preview_fingerprint: str
    replayed: bool


def _fingerprint(payload: dict[str, object]) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _preview(
    *,
    invoice: Invoice,
    disposition: PrepaidBillingCalendarDisposition,
    line: InvoiceLine | None = None,
    subscription: Subscription | None = None,
    entitlement: ServiceEntitlement | None = None,
    payment: Payment | None = None,
    proposed: PrepaidSettlementPeriod | None = None,
) -> PrepaidBillingCalendarPreview:
    payload: dict[str, object] = {
        "invoice_id": str(invoice.id),
        "invoice_updated_at": _utc(invoice.updated_at).isoformat(),
        "invoice_start": (
            _utc(invoice.billing_period_start).isoformat()
            if invoice.billing_period_start is not None
            else None
        ),
        "invoice_end": (
            _utc(invoice.billing_period_end).isoformat()
            if invoice.billing_period_end is not None
            else None
        ),
        "line_id": str(line.id) if line is not None else None,
        "line_updated_at": _utc(line.updated_at).isoformat()
        if line is not None
        else None,
        "subscription_id": str(subscription.id) if subscription is not None else None,
        "subscription_updated_at": (
            _utc(subscription.updated_at).isoformat()
            if subscription is not None
            else None
        ),
        "anchor": (
            _utc(subscription.next_billing_at).isoformat()
            if subscription is not None and subscription.next_billing_at is not None
            else None
        ),
        "entitlement_id": str(entitlement.id) if entitlement is not None else None,
        "entitlement_updated_at": (
            _utc(entitlement.updated_at).isoformat()
            if entitlement is not None
            else None
        ),
        "payment_id": str(payment.id) if payment is not None else None,
        "payment_updated_at": (
            _utc(payment.updated_at).isoformat() if payment is not None else None
        ),
        "payment_effective_at": (
            _utc(payment.paid_at or payment.created_at).isoformat()
            if payment is not None
            else None
        ),
        "proposed_start": proposed.starts_at.isoformat() if proposed else None,
        "proposed_end": proposed.ends_at.isoformat() if proposed else None,
        "disposition": disposition.value,
    }
    return PrepaidBillingCalendarPreview(
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        invoice_number=invoice.invoice_number,
        subscription_id=subscription.id if subscription is not None else None,
        invoice_line_id=line.id if line is not None else None,
        entitlement_id=entitlement.id if entitlement is not None else None,
        payment_id=payment.id if payment is not None else None,
        payment_effective_at=(
            _utc(payment.paid_at or payment.created_at) if payment is not None else None
        ),
        current_starts_at=(
            _utc(invoice.billing_period_start)
            if invoice.billing_period_start is not None
            else None
        ),
        current_ends_at=(
            _utc(invoice.billing_period_end)
            if invoice.billing_period_end is not None
            else None
        ),
        proposed_starts_at=proposed.starts_at if proposed is not None else None,
        proposed_ends_at=proposed.ends_at if proposed is not None else None,
        proposed_starts_on=(
            proposed.starts_on.isoformat() if proposed is not None else None
        ),
        proposed_ends_on=proposed.ends_on.isoformat() if proposed is not None else None,
        timezone_name=APP_TIMEZONE_NAME,
        disposition=disposition,
        reason=_REASONS[disposition],
        fingerprint=_fingerprint(payload),
    )


def preview_prepaid_billing_calendar_reconciliation(
    db: Session, invoice_id: UUID
) -> PrepaidBillingCalendarPreview:
    """Classify one invoice using current authoritative evidence."""

    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        _error(
            "invoice_not_found", "Invoice was not found.", invoice_id=str(invoice_id)
        )
    if (
        not invoice.is_active
        or invoice.is_proforma
        or invoice.status is not InvoiceStatus.paid
        or Decimal(str(invoice.balance_due)) != Decimal("0.00")
        or invoice.billing_period_start is None
        or invoice.billing_period_end is None
    ):
        return _preview(
            invoice=invoice,
            disposition=PrepaidBillingCalendarDisposition.invoice_not_paid,
        )

    lines = list(
        db.scalars(
            select(InvoiceLine).where(
                InvoiceLine.invoice_id == invoice.id,
                InvoiceLine.is_active.is_(True),
            )
        ).all()
    )
    base_lines = [
        line
        for line in lines
        if line.subscription_id is not None
        and (line.metadata_ or {}).get("kind") == "base_subscription"
    ]
    if len(lines) != 1 or len(base_lines) != 1:
        return _preview(
            invoice=invoice,
            disposition=PrepaidBillingCalendarDisposition.unsupported_invoice_lines,
        )
    line = base_lines[0]
    subscription = db.get(Subscription, line.subscription_id)
    if subscription is None or subscription.billing_mode is not BillingMode.prepaid:
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            disposition=PrepaidBillingCalendarDisposition.subscription_not_prepaid,
        )
    if subscription.billing_cycle is None:
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            disposition=PrepaidBillingCalendarDisposition.unsupported_cadence,
        )

    allocations = list(
        db.scalars(
            select(PaymentAllocation)
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.is_active.is_(True),
                PaymentAllocation.amount > Decimal("0.00"),
                Payment.is_active.is_(True),
                Payment.status == PaymentStatus.succeeded,
            )
        ).all()
    )
    if len(allocations) != 1:
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            disposition=PrepaidBillingCalendarDisposition.ambiguous_payment,
        )
    payment = db.get(Payment, allocations[0].payment_id)
    assert payment is not None
    if (
        payment.account_id != invoice.account_id
        or payment.currency != invoice.currency
        or Decimal(str(invoice.total)) <= Decimal("0.00")
        or Decimal(str(allocations[0].amount)) != Decimal(str(invoice.total))
        or Decimal(str(payment.amount)) < Decimal(str(allocations[0].amount))
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            payment=payment,
            disposition=PrepaidBillingCalendarDisposition.ambiguous_payment,
        )
    settlement = db.scalar(
        select(PaymentSettlement).where(PaymentSettlement.payment_id == payment.id)
    )
    if settlement is None or Decimal(str(settlement.amount)) != Decimal(
        str(payment.amount)
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            payment=payment,
            disposition=PrepaidBillingCalendarDisposition.settlement_missing,
        )
    has_return = bool(
        db.scalar(
            select(PaymentRefund.id).where(PaymentRefund.payment_id == payment.id)
        )
        or db.scalar(
            select(PaymentReversal.id).where(PaymentReversal.payment_id == payment.id)
        )
    )
    if has_return:
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            payment=payment,
            disposition=PrepaidBillingCalendarDisposition.payment_returned,
        )

    effective_at = _utc(payment.paid_at or payment.created_at)
    legacy = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=effective_at,
            billing_cycle=subscription.billing_cycle,
            timezone_name="UTC",
        )
    )
    proposed = resolve_prepaid_settlement_period(
        PrepaidSettlementPeriodQuery(
            effective_at=effective_at,
            billing_cycle=subscription.billing_cycle,
        )
    )
    if (
        not _same_instant(invoice.billing_period_start, legacy.starts_at)
        or not _same_instant(invoice.billing_period_end, legacy.ends_at)
        or (
            _same_instant(legacy.starts_at, proposed.starts_at)
            and _same_instant(legacy.ends_at, proposed.ends_at)
        )
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.period_signature_mismatch,
        )

    entitlements = list(
        db.scalars(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_invoice_id == invoice.id,
                ServiceEntitlement.subscription_id == subscription.id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        ).all()
    )
    matching = [
        item
        for item in entitlements
        if item.source_invoice_line_id == line.id
        and _same_instant(item.starts_at, invoice.billing_period_start)
        and _same_instant(item.ends_at, invoice.billing_period_end)
    ]
    entitlement = matching[0] if len(matching) == 1 else None
    if len(entitlements) != 1 or entitlement is None:
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.entitlement_mismatch,
        )
    if not _same_instant(subscription.next_billing_at, invoice.billing_period_end):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            entitlement=entitlement,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.anchor_changed,
        )
    if db.scalar(
        select(ServiceExtensionEntry.id).where(
            ServiceExtensionEntry.subscription_id == subscription.id
        )
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            entitlement=entitlement,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.service_extension_present,
        )
    if db.scalar(
        select(QuotaBucket.id).where(
            QuotaBucket.subscription_id == subscription.id,
            or_(
                (
                    (QuotaBucket.period_start < proposed.ends_at)
                    & (QuotaBucket.period_end > proposed.starts_at)
                ),
                (
                    (QuotaBucket.period_start < invoice.billing_period_end)
                    & (QuotaBucket.period_end > invoice.billing_period_start)
                ),
            ),
        )
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            entitlement=entitlement,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.usage_period_present,
        )
    if db.scalar(
        select(ServiceEntitlement.id).where(
            ServiceEntitlement.subscription_id == subscription.id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.id != entitlement.id,
            ServiceEntitlement.starts_at < proposed.ends_at,
            ServiceEntitlement.ends_at > proposed.starts_at,
        )
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            entitlement=entitlement,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.overlapping_entitlement,
        )
    if db.scalar(
        select(Invoice.id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .where(
            Invoice.id != invoice.id,
            Invoice.is_active.is_(True),
            Invoice.status != InvoiceStatus.void,
            InvoiceLine.is_active.is_(True),
            InvoiceLine.subscription_id == subscription.id,
            Invoice.billing_period_start < proposed.ends_at,
            Invoice.billing_period_end > proposed.starts_at,
        )
    ):
        return _preview(
            invoice=invoice,
            line=line,
            subscription=subscription,
            entitlement=entitlement,
            payment=payment,
            proposed=proposed,
            disposition=PrepaidBillingCalendarDisposition.overlapping_invoice,
        )
    return _preview(
        invoice=invoice,
        line=line,
        subscription=subscription,
        entitlement=entitlement,
        payment=payment,
        proposed=proposed,
        disposition=PrepaidBillingCalendarDisposition.eligible,
    )


def preview_prepaid_billing_calendar_cohort(
    db: Session, *, limit: int = 100, offset: int = 0
) -> PrepaidBillingCalendarCohort:
    """Return a bounded work queue of exact legacy-signature invoices."""

    bounded_limit = max(1, min(limit, 500))
    bounded_offset = max(0, offset)
    invoice_ids = tuple(
        db.scalars(
            select(Invoice.id)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .join(Subscription, Subscription.id == InvoiceLine.subscription_id)
            .where(
                Invoice.is_active.is_(True),
                Invoice.is_proforma.is_(False),
                Invoice.status == InvoiceStatus.paid,
                Invoice.balance_due == Decimal("0.00"),
                Invoice.billing_period_start.is_not(None),
                Invoice.billing_period_end.is_not(None),
                InvoiceLine.is_active.is_(True),
                Subscription.billing_mode == BillingMode.prepaid,
            )
            .group_by(Invoice.id, Invoice.paid_at)
            .order_by(Invoice.paid_at.desc(), Invoice.id)
            .offset(bounded_offset)
            .limit(bounded_limit + 1)
        ).all()
    )
    previews: list[PrepaidBillingCalendarPreview] = []
    for invoice_id in invoice_ids[:bounded_limit]:
        preview = preview_prepaid_billing_calendar_reconciliation(db, invoice_id)
        if (
            preview.disposition
            is not PrepaidBillingCalendarDisposition.period_signature_mismatch
        ):
            previews.append(preview)
    actionable = sum(item.actionable for item in previews)
    return PrepaidBillingCalendarCohort(
        previews=tuple(previews),
        scanned_count=min(len(invoice_ids), bounded_limit),
        actionable_count=actionable,
        blocked_count=len(previews) - actionable,
        offset=bounded_offset,
        limit=bounded_limit,
        has_previous=bounded_offset > 0,
        has_more=len(invoice_ids) > bounded_limit,
    )


def _replay_result(
    db: Session,
    *,
    command: ReconcilePrepaidBillingCalendarCommand,
    reservation: IdempotencyKey,
) -> PrepaidBillingCalendarReconciliationResult:
    if reservation.ref_id != str(command.invoice_id):
        _error("idempotency_conflict", "Idempotency key belongs to another invoice.")
    invoice = db.get(Invoice, command.invoice_id, populate_existing=True)
    evidence = (
        dict(invoice.metadata_ or {}).get(_METADATA_KEY)
        if invoice is not None
        else None
    )
    if not isinstance(evidence, dict):
        _error("idempotency_conflict", "Reconciliation evidence is incomplete.")
    if (
        evidence.get("idempotency_key") != command.context.idempotency_key
        or evidence.get("preview_fingerprint") != command.preview_fingerprint
    ):
        _error(
            "idempotency_conflict", "Idempotency evidence does not match this review."
        )
    return PrepaidBillingCalendarReconciliationResult(
        invoice_id=command.invoice_id,
        subscription_id=UUID(str(evidence["subscription_id"])),
        entitlement_id=UUID(str(evidence["entitlement_id"])),
        previous_starts_at=datetime.fromisoformat(str(evidence["previous_starts_at"])),
        previous_ends_at=datetime.fromisoformat(str(evidence["previous_ends_at"])),
        corrected_starts_at=datetime.fromisoformat(
            str(evidence["corrected_starts_at"])
        ),
        corrected_ends_at=datetime.fromisoformat(str(evidence["corrected_ends_at"])),
        preview_fingerprint=command.preview_fingerprint,
        replayed=True,
    )


def reconcile_prepaid_billing_calendar(
    db: Session, command: ReconcilePrepaidBillingCalendarCommand
) -> PrepaidBillingCalendarReconciliationResult:
    """Apply one signed, preview-bound historical correction atomically."""

    def operation() -> PrepaidBillingCalendarReconciliationResult:
        key = (command.context.idempotency_key or "").strip()
        if not key or len(key) > 120:
            _error("missing_idempotency_key", "A bounded idempotency key is required.")
        if (
            not command.context.reason.strip()
            or len(command.context.reason) > _MAX_REASON_LENGTH
        ):
            _error("invalid_reason", "A reason of 1 to 500 characters is required.")
        invoice = db.get(Invoice, command.invoice_id)
        if invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        lock_account(db, str(invoice.account_id))
        reservation = db.scalar(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
            .with_for_update()
        )
        if reservation is not None:
            return _replay_result(db, command=command, reservation=reservation)
        locked_invoice = lock_for_update(db, Invoice, command.invoice_id)
        if locked_invoice is None:
            _error("invoice_not_found", "Invoice was not found.")
        preliminary = preview_prepaid_billing_calendar_reconciliation(
            db, command.invoice_id
        )
        if preliminary.fingerprint != command.preview_fingerprint:
            _error(
                "stale_preview",
                "Billing evidence changed; preview the correction again.",
            )
        if not preliminary.actionable:
            _error(
                "not_actionable",
                "This calendar chain is not safe for automatic reconciliation.",
                disposition=preliminary.disposition.value,
            )
        assert preliminary.subscription_id is not None
        assert preliminary.invoice_line_id is not None
        assert preliminary.entitlement_id is not None
        assert preliminary.payment_id is not None
        subscription = lock_for_update(db, Subscription, preliminary.subscription_id)
        line = lock_for_update(db, InvoiceLine, preliminary.invoice_line_id)
        entitlement = lock_for_update(
            db, ServiceEntitlement, preliminary.entitlement_id
        )
        payment = lock_for_update(db, Payment, preliminary.payment_id)
        allocation_ids = tuple(
            db.scalars(
                select(PaymentAllocation.id).where(
                    PaymentAllocation.invoice_id == command.invoice_id
                )
            ).all()
        )
        settlement_ids = tuple(
            db.scalars(
                select(PaymentSettlement.id).where(
                    PaymentSettlement.payment_id == preliminary.payment_id
                )
            ).all()
        )
        locked_allocations = [
            lock_for_update(db, PaymentAllocation, allocation_id)
            for allocation_id in allocation_ids
        ]
        locked_settlements = [
            lock_for_update(db, PaymentSettlement, settlement_id)
            for settlement_id in settlement_ids
        ]
        if (
            subscription is None
            or line is None
            or entitlement is None
            or payment is None
            or any(item is None for item in locked_allocations)
            or any(item is None for item in locked_settlements)
        ):
            _error("stale_preview", "A reviewed calendar record no longer exists.")
        db.expire_all()
        current = preview_prepaid_billing_calendar_reconciliation(
            db, command.invoice_id
        )
        if current.fingerprint != command.preview_fingerprint or not current.actionable:
            _error(
                "stale_preview",
                "Billing evidence changed while acquiring locks; preview again.",
                disposition=current.disposition.value,
            )
        assert current.proposed_starts_at is not None
        assert current.proposed_ends_at is not None
        locked_invoice = db.get(Invoice, command.invoice_id)
        subscription = db.get(Subscription, preliminary.subscription_id)
        line = db.get(InvoiceLine, preliminary.invoice_line_id)
        entitlement = db.get(ServiceEntitlement, preliminary.entitlement_id)
        if (
            locked_invoice is None
            or subscription is None
            or line is None
            or entitlement is None
        ):
            _error("stale_preview", "A reviewed calendar record no longer exists.")
        reservation = IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=current.account_id,
            ref_id=str(current.invoice_id),
        )
        db.add(reservation)
        try:
            db.flush()
        except IntegrityError:
            _error(
                "idempotency_conflict", "The idempotency key was reserved concurrently."
            )

        previous_start = _utc(locked_invoice.billing_period_start)  # type: ignore[arg-type]
        previous_end = _utc(locked_invoice.billing_period_end)  # type: ignore[arg-type]
        corrected_start = current.proposed_starts_at
        corrected_end = current.proposed_ends_at
        locked_invoice.billing_period_start = corrected_start
        locked_invoice.billing_period_end = corrected_end
        line_metadata = dict(line.metadata_ or {})
        line_metadata["period_start"] = corrected_start.isoformat()
        line_metadata["period_end"] = corrected_end.isoformat()
        line_metadata["billing_period_start"] = corrected_start.isoformat()
        line_metadata["billing_period_end"] = corrected_end.isoformat()
        line.metadata_ = line_metadata
        entitlement.starts_at = corrected_start
        entitlement.ends_at = corrected_end
        entitlement_metadata = dict(entitlement.metadata_ or {})
        entitlement_metadata[_METADATA_KEY] = {
            "preview_fingerprint": current.fingerprint,
            "corrected_at": datetime.now(UTC).isoformat(),
        }
        entitlement.metadata_ = entitlement_metadata
        subscription.next_billing_at = corrected_end
        evidence = {
            "owner": _OWNER,
            "timezone": APP_TIMEZONE_NAME,
            "reason": command.context.reason.strip(),
            "actor": command.context.actor,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "idempotency_key": key,
            "preview_fingerprint": current.fingerprint,
            "subscription_id": str(subscription.id),
            "entitlement_id": str(entitlement.id),
            "payment_id": str(current.payment_id),
            "previous_starts_at": previous_start.isoformat(),
            "previous_ends_at": previous_end.isoformat(),
            "corrected_starts_at": corrected_start.isoformat(),
            "corrected_ends_at": corrected_end.isoformat(),
            "economic_delta": "0.00",
        }
        invoice_metadata = dict(locked_invoice.metadata_ or {})
        invoice_metadata[_METADATA_KEY] = evidence
        locked_invoice.metadata_ = invoice_metadata
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.user,
                actor_id=command.context.actor,
                action="reconcile_prepaid_billing_calendar",
                entity_type="invoice",
                entity_id=str(locked_invoice.id),
                metadata_=evidence,
            ),
        )
        emit_event(
            db,
            EventType.prepaid_billing_calendar_reconciled,
            {
                "schema_version": 1,
                "invoice_id": str(locked_invoice.id),
                **evidence,
            },
            actor=command.context.actor,
            account_id=locked_invoice.account_id,
            subscription_id=subscription.id,
            invoice_id=locked_invoice.id,
        )
        db.flush()
        return PrepaidBillingCalendarReconciliationResult(
            invoice_id=locked_invoice.id,
            subscription_id=subscription.id,
            entitlement_id=entitlement.id,
            previous_starts_at=previous_start,
            previous_ends_at=previous_end,
            corrected_starts_at=corrected_start,
            corrected_ends_at=corrected_end,
            preview_fingerprint=current.fingerprint,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_COMMAND,
        context=command.context,
        operation=operation,
    )
