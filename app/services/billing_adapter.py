"""Billing boundary for core services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus, PaymentStatus
from app.schemas.billing import (
    InvoiceCreate,
    InvoiceLineCreate,
    PaymentAllocationApply,
    PaymentCreate,
)
from app.services import display_format
from app.services.adapters import adapter_registry


@dataclass(frozen=True)
class InvoiceIntent:
    account_id: UUID
    invoice_number: str | None = None
    currency: str | None = None
    total: Decimal = Decimal("0.00")
    memo: str | None = None
    status: InvoiceStatus = InvoiceStatus.draft
    issued_at: datetime | None = None
    due_at: datetime | None = None


@dataclass(frozen=True)
class InvoiceLineIntent:
    description: str
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0.00")
    tax_rate_id: UUID | None = None


@dataclass(frozen=True)
class PaymentIntent:
    account_id: UUID
    amount: Decimal
    currency: str | None = None
    provider_id: UUID | None = None
    external_id: str | None = None
    memo: str | None = None
    status: PaymentStatus = PaymentStatus.pending
    allocations: list[PaymentAllocationApply] = field(default_factory=list)


class BillingAdapter:
    """Adapter around invoices and payments."""

    name = "billing"
    depends_on: tuple[str, ...] = ("db.session.sqlalchemy",)

    def __init__(self, billing_service: Any | None = None) -> None:
        self._billing_service = billing_service

    def _service(self):
        if self._billing_service is not None:
            return self._billing_service
        from app.services import billing as billing_service

        return billing_service

    def _default_currency(self, db: Session | None) -> str:
        if db is not None:
            return display_format.default_currency(db)
        return "NGN"

    def _currency(self, db: Session | None, currency: str | None) -> str:
        code = str(currency or "").strip().upper()
        return code or self._default_currency(db)

    def create_invoice(self, db: Session, intent: InvoiceIntent):
        billing_service = self._service()

        payload = InvoiceCreate(
            account_id=intent.account_id,
            invoice_number=intent.invoice_number,
            currency=self._currency(db, intent.currency),
            subtotal=intent.total,
            total=intent.total,
            balance_due=intent.total,
            status=intent.status,
            memo=intent.memo,
            issued_at=intent.issued_at,
            due_at=intent.due_at,
        )
        return billing_service.invoices.create(db, payload)

    def create_invoice_with_lines(
        self,
        db: Session,
        intent: InvoiceIntent,
        lines: list[InvoiceLineIntent],
        *,
        commit: bool = True,
    ):
        billing_service = self._service()
        payload = InvoiceCreate(
            account_id=intent.account_id,
            invoice_number=intent.invoice_number,
            currency=self._currency(db, intent.currency),
            subtotal=intent.total,
            total=intent.total,
            balance_due=intent.total,
            status=intent.status,
            memo=intent.memo,
            issued_at=intent.issued_at,
            due_at=intent.due_at,
        )
        line_payloads = tuple(
            InvoiceLineCreate(
                invoice_id=UUID(int=0),
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                tax_rate_id=line.tax_rate_id,
            )
            for line in lines
        )
        return billing_service.invoices.create_with_lines(
            db,
            payload,
            line_payloads,
            commit=commit,
        )

    def record_payment(self, db: Session, intent: PaymentIntent):
        billing_service = self._service()

        payload = PaymentCreate(
            account_id=intent.account_id,
            provider_id=intent.provider_id,
            amount=intent.amount,
            currency=self._currency(db, intent.currency),
            external_id=intent.external_id,
            status=intent.status,
            memo=intent.memo,
            allocations=list(intent.allocations),
        )
        return billing_service.payments.create(db, payload)


billing_adapter = BillingAdapter()
adapter_registry.register(billing_adapter)
