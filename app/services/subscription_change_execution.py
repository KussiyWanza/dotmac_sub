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

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentAllocation
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
from app.services.events import EventType, emit_event
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


@dataclass(frozen=True, slots=True)
class RemoteProvisionActionOutcome:
    request_id: UUID
    subscription_id: UUID
    status: RemoteProvisionActionStatus
    target_offer_name: str
    target_profile_name: str
    operation_reference: str
    message: str


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
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=subscription.subscriber_id,
            status=InvoiceStatus.issued,
            currency=currency,
            subtotal=amount,
            total=amount,
            balance_due=amount,
            issued_at=datetime.now(UTC),
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


def stage_remote_reprovision(
    db: Session, request: SubscriptionChangeRequest
) -> RemoteReprovisionOutcome:
    """Stage the exact offer profile on one subscription-scoped credential.

    The live offer remains unchanged. A later verifier must observe the exact
    profile on the exact RADIUS user after this request watermark.
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
    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscription_id == request.subscription_id,
                AccessCredential.is_active.is_(True),
            )
        ).all()
    )
    if len(credentials) != 1:
        raise SubscriptionChangeExecutionError(
            "remote_access_credential_ambiguous",
            "Remote reprovisioning requires exactly one active subscription credential",
        )
    credential = credentials[0]
    radius_user = db.scalar(
        select(RadiusUser).where(RadiusUser.access_credential_id == credential.id)
    )
    requested_at = datetime.now(UTC)
    stage_subscription_radius_profile(
        db,
        subscription_id=request.subscription_id,
        credential_id=credential.id,
        radius_profile_id=profiles[0].profile_id,
    )
    request.remote_radius_profile_id = profiles[0].profile_id
    request.remote_radius_user_id = radius_user.id if radius_user is not None else None
    request.remote_reprovision_requested_at = requested_at
    request.execution_state = SubscriptionChangeExecutionState.provisioning
    db.flush()
    return RemoteReprovisionOutcome(
        request.id,
        profiles[0].profile_id,
        radius_user.id if radius_user is not None else None,
        False,
    )


def provision_and_verify_remote_change(
    db: Session,
    *,
    request_id: UUID,
    subscription_id: UUID,
    account_id: UUID,
    actor_id: str,
    idempotency_key: str,
    reason: str,
) -> RemoteProvisionActionOutcome:
    """Project and verify one already-confirmed remote service change.

    This operator fallback does not approve intent. It replays the staged
    desired profile through the canonical scoped RADIUS projection and applies
    the commercial plan only after the exact fresh local observation exists.
    """

    key = idempotency_key.strip()
    if len(key) < 16:
        raise SubscriptionChangeExecutionError(
            "reconciliation_key_invalid", "Idempotency key is too short"
        )
    reason_value = reason.strip()
    if len(reason_value) < 8:
        raise SubscriptionChangeExecutionError(
            "reconciliation_reason_invalid", "Provisioning reason is too short"
        )
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    operation_reference = f"remote-plan-change:{request_id}:{key_hash[:12]}"
    request = _lock_request(db, request_id)
    subscription = db.get(Subscription, request.subscription_id)
    if (
        request.subscription_id != subscription_id
        or subscription is None
        or subscription.subscriber_id != account_id
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

    staged = stage_remote_reprovision(db, request)
    target_profile = db.get(RadiusProfile, staged.radius_profile_id)
    from app.services.radius import reconcile_subscription_connectivity

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
    request.reconciliation_actor_id = actor_id[:120]
    request.reconciliation_reason = reason_value
    request.reconciled_at = datetime.now(UTC)
    finalized = finalize_verified_remote_reprovision(
        db, request_id=request.id, actor_id=actor_id
    )
    return RemoteProvisionActionOutcome(
        finalized.id,
        finalized.subscription_id,
        RemoteProvisionActionStatus.completed,
        target_offer_name,
        target_profile.name if target_profile is not None else "Target profile",
        operation_reference,
        f"{target_offer_name} profile verified. Plan change completed.",
    )


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
    "SubscriptionChangeExecutionError",
    "RemoteReprovisionOutcome",
    "finalize_verified_remote_reprovision",
    "finalize_verified_service_change",
    "audit_execution_chain",
    "inspect_execution_chain_reconciliation",
    "reconcile_execution_chain",
    "repair_execution_chain",
    "settle_relocation_payment",
    "stage_relocation_charge",
    "stage_remote_reprovision",
]
