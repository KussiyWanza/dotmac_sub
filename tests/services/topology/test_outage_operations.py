from __future__ import annotations

from datetime import UTC, datetime

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.gis import GeoArea
from app.models.network import NetworkZone
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.operational_escalation import (
    OperationalDeliveryStatus,
    OperationalEntityType,
    OperationalEscalationDelivery,
    OperationalEscalationEvent,
    OperationalEscalationStatus,
    OperationalNotificationChannel,
    OperationalOwner,
    OperationalParticipantType,
    OperationalRoomLink,
    OperationalWatcher,
)
from app.models.service_team import (
    ServiceTeam,
    ServiceTeamCapability,
    ServiceTeamCapabilityDefinition,
    ServiceTeamCapabilityKey,
    ServiceTeamRoutingPolicy,
    ServiceTeamScopeBinding,
)
from app.models.subscriber import Subscriber
from app.services import operational_escalation, service_team_composition
from app.services.topology.outage import (
    confirm_incident,
    declare_outage,
    open_classifier_incident,
    resolve_classifier_incident,
    resolve_outage,
)
from app.services.topology.outage_operations import plan_outage_escalations


def _team(
    db_session,
    name: str,
    capability: ServiceTeamCapabilityKey,
    route_key: str,
) -> ServiceTeam:
    contract = service_team_composition.CAPABILITY_CONTRACTS[capability]
    if db_session.get(ServiceTeamCapabilityDefinition, capability.value) is None:
        db_session.add(
            ServiceTeamCapabilityDefinition(
                key=capability.value,
                display_name=contract.display_name,
                contract_owner=contract.contract_owner,
                contract_version=contract.contract_version,
                description=f"Test contract for {capability.value}",
                is_active=True,
            )
        )
    team = ServiceTeam(name=name)
    db_session.add(team)
    db_session.flush()
    db_session.add_all(
        [
            ServiceTeamCapability(
                team_id=team.id,
                capability_key=capability.value,
                is_active=True,
            ),
            ServiceTeamRoutingPolicy(
                domain="network.outage",
                route_key=route_key,
                team_id=team.id,
                priority=100,
                is_active=True,
            ),
        ]
    )
    db_session.flush()
    return team


def _seed_ops_teams(db_session):
    return {
        "operations": _team(
            db_session,
            "NOC",
            ServiceTeamCapabilityKey.outage_response,
            "incident.primary",
        ),
        "support": _team(
            db_session,
            "Support",
            ServiceTeamCapabilityKey.customer_support,
            "incident.support_watcher",
        ),
        "field": _team(
            db_session,
            "Field Service",
            ServiceTeamCapabilityKey.field_service,
            "incident.field_watcher",
        ),
    }


def _node(db_session) -> NetworkDevice:
    node = NetworkDevice(name="Garki OLT", is_active=True)
    db_session.add(node)
    db_session.flush()
    return node


def _zoned_node(db_session, *, geo_area_id=None) -> NetworkDevice:
    """A root node whose pop site sits in a zone, optionally bound to a GeoArea."""

    zone = NetworkZone(name="Garki Zone", geo_area_id=geo_area_id, is_active=True)
    db_session.add(zone)
    db_session.flush()
    site = PopSite(name="Garki POP", zone_id=zone.id)
    db_session.add(site)
    db_session.flush()
    node = NetworkDevice(name="Garki OLT", is_active=True, pop_site_id=site.id)
    db_session.add(node)
    db_session.flush()
    return node


def _geo_scoped_primary_team(
    db_session,
    *,
    name: str,
    geo_area_id,
    priority: int,
) -> ServiceTeam:
    """A second incident.primary team whose policy is scoped to one GeoArea.

    ``resolve_routing_team`` refuses priority ties as
    ``service_team_routing_ambiguous``, so the scoped policy must carry a
    distinct (lower) priority number than the seeded global policy.
    """

    team = ServiceTeam(name=name)
    db_session.add(team)
    db_session.flush()
    binding = ServiceTeamScopeBinding(
        team_id=team.id,
        scope_type="geo_area",
        geo_area_id=geo_area_id,
        is_active=True,
    )
    db_session.add(binding)
    db_session.flush()
    db_session.add_all(
        [
            ServiceTeamCapability(
                team_id=team.id,
                capability_key=ServiceTeamCapabilityKey.outage_response.value,
                is_active=True,
            ),
            ServiceTeamRoutingPolicy(
                domain="network.outage",
                route_key="incident.primary",
                team_id=team.id,
                scope_binding_id=binding.id,
                priority=priority,
                is_active=True,
            ),
        ]
    )
    db_session.flush()
    return team


def _subscriptions(db_session, offer_id, count: int) -> list[Subscription]:
    subscriptions = []
    for index in range(count):
        subscriber = Subscriber(
            first_name="Affected",
            last_name=str(index),
            email=f"affected-{index}@example.com",
            phone=f"+23480000000{index}",
        )
        db_session.add(subscriber)
        db_session.flush()
        subscription = Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer_id,
            status=SubscriptionStatus.active,
        )
        db_session.add(subscription)
        subscriptions.append(subscription)
    db_session.flush()
    return subscriptions


def test_declare_outage_creates_default_owner_watchers_and_room(db_session):
    teams = _seed_ops_teams(db_session)
    node = _node(db_session)

    incident = declare_outage(
        db_session,
        node=node,
        declared_by="noc@dotmac.io",
        severity="high",
        impact={"count": 184},
    )
    # The declared output commits atomically with the incident; the outage
    # lifecycle projection handler applies the operational consequences.
    db_session.commit()

    owner = db_session.query(OperationalOwner).one()
    watchers = db_session.query(OperationalWatcher).all()
    room = db_session.query(OperationalRoomLink).one()
    assert owner.entity_type == OperationalEntityType.outage
    assert owner.entity_id == str(incident.id)
    assert owner.service_team_id == teams["operations"].id
    assert owner.metadata_["affected_count"] == 184
    assert {watcher.service_team_id for watcher in watchers} == {
        teams["operations"].id,
        teams["support"].id,
        teams["field"].id,
    }
    assert {watcher.watcher_type for watcher in watchers} == {
        OperationalParticipantType.team
    }
    assert room.provider == "nextcloud_talk"
    assert room.metadata_["provisioning_status"] == "planned"
    assert "GARKI-OLT" in room.room_id
    assert db_session.query(OperationalEscalationEvent).count() == 0
    assert db_session.query(OperationalEscalationDelivery).count() == 0


def test_geo_scoped_policy_wins_for_incident_in_bound_zone(db_session):
    """A pop-site zone bound to GeoArea X routes to the X-scoped policy."""

    teams = _seed_ops_teams(db_session)
    area = GeoArea(name="Abuja Coverage", is_active=True)
    db_session.add(area)
    db_session.flush()
    node = _zoned_node(db_session, geo_area_id=area.id)
    abuja_noc = _geo_scoped_primary_team(
        db_session,
        name="Abuja NOC",
        geo_area_id=area.id,
        priority=10,
    )

    incident = declare_outage(
        db_session,
        node=node,
        declared_by="noc@dotmac.io",
        severity="high",
        impact={"count": 42},
    )
    db_session.commit()

    owner = db_session.query(OperationalOwner).one()
    assert owner.entity_type == OperationalEntityType.outage
    assert owner.entity_id == str(incident.id)
    assert owner.service_team_id == abuja_noc.id
    assert owner.service_team_id != teams["operations"].id
    # Owner metadata assignment is unchanged by geo-scoped selection.
    assert owner.metadata_["affected_count"] == 42
    watcher_teams = {
        watcher.service_team_id
        for watcher in db_session.query(OperationalWatcher).all()
    }
    assert abuja_noc.id in watcher_teams
    assert teams["support"].id in watcher_teams
    assert teams["field"].id in watcher_teams


def test_unbound_zone_falls_back_to_global_routing_policy(db_session):
    """Without a zone -> GeoArea binding the global policy still resolves."""

    teams = _seed_ops_teams(db_session)
    area = GeoArea(name="Abuja Coverage", is_active=True)
    db_session.add(area)
    db_session.flush()
    # The incident's pop-site zone exists but has no GeoArea binding, so the
    # geo-scoped policy (priority 10, bound to the area) must not match.
    node = _zoned_node(db_session, geo_area_id=None)
    _geo_scoped_primary_team(
        db_session,
        name="Abuja NOC",
        geo_area_id=area.id,
        priority=10,
    )

    incident = declare_outage(
        db_session,
        node=node,
        declared_by="noc@dotmac.io",
        severity="high",
        impact={"count": 7},
    )
    db_session.commit()

    owner = db_session.query(OperationalOwner).one()
    assert owner.entity_id == str(incident.id)
    assert owner.service_team_id == teams["operations"].id
    assert owner.metadata_["affected_count"] == 7


def test_classifier_outage_creates_operations_state_only_when_confirmed(db_session):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)

    incident = open_classifier_incident(
        db_session,
        root_node=node,
        affected_count=20,
        now=now,
    )
    db_session.commit()

    assert db_session.query(OperationalOwner).count() == 0
    assert db_session.query(OperationalWatcher).count() == 0

    confirm_incident(db_session, incident, now=now)
    db_session.commit()

    assert db_session.query(OperationalOwner).count() == 1
    assert db_session.query(OperationalWatcher).count() == 3
    assert db_session.query(OperationalRoomLink).count() == 1


def test_outage_operations_preserves_existing_primary_owner(db_session):
    teams = _seed_ops_teams(db_session)
    node = _node(db_session)
    incident = declare_outage(
        db_session,
        node=node,
        impact={"count": 4},
    )
    custom_owner = operational_escalation.set_owner(
        db_session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident.id,
        service_team_id=teams["field"].id,
        source="manual",
    )

    from app.services.topology.outage_operations import ensure_outage_operations

    ensure_outage_operations(db_session, incident)

    active_owners = (
        db_session.query(OperationalOwner)
        .filter(OperationalOwner.entity_type == OperationalEntityType.outage)
        .filter(OperationalOwner.entity_id == str(incident.id))
        .filter(OperationalOwner.is_active.is_(True))
        .all()
    )
    assert active_owners == [custom_owner]
    assert active_owners[0].service_team_id == teams["field"].id


def test_declare_outage_plans_deliveries_from_matching_policy(db_session):
    teams = _seed_ops_teams(db_session)
    node = _node(db_session)
    policy = operational_escalation.create_policy(
        db_session,
        name="High outage internal channels",
        entity_type=OperationalEntityType.outage,
        level=2,
        channels=[OperationalNotificationChannel.email],
        min_severity="high",
        min_affected_customers=100,
    )

    incident = declare_outage(
        db_session,
        node=node,
        severity="critical",
        impact={"count": 184},
    )
    db_session.commit()

    event = db_session.query(OperationalEscalationEvent).one()
    deliveries = db_session.query(OperationalEscalationDelivery).all()
    assert event.policy_id == policy.id
    assert event.entity_id == str(incident.id)
    assert event.trigger == "outage.created"
    assert event.level == 2
    assert event.affected_customer_count == 184
    assert {delivery.recipient_id for delivery in deliveries} == {
        str(teams["operations"].id),
        str(teams["support"].id),
        str(teams["field"].id),
    }
    assert {delivery.channel for delivery in deliveries} == {
        OperationalNotificationChannel.email
    }


def test_outage_policy_threshold_prevents_delivery_noise(db_session):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    operational_escalation.create_policy(
        db_session,
        name="Major outage only",
        entity_type=OperationalEntityType.outage,
        channels=[OperationalNotificationChannel.email],
        min_severity="high",
        min_affected_customers=100,
    )

    declare_outage(
        db_session,
        node=node,
        severity="medium",
        impact={"count": 99},
    )
    db_session.commit()

    assert db_session.query(OperationalEscalationEvent).count() == 0
    assert db_session.query(OperationalEscalationDelivery).count() == 0


def test_outage_policy_scope_matches_network_device(db_session):
    _seed_ops_teams(db_session)
    matching_node = _node(db_session)
    other_node = NetworkDevice(name="Wuse OLT", is_active=True)
    db_session.add(other_node)
    db_session.flush()
    operational_escalation.create_policy(
        db_session,
        name="Garki outage",
        entity_type=OperationalEntityType.outage,
        scope_type="network_device",
        scope_id=str(matching_node.id),
        channels=[OperationalNotificationChannel.email],
        min_severity="high",
    )

    declare_outage(
        db_session,
        node=other_node,
        severity="critical",
        impact={"count": 200},
    )
    db_session.commit()

    assert db_session.query(OperationalEscalationEvent).count() == 0


def test_outage_escalation_planning_is_idempotent_for_same_trigger(db_session):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    operational_escalation.create_policy(
        db_session,
        name="High outage internal channels",
        entity_type=OperationalEntityType.outage,
        channels=[OperationalNotificationChannel.email],
        min_severity="high",
    )
    incident = declare_outage(
        db_session,
        node=node,
        severity="high",
        impact={"count": 20},
    )
    db_session.commit()

    plan_outage_escalations(db_session, incident, trigger="outage.created")

    assert db_session.query(OperationalEscalationEvent).count() == 1
    assert db_session.query(OperationalEscalationDelivery).count() == 3


def test_customer_targeted_policy_adds_affected_subscribers_as_watchers(
    db_session,
    catalog_offer,
    monkeypatch,
):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    subscriptions = _subscriptions(db_session, catalog_offer.id, 2)
    # The projection handler re-resolves impact from topology; this test node
    # has no real topology, so pin the impact resolver to the seeded set.
    # Plain rows: the resolver runs inside the dispatch session's callback,
    # where touching ORM objects bound to the committed test session fails.
    from types import SimpleNamespace

    from app.services.topology import outage_operations

    impact_rows = [
        SimpleNamespace(subscriber_id=subscription.subscriber_id)
        for subscription in subscriptions
    ]
    monkeypatch.setattr(
        outage_operations,
        "affected_customers",
        lambda session, **kwargs: {"count": 2, "subscriptions": impact_rows},
    )
    operational_escalation.create_policy(
        db_session,
        name="Customer outage update",
        entity_type=OperationalEntityType.outage,
        channels=[
            {
                "channel": OperationalNotificationChannel.email,
                "recipients": ["watchers"],
                "participant_types": [OperationalParticipantType.subscriber],
            }
        ],
        min_severity="high",
        metadata={"customer_watchers": {"max_watchers": 5}},
    )

    incident = declare_outage(
        db_session,
        node=node,
        severity="high",
        impact={"count": 2, "subscriptions": subscriptions},
    )
    db_session.commit()

    subscriber_watchers = [
        watcher
        for watcher in db_session.query(OperationalWatcher).all()
        if watcher.subscriber_id is not None
    ]
    deliveries = db_session.query(OperationalEscalationDelivery).all()
    assert {watcher.subscriber_id for watcher in subscriber_watchers} == {
        subscription.subscriber_id for subscription in subscriptions
    }
    assert {delivery.recipient_type for delivery in deliveries} == {
        OperationalParticipantType.subscriber
    }
    assert {delivery.recipient_id for delivery in deliveries} == {
        str(subscription.subscriber_id) for subscription in subscriptions
    }
    assert incident.affected_count == 2


def test_customer_watcher_limit_prevents_bulk_customer_enrollment(
    db_session,
    catalog_offer,
    monkeypatch,
):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    subscriptions = _subscriptions(db_session, catalog_offer.id, 3)
    from types import SimpleNamespace

    from app.services.topology import outage_operations

    impact_rows = [
        SimpleNamespace(subscriber_id=subscription.subscriber_id)
        for subscription in subscriptions
    ]
    monkeypatch.setattr(
        outage_operations,
        "affected_customers",
        lambda session, **kwargs: {"count": 3, "subscriptions": impact_rows},
    )
    operational_escalation.create_policy(
        db_session,
        name="Small customer outage update only",
        entity_type=OperationalEntityType.outage,
        channels=[
            {
                "channel": OperationalNotificationChannel.email,
                "recipients": ["watchers"],
                "participant_types": [OperationalParticipantType.subscriber],
            }
        ],
        min_severity="high",
        metadata={"customer_watchers": {"max_watchers": 2}},
    )

    declare_outage(
        db_session,
        node=node,
        severity="high",
        impact={"count": 3, "subscriptions": subscriptions},
    )
    db_session.commit()

    assert (
        db_session.query(OperationalWatcher)
        .filter(OperationalWatcher.subscriber_id.is_not(None))
        .count()
        == 0
    )
    assert db_session.query(OperationalEscalationDelivery).count() == 0


def test_resolve_outage_cancels_pending_escalations(db_session):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    operational_escalation.create_policy(
        db_session,
        name="High outage internal channels",
        entity_type=OperationalEntityType.outage,
        channels=[OperationalNotificationChannel.email],
        min_severity="high",
    )
    incident = declare_outage(
        db_session,
        node=node,
        severity="high",
        impact={"count": 20},
    )
    db_session.commit()

    resolve_outage(db_session, incident.id)
    db_session.commit()

    event = db_session.query(OperationalEscalationEvent).one()
    deliveries = db_session.query(OperationalEscalationDelivery).all()
    assert event.status == OperationalEscalationStatus.canceled
    assert event.resolved_at is not None
    assert {delivery.delivery_status for delivery in deliveries} == {
        OperationalDeliveryStatus.suppressed
    }
    assert {delivery.metadata_["suppressed_reason"] for delivery in deliveries} == {
        "outage.resolved"
    }


def test_resolve_classifier_incident_cancels_pending_escalations(db_session):
    _seed_ops_teams(db_session)
    node = _node(db_session)
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    operational_escalation.create_policy(
        db_session,
        name="High classifier outage",
        entity_type=OperationalEntityType.outage,
        channels=[OperationalNotificationChannel.email],
        min_affected_customers=10,
    )
    incident = open_classifier_incident(
        db_session,
        root_node=node,
        affected_count=20,
        now=now,
    )
    confirm_incident(db_session, incident, now=now)
    db_session.commit()

    resolve_classifier_incident(db_session, incident, now=now)
    db_session.commit()

    event = db_session.query(OperationalEscalationEvent).one()
    assert event.status == OperationalEscalationStatus.canceled
    assert event.resolved_at is not None
    assert event.resolved_at.replace(tzinfo=UTC) == now
    assert {
        delivery.delivery_status
        for delivery in db_session.query(OperationalEscalationDelivery).all()
    } == {OperationalDeliveryStatus.suppressed}
