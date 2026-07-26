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
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    TopupIntent,
)
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationInbox,
)
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


def _source_payments(db: Session, account_id: str) -> list[tuple[Payment, Decimal]]:
    rows = (
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
        .all()
    )
    account_remaining: dict[str, Decimal] = {}
    sources: list[tuple[Payment, Decimal]] = []
    for payment in rows:
        currency = (payment.currency or "NGN").upper()
        if currency not in account_remaining:
            account_remaining[currency] = max(
                Decimal("0.00"),
                round_money(
                    get_account_credit_balance(db, account_id, currency=currency)
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
    ) -> AccountCreditInvoiceFundingPreview:
        """Project exact payment-backed credit for one invoice without writes."""

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
                )
            ),
        )
        sources = tuple(
            (payment, room)
            for payment, room in _source_payments(db, str(invoice.account_id))
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
            fingerprint=_invoice_funding_fingerprint(payload),
        )

    @staticmethod
    def apply_invoice_fully(
        db: Session,
        invoice: Invoice,
        *,
        preview_fingerprint: str,
    ) -> AccountCreditApplicationResult:
        """Apply exact payment-backed credit only when it covers the invoice."""

        return AccountCreditApplications._apply_invoice_payment_sources(
            db,
            invoice,
            preview_fingerprint=preview_fingerprint,
            require_full_funding=True,
        )

    @staticmethod
    def apply_invoice_available(
        db: Session,
        invoice: Invoice,
        *,
        preview_fingerprint: str,
    ) -> AccountCreditApplicationResult:
        """Apply all exact payment-backed credit before another typed source."""

        return AccountCreditApplications._apply_invoice_payment_sources(
            db,
            invoice,
            preview_fingerprint=preview_fingerprint,
            require_full_funding=False,
        )

    @staticmethod
    def _apply_invoice_payment_sources(
        db: Session,
        invoice: Invoice,
        *,
        preview_fingerprint: str,
        require_full_funding: bool,
    ) -> AccountCreditApplicationResult:
        lock_account(db, str(invoice.account_id))
        db.refresh(invoice)
        preview = AccountCreditApplications.preview_invoice_funding(db, invoice)
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
            for payment, room in _source_payments(db, str(invoice.account_id))
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


__all__ = [
    "AccountCreditApplicationError",
    "AccountCreditInvoiceFundingPreview",
    "AccountCreditApplicationResult",
    "AccountCreditApplications",
    "AccountCreditInvariantViolation",
    "ELIGIBLE_INVOICE_STATUSES",
    "eligible_invoices",
]
