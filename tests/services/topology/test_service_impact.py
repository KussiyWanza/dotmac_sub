"""network.service_impact: exposure is never downtime (OUTAGE_SLA_SPINE §1).

Pins the approved evidence rules: confirmed shared-boundary incidents cover
their exact audience, live sessions prevent accrual, suspected incidents are
exposure only, clearing splits restored vs unknown, terminal incidents carry
no live impact, and no covering incident means None — never a manufactured
confirmed word.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.network.service_impact import (
    resolve_incident_impacts,
    resolve_subscription_impact,
    summarize_incident_impact,
)
from app.services.service_impact_contracts import ImpactEvidenceKind, ImpactState
from app.services.topology.outage import declare_outage


def _nas_node_with_subs(db, offer_id, count):
    nas = NasDevice(name=f"NAS-{count}", management_ip="10.9.0.1")
    db.add(nas)
    db.flush()
    node = NetworkDevice(
        name="impact-node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        is_active=True,
    )
    db.add(node)
    db.flush()
    subscriptions = []
    for index in range(count):
        subscriber = Subscriber(
            first_name="Imp",
            last_name=str(index),
            email=f"imp-{index}-{nas.id}@example.test",
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


def _live_session(db, nas, subscription):
    db.add(
        RadiusActiveSession(
            subscription_id=subscription.id,
            subscriber_id=subscription.subscriber_id,
            nas_device_id=nas.id,
            username=subscription.login or f"user-{subscription.id}",
            acct_session_id=f"sess-{subscription.id}",
            session_start=datetime.now(UTC),
        )
    )
    db.flush()


def test_confirmed_incident_confirms_offline_audience_only(db_session, catalog_offer):
    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 3)
    # One member keeps a live session — continued service prevents accrual.
    _live_session(db_session, nas, subscriptions[0])
    incident = declare_outage(db_session, node=node)  # operator open = confirmed

    impacts = {
        impact.subscription_id: impact
        for impact in resolve_incident_impacts(db_session, incident)
    }

    online = impacts[str(subscriptions[0].id)]
    assert online.state is ImpactState.potentially_affected
    assert online.reason == "continued_service_observed"
    assert {evidence.kind for evidence in online.evidence} == {
        ImpactEvidenceKind.shared_boundary_failure,
        ImpactEvidenceKind.independent_observation,
    }
    for offline in (subscriptions[1], subscriptions[2]):
        impact = impacts[str(offline.id)]
        assert impact.state is ImpactState.confirmed_unavailable
        assert impact.reason == "shared_boundary_confirmed"
        assert impact.membership_token
        assert impact.scope_revision_sequence == 1
        assert impact.evidence[0].owner == "network.outage_lifecycle"

    summary = summarize_incident_impact(db_session, incident)
    assert summary.audience_count == 3
    assert summary.confirmed_unavailable == 2
    assert summary.potentially_affected == 1


def test_suspected_incident_is_exposure_only(db_session, catalog_offer):
    from app.services.topology.outage import open_classifier_incident

    _, node, _ = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    incident = open_classifier_incident(
        db_session, root_node=node, now=datetime.now(UTC)
    )

    impacts = resolve_incident_impacts(db_session, incident)

    assert len(impacts) == 2
    assert all(impact.state is ImpactState.potentially_affected for impact in impacts)
    assert all(impact.reason == "incident_suspected" for impact in impacts)


def test_clearing_splits_restored_from_unknown(db_session, catalog_offer):
    from app.services.topology.outage import (
        confirm_incident,
        open_classifier_incident,
        start_clearing,
    )

    nas, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    now = datetime.now(UTC)
    incident = open_classifier_incident(db_session, root_node=node, now=now)
    confirm_incident(db_session, incident, now=now)
    start_clearing(db_session, incident, now=now)
    _live_session(db_session, nas, subscriptions[0])

    impacts = {
        impact.subscription_id: impact
        for impact in resolve_incident_impacts(db_session, incident)
    }

    assert impacts[str(subscriptions[0].id)].state is ImpactState.restored
    dark = impacts[str(subscriptions[1].id)]
    assert dark.state is ImpactState.unknown
    assert dark.reason == "boundary_recovered_endpoint_dark"


def test_terminal_incident_carries_no_live_impact(db_session, catalog_offer):
    from app.services.topology.outage import resolve_outage

    _, node, _ = _nas_node_with_subs(db_session, catalog_offer.id, 1)
    incident = declare_outage(db_session, node=node)
    resolve_outage(db_session, incident.id)

    assert resolve_incident_impacts(db_session, incident) == ()


def test_no_covering_incident_resolves_none_not_confirmed(db_session, catalog_offer):
    _, _, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 1)

    assert resolve_subscription_impact(db_session, subscriptions[0].id) is None
    assert resolve_subscription_impact(db_session, uuid.uuid4()) is None


def test_subscription_lookup_finds_its_covering_incident(db_session, catalog_offer):
    _, node, subscriptions = _nas_node_with_subs(db_session, catalog_offer.id, 2)
    incident = declare_outage(db_session, node=node)

    impact = resolve_subscription_impact(db_session, subscriptions[1].id)

    assert impact is not None
    assert impact.incident_id == str(incident.id)
    assert impact.state is ImpactState.confirmed_unavailable
