"""Self-serve quote deposit collection via the existing billing surface.

The install deposit is collected by reusing the customer's normal invoice + pay
flow — so every configured provider (Paystack / Flutterwave / bank transfer /
saved card) works with no bespoke deposit gateway. A deposit Invoice is raised
for the quote, paid via ``create_invoice_payment_intent`` +
``verify_and_record_payment``, and on settlement the quote is accepted — which
records the deposit and triggers the sales order + install project.

The native quote-acceptance cutover runs behind the
``quotes_native_write_enabled`` flag (projects domain, default OFF):

* OFF — write-through to the CRM (``quotes_mirror.accept_quote``), unchanged.
* ON  — native accept (``sales.selfserve.accept_with_deposit``): the quote is
  accepted in sub's own ``quotes`` table, firing the native sales-order
  pipeline. The mirror row is upserted from the native payload afterwards so
  mirror-based reads (``/me/quotes`` and the web portal) and
  ``initiate_deposit``'s dedup check stay coherent during the transition
  window; that write-back retires with the mirror after native-read verification.

Billing-safety invariant (risk #2): on either path the sole ledger event per
deposit is ``verify_and_record_payment`` on the deposit invoice; the accept
only marks the sales order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceDiscountSource,
    InvoiceDiscountType,
    InvoiceStatus,
    TopupIntent,
)
from app.models.quote_mirror import QuoteMirror
from app.models.sales import Quote, QuoteDepositInvoiceLink, QuoteStatus
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services import customer_portal_flow_payments as payments
from app.services import invoice_discounts, quotes_mirror
from app.services.common import coerce_uuid, round_money
from app.services.customer_context import CustomerContext
from app.services.domain_errors import DomainError
from app.services.payment_gateway_adapter import payment_gateway_adapter
from app.services.payment_routing import gateway_options, select_checkout_provider
from app.services.sales import selfserve

logger = logging.getLogger(__name__)


class QuoteDepositError(DomainError):
    """Transport-neutral failure from quotation deposit eligibility or checkout."""


@dataclass(frozen=True, slots=True)
class QuotePaymentQuery:
    quote_id: UUID
    authorized_subscriber_ids: tuple[UUID, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class QuotePaymentPage:
    quote_id: UUID
    subscriber_id: UUID
    status: QuoteStatus
    currency: str
    payable_amount: Decimal
    expires_at: datetime | None
    provider_type: str


@dataclass(frozen=True, slots=True)
class InitiateQuoteDepositCommand:
    quote_id: UUID
    idempotency_key: str
    redirect_url: str


@dataclass(frozen=True, slots=True)
class QuoteCheckoutMetadata:
    payment_flow: str
    invoice_id: UUID
    invoice_number: str
    account_id: UUID
    provider_id: UUID

    def to_response(self) -> dict[str, str]:
        return {
            "payment_flow": self.payment_flow,
            "invoice_id": str(self.invoice_id),
            "invoice_number": self.invoice_number,
            "account_id": str(self.account_id),
            "provider_id": str(self.provider_id),
        }


@dataclass(frozen=True, slots=True)
class QuoteDepositIntentOutcome:
    invoice_id: UUID
    quote_id: UUID
    amount: Decimal
    currency: str
    provider_type: str
    provider_public_key: str
    payment_reference: str
    checkout_metadata: QuoteCheckoutMetadata
    checkout_url: str | None
    customer_email: str
    charged: bool
    replayed: bool

    def to_response(self) -> dict[str, object]:
        return {
            "invoice_id": str(self.invoice_id),
            "quote_id": str(self.quote_id),
            "amount": str(self.amount),
            "currency": self.currency,
            "provider_type": self.provider_type,
            "provider_public_key": self.provider_public_key,
            "reference": self.payment_reference,
            "payment_reference": self.payment_reference,
            "checkout_metadata": self.checkout_metadata.to_response(),
            "checkout_url": self.checkout_url,
            "customer_email": self.customer_email,
            "charged": self.charged,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class VerifyQuoteDepositCommand:
    quote_id: UUID
    reference: str


@dataclass(frozen=True, slots=True)
class QuoteDepositVerificationOutcome:
    quote_id: UUID
    reference: str
    paid: bool


def _error(suffix: str, message: str, **details: object) -> QuoteDepositError:
    return QuoteDepositError(
        code=f"sales.quote_deposits.{suffix}",
        message=message,
        details=details,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _authoritative_quote_deposit_amount(db: Session, quote: Quote) -> Decimal:
    payload = selfserve.build_portal_quote_payload(db, quote)
    if bool(payload.get("deposit_paid")):
        raise _error("already_paid", "This Quote deposit is already paid")
    return Decimal(str(payload.get("deposit_amount") or "0")).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class QuoteDepositInvoiceAmounts:
    original_subtotal: Decimal
    tax_total: Decimal
    original_total: Decimal
    discount: invoice_discounts.InvoiceDiscountInput | None


def _quote_deposit_invoice_amounts(
    quote: Quote, deposit: Decimal
) -> QuoteDepositInvoiceAmounts:
    """Split a discounted Quote deposit without changing its payable amount."""

    quote_total = round_money(quote.total)
    discount_amount = round_money(quote.discount_amount or 0)
    if (
        quote_total <= 0
        or discount_amount <= 0
        or not quote.discount_type
        or quote.discount_applied_by_system_user_id is None
    ):
        return QuoteDepositInvoiceAmounts(
            original_subtotal=deposit,
            tax_total=Decimal("0.00"),
            original_total=deposit,
            discount=None,
        )
    scale = deposit / quote_total
    original_subtotal = round_money(Decimal(quote.subtotal or 0) * scale)
    inherited_amount = round_money(discount_amount * scale)
    discounted_subtotal = round_money(original_subtotal - inherited_amount)
    tax_total = round_money(deposit - discounted_subtotal)
    if tax_total < 0:
        raise invoice_discounts.quote_inheritance_error(
            "The Quote discount cannot be represented on its deposit Invoice."
        )
    discount_type = InvoiceDiscountType(quote.discount_type)
    value = (
        round_money(quote.discount_value or 0)
        if discount_type is InvoiceDiscountType.percentage
        else inherited_amount
    )
    return QuoteDepositInvoiceAmounts(
        original_subtotal=original_subtotal,
        tax_total=tax_total,
        original_total=round_money(original_subtotal + tax_total),
        discount=invoice_discounts.InvoiceDiscountInput(
            discount_type=discount_type,
            value=value,
            reason=quote.discount_reason,
        ),
    )


def _stage_inherited_quote_discount(
    db: Session,
    *,
    quote: Quote,
    invoice: Invoice,
    amounts: QuoteDepositInvoiceAmounts,
) -> None:
    if amounts.discount is None or quote.discount_applied_by_system_user_id is None:
        return
    # Allocate account credit against the already-discounted deposit first.
    # Restore the proportional original basis only inside this same creation
    # transaction, immediately before the discount participant recalculates
    # back to the exact same payable amount.
    invoice.subtotal = amounts.original_subtotal
    invoice.tax_total = amounts.tax_total
    invoice.total = amounts.original_total
    invoice_discounts.stage_invoice_discount(
        db,
        invoice,
        invoice_discounts.StageInvoiceDiscountCommand(
            invoice_id=invoice.id,
            actor_system_user_id=quote.discount_applied_by_system_user_id,
            command_id=uuid5(
                NAMESPACE_URL, f"invoice-quote-discount:{quote.id}:{invoice.id}"
            ),
            discount=amounts.discount,
            source=InvoiceDiscountSource.quote,
            source_quote_id=quote.id,
            applied_at=datetime.now(UTC),
        ),
    )


def quote_payment_page(db: Session, query: QuotePaymentQuery) -> QuotePaymentPage:
    """Resolve authorization, eligibility, provider, and authoritative amount."""

    quote = db.get(Quote, query.quote_id)
    if (
        quote is None
        or not quote.is_active
        or quote.subscriber_id is None
        or quote.subscriber_id not in set(query.authorized_subscriber_ids)
    ):
        raise _error("quote_not_found", "Quote not found")
    try:
        status = QuoteStatus(quote.status)
    except ValueError as exc:
        raise _error(
            "status_ineligible", "This Quote is not eligible for payment"
        ) from exc
    if status not in {QuoteStatus.draft, QuoteStatus.sent}:
        raise _error(
            "status_ineligible",
            "This Quote is not eligible for payment",
            status=status.value,
        )
    if quote.expires_at is not None and _as_utc(quote.expires_at) <= _as_utc(
        query.observed_at
    ):
        raise _error("quote_expired", "This Quote has expired")
    if _native_deposit_invoice_paid(db, quote.id):
        raise _error("already_paid", "This Quote deposit is already paid")
    amount = _authoritative_quote_deposit_amount(db, quote)
    if amount <= Decimal("0.00"):
        raise _error("amount_unavailable", "This Quote has no deposit due")
    if not any(
        option.provider_type.value == "paystack" for option in gateway_options(db)
    ):
        raise _error(
            "paystack_unavailable",
            "Paystack payment is unavailable for this Quote",
        )
    return QuotePaymentPage(
        quote_id=quote.id,
        subscriber_id=quote.subscriber_id,
        status=status,
        currency=quote.currency,
        payable_amount=amount,
        expires_at=quote.expires_at,
        provider_type="paystack",
    )


def _native_write_enabled(db: Session) -> bool:
    """Select native quote writes or CRM write-through
    (delegates to the canonical helper next to its read twin)."""
    return selfserve.native_write_enabled(db)


def _quote_row(db: Session, subscriber_id: str, quote_id: str) -> QuoteMirror:
    sub_uuid = coerce_uuid(str(subscriber_id))
    row = db.scalar(
        select(QuoteMirror).where(
            QuoteMirror.crm_quote_id == str(quote_id),
            QuoteMirror.subscriber_id == sub_uuid,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    return row


def _native_deposit_invoice_paid(db: Session, quote_id: UUID) -> bool:
    """Read paid state through the structural Quote-to-Invoice identity."""

    return (
        db.scalar(
            select(Invoice.id)
            .join(
                QuoteDepositInvoiceLink,
                QuoteDepositInvoiceLink.invoice_id == Invoice.id,
            )
            .where(
                QuoteDepositInvoiceLink.quote_id == quote_id,
                Invoice.status == InvoiceStatus.paid,
            )
        )
        is not None
    )


def _existing_payable_deposit_invoice(
    db: Session, *, subscriber_id: UUID, quote_id: UUID
) -> Invoice | None:
    invoices = tuple(
        db.scalars(
            select(Invoice)
            .join(
                QuoteDepositInvoiceLink,
                QuoteDepositInvoiceLink.invoice_id == Invoice.id,
            )
            .where(
                QuoteDepositInvoiceLink.quote_id == quote_id,
                QuoteDepositInvoiceLink.account_id == subscriber_id,
                Invoice.account_id == subscriber_id,
                Invoice.status.in_(
                    (
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    )
                ),
            )
            .order_by(Invoice.created_at.desc(), Invoice.id.desc())
            .limit(2)
        ).all()
    )
    if len(invoices) > 1:
        raise _error(
            "invoice_ambiguous",
            "More than one payable deposit Invoice is linked to this Quote",
            quote_id=str(quote_id),
        )
    return invoices[0] if invoices else None


def _legacy_existing_payable_deposit_invoice(
    db: Session, *, subscriber_id: UUID, quote_id: str
) -> Invoice | None:
    """Compatibility read for CRM-only Quotes without a native structural row."""

    return db.scalars(
        select(Invoice)
        .where(
            Invoice.account_id == subscriber_id,
            Invoice.metadata_["payment_flow"].as_string() == "quote_deposit",
            Invoice.metadata_["quote_id"].as_string() == str(quote_id),
            Invoice.status.in_(
                (
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                )
            ),
        )
        .order_by(Invoice.created_at.desc(), Invoice.id.desc())
    ).first()


def _native_quote_for_reference(
    db: Session, *, subscriber_id: UUID, quote_id: str
) -> Quote | None:
    try:
        native_id = UUID(str(quote_id))
    except ValueError:
        return None
    quote = db.get(Quote, native_id)
    if quote is None or quote.subscriber_id != subscriber_id:
        return None
    return quote


def _stage_quote_deposit_invoice_link(
    db: Session, *, quote: Quote, invoice: Invoice
) -> QuoteDepositInvoiceLink:
    if quote.subscriber_id is None or invoice.account_id != quote.subscriber_id:
        raise _error(
            "invoice_identity_mismatch",
            "The deposit Invoice does not belong to the Quote customer",
            quote_id=str(quote.id),
            invoice_id=str(invoice.id),
        )
    existing = db.scalar(
        select(QuoteDepositInvoiceLink).where(
            QuoteDepositInvoiceLink.invoice_id == invoice.id
        )
    )
    if existing is not None:
        if existing.quote_id != quote.id or existing.account_id != quote.subscriber_id:
            raise _error(
                "invoice_identity_mismatch",
                "The deposit Invoice is already linked to another Quote",
                invoice_id=str(invoice.id),
            )
        return existing
    link = QuoteDepositInvoiceLink(
        quote_id=quote.id,
        invoice_id=invoice.id,
        account_id=quote.subscriber_id,
    )
    db.add(link)
    db.flush()
    return link


def initiate_deposit(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    provider: str | None = None,
    redirect_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Raise a deposit invoice for the quote and start its payment checkout.

    Quote resolution and the already-paid guard are native or mirror per the
    ``quotes_native_write_enabled`` flag (module docstring). The two paths use
    different id namespaces: native ``quote_id`` is the Quote UUID, mirror is
    ``crm_quote_id``.
    """
    if _native_write_enabled(db):
        return _initiate_deposit_native(
            db,
            customer,
            subscriber_id,
            quote_id,
            provider=provider,
            redirect_url=redirect_url,
            idempotency_key=idempotency_key,
        )
    row = _quote_row(db, subscriber_id, quote_id)
    if row.deposit_paid:
        raise HTTPException(status_code=409, detail="Deposit already paid")
    deposit = Decimal(str(row.deposit_amount or "0"))
    if deposit <= 0:
        raise HTTPException(status_code=400, detail="This quote has no deposit due")

    sub_uuid = coerce_uuid(str(subscriber_id))
    native_quote = _native_quote_for_reference(
        db,
        subscriber_id=sub_uuid,
        quote_id=quote_id,
    )
    invoice = (
        _existing_payable_deposit_invoice(
            db,
            subscriber_id=sub_uuid,
            quote_id=native_quote.id,
        )
        if native_quote is not None
        else _legacy_existing_payable_deposit_invoice(
            db,
            subscriber_id=sub_uuid,
            quote_id=str(quote_id),
        )
    )
    if invoice is None:
        invoice_amounts = (
            _quote_deposit_invoice_amounts(native_quote, deposit)
            if native_quote is not None
            else QuoteDepositInvoiceAmounts(
                original_subtotal=deposit,
                tax_total=Decimal("0.00"),
                original_total=deposit,
                discount=None,
            )
        )
        invoice = billing_service.invoices.create(
            db,
            InvoiceCreate(
                account_id=sub_uuid,
                status=InvoiceStatus.issued,
                currency=row.currency or "NGN",
                subtotal=deposit,
                tax_total=Decimal("0.00"),
                total=deposit,
                balance_due=deposit,
                issued_at=datetime.now(UTC),
                memo=f"Installation deposit · quote {quote_id}",
            ),
            commit=False,
        )
        # Trace the deposit back to its quote for reconciliation/audit.
        invoice.metadata_ = {
            "quote_id": str(quote_id),
            "payment_flow": "quote_deposit",
        }
        if native_quote is not None:
            _stage_inherited_quote_discount(
                db,
                quote=native_quote,
                invoice=invoice,
                amounts=invoice_amounts,
            )
            _stage_quote_deposit_invoice_link(
                db,
                quote=native_quote,
                invoice=invoice,
            )
        db.commit()

    try:
        intent = payments.create_invoice_payment_intent(
            db,
            customer,
            str(invoice.id),
            provider=provider,
            redirect_url=redirect_url,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "invoice_id": str(invoice.id),
        "quote_id": str(quote_id),
        "amount": str(deposit),
        "currency": intent.get("currency", row.currency or "NGN"),
        "provider_type": intent.get("provider_type"),
        "provider_public_key": intent.get("provider_public_key"),
        "payment_reference": intent.get("reference"),
        "checkout_metadata": intent.get("checkout_metadata"),
        "checkout_url": intent.get("checkout_url"),
        "customer_email": intent.get("customer_email"),
        "charged": bool(intent.get("charged")),
    }


def _initiate_deposit_native(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    provider: str | None = None,
    redirect_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Native-path deposit initiation: resolve the quote in sub's own table
    (UUID id namespace) and gate "already paid" on the paid deposit Invoice in
    the ledger — the mirror's ``deposit_paid`` flag plays no part (risk #2:
    a stale mirror must never allow a second charge)."""
    quote = selfserve.selfserve_quotes.get_for_subscriber(db, subscriber_id, quote_id)
    if _native_deposit_invoice_paid(db, quote.id):
        raise HTTPException(status_code=409, detail="Deposit already paid")
    payload = selfserve.build_portal_quote_payload(db, quote)
    deposit = Decimal(str(payload.get("deposit_amount") or "0"))
    if deposit <= 0:
        raise HTTPException(status_code=400, detail="This quote has no deposit due")

    sub_uuid = coerce_uuid(str(subscriber_id))
    invoice = _existing_payable_deposit_invoice(
        db, subscriber_id=sub_uuid, quote_id=quote.id
    )
    if invoice is None:
        invoice_amounts = _quote_deposit_invoice_amounts(quote, deposit)
        invoice = billing_service.invoices.create(
            db,
            InvoiceCreate(
                account_id=sub_uuid,
                status=InvoiceStatus.issued,
                currency=quote.currency or "NGN",
                subtotal=deposit,
                tax_total=Decimal("0.00"),
                total=deposit,
                balance_due=deposit,
                issued_at=datetime.now(UTC),
                memo=f"Installation deposit · quote {quote.id}",
            ),
            commit=False,
        )
        # Trace the deposit back to its quote for reconciliation/audit — and for
        # _native_deposit_invoice_paid, which keys on exactly these two fields.
        invoice.metadata_ = {
            "quote_id": str(quote.id),
            "payment_flow": "quote_deposit",
        }
        _stage_inherited_quote_discount(
            db,
            quote=quote,
            invoice=invoice,
            amounts=invoice_amounts,
        )
        _stage_quote_deposit_invoice_link(db, quote=quote, invoice=invoice)
        db.commit()

    try:
        intent = payments.create_invoice_payment_intent(
            db,
            customer,
            str(invoice.id),
            provider=provider,
            redirect_url=redirect_url,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "invoice_id": str(invoice.id),
        "quote_id": str(quote.id),
        "amount": str(deposit),
        "currency": intent.get("currency", quote.currency or "NGN"),
        "provider_type": intent.get("provider_type"),
        "provider_public_key": intent.get("provider_public_key"),
        "payment_reference": intent.get("reference"),
        "checkout_metadata": intent.get("checkout_metadata"),
        "checkout_url": intent.get("checkout_url"),
        "customer_email": intent.get("customer_email"),
        "charged": bool(intent.get("charged")),
    }


def _authorized_subscriber_ids(customer: CustomerContext) -> tuple[UUID, ...]:
    values: list[UUID] = []
    for value in customer.allowed_subscriber_ids:
        try:
            values.append(UUID(value))
        except ValueError:
            continue
    return tuple(values)


def _pending_quote_intent(
    db: Session,
    *,
    quote_id: UUID,
    subscriber_id: UUID,
    observed_at: datetime,
) -> tuple[Invoice, TopupIntent] | None:
    invoice = _existing_payable_deposit_invoice(
        db,
        subscriber_id=subscriber_id,
        quote_id=quote_id,
    )
    if invoice is None:
        return None
    intent = db.scalars(
        select(TopupIntent)
        .where(
            TopupIntent.account_id == subscriber_id,
            TopupIntent.provider_type == "paystack",
            TopupIntent.status == "pending",
            TopupIntent.invoice_id == invoice.id,
            TopupIntent.expires_at > _as_utc(observed_at),
        )
        .order_by(TopupIntent.created_at.desc(), TopupIntent.id.desc())
    ).first()
    return (invoice, intent) if intent is not None else None


def _typed_intent_outcome(
    payload: dict,
    *,
    account_id: UUID,
    replayed: bool,
) -> QuoteDepositIntentOutcome:
    try:
        provider_type = str(payload.get("provider_type") or "")
        provider_public_key = str(payload.get("provider_public_key") or "")
        payment_reference = str(payload.get("payment_reference") or "")
        customer_email = str(payload.get("customer_email") or "")
        checkout_payload = payload.get("checkout_metadata")
        if not isinstance(checkout_payload, dict):
            raise ValueError("missing checkout metadata")
        if provider_type != "paystack" or not all(
            (provider_public_key, payment_reference, customer_email)
        ):
            raise ValueError("incomplete Paystack intent")
        invoice_id = UUID(str(payload["invoice_id"]))
        return QuoteDepositIntentOutcome(
            invoice_id=invoice_id,
            quote_id=UUID(str(payload["quote_id"])),
            amount=Decimal(str(payload["amount"])).quantize(Decimal("0.01")),
            currency=str(payload["currency"]),
            provider_type=provider_type,
            provider_public_key=provider_public_key,
            payment_reference=payment_reference,
            checkout_metadata=QuoteCheckoutMetadata(
                payment_flow="invoice_payment",
                invoice_id=invoice_id,
                invoice_number=str(checkout_payload.get("invoice_number") or ""),
                account_id=account_id,
                provider_id=UUID(str(checkout_payload["provider_id"])),
            ),
            checkout_url=(
                str(payload["checkout_url"])
                if payload.get("checkout_url") is not None
                else None
            ),
            customer_email=customer_email,
            charged=bool(payload.get("charged")),
            replayed=replayed,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            "intent_incomplete", "Paystack did not return a complete checkout intent"
        ) from exc


def initiate_quote_deposit(
    db: Session,
    customer: CustomerContext,
    command: InitiateQuoteDepositCommand,
) -> QuoteDepositIntentOutcome:
    """Recheck eligibility and delegate mutation to the established deposit flow."""

    key = command.idempotency_key.strip()
    if not 16 <= len(key) <= 120:
        raise _error(
            "idempotency_key_invalid",
            "Quote payment idempotency evidence is required",
        )
    authorized_ids = _authorized_subscriber_ids(customer)
    if customer.read_only or not authorized_ids:
        raise _error("unauthorized", "This customer cannot pay the Quote")
    observed_at = datetime.now(UTC)
    # Serialize invoice selection/creation for this Quote. The established
    # invoice-payment intent owner applies its own idempotency and locking after
    # this Quote lock has protected the quote-to-invoice handoff.
    db.scalar(select(Quote.id).where(Quote.id == command.quote_id).with_for_update())
    page = quote_payment_page(
        db,
        QuotePaymentQuery(
            quote_id=command.quote_id,
            authorized_subscriber_ids=authorized_ids,
            observed_at=observed_at,
        ),
    )
    pending = _pending_quote_intent(
        db,
        quote_id=page.quote_id,
        subscriber_id=page.subscriber_id,
        observed_at=observed_at,
    )
    if pending is not None:
        invoice, intent = pending
        pending_amount = Decimal(str(intent.requested_amount)).quantize(Decimal("0.01"))
        invoice_amount = Decimal(
            str(invoice.balance_due or invoice.total or "0")
        ).quantize(Decimal("0.01"))
        if (
            pending_amount != page.payable_amount
            or invoice_amount != page.payable_amount
            or intent.currency != page.currency
            or invoice.currency != page.currency
        ):
            raise _error(
                "amount_mismatch",
                "The pending payment intent no longer matches the authoritative Quote amount",
            )
        route = select_checkout_provider(db, "paystack")
        gateway_context = payment_gateway_adapter.build_context(
            db,
            provider_type="paystack",
            capability_binding_id=intent.capability_binding_id
            or route.capability_binding_id,
            invoice_number=invoice.invoice_number,
        )
        public_key = str(gateway_context.public_key or "").strip()
        customer_email = payments._resolve_customer_email(
            db, dict(customer.raw)
        ).strip()
        if not public_key or not customer_email:
            raise _error(
                "intent_incomplete",
                "Paystack did not return a complete checkout intent",
            )
        return QuoteDepositIntentOutcome(
            invoice_id=invoice.id,
            quote_id=page.quote_id,
            amount=pending_amount,
            currency=intent.currency,
            provider_type="paystack",
            provider_public_key=public_key,
            payment_reference=intent.reference,
            checkout_metadata=QuoteCheckoutMetadata(
                payment_flow="invoice_payment",
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number or "",
                account_id=page.subscriber_id,
                provider_id=route.provider_id,
            ),
            checkout_url=None,
            customer_email=customer_email,
            charged=False,
            replayed=True,
        )
    try:
        payload = initiate_deposit(
            db,
            dict(customer.raw),
            str(page.subscriber_id),
            str(command.quote_id),
            provider="paystack",
            redirect_url=command.redirect_url,
            idempotency_key=key,
        )
    except HTTPException as exc:
        raise _error(
            "initiation_rejected",
            str(exc.detail),
            status_code=exc.status_code,
        ) from exc
    outcome = _typed_intent_outcome(
        payload,
        account_id=page.subscriber_id,
        replayed=False,
    )
    structural_link = db.scalar(
        select(QuoteDepositInvoiceLink.id).where(
            QuoteDepositInvoiceLink.quote_id == page.quote_id,
            QuoteDepositInvoiceLink.invoice_id == outcome.invoice_id,
            QuoteDepositInvoiceLink.account_id == page.subscriber_id,
        )
    )
    if (
        outcome.quote_id != page.quote_id
        or structural_link is None
        or outcome.amount != page.payable_amount
        or outcome.currency != page.currency
    ):
        raise _error(
            "amount_mismatch",
            "The payment intent did not match the authoritative Quote deposit",
        )
    return outcome


def verify_deposit(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    reference: str,
    provider: str | None = None,
) -> dict:
    """Verify the deposit payment; on full settlement, accept the quote.

    Acceptance is native or CRM write-through per the
    ``quotes_native_write_enabled`` flag (module docstring).
    """
    if _native_write_enabled(db):
        return _verify_deposit_native(
            db,
            customer,
            subscriber_id,
            quote_id,
            reference=reference,
            provider=provider,
        )

    row = _quote_row(db, subscriber_id, quote_id)
    try:
        result = payments.verify_and_record_payment(
            db, customer, reference, provider=provider
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    invoice = result.get("invoice")
    paid = (
        invoice is not None and getattr(invoice, "status", None) == InvoiceStatus.paid
    )
    if not paid:
        # Partial / pending — surface the current quote unchanged; the customer
        # can retry. (Deposits are single full payments, so this is the edge.)
        return {
            "paid": False,
            "quote": quotes_mirror._row_to_item(row),
            "reference": reference,
        }

    amount = str(result.get("amount") or row.deposit_amount or "0")
    quote = quotes_mirror.accept_quote(
        db,
        str(subscriber_id),
        str(quote_id),
        deposit_reference=reference,
        deposit_amount=amount,
        provider=provider,
    )
    return {"paid": True, "quote": quote, "reference": reference}


def _verify_deposit_native(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    reference: str,
    provider: str | None = None,
) -> dict:
    """Native tail (§2.2 step 4): verify the payment, then accept the quote
    in sub's own sales vertical — no CRM hop."""
    quote = selfserve.selfserve_quotes.get_for_subscriber(db, subscriber_id, quote_id)
    try:
        result = payments.verify_and_record_payment(
            db, customer, reference, provider=provider
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    invoice = result.get("invoice")
    paid = (
        invoice is not None and getattr(invoice, "status", None) == InvoiceStatus.paid
    )
    if not paid:
        return {
            "paid": False,
            "quote": selfserve.build_portal_quote_payload(db, quote),
            "reference": reference,
        }

    current = selfserve.build_portal_quote_payload(db, quote)
    amount = str(result.get("amount") or current.get("deposit_amount") or "0")
    payload = selfserve.selfserve_quotes.accept_with_deposit(
        db,
        str(subscriber_id),
        str(quote_id),
        deposit_reference=reference,
        deposit_amount=amount,
        provider=provider,
    )
    _sync_mirror_after_native_accept(db, subscriber_id, payload)
    return {"paid": True, "quote": payload, "reference": reference}


def _sync_mirror_after_native_accept(
    db: Session, subscriber_id: str, payload: dict
) -> None:
    """Transitional: reflect the native accept into the quote mirror so
    mirror-based reads and ``initiate_deposit``'s already-paid check stay
    coherent until native reads are verified and the mirror retires. Best-effort."""
    try:
        sub_uuid = coerce_uuid(str(subscriber_id))
        quotes_mirror._upsert_row(db, subscriber_id=sub_uuid, item=payload)
        db.commit()
    except Exception:  # pragma: no cover - defensive
        db.rollback()
        logger.warning(
            "quote_mirror_sync_after_native_accept_failed quote_id=%s",
            payload.get("id"),
            exc_info=True,
        )


def verify_quote_deposit(
    db: Session,
    customer: CustomerContext,
    command: VerifyQuoteDepositCommand,
) -> QuoteDepositVerificationOutcome:
    """Authorize the return and preserve the canonical verification lifecycle."""

    reference = command.reference.strip()
    if not reference:
        raise _error("reference_required", "Payment reference is required")
    authorized_ids = _authorized_subscriber_ids(customer)
    quote = db.get(Quote, command.quote_id)
    if (
        quote is None
        or not quote.is_active
        or quote.subscriber_id is None
        or quote.subscriber_id not in set(authorized_ids)
    ):
        raise _error("quote_not_found", "Quote not found")
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).one_or_none()
    invoice_id = intent.invoice_id if intent is not None else None
    invoice = db.get(Invoice, invoice_id) if invoice_id is not None else None
    structural_link = (
        db.scalar(
            select(QuoteDepositInvoiceLink.id).where(
                QuoteDepositInvoiceLink.quote_id == quote.id,
                QuoteDepositInvoiceLink.invoice_id == invoice_id,
                QuoteDepositInvoiceLink.account_id == quote.subscriber_id,
            )
        )
        if invoice_id is not None
        else None
    )
    if (
        intent is None
        or intent.account_id != quote.subscriber_id
        or intent.provider_type != "paystack"
        or intent.expires_at is None
        or _as_utc(intent.expires_at) <= datetime.now(UTC)
        or invoice is None
        or invoice.account_id != quote.subscriber_id
        or structural_link is None
    ):
        raise _error(
            "reference_mismatch",
            "Payment reference was not issued for this Quote",
        )
    try:
        payload = verify_deposit(
            db,
            dict(customer.raw),
            str(quote.subscriber_id),
            str(command.quote_id),
            reference=reference,
            provider="paystack",
        )
    except HTTPException as exc:
        raise _error(
            "verification_rejected",
            str(exc.detail),
            status_code=exc.status_code,
        ) from exc
    return QuoteDepositVerificationOutcome(
        quote_id=command.quote_id,
        reference=reference,
        paid=bool(payload.get("paid")),
    )
