"""Provisioning services compatibility module.

Re-exports helper functions, manager classes, and singleton service instances.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.provisioning_helpers import (
    ensure_ip_assignments_for_subscription,  # noqa: F401
    ensure_ipv4_assignment_for_subscription,  # noqa: F401
)
from app.services.provisioning_managers import (  # noqa: F401
    InstallAppointments,
    ProvisioningRuns,
    ProvisioningSteps,
    ProvisioningTasks,
    ProvisioningWorkflows,
    ServiceOrders,
    ServiceStateTransitions,
    install_appointments,
    provisioning_runs,
    provisioning_steps,
    provisioning_tasks,
    provisioning_workflows,
    service_orders,
    service_state_transitions,
)


def resolve_service_order_id_for_ont(db: Session, ont_id: str) -> str | None:
    """Find the active service order associated with an ONT unit.

    Returns the service order ID string, or None if no active order exists.
    """
    from app.models.provisioning import ServiceOrder, ServiceOrderStatus

    active_statuses = (
        ServiceOrderStatus.draft,
        ServiceOrderStatus.submitted,
        ServiceOrderStatus.scheduled,
        ServiceOrderStatus.provisioning,
        ServiceOrderStatus.active,
    )
    stmt = (
        select(ServiceOrder.id)
        .where(
            ServiceOrder.status.in_(active_statuses),
        )
        .order_by(ServiceOrder.created_at.desc())
        .limit(20)
    )
    for order_id in db.scalars(stmt).all():
        order = db.get(ServiceOrder, order_id)
        if not order:
            continue
        if str(getattr(order, "ont_unit_id", None) or "") == str(ont_id):
            return str(order.id)
        ec = getattr(order, "execution_context", None) or {}
        if str(ec.get("ont_unit_id", "")) == str(ont_id):
            return str(order.id)
    # If no order matches active/in-progress lifecycle states, fall back to
    # the most recent related order in any status so intent can still be restored
    # from recently completed/archived workflows.
    fallback_stmt = (
        select(ServiceOrder.id).order_by(ServiceOrder.created_at.desc()).limit(50)
    )
    for order_id in db.scalars(fallback_stmt).all():
        order = db.get(ServiceOrder, order_id)
        if not order:
            continue
        if str(getattr(order, "ont_unit_id", None) or "") == str(ont_id):
            return str(order.id)
        ec = getattr(order, "execution_context", None) or {}
        if str(ec.get("ont_unit_id", "")) == str(ont_id):
            return str(order.id)
    return None


__all__ = [name for name in globals() if not name.startswith("__")]
