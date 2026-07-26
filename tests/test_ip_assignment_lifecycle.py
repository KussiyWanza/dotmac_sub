"""Behavior tests for reviewed exact-service IPv4 assignment lifecycle repair."""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    IPAssignment,
    IpPool,
    IPv4Address,
    IPVersion,
    SubscriberAdditionalRoute,
)
from app.models.network_monitoring import NetworkDevice
from app.models.subscriber import Subscriber
from app.services.ip_assignment_lifecycle import (
    IPv4AssignmentLifecycleError,
    IPv4AssignmentRepairDecision,
    RepairServiceIPv4AssignmentCommand,
    preview_service_ipv4_assignment_repair,
    repair_service_ipv4_assignment,
)
from app.services.owner_commands import CommandContext


def _subscriber(db, suffix: str) -> Subscriber:
    subscriber = Subscriber(
        first_name="IPAM",
        last_name="Lifecycle",
        email=f"ipam-lifecycle-{suffix}-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _subscription(
    db,
    subscriber: Subscriber,
    offer,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
    served_ip: str | None = None,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"ipam-lifecycle-{uuid4().hex[:10]}",
        ipv4_address=served_ip,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _pool(db, suffix: str, *, management: bool = False) -> IpPool:
    pool = IpPool(
        name=(
            f"OLT Management Pool {suffix}"
            if management
            else f"Subscriber Pool {suffix}"
        ),
        ip_version=IPVersion.ipv4,
        cidr="10.30.0.0/24",
        is_active=True,
        notes="management" if management else None,
    )
    db.add(pool)
    db.flush()
    return pool


def _address(
    db,
    pool: IpPool,
    value: str,
    *,
    reserved: bool = False,
    allocation_type: str | None = "static",
) -> IPv4Address:
    address = IPv4Address(
        address=value,
        pool_id=pool.id,
        is_reserved=reserved,
        allocation_type=allocation_type,
    )
    db.add(address)
    db.flush()
    return address


def _assignment(
    db,
    subscriber: Subscriber,
    address: IPv4Address,
    *,
    subscription: Subscription | None = None,
) -> IPAssignment:
    assignment = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id if subscription is not None else None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
    )
    db.add(assignment)
    db.flush()
    return assignment


def _command(
    *,
    subscription_id: UUID,
    desired_address_id: UUID | None,
    deactivate_assignment_ids: tuple[UUID, ...],
    fingerprint: str,
    key: str = "ipam-lifecycle-test-1",
) -> RepairServiceIPv4AssignmentCommand:
    return RepairServiceIPv4AssignmentCommand(
        context=CommandContext.system(
            actor="test-operator",
            scope="ip_assignment_lifecycle_repair",
            reason="Reviewed exact-service IPAM lifecycle evidence",
            idempotency_key=key,
        ),
        subscription_id=subscription_id,
        desired_address_id=desired_address_id,
        deactivate_assignment_ids=deactivate_assignment_ids,
        preview_fingerprint=fingerprint,
    )


def test_repair_links_desired_legacy_assignment_and_deactivates_stale_exact(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "link")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        served_ip="10.30.0.10",
    )
    pool = _pool(db_session, "link")
    desired = _address(db_session, pool, "10.30.0.10")
    stale = _address(db_session, pool, "10.30.0.11")
    desired_assignment = _assignment(db_session, subscriber, desired)
    stale_assignment = _assignment(
        db_session,
        subscriber,
        stale,
        subscription=subscription,
    )
    subscription_id = subscription.id
    desired_id = desired.id
    stale_assignment_id = stale_assignment.id
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
        deactivate_assignment_ids=(stale_assignment_id,),
    )
    assert preview.decision is IPv4AssignmentRepairDecision.ready_link
    db_session.commit()

    outcome = repair_service_ipv4_assignment(
        db_session,
        _command(
            subscription_id=subscription_id,
            desired_address_id=desired_id,
            deactivate_assignment_ids=(stale_assignment_id,),
            fingerprint=preview.fingerprint,
        ),
    )

    assert outcome.linked_count == 1
    assert outcome.created_count == 0
    assert outcome.deactivated_count == 1
    db_session.refresh(desired_assignment)
    db_session.refresh(stale_assignment)
    db_session.refresh(subscription)
    assert desired_assignment.subscription_id == subscription.id
    assert desired_assignment.is_active is True
    assert stale_assignment.is_active is False
    assert subscription.ipv4_address == "10.30.0.10"


def test_repair_creates_new_history_row_without_writing_served_projection(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "create")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        served_ip="10.30.0.20",
    )
    pool = _pool(db_session, "create")
    desired = _address(db_session, pool, "10.30.0.21")
    subscription_id = subscription.id
    desired_id = desired.id
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    assert preview.decision is IPv4AssignmentRepairDecision.ready_create
    db_session.commit()
    outcome = repair_service_ipv4_assignment(
        db_session,
        _command(
            subscription_id=subscription_id,
            desired_address_id=desired_id,
            deactivate_assignment_ids=(),
            fingerprint=preview.fingerprint,
        ),
    )

    assignment = db_session.get(IPAssignment, outcome.desired_assignment_id)
    assert assignment is not None
    assert assignment.subscription_id == subscription_id
    assert assignment.subscriber_id == subscriber.id
    assert assignment.ipv4_address_id == desired.id
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "10.30.0.20"


def test_preview_refuses_cross_customer_target(db_session, catalog_offer) -> None:
    owner = _subscriber(db_session, "owner")
    owner_subscription = _subscription(db_session, owner, catalog_offer)
    target = _subscriber(db_session, "target")
    target_subscription = _subscription(db_session, target, catalog_offer)
    pool = _pool(db_session, "conflict")
    address = _address(db_session, pool, "10.30.0.30")
    _assignment(db_session, owner, address, subscription=owner_subscription)
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=target_subscription.id,
        desired_address_id=address.id,
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.target_owned_by_other_service
    )
    assert preview.applicable is False


@pytest.mark.parametrize(
    ("reserved", "allocation_type"),
    [(True, "static"), (False, "management")],
)
def test_preview_refuses_reserved_or_management_address(
    db_session,
    catalog_offer,
    reserved: bool,
    allocation_type: str,
) -> None:
    subscriber = _subscriber(db_session, uuid4().hex[:6])
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, uuid4().hex[:6])
    address = _address(
        db_session,
        pool,
        f"10.30.0.{40 + int(reserved)}",
        reserved=reserved,
        allocation_type=allocation_type,
    )
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=address.id,
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.target_address_not_serviceable
    )


def test_preview_refuses_management_pool(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "management-pool")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "management-pool", management=True)
    address = _address(db_session, pool, "10.30.0.45")
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=address.id,
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.target_address_not_serviceable
    )


def test_preview_refuses_abbreviated_management_pool(db_session, catalog_offer) -> None:
    """`OLT-MGMT` is a management pool even though it never says "management".

    The original guard matched the single substring "management", so the common
    hand-created abbreviation passed straight through and an OLT management
    address was assignable to a customer.
    """
    subscriber = _subscriber(db_session, "mgmt-abbrev")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = IpPool(
        name="OLT-MGMT Lagos",
        ip_version=IPVersion.ipv4,
        cidr="10.30.0.0/24",
        is_active=True,
        notes=None,
    )
    db_session.add(pool)
    db_session.flush()
    address = _address(db_session, pool, "10.30.0.46")
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=address.id,
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.target_address_not_serviceable
    )


def test_infrastructure_pool_detection_prefers_the_typed_signal() -> None:
    """`olt_device_id` decides, whatever the pool happens to be called.

    Text markers are a backstop for hand-created pools. A pool carrying the OLT
    foreign key is infrastructure even when its name reads like a customer
    range -- verified against production, where every pool with that FK was a
    management pool and no customer pool had one.
    """
    from app.services.ip_assignment_lifecycle import _pool_is_infrastructure

    def _unsaved(name: str, *, olt_device_id: UUID | None = None) -> IpPool:
        return IpPool(
            name=name,
            ip_version=IPVersion.ipv4,
            cidr="10.30.0.0/24",
            is_active=True,
            notes=None,
            olt_device_id=olt_device_id,
        )

    # Typed signal wins over an innocuous name.
    assert _pool_is_infrastructure(_unsaved("Garki IP Range", olt_device_id=uuid4()))
    # Text backstop still catches spelled-out and abbreviated forms.
    assert _pool_is_infrastructure(_unsaved("BOI Huawei OLT Management Pool"))
    assert _pool_is_infrastructure(_unsaved("OLT-MGMT Lagos"))
    assert _pool_is_infrastructure(_unsaved("Core MGT Range"))
    # A genuine customer pool stays serviceable.
    assert not _pool_is_infrastructure(_unsaved("Garki IP Range"))
    assert not _pool_is_infrastructure(_unsaved("Airport Fallback IP"))


def test_preview_refuses_network_device_management_ip(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "device-host")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "device-host")
    address = _address(db_session, pool, "10.30.0.46")
    db_session.add(
        NetworkDevice(
            name=f"IPAM device host {uuid4().hex[:8]}",
            mgmt_ip="10.30.0.46",
            is_active=True,
        )
    )
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=address.id,
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.target_address_is_device_host
    )


def test_preview_refuses_address_inside_active_routed_block(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "route")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "route")
    address = _address(db_session, pool, "10.30.0.50")
    db_session.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            cidr="10.30.0.48/29",
            prefix_length=29,
            metric=1,
            is_active=True,
            source="manual",
        )
    )
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=address.id,
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.target_address_in_routed_block
    )


def test_preview_requires_complete_exact_deactivation_set(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "complete")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "complete")
    desired = _address(db_session, pool, "10.30.0.60")
    stale = _address(db_session, pool, "10.30.0.61")
    _assignment(db_session, subscriber, stale, subscription=subscription)
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=desired.id,
    )

    assert preview.decision is IPv4AssignmentRepairDecision.incomplete_deactivation_set


def test_preview_refuses_deactivating_the_desired_assignment(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "desired-deactivate")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "desired-deactivate")
    desired = _address(db_session, pool, "10.30.0.65")
    assignment = _assignment(
        db_session,
        subscriber,
        desired,
        subscription=subscription,
    )
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription.id,
        desired_address_id=desired.id,
        deactivate_assignment_ids=(assignment.id,),
    )

    assert (
        preview.decision
        is IPv4AssignmentRepairDecision.desired_assignment_selected_for_deactivation
    )


def test_preview_refuses_deactivating_another_service_assignment(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "cross-service")
    target_subscription = _subscription(db_session, subscriber, catalog_offer)
    other_subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "cross-service")
    desired = _address(db_session, pool, "10.30.0.66")
    other_address = _address(db_session, pool, "10.30.0.67")
    other_assignment = _assignment(
        db_session,
        subscriber,
        other_address,
        subscription=other_subscription,
    )
    db_session.commit()

    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=target_subscription.id,
        desired_address_id=desired.id,
        deactivate_assignment_ids=(other_assignment.id,),
    )

    assert (
        preview.decision is IPv4AssignmentRepairDecision.deactivation_not_exact_service
    )


def test_apply_rejects_stale_preview(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "stale")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "stale")
    desired = _address(db_session, pool, "10.30.0.70")
    subscription_id = subscription.id
    desired_id = desired.id
    db_session.commit()
    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    db_session.commit()
    desired = db_session.get(IPv4Address, desired_id)
    assert desired is not None
    desired.is_reserved = True
    db_session.commit()

    with pytest.raises(IPv4AssignmentLifecycleError) as exc_info:
        repair_service_ipv4_assignment(
            db_session,
            _command(
                subscription_id=subscription_id,
                desired_address_id=desired_id,
                deactivate_assignment_ids=(),
                fingerprint=preview.fingerprint,
            ),
        )
    assert exc_info.value.code.endswith(".stale_preview")


def test_event_failure_rolls_back_complete_lifecycle_change(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "rollback")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "rollback")
    desired = _address(db_session, pool, "10.30.0.75")
    stale = _address(db_session, pool, "10.30.0.76")
    desired_assignment = _assignment(db_session, subscriber, desired)
    stale_assignment = _assignment(
        db_session,
        subscriber,
        stale,
        subscription=subscription,
    )
    subscription_id = subscription.id
    desired_id = desired.id
    desired_assignment_id = desired_assignment.id
    stale_assignment_id = stale_assignment.id
    db_session.commit()
    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
        deactivate_assignment_ids=(stale_assignment_id,),
    )
    db_session.commit()

    with (
        patch(
            "app.services.ip_assignment_lifecycle.emit_event",
            side_effect=RuntimeError("outbox unavailable"),
        ),
        pytest.raises(RuntimeError, match="outbox unavailable"),
    ):
        repair_service_ipv4_assignment(
            db_session,
            _command(
                subscription_id=subscription_id,
                desired_address_id=desired_id,
                deactivate_assignment_ids=(stale_assignment_id,),
                fingerprint=preview.fingerprint,
                key="ipam-lifecycle-rollback",
            ),
        )

    desired_assignment = db_session.get(IPAssignment, desired_assignment_id)
    stale_assignment = db_session.get(IPAssignment, stale_assignment_id)
    assert desired_assignment is not None
    assert stale_assignment is not None
    assert desired_assignment.subscription_id is None
    assert stale_assignment.is_active is True
    assert (
        db_session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.entity_id == "ipam-lifecycle-rollback"
            )
        )
        == 0
    )


def test_apply_is_durably_idempotent(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "idempotent")
    subscription = _subscription(db_session, subscriber, catalog_offer)
    pool = _pool(db_session, "idempotent")
    desired = _address(db_session, pool, "10.30.0.80")
    subscription_id = subscription.id
    desired_id = desired.id
    db_session.commit()
    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=desired_id,
    )
    db_session.commit()
    command = _command(
        subscription_id=subscription_id,
        desired_address_id=desired_id,
        deactivate_assignment_ids=(),
        fingerprint=preview.fingerprint,
        key="ipam-lifecycle-idempotent",
    )

    first = repair_service_ipv4_assignment(db_session, command)
    second = repair_service_ipv4_assignment(db_session, command)

    assert first.created_count == 1
    assert first.replayed is False
    assert second.desired_assignment_id == first.desired_assignment_id
    assert second.replayed is True
    assert (
        db_session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "network.ip_assignment.lifecycle_repaired",
                AuditEvent.entity_id == "ipam-lifecycle-idempotent",
            )
        )
        == 1
    )


def test_release_requires_terminal_subscription_and_exact_assignments(
    db_session,
    catalog_offer,
) -> None:
    subscriber = _subscriber(db_session, "release")
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.canceled,
        served_ip="10.30.0.90",
    )
    pool = _pool(db_session, "release")
    address = _address(db_session, pool, "10.30.0.90")
    assignment = _assignment(
        db_session,
        subscriber,
        address,
        subscription=subscription,
    )
    subscription_id = subscription.id
    assignment_id = assignment.id
    db_session.commit()
    preview = preview_service_ipv4_assignment_repair(
        db_session,
        subscription_id=subscription_id,
        desired_address_id=None,
        deactivate_assignment_ids=(assignment_id,),
    )
    assert preview.decision is IPv4AssignmentRepairDecision.ready_release
    db_session.commit()

    outcome = repair_service_ipv4_assignment(
        db_session,
        _command(
            subscription_id=subscription_id,
            desired_address_id=None,
            deactivate_assignment_ids=(assignment_id,),
            fingerprint=preview.fingerprint,
        ),
    )

    assert outcome.desired_assignment_id is None
    assert outcome.deactivated_count == 1
    db_session.refresh(assignment)
    db_session.refresh(subscription)
    assert assignment.is_active is False
    assert subscription.ipv4_address == "10.30.0.90"
