"""Bank-transfer proof flow: upload -> staff verify -> account/invoice credit.

Verification creates a real Payment (status=succeeded, paid_at from the
claimed transfer date) through the standard billing service, optionally
auto-allocated to the account's oldest open invoices; anything unallocated
stays as account credit. Rejection records why. Both outcomes notify the
customer (and the submitting reseller user, when different) on push + email.
Submission requests confirmation from active staff authorized by
``billing:proof:verify`` through the staff inbox plus email/WhatsApp; the shared
request closes only after verification or rejection.
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    BillingAccount,
    Invoice,
    InvoiceStatus,
    PaymentSettlementOrigin,
    PaymentStatus,
    TopupIntent,
)
from app.models.payment_proof import (
    PaymentProof,
    PaymentProofStatus,
)
from app.schemas.audit import AuditEventCreate
from app.services.billing.consolidated_payments import consolidated_settlement_key
from app.services.common import apply_pagination, coerce_uuid, round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.topup_intents import (
    DIRECT_TRANSFER_PROVIDER,
    DirectTransferBankAccountEvidence,
)

if TYPE_CHECKING:
    from app.schemas.billing import PaymentAllocationApply

logger = logging.getLogger(__name__)

MoneyInput = Decimal | int | float | str | None

_UPLOAD_DIR = Path("uploads/payment_proofs")
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf", ".webp"}
_MAX_BYTES = 10 * 1024 * 1024
_REVIEW_PERMISSION = "billing:proof:verify"
_REVIEW_ENTITY_TYPE = "payment_proof"
_REVIEW_SLA_TRIGGER = "payment_proof.review_requested"
SUBMISSION_SCOPE = "payment-proof:submit"
REVIEW_SCOPE = "payment-proof:review"
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


_SUBMIT_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_proofs",
    concern="payment-proof review lifecycle",
    name="submit_payment_proof",
)
_DIRECT_TRANSFER_SUBMIT_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_proofs",
    concern="payment-proof review lifecycle",
    name="submit_direct_transfer_payment_proof",
)
_VERIFY_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_proofs",
    concern="proof-backed payment request",
    name="verify_payment_proof",
)
_REJECT_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_proofs",
    concern="payment-proof review lifecycle",
    name="reject_payment_proof",
)


class PaymentProofUpload(Protocol):
    """Narrow upload boundary required by receipt-file storage."""

    filename: str | None

    async def read(self) -> bytes: ...


class PaymentProofError(DomainError):
    """Stable transport-neutral payment-proof rejection."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        field: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.field = field
        super().__init__(code=code, message=message, details=details)


class PaymentProofReviewError(PaymentProofError):
    """Stable rejection from payment-proof verification or rejection."""


def _error(
    suffix: str,
    message: str,
    *,
    field: str | None = None,
    **details: object,
) -> PaymentProofReviewError:
    return PaymentProofReviewError(
        code=f"financial.payment_proofs.{suffix}",
        message=message,
        field=field,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PaymentProofResult:
    """Immutable command result serialized only by adapters."""

    id: UUID
    account_id: UUID | None
    billing_account_id: UUID | None
    amount: Decimal
    gross_amount: Decimal | None
    wht_amount: Decimal | None
    wht_rate: Decimal | None
    verified_amount: Decimal | None
    currency: str
    bank_name: str | None
    reference: str | None
    paid_at: datetime | None
    status: PaymentProofStatus
    review_notes: str | None
    payment_id: UUID | None
    created_at: datetime
    duplicate_reference: bool = False
    withholding_tax_record_id: UUID | None = None

    @classmethod
    def from_model(
        cls,
        proof: PaymentProof,
        *,
        duplicate_reference: bool = False,
        withholding_tax_record_id: UUID | None = None,
    ) -> PaymentProofResult:
        return cls(
            id=proof.id,
            account_id=proof.account_id,
            billing_account_id=proof.billing_account_id,
            amount=proof.amount,
            gross_amount=proof.gross_amount,
            wht_amount=proof.wht_amount,
            wht_rate=proof.wht_rate,
            verified_amount=proof.verified_amount,
            currency=proof.currency,
            bank_name=proof.bank_name,
            reference=proof.reference,
            paid_at=proof.paid_at,
            status=proof.status,
            review_notes=proof.review_notes,
            payment_id=proof.payment_id,
            created_at=proof.created_at,
            duplicate_reference=duplicate_reference,
            withholding_tax_record_id=withholding_tax_record_id,
        )

    def to_dict(self) -> dict[str, object | None]:
        values: dict[str, object | None] = {
            "id": str(self.id),
            "account_id": str(self.account_id) if self.account_id else None,
            "billing_account_id": (
                str(self.billing_account_id) if self.billing_account_id else None
            ),
            "amount": self.amount,
            "gross_amount": self.gross_amount,
            "wht_amount": self.wht_amount,
            "wht_rate": self.wht_rate,
            "verified_amount": self.verified_amount,
            "currency": self.currency,
            "bank_name": self.bank_name,
            "reference": self.reference,
            "paid_at": self.paid_at,
            "status": self.status.value,
            "review_notes": self.review_notes,
            "payment_id": str(self.payment_id) if self.payment_id else None,
            "created_at": self.created_at,
            "duplicate_reference": self.duplicate_reference,
            "withholding_tax_record_id": (
                str(self.withholding_tax_record_id)
                if self.withholding_tax_record_id
                else None
            ),
        }
        return values


@dataclass(frozen=True, slots=True)
class PaymentProofReviewEligibility:
    """Read-side eligibility from the owner that executes proof review."""

    verify_allowed: bool
    verify_unavailable_reason: str | None
    reject_allowed: bool
    reject_unavailable_reason: str | None


@dataclass(frozen=True, slots=True)
class DirectTransferProofSubmissionCommand:
    """Typed evidence for one customer direct-transfer proof submission."""

    intent_id: UUID
    account_id: UUID
    submitted_by: UUID
    selected_bank_account: DirectTransferBankAccountEvidence
    paid_at: datetime
    file_path: str


async def save_proof_file(file: PaymentProofUpload) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise _error(
            "unsupported_file_type",
            "Upload a JPG, PNG, WEBP or PDF of the transfer receipt",
            field="file",
        )
    blob = await file.read()
    if len(blob) > _MAX_BYTES:
        raise _error(
            "file_too_large",
            "File too large (max 10 MB)",
            field="file",
        )
    if not blob:
        raise _error("empty_file", "Empty file", field="file")
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid_mod.uuid4().hex}{suffix}"
    (_UPLOAD_DIR / name).write_bytes(blob)
    return str(_UPLOAD_DIR / name)


def _serialize(p: PaymentProof) -> dict[str, object | None]:
    return PaymentProofResult.from_model(p).to_dict()


def get_proof(db: Session, proof_id: str) -> PaymentProof | None:
    try:
        return db.get(PaymentProof, coerce_uuid(proof_id))
    except (ValueError, TypeError):
        return None


def find_duplicate_proofs(db: Session, proof: PaymentProof) -> list[PaymentProof]:
    """Other non-rejected proofs for the same subscriber with the same reference.

    Used to flag likely duplicate submissions of one receipt, and to block
    verifying a reference that already backs a verified proof's payment.
    """
    reference = (proof.reference or "").strip()
    if not reference:
        return []
    query = db.query(PaymentProof)
    # Scope to the same target — a subscriber account or a reseller billing
    # account — so a null account_id on consolidated proofs doesn't collide
    # across resellers.
    if proof.billing_account_id is not None:
        query = query.filter(
            PaymentProof.billing_account_id == proof.billing_account_id
        )
    else:
        query = query.filter(PaymentProof.account_id == proof.account_id)
    return (
        query.filter(PaymentProof.id != proof.id)
        .filter(PaymentProof.reference == reference)
        .filter(PaymentProof.status != PaymentProofStatus.rejected)
        .order_by(PaymentProof.created_at.asc())
        .all()
    )


def review_eligibility(
    proof: PaymentProof,
    duplicates: list[PaymentProof] | tuple[PaymentProof, ...] = (),
) -> PaymentProofReviewEligibility:
    """Project the review actions that the command owner will accept.

    Execution still locks and rechecks these rules.  This projection exists so
    clients do not recreate lifecycle or duplicate-reference policy.
    """

    if proof.status != PaymentProofStatus.submitted:
        reason = f"This proof was already {proof.status.value}."
        return PaymentProofReviewEligibility(
            verify_allowed=False,
            verify_unavailable_reason=reason,
            reject_allowed=False,
            reject_unavailable_reason=reason,
        )
    verified_duplicate = next(
        (item for item in duplicates if item.status == PaymentProofStatus.verified),
        None,
    )
    if verified_duplicate is not None:
        return PaymentProofReviewEligibility(
            verify_allowed=False,
            verify_unavailable_reason=(
                "This transfer reference already backs verified proof "
                f"{verified_duplicate.id}. Reject this submission as a duplicate."
            ),
            reject_allowed=True,
            reject_unavailable_reason=None,
        )
    return PaymentProofReviewEligibility(
        verify_allowed=True,
        verify_unavailable_reason=None,
        reject_allowed=True,
        reject_unavailable_reason=None,
    )


def resolve_proof_file(proof: PaymentProof) -> tuple[Path, str]:
    """Return the (path, media_type) for a proof's receipt file.

    Raises a stable domain error when the file is missing, and refuses any
    ``file_path`` that does not resolve under the payment-proofs upload directory.
    """
    base = _UPLOAD_DIR.resolve()
    path = Path(proof.file_path).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise _error("file_not_found", "Receipt file not found", field="file")
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return path, media_type


def submit_proof(
    db: Session,
    account_id: str | None,
    *,
    context: CommandContext,
    submitted_by: str | None,
    amount: MoneyInput,
    currency: str = "NGN",
    bank_name: str | None = None,
    reference: str | None = None,
    paid_at: datetime | None = None,
    file_path: str,
    billing_account_id: str | None = None,
    gross_amount: MoneyInput = None,
    wht_rate: MoneyInput = None,
) -> PaymentProofResult:
    return execute_owner_command(
        db,
        definition=_SUBMIT_COMMAND,
        context=context,
        operation=lambda: _submit_proof(
            db,
            account_id,
            context=context,
            submitted_by=submitted_by,
            amount=amount,
            currency=currency,
            bank_name=bank_name,
            reference=reference,
            paid_at=paid_at,
            file_path=file_path,
            billing_account_id=billing_account_id,
            gross_amount=gross_amount,
            wht_rate=wht_rate,
        ),
    )


def submit_direct_transfer_proof(
    db: Session,
    command: DirectTransferProofSubmissionCommand,
    *,
    context: CommandContext,
) -> PaymentProofResult:
    """Atomically record a proof and link its locked direct-transfer intent."""

    return execute_owner_command(
        db,
        definition=_DIRECT_TRANSFER_SUBMIT_COMMAND,
        context=context,
        operation=lambda: _submit_direct_transfer_proof(
            db,
            command=command,
            context=context,
        ),
    )


def _submit_direct_transfer_proof(
    db: Session,
    *,
    command: DirectTransferProofSubmissionCommand,
    context: CommandContext,
) -> PaymentProofResult:
    from app.services import topup_intents

    intent = topup_intents.lock_direct_transfer_intent_for_proof(
        db,
        intent_id=command.intent_id,
        account_id=command.account_id,
    )
    result = _submit_proof(
        db,
        str(command.account_id),
        context=context,
        submitted_by=str(command.submitted_by),
        amount=intent.requested_amount,
        currency=intent.currency,
        bank_name=command.selected_bank_account.bank_name,
        reference=intent.reference,
        paid_at=command.paid_at,
        file_path=command.file_path,
        gross_amount=((intent.metadata_ or {}).get("withholding_tax") or {}).get(
            "gross_amount"
        ),
        wht_rate=((intent.metadata_ or {}).get("withholding_tax") or {}).get(
            "withholding_tax_rate_percent"
        ),
        allow_server_wht_snapshot=True,
    )
    topup_intents.stage_direct_transfer_proof_submission(
        db,
        intent=intent,
        proof_id=result.id,
        selected_bank_account=command.selected_bank_account,
        context=context,
    )
    return result


def _submit_proof(
    db: Session,
    account_id: str | None,
    *,
    context: CommandContext,
    submitted_by: str | None,
    amount: MoneyInput,
    currency: str = "NGN",
    bank_name: str | None = None,
    reference: str | None = None,
    paid_at: datetime | None = None,
    file_path: str,
    billing_account_id: str | None = None,
    gross_amount: MoneyInput = None,
    wht_rate: MoneyInput = None,
    allow_server_wht_snapshot: bool = False,
) -> PaymentProofResult:
    """Record a transfer receipt.

    ``amount`` is the net cash transferred (what's on the receipt). For a
    reseller bulk transfer made net of withholding tax pass ``billing_account_id``
    plus ``gross_amount`` (the billed value) and/or ``wht_rate``; the withheld
    tax (gross − net) is stored so verification can credit the gross and raise a
    WHT receivable.
    """
    if bool(account_id) == bool(billing_account_id):
        raise _error(
            "invalid_target",
            "Exactly one subscriber account or billing account target is required",
        )
    if not file_path.strip():
        raise _error("file_required", "A receipt file is required", field="file")
    try:
        value = round_money(to_decimal(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "invalid_amount", "Invalid transfer amount", field="amount"
        ) from exc
    if value <= Decimal("0.00"):
        raise _error(
            "amount_non_positive",
            "Amount must be greater than 0",
            field="amount",
        )
    if (
        account_id
        and not allow_server_wht_snapshot
        and (gross_amount not in (None, "") or wht_rate not in (None, ""))
    ):
        raise _error(
            "withholding_tax_basis_unavailable",
            "Customer transfer proofs cannot declare WHT outside the server-issued invoice intent snapshot",
            field="account_id",
        )
    if billing_account_id and (
        gross_amount not in (None, "") or wht_rate not in (None, "")
    ):
        raise _error(
            "withholding_tax_basis_unavailable",
            "Consolidated transfer proofs cannot declare WHT without an authoritative invoice basis",
            field="billing_account_id",
        )

    try:
        gross_value = round_money(to_decimal(gross_amount)) if gross_amount else None
        rate_value = (
            round_money(to_decimal(wht_rate)) if wht_rate not in (None, "") else None
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "invalid_withholding_tax",
            "Invalid gross amount or WHT rate",
            field="gross_amount",
        ) from exc
    if rate_value is not None and not (Decimal("0") <= rate_value < Decimal("100")):
        raise _error(
            "invalid_withholding_tax",
            "WHT rate must be at least 0 and less than 100 percent",
            field="wht_rate",
        )
    # Derive gross from rate when only a rate was supplied (net = gross·(1−rate)).
    if gross_value is None and rate_value and rate_value > 0:
        gross_value = round_money(value / (Decimal("1") - rate_value / Decimal("100")))
    wht_value = None
    if gross_value is not None:
        if gross_value < value:
            raise _error(
                "invalid_withholding_tax",
                "Gross amount cannot be less than the transferred amount",
                field="gross_amount",
            )
        wht_value = round_money(gross_value - value)
        if rate_value is None and gross_value > 0:
            rate_value = round_money(wht_value / gross_value * Decimal("100"))
        elif rate_value is not None:
            expected_wht = round_money(gross_value * rate_value / Decimal("100"))
            if expected_wht != wht_value:
                raise _error(
                    "invalid_withholding_tax",
                    ("Gross amount, transferred amount, and WHT rate do not reconcile"),
                    field="wht_rate",
                )

    proof = PaymentProof(
        account_id=coerce_uuid(account_id) if account_id else None,
        billing_account_id=(
            coerce_uuid(billing_account_id) if billing_account_id else None
        ),
        submitted_by=coerce_uuid(submitted_by) if submitted_by else None,
        amount=value,
        gross_amount=gross_value,
        wht_amount=wht_value,
        wht_rate=rate_value,
        currency=(currency or "NGN")[:3].upper(),
        bank_name=(bank_name or "").strip() or None,
        reference=(reference or "").strip() or None,
        paid_at=paid_at,
        file_path=file_path,
    )
    db.add(proof)
    db.flush()
    _queue_reviewer_confirmation(db, proof)
    _emit_transition(
        db,
        context=context,
        proof=proof,
        event_type=EventType.payment_proof_submitted,
    )
    # Flag likely duplicate submissions of the same receipt up front so both
    # the submitter and the reviewer see it before any money moves.
    return PaymentProofResult.from_model(
        proof,
        duplicate_reference=bool(find_duplicate_proofs(db, proof)),
    )


def list_for_account(
    db: Session, account_id: str, limit: int = 50, offset: int = 0
) -> list[dict[str, object | None]]:
    query = (
        db.query(PaymentProof)
        .filter(PaymentProof.account_id == coerce_uuid(account_id))
        .order_by(PaymentProof.created_at.desc())
    )
    return [_serialize(p) for p in apply_pagination(query, limit, offset).all()]


def list_for_billing_account(
    db: Session, billing_account_id: str, limit: int = 50, offset: int = 0
) -> list[dict[str, object | None]]:
    """Consolidated (reseller) transfer proofs for one billing account."""
    query = (
        db.query(PaymentProof)
        .filter(PaymentProof.billing_account_id == coerce_uuid(billing_account_id))
        .order_by(PaymentProof.created_at.desc())
    )
    return [_serialize(p) for p in apply_pagination(query, limit, offset).all()]


def list_admin(
    db: Session, status: str | None = "submitted", limit: int = 100, offset: int = 0
) -> list[dict[str, object | None]]:
    query = db.query(PaymentProof).order_by(PaymentProof.created_at.asc())
    if status:
        query = query.filter(PaymentProof.status == PaymentProofStatus(status))
    out: list[dict[str, object | None]] = []
    for p in apply_pagination(query, limit, offset).all():
        d = _serialize(p)
        d["file_path"] = p.file_path
        d["submitted_by"] = str(p.submitted_by) if p.submitted_by else None
        out.append(d)
    return out


def _open_invoice_allocations(
    db: Session, account_id: UUID, amount: Decimal
) -> list[PaymentAllocationApply]:
    """Spread the verified amount across the oldest open invoices."""
    from app.schemas.billing import PaymentAllocationApply

    open_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(open_statuses))
        .order_by(Invoice.created_at.asc())
        .all()
    )
    allocations: list[PaymentAllocationApply] = []
    remaining = amount
    for inv in invoices:
        if remaining <= Decimal("0.00"):
            break
        due = Decimal(str(inv.balance_due or 0))
        if due <= Decimal("0.00"):
            continue
        take = min(due, remaining)
        allocations.append(
            PaymentAllocationApply(
                invoice_id=inv.id, amount=take, memo="bank-transfer proof"
            )
        )
        remaining -= take
    return allocations


def verify_proof(
    db: Session,
    proof_id: str,
    *,
    context: CommandContext,
    verified_by: str,
    amount: MoneyInput = None,
    auto_allocate: bool = True,
    review_notes: str | None = None,
) -> PaymentProofResult:
    return execute_owner_command(
        db,
        definition=_VERIFY_COMMAND,
        context=context,
        operation=lambda: _verify_proof(
            db,
            proof_id,
            context=context,
            verified_by=verified_by,
            amount=amount,
            auto_allocate=auto_allocate,
            review_notes=review_notes,
        ),
    )


def _verify_proof(
    db: Session,
    proof_id: str,
    *,
    context: CommandContext,
    verified_by: str,
    amount: MoneyInput = None,
    auto_allocate: bool = True,
    review_notes: str | None = None,
) -> PaymentProofResult:
    """Confirm the transfer and create the real Payment.

    ``amount`` is the reviewer-confirmed amount from the bank statement; it
    defaults to the customer-claimed amount and is stored alongside it as
    ``verified_amount``. The subscriber row is locked (Postgres) and the
    status re-checked so two concurrent verifies cannot both create a payment.
    """
    proof = db.scalar(
        select(PaymentProof)
        .where(PaymentProof.id == coerce_uuid(proof_id))
        .with_for_update()
    )
    if proof is None:
        raise _error("not_found", "Payment proof not found", proof_id=proof_id)
    if proof.status != PaymentProofStatus.submitted:
        raise _error(
            "already_reviewed",
            "Proof already reviewed",
            proof_id=proof_id,
            status=proof.status.value,
        )

    # Reseller consolidated transfer (credits a billing account, may carry WHT).
    if proof.billing_account_id is not None:
        return _verify_consolidated_proof(
            db,
            proof,
            context=context,
            verified_by=verified_by,
            amount=amount,
            review_notes=review_notes,
        )

    if proof.account_id is None:
        raise _error(
            "invalid_target",
            "Payment proof has no canonical subscriber account target",
            proof_id=proof_id,
        )

    from app.schemas.billing import PaymentAllocationApply, PaymentCreate
    from app.services import billing as billing_service
    from app.services.billing._common import lock_account

    # Serialize concurrent reviews of this account's proofs, then re-check the
    # status: the loser of the race sees "already reviewed" instead of
    # double-creating the payment.
    lock_account(db, str(proof.account_id))
    db.refresh(proof)
    if proof.status != PaymentProofStatus.submitted:
        raise _error(
            "already_reviewed",
            "Proof already reviewed",
            proof_id=proof_id,
            status=proof.status.value,
        )

    try:
        value = round_money(to_decimal(amount if amount is not None else proof.amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "invalid_verified_amount",
            "Invalid verified amount",
            field="amount",
        ) from exc
    if value <= Decimal("0.00"):
        raise _error(
            "verified_amount_non_positive",
            "Verified amount must be greater than 0",
            field="amount",
        )

    duplicates = find_duplicate_proofs(db, proof)
    verified_dup = next(
        (d for d in duplicates if d.status == PaymentProofStatus.verified), None
    )
    if verified_dup is not None:
        raise _error(
            "duplicate_transfer_reference",
            (
                f"Reference '{proof.reference}' was already verified on another "
                f"proof ({verified_dup.id}) and paid. Reject this submission as "
                "a duplicate instead."
            ),
            duplicate_proof_id=str(verified_dup.id),
        )

    deposit_intent = db.scalar(
        select(TopupIntent).where(
            TopupIntent.account_id == proof.account_id,
            TopupIntent.reference == proof.reference,
            TopupIntent.purpose == "account_credit_deposit",
        )
    )
    direct_transfer_intent = db.scalar(
        select(TopupIntent)
        .where(
            TopupIntent.account_id == proof.account_id,
            TopupIntent.reference == proof.reference,
            TopupIntent.provider_type == DIRECT_TRANSFER_PROVIDER,
        )
        .order_by(TopupIntent.created_at.desc())
    )
    direct_transfer_metadata = (
        dict(direct_transfer_intent.metadata_ or {})
        if direct_transfer_intent is not None
        else {}
    )
    invoice_id_raw = (
        str(direct_transfer_metadata.get("invoice_id") or "").strip()
        if direct_transfer_metadata.get("payment_flow") == "invoice_payment"
        else ""
    )
    withholding_tax_snapshot = dict(
        direct_transfer_metadata.get("withholding_tax") or {}
    )
    wht_record_id: UUID | None = None
    if deposit_intent is not None:
        from app.services.account_credit_deposits import (
            AccountCreditDeposits,
            AccountCreditDepositSettlementSource,
            DepositEligibilityError,
            SettleAccountCreditDepositCommand,
        )

        try:
            settlement = AccountCreditDeposits.stage_verified_settlement(
                db,
                SettleAccountCreditDepositCommand(
                    intent_id=deposit_intent.id,
                    provider_type=deposit_intent.provider_type,
                    external_transaction_id=f"proof:{proof.id}",
                    amount=value,
                    currency=proof.currency,
                    provider_intent_id=deposit_intent.id,
                    source=AccountCreditDepositSettlementSource.payment_proof,
                ),
                context=context,
            )
        except DepositEligibilityError as exc:
            raise _error(
                "deposit_settlement_rejected",
                str(exc),
                deposit_error_code=exc.code,
            ) from exc
        payment = settlement.payment
    else:
        payment_amount = value
        allocations = None
        if invoice_id_raw:
            try:
                invoice_uuid = coerce_uuid(invoice_id_raw)
            except (TypeError, ValueError) as exc:
                raise _error(
                    "invoice_not_found",
                    "Direct-transfer intent is missing valid invoice evidence",
                    invoice_id=invoice_id_raw,
                ) from exc
            if withholding_tax_snapshot:
                expected_net = round_money(
                    to_decimal(withholding_tax_snapshot.get("net_amount") or 0)
                )
                if value != expected_net:
                    raise _error(
                        "verified_amount_conflict",
                        "Verified net cash does not match the server-issued WHT transfer amount",
                        proof_id=str(proof.id),
                    )
                payment_amount = round_money(
                    to_decimal(withholding_tax_snapshot.get("gross_amount") or 0)
                )
                proof.gross_amount = payment_amount
                proof.wht_amount = round_money(
                    to_decimal(
                        withholding_tax_snapshot.get("withholding_tax_amount") or 0
                    )
                )
                proof.wht_rate = round_money(
                    to_decimal(
                        withholding_tax_snapshot.get("withholding_tax_rate_percent")
                        or 0
                    )
                )
            allocations = [
                PaymentAllocationApply(
                    invoice_id=invoice_uuid,
                    amount=payment_amount,
                    memo="bank-transfer proof",
                )
            ]
            auto_allocate = False
        elif auto_allocate:
            allocations = _open_invoice_allocations(db, proof.account_id, value)
        payment = billing_service.payments.stage_create(
            db,
            PaymentCreate(
                account_id=proof.account_id,
                amount=payment_amount,
                currency=proof.currency,
                status=PaymentStatus.succeeded,
                paid_at=proof.paid_at or datetime.now(UTC),
                external_id=(proof.reference or "")[:120] or None,
                memo=f"Bank transfer (proof {proof.id})",
                allocations=allocations or None,
            ),
            auto_allocate=auto_allocate,
        )
        if withholding_tax_snapshot:
            from app.services import tax_accounting

            wht_record = tax_accounting.stage_withholding_tax_receivable(
                db,
                account_id=proof.account_id,
                billing_account_id=None,
                reseller_id=None,
                payment_id=payment.id,
                payment_proof_id=proof.id,
                gross_amount=payment_amount,
                net_amount=value,
                wht_amount=round_money(
                    to_decimal(
                        withholding_tax_snapshot.get("withholding_tax_amount") or 0
                    )
                ),
                wht_rate=round_money(
                    to_decimal(
                        withholding_tax_snapshot.get("withholding_tax_rate_percent")
                        or 0
                    )
                ),
                vat_exclusive_amount=round_money(
                    to_decimal(
                        withholding_tax_snapshot.get("vat_exclusive_amount") or 0
                    )
                ),
                vat_amount=round_money(
                    to_decimal(withholding_tax_snapshot.get("vat_amount") or 0)
                ),
                source_invoice_id=coerce_uuid(
                    str(
                        withholding_tax_snapshot.get("source_invoice_id")
                        or invoice_id_raw
                    )
                ),
                policy_version=int(withholding_tax_snapshot.get("policy_version") or 0),
                currency=proof.currency,
                context=context,
            )
            wht_record_id = wht_record.id
    proof.status = PaymentProofStatus.verified
    proof.verified_amount = value
    proof.verified_by = str(verified_by)
    proof.review_notes = (review_notes or "").strip() or None
    proof.payment_id = payment.id
    _resolve_reviewer_confirmation(db, proof)
    _audit(
        db,
        context,
        action="verify",
        proof=proof,
        actor_id=verified_by,
        metadata={
            "claimed_amount": str(proof.amount),
            "verified_amount": str(value),
            "currency": proof.currency,
            "reference": proof.reference,
            "payment_id": str(payment.id),
            "auto_allocate": auto_allocate,
        },
    )
    _notify(db, proof, approved=True)
    _emit_transition(
        db,
        context=context,
        proof=proof,
        event_type=EventType.payment_proof_verified,
    )
    return PaymentProofResult.from_model(
        proof,
        withholding_tax_record_id=wht_record_id,
    )


def _verify_consolidated_proof(
    db: Session,
    proof: PaymentProof,
    *,
    context: CommandContext,
    verified_by: str,
    amount: MoneyInput = None,
    review_notes: str | None = None,
) -> PaymentProofResult:
    """Verify a reseller bulk transfer: credit the billing account the gross and
    raise a withholding-tax receivable for any tax withheld at source.

    ``amount`` (reviewer-confirmed) is the *net cash* received; the account is
    credited ``net + wht`` so the withheld tax stays a tracked, reclaimable
    receivable rather than vanishing from the reseller's balance."""
    from sqlalchemy import select

    from app.services import billing as billing_service

    # Serialize concurrent reviews of this proof, then re-check its status under
    # the lock — exactly what the subscriber path does, and what this one never did.
    #
    # The status check before the dispatch is an UNLOCKED read, so two reviewers
    # clicking Verify at the same time both passed it and both created a succeeded
    # Payment for the gross value: the reseller's balance was credited TWICE, with
    # two WithholdingTaxRecord rows to match. No database constraint caught it
    # either — uq_payments_active_external_id only fires when provider_id IS NOT
    # NULL, and a proof-backed payment has none.
    #
    # Lock the billing account this credits (the subscriber path locks the
    # subscriber it credits) so the loser of the race sees "already reviewed".
    ba = db.scalar(
        select(BillingAccount)
        .where(BillingAccount.id == proof.billing_account_id)
        .with_for_update()
    )
    if ba is None:
        raise _error(
            "billing_account_not_found",
            "Billing account not found",
            billing_account_id=str(proof.billing_account_id),
        )

    db.refresh(proof)
    if proof.status != PaymentProofStatus.submitted:
        raise _error(
            "already_reviewed",
            "Proof already reviewed",
            proof_id=str(proof.id),
            status=proof.status.value,
        )

    try:
        net_value = round_money(
            to_decimal(amount if amount is not None else proof.amount)
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error(
            "invalid_verified_amount",
            "Invalid verified amount",
            field="amount",
        ) from exc
    if net_value <= Decimal("0.00"):
        raise _error(
            "verified_amount_non_positive",
            "Verified amount must be greater than 0",
            field="amount",
        )

    source_gross = (
        round_money(to_decimal(proof.gross_amount))
        if proof.gross_amount is not None
        else None
    )
    if source_gross is not None:
        if source_gross < net_value:
            raise _error(
                "verified_net_exceeds_gross",
                "Verified net amount exceeds the submitted gross amount",
                field="amount",
            )
        gross_value = source_gross
        wht_value = round_money(gross_value - net_value)
    else:
        # Legacy proofs may predate explicit gross capture. Their stored WHT is
        # retained as evidence and gross is reconstructed once at verification.
        wht_value = (
            round_money(to_decimal(proof.wht_amount))
            if proof.wht_amount
            else Decimal("0.00")
        )
        gross_value = round_money(net_value + wht_value)
    effective_wht_rate = (
        round_money(wht_value / gross_value * Decimal("100"))
        if wht_value > Decimal("0.00") and gross_value > Decimal("0.00")
        else None
    )

    verified_dup = next(
        (
            d
            for d in find_duplicate_proofs(db, proof)
            if d.status == PaymentProofStatus.verified
        ),
        None,
    )
    if verified_dup is not None:
        raise _error(
            "duplicate_transfer_reference",
            (
                f"Reference '{proof.reference}' was already verified on another "
                f"proof ({verified_dup.id}). Reject this submission as a duplicate."
            ),
            duplicate_proof_id=str(verified_dup.id),
        )

    from app.schemas.billing import BillingAccountPaymentPreviewRequest

    payment = billing_service.consolidated_payment_settlements.stage_settle_verified(
        db,
        str(ba.id),
        BillingAccountPaymentPreviewRequest(
            amount=gross_value,
            currency=proof.currency,
            paid_at=proof.paid_at or datetime.now(UTC),
            external_id=(proof.reference or "")[:120] or None,
            memo=f"Reseller bank transfer (proof {proof.id})",
            allocations=None,
            auto_allocate=False,
        ),
        idempotency_key=consolidated_settlement_key("payment-proof", str(proof.id)),
        origin=PaymentSettlementOrigin.manual,
        actor_id=str(verified_by),
    ).payment
    proof.status = PaymentProofStatus.verified
    proof.verified_amount = net_value
    proof.verified_by = str(verified_by)
    proof.review_notes = (review_notes or "").strip() or None
    proof.payment_id = payment.id

    wht_record_id: UUID | None = None
    if wht_value > Decimal("0.00"):
        from app.services import tax_accounting

        record = tax_accounting.stage_withholding_tax_receivable(
            db,
            account_id=None,
            billing_account_id=ba.id,
            reseller_id=ba.reseller_id,
            payment_id=payment.id,
            payment_proof_id=proof.id,
            gross_amount=gross_value,
            net_amount=net_value,
            wht_amount=wht_value,
            wht_rate=effective_wht_rate,
            currency=proof.currency,
            context=context,
        )
        wht_record_id = record.id

    _resolve_reviewer_confirmation(db, proof)
    _audit(
        db,
        context,
        action="verify",
        proof=proof,
        actor_id=verified_by,
        metadata={
            "billing_account_id": str(ba.id),
            "net_amount": str(net_value),
            "gross_amount": str(gross_value),
            "wht_amount": str(wht_value),
            "currency": proof.currency,
            "reference": proof.reference,
            "payment_id": str(payment.id),
            "withholding_tax_record_id": (
                str(wht_record_id) if wht_record_id else None
            ),
        },
    )
    # _notify targets account_id (None here) + submitted_by, so the reseller
    # user who uploaded the receipt is notified; the null account is skipped.
    _notify(db, proof, approved=True)
    _emit_transition(
        db,
        context=context,
        proof=proof,
        event_type=EventType.payment_proof_verified,
    )
    return PaymentProofResult.from_model(
        proof,
        withholding_tax_record_id=wht_record_id,
    )


def reject_proof(
    db: Session,
    proof_id: str,
    *,
    context: CommandContext,
    verified_by: str,
    review_notes: str,
) -> PaymentProofResult:
    return execute_owner_command(
        db,
        definition=_REJECT_COMMAND,
        context=context,
        operation=lambda: _reject_proof(
            db,
            proof_id,
            context=context,
            verified_by=verified_by,
            review_notes=review_notes,
        ),
    )


def _reject_proof(
    db: Session,
    proof_id: str,
    *,
    context: CommandContext,
    verified_by: str,
    review_notes: str,
) -> PaymentProofResult:
    proof = db.scalar(
        select(PaymentProof)
        .where(PaymentProof.id == coerce_uuid(proof_id))
        .with_for_update()
    )
    if proof is None:
        raise _error("not_found", "Payment proof not found", proof_id=proof_id)
    if proof.status != PaymentProofStatus.submitted:
        raise _error(
            "already_reviewed",
            "Proof already reviewed",
            proof_id=proof_id,
            status=proof.status.value,
        )
    if not (review_notes or "").strip():
        raise _error(
            "rejection_reason_required",
            "A rejection reason is required",
            field="review_notes",
        )
    proof.status = PaymentProofStatus.rejected
    proof.verified_by = str(verified_by)
    proof.review_notes = review_notes.strip()
    _resolve_reviewer_confirmation(db, proof)
    _audit(
        db,
        context,
        action="reject",
        proof=proof,
        actor_id=verified_by,
        metadata={
            "claimed_amount": str(proof.amount),
            "currency": proof.currency,
            "reference": proof.reference,
            "reason": proof.review_notes,
        },
    )
    _notify(db, proof, approved=False)
    _emit_transition(
        db,
        context=context,
        proof=proof,
        event_type=EventType.payment_proof_rejected,
    )
    return PaymentProofResult.from_model(proof)


def _emit_transition(
    db: Session,
    *,
    context: CommandContext,
    proof: PaymentProof,
    event_type: EventType,
) -> None:
    emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "payment_proof_id": str(proof.id),
            "account_id": str(proof.account_id) if proof.account_id else None,
            "billing_account_id": (
                str(proof.billing_account_id) if proof.billing_account_id else None
            ),
            "status": proof.status.value,
            "amount": str(proof.amount),
            "verified_amount": (
                str(proof.verified_amount)
                if proof.verified_amount is not None
                else None
            ),
            "currency": proof.currency,
            "payment_id": str(proof.payment_id) if proof.payment_id else None,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
        subscriber_id=proof.account_id,
        account_id=proof.account_id,
    )


def _review_notification_fingerprint(proof: PaymentProof) -> str:
    return f"payment-proof-review:{proof.id}"


def _review_notification_event_type(proof: PaymentProof) -> str:
    return f"payment_proof_review_requested:{proof.id}"


def _queue_reviewer_confirmation(db: Session, proof: PaymentProof) -> None:
    """Queue the review work item in the proof command transaction."""
    from app.services import staff_notifications

    reference = (proof.reference or "").strip() or "not provided"
    duplicate_note = (
        " A matching submitted reference already exists; check for a duplicate."
        if find_duplicate_proofs(db, proof)
        else ""
    )
    body = (
        f"A bank-transfer receipt for {proof.currency} {proof.amount} is waiting "
        f"for confirmation. Reference: {reference}.{duplicate_note}"
    )
    result = staff_notifications.queue_permission_review_request(
        db,
        permission_key=_REVIEW_PERMISSION,
        fingerprint=_review_notification_fingerprint(proof),
        event_type=_review_notification_event_type(proof),
        title="Bank transfer receipt needs confirmation",
        body=body,
        target_url=f"/admin/billing/payment-proofs/{proof.id}",
        category="billing",
        source="payment_proofs",
        sla_entity_type=_REVIEW_ENTITY_TYPE,
        sla_entity_id=str(proof.id),
        sla_trigger=_REVIEW_SLA_TRIGGER,
    )
    if result.target_count == 0:
        logger.error(
            "payment-proof %s has no active reviewer with %s",
            proof.id,
            _REVIEW_PERMISSION,
        )


def _resolve_reviewer_confirmation(db: Session, proof: PaymentProof) -> None:
    """Close the shared review work item in the proof command transaction."""
    from app.services import staff_notifications

    staff_notifications.resolve_permission_review_request(
        db,
        fingerprint=_review_notification_fingerprint(proof),
        event_type=_review_notification_event_type(proof),
        sla_entity_type=_REVIEW_ENTITY_TYPE,
        sla_entity_id=str(proof.id),
        sla_trigger=_REVIEW_SLA_TRIGGER,
    )


def _audit(
    db: Session,
    context: CommandContext,
    *,
    action: str,
    proof: PaymentProof,
    actor_id: str | None,
    metadata: Mapping[str, object],
) -> None:
    """Stage review evidence in the proof command transaction."""
    from app.services import audit as audit_service

    audit_service.audit_events.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(actor_id or context.actor),
            action=action,
            entity_type="payment_proof",
            entity_id=str(proof.id),
            status_code=200,
            is_success=True,
            request_id=str(context.correlation_id),
            metadata_={
                **metadata,
                "command_id": str(context.command_id),
                "command_scope": context.scope,
                "command_reason": context.reason,
            },
        ),
    )


def _notify(db: Session, proof: PaymentProof, *, approved: bool) -> None:
    """Queue customer delivery evidence in the proof command transaction."""
    from app.models.notification import NotificationChannel
    from app.models.subscriber import Subscriber
    from app.schemas.notification import NotificationCreate
    from app.services.notification import notifications as notifications_svc

    # account_id is null for reseller consolidated proofs; notify the
    # submitting reseller user in that case. Drop null ids so we never
    # db.get(Subscriber, None).
    targets = {proof.account_id}
    if proof.submitted_by and proof.submitted_by != proof.account_id:
        targets.add(proof.submitted_by)
    targets.discard(None)
    if approved:
        subject = "Bank transfer confirmed"
        body = (
            f"Your bank transfer of {proof.currency} {proof.amount} has been "
            "confirmed and credited to the account."
        )
    else:
        subject = "Bank transfer could not be confirmed"
        body = (
            f"We could not confirm the bank transfer of {proof.currency} "
            f"{proof.amount}."
        )
        if proof.review_notes:
            body += f"\n\nReason: {proof.review_notes}"
    for sid in targets:
        subscriber = db.get(Subscriber, sid)
        if not subscriber or not subscriber.email:
            continue
        for channel in (NotificationChannel.push, NotificationChannel.email):
            notifications_svc.queue_customer_notification(
                db,
                NotificationCreate(
                    channel=channel,
                    subscriber_id=sid,
                    recipient=subscriber.email,
                    subject=subject,
                    body=body,
                    category="billing",
                    event_type="payment_proof_"
                    + ("verified" if approved else "rejected"),
                ),
            )
