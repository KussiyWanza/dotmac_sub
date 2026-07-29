"""Deposit Account Credit lifecycle owner.

The customer flow and provider webhook/verify paths are adapters around this
service. A confirmed receipt is first recorded as exact unallocated payment
credit, then the canonical account-credit applicator consumes it for any debt
that became eligible after intent creation. The whole financial mutation is one
transaction and the deposit itself never grants prepaid service duration.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    AccountCreditApplicationPolicy,
    Invoice,
    Payment,
    PaymentSettlementOrigin,
    PaymentStatus,
    TopupAllocationPolicy,
    TopupIntent,
    TopupIntentPurpose,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import PaymentCreate
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.account_credit import (
    AccountCreditApplicationResult,
    AccountCreditApplications,
    eligible_invoices,
)
from app.services.billing.payments import Payments
from app.services.common import round_money, to_decimal
from app.services.events import emit_event
from app.services.events.types import AccountCreditFundingOrigin, EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.topup_intents import (
    DIRECT_TRANSFER_PROVIDER,
    CompleteTopupIntentCommand,
    TopupIntentChannel,
    TopupIntentCompletionSource,
    TopupIntentStatus,
    stage_topup_intent_completion,
)

PURPOSE = TopupIntentPurpose.account_credit_deposit.value
ALLOCATION_POLICY = TopupAllocationPolicy.credit_only.value
CREDIT_APPLICATION_POLICY = AccountCreditApplicationPolicy.pay_eligible_invoices.value
POLICY_VERSION = 1
SUPPORTED_CURRENCY = "NGN"
SETTLEMENT_SCOPE = "account-credit-deposit:settle"
SETTLEMENT_PARTICIPANT_SCOPE = "account-credit-deposit:settle-participant"

_SETTLE_COMMAND = OwnerCommandDefinition(
    owner="financial.account_credit_deposits",
    concern="verified Deposit Account Credit settlement command",
    name="settle_verified_account_credit_deposit",
)


class AccountCreditDepositSettlementSource(str, Enum):
    """Named observation paths allowed to settle a deposit intent."""

    customer_gateway_verify = "customer_gateway_verify"
    provider_webhook = "provider_webhook"
    gateway_reconciliation = "gateway_reconciliation"
    payment_proof = "payment_proof"


_SETTLEMENT_ORIGIN_BY_SOURCE = {
    AccountCreditDepositSettlementSource.customer_gateway_verify: (
        PaymentSettlementOrigin.provider_event
    ),
    AccountCreditDepositSettlementSource.provider_webhook: (
        PaymentSettlementOrigin.provider_event
    ),
    AccountCreditDepositSettlementSource.gateway_reconciliation: (
        PaymentSettlementOrigin.provider_event
    ),
    AccountCreditDepositSettlementSource.payment_proof: (
        PaymentSettlementOrigin.manual
    ),
}


class DepositEligibilityError(ValueError):
    """Named domain error suitable for web and mobile adapters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActiveDepositPhase(str, Enum):
    """Customer-facing meaning of one active blocking deposit request."""

    awaiting_receipt = "awaiting_receipt"
    awaiting_provider_confirmation = "awaiting_provider_confirmation"
    under_review = "under_review"
    receipt_rejected = "receipt_rejected"


class ActiveDepositNextAction(str, Enum):
    """Closed action vocabulary supplied to customer-facing adapters."""

    upload_receipt = "upload_receipt"
    wait_for_provider = "wait_for_provider"
    wait_for_review = "wait_for_review"
    contact_support = "contact_support"


@dataclass(frozen=True, slots=True)
class ActiveDepositRequest:
    """Typed read model for the active Deposit Account Credit request."""

    intent_id: uuid.UUID
    phase: ActiveDepositPhase
    next_action: ActiveDepositNextAction
    provider_type: str
    reference: str
    amount: Decimal
    currency: str
    created_at: datetime
    expires_at: datetime | None
    observed_at: datetime
    rejection_reason: str | None = None


@dataclass(frozen=True)
class DepositPreview:
    account_id: uuid.UUID
    currency: str
    current_account_credit: Decimal
    requested_deposit: Decimal
    eligible_invoice_count: int
    invoice_applications: tuple[DepositPreviewInvoiceApplication, ...]
    total_applied_to_invoices: Decimal
    total_outstanding_after_application: Decimal
    remaining_account_credit: Decimal
    projected_available_credit: Decimal
    allocation_policy: str
    credit_application_policy: str
    policy_version: int
    fingerprint: str


@dataclass(frozen=True)
class DepositPreviewInvoiceApplication:
    invoice_id: uuid.UUID
    invoice_number: str | None
    currency: str
    amount_applied: Decimal
    outstanding_after_application: Decimal


@dataclass(frozen=True, slots=True)
class SettleAccountCreditDepositCommand:
    """Typed verified receipt evidence admitted by settlement owners."""

    intent_id: uuid.UUID
    provider_type: str
    external_transaction_id: str
    amount: Decimal
    currency: str
    provider_intent_id: uuid.UUID
    source: AccountCreditDepositSettlementSource
    provider_fee: Decimal = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class StagedDepositSettlement:
    """Flush-only result for a wider caller-owned financial transaction."""

    intent: TopupIntent
    payment: Payment
    application: AccountCreditApplicationResult
    already_recorded: bool


@dataclass(frozen=True, slots=True)
class DepositSettlementResult:
    """Immutable public result returned after the root command commits."""

    intent_id: uuid.UUID
    payment_id: uuid.UUID
    account_id: uuid.UUID
    amount: Decimal
    provider_fee: Decimal
    currency: str
    applied_amount: Decimal
    allocation_ids: tuple[str, ...]
    remaining_account_credit: Decimal
    already_recorded: bool


def _fingerprint(
    *,
    account_id: uuid.UUID,
    amount: Decimal,
    currency: str,
    current_credit: Decimal,
    invoice_inputs: list[dict[str, str]],
) -> str:
    encoded = json.dumps(
        {
            "purpose": PURPOSE,
            "account_id": str(account_id),
            "amount": f"{amount:.2f}",
            "currency": currency,
            "current_account_credit": f"{current_credit:.2f}",
            "invoice_inputs": invoice_inputs,
            "allocation_policy": ALLOCATION_POLICY,
            "credit_application_policy": CREDIT_APPLICATION_POLICY,
            "policy_version": POLICY_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _invoice_preview_inputs(invoices: list[Invoice]) -> list[dict[str, str]]:
    return [
        {
            "invoice_id": str(invoice.id),
            "currency": str(invoice.currency or "").upper(),
            "balance_due": f"{round_money(to_decimal(invoice.balance_due or 0)):.2f}",
            "due_at": (
                invoice.due_at.astimezone(UTC).isoformat()
                if invoice.due_at is not None and invoice.due_at.tzinfo is not None
                else invoice.due_at.replace(tzinfo=UTC).isoformat()
                if invoice.due_at is not None
                else ""
            ),
            "created_at": (
                invoice.created_at.astimezone(UTC).isoformat()
                if invoice.created_at is not None
                and invoice.created_at.tzinfo is not None
                else invoice.created_at.replace(tzinfo=UTC).isoformat()
                if invoice.created_at is not None
                else ""
            ),
        }
        for invoice in invoices
    ]


def _preview_invoice_applications(
    invoices: list[Invoice],
    *,
    amount: Decimal,
    currency: str,
) -> tuple[tuple[DepositPreviewInvoiceApplication, ...], Decimal, Decimal]:
    remaining = round_money(amount)
    applications: list[DepositPreviewInvoiceApplication] = []
    total_applied = Decimal("0.00")
    total_outstanding = Decimal("0.00")

    for invoice in invoices:
        invoice_currency = str(invoice.currency or "").upper()
        balance_due = round_money(to_decimal(invoice.balance_due or 0))
        if balance_due <= Decimal("0.00") or invoice_currency != currency:
            applied = Decimal("0.00")
            outstanding = balance_due
        else:
            applied = min(remaining, balance_due)
            outstanding = round_money(balance_due - applied)
            remaining = round_money(remaining - applied)
        total_applied = round_money(total_applied + applied)
        total_outstanding = round_money(total_outstanding + outstanding)
        applications.append(
            DepositPreviewInvoiceApplication(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                currency=invoice_currency,
                amount_applied=applied,
                outstanding_after_application=outstanding,
            )
        )
    return tuple(applications), total_applied, total_outstanding


def _account(db: Session, account_id: uuid.UUID) -> Subscriber:
    account = db.get(Subscriber, account_id)
    if account is None:
        raise DepositEligibilityError(
            "deposit_account_not_found", "Billing account was not found"
        )
    if not account.is_active or account.status in {
        SubscriberStatus.disabled,
        SubscriberStatus.canceled,
    }:
        raise DepositEligibilityError(
            "deposit_account_inactive",
            "Deposit Account Credit is unavailable for this inactive account",
        )
    return account


def _pending_incompatible_intent(
    db: Session,
    account_id: uuid.UUID,
    *,
    observed_at: datetime | None = None,
) -> TopupIntent | None:
    candidates = db.scalars(
        select(TopupIntent)
        .where(TopupIntent.account_id == account_id)
        .where(
            TopupIntent.status.in_(
                [TopupIntentStatus.pending.value, TopupIntentStatus.submitted.value]
            )
        )
        .order_by(TopupIntent.created_at.desc())
    ).all()
    now = observed_at or datetime.now(UTC)
    for intent in candidates:
        expires_at = intent.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                continue
        flow = str((intent.metadata_ or {}).get("payment_flow") or "")
        if intent.purpose == PURPOSE or flow == "account_topup":
            return intent
    return None


def _rejected_proof_reason(db: Session, intent: TopupIntent) -> str | None:
    """Return the review note when this intent's receipt was rejected.

    Correlation is by the explicit link the submit path writes; the intent
    reference is the fallback because the link is missing on older rows. Returns
    None when no terminal rejection applies, so the caller keeps the normal
    under-review phase.
    """

    from app.models.payment_proof import PaymentProof, PaymentProofStatus

    proof: PaymentProof | None = None
    proof_id = (intent.metadata_ or {}).get("payment_proof_id")
    if proof_id:
        try:
            proof = db.get(PaymentProof, uuid.UUID(str(proof_id)))
        except (ValueError, TypeError):
            proof = None
    if proof is None and intent.reference:
        proof = db.scalars(
            select(PaymentProof)
            .where(PaymentProof.account_id == intent.account_id)
            .where(PaymentProof.reference == intent.reference)
            .order_by(PaymentProof.created_at.desc())
            .limit(1)
        ).first()
    if proof is None or proof.status is not PaymentProofStatus.rejected:
        return None
    note = (proof.review_notes or "").strip()
    return note or "Your bank transfer could not be confirmed."


def _existing_preview(db: Session, intent: TopupIntent) -> DepositPreview:
    """Return current display facts while preserving the authorized fingerprint."""
    assert intent.account_id is not None
    current_credit = round_money(
        get_account_credit_balance(db, str(intent.account_id), currency=intent.currency)
    )
    invoices = eligible_invoices(db, str(intent.account_id))
    applications, total_applied, outstanding_after = _preview_invoice_applications(
        invoices,
        amount=round_money(intent.requested_amount),
        currency=intent.currency,
    )
    remaining_account_credit = round_money(
        current_credit + round_money(intent.requested_amount) - total_applied
    )
    return DepositPreview(
        account_id=intent.account_id,
        currency=intent.currency,
        current_account_credit=current_credit,
        requested_deposit=round_money(intent.requested_amount),
        eligible_invoice_count=len(invoices),
        invoice_applications=applications,
        total_applied_to_invoices=total_applied,
        total_outstanding_after_application=outstanding_after,
        remaining_account_credit=remaining_account_credit,
        projected_available_credit=remaining_account_credit,
        allocation_policy=intent.allocation_policy or ALLOCATION_POLICY,
        credit_application_policy=(
            intent.credit_application_policy or CREDIT_APPLICATION_POLICY
        ),
        policy_version=intent.policy_version or POLICY_VERSION,
        fingerprint=intent.preview_fingerprint or "",
    )


class AccountCreditDeposits:
    """Own deposit eligibility, intent evidence, and atomic settlement."""

    @staticmethod
    def active_request(
        db: Session,
        *,
        account_id: uuid.UUID,
        observed_at: datetime | None = None,
    ) -> ActiveDepositRequest | None:
        """Resolve the blocking request phase and next valid customer action."""

        observed = observed_at or datetime.now(UTC)
        intent = _pending_incompatible_intent(
            db,
            account_id,
            observed_at=observed,
        )
        if intent is None:
            return None
        rejection_reason: str | None = None
        if intent.status == TopupIntentStatus.submitted.value:
            # A rejected proof used to leave the intent stranded here (fixed by
            # #1642, but pre-fix rows survive until repaired). Reporting that as
            # "under review" told the customer to wait for a decision that had
            # already been made against them, so read the proof before deciding.
            rejection_reason = _rejected_proof_reason(db, intent)
            if rejection_reason is not None:
                phase = ActiveDepositPhase.receipt_rejected
                next_action = ActiveDepositNextAction.contact_support
            else:
                phase = ActiveDepositPhase.under_review
                next_action = ActiveDepositNextAction.wait_for_review
        elif intent.provider_type == DIRECT_TRANSFER_PROVIDER:
            phase = ActiveDepositPhase.awaiting_receipt
            next_action = ActiveDepositNextAction.upload_receipt
        else:
            phase = ActiveDepositPhase.awaiting_provider_confirmation
            next_action = ActiveDepositNextAction.wait_for_provider
        return ActiveDepositRequest(
            intent_id=intent.id,
            phase=phase,
            next_action=next_action,
            provider_type=intent.provider_type,
            reference=intent.reference,
            amount=round_money(intent.requested_amount),
            currency=intent.currency,
            created_at=intent.created_at,
            expires_at=intent.expires_at,
            observed_at=observed,
            rejection_reason=rejection_reason,
        )

    @staticmethod
    def preview(
        db: Session,
        *,
        account_id: uuid.UUID,
        amount: Decimal | int | float | str,
        currency: str = SUPPORTED_CURRENCY,
        minimum: Decimal | int | float | str,
        maximum: Decimal | int | float | str,
        check_pending: bool = True,
    ) -> DepositPreview:
        _account(db, account_id)
        normalized_currency = str(currency or "").strip().upper()
        if normalized_currency != SUPPORTED_CURRENCY:
            raise DepositEligibilityError(
                "deposit_currency_unsupported",
                f"Deposit Account Credit supports {SUPPORTED_CURRENCY} only",
            )
        normalized_amount = round_money(to_decimal(amount))
        minimum_amount = round_money(to_decimal(minimum))
        maximum_amount = round_money(to_decimal(maximum))
        if normalized_amount < minimum_amount:
            raise DepositEligibilityError(
                "deposit_amount_below_minimum",
                f"Deposit amount must be at least ₦{minimum_amount:,.2f}",
            )
        if normalized_amount > maximum_amount:
            raise DepositEligibilityError(
                "deposit_amount_above_maximum",
                f"Deposit amount must not exceed ₦{maximum_amount:,.2f}",
            )
        invoices = eligible_invoices(db, str(account_id))
        if check_pending and _pending_incompatible_intent(db, account_id):
            raise DepositEligibilityError(
                "deposit_intent_already_pending",
                "A Deposit Account Credit request is already pending",
            )
        current_credit = round_money(
            get_account_credit_balance(
                db, str(account_id), currency=normalized_currency
            )
        )
        applications, total_applied, outstanding_after = _preview_invoice_applications(
            invoices,
            amount=normalized_amount,
            currency=normalized_currency,
        )
        remaining_account_credit = round_money(
            current_credit + normalized_amount - total_applied
        )
        fingerprint = _fingerprint(
            account_id=account_id,
            amount=normalized_amount,
            currency=normalized_currency,
            current_credit=current_credit,
            invoice_inputs=_invoice_preview_inputs(invoices),
        )
        return DepositPreview(
            account_id=account_id,
            currency=normalized_currency,
            current_account_credit=current_credit,
            requested_deposit=normalized_amount,
            eligible_invoice_count=len(invoices),
            invoice_applications=applications,
            total_applied_to_invoices=total_applied,
            total_outstanding_after_application=outstanding_after,
            remaining_account_credit=remaining_account_credit,
            projected_available_credit=remaining_account_credit,
            allocation_policy=ALLOCATION_POLICY,
            credit_application_policy=CREDIT_APPLICATION_POLICY,
            policy_version=POLICY_VERSION,
            fingerprint=fingerprint,
        )

    @staticmethod
    def stage_intent(
        db: Session,
        *,
        account_id: uuid.UUID,
        amount: Decimal | int | float | str,
        currency: str,
        minimum: Decimal | int | float | str,
        maximum: Decimal | int | float | str,
        reference: str,
        provider_type: str,
        provider_id: uuid.UUID | None,
        expires_at: datetime,
        idempotency_key: str,
        channel: TopupIntentChannel,
        created_by: str | None,
        expected_preview_fingerprint: str | None = None,
        capability_binding_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> tuple[TopupIntent, DepositPreview, bool]:
        key = str(idempotency_key or "").strip()
        if len(key) < 16 or len(key) > 120:
            raise DepositEligibilityError(
                "deposit_idempotency_invalid",
                "Deposit idempotency key must contain 16-120 characters",
            )
        lock_account(db, str(account_id))
        existing = db.scalar(
            select(TopupIntent).where(
                TopupIntent.account_id == account_id,
                TopupIntent.purpose == PURPOSE,
                TopupIntent.idempotency_key == key,
            )
        )
        if existing:
            if (
                round_money(existing.requested_amount)
                != round_money(to_decimal(amount))
                or existing.currency != str(currency).upper()
                or existing.provider_type != provider_type
                or existing.capability_binding_id != capability_binding_id
            ):
                raise DepositEligibilityError(
                    "deposit_idempotency_conflict",
                    "Deposit idempotency key was used with different details",
                )
            return existing, _existing_preview(db, existing), True

        preview = AccountCreditDeposits.preview(
            db,
            account_id=account_id,
            amount=amount,
            currency=currency,
            minimum=minimum,
            maximum=maximum,
        )
        expected_fingerprint = str(expected_preview_fingerprint or "").strip()
        if expected_fingerprint:
            if preview.fingerprint != expected_fingerprint:
                raise DepositEligibilityError(
                    "deposit_preview_stale",
                    "Deposit Account Credit preview changed; review the latest allocation preview",
                )
        intent = TopupIntent(
            account_id=account_id,
            provider_id=provider_id,
            capability_binding_id=capability_binding_id,
            purpose=PURPOSE,
            allocation_policy=ALLOCATION_POLICY,
            credit_application_policy=CREDIT_APPLICATION_POLICY,
            policy_version=POLICY_VERSION,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=key,
            channel=channel.value,
            created_by=created_by,
            reference=reference,
            provider_type=provider_type,
            currency=preview.currency,
            requested_amount=preview.requested_deposit,
            status=TopupIntentStatus.pending.value,
            expires_at=expires_at,
            metadata_=dict(metadata or {}),
        )
        db.add(intent)
        db.flush()
        return intent, preview, False

    @staticmethod
    def settle_verified(
        db: Session,
        command: SettleAccountCreditDepositCommand,
        *,
        context: CommandContext,
    ) -> DepositSettlementResult:
        """Settle verified receipt evidence in one owner-managed transaction."""

        return execute_owner_command(
            db,
            definition=_SETTLE_COMMAND,
            context=context,
            operation=lambda: AccountCreditDeposits._settle_result(
                db,
                command=command,
                context=context,
            ),
        )

    @staticmethod
    def _settle_result(
        db: Session,
        *,
        command: SettleAccountCreditDepositCommand,
        context: CommandContext,
    ) -> DepositSettlementResult:
        staged = AccountCreditDeposits.stage_verified_settlement(
            db,
            command,
            context=context,
        )
        if staged.intent.account_id is None:
            raise DepositEligibilityError(
                "deposit_intent_not_found", "Deposit intent has no billing account"
            )
        return DepositSettlementResult(
            intent_id=staged.intent.id,
            payment_id=staged.payment.id,
            account_id=staged.intent.account_id,
            amount=round_money(staged.payment.amount),
            provider_fee=round_money(staged.payment.provider_fee),
            currency=staged.payment.currency,
            applied_amount=round_money(staged.application.applied),
            allocation_ids=tuple(staged.application.allocation_ids),
            remaining_account_credit=round_money(
                get_account_credit_balance(
                    db,
                    str(staged.intent.account_id),
                    currency=staged.intent.currency,
                )
            ),
            already_recorded=staged.already_recorded,
        )

    @staticmethod
    def stage_verified_settlement(
        db: Session,
        command: SettleAccountCreditDepositCommand,
        *,
        context: CommandContext,
    ) -> StagedDepositSettlement:
        """Stage a verified deposit inside a wider caller-owned transaction."""

        initial = db.get(TopupIntent, command.intent_id)
        if initial is None or initial.account_id is None:
            raise DepositEligibilityError(
                "deposit_intent_not_found", "Deposit intent was not found"
            )
        lock_account(db, str(initial.account_id))
        intent = lock_for_update(db, TopupIntent, initial.id)
        if intent is None:
            raise DepositEligibilityError(
                "deposit_intent_not_found", "Deposit intent was not found"
            )
        if intent.account_id is None:
            raise DepositEligibilityError(
                "deposit_intent_not_found", "Deposit intent has no billing account"
            )
        if (
            intent.purpose != PURPOSE
            or intent.allocation_policy != ALLOCATION_POLICY
            or intent.credit_application_policy != CREDIT_APPLICATION_POLICY
            or intent.policy_version != POLICY_VERSION
        ):
            raise DepositEligibilityError(
                "deposit_contract_invalid", "Deposit intent policy is invalid"
            )
        _account(db, intent.account_id)
        amount = round_money(to_decimal(command.amount))
        provider_fee = round_money(to_decimal(command.provider_fee))
        if provider_fee < Decimal("0.00") or provider_fee > amount:
            raise DepositEligibilityError(
                "deposit_provider_fee_invalid",
                "Provider fee must be between zero and the confirmed amount",
            )
        credited_amount = round_money(amount - provider_fee)
        if credited_amount != round_money(intent.requested_amount):
            raise DepositEligibilityError(
                "deposit_amount_mismatch",
                "Provider net amount did not match the authorized deposit amount",
            )
        currency = command.currency.strip().upper()
        if currency != intent.currency:
            raise DepositEligibilityError(
                "deposit_currency_mismatch",
                "Provider currency did not match the authorized deposit currency",
            )
        provider_type = command.provider_type.strip().lower()
        external_transaction_id = command.external_transaction_id.strip()
        if not provider_type or not external_transaction_id:
            raise DepositEligibilityError(
                "deposit_provider_identity_invalid",
                "Provider and external transaction identities are required",
            )
        if len(external_transaction_id) > 120:
            raise DepositEligibilityError(
                "deposit_provider_identity_invalid",
                "External transaction identity is too long",
            )
        if provider_type != intent.provider_type:
            raise DepositEligibilityError(
                "deposit_provider_mismatch",
                "Provider did not match the authorized deposit provider",
            )
        if command.provider_intent_id != intent.id:
            raise DepositEligibilityError(
                "deposit_provider_correlation_mismatch",
                "Provider confirmation did not match the authorized deposit intent",
            )

        if intent.completed_payment_id:
            payment = db.get(Payment, intent.completed_payment_id)
            if payment is None or payment.account_id != intent.account_id:
                raise DepositEligibilityError(
                    "deposit_settlement_incomplete",
                    "Deposit settlement evidence is incomplete",
                )
            stage_topup_intent_completion(
                db,
                CompleteTopupIntentCommand(
                    intent_id=intent.id,
                    payment_id=payment.id,
                    source=TopupIntentCompletionSource.account_credit_deposit,
                ),
                context=context,
            )
            return StagedDepositSettlement(
                intent=intent,
                payment=payment,
                application=AccountCreditApplicationResult(
                    account_id=str(intent.account_id)
                ),
                already_recorded=True,
            )

        existing = db.scalar(
            select(Payment).where(
                Payment.provider_id == intent.provider_id,
                Payment.external_id == external_transaction_id,
                Payment.is_active.is_(True),
            )
        )
        if existing:
            if (
                existing.account_id != intent.account_id
                or round_money(existing.amount) != amount
                or round_money(existing.provider_fee) != provider_fee
                or existing.currency != currency
                or existing.status != PaymentStatus.succeeded
                or existing.settlement is None
                or round_money(existing.settlement.amount) != credited_amount
            ):
                raise DepositEligibilityError(
                    "deposit_provider_reference_conflict",
                    "Provider transaction is linked to different payment evidence",
                )
            payment = existing
            already_recorded = True
        elif intent.provider_id is not None:
            creation = Payments.stage_verified_provider_settlement(
                db,
                account_id=intent.account_id,
                provider_id=intent.provider_id,
                external_id=external_transaction_id,
                gross_amount=amount,
                provider_fee=provider_fee,
                net_amount=credited_amount,
                currency=currency,
                memo=(
                    f"{provider_type.replace('_', ' ').title()} "
                    "Deposit Account Credit "
                    f"ref: {intent.reference}"
                ),
            )
            payment = creation.payment
            already_recorded = creation.idempotent_replay
        else:
            creation = Payments.create_account_credit_deposit(
                db,
                PaymentCreate(
                    account_id=intent.account_id,
                    amount=amount,
                    provider_fee=provider_fee,
                    currency=currency,
                    status=PaymentStatus.succeeded,
                    provider_id=intent.provider_id,
                    external_id=external_transaction_id,
                    memo=(
                        f"{provider_type.replace('_', ' ').title()} "
                        "Deposit Account Credit "
                        f"ref: {intent.reference}"
                    ),
                    allocations=[],
                ),
                idempotency_key=f"account-credit-deposit-{intent.id}",
                origin=_SETTLEMENT_ORIGIN_BY_SOURCE[command.source],
                commit=False,
            )
            payment = creation.payment
            already_recorded = creation.idempotent_replay

        # Race policy: cash is accepted, then any invoice that appeared after
        # intent creation immediately consumes the evidenced credit.
        application = AccountCreditApplications.apply(db, str(intent.account_id))
        metadata = dict(intent.metadata_ or {})
        metadata.update(
            {
                "settlement_payment_id": str(payment.id),
                "application_allocation_ids": application.allocation_ids,
                "applied_amount": str(application.applied),
                "remaining_account_credit": str(
                    round_money(
                        get_account_credit_balance(
                            db, str(intent.account_id), currency=currency
                        )
                    )
                ),
            }
        )
        intent.metadata_ = metadata
        db.add(intent)
        stage_topup_intent_completion(
            db,
            CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=payment.id,
                source=TopupIntentCompletionSource.account_credit_deposit,
            ),
            context=context,
        )
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action="settle_account_credit_deposit",
                entity_type="topup_intent",
                entity_id=str(intent.id),
                metadata_={
                    "payment_id": str(payment.id),
                    "amount": str(amount),
                    "provider_fee": str(provider_fee),
                    "currency": currency,
                    "allocation_policy": intent.allocation_policy,
                    "credit_application_policy": intent.credit_application_policy,
                    "policy_version": intent.policy_version,
                    "application_allocation_ids": application.allocation_ids,
                    "access_consequence": (
                        "invoice_owner_rechecks_only_if_fully_funded"
                    ),
                    "settlement_source": command.source.value,
                    "command_id": str(context.command_id),
                    "correlation_id": str(context.correlation_id),
                },
            ),
        )
        emit_event(
            db,
            EventType.account_credit_deposited,
            {
                "schema_version": 1,
                "intent_id": str(intent.id),
                "payment_id": str(payment.id),
                "amount": str(amount),
                "provider_fee": str(provider_fee),
                "currency": currency,
                "applied_amount": str(application.applied),
                "allocation_ids": application.allocation_ids,
                "origin": AccountCreditFundingOrigin.account_credit_deposit.value,
                "source": command.source.value,
                "command_id": str(context.command_id),
                "correlation_id": str(context.correlation_id),
            },
            account_id=intent.account_id,
        )
        db.flush()
        return StagedDepositSettlement(
            intent=intent,
            payment=payment,
            application=application,
            already_recorded=already_recorded,
        )


__all__ = [
    "SETTLEMENT_PARTICIPANT_SCOPE",
    "SETTLEMENT_SCOPE",
    "AccountCreditDeposits",
    "AccountCreditDepositSettlementSource",
    "DepositEligibilityError",
    "DepositPreview",
    "DepositSettlementResult",
    "SettleAccountCreditDepositCommand",
    "StagedDepositSettlement",
]
