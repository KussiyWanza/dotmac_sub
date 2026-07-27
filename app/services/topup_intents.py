"""Canonical top-up intent lifecycle transitions and evidence links.

``TopupIntent.status`` remains a legacy ``String(20)`` column, but this module
owns its value set and every lifecycle projection. Callers supply typed evidence
and route each write through ``set_topup_intent_status`` so:

* garbage / unknown values are rejected at the write boundary, and
* a terminal intent (``expired``/``canceled``) being completed by a **late but
  real** gateway payment is recorded (``topup_intent_terminal_recovery``) rather
  than silently flipped.

NOTE — deliberately NOT a terminal lock. A gateway payment can legitimately
arrive after the sweep expired the intent (or the customer started a replacement,
canceling the old one); refusing to complete it would drop real money. Double
crediting is already prevented by the per-intent ``completed_payment_id`` /
per-payment ``external_id`` idempotency in the completion paths.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingAccount,
    Payment,
    PaymentProvider,
    PaymentStatus,
    TopupIntent,
)
from app.models.domain_settings import SettingDomain
from app.services.billing import collection_account_directory
from app.services.billing._common import lock_account
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.locking import lock_for_update
from app.services.owner_commands import CommandContext
from app.services.settings_spec import resolve_values_atomic

logger = logging.getLogger(__name__)

DIRECT_TRANSFER_PROVIDER = "direct_bank_transfer"
COMPLETION_SCOPE = "topup-intent:complete"
EXPIRY_SCOPE = "topup-intent:expire"
_DIRECT_TRANSFER_SETTING_KEYS = ("direct_bank_transfer_instructions",)


class TopupIntentStatus(str, Enum):
    pending = "pending"
    submitted = "submitted"
    completed = "completed"
    expired = "expired"
    canceled = "canceled"
    # A charge the gateway declined. Distinct from ``canceled`` (the customer
    # walked away) and ``expired`` (we gave up waiting). The saved-card path was
    # already passing "failed" — it just was not a member, so the write raised
    # ValueError, the surrounding commit never ran, and the idempotency-key
    # release was rolled back: a declined card locked the customer out of
    # retrying with a different one.
    failed = "failed"


class TopupIntentCompletionSource(str, Enum):
    """Named callers allowed to project confirmed payment evidence."""

    account_credit_deposit = "account_credit_deposit"
    provider_webhook = "provider_webhook"
    customer_invoice_verify = "customer_invoice_verify"
    customer_legacy_topup_verify = "customer_legacy_topup_verify"
    gateway_reconciliation = "gateway_reconciliation"
    payment_proof_review = "payment_proof_review"
    payment_proof_reconciliation = "payment_proof_reconciliation"
    reseller_verify = "reseller_verify"


class TopupIntentExpirySource(str, Enum):
    """Named callers allowed to park abandoned gateway intents."""

    gateway_reconciliation = "gateway_reconciliation"


class GatewayTopupIntentFlow(str, Enum):
    """Gateway checkout flows whose durable trace is owned here."""

    invoice_payment = "invoice_payment"
    reseller_consolidated = "reseller_consolidated"


class TopupIntentChannel(str, Enum):
    """Canonical adapter channels recorded on an intent."""

    customer_selfcare = "customer_selfcare"
    reseller_selfcare = "reseller_selfcare"


class TopupIntentFailureSource(str, Enum):
    """Named evidence sources allowed to fail a pending intent."""

    saved_card_charge = "saved_card_charge"


class TopupIntentFailureReason(str, Enum):
    """Non-sensitive reason vocabulary for failed intent projection."""

    gateway_charge_failed = "gateway_charge_failed"


class DirectTransferProofResolutionOutcome(str, Enum):
    """Terminal proof observations admitted by the intent projection owner."""

    verified = "verified"
    rejected = "rejected"


class DirectTransferProofResolutionSource(str, Enum):
    """Named coordinators allowed to project a reviewed transfer proof."""

    payment_proof_review = "payment_proof_review"
    payment_proof_reconciliation = "payment_proof_reconciliation"


_VALID_TOPUP_STATUSES: frozenset[str] = frozenset(s.value for s in TopupIntentStatus)
_TOPUP_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        TopupIntentStatus.expired.value,
        TopupIntentStatus.canceled.value,
        # A declined charge that later settles is a late recovery worth seeing,
        # exactly like the other two.
        TopupIntentStatus.failed.value,
    }
)


class TopupIntentError(DomainError, ValueError):
    """Stable rejection from the top-up intent lifecycle owner."""


def _error(suffix: str, message: str, **details: object) -> TopupIntentError:
    return TopupIntentError(
        code=f"financial.topup_intents.{suffix}",
        message=message,
        details=details,
    )


class DirectTransferAccountMapping(TypedDict):
    """Exact compatibility shape consumed by web and API adapters."""

    id: str
    enabled: str
    bank_name: str
    account_name: str
    account_number: str
    sort_code: str


class DirectTransferAdapterSettings(TypedDict):
    """Typed legacy settings projection backed by canonical configuration."""

    direct_bank_transfer_enabled: str
    direct_bank_transfer_bank_name: str
    direct_bank_transfer_account_name: str
    direct_bank_transfer_account_number: str
    direct_bank_transfer_sort_code: str
    direct_bank_transfer_instructions: str
    direct_bank_transfer_accounts_list: list[DirectTransferAccountMapping]


@dataclass(frozen=True, slots=True)
class DirectTransferBankAccountEvidence:
    """Exact configured bank account selected by the transfer submitter."""

    id: str
    bank_name: str
    account_name: str
    account_number: str
    sort_code: str = ""

    def __post_init__(self) -> None:
        for field in ("id", "bank_name", "account_name", "account_number"):
            value = getattr(self, field).strip()
            if not value:
                raise _error(
                    "invalid_bank_account_evidence",
                    "Selected bank-account evidence is incomplete",
                    field=field,
                )
            object.__setattr__(self, field, value)
        object.__setattr__(self, "sort_code", self.sort_code.strip())

    def to_metadata(self) -> dict[str, str]:
        return {
            "id": self.id,
            "bank_name": self.bank_name,
            "account_name": self.account_name,
            "account_number": self.account_number,
            "sort_code": self.sort_code,
        }


@dataclass(frozen=True, slots=True)
class DirectTransferConfiguredAccount:
    """One complete configured transfer destination and its availability."""

    id: str
    enabled: bool
    bank_name: str
    account_name: str
    account_number: str
    sort_code: str = ""

    def evidence(self) -> DirectTransferBankAccountEvidence:
        return DirectTransferBankAccountEvidence(
            id=self.id,
            bank_name=self.bank_name,
            account_name=self.account_name,
            account_number=self.account_number,
            sort_code=self.sort_code,
        )

    def to_dict(self) -> DirectTransferAccountMapping:
        evidence = self.evidence()
        return {
            "id": evidence.id,
            "enabled": "true" if self.enabled else "false",
            "bank_name": evidence.bank_name,
            "account_name": evidence.account_name,
            "account_number": evidence.account_number,
            "sort_code": evidence.sort_code,
        }


@dataclass(frozen=True, slots=True)
class DirectTransferConfiguration:
    """Canonical collection-account projection for direct bank transfer."""

    accounts: tuple[DirectTransferConfiguredAccount, ...]
    bank_name: str
    account_name: str
    account_number: str
    sort_code: str
    instructions: str

    @property
    def enabled_accounts(self) -> tuple[DirectTransferConfiguredAccount, ...]:
        return tuple(account for account in self.accounts if account.enabled)

    @property
    def enabled(self) -> bool:
        return bool(self.enabled_accounts)

    def to_adapter_settings(self) -> DirectTransferAdapterSettings:
        return {
            "direct_bank_transfer_enabled": "true" if self.enabled else "false",
            "direct_bank_transfer_bank_name": self.bank_name,
            "direct_bank_transfer_account_name": self.account_name,
            "direct_bank_transfer_account_number": self.account_number,
            "direct_bank_transfer_sort_code": self.sort_code,
            "direct_bank_transfer_instructions": self.instructions,
            "direct_bank_transfer_accounts_list": [
                account.to_dict() for account in self.accounts
            ],
        }


@dataclass(frozen=True, slots=True)
class TopupIntentProofLinkResult:
    """Immutable result of staging one direct-transfer proof link."""

    intent_id: UUID
    proof_id: UUID
    status: TopupIntentStatus


@dataclass(frozen=True, slots=True)
class StagedDirectTransferIntent:
    """Participant result consumed by the direct-transfer command owner."""

    intent: TopupIntent
    replaced_intent_ids: tuple[UUID, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class StageGatewayTopupIntentCommand:
    """Canonical gateway checkout facts admitted by a root coordinator."""

    flow: GatewayTopupIntentFlow
    reference: str
    provider_type: str
    currency: str
    requested_amount: Decimal
    expires_at: datetime
    provider_id: UUID | None = None
    capability_binding_id: UUID | None = None
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    invoice_id: UUID | None = None
    invoice_number: str | None = None
    reseller_id: UUID | None = None
    payment_method_id: UUID | None = None
    save_card: bool = False
    login_subscriber_id: UUID | None = None
    channel: TopupIntentChannel | None = None
    created_by: str | None = None


@dataclass(frozen=True, slots=True)
class StagedGatewayTopupIntent:
    """Flush-only gateway intent creation result."""

    intent: TopupIntent
    replayed: bool


@dataclass(frozen=True, slots=True)
class CompleteTopupIntentCommand:
    """Canonical payment evidence used to complete one intent projection."""

    intent_id: UUID
    payment_id: UUID
    source: TopupIntentCompletionSource


@dataclass(frozen=True, slots=True)
class ResolveDirectTransferProofCommand:
    """Exact reviewed-proof evidence used to terminalize one linked intent."""

    intent_id: UUID
    proof_id: UUID
    outcome: DirectTransferProofResolutionOutcome
    source: DirectTransferProofResolutionSource
    payment_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ExpireTopupIntentCommand:
    """Canonical time evidence used to park one abandoned gateway intent."""

    intent_id: UUID
    observed_at: datetime
    grace: timedelta
    source: TopupIntentExpirySource


@dataclass(frozen=True, slots=True)
class FailTopupIntentCommand:
    """Canonical evidence used to fail one pending saved-card intent."""

    intent_id: UUID
    source: TopupIntentFailureSource
    reason: TopupIntentFailureReason


@dataclass(frozen=True, slots=True)
class TopupIntentProjectionResult:
    """Immutable participant result for completion or expiry projection."""

    intent_id: UUID
    status: TopupIntentStatus
    payment_id: UUID | None
    changed: bool


@dataclass(frozen=True, slots=True)
class DirectTransferProofResolutionResult:
    """Immutable outcome of projecting one exact reviewed proof."""

    intent_id: UUID
    proof_id: UUID
    status: TopupIntentStatus
    payment_id: UUID | None
    changed: bool


def parse_direct_transfer_accounts(
    rows: Sequence[Mapping[str, object]],
) -> tuple[DirectTransferConfiguredAccount, ...]:
    """Normalize collection-account presentment rows for typed consumers."""

    accounts: list[DirectTransferConfiguredAccount] = []
    for item in rows:
        account_id = str(item.get("id") or "").strip()
        bank_name = str(item.get("bank_name") or "").strip()
        account_name = str(item.get("account_name") or "").strip()
        account_number = str(item.get("account_number") or "").strip()
        if not (account_id and bank_name and account_name and account_number):
            continue
        accounts.append(
            DirectTransferConfiguredAccount(
                id=account_id,
                enabled=str(item.get("enabled") or "").strip().lower()
                in {"1", "true", "yes", "on"},
                bank_name=bank_name,
                account_name=account_name,
                account_number=account_number,
                sort_code=str(item.get("sort_code") or "").strip(),
            )
        )
    return tuple(accounts)


def direct_transfer_configuration(db: Session) -> DirectTransferConfiguration:
    """Resolve configured collection-account destinations."""

    resolved = resolve_values_atomic(
        db,
        SettingDomain.billing,
        list(_DIRECT_TRANSFER_SETTING_KEYS),
    )
    settings: dict[str, object] = {
        key: str(resolved.get(key) or "").strip()
        for key in _DIRECT_TRANSFER_SETTING_KEYS
    }
    accounts = parse_direct_transfer_accounts(
        collection_account_directory.enabled_transfer_accounts(db)
    )
    primary = accounts[0] if accounts else None
    return DirectTransferConfiguration(
        accounts=accounts,
        bank_name=primary.bank_name if primary else "",
        account_name=primary.account_name if primary else "",
        account_number=primary.account_number if primary else "",
        sort_code=primary.sort_code if primary else "",
        instructions=str(settings["direct_bank_transfer_instructions"]),
    )


def set_topup_intent_status(
    intent: TopupIntent, new_status: TopupIntentStatus | str, *, source: str
) -> bool:
    """Validated write to ``TopupIntent.status``. Returns True if it changed.

    Rejects unknown values. Allows every transition (money safety — see module
    docstring) but emits ``topup_intent_terminal_recovery`` when a terminal
    intent is completed, so late-payment recoveries are observable.
    """
    raw = (
        new_status.value
        if isinstance(new_status, TopupIntentStatus)
        else str(new_status).strip()
    )
    if raw not in _VALID_TOPUP_STATUSES:
        raise _error(
            "invalid_status",
            "Invalid top-up intent status",
            status=str(new_status),
        )
    current = intent.status
    if current == raw:
        return False
    if current in _TOPUP_TERMINAL_STATUSES and raw == TopupIntentStatus.completed.value:
        logger.warning(
            "topup_intent_terminal_recovery",
            extra={
                "event": "topup_intent_terminal_recovery",
                "intent_id": str(getattr(intent, "id", None)),
                "from": current,
                "to": raw,
                "source": source,
            },
        )
    intent.status = raw
    return True


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def lock_topup_intent_scope(db: Session, intent_id: UUID) -> TopupIntent:
    """Lock the canonical account or billing-account scope, then the intent."""
    initial = db.get(TopupIntent, intent_id)
    if initial is None:
        raise _error(
            "not_found",
            "Top-up intent was not found",
            intent_id=str(intent_id),
        )
    if initial.account_id is not None:
        lock_account(db, str(initial.account_id))
    elif initial.billing_account_id is not None:
        account = lock_for_update(db, BillingAccount, initial.billing_account_id)
        if account is None:
            raise _error(
                "billing_account_not_found",
                "Top-up intent billing account was not found",
                intent_id=str(intent_id),
                billing_account_id=str(initial.billing_account_id),
            )
    else:
        raise _error(
            "scope_missing",
            "Top-up intent has no account scope",
            intent_id=str(intent_id),
        )
    intent = lock_for_update(db, TopupIntent, intent_id)
    if intent is None:
        raise _error(
            "not_found",
            "Top-up intent was not found",
            intent_id=str(intent_id),
        )
    return intent


def _gateway_intent_metadata(
    command: StageGatewayTopupIntentCommand,
) -> dict[str, str]:
    metadata = {"payment_flow": command.flow.value}
    if command.provider_id is not None:
        metadata["provider_id"] = str(command.provider_id)
    if command.capability_binding_id is not None:
        metadata["capability_binding_id"] = str(command.capability_binding_id)
    if command.flow is GatewayTopupIntentFlow.invoice_payment:
        if command.account_id is None or command.invoice_id is None:
            raise _error(
                "gateway_scope_invalid",
                "Invoice gateway intent requires account and invoice identities",
            )
        metadata.update(
            {
                "invoice_id": str(command.invoice_id),
                "invoice_number": str(command.invoice_number or ""),
                "account_id": str(command.account_id),
            }
        )
        if command.payment_method_id is not None:
            metadata["payment_method_id"] = str(command.payment_method_id)
    elif command.flow is GatewayTopupIntentFlow.reseller_consolidated:
        if command.billing_account_id is None or command.reseller_id is None:
            raise _error(
                "gateway_scope_invalid",
                "Reseller gateway intent requires billing-account and reseller identities",
            )
        if command.save_card:
            metadata["save_card"] = "1"
            if command.login_subscriber_id is not None:
                metadata["login_subscriber_id"] = str(command.login_subscriber_id)
            else:
                metadata["reseller_card_id"] = str(command.reseller_id)
        if command.payment_method_id is not None:
            metadata["payment_method_id"] = str(command.payment_method_id)
    return metadata


def stage_gateway_topup_intent(
    db: Session,
    command: StageGatewayTopupIntentCommand,
    *,
    context: CommandContext,
) -> StagedGatewayTopupIntent:
    """Create or replay one locked invoice/reseller gateway checkout trace."""

    del context  # The root coordinator records correlation on the creation event.
    has_account = command.account_id is not None
    has_billing_account = command.billing_account_id is not None
    if has_account == has_billing_account:
        raise _error(
            "gateway_scope_invalid",
            "Gateway intent requires exactly one account scope",
        )
    reference = command.reference.strip()
    provider_type = command.provider_type.strip().lower()
    currency = command.currency.strip().upper()
    amount = round_money(to_decimal(command.requested_amount))
    channel = command.channel.value if command.channel is not None else None
    created_by = str(command.created_by or "").strip() or None
    expires_at = _as_utc(command.expires_at)
    if not reference or not provider_type:
        raise _error(
            "gateway_identity_invalid",
            "Gateway reference and provider are required",
        )
    if len(currency) != 3:
        raise _error(
            "gateway_currency_invalid",
            "Gateway currency must be a three-letter code",
            currency=currency,
        )
    if amount <= Decimal("0.00"):
        raise _error(
            "amount_non_positive",
            "Gateway intent amount must be positive",
        )
    if expires_at is None or expires_at <= datetime.now(UTC):
        raise _error(
            "gateway_expiry_invalid",
            "Gateway intent expiry must be in the future",
        )

    if command.account_id is not None:
        lock_account(db, str(command.account_id))
    else:
        assert command.billing_account_id is not None
        billing_account = lock_for_update(
            db, BillingAccount, command.billing_account_id
        )
        if billing_account is None:
            raise _error(
                "billing_account_not_found",
                "Gateway intent billing account was not found",
                billing_account_id=str(command.billing_account_id),
            )

    metadata = _gateway_intent_metadata(command)
    existing = db.scalar(
        select(TopupIntent).where(TopupIntent.reference == reference).with_for_update()
    )
    if existing is not None:
        same_identity = all(
            (
                existing.account_id == command.account_id,
                existing.billing_account_id == command.billing_account_id,
                existing.provider_id == command.provider_id,
                existing.capability_binding_id == command.capability_binding_id,
                existing.provider_type == provider_type,
                existing.currency == currency,
                round_money(existing.requested_amount) == amount,
                dict(existing.metadata_ or {}) == metadata,
            )
        )
        if not same_identity:
            raise _error(
                "gateway_reference_conflict",
                "Gateway reference is linked to different intent evidence",
                reference=reference,
            )
        return StagedGatewayTopupIntent(intent=existing, replayed=True)

    intent = TopupIntent(
        account_id=command.account_id,
        billing_account_id=command.billing_account_id,
        provider_id=command.provider_id,
        capability_binding_id=command.capability_binding_id,
        reference=reference,
        provider_type=provider_type,
        currency=currency,
        requested_amount=amount,
        status=TopupIntentStatus.pending.value,
        expires_at=expires_at,
        channel=channel,
        created_by=created_by,
        metadata_=metadata,
    )
    db.add(intent)
    db.flush()
    return StagedGatewayTopupIntent(intent=intent, replayed=False)


def stage_gateway_topup_intent_created_event(
    db: Session,
    *,
    intent: TopupIntent,
    context: CommandContext,
) -> None:
    """Stage one correlation-safe event for a newly created gateway intent."""

    emit_event(
        db,
        EventType.topup_intent_gateway_created,
        {
            "schema_version": 1,
            "intent_id": str(intent.id),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "billing_account_id": (
                str(intent.billing_account_id) if intent.billing_account_id else None
            ),
            "payment_flow": str((intent.metadata_ or {}).get("payment_flow") or ""),
            "provider_type": intent.provider_type,
            "reference": intent.reference,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
    )


def stage_topup_intent_failure(
    db: Session,
    command: FailTopupIntentCommand,
    *,
    context: CommandContext,
) -> TopupIntentProjectionResult:
    """Fail one pending saved-card intent without completing the transaction."""

    intent = lock_topup_intent_scope(db, command.intent_id)
    if intent.status == TopupIntentStatus.failed.value:
        return TopupIntentProjectionResult(
            intent_id=intent.id,
            status=TopupIntentStatus.failed,
            payment_id=intent.completed_payment_id,
            changed=False,
        )
    if intent.status != TopupIntentStatus.pending.value:
        raise _error(
            "invalid_transition",
            "Only a pending top-up intent can record a saved-card failure",
            intent_id=str(intent.id),
            status=intent.status,
        )
    set_topup_intent_status(
        intent, TopupIntentStatus.failed, source=command.source.value
    )
    db.add(intent)
    emit_event(
        db,
        EventType.topup_intent_failed,
        {
            "schema_version": 1,
            "intent_id": str(intent.id),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "billing_account_id": (
                str(intent.billing_account_id) if intent.billing_account_id else None
            ),
            "source": command.source.value,
            "reason_code": command.reason.value,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
    )
    return TopupIntentProjectionResult(
        intent_id=intent.id,
        status=TopupIntentStatus.failed,
        payment_id=intent.completed_payment_id,
        changed=True,
    )


def _validate_completion_payment(
    db: Session,
    *,
    intent: TopupIntent,
    payment_id: UUID,
) -> Payment:
    payment = lock_for_update(db, Payment, payment_id)
    if payment is None or not payment.is_active:
        raise _error(
            "payment_not_found",
            "Completed payment evidence was not found",
            payment_id=str(payment_id),
        )
    if payment.status != PaymentStatus.succeeded:
        raise _error(
            "payment_not_succeeded",
            "Top-up intent completion requires succeeded payment evidence",
            payment_id=str(payment.id),
            payment_status=payment.status.value,
        )
    if intent.account_id is not None and payment.account_id != intent.account_id:
        raise _error(
            "payment_scope_mismatch",
            "Payment does not belong to the top-up intent account",
            intent_id=str(intent.id),
            payment_id=str(payment.id),
        )
    if (
        intent.billing_account_id is not None
        and payment.billing_account_id != intent.billing_account_id
    ):
        raise _error(
            "payment_scope_mismatch",
            "Payment does not belong to the top-up intent billing account",
            intent_id=str(intent.id),
            payment_id=str(payment.id),
        )
    if payment.currency.upper() != intent.currency.upper():
        raise _error(
            "payment_currency_mismatch",
            "Payment currency does not match the top-up intent",
            intent_id=str(intent.id),
            payment_id=str(payment.id),
            intent_currency=intent.currency,
            payment_currency=payment.currency,
        )
    if intent.provider_id is not None and payment.provider_id != intent.provider_id:
        raise _error(
            "payment_provider_mismatch",
            "Payment provider does not match the top-up intent",
            intent_id=str(intent.id),
            payment_id=str(payment.id),
        )
    if payment.provider_id is not None:
        provider = db.get(PaymentProvider, payment.provider_id)
        if (
            provider is not None
            and provider.provider_type.value != intent.provider_type
        ):
            raise _error(
                "payment_provider_mismatch",
                "Payment provider type does not match the top-up intent",
                intent_id=str(intent.id),
                payment_id=str(payment.id),
                intent_provider_type=intent.provider_type,
                payment_provider_type=provider.provider_type.value,
            )
    amount = round_money(to_decimal(payment.amount))
    if amount <= Decimal("0.00"):
        raise _error(
            "payment_amount_invalid",
            "Completed payment amount must be positive",
            payment_id=str(payment.id),
        )
    return payment


def stage_topup_intent_completion(
    db: Session,
    command: CompleteTopupIntentCommand,
    *,
    context: CommandContext,
) -> TopupIntentProjectionResult:
    """Project canonical succeeded-payment evidence onto one locked intent."""

    intent = lock_topup_intent_scope(db, command.intent_id)
    payment = _validate_completion_payment(
        db,
        intent=intent,
        payment_id=command.payment_id,
    )
    if (
        intent.completed_payment_id is not None
        and intent.completed_payment_id != payment.id
    ):
        raise _error(
            "completion_conflict",
            "Top-up intent is linked to different completed payment evidence",
            intent_id=str(intent.id),
            completed_payment_id=str(intent.completed_payment_id),
            supplied_payment_id=str(payment.id),
        )
    payment_external_id = str(payment.external_id or "").strip() or None
    if (
        intent.external_id
        and payment_external_id
        and intent.external_id != payment_external_id
    ):
        raise _error(
            "external_id_conflict",
            "Top-up intent has different provider transaction evidence",
            intent_id=str(intent.id),
            intent_external_id=intent.external_id,
            payment_external_id=payment_external_id,
        )

    previous_status = intent.status
    amount = round_money(to_decimal(payment.amount))
    completed_at = (
        _as_utc(payment.paid_at) or _as_utc(intent.completed_at) or datetime.now(UTC)
    )
    changed = any(
        (
            intent.completed_payment_id != payment.id,
            intent.external_id != payment_external_id,
            round_money(to_decimal(intent.actual_amount or 0)) != amount,
            _as_utc(intent.completed_at) != completed_at,
            intent.status != TopupIntentStatus.completed.value,
        )
    )
    intent.completed_payment_id = payment.id
    intent.external_id = payment_external_id
    intent.actual_amount = amount
    intent.completed_at = completed_at
    set_topup_intent_status(
        intent,
        TopupIntentStatus.completed,
        source=command.source.value,
    )
    db.add(intent)
    if changed:
        emit_event(
            db,
            EventType.topup_intent_completed,
            {
                "schema_version": 1,
                "topup_intent_id": str(intent.id),
                "payment_id": str(payment.id),
                "account_id": str(intent.account_id) if intent.account_id else None,
                "billing_account_id": (
                    str(intent.billing_account_id)
                    if intent.billing_account_id
                    else None
                ),
                "provider_type": intent.provider_type,
                "external_id": payment_external_id,
                "actual_amount": str(amount),
                "currency": intent.currency,
                "previous_status": previous_status,
                "status": intent.status,
                "source": command.source.value,
                "command_id": str(context.command_id),
                "correlation_id": str(context.correlation_id),
            },
            actor=context.actor,
            subscriber_id=intent.account_id,
            account_id=intent.account_id,
        )
    return TopupIntentProjectionResult(
        intent_id=intent.id,
        status=TopupIntentStatus.completed,
        payment_id=payment.id,
        changed=changed,
    )


def stage_direct_transfer_proof_resolution(
    db: Session,
    command: ResolveDirectTransferProofCommand,
    *,
    context: CommandContext,
) -> DirectTransferProofResolutionResult:
    """Project one exact terminal proof inside its review/reconciliation transaction."""

    intent = lock_topup_intent_scope(db, command.intent_id)
    if intent.provider_type != DIRECT_TRANSFER_PROVIDER:
        raise _error(
            "provider_mismatch",
            "Top-up intent is not a direct bank transfer",
            intent_id=str(intent.id),
            provider_type=intent.provider_type,
        )

    metadata = dict(intent.metadata_ or {})
    linked_proof_id = str(metadata.get("payment_proof_id") or "").strip()
    if linked_proof_id != str(command.proof_id):
        raise _error(
            "proof_resolution_link_mismatch",
            "Reviewed proof does not match the intent's exact proof evidence",
            intent_id=str(intent.id),
            proof_id=str(command.proof_id),
            linked_proof_id=linked_proof_id or None,
        )

    resolution = {
        "schema_version": 1,
        "payment_proof_id": str(command.proof_id),
        "outcome": command.outcome.value,
        "source": command.source.value,
    }
    existing_resolution = metadata.get("payment_proof_resolution")
    if existing_resolution is not None and existing_resolution != resolution:
        raise _error(
            "proof_resolution_conflict",
            "Intent already records different proof-resolution evidence",
            intent_id=str(intent.id),
            proof_id=str(command.proof_id),
        )

    if command.outcome is DirectTransferProofResolutionOutcome.verified:
        if command.payment_id is None:
            raise _error(
                "proof_resolution_payment_required",
                "Verified proof resolution requires canonical payment evidence",
                intent_id=str(intent.id),
                proof_id=str(command.proof_id),
            )
        completion_source = (
            TopupIntentCompletionSource.payment_proof_review
            if command.source
            is DirectTransferProofResolutionSource.payment_proof_review
            else TopupIntentCompletionSource.payment_proof_reconciliation
        )
        projection = stage_topup_intent_completion(
            db,
            CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=command.payment_id,
                source=completion_source,
            ),
            context=context,
        )
        metadata_changed = existing_resolution is None
        if metadata_changed:
            metadata["payment_proof_resolution"] = resolution
            intent.metadata_ = metadata
            db.add(intent)
        return DirectTransferProofResolutionResult(
            intent_id=intent.id,
            proof_id=command.proof_id,
            status=projection.status,
            payment_id=projection.payment_id,
            changed=projection.changed or metadata_changed,
        )

    if command.payment_id is not None:
        raise _error(
            "proof_resolution_payment_forbidden",
            "Rejected proof resolution cannot carry payment evidence",
            intent_id=str(intent.id),
            proof_id=str(command.proof_id),
        )
    if (
        intent.status == TopupIntentStatus.canceled.value
        and existing_resolution == resolution
    ):
        return DirectTransferProofResolutionResult(
            intent_id=intent.id,
            proof_id=command.proof_id,
            status=TopupIntentStatus.canceled,
            payment_id=intent.completed_payment_id,
            changed=False,
        )
    if intent.status != TopupIntentStatus.submitted.value:
        raise _error(
            "invalid_transition",
            "Only a submitted direct-transfer intent can record proof rejection",
            intent_id=str(intent.id),
            status=intent.status,
        )

    set_topup_intent_status(
        intent,
        TopupIntentStatus.canceled,
        source=command.source.value,
    )
    metadata["payment_proof_resolution"] = resolution
    metadata["canceled_reason"] = "payment_proof_rejected"
    intent.metadata_ = metadata
    db.add(intent)
    emit_event(
        db,
        EventType.topup_intent_direct_transfer_proof_rejected,
        {
            "schema_version": 1,
            "topup_intent_id": str(intent.id),
            "payment_proof_id": str(command.proof_id),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "status": intent.status,
            "provider_type": intent.provider_type,
            "reason_code": "payment_proof_rejected",
            "source": command.source.value,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
        subscriber_id=intent.account_id,
        account_id=intent.account_id,
    )
    return DirectTransferProofResolutionResult(
        intent_id=intent.id,
        proof_id=command.proof_id,
        status=TopupIntentStatus.canceled,
        payment_id=None,
        changed=True,
    )


def stage_topup_intent_expiry(
    db: Session,
    command: ExpireTopupIntentCommand,
    *,
    context: CommandContext,
) -> TopupIntentProjectionResult:
    """Park one abandoned gateway intent when canonical time evidence is due."""

    if command.grace < timedelta(0):
        raise _error(
            "expiry_grace_invalid",
            "Top-up intent expiry grace cannot be negative",
        )
    observed_at = _as_utc(command.observed_at)
    if observed_at is None:
        raise _error(
            "expiry_time_invalid", "Top-up intent observation time is required"
        )
    intent = lock_topup_intent_scope(db, command.intent_id)
    if intent.provider_type == DIRECT_TRANSFER_PROVIDER:
        raise _error(
            "provider_mismatch",
            "Direct-transfer intents are not gateway-expiry candidates",
            intent_id=str(intent.id),
        )
    if intent.completed_payment_id is not None:
        raise _error(
            "completion_conflict",
            "Completed top-up intent cannot be expired",
            intent_id=str(intent.id),
            completed_payment_id=str(intent.completed_payment_id),
        )
    if intent.status == TopupIntentStatus.expired.value:
        return TopupIntentProjectionResult(
            intent_id=intent.id,
            status=TopupIntentStatus.expired,
            payment_id=None,
            changed=False,
        )
    if intent.status != TopupIntentStatus.pending.value:
        raise _error(
            "invalid_transition",
            "Only a pending gateway intent can expire",
            intent_id=str(intent.id),
            status=intent.status,
        )
    expires_at = _as_utc(intent.expires_at)
    if expires_at is None or observed_at <= expires_at + command.grace:
        return TopupIntentProjectionResult(
            intent_id=intent.id,
            status=TopupIntentStatus.pending,
            payment_id=None,
            changed=False,
        )

    set_topup_intent_status(
        intent,
        TopupIntentStatus.expired,
        source=command.source.value,
    )
    db.add(intent)
    emit_event(
        db,
        EventType.topup_intent_expired,
        {
            "schema_version": 1,
            "topup_intent_id": str(intent.id),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "billing_account_id": (
                str(intent.billing_account_id) if intent.billing_account_id else None
            ),
            "provider_type": intent.provider_type,
            "expires_at": expires_at.isoformat(),
            "observed_at": observed_at.isoformat(),
            "grace_seconds": int(command.grace.total_seconds()),
            "status": intent.status,
            "source": command.source.value,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
        subscriber_id=intent.account_id,
        account_id=intent.account_id,
    )
    return TopupIntentProjectionResult(
        intent_id=intent.id,
        status=TopupIntentStatus.expired,
        payment_id=None,
        changed=True,
    )


def stage_invoice_direct_transfer_intent(
    db: Session,
    *,
    account_id: UUID,
    invoice_id: UUID,
    amount: Decimal | int | float | str,
    currency: str,
    reference: str,
    expires_at: datetime,
    idempotency_key: str,
    created_by: str,
    metadata: dict[str, object] | None = None,
    context: CommandContext,
) -> StagedDirectTransferIntent:
    """Stage one invoice transfer intent and explicitly retire older attempts."""

    normalized_amount = round_money(to_decimal(amount))
    if normalized_amount <= 0:
        raise _error(
            "amount_non_positive",
            "Direct-transfer amount must be greater than zero",
        )
    key = idempotency_key.strip()
    if not key:
        raise _error(
            "idempotency_key_invalid",
            "Direct-transfer intent idempotency evidence is required",
        )

    pending = tuple(
        db.scalars(
            select(TopupIntent)
            .where(TopupIntent.account_id == account_id)
            .where(TopupIntent.provider_type == DIRECT_TRANSFER_PROVIDER)
            .where(TopupIntent.status == TopupIntentStatus.pending.value)
            .order_by(TopupIntent.created_at.asc(), TopupIntent.id.asc())
            .with_for_update()
        ).all()
    )
    replay = next((intent for intent in pending if intent.idempotency_key == key), None)
    if replay is not None:
        replay_metadata = dict(replay.metadata_ or {})
        # The invoice identifies the request; the stored amount (and its WHT
        # snapshot) is immutable evidence. Replaying the same key for the same
        # invoice must return that snapshot even if the recomputed amount would
        # now differ (e.g. the WHT rate setting changed after creation). Only a
        # different invoice under the same key is a genuine conflict.
        if str(replay_metadata.get("invoice_id") or "") != str(invoice_id):
            raise _error(
                "idempotency_conflict",
                "Direct-transfer idempotency key was used with different details",
                intent_id=str(replay.id),
            )
        return StagedDirectTransferIntent(
            intent=replay,
            replaced_intent_ids=(),
            replayed=True,
        )

    intent = TopupIntent(
        account_id=account_id,
        reference=reference,
        provider_type=DIRECT_TRANSFER_PROVIDER,
        currency=currency,
        requested_amount=normalized_amount,
        status=TopupIntentStatus.pending.value,
        expires_at=expires_at,
        idempotency_key=key,
        channel=TopupIntentChannel.customer_selfcare.value,
        created_by=created_by,
        metadata_={
            "payment_method": "bank_transfer",
            "payment_flow": "invoice_payment",
            "invoice_id": str(invoice_id),
            **dict(metadata or {}),
        },
    )
    db.add(intent)
    db.flush()

    replaced_ids: list[UUID] = []
    for replaced in pending:
        set_topup_intent_status(
            replaced,
            TopupIntentStatus.canceled,
            source="direct_transfer_replacement",
        )
        replaced_metadata = dict(replaced.metadata_ or {})
        replaced_metadata.update(
            {
                "canceled_reason": "replaced_by_new_topup",
                "replaced_by_intent_id": str(intent.id),
            }
        )
        replaced.metadata_ = replaced_metadata
        db.add(replaced)
        replaced_ids.append(replaced.id)
        emit_event(
            db,
            EventType.topup_intent_direct_transfer_canceled,
            {
                "schema_version": 1,
                "topup_intent_id": str(replaced.id),
                "replacement_intent_id": str(intent.id),
                "account_id": str(account_id),
                "status": replaced.status,
                "reason": "replaced_by_new_topup",
                "command_id": str(context.command_id),
                "correlation_id": str(context.correlation_id),
            },
            actor=context.actor,
            subscriber_id=account_id,
            account_id=account_id,
        )

    stage_direct_transfer_intent_created_event(
        db,
        intent=intent,
        context=context,
        replaced_intent_ids=tuple(replaced_ids),
    )
    return StagedDirectTransferIntent(
        intent=intent,
        replaced_intent_ids=tuple(replaced_ids),
        replayed=False,
    )


def stage_direct_transfer_intent_created_event(
    db: Session,
    *,
    intent: TopupIntent,
    context: CommandContext,
    replaced_intent_ids: tuple[UUID, ...] = (),
) -> None:
    """Stage creation evidence for any canonical direct-transfer intent writer."""

    emit_event(
        db,
        EventType.topup_intent_direct_transfer_created,
        {
            "schema_version": 1,
            "topup_intent_id": str(intent.id),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "status": intent.status,
            "provider_type": intent.provider_type,
            "payment_flow": str((intent.metadata_ or {}).get("payment_flow") or ""),
            "replaced_intent_ids": [str(item) for item in replaced_intent_ids],
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
        subscriber_id=intent.account_id,
        account_id=intent.account_id,
    )


def lock_direct_transfer_intent_for_proof(
    db: Session,
    *,
    intent_id: UUID,
    account_id: UUID,
) -> TopupIntent:
    """Lock and validate the exact pending intent selected by an adapter."""

    intent = lock_for_update(db, TopupIntent, intent_id)
    if intent is None:
        raise _error(
            "not_found",
            "Direct-transfer top-up intent was not found",
            intent_id=str(intent_id),
        )
    if intent.account_id != account_id:
        raise _error(
            "account_mismatch",
            "Direct-transfer top-up intent does not belong to this account",
            intent_id=str(intent.id),
            account_id=str(account_id),
        )
    if intent.provider_type != DIRECT_TRANSFER_PROVIDER:
        raise _error(
            "provider_mismatch",
            "Top-up intent is not a direct bank transfer",
            intent_id=str(intent.id),
            provider_type=intent.provider_type,
        )
    if intent.status != TopupIntentStatus.pending.value:
        raise _error(
            "invalid_transition",
            "Direct-transfer top-up intent is no longer awaiting proof",
            intent_id=str(intent.id),
            status=intent.status,
        )
    return intent


def stage_direct_transfer_proof_submission(
    db: Session,
    *,
    intent: TopupIntent,
    proof_id: UUID,
    selected_bank_account: DirectTransferBankAccountEvidence,
    context: CommandContext,
) -> TopupIntentProofLinkResult:
    """Stage the intent transition and proof evidence in the caller transaction."""

    metadata = dict(intent.metadata_ or {})
    existing_proof_id = str(metadata.get("payment_proof_id") or "").strip()
    if existing_proof_id:
        raise _error(
            "proof_link_conflict",
            "Direct-transfer top-up intent already has payment-proof evidence",
            intent_id=str(intent.id),
            payment_proof_id=existing_proof_id,
        )
    if intent.status != TopupIntentStatus.pending.value:
        raise _error(
            "invalid_transition",
            "Direct-transfer top-up intent is no longer awaiting proof",
            intent_id=str(intent.id),
            status=intent.status,
        )

    set_topup_intent_status(
        intent,
        TopupIntentStatus.submitted,
        source="direct_transfer_proof_submission",
    )
    metadata["payment_proof_id"] = str(proof_id)
    metadata["selected_bank_account"] = selected_bank_account.to_metadata()
    intent.metadata_ = metadata
    db.add(intent)
    emit_event(
        db,
        EventType.topup_intent_direct_transfer_submitted,
        {
            "schema_version": 1,
            "topup_intent_id": str(intent.id),
            "payment_proof_id": str(proof_id),
            "account_id": str(intent.account_id) if intent.account_id else None,
            "status": intent.status,
            "provider_type": intent.provider_type,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
        },
        actor=context.actor,
        subscriber_id=intent.account_id,
        account_id=intent.account_id,
    )
    return TopupIntentProofLinkResult(
        intent_id=intent.id,
        proof_id=proof_id,
        status=TopupIntentStatus.submitted,
    )
