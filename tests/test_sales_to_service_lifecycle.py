"""End-to-end owner chain from funded sale through CX acceptance."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.customer_experience import (
    CustomerExperienceHandoff,
    CustomerExperienceHandoffEvent,
    CustomerExperienceHandoffEventImmutableError,
    CustomerExperienceHandoffStatus,
)
from app.models.project import Project, ProjectStatus
from app.models.provisioning import ServiceOrder, ServiceOrderStatus, ServiceOrderType
from app.models.sales import (
    SalesOrder,
    SalesOrderLine,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    Vendor,
)
from app.models.work_order import WorkOrder
from app.services import (
    account_lifecycle,
    customer_experience_handoffs,
    service_order_lifecycle,
)
from app.services.vendor_portal_operations import vendor_portal_operations


def _chain(db, subscriber, offer):
    order = SalesOrder(
        subscriber_id=subscriber.id,
        order_number=f"SO-E2E-{uuid4().hex[:10]}",
        status=SalesOrderStatus.paid.value,
        payment_status=SalesOrderPaymentStatus.paid.value,
        total=100,
        amount_paid=100,
        balance_due=0,
    )
    db.add(order)
    db.flush()
    line = SalesOrderLine(
        sales_order_id=order.id,
        description="Fiber service",
        quantity=1,
        unit_price=100,
        amount=100,
        metadata_={"sub_offer_id": str(offer.id)},
    )
    project = Project(
        name="Sales implementation",
        subscriber_id=subscriber.id,
        sales_order_id=order.id,
        status=ProjectStatus.active.value,
    )
    vendor = Vendor(name="Lifecycle Vendor", code=f"LV-{uuid4().hex[:8]}")
    reviewer = SystemUser(
        first_name="CX",
        last_name="Reviewer",
        email=f"cx-reviewer-{uuid4().hex}@example.test",
    )
    db.add_all([line, project, vendor, reviewer])
    db.flush()
    installation = InstallationProject(
        project_id=project.id,
        subscriber_id=subscriber.id,
        assigned_vendor_id=vendor.id,
        status=InstallationProjectStatus.completed.value,
    )
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.pending,
    )
    db.add_all([installation, subscription])
    db.flush()
    service_order = ServiceOrder(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        sales_order_id=order.id,
        sales_order_line_id=line.id,
        project_id=project.id,
        installation_project_id=installation.id,
        idempotency_key=f"sales-order-line:{line.id}:new_install",
        status=ServiceOrderStatus.draft,
        order_type=ServiceOrderType.new_install,
    )
    work_order = WorkOrder(
        subscriber_id=subscriber.id,
        project_id=project.id,
        title="Install customer service",
        requires_as_built_evidence=False,
    )
    db.add_all([service_order, work_order])
    db.commit()
    return order, project, installation, subscription, service_order, reviewer


def test_sales_service_activation_requires_verified_implementation(
    db_session, subscriber, catalog_offer
):
    _order, _project, _installation, subscription, service_order, _reviewer = _chain(
        db_session, subscriber, catalog_offer
    )

    with pytest.raises(service_order_lifecycle.ServiceOrderLifecycleError) as exc:
        service_order_lifecycle.transition_service_order(
            db_session,
            service_order_id=service_order.id,
            target_status=ServiceOrderStatus.submitted,
            actor_id="pytest",
        )
    assert exc.value.code == "implementation_not_ready"

    with pytest.raises(ValueError, match="successful service-order provisioning"):
        account_lifecycle.activate_subscription(db_session, str(subscription.id))


def test_verified_implementation_to_provisioning_to_cx_acceptance(
    db_session, subscriber, catalog_offer
):
    order, project, installation, subscription, service_order, reviewer = _chain(
        db_session, subscriber, catalog_offer
    )

    verification = vendor_portal_operations.transition_staff_project(
        db_session,
        str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
        reason="Implementation evidence accepted",
    )
    # The vendor owner commits only its fact. The registered lifecycle
    # projection consumes that durable event and asks downstream owners to
    # release implementation in a separate idempotent transaction; the
    # committed release output then auto-advances the sales-linked order
    # into provisioning through its lifecycle owner.
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(service_order)
    assert project.status == ProjectStatus.completed.value
    assert installation.status == InstallationProjectStatus.verified.value
    assert service_order.status == ServiceOrderStatus.provisioning
    assert (
        str(service_order.implementation_verification_event_id)
        == verification["domain_event_id"]
    )

    service_order_lifecycle.record_provisioning_result(
        db_session,
        service_order_id=service_order.id,
        succeeded=True,
        readiness_decision_id=uuid4(),
        actor_id="pytest",
    )
    db_session.commit()

    db_session.refresh(service_order)
    db_session.refresh(subscription)
    handoff = db_session.query(CustomerExperienceHandoff).one()
    assert service_order.status == ServiceOrderStatus.active
    assert subscription.status == SubscriptionStatus.active
    assert handoff.status == CustomerExperienceHandoffStatus.ready.value
    assert handoff.readiness_evidence["eligible"] is True

    customer_experience_handoffs.accept_handoff(
        db_session,
        handoff_id=handoff.id,
        actor_type="staff_user",
        actor_id=str(reviewer.id),
        reason="Customer welcome and support ownership confirmed",
    )
    db_session.refresh(order)
    assert handoff.status == CustomerExperienceHandoffStatus.accepted.value
    assert handoff.accepted_at is not None
    assert order.status == SalesOrderStatus.fulfilled.value
    assert db_session.query(CustomerExperienceHandoffEvent).count() == 2


def test_cx_lifecycle_evidence_is_append_only(db_session, subscriber, catalog_offer):
    _order, _project, installation, _subscription, service_order, reviewer = _chain(
        db_session, subscriber, catalog_offer
    )
    vendor_portal_operations.transition_staff_project(
        db_session,
        str(installation.id),
        action="verify",
        actor_id=str(reviewer.id),
    )
    db_session.commit()
    # The committed release output already advanced the order to provisioning.
    db_session.refresh(service_order)
    assert service_order.status == ServiceOrderStatus.provisioning
    service_order_lifecycle.record_provisioning_result(
        db_session,
        service_order_id=service_order.id,
        succeeded=True,
        readiness_decision_id=uuid4(),
        actor_id="pytest",
    )
    db_session.commit()
    evidence = db_session.query(CustomerExperienceHandoffEvent).one()
    evidence.reason = "rewritten"

    with pytest.raises(CustomerExperienceHandoffEventImmutableError):
        db_session.flush()
    db_session.rollback()
