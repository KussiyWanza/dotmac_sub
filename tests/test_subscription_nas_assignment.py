"""Behavior tests for the reviewed subscription service-access move owner."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.owner_commands import CommandContext
from app.services.subscription_nas_assignment import (
    MoveSubscriptionServiceAccessCommand,
    ServiceAccessMoveDecision,
    move_subscription_service_access,
    preview_subscription_service_access_move,
)
from app.services.web_catalog_subscription_workflows import (
    service_access_move_available_ipv4,
)


def _service_access_evidence(db, offer):
    subscriber = Subscriber(
        first_name="Service",
        last_name="Move",
        email=f"service-move-{uuid4().hex[:8]}@example.com",
    )
    source_nas = NasDevice(
        name=f"Source NAS {uuid4().hex[:8]}",
        status=NasDeviceStatus.active,
        is_active=True,
    )
    target_nas = NasDevice(
        name=f"Target NAS {uuid4().hex[:8]}",
        status=NasDeviceStatus.active,
        is_active=True,
    )
    db.add_all([subscriber, source_nas, target_nas])
    db.flush()
    source_pool = IpPool(
        name=f"Source pool {uuid4().hex[:8]}",
        ip_version=IPVersion.ipv4,
        cidr="10.81.1.0/24",
        nas_device_id=source_nas.id,
        is_active=True,
    )
    target_pool = IpPool(
        name=f"Target pool {uuid4().hex[:8]}",
        ip_version=IPVersion.ipv4,
        cidr="10.81.2.0/24",
        nas_device_id=target_nas.id,
        is_active=True,
    )
    db.add_all([source_pool, target_pool])
    db.flush()
    source_address = IPv4Address(
        address="10.81.1.10",
        pool_id=source_pool.id,
        is_reserved=False,
        allocation_type="static",
    )
    target_address = IPv4Address(
        address="10.81.2.20",
        pool_id=target_pool.id,
        is_reserved=False,
        allocation_type="static",
    )
    db.add_all([source_address, target_address])
    db.flush()
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        provisioning_nas_device_id=source_nas.id,
        status=SubscriptionStatus.active,
        login=f"service-move-{uuid4().hex[:10]}",
        ipv4_address=source_address.address,
    )
    db.add(subscription)
    db.flush()
    source_assignment = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=source_address.id,
        is_primary=True,
        is_active=True,
    )
    db.add(source_assignment)
    db.flush()
    return {
        "subscription": subscription,
        "source_nas": source_nas,
        "target_nas": target_nas,
        "source_assignment": source_assignment,
        "target_pool": target_pool,
        "target_address": target_address,
    }


def _command(evidence, fingerprint: str, *, key: str):
    return MoveSubscriptionServiceAccessCommand(
        context=CommandContext.system(
            actor="test-network-operator",
            scope="subscription_service_access_move",
            reason="Reviewed router and primary-IP move",
            idempotency_key=key,
        ),
        subscription_id=evidence["subscription"].id,
        target_nas_device_id=evidence["target_nas"].id,
        target_pool_id=evidence["target_pool"].id,
        target_ipv4=evidence["target_address"].address,
        preview_fingerprint=fingerprint,
    )


def _radius_state(evidence):
    subscription = evidence["subscription"]
    return ({subscription.login: subscription.ipv4_address}, {subscription.login}, 0)


def test_preview_rejects_pool_not_linked_to_target_router(
    db_session,
    catalog_offer,
) -> None:
    evidence = _service_access_evidence(db_session, catalog_offer)

    with patch(
        "app.services.ip_consistency_audit._external_ip_state",
        return_value=_radius_state(evidence),
    ):
        preview = preview_subscription_service_access_move(
            db_session,
            subscription_id=evidence["subscription"].id,
            target_nas_device_id=evidence["target_nas"].id,
            target_pool_id=evidence["target_pool"].id,
            target_ipv4=evidence["target_address"].address,
        )
    assert preview.decision is ServiceAccessMoveDecision.ready

    evidence["target_pool"].nas_device_id = evidence["source_nas"].id
    db_session.flush()
    blocked = preview_subscription_service_access_move(
        db_session,
        subscription_id=evidence["subscription"].id,
        target_nas_device_id=evidence["target_nas"].id,
        target_pool_id=evidence["target_pool"].id,
        target_ipv4=evidence["target_address"].address,
    )
    assert blocked.decision is ServiceAccessMoveDecision.target_pool_not_linked


def test_preview_accepts_shared_pool_linked_by_nas_radius_configuration(
    db_session,
    catalog_offer,
) -> None:
    evidence = _service_access_evidence(db_session, catalog_offer)
    target_pool = evidence["target_pool"]
    target_nas = evidence["target_nas"]
    target_pool.nas_device_id = None
    target_nas.tags = [f"radius_pool:{target_pool.id}"]
    db_session.flush()

    with patch(
        "app.services.ip_consistency_audit._external_ip_state",
        return_value=_radius_state(evidence),
    ):
        preview = preview_subscription_service_access_move(
            db_session,
            subscription_id=evidence["subscription"].id,
            target_nas_device_id=target_nas.id,
            target_pool_id=target_pool.id,
            target_ipv4=evidence["target_address"].address,
        )

    assert preview.decision is ServiceAccessMoveDecision.ready
    assert service_access_move_available_ipv4(
        db_session,
        target_nas_device_id=target_nas.id,
        target_pool_id=target_pool.id,
    ) == (evidence["target_address"].address,)


def test_move_commits_router_and_ipv4_without_commercial_changes(
    db_session,
    catalog_offer,
) -> None:
    evidence = _service_access_evidence(db_session, catalog_offer)
    subscription = evidence["subscription"]
    original_offer_id = subscription.offer_id
    original_next_billing_at = subscription.next_billing_at
    db_session.commit()

    with patch(
        "app.services.ip_consistency_audit._external_ip_state",
        return_value=_radius_state(evidence),
    ):
        preview = preview_subscription_service_access_move(
            db_session,
            subscription_id=subscription.id,
            target_nas_device_id=evidence["target_nas"].id,
            target_pool_id=evidence["target_pool"].id,
            target_ipv4=evidence["target_address"].address,
        )
        assert preview.decision is ServiceAccessMoveDecision.ready
        command = _command(evidence, preview.fingerprint, key="service-move-success")
        db_session.commit()
        outcome = move_subscription_service_access(
            db_session,
            command,
        )

    db_session.refresh(subscription)
    db_session.refresh(evidence["source_assignment"])
    target_assignment = db_session.scalar(
        select(IPAssignment).where(
            IPAssignment.subscription_id == subscription.id,
            IPAssignment.ipv4_address_id == evidence["target_address"].id,
            IPAssignment.is_active.is_(True),
        )
    )
    audit = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "service_intent.subscription_service_access_moved",
            AuditEvent.entity_id == "service-move-success",
        )
    )

    assert outcome.target_ipv4 == evidence["target_address"].address
    assert subscription.provisioning_nas_device_id == evidence["target_nas"].id
    assert subscription.ipv4_address == evidence["target_address"].address
    assert evidence["source_assignment"].is_active is False
    assert target_assignment is not None
    assert target_assignment.is_primary is True
    assert subscription.offer_id == original_offer_id
    assert subscription.next_billing_at == original_next_billing_at
    assert audit is not None
    assert audit.metadata_["billing_changed"] is False


def test_move_rolls_back_router_and_ipam_when_projection_fails(
    db_session,
    catalog_offer,
) -> None:
    evidence = _service_access_evidence(db_session, catalog_offer)
    subscription_id = evidence["subscription"].id
    source_nas_id = evidence["source_nas"].id
    source_assignment_id = evidence["source_assignment"].id
    target_address_id = evidence["target_address"].id
    db_session.commit()

    with patch(
        "app.services.ip_consistency_audit._external_ip_state",
        return_value=_radius_state(evidence),
    ):
        preview = preview_subscription_service_access_move(
            db_session,
            subscription_id=subscription_id,
            target_nas_device_id=evidence["target_nas"].id,
            target_pool_id=evidence["target_pool"].id,
            target_ipv4=evidence["target_address"].address,
        )
        command = _command(evidence, preview.fingerprint, key="service-move-rollback")
        db_session.commit()
        with (
            patch(
                "app.services.subscription_nas_assignment.apply_service_ipv4_projection_participant",
                side_effect=RuntimeError("projection failed"),
            ),
            pytest.raises(RuntimeError, match="projection failed"),
        ):
            move_subscription_service_access(
                db_session,
                command,
            )

    subscription = db_session.get(Subscription, subscription_id)
    source_assignment = db_session.get(IPAssignment, source_assignment_id)
    target_assignment = db_session.scalar(
        select(IPAssignment).where(
            IPAssignment.subscription_id == subscription_id,
            IPAssignment.ipv4_address_id == target_address_id,
            IPAssignment.is_active.is_(True),
        )
    )
    assert subscription is not None
    assert source_assignment is not None
    assert subscription.provisioning_nas_device_id == source_nas_id
    assert subscription.ipv4_address == "10.81.1.10"
    assert source_assignment.is_active is True
    assert target_assignment is None
