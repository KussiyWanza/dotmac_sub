"""Typed coordinator for payment consequences from verified inbox receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.billing import (
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentProviderEventStatus,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.integration_platform import IntegrationInbox
from app.services import billing as billing_service
from app.services.account_credit_deposits import (
    SETTLEMENT_PARTICIPANT_SCOPE,
    AccountCreditDeposits,
    AccountCreditDepositSettlementSource,
    DepositEligibilityError,
    SettleAccountCreditDepositCommand,
)
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.integrations import inbox as integration_inbox
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.payment_provider_events import (
    WEBHOOK_PARTICIPANT_SCOPE,
    PaymentProviderEventCommand,
    PaymentProviderEventError,
    PaymentProviderEventResult,
)
from app.services.topup_intents import (
    COMPLETION_SCOPE,
    CompleteTopupIntentCommand,
    TopupIntentCompletionSource,
    TopupIntentError,
    stage_topup_intent_completion,
)

PROCESS_SCOPE = "payment-webhook:process-claimed-receipt"
INTEGRATOR_PROCESS_SCOPE = "payment-webhook:process-integrator-settlement"

_PROCESS_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_webhooks",
    concern="billing consequence submission from verified receipts",
    name="process_claimed_payment_webhook",
)

_INTEGRATOR_PROCESS_COMMAND = OwnerCommandDefinition(
    owner="financial.payment_webhooks",
    concern="Integrator settlement observation projection",
    name="process_integrator_settlement",
)


class PaymentWebhookProvider(StrEnum):
    """Provider adapters with a versioned normalization contract."""

    PAYSTACK = "paystack"
    FLUTTERWAVE = "flutterwave"


# --- Paystack refund/dispute vocabulary -------------------------------------
#
# One explicit, named set per family. `_settlement_observation` and
# `identify_verified_payment_webhook` both dispatch off these sets rather than
# repeating string literals, so a future Paystack event type cannot silently
# reach either function's generic/default path by accident (the original bug
# this PR fixes) and so the two functions cannot drift out of sync with each
# other. `tests/architecture` pins non-vacuity: every member here produces
# behavior distinguishable from the informational default, and a synthetic
# unlisted event type does not.
_PAYSTACK_SETTLEMENT_EVENT_TYPES: frozenset[str] = frozenset({"charge.success"})

_PAYSTACK_REFUND_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "refund.processed",
        "refund.pending",
        "refund.processing",
        "refund.failed",
    }
)

_PAYSTACK_DISPUTE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "charge.dispute.create",
        "charge.dispute.resolve",
        "charge.dispute.remind",
    }
)

# Refund/dispute events are NEWLY recognized here; their receipt identity must
# be event-scoped (Defect D) because their payload's `id` (or, if Paystack
# ever adds one, `reference`) can equal the ORIGINAL charge's `charge.success`
# reference. An unscoped identity would make the inbox's tamper-collision
# detector treat the refund as a mismatched duplicate of the original charge
# receipt and quarantine the whole installation. `charge.success` keeps its
# existing unscoped format below, unchanged, so Paystack's retries of an
# already-processed event still resolve to the same receipt. Flutterwave's
# `charge.completed` identity is separately event-scoped below for a
# structurally different reason (multiple genuine transaction attempts
# sharing one merchant-supplied `tx_ref`, not an event-type collision); see
# `identify_verified_payment_webhook`'s Flutterwave branch.
_PAYSTACK_EVENT_SCOPED_IDENTITY_TYPES: frozenset[str] = (
    _PAYSTACK_REFUND_EVENT_TYPES | _PAYSTACK_DISPUTE_EVENT_TYPES
)

_PAYSTACK_RECOGNIZED_EVENT_TYPES: frozenset[str] = (
    _PAYSTACK_SETTLEMENT_EVENT_TYPES | _PAYSTACK_EVENT_SCOPED_IDENTITY_TYPES
)

# ASSUMPTION, unverified against live Paystack documentation/sandbox payloads
# (no network access in this environment): the dispute-resolution outcome is
# read from `data.resolution` (falling back to `data.status`), and the two
# values below are the ones believed to indicate the merchant lost/won. An
# unrecognized value raises loudly (`dispute_resolution_unrecognized`, dead
# lettered on first attempt) rather than guessing which way to move money.
# CONFIRM THESE VALUES against a real Paystack `charge.dispute.resolve`
# payload before relying on this in production.
_DISPUTE_RESOLUTION_MERCHANT_LOST: frozenset[str] = frozenset({"merchant-accepted"})
_DISPUTE_RESOLUTION_MERCHANT_WON: frozenset[str] = frozenset({"declined"})


class IntegratorSettlementKind(StrEnum):
    """Provider-neutral result the connector observed."""

    CAPTURE = "capture"
    CAPTURE_FAILED = "capture_failed"


class IntegratorSettlementArrival(StrEnum):
    """Which independently verified Integrator path produced the fact."""

    INGRESS = "ingress"
    POLL = "poll"


class PaymentWebhookError(DomainError, ValueError):
    """Stable rejection from verified payment-webhook processing."""


def _error(suffix: str, message: str, **details: object) -> PaymentWebhookError:
    return PaymentWebhookError(
        code=f"financial.payment_webhooks.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class PaymentWebhookReceiptIdentity:
    """Provider receipt identity needed by the integration inbox."""

    provider: PaymentWebhookProvider
    provider_event_id: str
    event_type: str
    # Prior, now-retired identity string the inbox should ALSO recognize as
    # "already processed" -- but only on an exact `payload_digest` match (see
    # `inbox.receive_verified`). Populated only when a provider's identity
    # CONSTRUCTION changed while it already had live receipts under the old
    # format (Flutterwave's `charge.completed`, moving off the collision-prone
    # bare `tx_ref`); `None` for every identity format that was never live
    # under a different shape. Exactly one legacy alias, never a collection --
    # `inbox.receive_verified` treats this as a narrow, bounded migration aid,
    # not an open-ended alternate identity namespace.
    legacy_provider_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessClaimedPaymentWebhookCommand:
    """Exact claimed receipt admitted by the provider adapter."""

    receipt_id: UUID
    provider: PaymentWebhookProvider
    # attempt_count the caller observed at claim time. Threaded through to
    # `mark_processed` so a claimant whose lease was reclaimed by someone
    # else while it was still running cannot complete this receipt out from
    # under the new owner — see `inbox.InboxLeaseLost`. `None` skips the
    # fence (legacy/other callers); the payments adapter always supplies it.
    claimed_attempt: int | None = None


@dataclass(frozen=True, slots=True)
class ProcessedPaymentWebhook:
    """Immutable evidence returned after the full consequence commits."""

    receipt_id: UUID
    provider: PaymentWebhookProvider
    provider_event_id: UUID | None
    payment_id: UUID | None
    replayed: bool = False

    def consequence(self) -> dict[str, object]:
        return {
            "status": "ok",
            "http_status": 200,
            "provider_event_id": (
                str(self.provider_event_id) if self.provider_event_id else None
            ),
            "payment_id": str(self.payment_id) if self.payment_id else None,
        }


@dataclass(frozen=True, slots=True)
class IntegratorObservedMoney:
    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class IntegratorSettlementObservationCommand:
    """Typed product-owned meaning of one connector-normalized observation."""

    kind: IntegratorSettlementKind
    provider_status: str
    amount: IntegratorObservedMoney
    provider_fee: IntegratorObservedMoney | None
    occurred_at: datetime
    arrival: IntegratorSettlementArrival
    merchant_reference: str | None
    provider_transaction_id: str


@dataclass(frozen=True, slots=True)
class ProcessIntegratorSettlementCommand:
    """One claimed receipt plus engine-owned source provenance."""

    receipt_id: UUID
    source_installation_id: UUID
    connector_key: str
    provider_event_id: str
    observation: IntegratorSettlementObservationCommand


@dataclass(frozen=True, slots=True)
class CompareIntegratorSettlementCommand:
    """Read-only mirror input; it deliberately has no receipt identity."""

    source_installation_id: UUID
    connector_key: str
    provider_event_id: str
    observation: IntegratorSettlementObservationCommand


@dataclass(frozen=True, slots=True)
class ProcessedIntegratorSettlement:
    receipt_id: UUID
    provider_id: UUID
    provider_event_id: UUID | None
    payment_id: UUID | None
    processing_status: str
    replayed: bool = False

    def consequence(self) -> dict[str, object]:
        return {
            "status": "ok",
            "provider_id": str(self.provider_id),
            "provider_event_id": (
                str(self.provider_event_id) if self.provider_event_id else None
            ),
            "payment_id": str(self.payment_id) if self.payment_id else None,
            "processing_status": self.processing_status,
        }


@dataclass(frozen=True, slots=True)
class IntegratorSettlementDisagreement:
    field: str
    integrator: str | None
    sub: str | None


@dataclass(frozen=True, slots=True)
class IntegratorSettlementMirrorResult:
    verdict: Literal["match", "missing", "blocked"]
    identity: str
    counterpart_identity: str | None
    blocking_reasons: tuple[str, ...]
    disagreements: tuple[IntegratorSettlementDisagreement, ...]

    @property
    def agrees(self) -> bool:
        return self.verdict == "match" and not self.blocking_reasons


@dataclass(frozen=True, slots=True)
class _SettlementObservation:
    status: PaymentStatus
    amount: Decimal | None
    provider_fee: Decimal
    currency: str | None
    reference: str | None
    metadata: Mapping[str, Any]
    # Set only for refund/reversal observations, whose event type is never in
    # `payment_provider_events._FINANCIAL_EFFECT_BY_EVENT_TYPE` (a
    # `charge.dispute.resolve` outcome is payload-dependent, not a pure
    # event-type map entry) -- normalization REQUIRES this to be present for
    # `refunded`/`reversed` observed statuses, so leaving it unset is a loud
    # failure (`financial_effect_required`), not a silent one.
    financial_effect: PaymentProviderEventFinancialEffect | None = None


@dataclass(frozen=True, slots=True)
class _PreparedPaymentWebhook:
    ingest: PaymentProviderEventCommand
    settlement: _SettlementObservation | None
    topup_intent: TopupIntent | None


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error("payload_invalid", f"Payment webhook {field} must be an object")
    return value


def identify_verified_payment_webhook(
    provider: PaymentWebhookProvider,
    payload: Mapping[str, Any],
) -> PaymentWebhookReceiptIdentity:
    """Derive one stable receipt identity before inbox admission."""

    data = _mapping(payload.get("data", {}), field="data")
    event_type = str(payload.get("event") or "unknown").strip() or "unknown"
    if (
        provider is PaymentWebhookProvider.PAYSTACK
        and event_type in _PAYSTACK_EVENT_SCOPED_IDENTITY_TYPES
    ):
        own_id = str(data.get("id") or "").strip()
        if not own_id:
            raise _error(
                "payload_invalid",
                "Payment webhook omitted its provider event identity",
                provider=provider.value,
            )
        return PaymentWebhookReceiptIdentity(
            provider=provider,
            provider_event_id=f"{provider.value}-{event_type}-{own_id}",
            event_type=event_type,
        )
    if provider is PaymentWebhookProvider.FLUTTERWAVE:
        # `tx_ref` is Flutterwave's MERCHANT-supplied reference and Flutterwave
        # explicitly permits it to repeat across multiple transaction ATTEMPTS
        # on the same checkout (e.g. a failed attempt followed by a successful
        # retry). Using it as the receipt identity (the prior format, below,
        # kept only as a legacy alias) made every retry collide with the
        # earlier attempt's receipt and get quarantined as tampering. `data.id`
        # is Flutterwave's own provider-generated per-attempt id, already
        # trusted elsewhere in this codebase as the per-attempt identity
        # (required on a successful settlement, used as
        # `PaymentProviderEvent.external_id`, which is itself unique per
        # provider -- see `uq_payment_provider_events_external_id`). Confirmed
        # against Flutterwave's webhook documentation
        # (https://developer.flutterwave.com/v3.0/docs/webhooks) and checkout
        # retry behavior
        # (https://developer.flutterwave.com/v3.0/docs/flutterwave-standard-1):
        # `data.id` is present on BOTH successful and failed `charge.completed`
        # deliveries, so it is REQUIRED here, not merely preferred. There is
        # deliberately no `data.flw_ref` fallback: that fallback was an
        # unverified assumption in an earlier draft of this fix and has been
        # removed rather than kept as an unconfirmed escape hatch -- a
        # payload missing `data.id` is a hard reject, never a silent
        # downgrade to a different, unverified field, and never a fall-back
        # to `tx_ref`, which would silently reintroduce this exact bug.
        own_id = str(data.get("id") or "").strip()
        if not own_id:
            raise _error(
                "payload_invalid",
                "Payment webhook omitted its provider event identity",
                provider=provider.value,
            )
        # The legacy alias must reproduce BOTH branches of the OLD (pre-fix)
        # identity construction (`data.get("tx_ref") or data.get("id")`), not
        # just the `tx_ref` branch -- otherwise a redelivery of an
        # already-processed, `tx_ref`-less old-format `charge.completed`
        # event (old systems used `data.id` whenever `tx_ref` was absent)
        # would fail to match its old receipt via this alias and could be
        # treated as a new event. `own_id` above is already the resolved
        # `data.id` value, now the REQUIRED primary identity field.
        tx_ref = str(data.get("tx_ref") or "").strip()
        legacy_id = f"{provider.value}-{tx_ref or own_id}"
        return PaymentWebhookReceiptIdentity(
            provider=provider,
            provider_event_id=f"{provider.value}-{event_type}-{own_id}",
            event_type=event_type,
            legacy_provider_event_id=legacy_id,
        )

    reference_field = "reference"
    identity = str(data.get(reference_field) or data.get("id") or "").strip()
    if not identity:
        raise _error(
            "payload_invalid",
            "Payment webhook omitted its provider event identity",
            provider=provider.value,
        )
    return PaymentWebhookReceiptIdentity(
        provider=provider,
        provider_event_id=f"{provider.value}-{identity}",
        event_type=event_type,
    )


def _money(value: object, *, field: str, divisor: Decimal | None = None) -> Decimal:
    try:
        amount = Decimal(str(value))
        if divisor is not None:
            amount /= divisor
        if not amount.is_finite():
            raise InvalidOperation
        return round_money(amount)
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise _error(
            "payload_invalid",
            f"Payment webhook {field} is not valid money",
            field=field,
        ) from exc


def _currency(value: object) -> str:
    currency = str(value or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise _error(
            "payload_invalid",
            "Successful payment webhook currency must be a three-letter code",
            field="currency",
        )
    return currency


def _paystack_original_transaction(
    data: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """The ORIGINAL charge's transaction id/reference from a refund/dispute
    payload.

    A refund or dispute payload's own top-level `id`/`reference` identify the
    refund/dispute itself, never the charge it applies to (Defect C) --
    matching those against `Payment.external_id` can never succeed. Paystack
    nests the original transaction under `data.transaction`.
    """

    transaction = data.get("transaction")
    transaction = transaction if isinstance(transaction, Mapping) else None
    tx_id = (
        str(transaction.get("id") or "").strip()
        if transaction is not None and transaction.get("id") is not None
        else None
    )
    tx_reference: str | None = None
    if transaction is not None and transaction.get("reference"):
        tx_reference = str(transaction["reference"]).strip()
    elif data.get("transaction_reference"):
        # ASSUMPTION, unverified: the design brief names this exact top-level
        # field; Paystack's documented payload nests the reference under
        # `transaction.reference` instead, so this is a defensive fallback,
        # not a confirmed field name.
        tx_reference = str(data["transaction_reference"]).strip()
    return tx_id or None, tx_reference or None


def _settlement_observation(
    provider: PaymentWebhookProvider,
    *,
    event_type: str,
    data: Mapping[str, Any],
) -> _SettlementObservation | None:
    if provider is PaymentWebhookProvider.PAYSTACK:
        if event_type not in _PAYSTACK_RECOGNIZED_EVENT_TYPES:
            return None

        if event_type in _PAYSTACK_SETTLEMENT_EVENT_TYPES:
            metadata = data.get("metadata")
            return _SettlementObservation(
                status=PaymentStatus.succeeded,
                amount=_money(
                    data.get("amount", 0), field="amount", divisor=Decimal(100)
                ),
                provider_fee=_money(
                    data.get("fees", 0), field="fees", divisor=Decimal(100)
                ),
                currency=_currency(data.get("currency")),
                reference=str(data.get("reference") or "").strip() or None,
                metadata=metadata if isinstance(metadata, Mapping) else {},
            )

        if event_type in _PAYSTACK_REFUND_EVENT_TYPES:
            if event_type != "refund.processed":
                # `refund.pending`/`refund.processing`/`refund.failed`: no
                # money has moved (or, for `.failed`, none ever will for this
                # attempt). The event is still admitted and recorded under
                # its own `event_type` -- that column is what makes it
                # distinguishable from a real settlement in the record -- but
                # it must not be interpreted as one.
                return None
            _, tx_reference = _paystack_original_transaction(data)
            return _SettlementObservation(
                status=PaymentStatus.refunded,
                amount=_money(
                    data.get("amount", 0), field="amount", divisor=Decimal(100)
                ),
                provider_fee=Decimal("0.00"),
                currency=_currency(data.get("currency")),
                reference=tx_reference,
                metadata={},
                financial_effect=PaymentProviderEventFinancialEffect.refund_confirmed,
            )

        # event_type in _PAYSTACK_DISPUTE_EVENT_TYPES
        if event_type != "charge.dispute.resolve":
            # `charge.dispute.create`: informational + alertable -- funds are
            # held, not yet lost. A full dispute lifecycle (due dates,
            # evidence submission) is out of scope; the audit event/emitted
            # event already produced for every staged provider event is this
            # PR's alerting surface. `charge.dispute.remind`: informational
            # only.
            return None
        resolution = (
            str(data.get("resolution") or data.get("status") or "").strip().lower()
        )
        if resolution in _DISPUTE_RESOLUTION_MERCHANT_WON:
            # Merchant kept the funds; nothing to reverse.
            return None
        if resolution not in _DISPUTE_RESOLUTION_MERCHANT_LOST:
            raise _error(
                "dispute_resolution_unrecognized",
                "Paystack dispute resolution outcome was not recognized; "
                "verify the payload contract before treating this as "
                "either a loss or a win",
                resolution=resolution or None,
            )
        _, tx_reference = _paystack_original_transaction(data)
        return _SettlementObservation(
            status=PaymentStatus.reversed,
            amount=_money(
                data.get("refund_amount", 0),
                field="refund_amount",
                divisor=Decimal(100),
            ),
            provider_fee=Decimal("0.00"),
            currency=_currency(data.get("currency")),
            reference=tx_reference,
            metadata={},
            financial_effect=PaymentProviderEventFinancialEffect.reversal_confirmed,
        )

    if event_type != "charge.completed":
        return None
    metadata = data.get("meta")
    status = str(data.get("status") or "").strip().lower()
    if status == "successful":
        return _SettlementObservation(
            status=PaymentStatus.succeeded,
            amount=_money(data.get("amount", 0), field="amount"),
            provider_fee=_money(data.get("app_fee", 0), field="app_fee"),
            currency=_currency(data.get("currency")),
            reference=str(data.get("tx_ref") or "").strip() or None,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
    if status == "failed":
        return _SettlementObservation(
            status=PaymentStatus.failed,
            amount=None,
            provider_fee=Decimal("0.00"),
            currency=None,
            reference=str(data.get("tx_ref") or "").strip() or None,
            metadata=metadata if isinstance(metadata, Mapping) else {},
        )
    return None


def _optional_uuid(value: object) -> UUID | None:
    if value is None or value == "":
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _metadata_uuid(
    metadata: Mapping[str, Any],
    *,
    field: str,
    error_suffix: str,
) -> UUID | None:
    value = metadata.get(field)
    if value is None or value == "":
        return None
    parsed = _optional_uuid(value)
    if parsed is None:
        raise _error(
            error_suffix,
            f"Payment webhook {field} is not a valid identifier",
            field=field,
        )
    return parsed


def _resolve_topup_intent(
    db: Session,
    *,
    provider: PaymentWebhookProvider,
    settlement: _SettlementObservation,
) -> TopupIntent | None:
    metadata_intent_id = _metadata_uuid(
        settlement.metadata,
        field="topup_intent_id",
        error_suffix="topup_intent_mismatch",
    )
    if metadata_intent_id is not None:
        intent = db.get(TopupIntent, metadata_intent_id)
        if intent is None:
            raise _error(
                "topup_intent_mismatch",
                "Payment webhook top-up intent does not exist",
                intent_id=str(metadata_intent_id),
            )
    elif settlement.reference:
        intent = db.scalar(
            select(TopupIntent).where(TopupIntent.reference == settlement.reference)
        )
    else:
        intent = None
    if intent is None:
        return None
    if settlement.reference and intent.reference != settlement.reference:
        raise _error(
            "topup_intent_mismatch",
            "Payment webhook reference does not match the selected top-up intent",
            intent_id=str(intent.id),
        )
    if intent.provider_type != provider.value:
        raise _error(
            "topup_intent_mismatch",
            "Payment webhook provider does not match the selected top-up intent",
            intent_id=str(intent.id),
        )
    return intent


def _resolve_reversal_payment_id(
    db: Session,
    *,
    provider_id: UUID,
    data: Mapping[str, Any],
) -> UUID | None:
    """Resolve the ORIGINAL payment for a refund/reversal observation.

    The observation's own `data.id` is the refund/dispute's own identity, not
    the original charge's (Defect C) -- matching it against
    `Payment.external_id` the way a settlement is matched can never succeed
    and would silently route every refund/dispute to `payment_not_found`.
    Priority order, each strictly more indirect than the last:

    1. The nested original transaction's own id, matched the same way a
       settlement is (`Payment.external_id`).
    2. The original transaction's reference, resolved against Sub's OWN prior
       succeeded observation (`PaymentProviderEvent.provider_reference`) --
       not against a live call to Paystack's API.

    A merchant-note/request-key third step (matching a Sub-initiated refund
    echoed back by Paystack) is deliberately NOT implemented here: grepping
    this codebase shows `PaymentGatewayAdapter.refund`, the only writer of a
    `merchant_note`/`request_key`, has no caller anywhere in `app/` today, and
    `_validate_refund_provider_event` explicitly rejects a manual refund for
    any provider-backed payment. There is no durable request-key record to
    match against yet; inventing one now would be a new persistence decision,
    not an implementation detail of this fix.
    """

    tx_id, tx_reference = _paystack_original_transaction(data)
    if tx_id:
        payment = db.scalar(
            select(Payment)
            .where(Payment.external_id == tx_id)
            .where(
                or_(
                    Payment.provider_id == provider_id,
                    Payment.provider_id.is_(None),
                )
            )
            .order_by((Payment.provider_id == provider_id).desc())
        )
        if payment is not None:
            return payment.id

    if tx_reference:
        prior_event = db.scalar(
            select(PaymentProviderEvent)
            .where(PaymentProviderEvent.provider_id == provider_id)
            .where(PaymentProviderEvent.provider_reference == tx_reference)
            .where(
                PaymentProviderEvent.observed_payment_status == PaymentStatus.succeeded
            )
            .where(PaymentProviderEvent.payment_id.is_not(None))
            .order_by(PaymentProviderEvent.received_at.desc())
        )
        if prior_event is not None:
            return prior_event.payment_id

    return None


def _prepare_payment_webhook(
    db: Session,
    *,
    receipt: IntegrationInbox,
    provider: PaymentWebhookProvider,
    provider_id: UUID,
) -> _PreparedPaymentWebhook:
    payload = _mapping(receipt.payload_json, field="payload")
    data = _mapping(payload.get("data", {}), field="data")
    settlement = _settlement_observation(
        provider,
        event_type=receipt.event_type,
        data=data,
    )
    external_id = str(data.get("id") or "").strip() or None
    ingest = PaymentProviderEventCommand(
        provider_id=provider_id,
        event_type=receipt.event_type,
        external_id=external_id,
        idempotency_key=receipt.provider_event_id,
        payload=dict(payload),
    )
    if settlement is None:
        return _PreparedPaymentWebhook(
            ingest=ingest, settlement=None, topup_intent=None
        )

    topup_intent = _resolve_topup_intent(
        db,
        provider=provider,
        settlement=settlement,
    )
    if settlement.status in (PaymentStatus.refunded, PaymentStatus.reversed):
        resolved_payment_id = _resolve_reversal_payment_id(
            db, provider_id=provider_id, data=data
        )
        return _PreparedPaymentWebhook(
            ingest=replace(
                ingest,
                observed_payment_status=settlement.status,
                financial_effect=settlement.financial_effect,
                amount=settlement.amount,
                currency=settlement.currency,
                provider_reference=settlement.reference,
                payment_id=resolved_payment_id,
            ),
            settlement=settlement,
            topup_intent=topup_intent,
        )
    if settlement.status != PaymentStatus.succeeded:
        # A failed attempt now gets its own independently-persisted receipt
        # (it is no longer collapsed into the same identity as a later
        # successful retry -- see the Flutterwave identity fix above), so it
        # must record `provider_reference` (the `tx_ref`) itself, or a failed
        # attempt with no reference couldn't be correlated back to its
        # checkout. Previously this branch dropped it; only the succeeded path
        # below set it.
        return _PreparedPaymentWebhook(
            ingest=replace(
                ingest,
                observed_payment_status=settlement.status,
                provider_reference=settlement.reference,
            ),
            settlement=settlement,
            topup_intent=topup_intent,
        )
    if settlement.amount is None or settlement.amount <= Decimal("0.00"):
        raise _error(
            "payload_invalid",
            "Successful payment webhook amount must be positive",
        )
    if (
        settlement.provider_fee < Decimal("0.00")
        or settlement.provider_fee > settlement.amount
    ):
        raise _error(
            "payload_invalid",
            "Payment webhook provider fee is outside the confirmed amount",
        )
    if not external_id:
        raise _error(
            "payload_invalid",
            "Successful payment webhook omitted its provider transaction identity",
        )
    invoice_id = _metadata_uuid(
        settlement.metadata,
        field="invoice_id",
        error_suffix="payload_invalid",
    )
    return _PreparedPaymentWebhook(
        ingest=replace(
            ingest,
            amount=settlement.amount,
            provider_fee=settlement.provider_fee,
            net_amount=(
                round_money(topup_intent.requested_amount)
                if topup_intent is not None
                else round_money(settlement.amount - settlement.provider_fee)
            ),
            provider_reference=settlement.reference,
            topup_intent_id=(topup_intent.id if topup_intent is not None else None),
            currency=settlement.currency,
            invoice_id=invoice_id,
            account_id=(topup_intent.account_id if topup_intent is not None else None),
            billing_account_id=(
                topup_intent.billing_account_id if topup_intent is not None else None
            ),
            observed_payment_status=settlement.status,
        ),
        settlement=settlement,
        topup_intent=topup_intent,
    )


def _stage_deposit_settlement(
    db: Session,
    *,
    prepared: _PreparedPaymentWebhook,
    provider: PaymentWebhookProvider,
    context: CommandContext,
) -> _PreparedPaymentWebhook:
    settlement = prepared.settlement
    intent = prepared.topup_intent
    if (
        settlement is None
        or settlement.status != PaymentStatus.succeeded
        or intent is None
        or intent.purpose != "account_credit_deposit"
    ):
        return prepared
    external_id = prepared.ingest.external_id
    if settlement.amount is None or not external_id:
        raise _error(
            "deposit_rejected",
            "Deposit provider confirmation omitted amount or transaction identity",
        )
    provider_intent_id = _optional_uuid(settlement.metadata.get("topup_intent_id"))
    if provider_intent_id is None:
        raise _error(
            "deposit_rejected",
            "Deposit provider confirmation omitted intent correlation",
            intent_id=str(intent.id),
        )
    try:
        result = AccountCreditDeposits.stage_verified_settlement(
            db,
            SettleAccountCreditDepositCommand(
                intent_id=intent.id,
                provider_type=provider.value,
                external_transaction_id=external_id,
                amount=settlement.amount,
                currency=settlement.currency or intent.currency,
                provider_intent_id=provider_intent_id,
                source=AccountCreditDepositSettlementSource.provider_webhook,
                provider_fee=settlement.provider_fee,
            ),
            context=CommandContext.system(
                actor=context.actor,
                scope=SETTLEMENT_PARTICIPANT_SCOPE,
                reason="Stage webhook-confirmed account-credit deposit",
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
                idempotency_key=f"account-credit-deposit-{intent.id}",
            ),
        )
    except DepositEligibilityError as exc:
        raise _error(
            "deposit_rejected",
            str(exc),
            deposit_error_code=exc.code,
            intent_id=str(intent.id),
        ) from exc
    return replace(
        prepared,
        ingest=replace(prepared.ingest, payment_id=result.payment.id),
    )


def _expected_financial_effect_unresolved(event: PaymentProviderEventResult) -> bool:
    """A real money-movement was expected but could not be resolved to a payment.

    ``PaymentProviderEventStatus.failed`` with ``error_code == "payment_not_found"``
    also covers a genuinely benign case: a declined/failed charge notification for
    which no payment ever existed and none was ever expected (nothing to reverse).
    Silently accepting that case is correct. Silently accepting an unmatched
    refund/reversal/dispute-loss observation is not — that hides a real
    consequence that never landed. Distinguish the two by whether the
    observation itself claimed a financial effect.
    """

    if event.status is not PaymentProviderEventStatus.failed:
        return False
    if event.error_code != "payment_not_found":
        return False
    if event.observed_payment_status in (
        PaymentStatus.refunded,
        PaymentStatus.reversed,
    ):
        return True
    return event.financial_effect is not PaymentProviderEventFinancialEffect.none


def _stage_provider_event(
    db: Session,
    ingest: PaymentProviderEventCommand,
    *,
    context: CommandContext,
) -> PaymentProviderEventResult:
    try:
        return billing_service.payment_provider_events.stage_verified_webhook_event(
            db,
            ingest,
            context=CommandContext.system(
                actor=context.actor,
                scope=WEBHOOK_PARTICIPANT_SCOPE,
                reason="Stage signature-verified payment-provider observation",
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
                idempotency_key=ingest.idempotency_key,
            ),
        )
    except PaymentProviderEventError as exc:
        raise _error(
            "provider_event_rejected",
            exc.message,
            provider_event_error_code=exc.code,
        ) from exc


def _stage_topup_consequences(
    db: Session,
    *,
    prepared: _PreparedPaymentWebhook,
    event: PaymentProviderEventResult,
    context: CommandContext,
) -> None:
    settlement = prepared.settlement
    intent = prepared.topup_intent
    if (
        settlement is None
        or settlement.status != PaymentStatus.succeeded
        or event.payment_id is None
        or intent is None
        or intent.purpose == "account_credit_deposit"
    ):
        return
    try:
        stage_topup_intent_completion(
            db,
            CompleteTopupIntentCommand(
                intent_id=intent.id,
                payment_id=event.payment_id,
                source=TopupIntentCompletionSource.provider_webhook,
            ),
            context=CommandContext.system(
                actor=context.actor,
                scope=COMPLETION_SCOPE,
                reason="Project webhook payment onto top-up intent",
                correlation_id=context.correlation_id,
                causation_id=context.command_id,
            ),
        )
    except TopupIntentError as exc:
        raise _error(
            "topup_projection_rejected",
            exc.message,
            topup_error_code=exc.code,
            intent_id=str(intent.id),
        ) from exc


def _result_from_consequence(
    receipt: IntegrationInbox,
    provider: PaymentWebhookProvider,
) -> ProcessedPaymentWebhook:
    consequence = receipt.consequence_json or {}
    return ProcessedPaymentWebhook(
        receipt_id=receipt.id,
        provider=provider,
        provider_event_id=_optional_uuid(consequence.get("provider_event_id")),
        payment_id=_optional_uuid(consequence.get("payment_id")),
        replayed=True,
    )


def _integrator_provider(db: Session, *, installation_id: UUID) -> PaymentProvider:
    provider = db.scalar(
        select(PaymentProvider)
        .where(PaymentProvider.integrator_installation_ref == installation_id)
        .with_for_update()
    )
    if provider is None or not provider.is_active:
        raise _error(
            "integrator_provider_not_configured",
            "No active payment provider is mapped to this Integrator installation",
            source_installation_id=str(installation_id),
        )
    return provider


def _integrator_intent(
    db: Session,
    *,
    provider: PaymentProvider,
    merchant_reference: str | None,
) -> TopupIntent | None:
    if not merchant_reference:
        return None
    intent = db.scalar(
        select(TopupIntent)
        .where(TopupIntent.reference == merchant_reference)
        .with_for_update()
    )
    if intent is None:
        return None
    if intent.provider_id is not None and intent.provider_id != provider.id:
        raise _error(
            "topup_intent_mismatch",
            "Settlement reference belongs to another payment provider",
            intent_id=str(intent.id),
        )
    if intent.provider_type != provider.provider_type.value:
        raise _error(
            "topup_intent_mismatch",
            "Settlement provider does not match the selected top-up intent",
            intent_id=str(intent.id),
        )
    return intent


def _integrator_receipt(
    db: Session, command: ProcessIntegratorSettlementCommand
) -> IntegrationInbox:
    receipt = lock_for_update(db, IntegrationInbox, command.receipt_id)
    if receipt is None:
        raise _error(
            "receipt_not_found",
            "Claimed Integrator settlement receipt was not found",
            receipt_id=str(command.receipt_id),
        )
    if receipt.state == "processed":
        return receipt
    if receipt.state != "processing":
        raise _error(
            "receipt_not_claimed",
            "Integrator settlement receipt must be claimed before processing",
            receipt_id=str(receipt.id),
            state=receipt.state,
        )
    headers = receipt.headers_json or {}
    if (
        str(headers.get("integrator_installation_id") or "")
        != str(command.source_installation_id)
        or str(headers.get("integrator_connector_key") or "") != command.connector_key
        or receipt.provider_event_id != command.provider_event_id
    ):
        raise _error(
            "receipt_source_mismatch",
            "Integrator settlement source does not match its claimed receipt",
            receipt_id=str(receipt.id),
        )
    return receipt


def _integrator_prepared(
    db: Session,
    *,
    command: ProcessIntegratorSettlementCommand,
    provider: PaymentProvider,
) -> _PreparedPaymentWebhook:
    observation = command.observation
    intent = _integrator_intent(
        db,
        provider=provider,
        merchant_reference=observation.merchant_reference,
    )
    status = (
        PaymentStatus.succeeded
        if observation.kind is IntegratorSettlementKind.CAPTURE
        else PaymentStatus.failed
    )
    ingest = PaymentProviderEventCommand(
        provider_id=provider.id,
        event_type=(
            "payment.succeeded"
            if status is PaymentStatus.succeeded
            else "payment.failed"
        ),
        external_id=observation.provider_transaction_id,
        idempotency_key=command.provider_event_id,
        provider_reference=observation.merchant_reference,
        observed_payment_status=status,
    )
    settlement = _SettlementObservation(
        status=status,
        amount=(
            observation.amount.amount if status is PaymentStatus.succeeded else None
        ),
        provider_fee=(
            observation.provider_fee.amount
            if observation.provider_fee is not None
            else Decimal("0.00")
        ),
        currency=(
            observation.amount.currency if status is PaymentStatus.succeeded else None
        ),
        reference=observation.merchant_reference,
        metadata={},
    )
    if status is PaymentStatus.failed:
        return _PreparedPaymentWebhook(
            ingest=ingest,
            settlement=settlement,
            topup_intent=intent,
        )
    if observation.provider_fee is None:
        # Some provider contracts do not expose a fee. Retrying the same bytes
        # cannot manufacture that fact, and treating absence as zero would
        # falsify net settlement. Keep the receipt retryable until a
        # product-owned policy or richer provider observation exists.
        raise _error(
            "provider_fee_unobserved",
            "Settlement consequence requires an observed provider fee",
            source_installation_id=str(command.source_installation_id),
        )
    if observation.provider_fee.currency != observation.amount.currency:
        raise _error(
            "payload_invalid",
            "Settlement provider fee currency differs from the gross amount",
        )
    if observation.provider_fee.amount > observation.amount.amount:
        raise _error(
            "payload_invalid",
            "Settlement provider fee exceeds the gross amount",
        )
    if intent is not None and intent.purpose == "account_credit_deposit":
        raise _error(
            "deposit_correlation_unavailable",
            "Settlement omitted provider-echoed deposit intent correlation",
            intent_id=str(intent.id),
        )
    return _PreparedPaymentWebhook(
        ingest=replace(
            ingest,
            amount=observation.amount.amount,
            provider_fee=observation.provider_fee.amount,
            net_amount=(
                round_money(intent.requested_amount)
                if intent is not None
                else round_money(
                    observation.amount.amount - observation.provider_fee.amount
                )
            ),
            topup_intent_id=(intent.id if intent is not None else None),
            currency=observation.amount.currency,
            invoice_id=(intent.invoice_id if intent is not None else None),
            account_id=(intent.account_id if intent is not None else None),
            billing_account_id=(
                intent.billing_account_id if intent is not None else None
            ),
        ),
        settlement=settlement,
        topup_intent=intent,
    )


def _integrator_result_from_consequence(
    receipt: IntegrationInbox,
) -> ProcessedIntegratorSettlement:
    consequence = receipt.consequence_json or {}
    provider_id = _optional_uuid(consequence.get("provider_id"))
    if provider_id is None:
        raise _error(
            "receipt_consequence_invalid",
            "Processed Integrator settlement has no provider identity",
            receipt_id=str(receipt.id),
        )
    return ProcessedIntegratorSettlement(
        receipt_id=receipt.id,
        provider_id=provider_id,
        provider_event_id=_optional_uuid(consequence.get("provider_event_id")),
        payment_id=_optional_uuid(consequence.get("payment_id")),
        processing_status=str(consequence.get("processing_status") or "processed"),
        replayed=True,
    )


def compare_integrator_settlement(
    db: Session,
    command: CompareIntegratorSettlementCommand,
) -> IntegratorSettlementMirrorResult:
    """Compare normalized evidence with the incumbent owner; write nothing."""

    provider = db.scalar(
        select(PaymentProvider).where(
            PaymentProvider.integrator_installation_ref
            == command.source_installation_id
        )
    )
    identity = command.observation.provider_transaction_id
    if provider is None or not provider.is_active:
        return IntegratorSettlementMirrorResult(
            verdict="blocked",
            identity=identity,
            counterpart_identity=None,
            blocking_reasons=("integrator_provider_not_configured",),
            disagreements=(),
        )
    counterpart = db.scalar(
        select(PaymentProviderEvent)
        .where(PaymentProviderEvent.provider_id == provider.id)
        .where(PaymentProviderEvent.external_id == identity)
    )
    if counterpart is None:
        return IntegratorSettlementMirrorResult(
            verdict="missing",
            identity=identity,
            counterpart_identity=None,
            blocking_reasons=(),
            disagreements=(),
        )

    observation = command.observation
    expected_status = (
        PaymentStatus.succeeded
        if observation.kind is IntegratorSettlementKind.CAPTURE
        else PaymentStatus.failed
    )
    expected_amount = (
        round_money(observation.amount.amount)
        if expected_status is PaymentStatus.succeeded
        else None
    )
    expected_fee = (
        round_money(observation.provider_fee.amount)
        if observation.provider_fee is not None
        else None
    )
    values = (
        (
            "observed_payment_status",
            expected_status.value,
            (
                counterpart.observed_payment_status.value
                if counterpart.observed_payment_status
                else None
            ),
        ),
        (
            "amount",
            str(expected_amount) if expected_amount is not None else None,
            str(round_money(counterpart.amount))
            if counterpart.amount is not None
            else None,
        ),
        (
            "provider_fee",
            str(expected_fee) if expected_fee is not None else None,
            str(round_money(counterpart.provider_fee)),
        ),
        ("currency", observation.amount.currency, counterpart.currency),
        (
            "provider_reference",
            observation.merchant_reference,
            counterpart.provider_reference,
        ),
    )
    disagreements = tuple(
        IntegratorSettlementDisagreement(field=field, integrator=left, sub=right)
        for field, left, right in values
        if left != right and not (field == "provider_fee" and left is None)
    )
    blocking = (
        ("provider_fee_unobserved",)
        if observation.provider_fee is None
        and expected_status is PaymentStatus.succeeded
        else ()
    )
    return IntegratorSettlementMirrorResult(
        verdict="match" if not disagreements else "blocked",
        identity=identity,
        counterpart_identity=str(counterpart.id),
        blocking_reasons=blocking,
        disagreements=disagreements,
    )


def process_integrator_settlement(
    db: Session,
    command: ProcessIntegratorSettlementCommand,
    *,
    context: CommandContext,
) -> ProcessedIntegratorSettlement:
    """Commit one product-owned consequence for a claimed Integrator receipt."""

    return execute_owner_command(
        db,
        definition=_INTEGRATOR_PROCESS_COMMAND,
        context=context,
        operation=lambda: _process_integrator_settlement(
            db,
            command=command,
            context=context,
        ),
    )


def _process_integrator_settlement(
    db: Session,
    *,
    command: ProcessIntegratorSettlementCommand,
    context: CommandContext,
) -> ProcessedIntegratorSettlement:
    receipt = _integrator_receipt(db, command)
    if receipt.state == "processed":
        return _integrator_result_from_consequence(receipt)
    provider = _integrator_provider(db, installation_id=command.source_installation_id)
    prepared = _integrator_prepared(db, command=command, provider=provider)
    event = _stage_provider_event(db, prepared.ingest, context=context)
    if (
        prepared.settlement is not None
        and prepared.settlement.status is PaymentStatus.succeeded
        and event.payment_id is None
    ):
        raise _error(
            "settlement_unlinked",
            "Successful settlement did not post or link a payment",
            provider_event_id=str(event.id),
        )
    _stage_topup_consequences(
        db,
        prepared=prepared,
        event=event,
        context=context,
    )
    result = ProcessedIntegratorSettlement(
        receipt_id=receipt.id,
        provider_id=provider.id,
        provider_event_id=event.id,
        payment_id=event.payment_id,
        processing_status=event.status.value,
    )
    integration_inbox.mark_processed(receipt, consequence=result.consequence())
    db.flush()
    return result


def process_claimed_payment_webhook(
    db: Session,
    command: ProcessClaimedPaymentWebhookCommand,
    *,
    context: CommandContext,
) -> ProcessedPaymentWebhook:
    """Commit one complete billing consequence for a claimed verified receipt."""

    return execute_owner_command(
        db,
        definition=_PROCESS_COMMAND,
        context=context,
        operation=lambda: _process_claimed_payment_webhook(
            db,
            command=command,
            context=context,
        ),
    )


def _process_claimed_payment_webhook(
    db: Session,
    *,
    command: ProcessClaimedPaymentWebhookCommand,
    context: CommandContext,
) -> ProcessedPaymentWebhook:
    receipt = lock_for_update(db, IntegrationInbox, command.receipt_id)
    if receipt is None:
        raise _error(
            "receipt_not_found",
            "Claimed payment webhook receipt was not found",
            receipt_id=str(command.receipt_id),
        )
    recorded_provider = str((receipt.headers_json or {}).get("provider") or "")
    if recorded_provider != command.provider.value:
        raise _error(
            "receipt_provider_mismatch",
            "Payment webhook provider does not match the verified receipt",
            receipt_id=str(receipt.id),
        )
    if receipt.state == "processed":
        return _result_from_consequence(receipt, command.provider)
    if receipt.state != "processing":
        raise _error(
            "receipt_not_claimed",
            "Payment webhook receipt must be claimed before processing",
            receipt_id=str(receipt.id),
            state=receipt.state,
        )
    provider = billing_service.payment_providers.get_by_type(
        db,
        PaymentProviderType(command.provider.value),
    )
    if provider is None:
        raise _error(
            "provider_not_configured",
            "No payment provider is configured for this verified receipt",
            provider=command.provider.value,
        )
    try:
        prepared = _prepare_payment_webhook(
            db,
            receipt=receipt,
            provider=command.provider,
            provider_id=provider.id,
        )
    except PaymentWebhookError:
        raise
    except ValueError as exc:
        raise _error(
            "payload_invalid",
            "Payment webhook could not be normalized",
        ) from exc
    prepared = _stage_deposit_settlement(
        db,
        prepared=prepared,
        provider=command.provider,
        context=context,
    )
    event = _stage_provider_event(db, prepared.ingest, context=context)
    if (
        prepared.settlement is not None
        and prepared.settlement.status == PaymentStatus.succeeded
        and event.payment_id is None
    ):
        raise _error(
            "settlement_unlinked",
            "Successful settlement did not post or link a payment",
            provider_event_id=str(event.id),
        )
    if _expected_financial_effect_unresolved(event):
        raise _error(
            "provider_event_unresolved",
            "Provider event indicated a financial effect that could not be "
            "resolved to a billing consequence",
            provider_event_id=str(event.id),
            provider_event_error_code=event.error_code,
        )
    _stage_topup_consequences(
        db,
        prepared=prepared,
        event=event,
        context=context,
    )
    result = ProcessedPaymentWebhook(
        receipt_id=receipt.id,
        provider=command.provider,
        provider_event_id=event.id,
        payment_id=event.payment_id,
    )
    integration_inbox.mark_processed(
        receipt,
        consequence=result.consequence(),
        claimed_attempt=command.claimed_attempt,
    )
    db.flush()
    return result


__all__ = [
    "CompareIntegratorSettlementCommand",
    "INTEGRATOR_PROCESS_SCOPE",
    "PROCESS_SCOPE",
    "IntegratorObservedMoney",
    "IntegratorSettlementArrival",
    "IntegratorSettlementKind",
    "IntegratorSettlementDisagreement",
    "IntegratorSettlementMirrorResult",
    "IntegratorSettlementObservationCommand",
    "PaymentWebhookError",
    "PaymentWebhookProvider",
    "PaymentWebhookReceiptIdentity",
    "ProcessClaimedPaymentWebhookCommand",
    "ProcessIntegratorSettlementCommand",
    "ProcessedIntegratorSettlement",
    "ProcessedPaymentWebhook",
    "identify_verified_payment_webhook",
    "compare_integrator_settlement",
    "process_claimed_payment_webhook",
    "process_integrator_settlement",
]
