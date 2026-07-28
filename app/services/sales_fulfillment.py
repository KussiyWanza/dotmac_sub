"""Cross-domain coordinator for SalesOrder implementation fulfillment.

Each domain owner still writes its own root. This coordinator carries exact
identifiers and commits the combined project/installation handoff once.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.domain_settings import SettingDomain
from app.models.project import Project, ProjectType
from app.models.provisioning import ServiceOrder
from app.models.sales import SalesOrder, SalesOrderStatus
from app.models.subscriber import Subscriber
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus
from app.services import installation_projects, projects, settings_spec
from app.services import service_address as service_address_service
from app.services.events import EventType, emit_event
from app.services.events.owner_outputs import consume_owner_output
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


class SalesFulfillmentError(ValueError):
    def __init__(self, code: str, message: str, *, kind: str = "conflict") -> None:
        super().__init__(message)
        self.code = code
        self.kind = kind


@dataclass(frozen=True)
class FulfillmentScope:
    sales_order: SalesOrder
    project: Project
    installation_project: InstallationProject


def _project_type(db: Session, order: SalesOrder) -> str:
    candidates = []
    if order.quote is not None and isinstance(order.quote.metadata_, dict):
        candidates.append(order.quote.metadata_.get("project_type"))
        install = order.quote.metadata_.get("install")
        if isinstance(install, dict):
            candidates.append(install.get("project_type"))
    if isinstance(order.metadata_, dict):
        candidates.append(order.metadata_.get("project_type"))
    configured = settings_spec.resolve_value(
        db, SettingDomain.projects, "default_sales_project_type"
    )
    candidates.append(configured)
    allowed = {item.value for item in ProjectType}
    resolved = next(
        (str(value) for value in candidates if str(value or "") in allowed), None
    )
    if resolved is None:
        raise SalesFulfillmentError(
            "project_type_unconfigured",
            "No valid sales implementation project type is configured",
            kind="invalid",
        )
    return resolved


def _customer_address(order: SalesOrder, subscriber: Subscriber) -> str | None:
    if order.quote is not None and isinstance(order.quote.metadata_, dict):
        install = order.quote.metadata_.get("install")
        if isinstance(install, dict):
            value = str(install.get("address") or "").strip()
            if value:
                return value
    addr = service_address_service.address_parts(subscriber)
    parts = [addr.address_line1, addr.address_line2, addr.city]
    return (
        ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        or None
    )


def ensure_implementation_scope(
    db: Session,
    *,
    sales_order_id: UUID,
    actor_id: str,
    commit: bool = True,
) -> FulfillmentScope:
    actor = str(actor_id or "").strip()
    if not actor:
        raise SalesFulfillmentError(
            "actor_required", "Fulfillment actor is required", kind="invalid"
        )
    order = db.scalars(
        select(SalesOrder)
        .where(SalesOrder.id == sales_order_id)
        .options(selectinload(SalesOrder.quote), selectinload(SalesOrder.subscriber))
        .with_for_update()
    ).one_or_none()
    if order is None or not order.is_active:
        raise SalesFulfillmentError(
            "sales_order_not_found", "Sales order not found", kind="not_found"
        )
    if order.status == SalesOrderStatus.cancelled.value:
        raise SalesFulfillmentError(
            "sales_order_canceled", "Canceled order cannot create implementation"
        )
    subscriber = order.subscriber
    if subscriber is None:
        raise SalesFulfillmentError(
            "subscriber_not_found", "Sales order Subscriber not found"
        )
    lead_id = order.quote.lead_id if order.quote is not None else None
    try:
        project = projects.prepare_sales_project(
            db,
            sales_order_id=order.id,
            quote_id=order.quote_id,
            subscriber_id=order.subscriber_id,
            lead_id=lead_id,
            name=f"Installation — {order.order_number or order.id}",
            project_type=_project_type(db, order),
            customer_address=_customer_address(order, subscriber),
            region=service_address_service.address_parts(subscriber).region,
            actor_id=actor,
        )
        installation = installation_projects.ensure_for_project(
            db,
            project_id=project.id,
            subscriber_id=order.subscriber_id,
            actor_id=actor,
        )
        if commit:
            db.commit()
            db.refresh(order)
            db.refresh(project)
            db.refresh(installation)
        return FulfillmentScope(order, project, installation)
    except (
        projects.SalesProjectLifecycleError,
        installation_projects.InstallationScopeError,
    ) as exc:
        if commit:
            db.rollback()
        raise SalesFulfillmentError(
            "fulfillment_rejected", str(exc), kind="invalid"
        ) from exc


def release_verified_implementation(
    db: Session,
    *,
    installation_project_id: UUID,
    verification_event_id: UUID,
    actor_id: str,
    commit: bool = True,
) -> int:
    installation = db.scalars(
        select(InstallationProject)
        .where(InstallationProject.id == installation_project_id)
        .with_for_update()
    ).one_or_none()
    if installation is None:
        raise SalesFulfillmentError(
            "installation_not_found", "Installation project not found", kind="not_found"
        )
    if installation.status != InstallationProjectStatus.verified.value:
        raise SalesFulfillmentError(
            "implementation_not_verified",
            "Only verified implementation can release provisioning",
        )
    project = projects.complete_from_verified_installation(
        db,
        project_id=installation.project_id,
        actor_id=actor_id,
        verification_event_id=verification_event_id,
    )
    from app.services import service_order_lifecycle

    released = 0
    orders = db.scalars(
        select(ServiceOrder)
        .where(ServiceOrder.project_id == project.id)
        .order_by(ServiceOrder.created_at, ServiceOrder.id)
        .with_for_update()
    ).all()
    for order in orders:
        changed = service_order_lifecycle.release_implementation(
            db,
            service_order_id=order.id,
            installation_project_id=installation.id,
            verification_event_id=verification_event_id,
            actor_id=actor_id,
        )
        released += int(changed)
    emit_event(
        db,
        EventType.implementation_released,
        {
            "project_id": str(project.id),
            "installation_project_id": str(installation.id),
            "sales_order_id": str(project.sales_order_id)
            if project.sales_order_id
            else None,
            "verification_event_id": str(verification_event_id),
            "released_service_orders": released,
        },
        actor=actor_id,
        subscriber_id=project.subscriber_id,
    )
    if commit:
        db.commit()
    return released


# --- receipted lifecycle-output consumption --------------------------------
#
# The registered SalesLifecycleProjectionHandler delivers committed producer
# outputs to these owner commands. Each command runs on a transaction-free
# owner-command session and wraps its effect in a unique
# ``(consumer, event_id)`` receipt via ``events.owner_outputs``, so the
# effect and its receipt commit atomically and a redelivery is an exact
# no-op. A raised failure leaves no receipt: the delivery stays durably
# failed and retryable in the outbox.

_CONSUMER = "sales.fulfillment"
_CONSUME_CONCERN = "committed lifecycle output consumption"

_CONSUME_VERIFIED_COMMAND = OwnerCommandDefinition(
    owner=_CONSUMER,
    concern=_CONSUME_CONCERN,
    name="consume_verified_implementation",
)
_CONSUME_RELEASE_COMMAND = OwnerCommandDefinition(
    owner=_CONSUMER,
    concern=_CONSUME_CONCERN,
    name="consume_service_order_release",
)
_CONSUME_ACCEPTANCE_COMMAND = OwnerCommandDefinition(
    owner=_CONSUMER,
    concern=_CONSUME_CONCERN,
    name="consume_cx_acceptance",
)


def consume_verified_implementation(
    db: Session,
    *,
    installation_project_id: UUID,
    verification_event_id: UUID,
    event_id: UUID,
    context: CommandContext,
) -> int | None:
    """Receipt one ``vendor_project.verified`` output into a release."""

    def _effect() -> int:
        return release_verified_implementation(
            db,
            installation_project_id=installation_project_id,
            verification_event_id=verification_event_id,
            actor_id=context.actor,
            commit=False,
        )

    return execute_owner_command(
        db,
        definition=_CONSUME_VERIFIED_COMMAND,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer=_CONSUMER,
            event_id=event_id,
            event_type=EventType.vendor_project_verified.value,
            producer_owner="operations.vendor_project_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )


def consume_service_order_release(
    db: Session,
    *,
    service_order_id: UUID,
    event_id: UUID,
    context: CommandContext,
) -> bool | None:
    """Receipt one ``service_order.released`` output into provisioning.

    Only sales-linked orders auto-enter provisioning; repair and
    reprovisioning orders keep manual progression, and an order an operator
    already progressed is left untouched.
    """

    def _effect() -> bool:
        from app.models.provisioning import ServiceOrder, ServiceOrderStatus
        from app.services import service_order_lifecycle

        order = db.get(ServiceOrder, service_order_id)
        if (
            order is None
            or order.sales_order_id is None
            or order.status
            not in {ServiceOrderStatus.submitted, ServiceOrderStatus.scheduled}
        ):
            return False
        service_order_lifecycle.transition_service_order(
            db,
            service_order_id=order.id,
            target_status=ServiceOrderStatus.provisioning,
            actor_id=context.actor,
            reason="Released implementation enters provisioning",
            event_evidence={"released_event_id": str(event_id)},
            commit=False,
        )
        return True

    return execute_owner_command(
        db,
        definition=_CONSUME_RELEASE_COMMAND,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer=_CONSUMER,
            event_id=event_id,
            event_type=EventType.service_order_released.value,
            producer_owner="operations.service_order_lifecycle",
            context=context,
            operation=_effect,
        )[0],
    )


def consume_cx_acceptance(
    db: Session,
    *,
    sales_order_id: UUID,
    handoff_id: UUID,
    event_id: UUID,
    context: CommandContext,
) -> bool | None:
    """Receipt one ``customer_experience.accepted`` output into fulfilment."""

    def _effect() -> bool:
        from app.services import sales_orders

        return sales_orders.fulfill_from_customer_experience(
            db,
            sales_order_id=sales_order_id,
            handoff_id=handoff_id,
            actor_id=context.actor,
        )

    return execute_owner_command(
        db,
        definition=_CONSUME_ACCEPTANCE_COMMAND,
        context=context,
        operation=lambda: consume_owner_output(
            db,
            consumer=_CONSUMER,
            event_id=event_id,
            event_type=EventType.customer_experience_accepted.value,
            producer_owner="customer.experience_handoff",
            context=context,
            operation=_effect,
        )[0],
    )
