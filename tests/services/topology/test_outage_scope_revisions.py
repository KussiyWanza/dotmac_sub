"""Immutable incident scope/audience revisions (OUTAGE_SLA_SPINE §3).

History is append-only, content-idempotent, and exact: each revision carries
the scope, the order-independent membership token, and entered/retained/left
member rows so the downtime ledger can reconstruct any past audience without
trusting the mutable incident root.
"""

from __future__ import annotations

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice
from app.models.subscriber import Subscriber
from app.services.bulk_actions import membership_scope_token
from app.services.topology.outage import (
    declare_outage,
    list_scope_revisions,
    record_scope_revision,
    repoint_root,
    revision_audience_subscription_ids,
)


def _nas_node_with_subs(db, offer_id, count, *, name="node-a", nas_name="NAS-A"):
    nas = NasDevice(name=nas_name, management_ip=f"10.0.{count}.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name=name,
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    subscriptions = []
    for index in range(count):
        subscriber = Subscriber(
            first_name="Rev",
            last_name=str(index),
            email=f"rev-{index}-{nas.id}@example.test",
        )
        db.add(subscriber)
        db.flush()
        subscription = Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer_id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas.id,
        )
        db.add(subscription)
        subscriptions.append(subscription)
    db.flush()
    return nas, node, subscriptions


def test_declare_appends_initial_revision_with_exact_audience(
    db_session, catalog_offer
):
    _, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 3)

    incident = declare_outage(db_session, node=node, declared_by="noc@x")

    revisions = list_scope_revisions(db_session, incident.id)
    assert [revision.sequence for revision in revisions] == [1]
    first = revisions[0]
    assert first.reason == "declared"
    assert first.new_scope_type == "node"
    assert first.new_scope_id == node.id
    assert first.old_scope_type is None
    assert first.member_count == 3
    assert first.entered_count == 3
    assert first.left_count == 0
    expected_ids = {str(subscription.id) for subscription in subscriptions}
    assert revision_audience_subscription_ids(first) == expected_ids
    # The token is the canonical order-independent membership fingerprint.
    assert first.membership_token == membership_scope_token(
        f"node:{node.id}", sorted(expected_ids)
    )
    # The revision snapshots effective_at from the incident's start.
    # (SQLite round-trips naive datetimes; compare the instant.)
    from datetime import UTC

    effective = first.effective_at
    if effective.tzinfo is None:
        effective = effective.replace(tzinfo=UTC)
    assert effective == incident.started_at


def test_unchanged_scope_and_audience_appends_nothing(db_session, catalog_offer):
    _, node, _ = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)

    assert record_scope_revision(db_session, incident, reason="confirmed") is None
    assert len(list_scope_revisions(db_session, incident.id)) == 1


def test_audience_drift_appends_delta_revision(db_session, catalog_offer):
    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)

    joiner = Subscriber(first_name="New", last_name="Sub", email=f"new-{nas.id}@x.test")
    db_session.add(joiner)
    db_session.flush()
    joined = Subscription(
        subscriber_id=joiner.id,
        offer_id=subscriptions[0].offer_id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas.id,
    )
    db_session.add(joined)
    db_session.flush()

    revision = record_scope_revision(db_session, incident, reason="audience_drift")

    assert revision is not None
    assert revision.sequence == 2
    assert revision.reason == "audience_drift"
    assert revision.entered_count == 1
    assert revision.left_count == 0
    assert revision.member_count == 3
    memberships = {
        str(member.subscription_id): member.membership for member in revision.members
    }
    assert memberships[str(joined.id)] == "entered"
    assert sum(1 for value in memberships.values() if value == "retained") == 2


def test_reroot_appends_revision_with_old_and_new_scope(db_session, catalog_offer):
    _, node_a, subs_a = _nas_node_with_subs(
        db_session, catalog_offer.id, 2, name="node-a", nas_name="NAS-A"
    )
    _, node_b, subs_b = _nas_node_with_subs(
        db_session, catalog_offer.id, 1, name="node-b", nas_name="NAS-B"
    )
    incident = declare_outage(db_session, node=node_a)

    moved = repoint_root(db_session, incident, node_b)

    assert moved is True
    revisions = list_scope_revisions(db_session, incident.id)
    assert [revision.sequence for revision in revisions] == [1, 2]
    reroot = revisions[1]
    assert reroot.reason == "rerooted"
    assert reroot.old_scope_type == "node"
    assert reroot.old_scope_id == node_a.id
    assert reroot.new_scope_id == node_b.id
    assert reroot.entered_count == 1
    assert reroot.left_count == 2
    assert revision_audience_subscription_ids(reroot) == {str(subs_b[0].id)}
    left_ids = {
        str(member.subscription_id)
        for member in reroot.members
        if member.membership == "left"
    }
    assert left_ids == {str(subscription.id) for subscription in subs_a}


def test_reroot_to_same_root_appends_nothing(db_session, catalog_offer):
    _, node, _ = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = declare_outage(db_session, node=node)

    assert repoint_root(db_session, incident, node) is False
    assert len(list_scope_revisions(db_session, incident.id)) == 1


def test_history_is_append_only_and_monotonic(db_session, catalog_offer):
    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = declare_outage(db_session, node=node)

    # Rerun after drift, then rerun again with no change: sequences stay
    # strictly monotonic and content-idempotent.
    subscriptions[0].status = SubscriptionStatus.suspended
    db_session.flush()
    drifted = record_scope_revision(db_session, incident, reason="audience_drift")
    assert drifted is not None and drifted.sequence == 2
    assert record_scope_revision(db_session, incident, reason="audience_drift") is None

    revisions = list_scope_revisions(db_session, incident.id)
    assert [revision.sequence for revision in revisions] == [1, 2]
    # The drift revision records the exact departure.
    assert revisions[1].left_count == 1
    assert revisions[1].member_count == 0
