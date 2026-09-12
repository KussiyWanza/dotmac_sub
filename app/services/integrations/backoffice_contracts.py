"""Provider-neutral typed capability contracts for back-office collaboration."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ERP_OUTBOX_CAPABILITY = "erp.outbox.deliver.v1"
ERP_STATUS_CAPABILITY = "erp.status.read.v1"
ERP_INVENTORY_CAPABILITY = "erp.inventory.read.v1"
ERP_MATERIAL_STATUS_WEBHOOK_CAPABILITY = "erp.material_status.webhook.v1"
ERP_STAFF_ACCESS_RECONCILE_CAPABILITY = "erp.staff_access.reconcile.v1"
ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY = "erp.staff_access.webhook.v1"
ERP_OPERATIONAL_SYNC_CAPABILITY = "erp.operational_context.sync.v1"
ERP_REGULATORY_CAPABILITY = "erp.regulatory.read.v1"
ERP_EXPENSE_FORM_CAPABILITY = "erp.expense.form_context.v1"
WORKFORCE_ATTENDANCE_READ_CAPABILITY = "workforce.attendance.read.v1"
WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY = "workforce.attendance.punch.v1"

EXPENSE_RECEIPT_CONTRACT_VERSION = "expense-receipt.v1"
ErpExpenseReceiptMimeType = Literal[
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
]


class ErpExpenseClaimLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_line_id: UUID
    category_code: str
    description: str
    claimed_amount: str
    expense_date: str
    vendor_name: str | None = None
    receipt_url: str | None = None
    notes: str | None = None


class ErpExpenseClaimDraftCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_claim_id: UUID
    purpose: str
    claim_date: str
    requested_by_email: str
    requested_approver_id: UUID | None = None
    payment_destination_token: str | None = Field(default=None, repr=False)
    ticket_source_reference: str | None = None
    project_source_reference: str | None = None
    currency_code: str
    remarks: str = ""
    reference_number: str | None = None
    items: tuple[ErpExpenseClaimLine, ...]


class ErpExpenseDraftLineOutcome(BaseModel):
    source_line_id: UUID
    item_id: UUID


class ErpExpenseClaimDraftOutcome(BaseModel):
    claim_id: UUID
    claim_number: str
    status: str
    source_claim_id: UUID
    items: tuple[ErpExpenseDraftLineOutcome, ...]


class ErpExpenseReceiptUploadCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_claim_id: UUID
    item_id: UUID
    source_line_id: UUID
    source_attachment_id: UUID
    file_name: str
    mime_type: ErpExpenseReceiptMimeType
    size_bytes: int = Field(gt=0, le=10 * 1024 * 1024)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_base64: str = Field(min_length=1, repr=False)
    contract_version: Literal["expense-receipt.v1"] = "expense-receipt.v1"

    @property
    def idempotency_key(self) -> str:
        return (
            f"{self.contract_version}:{self.source_claim_id}:"
            f"{self.source_line_id}:{self.source_attachment_id}"
        )


class ErpExpenseReceiptUploadOutcome(BaseModel):
    attachment_id: UUID
    source_claim_id: UUID
    item_id: UUID
    source_attachment_id: UUID
    checksum_sha256: str
    created: bool


class ErpExpenseSubmissionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_claim_id: UUID


class ErpExpenseClaimTransitionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_claim_id: UUID
    claim_id: UUID
    claim_number: str
    status: Literal["submitted", "approved", "rejected"]


class ErpExpenseApprovalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_claim_id: UUID
    decision_id: UUID
    decided_by_email: str
    decided_at: str
    notes: str | None = None


class ErpExpenseRejectionCommand(ErpExpenseApprovalCommand):
    reason: str
