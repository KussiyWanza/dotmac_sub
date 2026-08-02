"""Canonical application owner for evidenced subscriber account credit.

Account credit is not a wallet counter. It is the unconsumed portion of exact,
succeeded payment settlements. This owner serializes one account, chooses
eligible invoices and source payments deterministically, and composes the
existing payment-allocation preview/confirmation owner for every transfer.
It never creates payments or ledger entries directly and never commits.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentStatus,
    TopupIntent,
)
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationInbox,
)
from app.models.prepaid_funding import PrepaidOpeningFundingConsumption
from app.schemas.billing import (
    PaymentAllocationConfirm,
    PaymentAllocationPreviewRequest,
)
from app.services.billing._common import (
    get_account_credit_balance,
    lock_account,
    resolve_invoice_settlement_amounts,
)
from app.services.billing.ledger import LedgerEntries
from app.services.billing.payments import PaymentAllocations
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.domain_errors import DomainError

logger = logging.getLogger(__name__)

ELIGIBLE_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


@dataclass
class AccountCreditApplicationResult:
    account_id: str
    available_credit: Decimal = Decimal("0.00")
    applied: Decimal = Decimal("0.00")
    invoices_settled: list[str] = field(default_factory=list)
    invoices_touched: list[str] = field(default_factory=list)
    allocation_ids: list[str] = field(default_factory=list)
    unbacked_credit: Decimal = Decimal("0.00")

    @property
    def changed(self) -> bool:
        return self.applied > 0


@dataclass(frozen=True, slots=True)
class AccountCreditInvoiceFundingPreview:
    """Exact payment-backed funding available to one invoice."""

    invoice_id: UUID
    account_id: UUID
    currency: str
    invoice_remaining: Decimal
    account_credit: Decimal
    payment_backed_credit: Decimal
    spendable_credit: Decimal
    shortfall: Decimal
    unbacked_credit: Decimal
    source_payment_ids: tuple[UUID, ...]
    funding_position_at: datetime | None
    fingerprint: str

    @property
    def fully_funded(self) -> bool:
        return self.invoice_remaining > Decimal("0.00") and self.shortfall == Decimal(
            "0.00"
        )


class AccountCreditApplicationError(DomainError):
    """Fail-closed exact account-credit application failure."""


@dataclass(frozen=True)
class AccountCreditInvariantViolation:
    code: str
    account_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class AccountCreditInvariantSummary:
    """Bounded full-fleet account-credit invariant observation."""

    eligible_invoice_with_unused_credit: int = 0
    payment_overallocated: int = 0
    negative_payment_credit_source_availability: int = 0
    paid_invoice_underfunded: int = 0
    settled_deposit_without_exact_payment: int = 0
    duplicate_provider_reference: int = 0
    deposit_webhook_unresolved: int = 0

    @property
    def total(self) -> int:
        return (
            self.eligible_invoice_with_unused_credit
            + self.payment_overallocated
            + self.negative_payment_credit_source_availability
            + self.paid_invoice_underfunded
            + self.settled_deposit_without_exact_payment
            + self.duplicate_provider_reference
            + self.deposit_webhook_unresolved
        )


@dataclass(frozen=True)
class AccountCreditReleaseEntry:
    original_entry_id: UUID
    result_entry_type: LedgerEntryType
    result_source: LedgerSource
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class AccountCreditReleasePreview:
    invoice_id: UUID
    allocation_ids: tuple[UUID, ...]
    amount: Decimal
    entries: tuple[AccountCreditReleaseEntry, ...]


def _invoice_void_release_preview(
    db: Session, invoice_id: UUID
) -> AccountCreditReleasePreview:
    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == invoice_id)
        .filter(PaymentAllocation.is_active.is_(True))
        .order_by(PaymentAllocation.created_at.asc(), PaymentAllocation.id.asc())
        .all()
    )
    entries: list[AccountCreditReleaseEntry] = []
    for allocation in allocations:
        payment = allocation.payment
        invoice_entry = (
            db.get(LedgerEntry, allocation.ledger_entry_id)
            if allocation.ledger_entry_id
            else None
        )
        consumption_entry = (
            db.get(LedgerEntry, allocation.consumption_ledger_entry_id)
            if allocation.consumption_ledger_entry_id
            else None
        )
        if (
            payment is None
            or payment.status != PaymentStatus.succeeded
            or payment.refunds
            or payment.reversal is not None
            or payment.settlement is None
            or invoice_entry is None
            or consumption_entry is None
            or not invoice_entry.is_active
            or not consumption_entry.is_active
            or invoice_entry.invoice_id != invoice_id
            or consumption_entry.invoice_id is not None
            or invoice_entry.payment_id != payment.id
            or consumption_entry.payment_id != payment.id
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Invoice has applied payment or credit value; reverse that "
                    "settlement through its owner before voiding"
                ),
            )
        for entry in (invoice_entry, consumption_entry):
            entries.append(
                AccountCreditReleaseEntry(
                    original_entry_id=entry.id,
                    result_entry_type=(
                        LedgerEntryType.credit
                        if entry.entry_type == LedgerEntryType.debit
                        else LedgerEntryType.debit
                    ),
                    result_source=entry.source or LedgerSource.other,
                    amount=round_money(to_decimal(entry.amount)),
                    currency=entry.currency,
                )
            )
    return AccountCreditReleasePreview(
        invoice_id=invoice_id,
        allocation_ids=tuple(allocation.id for allocation in allocations),
        amount=round_money(
            sum(
                (to_decimal(allocation.amount) for allocation in allocations),
                Decimal("0.00"),
            )
        ),
        entries=tuple(entries),
    )


def eligible_invoices(db: Session, account_id: str) -> list[Invoice]:
    """Return collectible invoices in the canonical oldest-debt order."""
    return (
        db.query(Invoice)
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.is_proforma.is_not(True))
        .filter(Invoice.status.in_(ELIGIBLE_INVOICE_STATUSES))
        .filter(Invoice.balance_due > 0)
        .order_by(
            Invoice.due_at.asc().nulls_last(),
            Invoice.created_at.asc(),
            Invoice.id.asc(),
        )
        .all()
    )


def _source_payments(
    db: Session,
    account_id: str,
    *,
    funding_position_at: datetime | None = None,
) -> list[tuple[Payment, Decimal]]:
    query = (
        db.query(Payment)
        .filter(Payment.account_id == coerce_uuid(account_id))
        .filter(Payment.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        # Historical Splynx rows are migration evidence, not reusable cash.
        .filter(Payment.splynx_payment_id.is_(None))
        .order_by(
            Payment.paid_at.asc().nulls_last(),
            Payment.created_at.asc(),
            Payment.id.asc(),
        )
    )
    if funding_position_at is not None:
        query = query.filter(
            or_(
                Payment.created_at > funding_position_at,
                func.coalesce(Payment.paid_at, Payment.created_at)
                > funding_position_at,
            )
        )
    rows = query.all()
    account_remaining: dict[str, Decimal] = {}
    sources: list[tuple[Payment, Decimal]] = []
    for payment in rows:
        currency = (payment.currency or "NGN").upper()
        if currency not in account_remaining:
            account_remaining[currency] = max(
                Decimal("0.00"),
                round_money(
                    get_account_credit_balance(
                        db,
                        account_id,
                        currency=currency,
                        after=funding_position_at,
                    )
                ),
            )
        room = min(
            PaymentAllocations.available_amount(db, str(payment.id)),
            account_remaining[currency],
        )
        if room > 0:
            sources.append((payment, room))
            account_remaining[currency] = round_money(
                account_remaining[currency] - room
            )
    return sources


def _allocation_key(payment: Payment, invoice: Invoice) -> str:
    return f"account-credit-apply-{payment.id}-{invoice.id}"


def _invoice_funding_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AccountCreditApplications:
    """Single orchestration owner for payment-backed account-credit use."""

    @staticmethod
    def preview_invoice_funding(
        db: Session,
        invoice: Invoice,
        *,
        funding_position_at: datetime | None = None,
    ) -> AccountCreditInvoiceFundingPreview:
        """Project exact payment-backed credit for one invoice without writes.

        A reviewed opening-position boundary scopes both the reusable-credit
        ledger and source payments. Pre-boundary facts are already represented
        by that baseline and must neither be reused nor reported as current
        unbacked credit.
        """

        currency = (invoice.currency or "NGN").upper()
        invoice_remaining = max(
            Decimal("0.00"),
            round_money(to_decimal(invoice.balance_due or Decimal("0.00"))),
        )
        account_credit = max(
            Decimal("0.00"),
            round_money(
                get_account_credit_balance(
                    db,
                    str(invoice.account_id),
                    currency=currency,
                    after=funding_position_at,
                )
            ),
        )
        sources = tuple(
            (payment, room)
            for payment, room in _source_payments(
                db,
                str(invoice.account_id),
                funding_position_at=funding_position_at,
            )
            if (payment.currency or "NGN").upper() == currency
        )
        payment_backed = round_money(
            sum((room for _payment, room in sources), Decimal("0.00"))
        )
        spendable = min(account_credit, payment_backed)
        shortfall = max(
            Decimal("0.00"),
            round_money(invoice_remaining - spendable),
        )
        unbacked = max(
            Decimal("0.00"),
            round_money(account_credit - payment_backed),
        )
        source_payment_ids = tuple(payment.id for payment, _room in sources)
        payload: dict[str, object] = {
            "invoice_id": invoice.id,
            "account_id": invoice.account_id,
            "status": invoice.status.value,
            "currency": currency,
            "invoice_remaining": invoice_remaining,
            "account_credit": account_credit,
            "payment_backed_credit": payment_backed,
            "spendable_credit": spendable,
            "shortfall": shortfall,
            "unbacked_credit": unbacked,
            "funding_position_at": funding_position_at,
            "source_payments": tuple(
                (payment.id, round_money(room)) for payment, room in sources
            ),
        }
        return AccountCreditInvoiceFundingPreview(
            invoice_id=invoice.id,
            account_id=invoice.account_id,
            currency=currency,
            invoice_remaining=invoice_remaining,
            account_credit=account_credit,
            payment_backed_credit=payment_backed,
            spendable_credit=spendable,
            shortfall=shortfall,
            unbacked_credit=unbacked,
            source_payment_ids=source_payment_ids,
            funding_position_at=funding_position_at,
            fingerprint=_invoice_funding_fingerprint(payload),
        )

    @staticmethod
    def apply_invoice_fully(
        db: Session,
        invoice: Invoice,
        *,
        preview_fingerprint: str,
        funding_position_at: datetime | None = None,
    ) -> AccountCreditApplicationResult:
        """Apply exact payment-backed credit only when it covers the invoice."""

        return AccountCreditApplications._apply_invoice_payment_sources(
            db,
            invoice,
            preview_fingerprint=preview_fingerprint,
            require_full_funding=True,
            funding_position_at=funding_position_at,
        )

    @staticmethod
    def apply_invoice_available(
        db: Session,
        invoice: Invoice,
        *,
        preview_fingerprint: str,
        funding_position_at: datetime | None = None,
    ) -> AccountCreditApplicationResult:
        """Apply all exact payment-backed credit before another typed source."""

        return AccountCreditApplications._apply_invoice_payment_sources(
            db,
            invoice,
            preview_fingerprint=preview_fingerprint,
            require_full_funding=False,
            funding_position_at=funding_position_at,
        )

    @staticmethod
    def _apply_invoice_payment_sources(
        db: Session,
        invoice: Invoice,
        *,
        preview_fingerprint: str,
        require_full_funding: bool,
        funding_position_at: datetime | None,
    ) -> AccountCreditApplicationResult:
        lock_account(db, str(invoice.account_id))
        db.refresh(invoice)
        preview = AccountCreditApplications.preview_invoice_funding(
            db,
            invoice,
            funding_position_at=funding_position_at,
        )
        if preview.fingerprint != preview_fingerprint:
            raise AccountCreditApplicationError(
                code="financial.account_credit_applications.stale_preview",
                message="Invoice funding changed after preview; preview again.",
                details={"invoice_id": str(invoice.id)},
            )
        if require_full_funding and not preview.fully_funded:
            raise AccountCreditApplicationError(
                code="financial.account_credit_applications.insufficient_funding",
                message="Exact payment-backed credit does not fully fund the invoice.",
                details={
                    "invoice_id": str(invoice.id),
                    "shortfall": str(preview.shortfall),
                },
            )

        result = AccountCreditApplicationResult(
            account_id=str(invoice.account_id),
            available_credit=preview.spendable_credit,
            unbacked_credit=preview.unbacked_credit,
        )
        remaining = preview.invoice_remaining
        sources = [
            (payment, room)
            for payment, room in _source_payments(
                db,
                str(invoice.account_id),
                funding_position_at=funding_position_at,
            )
            if (payment.currency or "NGN").upper() == preview.currency
        ]
        for payment, room in sources:
            if remaining <= Decimal("0.00"):
                break
            amount = min(remaining, room)
            if amount <= Decimal("0.00"):
                continue
            request = PaymentAllocationPreviewRequest(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=amount,
            )
            try:
                allocation_preview = PaymentAllocations.preview(db, request)
                confirmation = PaymentAllocations.stage_confirm(
                    db,
                    PaymentAllocationConfirm(
                        **request.model_dump(),
                        preview_fingerprint=allocation_preview.fingerprint,
                        idempotency_key=_allocation_key(payment, invoice),
                    ),
                )
            except HTTPException as exc:
                raise AccountCreditApplicationError(
                    code="financial.account_credit_applications.allocation_rejected",
                    message="Payment-allocation owner rejected exact invoice funding.",
                    details={
                        "invoice_id": str(invoice.id),
                        "payment_id": str(payment.id),
                        "reason": str(exc.detail),
                    },
                ) from exc
            applied = round_money(to_decimal(confirmation.allocation.amount))
            result.applied = round_money(result.applied + applied)
            result.allocation_ids.append(str(confirmation.allocation.id))
            remaining = round_money(remaining - applied)
            _stage_application_posting(
                db,
                allocation=confirmation.allocation,
                invoice=invoice,
                payment=payment,
                currency=preview.currency,
                amount=applied,
            )

        db.flush()
        db.refresh(invoice)
        if require_full_funding and (
            remaining != Decimal("0.00") or invoice.status != InvoiceStatus.paid
        ):
            raise AccountCreditApplicationError(
                code="financial.account_credit_applications.incomplete_application",
                message="Exact invoice funding did not produce a paid invoice.",
                details={
                    "invoice_id": str(invoice.id),
                    "remaining": str(remaining),
                    "status": invoice.status.value,
                },
            )
        result.invoices_touched.append(str(invoice.id))
        if invoice.status == InvoiceStatus.paid:
            result.invoices_settled.append(str(invoice.id))
        return result

    @staticmethod
    def apply(db: Session, account_id: str) -> AccountCreditApplicationResult:
        result = AccountCreditApplicationResult(account_id=str(account_id))
        lock_account(db, str(account_id))

        invoices = eligible_invoices(db, str(account_id))
        if not invoices:
            return result

        currencies = sorted(
            {(invoice.currency or "NGN").upper() for invoice in invoices}
        )
        credit_by_currency = {
            currency: round_money(
                get_account_credit_balance(db, str(account_id), currency=currency)
            )
            for currency in currencies
        }
        result.available_credit = round_money(
            sum(
                (max(value, Decimal("0.00")) for value in credit_by_currency.values()),
                Decimal("0.00"),
            )
        )
        if result.available_credit <= 0:
            return result

        sources = _source_payments(db, str(account_id))
        backed_by_currency: dict[str, Decimal] = {}
        for payment, room in sources:
            currency = (payment.currency or "NGN").upper()
            backed_by_currency[currency] = round_money(
                backed_by_currency.get(currency, Decimal("0.00")) + room
            )
        result.unbacked_credit = round_money(
            sum(
                (
                    max(
                        credit_by_currency.get(currency, Decimal("0.00"))
                        - backed_by_currency.get(currency, Decimal("0.00")),
                        Decimal("0.00"),
                    )
                    for currency in currencies
                ),
                Decimal("0.00"),
            )
        )
        remaining_by_currency = {
            currency: min(
                max(credit_by_currency.get(currency, Decimal("0.00")), Decimal("0.00")),
                backed_by_currency.get(currency, Decimal("0.00")),
            )
            for currency in currencies
        }
        room_by_payment = {payment.id: room for payment, room in sources}

        for invoice in invoices:
            currency = (invoice.currency or "NGN").upper()
            invoice_remaining = round_money(to_decimal(invoice.balance_due or 0))
            if invoice_remaining <= 0:
                continue
            for payment, _room in sources:
                available = remaining_by_currency.get(currency, Decimal("0.00"))
                payment_room = room_by_payment.get(payment.id, Decimal("0.00"))
                if available <= 0 or invoice_remaining <= 0:
                    break
                if (payment.currency or "NGN").upper() != currency or payment_room <= 0:
                    continue
                amount = min(available, payment_room, invoice_remaining)
                request = PaymentAllocationPreviewRequest(
                    payment_id=payment.id,
                    invoice_id=invoice.id,
                    amount=amount,
                )
                preview = PaymentAllocations.preview(db, request)
                confirmation = PaymentAllocations.stage_confirm(
                    db,
                    PaymentAllocationConfirm(
                        **request.model_dump(),
                        preview_fingerprint=preview.fingerprint,
                        idempotency_key=_allocation_key(payment, invoice),
                    ),
                )
                applied = round_money(to_decimal(confirmation.allocation.amount))
                result.applied = round_money(result.applied + applied)
                result.allocation_ids.append(str(confirmation.allocation.id))
                _stage_application_posting(
                    db,
                    allocation=confirmation.allocation,
                    invoice=invoice,
                    payment=payment,
                    currency=currency,
                    amount=applied,
                )
                if str(invoice.id) not in result.invoices_touched:
                    result.invoices_touched.append(str(invoice.id))
                invoice_remaining = round_money(invoice_remaining - applied)
                remaining_by_currency[currency] = round_money(available - applied)
                room_by_payment[payment.id] = round_money(payment_room - applied)

            db.flush()
            db.refresh(invoice)
            if invoice.status == InvoiceStatus.paid:
                result.invoices_settled.append(str(invoice.id))

        db.flush()
        if result.changed:
            logger.info(
                "account_credit_applied",
                extra={
                    "event": "account_credit_applied",
                    "account_id": str(account_id),
                    "amount": str(result.applied),
                    "invoice_count": len(result.invoices_touched),
                },
            )
        return result

    @staticmethod
    def preview_invoice_void_release(
        db: Session, invoice_id: UUID
    ) -> AccountCreditReleasePreview:
        """Preview exact allocation evidence a void would return to credit."""
        return _invoice_void_release_preview(db, invoice_id)

    @staticmethod
    def release_for_invoice_void(
        db: Session,
        *,
        invoice_id: UUID,
        expected_allocation_ids: tuple[UUID, ...],
        memo: str,
    ) -> list[tuple[LedgerEntry, UUID]]:
        """Append reversals and retire allocations; the caller owns the commit."""
        invoice = db.get(Invoice, invoice_id)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        lock_account(db, str(invoice.account_id))
        preview = _invoice_void_release_preview(db, invoice_id)
        if preview.allocation_ids != expected_allocation_ids:
            raise HTTPException(
                status_code=409,
                detail="Account-credit allocation evidence changed; preview again",
            )
        if not preview.allocation_ids:
            return []
        allocations = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.id.in_(preview.allocation_ids))
            .with_for_update()
            .all()
        )
        if len(allocations) != len(preview.allocation_ids):
            raise HTTPException(
                status_code=409,
                detail="Account-credit allocation evidence changed; preview again",
            )
        reversals: list[tuple[LedgerEntry, UUID]] = []
        for entry in preview.entries:
            reversal = LedgerEntries.reverse(
                db,
                str(entry.original_entry_id),
                memo=memo,
                commit=False,
            )
            reversals.append((reversal, entry.original_entry_id))
        for allocation in allocations:
            allocation.is_active = False
            allocation.payment.updated_at = datetime.now(UTC)
        db.flush()
        return reversals

    @staticmethod
    def inspect_invariants(
        db: Session, account_id: str | None = None
    ) -> list[AccountCreditInvariantViolation]:
        """Read-only drift scan; it never invents or posts financial evidence."""
        query = (
            db.query(Invoice.account_id)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(ELIGIBLE_INVOICE_STATUSES), Invoice.balance_due > 0
            )
        )
        if account_id:
            query = query.filter(Invoice.account_id == coerce_uuid(account_id))
        account_ids = sorted({str(row[0]) for row in query.all()})
        violations: list[AccountCreditInvariantViolation] = []
        for candidate_id in account_ids:
            invoices = eligible_invoices(db, candidate_id)
            currencies = {(invoice.currency or "NGN").upper() for invoice in invoices}
            for currency in sorted(currencies):
                credit = round_money(
                    get_account_credit_balance(db, candidate_id, currency=currency)
                )
                if credit > 0:
                    violations.append(
                        AccountCreditInvariantViolation(
                            code="eligible_invoice_with_unused_credit",
                            account_id=candidate_id,
                            detail=f"{currency} {credit:.2f} remains while debt is payable",
                        )
                    )

        # A payment must never carry more active allocations than its cash fact.
        payment_query = db.query(Payment).filter(Payment.is_active.is_(True))
        if account_id:
            payment_query = payment_query.filter(
                Payment.account_id == coerce_uuid(account_id)
            )
        for payment in payment_query.all():
            allocated = round_money(
                sum(
                    (
                        to_decimal(allocation.amount)
                        for allocation in payment.allocations
                        if allocation.is_active
                    ),
                    Decimal("0.00"),
                )
            )
            if allocated > round_money(to_decimal(payment.amount)):
                violations.append(
                    AccountCreditInvariantViolation(
                        code="payment_overallocated",
                        account_id=str(payment.account_id or ""),
                        detail=(
                            f"payment {payment.id} allocates {allocated:.2f} from "
                            f"{to_decimal(payment.amount):.2f}"
                        ),
                    )
                )
            if payment.settlement is not None:
                source_consumed = round_money(
                    sum(
                        (
                            to_decimal(allocation.amount)
                            for allocation in payment.allocations
                            if allocation.is_active
                            and allocation.consumption_ledger_entry_id is not None
                        ),
                        Decimal("0.00"),
                    )
                )
                source_capacity = round_money(
                    to_decimal(payment.settlement.unallocated_amount)
                    - to_decimal(payment.settlement.prepaid_amount)
                )
                if source_consumed > source_capacity:
                    violations.append(
                        AccountCreditInvariantViolation(
                            code="negative_payment_credit_source_availability",
                            account_id=str(payment.account_id or ""),
                            detail=(
                                f"payment {payment.id} consumed {source_consumed:.2f} "
                                f"from source capacity {source_capacity:.2f}"
                            ),
                        )
                    )

        paid_query = db.query(Invoice).filter(
            Invoice.is_active.is_(True), Invoice.status == InvoiceStatus.paid
        )
        if account_id:
            paid_query = paid_query.filter(
                Invoice.account_id == coerce_uuid(account_id)
            )
        for invoice in paid_query.all():
            settlement = resolve_invoice_settlement_amounts(db, invoice.id)
            funded = settlement.total_applied
            total = round_money(to_decimal(invoice.total))
            if funded < total:
                violations.append(
                    AccountCreditInvariantViolation(
                        code="paid_invoice_underfunded",
                        account_id=str(invoice.account_id),
                        detail=(
                            f"paid invoice {invoice.id} has {funded:.2f} of "
                            f"{total:.2f} settlement evidence"
                        ),
                    )
                )
        intent_query = db.query(TopupIntent).filter(
            TopupIntent.purpose == "account_credit_deposit"
        )
        if account_id:
            intent_query = intent_query.filter(
                TopupIntent.account_id == coerce_uuid(account_id)
            )
        for intent in intent_query.all():
            if intent.status == "completed":
                settlement_payment = (
                    db.get(Payment, intent.completed_payment_id)
                    if intent.completed_payment_id
                    else None
                )
                if settlement_payment is None or settlement_payment.settlement is None:
                    violations.append(
                        AccountCreditInvariantViolation(
                            code="settled_deposit_without_exact_payment",
                            account_id=str(intent.account_id or ""),
                            detail=f"deposit intent {intent.id} lacks settlement evidence",
                        )
                    )

        duplicate_rows = (
            db.query(Payment.provider_id, Payment.external_id)
            .filter(Payment.provider_id.isnot(None), Payment.external_id.isnot(None))
            .filter(Payment.is_active.is_(True))
            .group_by(Payment.provider_id, Payment.external_id)
            .having(func.count(Payment.id) > 1)
            .all()
        )
        for provider_id, external_id in duplicate_rows:
            violations.append(
                AccountCreditInvariantViolation(
                    code="duplicate_provider_reference",
                    account_id=str(account_id or ""),
                    detail=f"provider {provider_id} transaction {external_id} is duplicated",
                )
            )

        for receipt in (
            db.query(IntegrationInbox)
            .join(
                IntegrationCapabilityBinding,
                IntegrationCapabilityBinding.id
                == IntegrationInbox.capability_binding_id,
            )
            .filter(
                IntegrationCapabilityBinding.capability_id == "payments.webhook.v1",
                IntegrationInbox.state.in_(
                    {"verified", "processing", "retryable", "dead_letter"}
                ),
            )
            .all()
        ):
            data = (receipt.payload_json or {}).get("data") or {}
            metadata = data.get("metadata") or data.get("meta") or {}
            intent_id = metadata.get("topup_intent_id")
            if not intent_id:
                continue
            try:
                unresolved_intent = db.get(TopupIntent, coerce_uuid(intent_id))
            except (TypeError, ValueError):
                unresolved_intent = None
            if (
                unresolved_intent is not None
                and unresolved_intent.purpose == "account_credit_deposit"
            ):
                violations.append(
                    AccountCreditInvariantViolation(
                        code="deposit_webhook_unresolved",
                        account_id=str(unresolved_intent.account_id or ""),
                        detail=f"deposit webhook {receipt.id} needs attention",
                    )
                )
        return violations

    @staticmethod
    def summarize_invariants(db: Session) -> AccountCreditInvariantSummary:
        """Return full-fleet invariant counts with a fixed query budget.

        The detailed inspector above is an operator-facing forensic read model:
        it returns exact entity references and therefore may walk individual
        records. Billing health needs only bounded counts. This projection keeps
        the same invariant definitions but lets the database aggregate source
        facts in bulk, so snapshot runtime does not grow by one query per payment
        or paid invoice.
        """
        zero = Decimal("0.00")

        payable_currencies = (
            select(
                Invoice.account_id.label("account_id"),
                func.upper(func.coalesce(Invoice.currency, "NGN")).label("currency"),
            )
            .where(
                Invoice.is_active.is_(True),
                Invoice.is_proforma.is_not(True),
                Invoice.status.in_(ELIGIBLE_INVOICE_STATUSES),
                Invoice.balance_due > zero,
            )
            .distinct()
            .subquery()
        )
        credit_total = func.coalesce(
            func.sum(
                case(
                    (
                        LedgerEntry.entry_type == LedgerEntryType.credit,
                        LedgerEntry.amount,
                    ),
                    else_=zero,
                )
            ),
            zero,
        )
        debit_total = func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            LedgerEntry.entry_type == LedgerEntryType.debit,
                            or_(
                                LedgerEntry.source.is_(None),
                                LedgerEntry.source.notin_(
                                    [LedgerSource.refund, LedgerSource.payment]
                                ),
                                LedgerEntry.payment_id.is_(None),
                            ),
                        ),
                        LedgerEntry.amount,
                    ),
                    else_=zero,
                )
            ),
            zero,
        )
        unused_credit_rows = db.execute(
            select(
                payable_currencies.c.account_id,
                payable_currencies.c.currency,
                credit_total.label("credit_total"),
                debit_total.label("debit_total"),
            )
            .select_from(payable_currencies)
            .outerjoin(
                LedgerEntry,
                and_(
                    LedgerEntry.account_id == payable_currencies.c.account_id,
                    LedgerEntry.currency == payable_currencies.c.currency,
                    LedgerEntry.invoice_id.is_(None),
                    LedgerEntry.is_active.is_(True),
                ),
            )
            .group_by(
                payable_currencies.c.account_id,
                payable_currencies.c.currency,
            )
        ).all()
        eligible_invoice_with_unused_credit = sum(
            1
            for row in unused_credit_rows
            if round_money(to_decimal(row.credit_total) - to_decimal(row.debit_total))
            > zero
        )

        allocation_totals = (
            select(
                PaymentAllocation.payment_id.label("payment_id"),
                func.coalesce(func.sum(PaymentAllocation.amount), zero).label(
                    "allocated"
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                PaymentAllocation.consumption_ledger_entry_id.is_not(
                                    None
                                ),
                                PaymentAllocation.amount,
                            ),
                            else_=zero,
                        )
                    ),
                    zero,
                ).label("source_consumed"),
            )
            .where(PaymentAllocation.is_active.is_(True))
            .group_by(PaymentAllocation.payment_id)
            .subquery()
        )
        allocated = func.coalesce(allocation_totals.c.allocated, zero)
        source_consumed = func.coalesce(
            allocation_totals.c.source_consumed,
            zero,
        )
        source_capacity = (
            PaymentSettlement.unallocated_amount - PaymentSettlement.prepaid_amount
        )
        payment_invariant_rows = db.execute(
            select(
                Payment.id,
                Payment.amount,
                allocated.label("allocated"),
                PaymentSettlement.id.label("settlement_id"),
                source_consumed.label("source_consumed"),
                source_capacity.label("source_capacity"),
            )
            .outerjoin(
                allocation_totals,
                allocation_totals.c.payment_id == Payment.id,
            )
            .outerjoin(
                PaymentSettlement,
                PaymentSettlement.payment_id == Payment.id,
            )
            .where(Payment.is_active.is_(True))
            .where(
                or_(
                    allocated > Payment.amount,
                    and_(
                        PaymentSettlement.id.is_not(None),
                        source_consumed > source_capacity,
                    ),
                )
            )
        ).all()
        payment_overallocated = 0
        negative_payment_credit_source_availability = 0
        for row in payment_invariant_rows:
            if round_money(row.allocated) > round_money(row.amount):
                payment_overallocated += 1
            if row.settlement_id is not None and round_money(
                row.source_consumed
            ) > round_money(row.source_capacity):
                negative_payment_credit_source_availability += 1

        effective_payment_amount = case(
            (
                Payment.status == PaymentStatus.succeeded,
                PaymentAllocation.amount,
            ),
            (
                and_(
                    Payment.status == PaymentStatus.partially_refunded,
                    Payment.amount > zero,
                ),
                PaymentAllocation.amount
                * (Payment.amount - func.coalesce(Payment.refunded_amount, zero))
                / func.nullif(Payment.amount, zero),
            ),
            else_=zero,
        )
        payment_totals = (
            select(
                PaymentAllocation.invoice_id.label("invoice_id"),
                func.coalesce(func.sum(effective_payment_amount), zero).label("amount"),
            )
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .where(
                PaymentAllocation.is_active.is_(True),
                Payment.is_active.is_(True),
                Payment.status.in_(
                    [PaymentStatus.succeeded, PaymentStatus.partially_refunded]
                ),
            )
            .group_by(PaymentAllocation.invoice_id)
            .subquery()
        )
        credit_totals = (
            select(
                CreditNoteApplication.invoice_id.label("invoice_id"),
                func.coalesce(func.sum(CreditNoteApplication.amount), zero).label(
                    "amount"
                ),
            )
            .group_by(CreditNoteApplication.invoice_id)
            .subquery()
        )
        opening_funding_totals = (
            select(
                PrepaidOpeningFundingConsumption.invoice_id.label("invoice_id"),
                func.coalesce(
                    func.sum(PrepaidOpeningFundingConsumption.amount),
                    zero,
                ).label("amount"),
            )
            .group_by(PrepaidOpeningFundingConsumption.invoice_id)
            .subquery()
        )
        payments_applied = func.round(
            func.coalesce(payment_totals.c.amount, zero),
            2,
        )
        credits_applied = func.round(
            func.coalesce(credit_totals.c.amount, zero),
            2,
        )
        opening_funding_applied = func.round(
            func.coalesce(opening_funding_totals.c.amount, zero),
            2,
        )
        funded_total = func.round(
            payments_applied + credits_applied + opening_funding_applied,
            2,
        )
        paid_invoice_underfunded = int(
            db.execute(
                select(func.count(Invoice.id))
                .outerjoin(
                    payment_totals,
                    payment_totals.c.invoice_id == Invoice.id,
                )
                .outerjoin(
                    credit_totals,
                    credit_totals.c.invoice_id == Invoice.id,
                )
                .outerjoin(
                    opening_funding_totals,
                    opening_funding_totals.c.invoice_id == Invoice.id,
                )
                .where(
                    Invoice.is_active.is_(True),
                    Invoice.status == InvoiceStatus.paid,
                    funded_total < func.round(Invoice.total, 2),
                )
            ).scalar()
            or 0
        )

        settled_deposit_without_exact_payment = int(
            db.execute(
                select(func.count(TopupIntent.id))
                .outerjoin(Payment, Payment.id == TopupIntent.completed_payment_id)
                .outerjoin(
                    PaymentSettlement,
                    PaymentSettlement.payment_id == Payment.id,
                )
                .where(
                    TopupIntent.purpose == "account_credit_deposit",
                    TopupIntent.status == "completed",
                    or_(
                        Payment.id.is_(None),
                        PaymentSettlement.id.is_(None),
                    ),
                )
            ).scalar()
            or 0
        )

        duplicate_provider_groups = (
            select(Payment.provider_id, Payment.external_id)
            .where(
                Payment.provider_id.is_not(None),
                Payment.external_id.is_not(None),
                Payment.is_active.is_(True),
            )
            .group_by(Payment.provider_id, Payment.external_id)
            .having(func.count(Payment.id) > 1)
            .subquery()
        )
        duplicate_provider_reference = int(
            db.execute(
                select(func.count()).select_from(duplicate_provider_groups)
            ).scalar()
            or 0
        )

        unresolved_receipts = db.execute(
            select(IntegrationInbox.payload_json)
            .join(
                IntegrationCapabilityBinding,
                IntegrationCapabilityBinding.id
                == IntegrationInbox.capability_binding_id,
            )
            .where(
                IntegrationCapabilityBinding.capability_id == "payments.webhook.v1",
                IntegrationInbox.state.in_(
                    {"verified", "processing", "retryable", "dead_letter"}
                ),
            )
        ).scalars()
        receipt_intent_ids: list[UUID] = []
        for payload in unresolved_receipts:
            data = (payload or {}).get("data") or {}
            metadata = data.get("metadata") or data.get("meta") or {}
            intent_id = metadata.get("topup_intent_id")
            if not intent_id:
                continue
            try:
                receipt_intent_ids.append(coerce_uuid(intent_id))
            except (TypeError, ValueError):
                continue

        deposit_intent_ids: set[UUID] = set()
        if receipt_intent_ids:
            deposit_intent_ids = set(
                db.execute(
                    select(TopupIntent.id).where(
                        TopupIntent.id.in_(set(receipt_intent_ids)),
                        TopupIntent.purpose == "account_credit_deposit",
                    )
                )
                .scalars()
                .all()
            )
        deposit_webhook_unresolved = sum(
            1 for intent_id in receipt_intent_ids if intent_id in deposit_intent_ids
        )

        return AccountCreditInvariantSummary(
            eligible_invoice_with_unused_credit=eligible_invoice_with_unused_credit,
            payment_overallocated=payment_overallocated,
            negative_payment_credit_source_availability=(
                negative_payment_credit_source_availability
            ),
            paid_invoice_underfunded=paid_invoice_underfunded,
            settled_deposit_without_exact_payment=(
                settled_deposit_without_exact_payment
            ),
            duplicate_provider_reference=duplicate_provider_reference,
            deposit_webhook_unresolved=deposit_webhook_unresolved,
        )


__all__ = [
    "AccountCreditApplicationError",
    "AccountCreditInvoiceFundingPreview",
    "AccountCreditApplicationResult",
    "AccountCreditApplications",
    "AccountCreditInvariantSummary",
    "AccountCreditInvariantViolation",
    "ELIGIBLE_INVOICE_STATUSES",
    "eligible_invoices",
]


def _stage_application_posting(
    db: Session,
    *,
    allocation,
    invoice,
    payment,
    currency: str,
    amount,
) -> None:
    """One shadow posting group per credit-to-invoice allocation.

    The deciding economic owner is the credit applicator, whatever host
    command carries the transaction. Unwrapped legacy roots skip; the
    verifier owns that gap (ADR 0007 Phase 3 forward-shadow).
    """
    from app.services.owner_commands import (
        current_command_context,
        owner_command_active,
    )

    if not owner_command_active(db):
        return
    from datetime import UTC as _UTC

    from app.models.customer_subledger import (
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

    db.flush()
    occurred = allocation.created_at
    if occurred is None:
        raise AccountCreditApplicationError(
            code="financial.account_credit_applications.missing_allocation_instant",
            message=("Allocation has no created_at instant for posting provenance."),
            details={"allocation_id": str(allocation.id)},
        )
    stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=invoice.account_id,
            currency=currency,
            command_kind=PostingCommandKind.customer_credit_application,
            producer_owner=PostingProducer.account_credit_applications,
            source_kind=PostingSourceKind.payment_allocation,
            source_id=allocation.id,
            occurred_at=(
                occurred.replace(tzinfo=_UTC) if occurred.tzinfo is None else occurred
            ),
            effects=(
                EffectInput(
                    effect=PositionEffectKind.customer_credit_consumed,
                    amount=amount,
                    payment_id=payment.id,
                ),
                EffectInput(
                    effect=PositionEffectKind.receivable_settled,
                    amount=amount,
                    invoice_id=invoice.id,
                ),
            ),
            idempotency_key=f"posting:payment_allocation:{allocation.id}",
        ),
        context=current_command_context(db),
    )
