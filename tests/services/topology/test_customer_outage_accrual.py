"""network.customer_outage_accrual: the immutable downtime ledger (§2, §7).

Pins the approved clock rules: intervals open at the earliest qualifying
observation for the original audience and at audience entry for joiners; a
first healthy observation ends provisionally; re-darkening before
finalization keeps one continuous interval; resolution finalizes at the
proven recovery timestamp (never resolved_at semantics for downtime);
discard finalizes as a reviewed exclusion candidate; reruns converge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.network.customer_outage_accrual import (
    intervals_for_incident,
    intervals_for_subscription,
    reconcile_incident_accrual,
)
from app.services.topology.outage import (
    confirm_incident,
    declare_outage,
    open_classifier_incident,
    record_scope_revision,
    reopen_incident,
    resolve_outage,
    start_clearing,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _nas_node_with_subs(db, offer_id, count, *, ip="10.8.0.1"):
    nas = NasDevice(name=f"NAS-{ip}", management_ip=ip)
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name=f"acc-node-{ip}",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    subscriptions = []
    for index in range(count):
        subscriber = Subscriber(
            first_name="Acc",
            last_name=str(index),
            email=f"acc-{index}-{nas.id}@example.test",
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


def _session_for(db, nas, subscription):
    row = RadiusActiveSession(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        nas_device_id=nas.id,
        username=f"user-{subscription.id}",
        acct_session_id=f"sess-{subscription.id}",
        session_start=NOW,
    )
    db.add(row)
    db.flush()
    return row


def test_confirmed_incident_opens_intervals_for_dark_members_only(
    db_session, catalog_offer
):
    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 3)
    _session_for(db_session, nas, subscriptions[0])
    incident = declare_outage(db_session, node=node)

    result = reconcile_incident_accrual(db_session, incident, now=NOW)

    assert result.opened == 2
    intervals = intervals_for_incident(db_session, incident.id)
    assert len(intervals) == 2
    dark_ids = {str(subscriptions[1].id), str(subscriptions[2].id)}
    assert {str(interval.subscription_id) for interval in intervals} == dark_ids
    for interval in intervals:
        assert interval.state == "confirmed_unavailable"
        assert interval.quality == "exact"
        assert interval.ended_at is None
        assert interval.finalized_at is None
        # Original audience: accrual starts at the incident's earliest
        # qualifying observation, not at reconcile time.
        started = interval.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        assert (
            started == incident.started_at.replace(tzinfo=UTC)
            if incident.started_at.tzinfo is None
            else incident.started_at
        )
        assert interval.first_evidence_ref.startswith("incident:")

    # Rerun converges: nothing new opens, nothing duplicates.
    rerun = reconcile_incident_accrual(db_session, incident, now=NOW)
    assert rerun.opened == 0
    assert len(intervals_for_incident(db_session, incident.id)) == 2


def test_recovery_hold_keeps_one_continuous_interval(db_session, catalog_offer):
    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = open_classifier_incident(db_session, root_node=node, now=NOW)
    confirm_incident(db_session, incident, now=NOW)
    reconcile_incident_accrual(db_session, incident, now=NOW)

    # Boundary recovers; the member comes back online -> provisional end at
    # the first healthy observation.
    start_clearing(db_session, incident, now=NOW + timedelta(minutes=10))
    session_row = _session_for(db_session, nas, subscriptions[0])
    healthy_at = NOW + timedelta(minutes=11)
    reconcile_incident_accrual(db_session, incident, now=healthy_at)
    (interval,) = intervals_for_incident(db_session, incident.id)
    assert interval.ended_at is not None
    assert interval.finalized_at is None

    # Service fails during the hold: the incident reopens and the SAME
    # interval continues — the provisional end clears, no second row.
    db_session.delete(session_row)
    db_session.flush()
    reopen_incident(db_session, incident)
    result = reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(minutes=13)
    )
    assert result.reopened == 1
    (interval,) = intervals_for_incident(db_session, incident.id)
    assert interval.ended_at is None
    assert interval.recovery_evidence_ref is None


def test_resolution_finalizes_at_proven_recovery_not_resolved_at(
    db_session, catalog_offer
):
    _, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = open_classifier_incident(db_session, root_node=node, now=NOW)
    confirm_incident(db_session, incident, now=NOW)
    reconcile_incident_accrual(db_session, incident, now=NOW)

    cleared_at = NOW + timedelta(minutes=30)
    start_clearing(db_session, incident, now=cleared_at)
    from app.services.topology.outage import resolve_classifier_incident

    resolve_classifier_incident(db_session, incident, now=NOW + timedelta(minutes=40))
    result = reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(minutes=41)
    )

    assert result.finalized == 1
    (interval,) = intervals_for_incident(db_session, incident.id)
    assert interval.finalized_at is not None
    ended = interval.ended_at
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=UTC)
    # Downtime closes at the proven recovery timestamp (cleared_at), not the
    # later administrative resolved_at.
    assert ended == cleared_at
    assert interval.exclusion_candidate is None
    # History for the subscription reads back.
    assert len(intervals_for_subscription(db_session, subscriptions[0].id)) == 1


def test_discard_finalizes_as_reviewed_exclusion(db_session, catalog_offer):
    _, node, _ = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = open_classifier_incident(db_session, root_node=node, now=NOW)
    confirm_incident(db_session, incident, now=NOW)
    reconcile_incident_accrual(db_session, incident, now=NOW)

    from app.services.topology.outage import discard_incident

    discard_incident(db_session, incident)
    reconcile_incident_accrual(db_session, incident, now=NOW + timedelta(minutes=5))

    (interval,) = intervals_for_incident(db_session, incident.id)
    assert interval.finalized_at is not None
    assert interval.exclusion_candidate == "incident_discarded"


def test_mid_incident_audience_entry_starts_at_entry_not_incident_start(
    db_session, catalog_offer
):
    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = declare_outage(db_session, node=node)
    # Pin the incident's clock to the fixed test NOW: entry clamping compares
    # against the incident start, and mixing wall-clock with fixed times made
    # this assertion depend on the time of day the suite ran.
    incident.started_at = NOW
    db_session.flush()
    reconcile_incident_accrual(db_session, incident, now=NOW)

    joiner = Subscriber(
        first_name="Join", last_name="Later", email=f"join-{nas.id}@example.test"
    )
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
    entry_at = NOW + timedelta(minutes=20)
    record_scope_revision(
        db_session, incident, reason="audience_drift", effective_at=entry_at
    )
    reconcile_incident_accrual(db_session, incident, now=NOW + timedelta(minutes=21))

    intervals = {
        str(interval.subscription_id): interval
        for interval in intervals_for_incident(db_session, incident.id)
    }
    late = intervals[str(joined.id)]
    started = late.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    assert started == entry_at
    # The original member's interval kept its original start — history was
    # not rewritten by the audience change.
    original = intervals[str(subscriptions[0].id)]
    assert original.started_at < late.started_at


def test_operator_resolution_end_to_end(db_session, catalog_offer):
    _, node, _ = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)
    reconcile_incident_accrual(db_session, incident, now=NOW)
    assert len(intervals_for_incident(db_session, incident.id)) == 2

    resolve_outage(db_session, incident.id)
    result = reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(hours=1)
    )

    assert result.finalized == 2
    for interval in intervals_for_incident(db_session, incident.id):
        assert interval.ended_at is not None
        assert interval.finalized_at is not None
