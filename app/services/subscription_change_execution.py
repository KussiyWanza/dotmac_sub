"""Canonical coordinator for deferred service-change execution.

The coordinator owns only the cross-owner invariant and durable links. Money,
invoice settlement, service-order state, field execution, provisioning
readiness, and the final subscription mutation remain with their registered
owners.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
)
from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    OfferRadiusProfile,
    RadiusProfile,
    Subscription,
)
from app.models.provisioning import (
    ProvisioningReadinessDecision,
    ProvisioningReadinessDecisionStatus,
    ServiceOrder,
    ServiceOrderStatus,
    ServiceOrderType,
)
from app.models.radius import RadiusUser
from app.models.subscription_change import (
    SubscriptionChangeExecutionState,
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.schemas.billing import InvoiceCreate
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.services import billing as billing_service
from app.services.audit_adapter import stage_audit_event
from app.services.events import EventType, emit_event
from app.services.prepaid_plan_changes import (
    PrepaidPlanChangeDecision,
    resolve_prepaid_plan_change,
)
from app.services.radius_access_state import stage_subscription_radius_profile
from app.services.subscription_changes import subscription_change_requests
from app.services.work_order_commands import work_order_commands


class SubscriptionChangeExecutionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FulfillmentOutcome:
    request_id: UUID
    service_order_id: UUID
    work_order_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class RemoteReprovisionOutcome:
    request_id: UUID
    radius_profile_id: UUID
    radius_user_id: UUID | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ExecutionDrift:
    request_id: UUID
    code: str
    repairable: bool


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationItem:
    request_id: UUID
    subscription_id: UUID
    status: str
    execution_state: str
    findings: tuple[ExecutionDrift, ...]
    reviewed_head: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationInspection:
    items: tuple[ExecutionReconciliationItem, ...]
    inspected_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionReconciliationOutcome:
    request_id: UUID
    execution_state: str
    replayed: bool
    reviewed_head: str


class RemoteProvisionActionStatus(StrEnum):
    completed = "completed"
    replayed = "replayed"
    price_review_required = "price_review_required"
    billing_blocked = "billing_blocked"


@dataclass(frozen=True, slots=True)
class RemoteProvisionActionCommand:
    request_id: UUID
    subscription_id: UUID
    account_id: UUID
    actor_id: str
    idempotency_key: str
    reason: str
    confirmed_price_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteProvisionPriceReview:
    fingerprint: str
    effective_at: datetime
    previous_amount: Decimal
    current_amount: Decimal
    currency: str
    available_balance: Decimal
    shortfall: Decimal
    allowed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class RemoteProvisionActionOutcome:
    request_id: UUID
    subscription_id: UUID
    status: RemoteProvisionActionStatus
    target_offer_name: str
    target_profile_name: str
    operation_reference: str
    message: str
    price_review: RemoteProvisionPriceReview | None = None


_PRICE_REVIEW_SNAPSHOT_KEY = "provisioning_price_review"


def _snapshot_decimal(snapshot: dict[str, object], key: str) -> Decimal:
    try:
        return Decimal(str(snapshot.get(key) or "0.00")).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return Decimal("0.00")


def _upgrade_amount(snapshot: dict[str, object]) -> Decimal:
    return max(Decimal("0.00"), _snapshot_decimal(snapshot, "required_amount"))


def _format_amount(currency: str, amount: Decimal) -> str:
    normalized = currency.strip().upper() or "NGN"
    prefix = "₦" if normalized == "NGN" else f"{normalized} "
    return f"{prefix}{amount:,.2f}"


def _stored_price_review(
    request: SubscriptionChangeRequest,
) -> RemoteProvisionPriceReview | None:
    snapshot = request.confirmation_snapshot or {}
    raw = snapshot.get(_PRICE_REVIEW_SNAPSHOT_KEY)
    if not isinstance(raw, dict):
        return None
    fingerprint = str(raw.get("fingerprint") or "").strip()
    effective_raw = str(raw.get("effective_at") or "").strip()
    if not fingerprint or not effective_raw:
        return None
    try:
        effective_at = datetime.fromisoformat(effective_raw)
    except ValueError:
        return None
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)
    else:
        effective_at = effective_at.astimezone(UTC)
    return RemoteProvisionPriceReview(
        fingerprint=fingerprint,
        effective_at=effective_at,
        previous_amount=_snapshot_decimal(raw, "previous_amount"),
        current_amount=_snapshot_decimal(raw, "current_amount"),
        currency=str(raw.get("currency") or "NGN").upper(),
        available_balance=_snapshot_decimal(raw, "available_balance"),
        shortfall=_snapshot_decimal(raw, "shortfall"),
        allowed=bool(raw.get("allowed", False)),
        reason=str(raw.get("reason") or "").strip() or None,
    )


def _price_review_snapshot(review: RemoteProvisionPriceReview) -> dict[str, object]:
    return {
        "fingerprint": review.fingerprint,
        "effective_at": review.effective_at.isoformat(),
        "previous_amount": str(review.previous_amount),
        "current_amount": str(review.current_amount),
        "currency": review.currency,
        "available_balance": str(review.available_balance),
        "shortfall": str(review.shortfall),
        "allowed": review.allowed,
        "reason": review.reason,
    }


def _refreshed_financial_decision(
    db: Session,
    *,
    request: SubscriptionChangeRequest,
    subscription: Subscription,
    effective_at: datetime,
) -> PrepaidPlanChangeDecision:
    return resolve_prepaid_plan_change(
        db,
        subscription,
        str(request.requested_offer_id),
        effective_at=effective_at,
    )


def _billing_block_message(review: RemoteProvisionPriceReview) -> str:
    price = _format_amount(review.currency, review.current_amount)
    balance = _format_amount(review.currency, review.available_balance)
    if review.reason == "insufficient_prepaid_funding":
        shortfall = _format_amount(review.currency, review.shortfall)
        return (
            f"The refreshed upgrade price is {price}, but the available prepaid "
            f"balance is {balance} (shortfall {shortfall}). Fund the account, then "
            "refresh the price and continue."
        )
    if review.reason == "collection_blocking_balance":
        return (
            "This account has a collection-blocking balance. Resolve the billing "
            "balance, then refresh the price and continue."
        )
    if review.reason == "catalog_currency_mismatch":
        return (
            "The current and requested plans use different currencies. Correct the "
            "catalog pricing before provisioning."
        )
    return (
        "Billing no longer permits this upgrade. Refresh the account balance and "
        "price before provisioning."
    )


def _active_remote_credential(
    db: Session, *, subscription_id: UUID
) -> AccessCredential | None:
    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscription_id == subscription_id,
                AccessCredential.is_active.is_(True),
            )
        ).all()
    )
    return credentials[0] if len(credentials) == 1 else None


def _recover_remote_provision_failure(
    db: Session,
    *,
    command: RemoteProvisionActionCommand,
    credential_id: UUID,
    previous_radius_profile_id: UUID | None,
    operation_reference: str,
    failure: Exception,
) -> bool:
    """Converge database and RADIUS state after a post-projection failure.

    ``True`` means the commercial change committed and its request record was
    repaired to completed. ``False`` means the old commercial plan remained
    authoritative and its previous RADIUS projection was restored, leaving the
    request retryable.
    """

    request = _lock_request(db, command.request_id)
    subscription = db.get(Subscription, request.subscription_id)
    if subscription is None:
        raise SubscriptionChangeExecutionError(
            "subscription_not_found",
            "Subscription disappeared during provisioning recovery",
        )

    commercial_change_committed = (
        request.status == SubscriptionChangeStatus.applied
        and subscription.offer_id == request.requested_offer_id
    )
    if commercial_change_committed:
        request.execution_state = SubscriptionChangeExecutionState.completed
        stage_audit_event(
            db,
            action="recover_remote_plan_change_completion",
            entity_type="subscription_change_request",
            entity_id=str(request.id),
            actor_type=AuditActorType.user,
            actor_id=command.actor_id,
            metadata={
                "subscription_id": str(subscription.id),
                "operation_reference": operation_reference,
                "result": "commercial_change_already_committed",
                "failure_type": type(failure).__name__,
            },
        )
        db.flush()
        return True

    credential = db.get(AccessCredential, credential_id)
    if (
        credential is None
        or credential.subscription_id != subscription.id
        or not credential.is_active
    ):
        raise SubscriptionChangeExecutionError(
            "remote_access_credential_ambiguous",
            "The previous RADIUS credential is unavailable for recovery",
        )
    stage_subscription_radius_profile(
        db,
        subscription_id=subscription.id,
        credential_id=credential.id,
        radius_profile_id=previous_radius_profile_id,
    )
    from app.services.radius import reconcile_subscription_connectivity

    reconcile_subscription_connectivity(db, str(subscription.id)).require_projected()
    request.execution_state = SubscriptionChangeExecutionState.provisioning
    request.remote_reprovision_requested_at = None
    request.provisioning_verified_at = None
    stage_audit_event(
        db,
        action="rollback_remote_plan_change_provisioning",
        entity_type="subscription_change_request",
        entity_id=str(request.id),
        actor_type=AuditActorType.user,
        actor_id=command.actor_id,
        status_code=409,
        is_success=False,
        metadata={
            "subscription_id": str(subscription.id),
            "operation_reference": operation_reference,
            "result": "previous_radius_profile_restored",
            "failure_type": type(failure).__name__,
        },
    )
    db.flush()
    return False


def _lock_request(db: Session, request_id: UUID) -> SubscriptionChangeRequest:
    request = db.scalar(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.id == request_id)
        .with_for_update()
    )
    if request is None or not request.is_active:
        raise SubscriptionChangeExecutionError(
            "service_change_not_found", "Service-change request not found"
        )
    return request


def stage_relocation_charge(
    db: Session, request: SubscriptionChangeRequest
) -> Invoice | None:
    """Create and link the exact relocation invoice inside confirmation."""

    amount = Decimal(request.field_fee_amount or 0)
    if amount <= Decimal("0.00"):
        request.execution_state = SubscriptionChangeExecutionState.payment_settled
        return None
    if request.field_fee_invoice_id is not None:
        return db.get(Invoice, request.field_fee_invoice_id)
    subscription = db.get(Subscription, request.subscription_id)
    if subscription is None:
        raise SubscriptionChangeExecutionError(
            "subscription_not_found", "Subscription not found"
        )
    currency = str(request.field_fee_currency or "").upper()
    if len(currency) != 3:
        raise SubscriptionChangeExecutionError(
            "relocation_currency_missing", "Relocation charge currency is missing"
        )
    issued_at = datetime.now(UTC)
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=subscription.subscriber_id,
            status=InvoiceStatus.issued,
            currency=currency,
            subtotal=amount,
            total=amount,
            balance_due=amount,
            issued_at=issued_at,
            due_at=issued_at,
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref=f"subscription-change:{request.id}:field-fee",
            due_date_policy_version="subscription-change-pay-before-execution-v1",
            memo=f"Service relocation charge · request {request.id}",
        ),
        commit=False,
    )
    invoice.metadata_ = {
        "payment_flow": "subscription_relocation",
        "subscription_change_request_id": str(request.id),
        "field_quote_fingerprint": request.field_quote_fingerprint,
    }
    request.field_fee_invoice_id = invoice.id
    request.execution_state = SubscriptionChangeExecutionState.awaiting_payment
    db.flush()
    return invoice


def prepare_remote_reprovision(
    db: Session, request: SubscriptionChangeRequest
) -> RemoteReprovisionOutcome:
    """Persist the exact target profile without changing live RADIUS intent.

    Confirmation records the target and exact credential/user scope. The
    desired credential profile remains unchanged until an operator confirms
    the execution-time price and explicitly starts provisioning.
    """

    if request.remote_radius_profile_id is not None:
        return RemoteReprovisionOutcome(
            request.id,
            request.remote_radius_profile_id,
            request.remote_radius_user_id,
            True,
        )
    profiles = list(
        db.scalars(
            select(OfferRadiusProfile).where(
                OfferRadiusProfile.offer_id == request.requested_offer_id
            )
        ).all()
    )
    if not profiles:
        raise SubscriptionChangeExecutionError(
            "remote_radius_profile_ambiguous",
            "The requested plan has no RADIUS profile configured",
        )
    if len(profiles) > 1:
        raise SubscriptionChangeExecutionError(
            "remote_radius_profile_ambiguous",
            "The requested plan has multiple RADIUS profiles; exactly one is required",
        )
    credential = _active_remote_credential(db, subscription_id=request.subscription_id)
    if credential is None:
        raise SubscriptionChangeExecutionError(
            "remote_access_credential_ambiguous",
            "Remote reprovisioning requires exactly one active subscription credential",
        )
    radius_user = db.scalar(
        select(RadiusUser).where(RadiusUser.access_credential_id == credential.id)
    )
    request.remote_radius_profile_id = profiles[0].profile_id
    request.remote_radius_user_id = radius_user.id if radius_user is not None else None
    request.remote_reprovision_requested_at = None
    request.execution_state = SubscriptionChangeExecutionState.provisioning
    db.flush()
    return RemoteReprovisionOutcome(
        request.id,
        profiles[0].profile_id,
        radius_user.id if radius_user is not None else None,
        False,
    )


def stage_remote_reprovision(
    db: Session, request: SubscriptionChangeRequest
) -> RemoteReprovisionOutcome:
    """Stage the confirmed target profile for the explicit RADIUS projection."""

    prepared = prepare_remote_reprovision(db, request)
    credential = _active_remote_credential(db, subscription_id=request.subscription_id)
    if credential is None:
        raise SubscriptionChangeExecutionError(
            "remote_access_credential_ambiguous",
            "Remote reprovisioning requires exactly one active subscription credential",
        )
    stage_subscription_radius_profile(
        db,
        subscription_id=request.subscription_id,
        credential_id=credential.id,
        radius_profile_id=prepared.radius_profile_id,
    )
    request.remote_reprovision_requested_at = datetime.now(UTC)
    request.execution_state = SubscriptionChangeExecutionState.provisioning
    db.flush()
    return prepared


def _provision_and_verify_remote_change(
    db: Session,
    command: RemoteProvisionActionCommand,
) -> RemoteProvisionActionOutcome:
    """Project and verify one already-confirmed remote service change.

    This operator fallback does not approve intent. It replays the staged
    desired profile through the canonical scoped RADIUS projection and applies
    the commercial plan only after the exact fresh local observation exists.
    """

    key = command.idempotency_key.strip()
    if len(key) < 16:
        raise SubscriptionChangeExecutionError(
            "reconciliation_key_invalid", "Idempotency key is too short"
        )
    reason_value = command.reason.strip()
    if len(reason_value) < 8:
        raise SubscriptionChangeExecutionError(
            "reconciliation_reason_invalid", "Provisioning reason is too short"
        )
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    operation_reference = f"remote-plan-change:{command.request_id}:{key_hash[:12]}"
    request = _lock_request(db, command.request_id)
    subscription = db.get(Subscription, request.subscription_id)
    if (
        request.subscription_id != command.subscription_id
        or subscription is None
        or subscription.subscriber_id != command.account_id
    ):
        raise SubscriptionChangeExecutionError(
            "service_change_not_found",
            "Remote service-change request does not belong to this customer service",
        )
    target_offer = db.get(CatalogOffer, request.requested_offer_id)
    target_offer_name = (
        target_offer.name if target_offer is not None else "Requested plan"
    )
    target_profile = (
        db.get(RadiusProfile, request.remote_radius_profile_id)
        if request.remote_radius_profile_id is not None
        else None
    )
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        return RemoteProvisionActionOutcome(
            request.id,
            request.subscription_id,
            RemoteProvisionActionStatus.replayed,
            target_offer_name,
            target_profile.name if target_profile is not None else "Target profile",
            operation_reference,
            f"{target_offer_name} was already provisioned and verified.",
        )
    if (
        request.status
        not in {
            SubscriptionChangeStatus.pending,
            SubscriptionChangeStatus.approved,
        }
        or request.execution_state != SubscriptionChangeExecutionState.provisioning
        or _delivery_mode(request) != "remote_reprovision"
    ):
        raise SubscriptionChangeExecutionError(
            "remote_reprovision_not_staged",
            "This service change is not awaiting remote RADIUS provisioning",
        )
    if (
        request.reconciliation_idempotency_key_hash is not None
        and request.reconciliation_idempotency_key_hash != key_hash
    ):
        raise SubscriptionChangeExecutionError(
            "reconciliation_key_conflict",
            "A different operator provisioning attempt is already recorded",
        )

    stored_review = _stored_price_review(request)
    review_effective_at = (
        stored_review.effective_at if stored_review is not None else datetime.now(UTC)
    )
    decision = _refreshed_financial_decision(
        db,
        request=request,
        subscription=subscription,
        effective_at=review_effective_at,
    )
    current_snapshot = decision.as_quote_dict()
    current_amount = _upgrade_amount(current_snapshot)
    current_currency = decision.currency.upper()
    confirmed_fingerprint = (command.confirmed_price_fingerprint or "").strip()
    previous_snapshot = request.confirmation_snapshot or {}
    previous_amount = (
        stored_review.current_amount
        if stored_review is not None
        else _upgrade_amount(previous_snapshot)
    )
    previous_currency = (
        stored_review.currency
        if stored_review is not None
        else str(previous_snapshot.get("currency") or current_currency).upper()
    )
    amount_changed = (
        current_amount != previous_amount or current_currency != previous_currency
    )
    if stored_review is not None:
        stored_review_was_confirmed = confirmed_fingerprint == stored_review.fingerprint
        amount_changed_since_review = (
            current_amount != stored_review.current_amount
            or current_currency != stored_review.currency
        )
        review_required = not stored_review_was_confirmed or amount_changed_since_review
    else:
        current_decision_was_confirmed = confirmed_fingerprint == decision.fingerprint
        review_required = amount_changed and not current_decision_was_confirmed
    review = RemoteProvisionPriceReview(
        fingerprint=decision.fingerprint,
        effective_at=decision.effective_at,
        previous_amount=previous_amount,
        current_amount=current_amount,
        currency=current_currency,
        available_balance=decision.prepaid_funding_before,
        shortfall=decision.shortfall,
        allowed=decision.allowed,
        reason=decision.reason,
    )
    if review_required or not decision.allowed:
        snapshot = dict(previous_snapshot)
        snapshot[_PRICE_REVIEW_SNAPSHOT_KEY] = _price_review_snapshot(review)
        request.confirmation_snapshot = snapshot
        request.execution_state = SubscriptionChangeExecutionState.provisioning
        action_status = (
            RemoteProvisionActionStatus.price_review_required
            if review_required
            else RemoteProvisionActionStatus.billing_blocked
        )
        message = (
            "The upgrade price changed from "
            f"{_format_amount(previous_currency, previous_amount)} to "
            f"{_format_amount(current_currency, current_amount)}. Review and "
            "confirm the new amount."
            if review_required
            else _billing_block_message(review)
        )
        stage_audit_event(
            db,
            action="review_remote_plan_change_price",
            entity_type="subscription_change_request",
            entity_id=str(request.id),
            actor_type=AuditActorType.user,
            actor_id=command.actor_id,
            status_code=409,
            is_success=False,
            metadata={
                "subscription_id": str(subscription.id),
                "result": action_status.value,
                "previous_amount": str(previous_amount),
                "current_amount": str(current_amount),
                "currency": current_currency,
                "available_balance": str(decision.prepaid_funding_before),
                "shortfall": str(decision.shortfall),
                "billing_reason": decision.reason,
                "operation_reference": operation_reference,
            },
        )
        db.commit()
        return RemoteProvisionActionOutcome(
            request.id,
            request.subscription_id,
            action_status,
            target_offer_name,
            target_profile.name if target_profile is not None else "Target profile",
            operation_reference,
            message,
            review,
        )

    refreshed_snapshot = dict(previous_snapshot)
    refreshed_snapshot.update(json.loads(json.dumps(current_snapshot, default=str)))
    refreshed_snapshot.pop(_PRICE_REVIEW_SNAPSHOT_KEY, None)
    request.confirmation_snapshot = refreshed_snapshot
    request.confirmation_preview_fingerprint = decision.fingerprint
    request.confirmed_at = datetime.now(UTC)

    credential = _active_remote_credential(db, subscription_id=subscription.id)
    if credential is None:
        raise SubscriptionChangeExecutionError(
            "remote_access_credential_ambiguous",
            "Remote reprovisioning requires exactly one active subscription credential",
        )
    credential_id = credential.id
    previous_radius_profile_id = credential.radius_profile_id
    projection_attempted = False
    commercial_finalization_started = False
    try:
        staged = stage_remote_reprovision(db, request)
        db.flush()
        target_profile = db.get(RadiusProfile, staged.radius_profile_id)
        from app.services.radius import reconcile_subscription_connectivity

        projection_attempted = True
        projection = reconcile_subscription_connectivity(db, str(subscription.id))
        if not projection.ok:
            reason_by_disposition = {
                "ineligible_subscription": "The subscription is not eligible for RADIUS provisioning",
                "missing_login": "The subscription has no RADIUS login configured",
                "target_unavailable": "No active RADIUS projection target is available",
                "unbuildable_login": "The RADIUS projection could not build this login",
            }
            raise SubscriptionChangeExecutionError(
                "remote_reprovision_verification_missing",
                reason_by_disposition.get(
                    projection.disposition.value,
                    "RADIUS provisioning did not converge",
                ),
            )

        request.reconciliation_idempotency_key_hash = key_hash
        request.reconciliation_actor_id = command.actor_id[:120]
        request.reconciliation_reason = reason_value
        request.reconciled_at = datetime.now(UTC)
        stage_audit_event(
            db,
            action="provision_remote_plan_change",
            entity_type="subscription_change_request",
            entity_id=str(request.id),
            actor_type=AuditActorType.user,
            actor_id=command.actor_id,
            metadata={
                "subscription_id": str(subscription.id),
                "target_offer_name": target_offer_name,
                "target_profile_name": (
                    target_profile.name if target_profile is not None else None
                ),
                "operation_reference": operation_reference,
                "result": RemoteProvisionActionStatus.completed.value,
                "confirmed_upgrade_amount": str(current_amount),
                "currency": current_currency,
                "price_fingerprint": decision.fingerprint,
            },
        )
        commercial_finalization_started = True
        finalized = finalize_verified_remote_reprovision(
            db, request_id=request.id, actor_id=command.actor_id
        )
    except Exception as failure:
        if not projection_attempted:
            db.rollback()
            raise
        if commercial_finalization_started:
            db.rollback()
        try:
            commercial_change_completed = _recover_remote_provision_failure(
                db,
                command=command,
                credential_id=credential_id,
                previous_radius_profile_id=previous_radius_profile_id,
                operation_reference=operation_reference,
                failure=failure,
            )
            db.commit()
        except Exception as recovery_error:
            db.rollback()
            try:
                stage_audit_event(
                    db,
                    action="rollback_remote_plan_change_provisioning",
                    entity_type="subscription_change_request",
                    entity_id=str(command.request_id),
                    actor_type=AuditActorType.user,
                    actor_id=command.actor_id,
                    status_code=500,
                    is_success=False,
                    metadata={
                        "subscription_id": str(command.subscription_id),
                        "operation_reference": operation_reference,
                        "result": "radius_recovery_failed",
                        "failure_type": type(failure).__name__,
                        "recovery_failure_type": type(recovery_error).__name__,
                    },
                )
                db.commit()
            except Exception:
                db.rollback()
            raise SubscriptionChangeExecutionError(
                "remote_reprovision_compensation_failed",
                "Provisioning failed and the previous RADIUS profile could not be "
                "restored automatically. The request remains retryable; reconcile "
                f"operation {operation_reference} before retrying.",
            ) from recovery_error
        if commercial_change_completed:
            return RemoteProvisionActionOutcome(
                request.id,
                request.subscription_id,
                RemoteProvisionActionStatus.completed,
                target_offer_name,
                target_profile.name if target_profile is not None else "Target profile",
                operation_reference,
                f"{target_offer_name} profile verified. Plan change completed.",
            )
        raise
    return RemoteProvisionActionOutcome(
        finalized.id,
        finalized.subscription_id,
        RemoteProvisionActionStatus.completed,
        target_offer_name,
        target_profile.name if target_profile is not None else "Target profile",
        operation_reference,
        f"{target_offer_name} profile verified. Plan change completed.",
    )


def provision_and_verify_remote_change(
    db: Session,
    command: RemoteProvisionActionCommand,
) -> RemoteProvisionActionOutcome:
    """Execute remote provisioning and propagate errors from a clean session.

    The web adapter records failed attempts after this command returns. Ensure
    that audit persistence cannot commit partially staged price or RADIUS state
    from any validation, projection, finalization, or recovery failure.
    """

    try:
        return _provision_and_verify_remote_change(db, command)
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise


def finalize_verified_remote_reprovision(
    db: Session, *, request_id: UUID, actor_id: str
) -> SubscriptionChangeRequest:
    """Apply a remote change only from exact, fresh RADIUS read-model evidence."""

    request = _lock_request(db, request_id)
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        return request
    if (
        request.execution_state != SubscriptionChangeExecutionState.provisioning
        or request.remote_radius_profile_id is None
        or request.remote_reprovision_requested_at is None
    ):
        raise SubscriptionChangeExecutionError(
            "remote_reprovision_not_staged",
            "Remote reprovisioning has not been staged",
        )
    radius_user = (
        db.get(RadiusUser, request.remote_radius_user_id)
        if request.remote_radius_user_id is not None
        else db.scalar(
            select(RadiusUser).where(
                RadiusUser.subscription_id == request.subscription_id
            )
        )
    )
    observed_at = radius_user.last_sync_at if radius_user is not None else None
    if observed_at is not None and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    requested_at = request.remote_reprovision_requested_at
    if requested_at is not None and requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    if (
        radius_user is None
        or radius_user.subscription_id != request.subscription_id
        or radius_user.radius_profile_id != request.remote_radius_profile_id
        or observed_at is None
        or requested_at is None
        or observed_at < requested_at
    ):
        raise SubscriptionChangeExecutionError(
            "remote_reprovision_verification_missing",
            "The exact target RADIUS profile has not been observed after staging",
        )
    request.remote_radius_user_id = radius_user.id
    request.provisioning_verified_at = observed_at
    request.execution_state = SubscriptionChangeExecutionState.provisioning_verified
    if request.status == SubscriptionChangeStatus.pending:
        subscription_change_requests.approve(db, str(request.id), commit=False)
    if request.status != SubscriptionChangeStatus.approved:
        raise SubscriptionChangeExecutionError(
            "service_change_not_finalizable", "Service change cannot be finalized"
        )
    applied = subscription_change_requests.apply(
        db,
        str(request.id),
        plan_change_operation_key=f"subscription-change:{request.id}:remote-finalize",
        plan_change_preview_fingerprint=request.confirmation_preview_fingerprint,
        plan_change_effective_at=_confirmation_effective_at(request),
        plan_change_actor_id=actor_id,
    )
    applied.execution_state = SubscriptionChangeExecutionState.completed
    db.commit()
    db.refresh(applied)
    return applied


def _confirmation_effective_at(request: SubscriptionChangeRequest) -> datetime | None:
    snapshot = request.confirmation_snapshot or {}
    raw = snapshot.get("preview_effective_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def settle_relocation_payment(
    db: Session, *, request_id: UUID, payment_id: UUID
) -> FulfillmentOutcome:
    """Admit canonical allocation evidence and release field fulfillment once."""

    request = _lock_request(db, request_id)
    if request.service_order_id is not None and request.work_order_id is not None:
        return FulfillmentOutcome(
            request.id, request.service_order_id, request.work_order_id, True
        )
    if request.execution_state not in {
        SubscriptionChangeExecutionState.awaiting_payment,
        SubscriptionChangeExecutionState.payment_settled,
    }:
        raise SubscriptionChangeExecutionError(
            "service_change_not_awaiting_payment",
            "Service change is not awaiting payment",
        )
    if request.field_fee_invoice_id is None:
        raise SubscriptionChangeExecutionError(
            "relocation_invoice_missing", "Relocation invoice evidence is missing"
        )
    invoice = db.get(Invoice, request.field_fee_invoice_id)
    payment = db.get(Payment, payment_id)
    if invoice is None or payment is None:
        raise SubscriptionChangeExecutionError(
            "settlement_evidence_missing", "Canonical settlement evidence is missing"
        )
    allocated = db.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
            PaymentAllocation.invoice_id == invoice.id,
            PaymentAllocation.payment_id == payment.id,
            PaymentAllocation.is_active.is_(True),
        )
    )
    expected = Decimal(request.field_fee_amount or 0)
    if (
        invoice.status != InvoiceStatus.paid
        or Decimal(allocated or 0) < expected
        or invoice.currency != request.field_fee_currency
        or Decimal(invoice.total or 0) != expected
    ):
        raise SubscriptionChangeExecutionError(
            "relocation_fee_not_settled",
            "The exact relocation charge has not been canonically settled",
        )
    subscription = db.get(Subscription, request.subscription_id)
    if subscription is None:
        raise SubscriptionChangeExecutionError(
            "subscription_not_found", "Subscription not found"
        )
    request.field_fee_payment_id = payment.id
    request.payment_settled_at = request.payment_settled_at or datetime.now(UTC)
    request.execution_state = SubscriptionChangeExecutionState.payment_settled
    service_order = ServiceOrder(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        idempotency_key=f"subscription-change:{request.id}:service-order",
        status=ServiceOrderStatus.submitted,
        order_type=ServiceOrderType.change_service,
        notes="Field relocation issued from canonical service-change intent",
        execution_context={
            "subscription_change_request_id": str(request.id),
            "target_service_address_id": str(request.target_service_address_id),
            "field_fee_invoice_id": str(invoice.id),
            "field_fee_payment_id": str(payment.id),
        },
    )
    db.add(service_order)
    db.flush()
    emit_event(
        db,
        EventType.service_order_created,
        {
            "service_order_id": str(service_order.id),
            "subscription_change_request_id": str(request.id),
            "order_type": ServiceOrderType.change_service.value,
        },
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        service_order_id=service_order.id,
    )
    work_order = work_order_commands.create(
        db,
        WorkOrderHeaderCreate(
            title="Service relocation",
            subscriber_id=subscription.subscriber_id,
            description="Execute the approved service-address relocation.",
            status="scheduled",
            priority="normal",
            work_type="relocation",
            address=f"Address reference {request.target_service_address_id}",
            tags=["service-change", "relocation"],
        ),
        request_id=f"subscription-change:{request.id}:work-order",
        idempotency_key=f"subscription-change:{request.id}:work-order",
        commit=False,
    )
    request.service_order_id = service_order.id
    request.work_order_id = work_order.id
    request.execution_state = SubscriptionChangeExecutionState.fulfillment_released
    db.flush()
    return FulfillmentOutcome(request.id, service_order.id, work_order.id, False)


def finalize_verified_service_change(
    db: Session,
    *,
    request_id: UUID,
    readiness_decision_id: UUID,
    actor_id: str,
) -> SubscriptionChangeRequest:
    """Apply address/offer only from the exact activated readiness decision."""

    request = _lock_request(db, request_id)
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        return request
    decision = db.get(ProvisioningReadinessDecision, readiness_decision_id)
    if (
        decision is None
        or decision.service_order_id != request.service_order_id
        or decision.status != ProvisioningReadinessDecisionStatus.activated
    ):
        raise SubscriptionChangeExecutionError(
            "provisioning_verification_missing",
            "The exact service order has not passed provisioning verification",
        )
    request.provisioning_readiness_decision_id = decision.id
    request.provisioning_verified_at = datetime.now(UTC)
    request.execution_state = SubscriptionChangeExecutionState.provisioning_verified
    if request.status == SubscriptionChangeStatus.pending:
        subscription_change_requests.approve(db, str(request.id), commit=False)
    if request.status != SubscriptionChangeStatus.approved:
        raise SubscriptionChangeExecutionError(
            "service_change_not_finalizable", "Service change cannot be finalized"
        )
    applied = subscription_change_requests.apply(
        db,
        str(request.id),
        plan_change_operation_key=f"subscription-change:{request.id}:finalize",
        plan_change_actor_id=actor_id,
    )
    applied.execution_state = SubscriptionChangeExecutionState.completed
    db.commit()
    db.refresh(applied)
    return applied


def audit_execution_chain(
    db: Session, *, request_id: UUID
) -> tuple[ExecutionDrift, ...]:
    """Report deterministic drift without changing authoritative state."""

    request = db.get(SubscriptionChangeRequest, request_id)
    if request is None:
        return (ExecutionDrift(request_id, "service_change_not_found", False),)
    findings: list[ExecutionDrift] = []
    if (
        request.execution_state == SubscriptionChangeExecutionState.provisioning
        and _delivery_mode(request) == "remote_reprovision"
        and _remote_radius_verification_ready(db, request)
    ):
        findings.append(
            ExecutionDrift(request.id, "remote_verified_not_finalized", True)
        )
    if (
        request.execution_state == SubscriptionChangeExecutionState.awaiting_payment
        and request.field_fee_invoice_id is not None
    ):
        payment_id = _settled_payment_id(db, request)
        if payment_id is not None:
            findings.append(ExecutionDrift(request.id, "paid_not_released", True))
    if request.execution_state == SubscriptionChangeExecutionState.payment_settled and (
        request.service_order_id is None or request.work_order_id is None
    ):
        findings.append(
            ExecutionDrift(
                request.id,
                "settled_not_released",
                _settled_payment_id(db, request) is not None,
            )
        )
    if request.service_order_id is not None and request.execution_state in {
        SubscriptionChangeExecutionState.fulfillment_released,
        SubscriptionChangeExecutionState.provisioning,
    }:
        activated = db.scalar(
            select(ProvisioningReadinessDecision.id).where(
                ProvisioningReadinessDecision.service_order_id
                == request.service_order_id,
                ProvisioningReadinessDecision.status
                == ProvisioningReadinessDecisionStatus.activated,
            )
        )
        if activated is not None:
            findings.append(ExecutionDrift(request.id, "verified_not_finalized", True))
    if request.execution_state == SubscriptionChangeExecutionState.completed:
        subscription = db.get(Subscription, request.subscription_id)
        if subscription is None or (
            subscription.offer_id != request.requested_offer_id
            or (
                request.target_service_address_id is not None
                and subscription.service_address_id != request.target_service_address_id
            )
        ):
            findings.append(
                ExecutionDrift(request.id, "completed_subscription_drift", False)
            )
    return tuple(findings)


def inspect_execution_chain_reconciliation(
    db: Session, *, limit: int = 200
) -> ExecutionReconciliationInspection:
    """Return bounded, read-only interrupted-chain evidence for operators."""

    requests = list(
        db.scalars(
            select(SubscriptionChangeRequest)
            .where(SubscriptionChangeRequest.is_active.is_(True))
            .order_by(SubscriptionChangeRequest.updated_at.desc())
            .limit(max(1, min(limit, 500)))
        ).all()
    )
    items: list[ExecutionReconciliationItem] = []
    for request in requests:
        findings = audit_execution_chain(db, request_id=request.id)
        if not findings:
            continue
        items.append(
            ExecutionReconciliationItem(
                request_id=request.id,
                subscription_id=request.subscription_id,
                status=request.status.value,
                execution_state=(
                    request.execution_state.value
                    if request.execution_state is not None
                    else "unknown"
                ),
                findings=findings,
                reviewed_head=_execution_reviewed_head(request, findings),
                updated_at=request.updated_at,
            )
        )
    return ExecutionReconciliationInspection(tuple(items), datetime.now(UTC))


def reconcile_execution_chain(
    db: Session,
    *,
    request_id: UUID,
    expected_head: str,
    idempotency_key: str,
    actor_id: str,
    reason: str,
) -> ExecutionReconciliationOutcome:
    """Perform one reviewed, idempotent repair from canonical evidence."""

    if len(expected_head) != 64:
        raise SubscriptionChangeExecutionError(
            "reconciliation_head_invalid", "Reviewed reconciliation head is invalid"
        )
    key = idempotency_key.strip()
    if len(key) < 16:
        raise SubscriptionChangeExecutionError(
            "reconciliation_key_invalid", "Idempotency key is too short"
        )
    reason_value = reason.strip()
    if len(reason_value) < 8:
        raise SubscriptionChangeExecutionError(
            "reconciliation_reason_invalid", "Reconciliation reason is too short"
        )
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    request = _lock_request(db, request_id)
    if request.reconciliation_idempotency_key_hash == key_hash:
        if request.reconciliation_reviewed_head != expected_head:
            raise SubscriptionChangeExecutionError(
                "reconciliation_key_conflict",
                "Idempotency key was already used for different reviewed evidence",
            )
        return ExecutionReconciliationOutcome(
            request.id,
            request.execution_state.value if request.execution_state else "unknown",
            True,
            expected_head,
        )
    existing_key = db.scalar(
        select(SubscriptionChangeRequest.id).where(
            SubscriptionChangeRequest.reconciliation_idempotency_key_hash == key_hash
        )
    )
    if existing_key is not None:
        raise SubscriptionChangeExecutionError(
            "reconciliation_key_conflict",
            "Idempotency key is already bound to another service change",
        )
    findings = audit_execution_chain(db, request_id=request.id)
    current_head = _execution_reviewed_head(request, findings)
    if current_head != expected_head:
        raise SubscriptionChangeExecutionError(
            "reconciliation_head_stale",
            "Execution evidence changed; refresh and review before repairing",
        )
    if not findings or not any(item.repairable for item in findings):
        raise SubscriptionChangeExecutionError(
            "reconciliation_not_repairable",
            "This execution chain has no repairable canonical drift",
        )
    repaired = repair_execution_chain(db, request_id=request.id, actor_id=actor_id)
    repaired.reconciliation_idempotency_key_hash = key_hash
    repaired.reconciliation_reviewed_head = expected_head
    repaired.reconciliation_actor_id = actor_id[:120]
    repaired.reconciliation_reason = reason_value
    repaired.reconciled_at = datetime.now(UTC)
    db.commit()
    return ExecutionReconciliationOutcome(
        repaired.id,
        repaired.execution_state.value if repaired.execution_state else "unknown",
        False,
        expected_head,
    )


def repair_execution_chain(
    db: Session, *, request_id: UUID, actor_id: str
) -> SubscriptionChangeRequest:
    """Idempotently resume a chain from canonical persisted evidence."""

    request = _lock_request(db, request_id)
    if (
        request.execution_state == SubscriptionChangeExecutionState.provisioning
        and _delivery_mode(request) == "remote_reprovision"
    ):
        return finalize_verified_remote_reprovision(
            db, request_id=request.id, actor_id=actor_id
        )
    if request.execution_state in {
        SubscriptionChangeExecutionState.awaiting_payment,
        SubscriptionChangeExecutionState.payment_settled,
    }:
        payment_id = _settled_payment_id(db, request)
        if payment_id is None:
            raise SubscriptionChangeExecutionError(
                "relocation_fee_not_settled",
                "No canonical settlement is available for repair",
            )
        settle_relocation_payment(db, request_id=request.id, payment_id=payment_id)
        request = _lock_request(db, request.id)
    if request.service_order_id is not None:
        decision = db.scalar(
            select(ProvisioningReadinessDecision)
            .where(
                ProvisioningReadinessDecision.service_order_id
                == request.service_order_id,
                ProvisioningReadinessDecision.status
                == ProvisioningReadinessDecisionStatus.activated,
            )
            .order_by(ProvisioningReadinessDecision.decided_at.desc())
            .limit(1)
        )
        if decision is not None:
            return finalize_verified_service_change(
                db,
                request_id=request.id,
                readiness_decision_id=decision.id,
                actor_id=actor_id,
            )
    return request


def _settled_payment_id(db: Session, request: SubscriptionChangeRequest) -> UUID | None:
    invoice = (
        db.get(Invoice, request.field_fee_invoice_id)
        if request.field_fee_invoice_id is not None
        else None
    )
    expected = Decimal(request.field_fee_amount or 0)
    if (
        invoice is None
        or invoice.status != InvoiceStatus.paid
        or expected <= Decimal("0.00")
        or invoice.currency != request.field_fee_currency
        or Decimal(invoice.total or 0) != expected
    ):
        return None
    allocations = list(
        db.scalars(
            select(PaymentAllocation)
            .where(
                PaymentAllocation.invoice_id == request.field_fee_invoice_id,
                PaymentAllocation.is_active.is_(True),
            )
            .order_by(PaymentAllocation.created_at.asc())
        ).all()
    )
    for allocation in allocations:
        allocated = db.scalar(
            select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
                PaymentAllocation.invoice_id == invoice.id,
                PaymentAllocation.payment_id == allocation.payment_id,
                PaymentAllocation.is_active.is_(True),
            )
        )
        if Decimal(allocated or 0) >= expected:
            return allocation.payment_id
    return None


def _execution_reviewed_head(
    request: SubscriptionChangeRequest, findings: tuple[ExecutionDrift, ...]
) -> str:
    evidence = {
        "request_id": str(request.id),
        "updated_at": request.updated_at.isoformat(),
        "status": request.status.value,
        "execution_state": (
            request.execution_state.value if request.execution_state else None
        ),
        "invoice_id": str(request.field_fee_invoice_id or ""),
        "payment_id": str(request.field_fee_payment_id or ""),
        "service_order_id": str(request.service_order_id or ""),
        "work_order_id": str(request.work_order_id or ""),
        "readiness_id": str(request.provisioning_readiness_decision_id or ""),
        "remote_profile_id": str(request.remote_radius_profile_id or ""),
        "remote_user_id": str(request.remote_radius_user_id or ""),
        "findings": sorted((item.code, item.repairable) for item in findings),
    }
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _delivery_mode(request: SubscriptionChangeRequest) -> str | None:
    snapshot = request.confirmation_snapshot or {}
    value = snapshot.get("delivery_mode")
    return value if isinstance(value, str) else None


def _remote_radius_verification_ready(
    db: Session, request: SubscriptionChangeRequest
) -> bool:
    if (
        request.remote_radius_profile_id is None
        or request.remote_reprovision_requested_at is None
    ):
        return False
    users = list(
        db.scalars(
            select(RadiusUser).where(
                RadiusUser.subscription_id == request.subscription_id,
                RadiusUser.radius_profile_id == request.remote_radius_profile_id,
            )
        ).all()
    )
    if len(users) != 1 or users[0].last_sync_at is None:
        return False
    observed_at = users[0].last_sync_at
    requested_at = request.remote_reprovision_requested_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=UTC)
    return observed_at >= requested_at


__all__ = [
    "ExecutionDrift",
    "ExecutionReconciliationInspection",
    "ExecutionReconciliationItem",
    "ExecutionReconciliationOutcome",
    "FulfillmentOutcome",
    "RemoteProvisionActionCommand",
    "RemoteProvisionActionOutcome",
    "RemoteProvisionActionStatus",
    "RemoteProvisionPriceReview",
    "SubscriptionChangeExecutionError",
    "RemoteReprovisionOutcome",
    "finalize_verified_remote_reprovision",
    "prepare_remote_reprovision",
    "finalize_verified_service_change",
    "audit_execution_chain",
    "inspect_execution_chain_reconciliation",
    "provision_and_verify_remote_change",
    "reconcile_execution_chain",
    "repair_execution_chain",
    "settle_relocation_payment",
    "stage_relocation_charge",
    "stage_remote_reprovision",
]
