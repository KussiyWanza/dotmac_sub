from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.billing import (
    BankAccountType,
    BillingRunStatus,
    CollectionAccountType,
    CreditNoteStatus,
    InvoiceClosureOrigin,
    InvoiceClosureType,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntryType,
    LedgerSource,
    PaymentChannelType,
    PaymentMethodType,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentRefundOrigin,
    PaymentReversalOrigin,
    PaymentSettlementOrigin,
    PaymentStatus,
    TaxApplication,
)
from app.models.catalog import BillingCycle
from app.schemas.status_presentation import StatusPresentation


class InvoiceBase(BaseModel):
    account_id: UUID
    invoice_number: str | None = Field(default=None, max_length=80)
    status: InvoiceStatus = InvoiceStatus.draft
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    # Money fields are UNBOUNDED on the shared base so response (*Read) models
    # can serialize stored signed/zero values. The create-side ge=0 bounds live
    # on *Create only. See docs/audit-read-model-constraints.md (and #560).
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    balance_due: Decimal = Decimal("0.00")
    billing_period_start: datetime | None = None
    billing_period_end: datetime | None = None
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None
    is_proforma: bool = False
    is_active: bool = True


class InvoiceCreate(InvoiceBase):
    # Create-side money bounds (kept off the shared base — see InvoiceBase).
    subtotal: Decimal = Field(default=Decimal("0.00"), ge=0)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0)
    total: Decimal = Field(default=Decimal("0.00"), ge=0)
    balance_due: Decimal = Field(default=Decimal("0.00"), ge=0)


class InvoiceUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_number: str | None = Field(default=None, max_length=80)
    status: InvoiceStatus | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(default=None, ge=0)
    tax_total: Decimal | None = Field(default=None, ge=0)
    total: Decimal | None = Field(default=None, ge=0)
    balance_due: Decimal | None = Field(default=None, ge=0)
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None
    is_proforma: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _validate_status_timestamps(self) -> InvoiceUpdate:
        fields_set = self.model_fields_set
        if "status" in fields_set and self.status == InvoiceStatus.paid:
            if "paid_at" not in fields_set or self.paid_at is None:
                raise ValueError("paid_at is required when status is paid")
        return self


class InvoiceLineBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    invoice_id: UUID
    subscription_id: UUID | None = None
    description: str = Field(min_length=1, max_length=255)
    # Bounds are create-only (see InvoiceBase); base stays unbounded for *Read.
    quantity: Decimal = Decimal("1.000")
    unit_price: Decimal = Decimal("0.00")
    amount: Decimal | None = None
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive
    # JSONB in the ORM: dict payloads (e.g. subscription line context); str
    # kept for legacy writers that send a pre-encoded JSON string.
    metadata_: dict[str, Any] | str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool = True


class InvoiceLineCreate(InvoiceLineBase):
    # Create-side bounds (kept off the shared base — see InvoiceLineBase).
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)


class SystemInvoiceLineCreate(InvoiceLineCreate):
    """Internal automation request with a stable source-fact identity."""

    billing_line_key: str | None = Field(default=None, max_length=255)


class InvoiceLineUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    invoice_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication | None = None
    # JSONB in the ORM: dict payloads (e.g. subscription line context); str
    # kept for legacy writers that send a pre-encoded JSON string.
    metadata_: dict[str, Any] | str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool | None = None


class InvoiceLineRead(InvoiceLineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # Money/quantity inherit unbounded from InvoiceLineBase (create-only bounds).
    id: UUID
    created_at: datetime
    updated_at: datetime


class InvoiceSyncLineRead(BaseModel):
    """Minimal invoice-line contract used by the ERP bulk sync feed."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None = None
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive


class InvoiceSyncRead(BaseModel):
    """Accounting fields required to mirror an invoice into DotMac ERP.

    Keep this deliberately separate from ``InvoiceRead``. The normal API model
    includes payment allocations and UI/detail fields which make a bulk AR pull
    unnecessarily expensive.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    invoice_number: str | None = None
    status: InvoiceStatus
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    balance_due: Decimal
    issued_at: datetime | None = None
    due_at: datetime | None = None
    paid_at: datetime | None = None
    memo: str | None = None
    is_proforma: bool = False
    updated_at: datetime
    lines: list[InvoiceSyncLineRead] = Field(default_factory=list)


class CreditNoteBase(BaseModel):
    account_id: UUID
    invoice_id: UUID | None = None
    credit_number: str | None = Field(default=None, max_length=80)
    status: CreditNoteStatus = CreditNoteStatus.draft
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    # Bounds are create-only (see InvoiceBase); base stays unbounded for *Read.
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    memo: str | None = None
    is_active: bool = True


class CreditNoteCreate(CreditNoteBase):
    # Create-side bounds (kept off the shared base — see CreditNoteBase).
    subtotal: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)
    total: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)


class CreditNoteUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_id: UUID | None = None
    credit_number: str | None = Field(default=None, max_length=80)
    status: CreditNoteStatus | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    subtotal: Decimal | None = Field(default=None, ge=0, lt=10000000000)
    tax_total: Decimal | None = Field(default=None, ge=0, lt=10000000000)
    total: Decimal | None = Field(default=None, ge=0, lt=10000000000)
    memo: str | None = None
    is_active: bool | None = None


class CreditNoteLineBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    credit_note_id: UUID
    description: str = Field(min_length=1, max_length=255)
    # Bounds are create-only (see InvoiceBase); base stays unbounded for *Read.
    quantity: Decimal = Decimal("1.000")
    unit_price: Decimal = Decimal("0.00")
    amount: Decimal | None = None
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive
    # JSONB in the ORM: dict payloads (e.g. subscription line context); str
    # kept for legacy writers that send a pre-encoded JSON string.
    metadata_: dict[str, Any] | str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool = True


class CreditNoteLineCreate(CreditNoteLineBase):
    # Create-side bounds (kept off the shared base — see CreditNoteLineBase).
    quantity: Decimal = Field(default=Decimal("1.000"), gt=0)
    unit_price: Decimal = Field(default=Decimal("0.00"), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)


class CreditNoteLineUpdate(BaseModel):
    credit_note_id: UUID | None = None
    description: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: Decimal | None = Field(default=None, gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication | None = None
    # JSONB in the ORM: dict payloads (e.g. subscription line context); str
    # kept for legacy writers that send a pre-encoded JSON string.
    metadata_: dict[str, Any] | str | None = Field(
        default=None,
        serialization_alias="metadata",
    )
    is_active: bool | None = None


class CreditNoteLineRead(CreditNoteLineBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    # Money/quantity inherit unbounded from CreditNoteLineBase (create-only).
    id: UUID
    created_at: datetime
    updated_at: datetime


class CreditNoteApplicationBase(BaseModel):
    credit_note_id: UUID
    invoice_id: UUID
    # No *Create subclass exists (creation goes via CreditNoteApplyRequest, which
    # keeps its own gt=0), so this bound only ever reached *Read. Drop it so the
    # read model serializes stored signed amounts.
    amount: Decimal = Decimal("0.00")
    memo: str | None = None


class CreditNoteApplicationRead(CreditNoteApplicationBase):
    model_config = ConfigDict(from_attributes=True)

    # amount inherits unbounded from CreditNoteApplicationBase (no create-side
    # bound exists to drop).
    id: UUID
    ledger_entry_id: UUID | None = None
    consumption_ledger_entry_id: UUID | None = None
    preview_fingerprint: str | None = None
    created_at: datetime
    updated_at: datetime


class CreditNoteApplicationPreviewRequest(BaseModel):
    invoice_id: UUID
    amount: Decimal | None = Field(default=None, gt=0)


class CreditNoteIssuePreviewRequest(BaseModel):
    account_id: UUID
    invoice_id: UUID | None = None
    credit_number: str | None = Field(default=None, max_length=80)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    subtotal: Decimal = Field(ge=0, lt=10000000000)
    tax_total: Decimal = Field(default=Decimal("0.00"), ge=0, lt=10000000000)
    total: Decimal = Field(gt=0, lt=10000000000)
    memo: str | None = None
    line_description: str | None = Field(default=None, min_length=1, max_length=255)
    line_tax_rate_id: UUID | None = None
    line_tax_application: TaxApplication = TaxApplication.exempt


class CreditNoteIssuePreviewRead(BaseModel):
    credit_note_id: UUID | None = None
    account_id: UUID
    invoice_id: UUID | None = None
    credit_number: str | None = None
    currency: str
    credit_total: Decimal
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    invoice_receivable_before: Decimal | None = None
    invoice_receivable_after: Decimal | None = None
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


class CreditNoteIssueRequest(CreditNoteIssuePreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class CreditNoteIssueConfirmation(BaseModel):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class CreditNoteVoidPreviewRead(BaseModel):
    credit_note_id: UUID
    account_id: UUID
    credit_number: str | None = None
    currency: str
    credit_available_before: Decimal
    credit_available_after: Decimal
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    reverses_ledger_entry_id: UUID
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


class CreditNoteVoidRequest(BaseModel):
    memo: str | None = None
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class CreditNoteApplicationPreviewRead(BaseModel):
    credit_note_id: UUID
    credit_number: str | None = None
    invoice_id: UUID
    invoice_number: str | None = None
    account_id: UUID
    currency: str
    credit_available_before: Decimal
    invoice_receivable_before: Decimal
    apply_amount: Decimal
    credit_available_after: Decimal
    invoice_receivable_after: Decimal
    settles_invoice: bool
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    access_consequence: str
    fingerprint: str


class CreditNoteApplyRequest(CreditNoteApplicationPreviewRequest):
    amount: Decimal = Field(gt=0)
    memo: str | None = None
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class PaymentMethodBase(BaseModel):
    # Exactly one owner: customer subscriber (account_id) or reseller org
    # (reseller_id, for subscriber-less reseller_user logins — Layer 3).
    account_id: UUID | None = None
    reseller_id: UUID | None = None
    payment_channel_id: UUID | None = None
    method_type: PaymentMethodType = PaymentMethodType.card
    label: str | None = Field(default=None, max_length=120)
    token: str | None = Field(default=None, max_length=255)
    last4: str | None = Field(default=None, max_length=4)
    brand: str | None = Field(default=None, max_length=40)
    expires_month: int | None = Field(default=None, ge=1, le=12)
    expires_year: int | None = Field(default=None, ge=2000, le=2100)
    is_default: bool = False
    is_active: bool = True


class PaymentMethodCreate(PaymentMethodBase):
    pass


class PaymentMethodUpdate(BaseModel):
    account_id: UUID | None = None
    payment_channel_id: UUID | None = None
    method_type: PaymentMethodType | None = None
    label: str | None = Field(default=None, max_length=120)
    token: str | None = Field(default=None, max_length=255)
    last4: str | None = Field(default=None, max_length=4)
    brand: str | None = Field(default=None, max_length=40)
    expires_month: int | None = Field(default=None, ge=1, le=12)
    expires_year: int | None = Field(default=None, ge=2000, le=2100)
    is_default: bool | None = None
    is_active: bool | None = None


class PaymentMethodRead(PaymentMethodBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class BankAccountBase(BaseModel):
    account_id: UUID
    payment_method_id: UUID | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    account_type: BankAccountType = BankAccountType.checking
    account_last4: str | None = Field(default=None, max_length=4)
    routing_last4: str | None = Field(default=None, max_length=4)
    token: str | None = Field(default=None, max_length=255)
    is_default: bool = False
    is_active: bool = True


class BankAccountCreate(BankAccountBase):
    pass


class BankAccountUpdate(BaseModel):
    account_id: UUID | None = None
    payment_method_id: UUID | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    account_type: BankAccountType | None = None
    account_last4: str | None = Field(default=None, max_length=4)
    routing_last4: str | None = Field(default=None, max_length=4)
    token: str | None = Field(default=None, max_length=255)
    is_default: bool | None = None
    is_active: bool | None = None


class BankAccountRead(BankAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PaymentBase(BaseModel):
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    provider_fee: Decimal = Field(default=Decimal("0.00"), ge=0)
    # Bound is create-only (see InvoiceBase); base stays unbounded for *Read.
    amount: Decimal
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    status: PaymentStatus = PaymentStatus.pending
    paid_at: datetime | None = None
    external_id: str | None = Field(default=None, max_length=120)
    memo: str | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _exactly_one_account_scope(self) -> PaymentBase:
        if (self.account_id is None) == (self.billing_account_id is None):
            raise ValueError(
                "exactly one of account_id or billing_account_id must be set"
            )
        return self


class PaymentCreate(PaymentBase):
    # Create-side bound (kept off the shared base — see PaymentBase).
    amount: Decimal = Field(gt=0, lt=10000000000)
    allocations: list[PaymentAllocationApply] | None = None


class PaymentCreationPreviewRequest(PaymentCreate):
    auto_allocate: bool = True


class PaymentCreationAllocationEffectRead(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    receivable_before: Decimal
    receivable_after: Decimal
    allocation_amount: Decimal
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource


class PaymentCreationPreviewRead(BaseModel):
    account_id: UUID
    amount: Decimal
    currency: str
    status: PaymentStatus
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    allocation_effects: list[PaymentCreationAllocationEffectRead]
    unallocated_amount: Decimal
    unallocated_ledger_entry_type: LedgerEntryType | None = None
    unallocated_ledger_source: LedgerSource | None = None
    access_consequence: str
    fingerprint: str


class PaymentCreationConfirm(PaymentCreationPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class PaymentSettlementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    unallocated_ledger_entry_id: UUID | None = None
    prepaid_ledger_entry_id: UUID | None = None
    billing_account_ledger_entry_id: UUID | None = None
    amount: Decimal
    unallocated_amount: Decimal
    prepaid_amount: Decimal
    currency: str
    origin: PaymentSettlementOrigin
    preview_fingerprint: str | None = None
    idempotency_key: str | None = None
    created_at: datetime


class PaymentSettlementConfirm(BaseModel):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class PaymentSettlementEvidenceCandidateRead(BaseModel):
    ledger_entry_id: UUID
    invoice_id: UUID | None = None
    entry_type: LedgerEntryType
    source: LedgerSource | None = None
    amount: Decimal
    currency: str


class PaymentSettlementEvidenceInspectionRead(BaseModel):
    payment_id: UUID
    payment_amount: Decimal
    currency: str
    already_reconciled: bool
    active_allocation_ids: list[UUID]
    candidate_entries: list[PaymentSettlementEvidenceCandidateRead]


class PaymentSettlementReconciliationRequest(BaseModel):
    allocation_ledger_entry_ids: dict[UUID, UUID]
    unallocated_ledger_entry_id: UUID | None = None
    prepaid_ledger_entry_id: UUID | None = None
    reason: str = Field(min_length=8, max_length=500)


class BillingAccountLedgerEvidenceCandidateRead(BaseModel):
    billing_account_ledger_entry_id: UUID
    entry_type: LedgerEntryType
    source: LedgerSource
    amount: Decimal
    currency: str
    balance_after: Decimal


class BillingAccountPaymentProvenanceCandidateRead(BaseModel):
    provenance_type: str
    provenance_id: UUID
    status: str
    amount: Decimal
    currency: str


class BillingAccountPaymentSettlementEvidenceInspectionRead(BaseModel):
    payment_id: UUID
    billing_account_id: UUID
    payment_state: PaymentStatus
    payment_amount: Decimal
    currency: str
    already_reconciled: bool
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    active_allocation_ids: list[UUID]
    allocation_candidate_entries: list[PaymentSettlementEvidenceCandidateRead]
    billing_account_candidate_entries: list[BillingAccountLedgerEvidenceCandidateRead]
    provenance_candidates: list[BillingAccountPaymentProvenanceCandidateRead]


class BillingAccountPaymentSettlementReconciliationRequest(BaseModel):
    allocation_ledger_entry_ids: dict[UUID, UUID]
    billing_account_ledger_entry_id: UUID | None = None
    provenance_type: str = Field(
        pattern="^(provider_event|payment_proof|topup_intent)$"
    )
    provenance_id: UUID
    reason: str = Field(min_length=8, max_length=500)


class BillingAccountPaymentSettlementAllocationEvidenceRead(BaseModel):
    payment_allocation_id: UUID
    invoice_id: UUID
    account_id: UUID
    amount: Decimal
    ledger_entry_id: UUID


class BillingAccountPaymentSettlementReconciliationPreviewRead(BaseModel):
    payment_id: UUID
    billing_account_id: UUID
    payment_state: PaymentStatus
    payment_amount: Decimal
    currency: str
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    allocated_amount: Decimal
    unallocated_amount: Decimal
    allocation_evidence: list[BillingAccountPaymentSettlementAllocationEvidenceRead]
    billing_account_ledger_entry_id: UUID | None = None
    provenance_type: str
    provenance_id: UUID
    money_posted: bool
    service_access_consequence: str
    fingerprint: str


class BillingAccountPaymentSettlementReconciliationConfirm(
    BillingAccountPaymentSettlementReconciliationRequest
):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class BillingAccountPaymentSettlementReconciliationResultRead(BaseModel):
    settlement: PaymentSettlementRead
    reconciliation_evidence_id: UUID
    provenance_type: str
    provenance_id: UUID
    idempotent_replay: bool = False


class PaymentUpdate(BaseModel):
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, lt=10000000000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: PaymentStatus | None = None
    paid_at: datetime | None = None
    external_id: str | None = Field(default=None, max_length=120)
    memo: str | None = None
    is_active: bool | None = None


class PaymentRead(PaymentBase):
    model_config = ConfigDict(from_attributes=True)

    # amount inherits unbounded from PaymentBase (create-only bound).
    id: UUID
    created_at: datetime
    updated_at: datetime
    # Total refunded so far; gross `amount` is unchanged, so net = amount -
    # refunded_amount. Consumers (e.g. ERP GL posting) use this to post net cash.
    refunded_amount: Decimal = Decimal("0")
    # Gateway fee withheld from settlement (bank received amount - provider_fee).
    # ERP splits the receipt (Dr Bank net / Dr bank-charges / Cr AR gross).
    provider_fee: Decimal = Decimal("0")
    allocations: list[PaymentAllocationRead] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_presentation(self) -> StatusPresentation:
        """Canonical label/tone/icon projection for payment rendering."""
        from app.services.status_presentation import payment_status_presentation

        return payment_status_presentation(self.status)


class PaymentRefundInvoiceEffectRead(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    receivable_before: Decimal
    receivable_after: Decimal
    refund_attributed: Decimal


class PaymentRefundPreviewRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, lt=10000000000)
    reason: str | None = None


class PaymentRefundPreviewRead(BaseModel):
    payment_id: UUID
    account_id: UUID
    currency: str
    payment_gross: Decimal
    refunded_before: Decimal
    refundable_before: Decimal
    refund_amount: Decimal
    refunded_after: Decimal
    payment_net_after: Decimal
    status_after: PaymentStatus
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    account_credit_consumption: Decimal
    invoice_effects: list[PaymentRefundInvoiceEffectRead]
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


class PaymentRefundRequest(PaymentRefundPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class PaymentRefundRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    provider_event_id: UUID | None = None
    ledger_entry_id: UUID | None = None
    billing_account_ledger_entry_id: UUID | None = None
    credit_consumption_ledger_entry_id: UUID | None = None
    amount: Decimal
    currency: str
    origin: PaymentRefundOrigin
    reason: str | None = None
    preview_fingerprint: str | None = None
    created_at: datetime


class PaymentReversalPreviewRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PaymentReversalPreviewRead(BaseModel):
    payment_id: UUID
    account_id: UUID
    currency: str
    payment_gross: Decimal
    refunded_before: Decimal
    payment_net_before: Decimal
    reversal_amount: Decimal
    status_after: PaymentStatus
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    account_credit_consumption: Decimal
    invoice_effects: list[PaymentRefundInvoiceEffectRead]
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


class PaymentReversalRequest(PaymentReversalPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class PaymentReversalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    payment_id: UUID
    provider_event_id: UUID | None = None
    ledger_entry_id: UUID | None = None
    billing_account_ledger_entry_id: UUID | None = None
    credit_consumption_ledger_entry_id: UUID | None = None
    amount: Decimal
    currency: str
    origin: PaymentReversalOrigin
    reason: str
    preview_fingerprint: str | None = None
    created_at: datetime


class BillingAccountPaymentReturnInvoiceEffectRead(BaseModel):
    payment_allocation_id: UUID
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None = None
    receivable_before: Decimal
    return_amount: Decimal
    receivable_after: Decimal
    result_ledger_entry_type: LedgerEntryType
    result_ledger_source: LedgerSource


class BillingAccountPaymentRefundPreviewRead(BaseModel):
    payment_id: UUID
    billing_account_id: UUID
    currency: str
    payment_gross: Decimal
    refunded_before: Decimal
    refundable_before: Decimal
    refund_amount: Decimal
    refunded_after: Decimal
    payment_net_after: Decimal
    status_after: PaymentStatus
    consolidated_credit_before: Decimal
    consolidated_credit_after: Decimal
    consolidated_credit_consumption: Decimal
    invoice_effects: list[BillingAccountPaymentReturnInvoiceEffectRead]
    billing_account_ledger_entry_type: LedgerEntryType | None = None
    billing_account_ledger_source: LedgerSource | None = None
    service_access_consequence: str
    fingerprint: str


class BillingAccountPaymentRefundRequest(PaymentRefundRequest):
    pass


class BillingAccountPaymentReversalPreviewRead(BaseModel):
    payment_id: UUID
    billing_account_id: UUID
    currency: str
    payment_gross: Decimal
    refunded_before: Decimal
    payment_net_before: Decimal
    reversal_amount: Decimal
    status_after: PaymentStatus
    consolidated_credit_before: Decimal
    consolidated_credit_after: Decimal
    consolidated_credit_consumption: Decimal
    invoice_effects: list[BillingAccountPaymentReturnInvoiceEffectRead]
    billing_account_ledger_entry_type: LedgerEntryType | None = None
    billing_account_ledger_source: LedgerSource | None = None
    service_access_consequence: str
    fingerprint: str


class BillingAccountPaymentReversalRequest(PaymentReversalRequest):
    pass


class PaymentSyncRead(BaseModel):
    """Minimal payment contract used by the ERP bulk sync feed."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    amount: Decimal
    refunded_amount: Decimal = Decimal("0")
    provider_fee: Decimal = Decimal("0")
    # Accounting facts from the one proof-backed WHT receivable, when present.
    # ``amount`` remains the gross customer credit for backward compatibility;
    # ERP posts ``net_amount`` to bank and ``wht_amount`` to WHT receivable.
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None
    wht_amount: Decimal = Decimal("0")
    wht_rate: Decimal | None = None
    wht_status: str | None = None
    wht_record_id: UUID | None = None
    wht_certificate_reference: str | None = None
    wht_resolved_at: datetime | None = None
    currency: str
    status: PaymentStatus
    paid_at: datetime | None = None
    external_id: str | None = None
    memo: str | None = None
    updated_at: datetime
    allocations: list[PaymentAllocationRead] = Field(default_factory=list)
    settlement: PaymentSettlementRead | None = None
    intent_purpose: str | None = None
    allocation_policy: str | None = None
    credit_application_policy: str | None = None
    policy_version: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _project_wht_accounting_facts(cls, value: Any) -> Any:
        """Flatten the WHT source record into the bounded ERP payment feed."""
        if isinstance(value, dict):
            return value
        record = getattr(value, "withholding_tax_record", None)
        gross = getattr(record, "gross_amount", None)
        net = getattr(record, "net_amount", None)
        status = getattr(record, "status", None)
        deposit_intent = next(
            (
                intent
                for intent in getattr(value, "topup_intents", [])
                if getattr(intent, "purpose", None) == "account_credit_deposit"
            ),
            None,
        )
        return {
            "id": value.id,
            "account_id": value.account_id,
            "billing_account_id": value.billing_account_id,
            "payment_method_id": value.payment_method_id,
            "payment_channel_id": value.payment_channel_id,
            "collection_account_id": value.collection_account_id,
            "provider_id": value.provider_id,
            "amount": value.amount,
            "refunded_amount": value.refunded_amount,
            "provider_fee": value.provider_fee,
            "gross_amount": gross if gross is not None else value.amount,
            "net_amount": net if net is not None else value.amount,
            "wht_amount": getattr(record, "wht_amount", Decimal("0")),
            "wht_rate": getattr(record, "wht_rate", None),
            "wht_status": getattr(status, "value", status),
            "wht_record_id": getattr(record, "id", None),
            "wht_certificate_reference": getattr(record, "certificate_reference", None),
            "wht_resolved_at": getattr(record, "resolved_at", None),
            "currency": value.currency,
            "status": value.status,
            "paid_at": value.paid_at,
            "external_id": value.external_id,
            "memo": value.memo,
            "updated_at": value.updated_at,
            "allocations": value.allocations,
            "settlement": value.settlement,
            "intent_purpose": getattr(deposit_intent, "purpose", None),
            "allocation_policy": getattr(deposit_intent, "allocation_policy", None),
            "credit_application_policy": getattr(
                deposit_intent, "credit_application_policy", None
            ),
            "policy_version": getattr(deposit_intent, "policy_version", None),
        }


# --- Customer-initiated online payment (hosted checkout) ------------------
# These power the self-care mobile/SPA flow: the client initiates a provider
# checkout for one of *its own* invoices, completes payment with the provider
# SDK using the returned public key + reference, then asks the server to verify
# and record it. Scoping is by the authenticated principal, not a permission.


class PaymentInitiateRequest(BaseModel):
    invoice_id: UUID
    # Which online gateway to checkout with for a new card; ignored for saved
    # cards. Defaults to the configured provider when omitted.
    provider: str | None = None
    # Charge this saved card server-side (one-tap); idempotency_key makes that
    # charge safe against a double-submit.
    payment_method_id: UUID | None = None
    idempotency_key: str | None = None


class PaymentInitiateResponse(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    amount: Decimal
    currency: str = "NGN"
    provider_type: str
    provider_public_key: str | None = None
    payment_reference: str
    customer_email: str | None = None
    # True when a saved card was charged server-side — skip the gateway webview
    # and go straight to verify (mirrors the top-up flow).
    charged: bool = False
    checkout_url: str | None = None


class PaymentVerifyRequest(BaseModel):
    reference: str = Field(min_length=1)
    provider: str | None = None
    save_card: bool = False


class PaymentVerifyResponse(BaseModel):
    reference: str
    payment_id: UUID
    invoice_id: UUID | None = None
    amount: Decimal
    currency: str = "NGN"
    status: str
    already_recorded: bool = False
    card_saved: bool | None = None
    card_save_message: str | None = None


# --- Prepaid account top-up (hosted checkout) -----------------------------


class PaymentProviderOption(BaseModel):
    """An online checkout option (Paystack/Flutterwave) for the pay selector."""

    provider_type: str
    label: str


class BankTransferAccount(BaseModel):
    bank_name: str
    account_name: str
    account_number: str
    sort_code: str | None = None


class DirectBankTransferConfig(BaseModel):
    """Admin-configured bank account(s) for the direct-transfer pay option."""

    enabled: bool = False
    instructions: str | None = None
    accounts: list[BankTransferAccount] = Field(default_factory=list)


class TopupEligibleInvoice(BaseModel):
    id: UUID
    invoice_number: str | None = None
    balance_due: Decimal
    currency: str


class TopupPreviewInvoiceApplication(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    currency: str
    amount_applied: Decimal
    outstanding_after_application: Decimal


class TopupPreviewResponse(BaseModel):
    account_id: UUID
    currency: str = "NGN"
    current_account_credit: Decimal
    requested_deposit: Decimal
    eligible_invoice_count: int
    invoice_applications: list[TopupPreviewInvoiceApplication] = Field(
        default_factory=list
    )
    total_applied_to_invoices: Decimal = Decimal("0.00")
    total_outstanding_after_application: Decimal = Decimal("0.00")
    remaining_account_credit: Decimal = Decimal("0.00")
    projected_available_credit: Decimal = Decimal("0.00")
    allocation_policy: str = "credit_only"
    credit_application_policy: str = "pay_eligible_invoices"
    policy_version: int = 1
    preview_fingerprint: str = Field(min_length=64, max_length=64)


class TopupPageResponse(BaseModel):
    provider_type: str
    provider_public_key: str | None = None
    currency: str = "NGN"
    prepaid_balance: Decimal | None = None
    account_credit: Decimal | None = None
    deposit_allowed: bool = True
    eligible_unpaid_total: Decimal = Decimal("0.00")
    eligible_unpaid_invoices: list[TopupEligibleInvoice] = Field(default_factory=list)
    min_amount: int
    max_amount: int
    preset_amounts: list[int] = Field(default_factory=list)
    customer_email: str | None = None
    # The full pay-with selector: online gateways (Paystack/Flutterwave) and the
    # direct-bank-transfer option, mirroring the web top-up chooser. Mobile
    # renders one row per online option plus a bank-transfer + saved-card flow.
    payment_options: list[PaymentProviderOption] = Field(default_factory=list)
    direct_bank_transfer: DirectBankTransferConfig | None = None


class TopupPreviewRequest(BaseModel):
    amount: Decimal = Field(gt=0)


class TopupInitiateRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    # Which online gateway to checkout with when paying by a new card. Defaults
    # to the configured provider when omitted; ignored for saved-card charges.
    provider: str | None = None
    # When set, charge this saved card server-side (one-tap repeat pay) instead
    # of opening the gateway checkout. idempotency_key makes that charge safe
    # against a double-tap (a replay returns the original intent).
    payment_method_id: UUID | None = None
    idempotency_key: str | None = None
    preview_fingerprint: str = Field(min_length=64, max_length=64)


class TopupInitiateResponse(BaseModel):
    intent_id: str
    provider_type: str
    provider_public_key: str | None = None
    payment_reference: str
    amount: Decimal
    currency: str = "NGN"
    customer_email: str | None = None
    # True when a saved card was charged server-side — the client should skip
    # the gateway webview and go straight to verify.
    charged: bool = False
    checkout_url: str | None = None
    purpose: str = "account_credit_deposit"
    allocation_policy: str = "credit_only"
    credit_application_policy: str = "pay_eligible_invoices"
    policy_version: int = 1
    preview_fingerprint: str


class TopupVerifyRequest(BaseModel):
    reference: str = Field(min_length=1)
    save_card: bool = False


class TopupVerifyResponse(BaseModel):
    reference: str
    amount: Decimal
    currency: str = "NGN"
    already_recorded: bool = False
    available_balance: Decimal | None = None
    amount_credited: Decimal | None = None
    prepaid_amount_applied: Decimal = Decimal("0.00")
    credit_added: Decimal | None = None
    allocated_total: Decimal = Decimal("0.00")
    allocated_to_invoices: list[dict[str, Any]] = Field(default_factory=list)
    renewed_services: list[dict[str, Any]] = Field(default_factory=list)
    card_saved: bool | None = None
    card_save_message: str | None = None


class PaymentAllocationBase(BaseModel):
    payment_id: UUID
    invoice_id: UUID
    # Bound is create-only (see InvoiceBase); base stays unbounded for *Read.
    amount: Decimal = Decimal("0.00")
    memo: str | None = None


class PaymentAllocationApply(BaseModel):
    invoice_id: UUID
    amount: Decimal = Field(default=Decimal("0.00"), gt=0)
    memo: str | None = None


class PaymentAllocationCreate(PaymentAllocationBase):
    # Create-side bound (kept off the shared base — see PaymentAllocationBase).
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)


class PaymentAllocationPreviewRequest(BaseModel):
    payment_id: UUID
    invoice_id: UUID
    amount: Decimal = Field(gt=0, lt=10000000000)


class PaymentAllocationPreviewRead(BaseModel):
    payment_id: UUID
    settlement_id: UUID
    invoice_id: UUID
    invoice_number: str | None = None
    amount: Decimal
    currency: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    payment_unallocated_before: Decimal
    payment_unallocated_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    receivable_before: Decimal
    receivable_after: Decimal
    invoice_ledger_entry_type: LedgerEntryType
    invoice_ledger_source: LedgerSource
    account_credit_ledger_entry_type: LedgerEntryType
    account_credit_ledger_source: LedgerSource
    access_consequence: str
    fingerprint: str


class PaymentAllocationConfirm(PaymentAllocationPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class PaymentAllocationRead(PaymentAllocationBase):
    model_config = ConfigDict(from_attributes=True)

    # amount inherits unbounded from PaymentAllocationBase (create-only bound).
    id: UUID
    ledger_entry_id: UUID | None = None
    consumption_ledger_entry_id: UUID | None = None
    preview_fingerprint: str | None = None
    created_at: datetime


class PaymentProviderBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    provider_type: PaymentProviderType = PaymentProviderType.custom
    is_active: bool = True
    notes: str | None = None


class PaymentProviderRead(PaymentProviderBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PaymentProviderEventBase(BaseModel):
    provider_id: UUID
    payment_id: UUID | None = None
    invoice_id: UUID | None = None
    event_type: str = Field(min_length=1, max_length=120)
    external_id: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    financial_effect: PaymentProviderEventFinancialEffect = (
        PaymentProviderEventFinancialEffect.none
    )
    status: PaymentProviderEventStatus = PaymentProviderEventStatus.pending
    payload: dict | None = None
    error: str | None = None
    received_at: datetime | None = None
    processed_at: datetime | None = None


class PaymentProviderEventCreate(PaymentProviderEventBase):
    pass


class PaymentProviderEventRead(PaymentProviderEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source: PaymentProviderEventSource
    observation_digest: str | None = Field(default=None, min_length=64, max_length=64)
    observed_payment_status: PaymentStatus | None = None
    provider_fee: Decimal = Decimal("0.00")
    net_amount: Decimal | None = None
    provider_reference: str | None = None
    error_code: str | None = None


class PaymentProviderEventIngest(BaseModel):
    provider_id: UUID
    payment_id: UUID | None = None
    invoice_id: UUID | None = None
    account_id: UUID | None = None
    # Reseller-consolidated billing account. Carried from the originating
    # TopupIntent so a consolidated webhook payment posts against the billing
    # account (crediting its balance / settling member invoices) instead of
    # landing with billing_account_id NULL and never settling. (cutover fix)
    billing_account_id: UUID | None = None
    event_type: str = Field(min_length=1, max_length=120)
    external_id: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    amount: Decimal | None = Field(default=None, gt=0)
    provider_fee: Decimal = Field(default=Decimal("0.00"), ge=0)
    net_amount: Decimal | None = Field(default=None, gt=0)
    provider_reference: str | None = Field(default=None, max_length=120)
    topup_intent_id: UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    financial_effect: PaymentProviderEventFinancialEffect | None = None
    payload: dict | None = None
    # Provider-aware status resolved by the webhook layer. Some providers reuse
    # one event type for both outcomes (Flutterwave's "charge.completed" carries
    # either a successful or a failed charge), so the raw event_type alone is
    # not always mappable. When set, this overrides the static event-type map.
    status_hint: PaymentStatus | None = None


class LedgerEntryBase(BaseModel):
    account_id: UUID
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    entry_type: LedgerEntryType
    source: LedgerSource | None = None
    category: LedgerCategory | None = None
    # Bound is create-only (see InvoiceBase); base stays unbounded for *Read.
    amount: Decimal = Decimal("0.00")
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    memo: str | None = None
    is_active: bool = True
    effective_date: datetime | None = None


class LedgerEntryCreate(LedgerEntryBase):
    # Create-side bound (kept off the shared base — see LedgerEntryBase).
    amount: Decimal = Field(default=Decimal("0.00"), ge=0)


class LedgerEntryUpdate(BaseModel):
    account_id: UUID | None = None
    invoice_id: UUID | None = None
    payment_id: UUID | None = None
    entry_type: LedgerEntryType | None = None
    source: LedgerSource | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    memo: str | None = None
    is_active: bool | None = None


class LedgerEntryRead(LedgerEntryBase):
    model_config = ConfigDict(from_attributes=True)

    # amount inherits unbounded from LedgerEntryBase (create-only bound);
    # ledger amounts are inherently signed (debits/credits/reversals).
    id: UUID
    # Real-world date of the entry (invoice issue / payment / imported txn date).
    # NULL for native and unbackfilled rows; clients should prefer it over
    # created_at (the import instant) and fall back to created_at when NULL.
    effective_date: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AccountAdjustmentPreviewRequest(BaseModel):
    account_id: UUID
    category: LedgerCategory = LedgerCategory.other
    amount: Decimal = Field(gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    memo: str = Field(min_length=3, max_length=500)
    reason: str = Field(min_length=3, max_length=1000)


class AccountAdjustmentPreviewRead(BaseModel):
    account_id: UUID
    category: LedgerCategory
    amount: Decimal
    currency: str
    memo: str
    reason: str
    origin: str
    origin_ref: str | None = None
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    shortfall: Decimal
    allowed: bool
    rejection_reason: str | None = None
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


class AccountAdjustmentConfirm(AccountAdjustmentPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class AccountAdjustmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    category: LedgerCategory
    amount: Decimal
    currency: str
    memo: str
    reason: str
    origin: str
    origin_ref: str | None = None
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    access_consequence: str
    preview_fingerprint: str
    idempotency_key: str
    ledger_entry_id: UUID
    reversal_ledger_entry_id: UUID | None = None
    reversal_preview_fingerprint: str | None = None
    reversal_idempotency_key: str | None = None
    reversal_reason: str | None = None
    reversal_prepaid_funding_before: Decimal | None = None
    reversal_prepaid_funding_after: Decimal | None = None
    reversed_at: datetime | None = None
    created_at: datetime


class AccountAdjustmentReversalPreviewRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class AccountAdjustmentReversalPreviewRead(BaseModel):
    adjustment_id: UUID
    account_id: UUID
    amount: Decimal
    currency: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    reverses_ledger_entry_id: UUID
    access_consequence: str
    reason: str
    fingerprint: str


class AccountAdjustmentReversalConfirm(AccountAdjustmentReversalPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class TaxRateBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    # Bound is create-only (see InvoiceBase); base stays unbounded for *Read.
    rate: Decimal = Decimal("0.0000")
    is_active: bool = True
    description: str | None = None


class TaxRateCreate(TaxRateBase):
    # Create-side bound (kept off the shared base — see TaxRateBase).
    rate: Decimal = Field(default=Decimal("0.0000"), ge=0, lt=100)


class TaxRateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=40)
    rate: Decimal | None = Field(default=None, ge=0, lt=100)
    is_active: bool | None = None
    description: str | None = None


class TaxRateRead(TaxRateBase):
    model_config = ConfigDict(from_attributes=True)

    # rate inherits unbounded from TaxRateBase (create-only bound).
    id: UUID
    created_at: datetime
    updated_at: datetime


class InvoiceRead(InvoiceBase):
    model_config = ConfigDict(from_attributes=True)

    # Money fields inherit unbounded from InvoiceBase (bounds are create-only),
    # so stored signed/zero values serialize without a per-field override.
    id: UUID
    created_at: datetime
    updated_at: datetime
    lines: list[InvoiceLineRead] = Field(default_factory=list)
    payment_allocations: list[PaymentAllocationRead] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status_presentation(self) -> StatusPresentation:
        """Canonical label/tone/icon projection for invoice rendering."""
        from app.services.status_presentation import invoice_status_presentation

        return invoice_status_presentation(self.status)


class CreditNoteRead(CreditNoteBase):
    model_config = ConfigDict(from_attributes=True)

    # Read model reflects stored data: credit notes carry credit (often
    # negative) amounts, so don't inherit the create-side ge=0/lt constraints
    # (they 500 response serialization — same fix as InvoiceRead in #272).
    subtotal: Decimal = Decimal("0.00")
    tax_total: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    id: UUID
    applied_total: Decimal
    issued_at: datetime | None = None
    funding_ledger_entry_id: UUID | None = None
    issue_preview_fingerprint: str | None = None
    void_ledger_entry_id: UUID | None = None
    void_preview_fingerprint: str | None = None
    created_at: datetime
    updated_at: datetime
    lines: list[CreditNoteLineRead] = Field(default_factory=list)
    applications: list[CreditNoteApplicationRead] = Field(default_factory=list)


class CreditNoteSyncLineRead(BaseModel):
    """Minimal credit-note line contract used by the ERP sync feed."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None = None
    tax_rate_id: UUID | None = None
    tax_application: TaxApplication = TaxApplication.exclusive


class CreditNoteSyncRead(BaseModel):
    """Accounting fields required to mirror a credit note into DotMac ERP."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_id: UUID
    invoice_id: UUID | None = None
    credit_number: str | None = None
    status: CreditNoteStatus
    currency: str
    subtotal: Decimal
    tax_total: Decimal
    total: Decimal
    applied_total: Decimal
    memo: str | None = None
    issued_at: datetime | None = None
    updated_at: datetime
    lines: list[CreditNoteSyncLineRead] = Field(default_factory=list)


class InvoiceWriteOffRequest(BaseModel):
    memo: str | None = None


class InvoiceClosureConfirm(BaseModel):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)
    memo: str | None = Field(default=None, max_length=2000)


class InvoiceClosureLedgerEffectRead(BaseModel):
    reverses_ledger_entry_id: UUID | None = None
    result_entry_type: LedgerEntryType
    result_source: LedgerSource
    amount: Decimal
    currency: str


class InvoiceClosurePreviewRead(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    account_id: UUID
    closure_type: InvoiceClosureType
    status_before: InvoiceStatus
    status_after: InvoiceStatus
    invoice_total: Decimal
    payments_applied: Decimal
    credits_applied: Decimal
    receivable_before: Decimal
    receivable_after: Decimal
    closure_amount: Decimal
    currency: str
    ledger_effects: list[InvoiceClosureLedgerEffectRead]
    access_consequence: str
    fingerprint: str


class InvoiceClosureLedgerEvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ledger_entry_id: UUID
    reverses_ledger_entry_id: UUID | None = None


class InvoiceClosureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    invoice_id: UUID
    closure_type: InvoiceClosureType
    origin: InvoiceClosureOrigin
    amount: Decimal
    receivable_before: Decimal
    receivable_after: Decimal
    payments_applied: Decimal
    credits_applied: Decimal
    currency: str
    reason: str | None = None
    preview_fingerprint: str | None = None
    idempotency_key: str | None = None
    created_at: datetime
    ledger_evidence: list[InvoiceClosureLedgerEvidenceRead]


class InvoiceClosureConfirmationRead(BaseModel):
    invoice: InvoiceRead
    closure: InvoiceClosureRead
    idempotent_replay: bool = False


class InvoiceClosureEvidenceCandidateRead(BaseModel):
    ledger_entry_id: UUID
    entry_type: LedgerEntryType
    source: LedgerSource
    amount: Decimal
    currency: str
    reversal_of_entry_id: UUID | None = None


class InvoiceClosureEvidenceInspectionRead(BaseModel):
    invoice_id: UUID
    closure_type: InvoiceClosureType
    expected_amount: Decimal
    currency: str
    candidates: list[InvoiceClosureEvidenceCandidateRead]
    fingerprint: str


class InvoiceClosureEvidenceSelection(BaseModel):
    ledger_entry_id: UUID
    reverses_ledger_entry_id: UUID | None = None


class InvoiceClosureReconciliationRequest(BaseModel):
    closure_type: InvoiceClosureType
    evidence: list[InvoiceClosureEvidenceSelection] = Field(default_factory=list)
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)
    reason: str | None = Field(default=None, max_length=2000)


class InvoiceBulkWriteOffRequest(BaseModel):
    invoice_ids: list[UUID]
    memo: str | None = None


class InvoiceBulkVoidRequest(BaseModel):
    invoice_ids: list[UUID]
    memo: str | None = None


class InvoiceBulkActionResponse(BaseModel):
    updated: int


class InvoiceRunRequest(BaseModel):
    run_at: datetime | None = None
    billing_cycle: BillingCycle | None = None
    dry_run: bool = False


class InvoiceRunResponse(BaseModel):
    run_id: UUID | None = None
    run_at: datetime
    subscriptions_scanned: int
    subscriptions_billed: int
    invoices_created: int
    lines_created: int
    skipped: int


class BillingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_at: datetime
    billing_cycle: str | None
    status: BillingRunStatus
    started_at: datetime
    finished_at: datetime | None
    subscriptions_scanned: int
    subscriptions_billed: int
    invoices_created: int
    lines_created: int
    skipped: int
    error: str | None
    created_at: datetime


class CollectionAccountBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    account_type: CollectionAccountType = CollectionAccountType.bank
    bank_name: str | None = Field(default=None, max_length=120)
    account_name: str | None = Field(default=None, max_length=200)
    account_number: str | None = Field(default=None, max_length=64)
    sort_code: str | None = Field(default=None, max_length=32)
    accounting_code: str | None = Field(default=None, max_length=64)
    presentment_priority: int = 0
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    notes: str | None = None


class CollectionAccountCreate(CollectionAccountBase):
    model_config = ConfigDict(extra="forbid")


class CollectionAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    account_type: CollectionAccountType | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    account_name: str | None = Field(default=None, max_length=200)
    account_number: str | None = Field(default=None, max_length=64)
    sort_code: str | None = Field(default=None, max_length=32)
    accounting_code: str | None = Field(default=None, max_length=64)
    presentment_priority: int | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    notes: str | None = None


class CollectionAccountRead(CollectionAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_last4: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PaymentChannelBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    channel_type: PaymentChannelType = PaymentChannelType.other
    provider_id: UUID | None = None
    fee_rules: dict | None = None
    accounting_code: str | None = Field(default=None, max_length=64)
    notes: str | None = None


class PaymentChannelCreate(PaymentChannelBase):
    model_config = ConfigDict(extra="forbid")


class PaymentChannelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    channel_type: PaymentChannelType | None = None
    provider_id: UUID | None = None
    fee_rules: dict | None = None
    accounting_code: str | None = Field(default=None, max_length=64)
    notes: str | None = None


class PaymentChannelRead(PaymentChannelBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class PaymentChannelAccountBase(BaseModel):
    channel_id: UUID
    collection_account_id: UUID
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    priority: int = 0


class PaymentChannelAccountCreate(PaymentChannelAccountBase):
    model_config = ConfigDict(extra="forbid")


class PaymentChannelAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    priority: int | None = None


class PaymentChannelAccountRead(PaymentChannelAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_default: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class BillingAccountBase(BaseModel):
    reseller_id: UUID
    name: str = Field(min_length=1, max_length=160)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    status: str = Field(default="active", max_length=20)
    is_active: bool = True


class BillingAccountCreate(BillingAccountBase):
    pass


class BillingAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    status: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None


class BillingAccountRead(BillingAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    balance: Decimal
    created_at: datetime
    updated_at: datetime


class BillingAccountConsolidatedPaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    provider_fee: Decimal = Field(default=Decimal("0.00"), ge=0)
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    paid_at: datetime | None = None
    memo: str | None = None
    payment_method_id: UUID | None = None
    payment_channel_id: UUID | None = None
    collection_account_id: UUID | None = None
    provider_id: UUID | None = None
    external_id: str | None = Field(default=None, max_length=120)
    allocations: list[PaymentAllocationApply] | None = None


class BillingAccountPaymentPreviewRequest(BillingAccountConsolidatedPaymentCreate):
    auto_allocate: bool = True


class BillingAccountPaymentAllocationEffectRead(BaseModel):
    invoice_id: UUID
    account_id: UUID
    invoice_number: str | None = None
    receivable_before: Decimal
    receivable_after: Decimal
    allocation_amount: Decimal
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource


class BillingAccountPaymentPreviewRead(BaseModel):
    billing_account_id: UUID
    amount: Decimal
    currency: str
    payment_state: PaymentStatus
    consolidated_credit_before: Decimal
    consolidated_credit_after: Decimal
    allocation_effects: list[BillingAccountPaymentAllocationEffectRead]
    allocated_amount: Decimal
    unallocated_amount: Decimal
    unallocated_ledger_entry_type: LedgerEntryType | None = None
    unallocated_ledger_source: LedgerSource | None = None
    payment_consequence: str
    service_access_consequence: str
    fingerprint: str


class BillingAccountPaymentConfirm(BillingAccountPaymentPreviewRequest):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class BillingAccountCreditAllocationPreviewRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)


class BillingAccountCreditSourceEffectRead(BaseModel):
    billing_account_ledger_entry_id: UUID
    payment_id: UUID
    available_before: Decimal
    consumed_amount: Decimal
    available_after: Decimal


class BillingAccountCreditInvoiceEffectRead(BaseModel):
    invoice_id: UUID
    invoice_number: str | None = None
    receivable_before: Decimal
    allocation_amount: Decimal
    receivable_after: Decimal
    source_billing_account_ledger_entry_id: UUID
    source_payment_id: UUID
    subscriber_ledger_entry_type: LedgerEntryType = LedgerEntryType.credit
    subscriber_ledger_source: LedgerSource = LedgerSource.payment


class BillingAccountCreditAllocationPreviewRead(BaseModel):
    billing_account_id: UUID
    subscriber_id: UUID
    currency: str
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    unbacked_consolidated_credit: Decimal
    subscriber_receivable_before: Decimal
    allocation_amount: Decimal
    subscriber_receivable_after: Decimal
    source_effects: list[BillingAccountCreditSourceEffectRead]
    invoice_effects: list[BillingAccountCreditInvoiceEffectRead]
    consolidated_ledger_entry_type: LedgerEntryType = LedgerEntryType.debit
    consolidated_ledger_source: LedgerSource = LedgerSource.payment
    service_access_consequence: str
    fingerprint: str


class BillingAccountCreditAllocationConfirm(
    BillingAccountCreditAllocationPreviewRequest
):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class BillingAccountCreditAllocationResultRead(BaseModel):
    allocation_id: UUID
    billing_account_id: UUID
    subscriber_id: UUID
    amount: Decimal
    currency: str
    billing_account_ledger_entry_id: UUID
    payment_allocation_ids: list[UUID]
    subscriber_ledger_entry_ids: list[UUID]
    service_access_consequence: str
    idempotent_replay: bool = False


class BillingAccountCreditConsumptionSourceCandidateRead(BaseModel):
    billing_account_ledger_entry_id: UUID
    payment_id: UUID
    amount: Decimal
    linked_consumption: Decimal
    returned_amount: Decimal
    available_amount: Decimal


class BillingAccountCreditConsumptionAllocationCandidateRead(BaseModel):
    payment_allocation_id: UUID
    payment_id: UUID
    payment_has_settlement: bool
    invoice_id: UUID
    subscriber_id: UUID
    amount: Decimal
    subscriber_ledger_entry_ids: list[UUID]


class BillingAccountCreditConsumptionDebitCandidateRead(BaseModel):
    billing_account_ledger_entry_id: UUID
    payment_id: UUID | None = None
    amount: Decimal
    source: LedgerSource
    balance_after: Decimal


class BillingAccountCreditConsumptionEvidenceInspectionRead(BaseModel):
    billing_account_id: UUID
    currency: str
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    unbacked_projection_amount: Decimal
    missing_debit_projection_amount: Decimal
    source_candidates: list[BillingAccountCreditConsumptionSourceCandidateRead]
    allocation_candidates: list[BillingAccountCreditConsumptionAllocationCandidateRead]
    debit_candidates: list[BillingAccountCreditConsumptionDebitCandidateRead]
    service_access_consequence: str


class BillingAccountCreditConsumptionSelection(BaseModel):
    payment_allocation_id: UUID
    subscriber_ledger_entry_id: UUID
    source_billing_account_ledger_entry_id: UUID


class BillingAccountCreditConsumptionReconciliationRequest(BaseModel):
    allocation_evidence: list[BillingAccountCreditConsumptionSelection] = Field(
        min_length=1
    )
    billing_account_debit_ledger_entry_id: UUID | None = None
    create_missing_billing_account_debit: bool = False
    reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def require_exact_debit_action(self):
        selected_existing = self.billing_account_debit_ledger_entry_id is not None
        if selected_existing == self.create_missing_billing_account_debit:
            raise ValueError(
                "Select one existing billing-account debit or explicitly create "
                "the missing debit"
            )
        return self


class BillingAccountCreditConsumptionEffectRead(BaseModel):
    payment_allocation_id: UUID
    payment_id: UUID
    invoice_id: UUID
    subscriber_id: UUID
    subscriber_ledger_entry_id: UUID
    source_billing_account_ledger_entry_id: UUID
    amount: Decimal


class BillingAccountCreditConsumptionReconciliationPreviewRead(BaseModel):
    billing_account_id: UUID
    subscriber_id: UUID
    currency: str
    recorded_consolidated_credit_before: Decimal
    recorded_consolidated_credit_after: Decimal
    evidenced_consolidated_credit_before: Decimal
    evidenced_consolidated_credit_after: Decimal
    projection_drift_before: Decimal
    projection_drift_after: Decimal
    allocation_amount: Decimal
    allocation_effects: list[BillingAccountCreditConsumptionEffectRead]
    billing_account_debit_action: str
    billing_account_debit_ledger_entry_id: UUID | None = None
    billing_account_debit_amount: Decimal
    billing_account_balance_changed: bool = False
    ledger_transaction_created: bool
    service_access_consequence: str
    fingerprint: str


class BillingAccountCreditConsumptionReconciliationConfirm(
    BillingAccountCreditConsumptionReconciliationRequest
):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class BillingAccountCreditConsumptionReconciliationResultRead(BaseModel):
    reconciliation_evidence_id: UUID
    allocation_id: UUID
    billing_account_id: UUID
    subscriber_id: UUID
    amount: Decimal
    currency: str
    billing_account_debit_action: str
    billing_account_ledger_entry_id: UUID
    payment_allocation_ids: list[UUID]
    subscriber_ledger_entry_ids: list[UUID]
    billing_account_balance_changed: bool = False
    service_access_consequence: str
    idempotent_replay: bool = False


class BillingAccountPaymentReturnAllocationCandidateRead(BaseModel):
    payment_allocation_id: UUID
    invoice_id: UUID
    account_id: UUID
    amount: Decimal
    allocation_active: bool
    candidate_ledger_entry_ids: list[UUID]


class BillingAccountPaymentReturnEvidenceInspectionRead(BaseModel):
    return_type: str
    return_id: UUID
    payment_id: UUID
    billing_account_id: UUID
    payment_state: PaymentStatus
    return_amount: Decimal
    currency: str
    already_reconciled: bool
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    linked_billing_account_ledger_entry_id: UUID | None = None
    linked_allocation_evidence_ids: list[UUID]
    linked_provider_event_id: UUID | None = None
    billing_account_candidate_entries: list[BillingAccountLedgerEvidenceCandidateRead]
    allocation_candidates: list[BillingAccountPaymentReturnAllocationCandidateRead]
    provider_candidates: list[BillingAccountPaymentProvenanceCandidateRead]
    service_access_consequence: str


class BillingAccountPaymentReturnReconciliationRequest(BaseModel):
    billing_account_ledger_entry_id: UUID | None = None
    allocation_ledger_entry_ids: dict[UUID, UUID] = Field(default_factory=dict)
    provider_event_id: UUID | None = None
    reason: str = Field(min_length=10, max_length=1000)


class BillingAccountPaymentReturnAllocationEvidenceRead(BaseModel):
    payment_allocation_id: UUID
    invoice_id: UUID
    account_id: UUID
    amount: Decimal
    ledger_entry_id: UUID


class BillingAccountPaymentReturnReconciliationPreviewRead(BaseModel):
    return_type: str
    return_id: UUID
    payment_id: UUID
    billing_account_id: UUID
    currency: str
    return_amount: Decimal
    payment_state_before: PaymentStatus
    payment_state_after: PaymentStatus
    payment_refunded_amount_before: Decimal
    payment_refunded_amount_after: Decimal
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    billing_account_return_amount: Decimal
    billing_account_ledger_entry_id: UUID | None = None
    allocation_return_amount: Decimal
    allocation_evidence: list[BillingAccountPaymentReturnAllocationEvidenceRead]
    provider_event_id: UUID | None = None
    money_posted: bool = False
    billing_account_balance_changed: bool = False
    service_access_consequence: str
    fingerprint: str


class BillingAccountPaymentReturnReconciliationConfirm(
    BillingAccountPaymentReturnReconciliationRequest
):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class BillingAccountPaymentReturnReconciliationResultRead(BaseModel):
    reconciliation_evidence_id: UUID
    return_type: str
    return_id: UUID
    payment_id: UUID
    billing_account_id: UUID
    payment_state: PaymentStatus
    return_amount: Decimal
    currency: str
    billing_account_ledger_entry_id: UUID | None = None
    allocation_evidence_ids: list[UUID]
    subscriber_ledger_entry_ids: list[UUID]
    provider_event_id: UUID | None = None
    money_posted: bool = False
    billing_account_balance_changed: bool = False
    service_access_consequence: str
    idempotent_replay: bool = False


class BillingAccountMissingPaymentReturnEvidenceInspectionRead(BaseModel):
    return_type: str
    payment_id: UUID
    billing_account_id: UUID
    payment_state: PaymentStatus
    payment_amount: Decimal
    currency: str
    existing_refund_ids: list[UUID]
    existing_reversal_id: UUID | None = None
    status_only_candidate: bool
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    billing_account_candidate_entries: list[BillingAccountLedgerEvidenceCandidateRead]
    allocation_candidates: list[BillingAccountPaymentReturnAllocationCandidateRead]
    provider_candidates: list[BillingAccountPaymentProvenanceCandidateRead]
    service_access_consequence: str


class BillingAccountPaymentReturnDocumentReconstructionRequest(
    BillingAccountPaymentReturnReconciliationRequest
):
    return_amount: Decimal = Field(gt=0, lt=10000000000)
    source_reference: str = Field(min_length=3, max_length=255)


class BillingAccountPaymentReturnDocumentReconstructionPreviewRead(BaseModel):
    proposed_return_id: UUID
    return_type: str
    payment_id: UUID
    billing_account_id: UUID
    currency: str
    return_amount: Decimal
    source_reference: str
    payment_state_before: PaymentStatus
    payment_state_after: PaymentStatus
    payment_refunded_amount_before: Decimal
    payment_refunded_amount_after: Decimal
    recorded_consolidated_credit: Decimal
    evidenced_consolidated_credit: Decimal
    projection_drift: Decimal
    billing_account_return_amount: Decimal
    billing_account_ledger_entry_id: UUID | None = None
    allocation_return_amount: Decimal
    allocation_evidence: list[BillingAccountPaymentReturnAllocationEvidenceRead]
    provider_event_id: UUID | None = None
    return_document_created: bool = False
    money_posted: bool = False
    billing_account_balance_changed: bool = False
    service_access_consequence: str
    evidence_fingerprint: str
    fingerprint: str


class BillingAccountPaymentReturnDocumentReconstructionConfirm(
    BillingAccountPaymentReturnDocumentReconstructionRequest
):
    proposed_return_id: UUID
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=16, max_length=120)


class BillingAccountPaymentReturnDocumentReconstructionResultRead(BaseModel):
    reconstruction_evidence_id: UUID
    reconciliation_evidence_id: UUID
    return_type: str
    return_id: UUID
    payment_id: UUID
    billing_account_id: UUID
    payment_state: PaymentStatus
    return_amount: Decimal
    currency: str
    source_reference: str
    billing_account_ledger_entry_id: UUID | None = None
    allocation_evidence_ids: list[UUID]
    subscriber_ledger_entry_ids: list[UUID]
    provider_event_id: UUID | None = None
    return_document_created: bool = True
    money_posted: bool = False
    billing_account_balance_changed: bool = False
    service_access_consequence: str
    idempotent_replay: bool = False


class BillingAccountStatementSubscriberLine(BaseModel):
    subscriber_id: UUID
    subscriber_name: str
    open_invoice_count: int
    open_balance: Decimal
    allocation_allowed: bool = False
    allocation_max: Decimal = Decimal("0.00")
    allocation_reason: str | None = None


class BillingAccountStatementPayment(BaseModel):
    payment_id: UUID
    amount: Decimal
    currency: str
    paid_at: datetime | None
    memo: str | None
    allocated_total: Decimal
    unallocated_amount: Decimal


class BillingAccountStatement(BaseModel):
    billing_account: BillingAccountRead
    subscribers: list[BillingAccountStatementSubscriberLine]
    subscribers_total: int = 0
    recent_payments: list[BillingAccountStatementPayment]
    recent_payments_total: int = 0
    total_outstanding: Decimal
    open_invoice_count: int = 0
    paid_invoice_total: Decimal = Decimal("0.00")
    unallocated_balance: Decimal


class AccountBalanceResponse(BaseModel):
    """Customer account credit balance (positive = credit on file)."""

    credit_balance: Decimal = Decimal("0.00")
    currency: str = "NGN"


class MyPaymentMethodRead(BaseModel):
    """Customer-facing saved card — never exposes the reusable token."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    method_type: PaymentMethodType
    label: str | None = None
    last4: str | None = None
    brand: str | None = None
    expires_month: int | None = None
    expires_year: int | None = None
    is_default: bool = False
    created_at: datetime


class AutopayStatusResponse(BaseModel):
    enabled: bool = False
    payment_method_id: UUID | None = None
    failure_count: int = 0
    last_failure_at: datetime | None = None
    last_failure_reason: str | None = None
    # Enabled but no longer charged (too many declines) until the customer
    # re-enables autopay or picks a new default card.
    suspended: bool = False


class AutopayEnableRequest(BaseModel):
    payment_method_id: UUID | None = None
