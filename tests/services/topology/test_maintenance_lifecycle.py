"""network.maintenance_lifecycle (OUTAGE_SLA_SPINE §5).

Pins the approved rules: seven-day notice gates exclusion, audience drift
refuses a silent start, overrun becomes an outage through the lifecycle
owner, and only the properly announced planned window excludes accrual.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.network_monitoring import NetworkDevice
from app.models.subscriber import Subscriber
from app.services.network import maintenance_lifecycle as maintenance
from app.services.network.customer_outage_accrual import (
    intervals_for_incident,
    reconcile_incident_accrual,
)
from app.services.topology.outage import declare_outage

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _nas_node_with_subs(db, offer_id, count):
    nas = NasDevice(name="NAS-MW", management_ip="10.7.0.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name="mw-node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    for index in range(count):
        subscriber = Subscriber(
            first_name="Mw",
            last_name=str(index),
            email=f"mw-{index}-{nas.id}@example.test",
        )
        db.add(subscriber)
        db.flush()
        db.add(
            Subscription(
                subscriber_id=subscriber.id,
                offer_id=offer_id,
                status=SubscriptionStatus.active,
                provisioning_nas_device_id=nas.id,
            )
        )
    db.flush()
    return nas, node


def _window(db, node, *, start, end, owner="noc@x"):
    return maintenance.create_maintenance_window(
        db,
        node=node,
        planned_start=start,
        planned_end=end,
        reason="planned upgrade",
        owner=owner,
        customer_message="We are upgrading your area.",
        backout_plan="Roll back to previous firmware.",
    )


def test_lifecycle_happy_path_stages_events(db_session, catalog_offer):
    _, node = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    window = _window(
        db_session,
        node,
        start=NOW + timedelta(days=10),
        end=NOW + timedelta(days=10, hours=2),
    )
    maintenance.approve_window(db_session, window, approved_by="ops-lead@x")
    maintenance.announce_window(db_session, window, now=NOW)

    assert window.status == "announced"
    assert window.audience_count == 2
    assert window.audience_token
    assert maintenance.notice_satisfied(window) is True

    maintenance.begin_window(db_session, window, now=NOW + timedelta(days=10))
    maintenance.complete_window(
        db_session, window, now=NOW + timedelta(days=10, hours=1)
    )
    assert window.status == "completed"

    staged = {
        row.event_type
        for row in db_session.query(EventStore).all()
        if row.event_type.startswith("maintenance.")
    }
    assert staged == {
        "maintenance.announced",
        "maintenance.started",
        "maintenance.completed",
    }


def test_short_notice_never_satisfies_the_exclusion_gate(db_session, catalog_offer):
    _, node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    window = _window(
        db_session,
        node,
        start=NOW + timedelta(days=2),
        end=NOW + timedelta(days=2, hours=2),
    )
    maintenance.approve_window(db_session, window, approved_by="ops-lead@x")
    maintenance.announce_window(db_session, window, now=NOW)

    assert window.status == "announced"
    assert maintenance.notice_satisfied(window) is False


def test_material_audience_drift_refuses_silent_start(db_session, catalog_offer):
    nas, node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    window = _window(
        db_session,
        node,
        start=NOW + timedelta(days=8),
        end=NOW + timedelta(days=8, hours=2),
    )
    maintenance.approve_window(db_session, window, approved_by="ops-lead@x")
    maintenance.announce_window(db_session, window, now=NOW)

    joiner = Subscriber(
        first_name="Drift", last_name="Sub", email=f"drift-{nas.id}@example.test"
    )
    db_session.add(joiner)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=joiner.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.active,
            provisioning_nas_device_id=nas.id,
        )
    )
    db_session.flush()

    with pytest.raises(ValueError, match="material scope drift"):
        maintenance.begin_window(db_session, window, now=NOW + timedelta(days=8))

    maintenance.begin_window(
        db_session, window, now=NOW + timedelta(days=8), drift_approved=True
    )
    assert window.status == "in_progress"
    assert window.audience_count == 2


def test_overrun_becomes_an_outage_through_the_lifecycle_owner(
    db_session, catalog_offer
):
    _, node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    window = _window(
        db_session,
        node,
        start=NOW + timedelta(days=9),
        end=NOW + timedelta(days=9, hours=2),
    )
    maintenance.approve_window(db_session, window, approved_by="ops-lead@x")
    maintenance.announce_window(db_session, window, now=NOW)
    maintenance.begin_window(db_session, window, now=NOW + timedelta(days=9))
    maintenance.complete_window(
        db_session, window, now=NOW + timedelta(days=9, hours=5)
    )

    assert window.status == "overrun"
    incident = maintenance.escalate_overrun_to_outage(
        db_session, window, now=NOW + timedelta(days=9, hours=5)
    )
    assert window.linked_outage_incident_id == incident.id
    assert incident.declared_by == "system:maintenance-overrun"
    # Idempotent: escalating again returns the same incident.
    again = maintenance.escalate_overrun_to_outage(db_session, window)
    assert again.id == incident.id


def test_announced_window_marks_accrual_as_reviewed_exclusion(
    db_session, catalog_offer
):
    _, node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    window = _window(
        db_session,
        node,
        start=NOW + timedelta(days=8),
        end=NOW + timedelta(days=8, hours=4),
    )
    maintenance.approve_window(db_session, window, approved_by="ops-lead@x")
    maintenance.announce_window(db_session, window, now=NOW)
    maintenance.begin_window(db_session, window, now=NOW + timedelta(days=8))

    incident = declare_outage(db_session, node=node)
    incident.started_at = NOW + timedelta(days=8, hours=1)
    db_session.flush()
    reconcile_incident_accrual(
        db_session, incident, now=NOW + timedelta(days=8, hours=1)
    )

    (interval,) = intervals_for_incident(db_session, incident.id)
    assert interval.exclusion_candidate == "planned_maintenance"


def test_unannounced_window_never_excludes(db_session, catalog_offer):
    _, node = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    window = _window(
        db_session,
        node,
        start=NOW - timedelta(hours=1),
        end=NOW + timedelta(hours=3),
    )
    maintenance.approve_window(db_session, window, approved_by="ops-lead@x")
    # Never announced: emergency work is unplanned by default.

    incident = declare_outage(db_session, node=node)
    reconcile_incident_accrual(db_session, incident, now=NOW)

    (interval,) = intervals_for_incident(db_session, incident.id)
    assert interval.exclusion_candidate is None
