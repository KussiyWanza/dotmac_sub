"""Native sales-order schemas ported from CRM.

Ported from ``dotmac_crm/app/schemas/sales_order.py`` with native ownership
deltas: ``person_id`` becomes ``subscriber_id``, and the legacy
``account_id``/``invoice_id`` fields (removed from the CRM model long ago,
and the source of the crm#233 positional mis-plumb in the list API) are
dropped — this is the FIXED shape.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.sales import (
    QuoteDiscountType,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.sales_order_waiver import WaiverState


class SalesOrderBase(BaseModel):
    subscriber_id: UUID
    quote_id: UUID | None = None
    # Native SystemUser UUID; historical imported CRM owner UUIDs remain readable.
    owner_agent_id: UUID | None = None
    source: str | None = Field(default=None, max_length=80)
    order_number: str | None = Field(default=None, max_length=80)
    status: SalesOrderStatus = SalesOrderStatus.draft
    payment_status: SalesOrderPaymentStatus = SalesOrderPaymentStatus.pending
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0)
    total: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount_paid: Decimal = Field(default=Decimal("0.00"), ge=0)
    balance_due: Decimal = Field(default=Decimal("0.00"), ge=0)
    payment_due_date: datetime | None = None
    paid_at: datetime | None = None
    deposit_required: bool = False
    deposit_paid: bool = False
    contract_signed: bool = False
    signed_at: datetime | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True


class SalesOrderCreate(SalesOrderBase):
    pass


class SalesOrderUpdate(BaseModel):
    subscriber_id: UUID | None = None
    quote_id: UUID | None = None
    owner_agent_id: UUID | None = None
    source: str | None = Field(default=None, max_length=80)
    order_number: str | None = Field(default=None, max_length=80)
    status: SalesOrderStatus | None = None
    payment_status: SalesOrderPaymentStatus | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(default=None, ge=0)
    tax_total: Decimal | None = Field(default=None, ge=0)
    total: Decimal | None = Field(default=None, ge=0)
    amount_paid: Decimal | None = Field(default=None, ge=0)
    balance_due: Decimal | None = Field(default=None, ge=0)
    payment_due_date: datetime | None = None
    paid_at: datetime | None = None
    deposit_required: bool | None = None
    deposit_paid: bool | None = None
    contract_signed: bool | None = None
    signed_at: datetime | None = None
    notes: str | None = None
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_status_timestamps(self) -> SalesOrderUpdate:
        fields_set = self.model_fields_set
        if (
            "payment_status" in fields_set
            and self.payment_status == SalesOrderPaymentStatus.paid
            and ("paid_at" not in fields_set or self.paid_at is None)
        ):
            raise ValueError("paid_at is required when payment_status is paid")
        return self


class SalesOrderRead(SalesOrderBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    discount_type: QuoteDiscountType | None = None
    discount_value: Decimal | None = None
    discount_amount: Decimal = Decimal("0.00")
    created_at: datetime
    updated_at: datetime


class SalesOrderLineBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    sales_order_id: UUID
    # CRM inventory UUID carried verbatim while inventory remains external.
    inventory_item_id: UUID | None = None
    description: str = Field(min_length=1, max_length=255)
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool = True


class SalesOrderLineCreate(SalesOrderLineBase):
    pass


class SalesOrderLineUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    sales_order_id: UUID | None = None
    inventory_item_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    metadata_: dict | None = Field(default=None, serialization_alias="metadata")
    is_active: bool | None = None


class SalesOrderLineRead(SalesOrderLineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SalesOrderWaiverGrant(BaseModel):
    """Grant a waiver on one order.

    Carries no payment field. A waiver states that an amount will not be
    pursued; it never asserts that money arrived and can never reach ``paid``.
    """

    waived_amount: Decimal = Field(gt=0)
    reason_code: str = Field(max_length=80)
    reason_text: str | None = None
    #: A retried request with the same key and inputs returns the original
    #: waiver; the same key with different inputs is a conflict.
    idempotency_key: str = Field(min_length=8, max_length=255)


class SalesOrderWaiverRevoke(BaseModel):
    """Withdraw an active waiver. The order becomes pursuable again."""

    reason_code: str = Field(max_length=80)
    reason_text: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=255)


class SalesOrderWaiverRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sales_order_id: UUID
    state: WaiverState
    waived_amount: Decimal
    currency: str
    reason_code: str
    reason_text: str | None
    granted_by: str
    granted_at: datetime
    revoked_by: str | None
    revoked_at: datetime | None
    revoke_reason_code: str | None
