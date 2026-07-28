"""Sole writer for durable implementation-to-CX handoff decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import SubscriptionStatus
from app.models.customer_experience import (
    CustomerExperienceHandoff,
    CustomerExperienceHandoffEvent,
    CustomerExperienceHandoffStatus,
)
from app.models.project import ProjectStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.sales import SalesOrderStatus
from app.models.vendor_routes import InstallationProjectStatus
from app.services.events import EventType, emit_event

POLICY_VERSION = 1


class CustomerExperienceHandoffError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


def _event(
    db: Session,
    *,
    handoff: CustomerExperienceHandoff,
    event_type: EventType,
    previous: str,
    target: str,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
) -> None:
    domain_event = emit_event(
        db,
        event_type,
        {
            "handoff_id": str(handoff.id),
            "subscriber_id": str(handoff.subscriber_id),
            "subscription_id": str(handoff.subscription_id),
            "sales_order_id": str(handoff.sales_order_id),
            "project_id": str(handoff.project_id),
            "service_order_id": str(handoff.service_order_id),
            "from_status": previous,
            "to_status": target,
            "reason": reason,
            "readiness_evidence": handoff.readiness_evidence,
        },
        actor=actor_id,
        subscriber_id=handoff.subscriber_id,
        subscription_id=handoff.subscription_id,
        service_order_id=handoff.service_order_id,
    )
    db.add(
        CustomerExperienceHandoffEvent(
            event_id=domain_event.event_id,
            handoff_id=handoff.id,
            event_type=domain_event.event_type.value,
            from_status=previous,
            to_status=target,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            decision_context={
                "policy_version": POLICY_VERSION,
                "readiness_evidence": handoff.readiness_evidence,
            },
            occurred_at=domain_event.occurred_at,
        )
    )


def _readiness(service_order: ServiceOrder) -> dict[str, object]:
    project = service_order.project
    installation = service_order.installation_project
    subscription = service_order.subscription
    sales_order = service_order.sales_order
    evidence: dict[str, object] = {
        "policy_version": POLICY_VERSION,
        "implementation_verified": bool(
            service_order.implementation_verified_at
            and installation
            and installation.status == InstallationProjectStatus.verified.value
        ),
        "project_completed": bool(
            project and project.status == ProjectStatus.completed.value
        ),
        "provisioning_completed": service_order.status == ServiceOrderStatus.active,
        "subscription_active": bool(
            subscription and subscription.status == SubscriptionStatus.active
        ),
        "sale_funded": bool(
            sales_order
            and sales_order.status
            in {SalesOrderStatus.paid.value, SalesOrderStatus.fulfilled.value}
        ),
    }
    evidence["eligible"] = all(
        bool(value)
        for key, value in evidence.items()
        if key not in {"policy_version", "eligible"}
    )
    return evidence


def ensure_ready_for_service_order(
    db: Session,
    *,
    service_order_id: UUID,
    actor_id: str,
) -> CustomerExperienceHandoff:
    order = db.scalars(
        select(ServiceOrder)
        .where(ServiceOrder.id == service_order_id)
        .with_for_update()
    ).one_or_none()
    if order is None:
        raise CustomerExperienceHandoffError(
            "service_order_not_found", "Service order not found", kind="not_found"
        )
    required = {
        "subscription_id": order.subscription_id,
        "sales_order_id": order.sales_order_id,
        "project_id": order.project_id,
        "installation_project_id": order.installation_project_id,
    }
    missing = sorted(key for key, value in required.items() if value is None)
    if missing:
        raise CustomerExperienceHandoffError(
            "incomplete_handoff_context",
            "Service order lacks structural lifecycle context: " + ", ".join(missing),
        )
    evidence = _readiness(order)
    handoff = db.scalars(
        select(CustomerExperienceHandoff).where(
            CustomerExperienceHandoff.subscription_id == order.subscription_id
        )
    ).one_or_none()
    if handoff is None:
        handoff = CustomerExperienceHandoff(
            subscriber_id=order.subscriber_id,
            subscription_id=order.subscription_id,
            sales_order_id=order.sales_order_id,
            project_id=order.project_id,
            installation_project_id=order.installation_project_id,
            service_order_id=order.id,
            status=CustomerExperienceHandoffStatus.pending.value,
            policy_version=POLICY_VERSION,
            readiness_evidence=evidence,
        )
        db.add(handoff)
        db.flush()
    else:
        expected = (
            order.subscriber_id,
            order.sales_order_id,
            order.project_id,
            order.installation_project_id,
            order.id,
        )
        actual = (
            handoff.subscriber_id,
            handoff.sales_order_id,
            handoff.project_id,
            handoff.installation_project_id,
            handoff.service_order_id,
        )
        if actual != expected:
            raise CustomerExperienceHandoffError(
                "handoff_context_mismatch",
                "Existing customer-experience handoff has different lifecycle roots",
            )
        handoff.readiness_evidence = evidence
    if (
        evidence["eligible"]
        and handoff.status == CustomerExperienceHandoffStatus.pending.value
    ):
        previous = handoff.status
        handoff.status = CustomerExperienceHandoffStatus.ready.value
        handoff.ready_at = datetime.now(UTC)
        _event(
            db,
            handoff=handoff,
            event_type=EventType.customer_experience_ready,
            previous=previous,
            target=handoff.status,
            actor_type="system",
            actor_id=actor_id,
            reason="Implementation, provisioning, funding, and access are ready",
        )
    db.flush()
    return handoff


def accept_handoff(
    db: Session,
    *,
    handoff_id: UUID,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
    commit: bool = True,
) -> CustomerExperienceHandoff:
    actor = str(actor_id or "").strip()
    if not actor:
        raise CustomerExperienceHandoffError(
            "actor_required", "Handoff actor is required", kind="invalid"
        )
    handoff = db.scalars(
        select(CustomerExperienceHandoff)
        .where(CustomerExperienceHandoff.id == handoff_id)
        .with_for_update()
    ).one_or_none()
    if handoff is None:
        raise CustomerExperienceHandoffError(
            "handoff_not_found", "Handoff not found", kind="not_found"
        )
    if handoff.status == CustomerExperienceHandoffStatus.accepted.value:
        return handoff
    if handoff.status != CustomerExperienceHandoffStatus.ready.value:
        raise CustomerExperienceHandoffError(
            "handoff_not_ready", "Only a ready handoff can be accepted"
        )
    previous = handoff.status
    handoff.status = CustomerExperienceHandoffStatus.accepted.value
    handoff.accepted_at = datetime.now(UTC)
    handoff.accepted_by_actor_type = str(actor_type or "staff_user")
    handoff.accepted_by_actor_id = actor
    handoff.attention_reason = None
    # The accepted output below is the durable trigger for SalesOrder
    # fulfilment: the registered lifecycle projection handler asks the
    # sales-order owner to apply it after this owner's fact commits.
    _event(
        db,
        handoff=handoff,
        event_type=EventType.customer_experience_accepted,
        previous=previous,
        target=handoff.status,
        actor_type=handoff.accepted_by_actor_type,
        actor_id=actor,
        reason=(reason or "").strip() or None,
    )
    db.flush()
    if commit:
        db.commit()
        db.refresh(handoff)
    return handoff


def mark_needs_attention(
    db: Session,
    *,
    handoff_id: UUID,
    actor_type: str,
    actor_id: str,
    reason: str,
    commit: bool = True,
) -> CustomerExperienceHandoff:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise CustomerExperienceHandoffError(
            "reason_required", "Attention reason is required", kind="invalid"
        )
    handoff = db.scalars(
        select(CustomerExperienceHandoff)
        .where(CustomerExperienceHandoff.id == handoff_id)
        .with_for_update()
    ).one_or_none()
    if handoff is None:
        raise CustomerExperienceHandoffError(
            "handoff_not_found", "Handoff not found", kind="not_found"
        )
    if handoff.status == CustomerExperienceHandoffStatus.needs_attention.value:
        if handoff.attention_reason == normalized_reason:
            return handoff
        raise CustomerExperienceHandoffError(
            "attention_reason_conflict", "Handoff already records a different issue"
        )
    if handoff.status not in {
        CustomerExperienceHandoffStatus.pending.value,
        CustomerExperienceHandoffStatus.ready.value,
    }:
        raise CustomerExperienceHandoffError(
            "handoff_terminal", "Accepted or canceled handoff cannot be blocked"
        )
    previous = handoff.status
    handoff.status = CustomerExperienceHandoffStatus.needs_attention.value
    handoff.attention_reason = normalized_reason
    _event(
        db,
        handoff=handoff,
        event_type=EventType.customer_experience_needs_attention,
        previous=previous,
        target=handoff.status,
        actor_type=str(actor_type or "staff_user"),
        actor_id=str(actor_id),
        reason=normalized_reason,
    )
    db.flush()
    if commit:
        db.commit()
        db.refresh(handoff)
    return handoff


def list_handoffs(
    db: Session, *, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[CustomerExperienceHandoff]:
    query = select(CustomerExperienceHandoff)
    if status:
        try:
            normalized = CustomerExperienceHandoffStatus(status).value
        except ValueError as exc:
            raise CustomerExperienceHandoffError(
                "invalid_status", "Invalid handoff status", kind="invalid"
            ) from exc
        query = query.where(CustomerExperienceHandoff.status == normalized)
    return list(
        db.scalars(
            query.order_by(CustomerExperienceHandoff.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


# --- receipted lifecycle-output consumption --------------------------------

_READINESS_DEFINITION = None  # built lazily to avoid import cycles


def consume_service_order_completion(
    db: Session,
    *,
    service_order_id: UUID,
    event_id,
    context,
) -> CustomerExperienceHandoff | None:
    """Receipt one committed service-order completion into CX readiness.

    The handoff readiness decision, its acceptance-deadline timer, and the
    unique ``(consumer, event_id)`` receipt commit atomically; a redelivery
    is an exact no-op.
    """
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec
    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import (
        OwnerCommandDefinition,
        execute_owner_command,
    )

    definition = OwnerCommandDefinition(
        owner="customer.experience_handoff",
        concern=("implementation-to-customer-experience readiness decision"),
        name="consume_service_order_completion",
    )

    def _effect() -> CustomerExperienceHandoff:
        handoff = ensure_ready_for_service_order(
            db,
            service_order_id=service_order_id,
            actor_id="sales.lifecycle_projection",
        )
        if handoff.status == CustomerExperienceHandoffStatus.ready.value:
            # The acceptance deadline is a durable per-handoff timer; a
            # handoff accepted or blocked before it fires makes the firing
            # a state-guarded no-op.
            from datetime import timedelta

            from app.services.owner_commands import CommandContext
            from app.services.runtime_durable_timers import (
                ScheduleTimerCommand,
                schedule_timer,
            )

            hours = int(
                settings_spec.resolve_value(
                    db, SettingDomain.workflow, "cx_acceptance_attention_hours"
                )
                or 72
            )
            due_at = datetime.now(UTC) + timedelta(hours=max(1, hours))
            schedule_timer(
                db,
                ScheduleTimerCommand(
                    owner="customer.experience_handoff",
                    entity_kind="cx_handoff",
                    entity_id=handoff.id,
                    purpose="cx_acceptance_due",
                    due_at=due_at,
                    output_event_type="sales.cx_acceptance_due",
                ),
                context=CommandContext.system(
                    actor="customer.experience_handoff",
                    scope=str(handoff.id),
                    reason="CX acceptance deadline",
                    idempotency_key=(
                        f"cx-acceptance:{handoff.id}:{due_at.isoformat()}"
                    ),
                ),
            )
        return handoff

    return execute_owner_command(
        db,
        definition=definition,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer="customer.experience_handoff",
            event_id=event_id,
            event_type="service_order.completed",
            producer_owner="operations.service_order_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )


def consume_cx_acceptance_due(
    db: Session,
    *,
    handoff_id: UUID,
    event_id,
    context,
) -> str | None:
    """Receipt one fired acceptance-deadline timer into needs-attention.

    A handoff already accepted, blocked, or canceled makes a stale firing
    an exact no-op; acceptance itself always remains a staff decision.
    """
    from app.services.events.owner_outputs import consume_owner_output
    from app.services.owner_commands import (
        OwnerCommandDefinition,
        execute_owner_command,
    )

    definition = OwnerCommandDefinition(
        owner="customer.experience_handoff",
        concern="CX acceptance and needs-attention lifecycle",
        name="consume_cx_acceptance_due",
    )

    def _effect() -> str:
        handoff = db.get(CustomerExperienceHandoff, handoff_id)
        if handoff is None:
            return "skipped_missing"
        if handoff.status != CustomerExperienceHandoffStatus.ready.value:
            return "skipped_state"
        mark_needs_attention(
            db,
            handoff_id=handoff.id,
            actor_type="system",
            actor_id="customer.experience_handoff",
            reason="Customer acceptance overdue",
            commit=False,
        )
        return "flagged"

    return execute_owner_command(
        db,
        definition=definition,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer="customer.experience_handoff",
            event_id=event_id,
            event_type="sales.cx_acceptance_due",
            producer_owner="runtime.durable_timers",
            context=context,
            operation=_effect,
        )[0],
    )
