"""Transport-neutral sole writer for service-order status transitions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import SubscriptionStatus
from app.models.project import ProjectStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.vendor_routes import InstallationProjectStatus
from app.services.account_lifecycle import activate_subscription
from app.services.events import EventType, emit_event


class ServiceOrderLifecycleError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


_ALLOWED: dict[ServiceOrderStatus, frozenset[ServiceOrderStatus]] = {
    ServiceOrderStatus.draft: frozenset(
        {ServiceOrderStatus.submitted, ServiceOrderStatus.canceled}
    ),
    ServiceOrderStatus.submitted: frozenset(
        {
            ServiceOrderStatus.scheduled,
            ServiceOrderStatus.provisioning,
            ServiceOrderStatus.failed,
            ServiceOrderStatus.canceled,
        }
    ),
    ServiceOrderStatus.scheduled: frozenset(
        {
            ServiceOrderStatus.provisioning,
            ServiceOrderStatus.failed,
            ServiceOrderStatus.canceled,
        }
    ),
    ServiceOrderStatus.provisioning: frozenset(
        {
            ServiceOrderStatus.active,
            ServiceOrderStatus.failed,
            ServiceOrderStatus.canceled,
        }
    ),
    ServiceOrderStatus.failed: frozenset(
        {ServiceOrderStatus.submitted, ServiceOrderStatus.canceled}
    ),
    ServiceOrderStatus.active: frozenset(),
    ServiceOrderStatus.canceled: frozenset(),
}


def _status(value: ServiceOrderStatus | str) -> ServiceOrderStatus:
    try:
        return (
            value
            if isinstance(value, ServiceOrderStatus)
            else ServiceOrderStatus(value)
        )
    except ValueError as exc:
        raise ServiceOrderLifecycleError(
            "invalid_status", "Invalid service-order status", kind="invalid"
        ) from exc


def _lock(db: Session, service_order_id: UUID) -> ServiceOrder:
    order = db.scalars(
        select(ServiceOrder)
        .where(ServiceOrder.id == service_order_id)
        .with_for_update()
    ).one_or_none()
    if order is None:
        raise ServiceOrderLifecycleError(
            "service_order_not_found", "Service order not found", kind="not_found"
        )
    return order


def _implementation_ready(order: ServiceOrder) -> bool:
    if order.project_id is None and order.sales_order_line_id is None:
        return True
    return bool(
        order.project
        and order.project.status == ProjectStatus.completed.value
        and order.installation_project
        and order.installation_project.status
        == InstallationProjectStatus.verified.value
        and order.implementation_verified_at
        and order.implementation_verification_event_id
    )


def release_implementation(
    db: Session,
    *,
    service_order_id: UUID,
    installation_project_id: UUID,
    verification_event_id: UUID,
    actor_id: str,
) -> bool:
    order = _lock(db, service_order_id)
    if order.installation_project_id != installation_project_id:
        raise ServiceOrderLifecycleError(
            "installation_project_mismatch",
            "Service order is not bound to the verified installation project",
            kind="invalid",
        )
    if order.installation_project is None or (
        order.installation_project.status != InstallationProjectStatus.verified.value
    ):
        raise ServiceOrderLifecycleError(
            "implementation_not_verified",
            "Installation project is not verified",
        )
    if order.project is None or order.project.status != ProjectStatus.completed.value:
        raise ServiceOrderLifecycleError(
            "project_not_completed", "Native project is not completed"
        )
    if order.implementation_verification_event_id is not None:
        if order.implementation_verification_event_id != verification_event_id:
            raise ServiceOrderLifecycleError(
                "verification_evidence_conflict",
                "Service order already carries different verification evidence",
            )
        return False
    order.implementation_verified_at = datetime.now(UTC)
    order.implementation_verification_event_id = verification_event_id
    previous = order.status
    if previous == ServiceOrderStatus.draft:
        order.status = ServiceOrderStatus.submitted
    elif previous not in {
        ServiceOrderStatus.submitted,
        ServiceOrderStatus.scheduled,
        ServiceOrderStatus.provisioning,
        ServiceOrderStatus.active,
    }:
        raise ServiceOrderLifecycleError(
            "service_order_not_releasable",
            f"Service order in status '{previous.value}' cannot be released",
        )
    emit_event(
        db,
        EventType.service_order_released,
        {
            "service_order_id": str(order.id),
            "project_id": str(order.project_id),
            "installation_project_id": str(installation_project_id),
            "verification_event_id": str(verification_event_id),
            "from_status": previous.value,
            "to_status": order.status.value,
            "sales_order_id": str(order.sales_order_id)
            if order.sales_order_id
            else None,
        },
        actor=actor_id,
        subscriber_id=order.subscriber_id,
        subscription_id=order.subscription_id,
        service_order_id=order.id,
    )
    db.flush()
    return True


def transition_service_order(
    db: Session,
    *,
    service_order_id: UUID,
    target_status: ServiceOrderStatus | str,
    actor_id: str,
    reason: str | None = None,
    event_evidence: Mapping[str, object] | None = None,
    emit_subscription_activation: bool = True,
    commit: bool = True,
) -> ServiceOrder:
    order = _lock(db, service_order_id)
    target = _status(target_status)
    previous = order.status
    if previous == target:
        return order
    if target not in _ALLOWED[previous]:
        raise ServiceOrderLifecycleError(
            "invalid_transition",
            f"Cannot move service order from '{previous.value}' to '{target.value}'",
        )
    if target not in {ServiceOrderStatus.draft, ServiceOrderStatus.canceled} and (
        not _implementation_ready(order)
    ):
        raise ServiceOrderLifecycleError(
            "implementation_not_ready",
            "Verified implementation is required before provisioning",
        )
    if (
        target == ServiceOrderStatus.active
        and previous != ServiceOrderStatus.provisioning
    ):
        raise ServiceOrderLifecycleError(
            "provisioning_result_required",
            "Only a successful provisioning run can activate a service order",
        )
    order.status = target
    if target == ServiceOrderStatus.active and order.subscription_id is not None:
        subscription = order.subscription
        if subscription is None:
            raise ServiceOrderLifecycleError(
                "subscription_not_found", "Service order Subscription not found"
            )
        if subscription.status == SubscriptionStatus.pending:
            activate_subscription(
                db,
                str(subscription.id),
                emit=emit_subscription_activation,
            )
        elif subscription.status != SubscriptionStatus.active:
            raise ServiceOrderLifecycleError(
                "subscription_not_activatable",
                f"Subscription in '{subscription.status.value}' cannot be activated",
            )
    event_type = (
        EventType.service_order_completed
        if target == ServiceOrderStatus.active
        else EventType.service_order_assigned
        if target == ServiceOrderStatus.provisioning
        else None
    )
    if event_type is not None:
        emit_event(
            db,
            event_type,
            {
                "service_order_id": str(order.id),
                "from_status": previous.value,
                "to_status": target.value,
                "reason": reason,
                "evidence": dict(event_evidence or {}),
                "sales_order_id": str(order.sales_order_id)
                if order.sales_order_id
                else None,
            },
            actor=actor_id,
            subscriber_id=order.subscriber_id,
            subscription_id=order.subscription_id,
            service_order_id=order.id,
        )
    db.flush()
    if commit:
        db.commit()
        db.refresh(order)
    return order


def restore_recorded_status(
    db: Session,
    *,
    service_order_id: UUID,
    target_status: ServiceOrderStatus | str,
    actor_id: str,
    reason: str,
) -> bool:
    """Restore administratively recorded state through the canonical writer.

    Recovery is not a business transition: it can reinstate a terminal state
    from a durable snapshot. It is nevertheless locked, idempotent, and emits
    append-only evidence instead of granting restore tooling a parallel writer.
    """

    actor = str(actor_id or "").strip()
    if not actor:
        raise ServiceOrderLifecycleError(
            "actor_required", "Recovery actor is required", kind="invalid"
        )
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ServiceOrderLifecycleError(
            "reason_required", "Recovery reason is required", kind="invalid"
        )
    order = _lock(db, service_order_id)
    target = _status(target_status)
    previous = order.status
    if previous == target:
        return False
    order.status = target
    emit_event(
        db,
        EventType.service_order_recovered,
        {
            "service_order_id": str(order.id),
            "from_status": previous.value,
            "to_status": target.value,
            "reason": normalized_reason,
        },
        actor=actor,
        subscriber_id=order.subscriber_id,
        subscription_id=order.subscription_id,
        service_order_id=order.id,
    )
    db.flush()
    return True


def record_provisioning_result(
    db: Session,
    *,
    service_order_id: UUID,
    succeeded: bool,
    readiness_decision_id: UUID,
    actor_id: str,
    reason: str | None = None,
) -> ServiceOrder:
    """Apply one provisioning-owner decision through the sole status writer."""

    return transition_service_order(
        db,
        service_order_id=service_order_id,
        target_status=(
            ServiceOrderStatus.active if succeeded else ServiceOrderStatus.failed
        ),
        actor_id=actor_id,
        reason=reason,
        event_evidence={"readiness_decision_id": str(readiness_decision_id)},
        emit_subscription_activation=False,
        commit=False,
    )
