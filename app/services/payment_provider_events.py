"""Canonical payment-provider observation and processing owner.

Provider adapters supply verified observations. This owner persists their exact
normalized identity and provenance, proves replay equivalence, and composes the
named payment participants. It never interprets an administrative record as
permission to move money.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventSource,
    PaymentProviderEventStatus,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import round_money, to_decimal
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.payment_provider_events"
ADMINISTRATIVE_INGEST_SCOPE = "payment-provider-event:administrative-ingest"
WEBHOOK_PARTICIPANT_SCOPE = "payment-provider-event:verified-webhook-participant"
RECONCILIATION_PARTICIPANT_SCOPE = (
    "payment-provider-event:gateway-reconciliation-participant"
)
MAX_PAGE_SIZE = 200

_INGEST_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="payment-provider event ingestion",
    name="ingest_administrative_provider_event",
)


class PaymentProviderEventError(DomainError, ValueError):
    """Stable transport-neutral rejection from the provider-event owner."""


class ProviderEventOrderBy(StrEnum):
    received_at = "received_at"
    processed_at = "processed_at"


class ProviderEventOrderDirection(StrEnum):
    ascending = "asc"
    descending = "desc"


@dataclass(frozen=True, slots=True)
class PaymentProviderEventCommand:
    """Normalized provider observation admitted by a named trust adapter."""

    provider_id: UUID
    event_type: str
    payment_id: UUID | None = None
    invoice_id: UUID | None = None
    account_id: UUID | None = None
    billing_account_id: UUID | None = None
    external_id: str | None = None
    idempotency_key: str | None = None
    amount: Decimal | None = None
    provider_fee: Decimal = Decimal("0.00")
    net_amount: Decimal | None = None
    provider_reference: str | None = None
    topup_intent_id: UUID | None = None
    currency: str | None = None
    financial_effect: PaymentProviderEventFinancialEffect | None = None
    payload: dict[str, Any] | None = None
    observed_payment_status: PaymentStatus | None = None


@dataclass(frozen=True, slots=True)
class PaymentProviderEventQuery:
    provider_id: UUID | None = None
    payment_id: UUID | None = None
    invoice_id: UUID | None = None
    status: PaymentProviderEventStatus | None = None
    order_by: ProviderEventOrderBy = ProviderEventOrderBy.received_at
    order_direction: ProviderEventOrderDirection = (
        ProviderEventOrderDirection.descending
    )
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class PaymentProviderEventResult:
    """Immutable provider-event state returned across service boundaries."""

    id: UUID
    provider_id: UUID
    payment_id: UUID | None
    invoice_id: UUID | None
    event_type: str
    external_id: str | None
    idempotency_key: str | None
    source: PaymentProviderEventSource
    observation_digest: str | None
    observed_payment_status: PaymentStatus | None
    amount: Decimal | None
    provider_fee: Decimal
    net_amount: Decimal | None
    provider_reference: str | None
    currency: str | None
    financial_effect: PaymentProviderEventFinancialEffect
    status: PaymentProviderEventStatus
    payload: dict[str, Any] | None
    error_code: str | None
    error: str | None
    received_at: datetime
    processed_at: datetime | None
    replayed: bool = False

    @property
    def created_at(self) -> datetime:
        """Compatibility display alias; the canonical timestamp is received_at."""

        return self.received_at


def _error(suffix: str, message: str, **details: object) -> PaymentProviderEventError:
    return PaymentProviderEventError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=details,
    )


def _result(
    event: PaymentProviderEvent, *, replayed: bool = False
) -> PaymentProviderEventResult:
    return PaymentProviderEventResult(
        id=event.id,
        provider_id=event.provider_id,
        payment_id=event.payment_id,
        invoice_id=event.invoice_id,
        event_type=event.event_type,
        external_id=event.external_id,
        idempotency_key=event.idempotency_key,
        source=event.source,
        observation_digest=event.observation_digest,
        observed_payment_status=event.observed_payment_status,
        amount=round_money(event.amount) if event.amount is not None else None,
        provider_fee=round_money(event.provider_fee),
        net_amount=(
            round_money(event.net_amount) if event.net_amount is not None else None
        ),
        provider_reference=event.provider_reference,
        currency=event.currency,
        financial_effect=event.financial_effect,
        status=event.status,
        payload=dict(event.payload) if event.payload is not None else None,
        error_code=event.error_code,
        error=event.error,
        received_at=event.received_at,
        processed_at=event.processed_at,
        replayed=replayed,
    )


def _money(value: Decimal | None, *, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        normalized = round_money(value)
        if not normalized.is_finite():
            raise InvalidOperation
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise _error("money_invalid", f"Provider event {field} is invalid") from exc
    return normalized


def _currency(value: str | None, *, required: bool) -> str | None:
    normalized = str(value or "").strip().upper()
    if not normalized:
        if required:
            raise _error(
                "currency_required",
                "Provider event currency is required for monetary evidence",
            )
        return None
    if len(normalized) != 3 or not normalized.isalpha():
        raise _error(
            "currency_invalid",
            "Provider event currency must be a three-letter code",
        )
    return normalized


def _text(
    value: str | None,
    *,
    field: str,
    maximum: int,
    required: bool = False,
) -> str | None:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise _error("observation_invalid", f"Provider event {field} is required")
    if len(normalized) > maximum:
        raise _error(
            "observation_invalid",
            f"Provider event {field} exceeds its supported length",
            field=field,
        )
    return normalized or None


_STATUS_BY_EVENT_TYPE = {
    "payment.succeeded": PaymentStatus.succeeded,
    "charge.succeeded": PaymentStatus.succeeded,
    "charge.success": PaymentStatus.succeeded,
    "payment.failed": PaymentStatus.failed,
    "charge.failed": PaymentStatus.failed,
    "payment.refunded": PaymentStatus.refunded,
    "charge.refunded": PaymentStatus.refunded,
    "payment.reversed": PaymentStatus.reversed,
    "charge.reversed": PaymentStatus.reversed,
    "payment.canceled": PaymentStatus.canceled,
}

_FINANCIAL_EFFECT_BY_EVENT_TYPE = {
    "payment.refunded": PaymentProviderEventFinancialEffect.refund_confirmed,
    "charge.refunded": PaymentProviderEventFinancialEffect.refund_confirmed,
    "payment.reversed": PaymentProviderEventFinancialEffect.reversal_confirmed,
    "charge.reversed": PaymentProviderEventFinancialEffect.reversal_confirmed,
}

_SCOPE_BY_SOURCE = {
    PaymentProviderEventSource.administrative_ingest: ADMINISTRATIVE_INGEST_SCOPE,
    PaymentProviderEventSource.verified_webhook: WEBHOOK_PARTICIPANT_SCOPE,
    PaymentProviderEventSource.gateway_reconciliation: RECONCILIATION_PARTICIPANT_SCOPE,
}


@dataclass(frozen=True, slots=True)
class _NormalizedObservation:
    command: PaymentProviderEventCommand
    source: PaymentProviderEventSource
    event_type: str
    external_id: str | None
    idempotency_key: str
    amount: Decimal | None
    provider_fee: Decimal
    net_amount: Decimal | None
    provider_reference: str | None
    currency: str | None
    observed_status: PaymentStatus | None
    financial_effect: PaymentProviderEventFinancialEffect
    payload: dict[str, Any] | None
    digest: str


def _normalize(
    command: PaymentProviderEventCommand,
    *,
    source: PaymentProviderEventSource,
    context: CommandContext,
) -> _NormalizedObservation:
    expected_scope = _SCOPE_BY_SOURCE[source]
    if context.scope != expected_scope:
        raise _error(
            "command_scope_mismatch",
            "Provider event command scope does not match its admission source",
            expected_scope=expected_scope,
            actual_scope=context.scope,
        )
    event_type = _text(
        command.event_type,
        field="event_type",
        maximum=120,
        required=True,
    )
    assert event_type is not None
    external_id = _text(command.external_id, field="external_id", maximum=160)
    requested_key = _text(
        command.idempotency_key,
        field="idempotency_key",
        maximum=160,
    )
    amount = _money(command.amount, field="amount")
    provider_fee = _money(command.provider_fee, field="provider_fee")
    assert provider_fee is not None
    net_amount = _money(command.net_amount, field="net_amount")
    if amount is not None and amount <= Decimal("0.00"):
        raise _error("money_invalid", "Provider event amount must be positive")
    if provider_fee < Decimal("0.00"):
        raise _error("money_invalid", "Provider event fee cannot be negative")
    if amount is not None and provider_fee > amount:
        raise _error("money_invalid", "Provider event fee exceeds its gross amount")
    if net_amount is not None and net_amount <= Decimal("0.00"):
        raise _error("money_invalid", "Provider event net amount must be positive")
    if amount is not None and net_amount is not None and net_amount > amount:
        raise _error("money_invalid", "Provider event net amount exceeds gross amount")
    currency = _currency(
        command.currency,
        required=amount is not None
        or provider_fee != Decimal("0.00")
        or net_amount is not None,
    )
    mapped_status = _STATUS_BY_EVENT_TYPE.get(event_type)
    if (
        command.observed_payment_status is not None
        and mapped_status is not None
        and command.observed_payment_status is not mapped_status
    ):
        raise _error(
            "status_conflict",
            "Provider event type conflicts with its normalized payment status",
        )
    observed_status = command.observed_payment_status or mapped_status
    mapped_effect = _FINANCIAL_EFFECT_BY_EVENT_TYPE.get(event_type)
    if (
        command.financial_effect is not None
        and mapped_effect is not None
        and command.financial_effect is not mapped_effect
    ):
        raise _error(
            "financial_effect_conflict",
            "Provider event type conflicts with its normalized financial effect",
        )
    financial_effect = (
        mapped_effect
        or command.financial_effect
        or PaymentProviderEventFinancialEffect.none
    )
    if observed_status is PaymentStatus.refunded and financial_effect is not (
        PaymentProviderEventFinancialEffect.refund_confirmed
    ):
        raise _error(
            "financial_effect_required",
            "A refunded provider status requires normalized refund evidence",
        )
    if observed_status is PaymentStatus.reversed and financial_effect is not (
        PaymentProviderEventFinancialEffect.reversal_confirmed
    ):
        raise _error(
            "financial_effect_required",
            "A reversed provider status requires normalized reversal evidence",
        )
    if (
        financial_effect is not PaymentProviderEventFinancialEffect.none
        and source is not PaymentProviderEventSource.verified_webhook
    ):
        raise _error(
            "untrusted_financial_effect",
            "Refund and reversal evidence requires a signature-verified webhook",
        )
    if source is PaymentProviderEventSource.administrative_ingest and (
        observed_status is not None
        or financial_effect is not PaymentProviderEventFinancialEffect.none
    ):
        raise _error(
            "untrusted_financial_observation",
            "Administrative provider events cannot change payment state",
        )
    if source is not PaymentProviderEventSource.administrative_ingest and not (
        requested_key or external_id
    ):
        raise _error(
            "identity_required",
            "Verified provider events require an idempotency or transaction identity",
        )
    payload = dict(command.payload) if command.payload is not None else None
    provider_reference = _text(
        command.provider_reference,
        field="provider_reference",
        maximum=120,
    )
    provider_verified = source in {
        PaymentProviderEventSource.verified_webhook,
        PaymentProviderEventSource.gateway_reconciliation,
    }
    financial_observation = (
        observed_status is not None
        or financial_effect is not PaymentProviderEventFinancialEffect.none
    )
    digest_material = {
        "provider_id": str(command.provider_id),
        "verification_class": (
            "provider_verified" if provider_verified else source.value
        ),
        "event_type": (
            None if provider_verified and financial_observation else event_type
        ),
        "payment_id": str(command.payment_id) if command.payment_id else None,
        "invoice_id": str(command.invoice_id) if command.invoice_id else None,
        "account_id": str(command.account_id) if command.account_id else None,
        "billing_account_id": (
            str(command.billing_account_id) if command.billing_account_id else None
        ),
        "external_id": external_id,
        "idempotency_key": requested_key,
        "amount": f"{amount:.2f}" if amount is not None else None,
        "provider_fee": f"{provider_fee:.2f}",
        "net_amount": f"{net_amount:.2f}" if net_amount is not None else None,
        "provider_reference": provider_reference,
        "topup_intent_id": (
            str(command.topup_intent_id) if command.topup_intent_id else None
        ),
        "currency": currency,
        "observed_status": observed_status.value if observed_status else None,
        "financial_effect": financial_effect.value,
        "payload": None if provider_verified and financial_observation else payload,
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_material,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    idempotency_key = requested_key or (
        f"{source.value}:{external_id}" if external_id else f"administrative:{digest}"
    )
    if len(idempotency_key) > 160:
        idempotency_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return _NormalizedObservation(
        command=command,
        source=source,
        event_type=event_type,
        external_id=external_id,
        idempotency_key=idempotency_key,
        amount=amount,
        provider_fee=provider_fee,
        net_amount=net_amount,
        provider_reference=provider_reference,
        currency=currency,
        observed_status=observed_status,
        financial_effect=financial_effect,
        payload=payload,
        digest=digest,
    )


def _provider(db: Session, provider_id: UUID) -> PaymentProvider:
    provider = db.scalar(
        select(PaymentProvider)
        .where(PaymentProvider.id == provider_id)
        .with_for_update()
    )
    if provider is None or not provider.is_active:
        raise _error("provider_not_found", "Payment provider was not found")
    return provider


def _existing_event(
    db: Session,
    observation: _NormalizedObservation,
) -> PaymentProviderEvent | None:
    identities = [
        PaymentProviderEvent.idempotency_key == observation.idempotency_key,
    ]
    if observation.external_id is not None:
        identities.append(PaymentProviderEvent.external_id == observation.external_id)
    matches = tuple(
        db.scalars(
            select(PaymentProviderEvent)
            .where(PaymentProviderEvent.provider_id == observation.command.provider_id)
            .where(or_(*identities))
            .with_for_update()
        ).all()
    )
    if len(matches) > 1:
        raise _error(
            "identity_collision",
            "Provider event identities resolve to different canonical records",
        )
    return matches[0] if matches else None


def _legacy_replay_matches(
    event: PaymentProviderEvent, observation: _NormalizedObservation
) -> bool:
    return (
        event.event_type == observation.event_type
        and event.external_id == observation.external_id
        and event.amount == observation.amount
        and event.currency == observation.currency
        and event.financial_effect is observation.financial_effect
    )


def _admit_event(
    db: Session,
    observation: _NormalizedObservation,
) -> tuple[PaymentProviderEvent, bool]:
    existing = _existing_event(db, observation)
    if existing is not None:
        exact = (
            existing.observation_digest == observation.digest
            if existing.observation_digest is not None
            else _legacy_replay_matches(existing, observation)
        )
        if not exact:
            raise _error(
                "replay_conflict",
                "Provider event identity was reused with different evidence",
                event_id=str(existing.id),
            )
        resumable = existing.status is PaymentProviderEventStatus.pending or (
            existing.source is PaymentProviderEventSource.legacy_unknown
            and existing.payment_id is None
            and observation.observed_status is PaymentStatus.succeeded
            and observation.amount is not None
        )
        if not resumable:
            return existing, True
        existing.source = observation.source
        existing.observation_digest = observation.digest
        existing.observed_payment_status = observation.observed_status
        existing.provider_fee = observation.provider_fee
        existing.net_amount = observation.net_amount
        existing.provider_reference = observation.provider_reference
        existing.payload = observation.payload
        existing.error_code = None
        existing.error = None
        return existing, False

    event = PaymentProviderEvent(
        provider_id=observation.command.provider_id,
        payment_id=observation.command.payment_id,
        invoice_id=observation.command.invoice_id,
        event_type=observation.event_type,
        external_id=observation.external_id,
        idempotency_key=observation.idempotency_key,
        source=observation.source,
        observation_digest=observation.digest,
        observed_payment_status=observation.observed_status,
        amount=observation.amount,
        provider_fee=observation.provider_fee,
        net_amount=observation.net_amount,
        provider_reference=observation.provider_reference,
        currency=observation.currency,
        financial_effect=observation.financial_effect,
        payload=observation.payload,
    )
    db.add(event)
    db.flush()
    return event, False


def _translate_participant_error(exc: Exception) -> PaymentProviderEventError | None:
    if isinstance(exc, PaymentProviderEventError):
        return exc
    if isinstance(exc, DomainError):
        return _error(
            "financial_consequence_rejected",
            exc.message,
            participant_error_code=exc.code,
        )
    status_code = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if isinstance(status_code, int) and isinstance(detail, str):
        return _error(
            "financial_consequence_rejected",
            detail,
            participant_status=status_code,
        )
    return None


def _invoice_net_amount(observation: _NormalizedObservation) -> Decimal:
    if observation.net_amount is None:
        raise _error(
            "net_amount_required",
            "Verified invoice settlement requires an explicit normalized net amount",
        )
    return observation.net_amount


def _stage_financial_consequences(
    db: Session,
    *,
    provider: PaymentProvider,
    observation: _NormalizedObservation,
    event: PaymentProviderEvent,
) -> None:
    from app.schemas.billing import (
        BillingAccountPaymentPreviewRequest,
        PaymentAllocationApply,
        PaymentAllocationConfirm,
        PaymentAllocationCreate,
        PaymentAllocationPreviewRequest,
        PaymentCreate,
    )
    from app.services.billing._common import (
        _validate_account,
        _validate_invoice_currency,
    )
    from app.services.billing.consolidated_payments import (
        ConsolidatedPaymentRefunds,
        ConsolidatedPaymentReversals,
        ConsolidatedPaymentSettlements,
        consolidated_settlement_key,
    )
    from app.services.billing.payments import (
        PaymentAllocations,
        PaymentProviderFeeObservationCommand,
        PaymentReversals,
        Payments,
        Refunds,
    )

    command = observation.command
    invoice = db.get(Invoice, command.invoice_id) if command.invoice_id else None
    if command.invoice_id is not None and invoice is None:
        raise _error("invoice_not_found", "Provider event invoice was not found")
    if invoice and command.account_id and invoice.account_id != command.account_id:
        raise _error(
            "invoice_account_mismatch",
            "Provider event invoice does not belong to its account",
        )
    account_id = command.account_id or (invoice.account_id if invoice else None)
    billing_account_id = command.billing_account_id if account_id is None else None
    new_status = observation.observed_status
    payment: Payment | None = None
    created_settled = False
    if command.payment_id:
        payment = db.scalar(
            select(Payment).where(Payment.id == command.payment_id).with_for_update()
        )
        if payment is None:
            raise _error("payment_not_found", "Provider event payment was not found")
    elif observation.external_id:
        payment = db.scalar(
            select(Payment)
            .where(Payment.external_id == observation.external_id)
            .where(
                or_(
                    Payment.provider_id == provider.id,
                    Payment.provider_id.is_(None),
                )
            )
            .order_by((Payment.provider_id == provider.id).desc())
            .with_for_update()
        )
    if (
        payment is not None
        and observation.source
        in {
            PaymentProviderEventSource.verified_webhook,
            PaymentProviderEventSource.gateway_reconciliation,
        }
        and new_status is PaymentStatus.succeeded
        and observation.external_id
        and observation.amount is not None
    ):
        payment = Payments.stage_provider_fee_observation(
            db,
            PaymentProviderFeeObservationCommand(
                payment_id=payment.id,
                provider_id=provider.id,
                external_id=observation.external_id,
                gross_amount=observation.amount,
                provider_fee=observation.provider_fee,
                currency=observation.currency or payment.currency,
            ),
        )
    if (
        payment is None
        and observation.amount is not None
        and (account_id or billing_account_id)
    ):
        if account_id is not None:
            _validate_account(db, str(account_id))
        currency = observation.currency or (invoice.currency if invoice else None)
        if currency is None:
            raise _error(
                "currency_required",
                "Provider event currency is required to create a payment",
            )
        if invoice:
            _validate_invoice_currency(invoice, currency)
        if new_status is PaymentStatus.succeeded:
            is_invoice_checkout = bool(
                invoice is not None
                and account_id is not None
                and observation.external_id
                and observation.provider_reference
            )
            if is_invoice_checkout:
                from app.services.provider_payment_settlements import (
                    VerifiedInvoiceSettlementCommand,
                    stage_verified_invoice_payment,
                )

                assert invoice is not None
                assert account_id is not None
                assert observation.external_id is not None
                assert observation.provider_reference is not None
                result = stage_verified_invoice_payment(
                    db,
                    VerifiedInvoiceSettlementCommand(
                        account_id=account_id,
                        invoice_id=invoice.id,
                        topup_intent_id=command.topup_intent_id,
                        provider_id=provider.id,
                        provider_reference=observation.provider_reference,
                        external_id=observation.external_id,
                        gross_amount=observation.amount,
                        provider_fee=observation.provider_fee,
                        net_amount=_invoice_net_amount(observation),
                        currency=currency,
                        memo=f"{provider.name} webhook event: {observation.event_type}",
                    ),
                )
                payment = result.payment
                created_settled = True
            allocations: list[PaymentAllocationApply] | None = None
            if (
                not is_invoice_checkout
                and invoice is not None
                and account_id is not None
            ):
                balance_due = round_money(to_decimal(invoice.balance_due or 0))
                if balance_due > Decimal("0.00"):
                    allocations = [
                        PaymentAllocationApply(
                            invoice_id=invoice.id,
                            amount=min(observation.amount, balance_due),
                        )
                    ]
            is_consolidated = billing_account_id is not None and account_id is None
            if not is_invoice_checkout and is_consolidated:
                payment = ConsolidatedPaymentSettlements.stage_settle_verified(
                    db,
                    str(billing_account_id),
                    BillingAccountPaymentPreviewRequest(
                        provider_id=provider.id,
                        amount=observation.amount,
                        provider_fee=observation.provider_fee,
                        currency=currency,
                        external_id=observation.external_id,
                        memo=(
                            f"{provider.name} webhook event: {observation.event_type}"
                        ),
                        allocations=None,
                        auto_allocate=False,
                    ),
                    idempotency_key=consolidated_settlement_key(
                        "provider-event", str(event.id)
                    ),
                    origin=PaymentSettlementOrigin.provider_event,
                ).payment
                created_settled = True
            elif not is_invoice_checkout:
                payment = Payments.stage_create(
                    db,
                    PaymentCreate(
                        account_id=account_id,
                        provider_id=provider.id,
                        amount=observation.amount,
                        provider_fee=observation.provider_fee,
                        currency=currency,
                        status=PaymentStatus.succeeded,
                        external_id=observation.external_id,
                        memo=(
                            f"{provider.name} webhook event: {observation.event_type}"
                        ),
                        allocations=allocations,
                    ),
                    origin=PaymentSettlementOrigin.provider_event,
                )
                created_settled = True
        else:
            payment = Payments.stage_create(
                db,
                PaymentCreate(
                    account_id=account_id,
                    billing_account_id=billing_account_id,
                    amount=observation.amount,
                    provider_fee=observation.provider_fee,
                    currency=currency,
                    provider_id=provider.id,
                    external_id=observation.external_id,
                    status=PaymentStatus.pending,
                    memo=f"{provider.name} observation: {observation.event_type}",
                ),
                auto_allocate=False,
                origin=PaymentSettlementOrigin.provider_event,
            )
    elif payment and command.invoice_id and invoice and not payment.allocations:
        if (
            payment.status is PaymentStatus.succeeded
            and observation.provider_reference
            and observation.external_id
            and account_id is not None
            and observation.amount is not None
        ):
            from app.services.provider_payment_settlements import (
                VerifiedInvoiceSettlementCommand,
                stage_verified_invoice_payment,
            )

            result = stage_verified_invoice_payment(
                db,
                VerifiedInvoiceSettlementCommand(
                    account_id=account_id,
                    invoice_id=invoice.id,
                    topup_intent_id=command.topup_intent_id,
                    provider_id=provider.id,
                    provider_reference=observation.provider_reference,
                    external_id=observation.external_id,
                    gross_amount=observation.amount,
                    provider_fee=observation.provider_fee,
                    net_amount=_invoice_net_amount(observation),
                    currency=observation.currency or invoice.currency,
                    memo=f"{provider.name} webhook event: {observation.event_type}",
                ),
            )
            payment = result.payment
        else:
            balance_due = round_money(to_decimal(invoice.balance_due or 0))
            alloc_amount = min(round_money(to_decimal(payment.amount)), balance_due)
            if (
                alloc_amount > Decimal("0.00")
                and payment.status is PaymentStatus.succeeded
            ):
                preview_request = PaymentAllocationPreviewRequest(
                    payment_id=payment.id,
                    invoice_id=invoice.id,
                    amount=alloc_amount,
                )
                preview = PaymentAllocations.preview(db, preview_request)
                key_material = ":".join(
                    [
                        str(provider.id),
                        observation.idempotency_key,
                        observation.external_id or "",
                        observation.event_type,
                        str(invoice.id),
                    ]
                )
                key = (
                    "provider-allocation-"
                    + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
                )
                PaymentAllocations.stage_confirm(
                    db,
                    PaymentAllocationConfirm(
                        payment_id=payment.id,
                        invoice_id=invoice.id,
                        amount=alloc_amount,
                        preview_fingerprint=preview.fingerprint,
                        idempotency_key=key,
                    ),
                )
            elif alloc_amount > Decimal("0.00"):
                PaymentAllocations.stage_record_intent(
                    db,
                    PaymentAllocationCreate(
                        payment_id=payment.id,
                        invoice_id=invoice.id,
                        amount=alloc_amount,
                        memo=f"{provider.name} invoice intent",
                    ),
                )

    allocation_invoice_id = command.invoice_id
    if allocation_invoice_id is None and payment and payment.allocations:
        allocation_invoice_id = payment.allocations[0].invoice_id
    event.payment_id = payment.id if payment else None
    event.invoice_id = allocation_invoice_id
    if created_settled:
        event.status = PaymentProviderEventStatus.processed
        event.processed_at = datetime.now(UTC)
    elif new_status and payment:
        if new_status is PaymentStatus.refunded:
            if payment.billing_account_id is not None:
                ConsolidatedPaymentRefunds.stage_provider_event(
                    db,
                    payment_id=str(payment.id),
                    provider_event_id=event.id,
                )
            else:
                refund_result = Refunds.stage_provider_event_refund(
                    db,
                    payment_id=str(payment.id),
                    provider_event_id=event.id,
                )
                _stage_refund_posting(db, refund_result)
        elif new_status is PaymentStatus.reversed:
            if payment.billing_account_id is not None:
                ConsolidatedPaymentReversals.stage_provider_event(
                    db,
                    payment_id=str(payment.id),
                    provider_event_id=event.id,
                )
            else:
                reversal_result = PaymentReversals.stage_provider_event_reversal(
                    db,
                    payment_id=str(payment.id),
                    provider_event_id=event.id,
                )
                _stage_reversal_posting(db, reversal_result)
        elif new_status is PaymentStatus.succeeded and payment.billing_account_id:
            ConsolidatedPaymentSettlements.stage_settle_verified(
                db,
                str(payment.billing_account_id),
                BillingAccountPaymentPreviewRequest(
                    amount=payment.amount,
                    provider_fee=payment.provider_fee,
                    currency=payment.currency,
                    paid_at=payment.paid_at,
                    memo=payment.memo,
                    payment_method_id=payment.payment_method_id,
                    payment_channel_id=payment.payment_channel_id,
                    collection_account_id=payment.collection_account_id,
                    provider_id=payment.provider_id,
                    external_id=payment.external_id,
                    allocations=[
                        PaymentAllocationApply(
                            invoice_id=allocation.invoice_id,
                            amount=allocation.amount,
                            memo=allocation.memo,
                        )
                        for allocation in payment.allocations
                        if allocation.is_active
                    ]
                    or None,
                    auto_allocate=payment.auto_allocate_on_settlement,
                ),
                idempotency_key=consolidated_settlement_key(
                    "provider-event", str(event.id)
                ),
                origin=PaymentSettlementOrigin.provider_event,
                existing_payment_id=str(payment.id),
            )
        else:
            Payments.stage_status_transition(
                db,
                str(payment.id),
                new_status,
                origin=PaymentSettlementOrigin.provider_event,
            )
        event.status = PaymentProviderEventStatus.processed
        event.processed_at = datetime.now(UTC)
    elif new_status and payment is None:
        event.status = PaymentProviderEventStatus.failed
        event.error_code = "payment_not_found"
        event.error = "Payment not found for provider event"
        event.processed_at = datetime.now(UTC)
    else:
        event.status = PaymentProviderEventStatus.processed
        event.processed_at = datetime.now(UTC)


def _record_processing_evidence(
    db: Session,
    *,
    event: PaymentProviderEvent,
    context: CommandContext,
) -> None:
    actor_namespace = context.actor.partition(":")[0]
    actor_type = {
        "api_key": AuditActorType.api_key,
        "system": AuditActorType.system,
        "user": AuditActorType.user,
    }.get(actor_namespace, AuditActorType.service)
    stage_audit_event(
        db,
        action="process",
        entity_type="payment_provider_event",
        entity_id=str(event.id),
        actor_type=actor_type,
        actor_id=context.actor,
        request_id=str(context.correlation_id),
        is_success=event.status is PaymentProviderEventStatus.processed,
        metadata={
            "owner": OWNER,
            "source": event.source.value,
            "provider_id": str(event.provider_id),
            "payment_id": str(event.payment_id) if event.payment_id else None,
            "invoice_id": str(event.invoice_id) if event.invoice_id else None,
            "observed_payment_status": (
                event.observed_payment_status.value
                if event.observed_payment_status
                else None
            ),
            "financial_effect": event.financial_effect.value,
            "processing_status": event.status.value,
            "error_code": event.error_code,
            "observation_digest": event.observation_digest,
            "command_id": str(context.command_id),
        },
    )
    event_type = (
        EventType.payment_provider_event_processed
        if event.status is PaymentProviderEventStatus.processed
        else EventType.payment_provider_event_failed
    )
    emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "aggregate_type": "payment_provider_event",
            "aggregate_id": str(event.id),
            "aggregate_version": event.observation_digest,
            "provider_id": str(event.provider_id),
            "payment_id": str(event.payment_id) if event.payment_id else None,
            "invoice_id": str(event.invoice_id) if event.invoice_id else None,
            "source": event.source.value,
            "observed_payment_status": (
                event.observed_payment_status.value
                if event.observed_payment_status
                else None
            ),
            "financial_effect": event.financial_effect.value,
            "processing_status": event.status.value,
            "error_code": event.error_code,
            "command_id": str(context.command_id),
            "correlation_id": str(context.correlation_id),
            "causation_id": (
                str(context.causation_id) if context.causation_id else None
            ),
        },
        actor=context.actor,
    )


def _stage_provider_event(
    db: Session,
    command: PaymentProviderEventCommand,
    *,
    source: PaymentProviderEventSource,
    context: CommandContext,
) -> PaymentProviderEventResult:
    observation = _normalize(command, source=source, context=context)
    provider = _provider(db, command.provider_id)
    event, replayed = _admit_event(db, observation)
    if replayed:
        return _result(event, replayed=True)
    if source is not PaymentProviderEventSource.administrative_ingest:
        try:
            _stage_financial_consequences(
                db,
                provider=provider,
                observation=observation,
                event=event,
            )
        except Exception as exc:
            translated = _translate_participant_error(exc)
            if translated is not None:
                raise translated from exc
            raise
    else:
        event.status = PaymentProviderEventStatus.processed
        event.processed_at = datetime.now(UTC)
    _record_processing_evidence(db, event=event, context=context)
    db.flush()
    return _result(event)


class PaymentProviderEvents:
    """Typed public facade for provider-event reads and commands."""

    @staticmethod
    def get(db: Session, event_id: UUID) -> PaymentProviderEventResult:
        event = db.get(PaymentProviderEvent, event_id)
        if event is None:
            raise _error("event_not_found", "Payment provider event was not found")
        return _result(event)

    @staticmethod
    def list(
        db: Session, query: PaymentProviderEventQuery
    ) -> tuple[PaymentProviderEventResult, ...]:
        if query.limit < 1 or query.limit > MAX_PAGE_SIZE or query.offset < 0:
            raise _error(
                "pagination_invalid",
                "Payment provider event pagination is outside supported bounds",
            )
        statement = select(PaymentProviderEvent)
        if query.provider_id is not None:
            statement = statement.where(
                PaymentProviderEvent.provider_id == query.provider_id
            )
        if query.payment_id is not None:
            statement = statement.where(
                PaymentProviderEvent.payment_id == query.payment_id
            )
        if query.invoice_id is not None:
            statement = statement.where(
                PaymentProviderEvent.invoice_id == query.invoice_id
            )
        if query.status is not None:
            statement = statement.where(PaymentProviderEvent.status == query.status)
        order_column = {
            ProviderEventOrderBy.received_at: PaymentProviderEvent.received_at,
            ProviderEventOrderBy.processed_at: PaymentProviderEvent.processed_at,
        }[query.order_by]
        statement = statement.order_by(
            order_column.desc()
            if query.order_direction is ProviderEventOrderDirection.descending
            else order_column.asc()
        )
        return tuple(
            _result(event)
            for event in db.scalars(
                statement.offset(query.offset).limit(query.limit)
            ).all()
        )

    @staticmethod
    def ingest(
        db: Session,
        command: PaymentProviderEventCommand,
        *,
        context: CommandContext,
    ) -> PaymentProviderEventResult:
        """Record a non-financial administrative observation atomically."""

        return execute_owner_command(
            db,
            definition=_INGEST_COMMAND,
            context=context,
            operation=lambda: _stage_provider_event(
                db,
                command,
                source=PaymentProviderEventSource.administrative_ingest,
                context=context,
            ),
        )

    @staticmethod
    def stage_verified_webhook_event(
        db: Session,
        command: PaymentProviderEventCommand,
        *,
        context: CommandContext,
    ) -> PaymentProviderEventResult:
        """Stage one signature-verified observation in its coordinator transaction."""

        return _stage_provider_event(
            db,
            command,
            source=PaymentProviderEventSource.verified_webhook,
            context=context,
        )

    @staticmethod
    def stage_verified_reconciliation_event(
        db: Session,
        command: PaymentProviderEventCommand,
        *,
        context: CommandContext,
    ) -> PaymentProviderEventResult:
        """Stage one gateway-verified observation in its coordinator transaction."""

        return _stage_provider_event(
            db,
            command,
            source=PaymentProviderEventSource.gateway_reconciliation,
            context=context,
        )


payment_provider_events = PaymentProviderEvents()


__all__ = [
    "ADMINISTRATIVE_INGEST_SCOPE",
    "MAX_PAGE_SIZE",
    "PaymentProviderEventCommand",
    "PaymentProviderEventError",
    "PaymentProviderEventQuery",
    "PaymentProviderEventResult",
    "PaymentProviderEvents",
    "ProviderEventOrderBy",
    "ProviderEventOrderDirection",
    "RECONCILIATION_PARTICIPANT_SCOPE",
    "WEBHOOK_PARTICIPANT_SCOPE",
    "payment_provider_events",
]


def _posting_instant(value):
    from datetime import UTC as _UTC

    if value is None:
        raise ValueError(
            "provider-event money record has no instant for posting provenance"
        )
    return value.replace(tzinfo=_UTC) if value.tzinfo is None else value


def _stage_refund_posting(db, result) -> None:
    """One shadow posting group per provider refund (ADR 0007 Phase 3).

    A partial refund is a NEW economic posting, never a reversal. Staged at
    the deciding owner root after the participant returns; shadow authority
    only.
    """
    if result.idempotent_replay:
        return
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db):
        # Legacy root without an owner command: the money transition
        # proceeds unposted and the verifier owns the gap as
        # producer_not_owner_wrapped debt. Never break the transition.
        return
    from decimal import Decimal as _Decimal

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
    from app.services.owner_commands import current_command_context

    refund = result.refund
    effects = [
        EffectInput(
            effect=PositionEffectKind.credit_refunded,
            amount=_Decimal(str(refund.amount)),
            payment_id=result.payment.id,
        )
    ]
    if result.credit_consumption_ledger_entry is not None:
        effects.append(
            EffectInput(
                effect=PositionEffectKind.customer_credit_consumed,
                amount=_Decimal(
                    str(abs(result.credit_consumption_ledger_entry.amount))
                ),
                payment_id=result.payment.id,
            )
        )
    stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=result.payment.account_id,
            currency=refund.currency,
            command_kind=PostingCommandKind.refund,
            producer_owner=PostingProducer.payment_provider_events,
            source_kind=PostingSourceKind.payment_refund,
            source_id=refund.id,
            occurred_at=_posting_instant(refund.created_at),
            effects=tuple(effects),
            idempotency_key=f"posting:payment_refund:{refund.id}",
        ),
        context=current_command_context(db),
    )


def _stage_reversal_posting(db, result) -> None:
    """A true reversal negates and links ONE complete original group.

    If the original settlement predates the forward-shadow (no group
    exists), nothing is staged: the verifier classifies the gap as
    original_group_missing evidence debt instead of inventing history.
    """
    if result.idempotent_replay:
        return
    from app.services.owner_commands import owner_command_active

    if not owner_command_active(db):
        return
    from sqlalchemy import select as _select

    from app.models.customer_subledger import (
        CustomerPostingGroup,
        PostingCommandKind,
        PostingProducer,
        PostingSourceKind,
    )
    from app.services.billing.customer_subledger import (
        StageReversalCommand,
        stage_reversal,
    )
    from app.services.owner_commands import current_command_context

    original = (
        db.execute(
            _select(CustomerPostingGroup).where(
                CustomerPostingGroup.source_kind == PostingSourceKind.payment.value,
                CustomerPostingGroup.source_id == result.payment.id,
                CustomerPostingGroup.command_kind != PostingCommandKind.reversal,
                CustomerPostingGroup.reverses_group_id.is_(None),
            )
        )
        .scalars()
        .first()
    )
    if original is None:
        return
    reversal = result.reversal
    stage_reversal(
        db,
        StageReversalCommand(
            original_group_id=original.id,
            producer_owner=PostingProducer.payment_provider_events,
            source_kind=PostingSourceKind.payment_reversal,
            source_id=reversal.id,
            occurred_at=_posting_instant(reversal.created_at),
            idempotency_key=f"posting:payment_reversal:{reversal.id}",
        ),
        context=current_command_context(db),
    )
