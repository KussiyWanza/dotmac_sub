"""Behavior tests for exact-service IPAM ownership reconciliation."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.ip_assignment_repair import (
    IPAssignmentOwnershipDecision,
    IPAssignmentOwnershipError,
    ReconcileIPAssignmentOwnershipCommand,
    preview_ip_assignment_service_ownership,
    reconcile_ip_assignment_service_ownership,
)
from app.services.owner_commands import CommandContext


def _subscriber(db, suffix: str) -> Subscriber:
    subscriber = Subscriber(
        first_name="IPAM",
        last_name="Owner",
        email=f"ipam-owner-{suffix}-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _subscription(
    db,
    subscriber: Subscriber,
    offer,
    address: str | None,
    *,
    status: SubscriptionStatus = SubscriptionStatus.active,
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"ipam-{uuid4().hex[:12]}",
        ipv4_address=address,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _assignment(
    db,
    subscriber: Subscriber | None,
    address: str,
    *,
    subscription: Subscription | None = None,
) -> IPAssignment:
    address_row = IPv4Address(address=address, allocation_type="static")
    db.add(address_row)
    db.flush()
    assignment = IPAssignment(
        subscriber_id=subscriber.id if subscriber is not None else None,
        subscription_id=subscription.id if subscription is not None else None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address_row.id,
        is_active=True,
    )
    db.add(assignment)
    db.flush()
    return assignment


def _item(db, assignment: IPAssignment):
    preview = preview_ip_assignment_service_ownership(
        db,
        assignment_ids=(assignment.id,),
    )
    assert len(preview.items) == 1
    return preview.items[0]


def _command(
    assignment_ids: tuple[UUID, ...],
    fingerprint: str,
    *,
    key: str = "ipam-ownership-test-1",
) -> ReconcileIPAssignmentOwnershipCommand:
    return ReconcileIPAssignmentOwnershipCommand(
        context=CommandContext.system(
            actor="test-operator",
            scope="ipam_service_ownership_reconciliation",
            reason="Reviewed exact service ownership evidence",
            idempotency_key=key,
        ),
        preview_fingerprint=fingerprint,
        assignment_ids=assignment_ids,
    )


def test_preview_classifies_safe_missing_service_link(
    db_session, catalog_offer
) -> None:
    subscriber = _subscriber(db_session, "safe")
    subscription = _subscription(db_session, subscriber, catalog_offer, "10.20.0.10")
    assignment = _assignment(db_session, subscriber, "10.20.0.10")
    db_session.commit()

    item = _item(db_session, assignment)

    assert (
        item.decision is IPAssignmentOwnershipDecision.repairable_missing_service_link
    )
    assert item.proposed_subscription_id == subscription.id
    db_session.refresh(assignment)
    assert assignment.subscription_id is None


def test_preview_refuses_multiple_active_services(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "services")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.11")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.12")
    assignment = _assignment(db_session, subscriber, "10.20.0.11")
    db_session.commit()

    assert (
        _item(db_session, assignment).decision
        is IPAssignmentOwnershipDecision.ambiguous_active_services
    )


def test_preview_refuses_multiple_active_assignments(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "assignments")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.13")
    assignment = _assignment(db_session, subscriber, "10.20.0.13")
    _assignment(db_session, subscriber, "10.20.0.14")
    db_session.commit()

    assert (
        _item(db_session, assignment).decision
        is IPAssignmentOwnershipDecision.ambiguous_active_assignments
    )


def test_preview_refuses_served_address_disagreement(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "mismatch")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.15")
    assignment = _assignment(db_session, subscriber, "10.20.0.16")
    db_session.commit()

    assert (
        _item(db_session, assignment).decision
        is IPAssignmentOwnershipDecision.served_address_mismatch
    )


def test_preview_classifies_exact_existing_link(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "exact")
    subscription = _subscription(db_session, subscriber, catalog_offer, "10.20.0.17")
    assignment = _assignment(
        db_session,
        subscriber,
        "10.20.0.17",
        subscription=subscription,
    )
    db_session.commit()

    assert _item(db_session, assignment).decision is IPAssignmentOwnershipDecision.exact


def test_apply_links_only_subscription_and_records_evidence(
    db_session, catalog_offer
) -> None:
    subscriber = _subscriber(db_session, "apply")
    subscription = _subscription(db_session, subscriber, catalog_offer, "10.20.0.18")
    assignment = _assignment(db_session, subscriber, "10.20.0.18")
    db_session.commit()
    address_id = assignment.ipv4_address_id

    preview = preview_ip_assignment_service_ownership(
        db_session,
        assignment_ids=(assignment.id,),
    )
    assignment_id = assignment.id
    db_session.commit()
    outcome = reconcile_ip_assignment_service_ownership(
        db_session,
        _command((assignment_id,), preview.fingerprint),
    )

    assert outcome.linked_count == 1
    assert outcome.replayed is False
    db_session.refresh(assignment)
    db_session.refresh(subscription)
    assert assignment.subscription_id == subscription.id
    assert assignment.subscriber_id == subscriber.id
    assert assignment.ipv4_address_id == address_id
    assert assignment.is_active is True
    assert subscription.ipv4_address == "10.20.0.18"
    actions = set(
        db_session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_id.in_((str(assignment.id), "ipam-ownership-test-1"))
            )
        ).all()
    )
    assert actions == {
        "network.ip_assignment.service_ownership_linked",
        "network.ip_assignment.service_ownership_reconciled",
    }


def test_apply_rejects_stale_preview(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "stale")
    subscription = _subscription(db_session, subscriber, catalog_offer, "10.20.0.19")
    assignment = _assignment(db_session, subscriber, "10.20.0.19")
    db_session.commit()

    preview = preview_ip_assignment_service_ownership(
        db_session,
        assignment_ids=(assignment.id,),
    )
    assignment_id = assignment.id
    subscription_id = subscription.id
    db_session.commit()
    subscription = db_session.get(Subscription, subscription_id)
    assert subscription is not None
    subscription.ipv4_address = "10.20.0.20"
    db_session.commit()

    with pytest.raises(IPAssignmentOwnershipError) as exc_info:
        reconcile_ip_assignment_service_ownership(
            db_session,
            _command((assignment_id,), preview.fingerprint),
        )
    assert exc_info.value.code.endswith(".stale_preview")
    db_session.rollback()
    db_session.refresh(assignment)
    assert assignment.subscription_id is None


def test_apply_is_durably_idempotent(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "idempotent")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.21")
    assignment = _assignment(db_session, subscriber, "10.20.0.21")
    db_session.commit()

    preview = preview_ip_assignment_service_ownership(
        db_session,
        assignment_ids=(assignment.id,),
    )
    assignment_id = assignment.id
    db_session.commit()
    command = _command(
        (assignment_id,),
        preview.fingerprint,
        key="ipam-ownership-idempotent",
    )
    first = reconcile_ip_assignment_service_ownership(db_session, command)
    second = reconcile_ip_assignment_service_ownership(db_session, command)

    assert first.linked_count == 1
    assert first.replayed is False
    assert second.linked_count == 1
    assert second.replayed is True
    batch_count = db_session.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "network.ip_assignment.service_ownership_reconciled",
            AuditEvent.entity_id == "ipam-ownership-idempotent",
        )
    )
    assert batch_count == 1


def test_apply_refuses_ambiguous_cohort(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session, "unsafe")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.22")
    _subscription(db_session, subscriber, catalog_offer, "10.20.0.23")
    assignment = _assignment(db_session, subscriber, "10.20.0.22")
    db_session.commit()

    preview = preview_ip_assignment_service_ownership(
        db_session,
        assignment_ids=(assignment.id,),
    )
    assignment_id = assignment.id
    db_session.commit()
    with pytest.raises(IPAssignmentOwnershipError) as exc_info:
        reconcile_ip_assignment_service_ownership(
            db_session,
            _command((assignment_id,), preview.fingerprint),
        )
    assert exc_info.value.code.endswith(".unsafe_cohort")
