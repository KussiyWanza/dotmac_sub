"""The combined network-programme flow, end to end in one scenario.

Proves the composed programme (path projection + impact resolver + scope
revisions + ticket links + downtime ledger + shadow SLA) as ONE flow across
the twelve required steps: confirm a shared-boundary incident; resolve exact
potential and confirmed audiences; record one immutable scope revision and
the canonical ticket link; open per-subscription downtime only where
evidence qualifies; show the incident consistently in Customer 360 and the
Explorer; process mid-incident audience entry and rerooting; verify failover
customers accrue nothing; keep clearing→reopened one continuous interval;
resolve at the verified recovery time; recalculate the shadow SLA without
overlapping intervals; confirm recovery closes no tickets; and pin that the
legacy and new SLA figures never render together.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.services import customer_service_level as sla
from app.services import network_explorer as explorer
from app.services import web_customer_details as details
from app.services.network import service_impact
from app.services.network.customer_outage_accrual import (
    intervals_for_incident,
    reconcile_incident_accrual,
)
from app.services.service_impact_contracts import ImpactState
from app.services.topology import outage

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _shared_boundary(db, offer_id, count):
    nas = NasDevice(name="NAS-FLOW", management_ip="10.4.0.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name="flow-node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    subscriptions = []
    for index in range(count):
        subscriber = Subscriber(
            first_name="Flow",
            last_name=str(index),
            email=f"flow-{index}-{nas.id}@example.test",
        )
        db.add(subscriber)
        db.flush()
        subscription = Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer_id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas.id,
            login=f"flow-{index}",
        )
        db.add(subscription)
        subscriptions.append(subscription)
    db.flush()
    for subscription in subscriptions:
        subscription.created_at = NOW - timedelta(days=15)
    db.flush()
    return nas, node, subscriptions


def _session(db, nas, subscription):
    row = RadiusActiveSession(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        nas_device_id=nas.id,
        username=subscription.login,
        acct_session_id=f"flow-{subscription.id}",
        session_start=NOW,
    )
    db.add(row)
    db.flush()
    return row


def test_combined_outage_sla_flow(db_session, catalog_offer):
    nas, node, subscriptions = _shared_boundary(db_session, catalog_offer.id, 3)
    failover = subscriptions[0]
    _session(db_session, nas, failover)

    # 1. Confirm a shared-device incident (classifier path).
    incident = outage.open_classifier_incident(db_session, root_node=node, now=NOW)
    incident.started_at = NOW
    db_session.flush()
    outage.confirm_incident(db_session, incident, now=NOW)

    # 2. Exact potential vs confirmed audiences.
    impacts = {
        impact.subscription_id: impact
        for impact in service_impact.resolve_incident_impacts(
            db_session, incident, now=NOW
        )
    }
    assert impacts[str(failover.id)].state is ImpactState.potentially_affected
    dark_ids = {str(subscriptions[1].id), str(subscriptions[2].id)}
    for dark_id in dark_ids:
        assert impacts[dark_id].state is ImpactState.confirmed_unavailable

    # 3. One immutable scope revision so far, plus the canonical ticket link.
    revisions = outage.list_scope_revisions(db_session, incident.id)
    assert [revision.sequence for revision in revisions] == [1]
    ticket = Ticket(title="Flow infra ticket", status="open")
    db_session.add(ticket)
    db_session.flush()
    outage.link_infrastructure_ticket(
        db_session, incident, ticket.id, linked_by="noc@x"
    )

    # 4. Downtime opens only where evidence qualifies (dark members only).
    reconcile_incident_accrual(db_session, incident, now=NOW)
    intervals = intervals_for_incident(db_session, incident.id)
    assert {str(i.subscription_id) for i in intervals} == dark_ids

    # 5. Customer 360 and the Explorer tell the same story.
    card_word = details._build_service_impact(db_session, subscriptions[1])
    assert card_word is not None
    assert card_word["state"] == "confirmed_unavailable"
    assert card_word["incident_id"] == str(incident.id)
    inspector = explorer.build_inspector(
        db_session, f"device:{node.id}", include_customer_identity=True
    )
    assert any(row.incident_id == str(incident.id) for row in inspector.incidents)
    summary = service_impact.summarize_incident_impact(db_session, incident, now=NOW)
    assert summary.confirmed_unavailable == 2
    assert summary.potentially_affected == 1

    # 6. Mid-incident audience entry: the joiner accrues from entry, not
    # from the incident start.
    joiner_person = Subscriber(
        first_name="Flow", last_name="Join", email=f"flow-join-{nas.id}@example.test"
    )
    db_session.add(joiner_person)
    db_session.flush()
    joiner = Subscription(
        subscriber_id=joiner_person.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas.id,
        login="flow-join",
    )
    db_session.add(joiner)
    db_session.flush()
    entry_at = NOW + timedelta(minutes=30)
    drift = outage.record_scope_revision(
        db_session, incident, reason="audience_drift", effective_at=entry_at
    )
    assert drift is not None and drift.sequence == 2
    reconcile_incident_accrual(db_session, incident, now=NOW + timedelta(minutes=31))
    by_subscription = {
        str(i.subscription_id): i
        for i in intervals_for_incident(db_session, incident.id)
    }
    joiner_started = by_subscription[str(joiner.id)].started_at
    if joiner_started.tzinfo is None:
        joiner_started = joiner_started.replace(tzinfo=UTC)
    assert joiner_started == entry_at

    # 7. The failover customer never accrued.
    assert str(failover.id) not in by_subscription

    # 8. clearing → reopened stays one continuous interval.
    outage.start_clearing(db_session, incident, now=NOW + timedelta(hours=1))
    recovered = _session(db_session, nas, subscriptions[1])
    reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(hours=1, minutes=1)
    )
    interval_b = {
        str(i.subscription_id): i
        for i in intervals_for_incident(db_session, incident.id)
    }[str(subscriptions[1].id)]
    assert interval_b.ended_at is not None and interval_b.finalized_at is None
    db_session.delete(recovered)
    db_session.flush()
    outage.reopen_incident(db_session, incident)
    reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(hours=1, minutes=5)
    )
    refreshed = {
        str(i.subscription_id): i
        for i in intervals_for_incident(db_session, incident.id)
    }
    assert refreshed[str(subscriptions[1].id)].ended_at is None
    # Still one row per subscription — no duplicate intervals.
    assert len(intervals_for_incident(db_session, incident.id)) == 3

    # 9. Resolution closes at the verified recovery time, not resolved_at.
    cleared_at = NOW + timedelta(hours=2)
    outage.start_clearing(db_session, incident, now=cleared_at)
    outage.resolve_classifier_incident(
        db_session, incident, now=NOW + timedelta(hours=2, minutes=15)
    )
    reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(hours=2, minutes=16)
    )
    for interval in intervals_for_incident(db_session, incident.id):
        assert interval.finalized_at is not None
        ended = interval.ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        assert ended == cleared_at

    # 10. The shadow SLA unions the member's downtime without overlap: a
    # second overlapping incident on the same boundary must not double-count.
    second = outage.declare_outage(db_session, node=node)
    second.started_at = NOW + timedelta(minutes=10)
    db_session.flush()
    reconcile_incident_accrual(db_session, second, now=NOW + timedelta(minutes=11))
    outage.resolve_outage(db_session, second.id)
    second.cleared_at = NOW + timedelta(hours=1, minutes=30)
    db_session.flush()
    reconcile_incident_accrual(
        db_session, second, now=NOW + timedelta(hours=2, minutes=20)
    )
    score = sla.score_subscription_period(
        db_session,
        subscriptions[2],
        now=NOW + timedelta(hours=3),
    )
    # Union of [NOW, cleared_at] windows: exactly two hours — never a sum
    # of the two incidents' overlapping intervals.
    assert score.unavailable_seconds == 2 * 3600
    assert score.verdict.value == "no_contractual_sla"

    # 11. Recovery never transitioned the canonical ticket.
    db_session.refresh(ticket)
    assert ticket.status == "open"

    # 12. The legacy and new availability figures never render together.
    template = Path("templates/admin/customers/detail.html").read_text()
    assert "customer_availability" not in template
