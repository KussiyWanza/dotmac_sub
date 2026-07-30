from __future__ import annotations

from sqlalchemy import or_

from app.models.network import FdhCabinet
from app.models.network_monitoring import NetworkDevice, OutageIncident, PopSite
from app.models.operational_escalation import (
    OperationalEntityType,
    OperationalEscalationEvent,
    OperationalEscalationPolicy,
    OperationalOwner,
    OperationalOwnerRole,
    OperationalParticipantType,
    OperationalRoomProvider,
    OperationalWatcher,
    OperationalWatcherRole,
)
from app.services import operational_escalation, service_team_composition
from app.services.network.zones import NetworkZones, ZoneGeoAreaResolutionKind
from app.services.topology.affected import affected_customers

SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "minor": 1,
    "medium": 2,
    "moderate": 2,
    "high": 3,
    "major": 3,
    "critical": 4,
}


def _scope_label(incident: OutageIncident) -> str:
    if incident.root_node_id is not None:
        return f"NODE-{incident.root_node_id}"
    if incident.basestation_id is not None:
        return f"SITE-{incident.basestation_id}"
    if incident.fdh_cabinet_id is not None:
        return f"FDH-{incident.fdh_cabinet_id}"
    return str(incident.id)


def _scope_name(session, incident: OutageIncident) -> str:
    if incident.root_node_id is not None:
        node = session.get(NetworkDevice, incident.root_node_id)
        if node is not None and node.name:
            return node.name
    if incident.basestation_id is not None:
        site = session.get(PopSite, incident.basestation_id)
        if site is not None and site.name:
            return site.name
    if incident.fdh_cabinet_id is not None:
        return f"FDH {incident.fdh_cabinet_id}"
    return str(incident.id)


OUTAGE_ROUTING_DOMAIN = "network.outage"
OUTAGE_PRIMARY_ROUTE = "incident.primary"
OUTAGE_SUPPORT_WATCHER_ROUTE = "incident.support_watcher"
OUTAGE_FIELD_WATCHER_ROUTE = "incident.field_watcher"


def _incident_zone_id(session, incident: OutageIncident):
    """Map this incident's own location handles to a network zone.

    The handles are outage-domain facts; which GeoArea a zone belongs to is the
    zone owner's concern and is resolved through ``NetworkZones.resolve_geo_area``.
    Handle precedence mirrors ``_scope_name``: root node, then basestation,
    then FDH cabinet.
    """

    if incident.root_node_id is not None:
        node = session.get(NetworkDevice, incident.root_node_id)
        if node is not None and node.pop_site_id is not None:
            site = session.get(PopSite, node.pop_site_id)
            if site is not None and site.zone_id is not None:
                return site.zone_id
    if incident.basestation_id is not None:
        site = session.get(PopSite, incident.basestation_id)
        if site is not None and site.zone_id is not None:
            return site.zone_id
    if incident.fdh_cabinet_id is not None:
        cabinet = session.get(FdhCabinet, incident.fdh_cabinet_id)
        if cabinet is not None and cabinet.zone_id is not None:
            return cabinet.zone_id
    return None


def _incident_geo_resolution(session, incident: OutageIncident):
    return NetworkZones.resolve_geo_area(session, _incident_zone_id(session, incident))


def _has_active_primary_owner(session, incident: OutageIncident) -> bool:
    return (
        session.query(OperationalOwner)
        .filter(OperationalOwner.entity_type == OperationalEntityType.outage)
        .filter(OperationalOwner.entity_id == str(incident.id))
        .filter(OperationalOwner.role == OperationalOwnerRole.primary)
        .filter(OperationalOwner.is_active.is_(True))
        .first()
        is not None
    )


def ensure_outage_operations(session, incident: OutageIncident) -> None:
    """Attach generic operational owner/watchers/room metadata to an outage.

    This does not send notifications. It only records who owns the incident, who
    should watch it, and the deterministic Nextcloud room identity later delivery
    slices can create/link.
    """

    entity_id = str(incident.id)
    geography = _incident_geo_resolution(session, incident)
    if geography.kind is ZoneGeoAreaResolutionKind.unavailable:
        # Approved fail-closed rule: a stale zone binding must deny the scoped
        # routing consequence rather than masquerade as unbound global routing.
        # The incident stays unrouted and loudly visible until the binding is
        # repaired; the coordination room below is still linked.
        operations = support = field = None
    else:
        operations = service_team_composition.resolve_routing_team(
            session,
            domain=OUTAGE_ROUTING_DOMAIN,
            route_key=OUTAGE_PRIMARY_ROUTE,
            geo_area_id=geography.geo_area_id,
        )
        support = service_team_composition.resolve_routing_team(
            session,
            domain=OUTAGE_ROUTING_DOMAIN,
            route_key=OUTAGE_SUPPORT_WATCHER_ROUTE,
            geo_area_id=geography.geo_area_id,
        )
        field = service_team_composition.resolve_routing_team(
            session,
            domain=OUTAGE_ROUTING_DOMAIN,
            route_key=OUTAGE_FIELD_WATCHER_ROUTE,
            geo_area_id=geography.geo_area_id,
        )

    if operations is not None and not _has_active_primary_owner(session, incident):
        operational_escalation.set_owner(
            session,
            entity_type=OperationalEntityType.outage,
            entity_id=entity_id,
            service_team_id=operations.team_id,
            source="outage_routing_policy",
            reason="Explicit outage primary route",
            metadata={
                "routing_policy_id": str(operations.policy_id),
                "status": incident.status,
                "severity": incident.severity,
                "affected_count": incident.affected_count,
            },
        )

    for team, role in (
        (operations, OperationalWatcherRole.lead),
        (support, OperationalWatcherRole.watcher),
        (field, OperationalWatcherRole.watcher),
    ):
        if team is None:
            continue
        operational_escalation.add_watcher(
            session,
            entity_type=OperationalEntityType.outage,
            entity_id=entity_id,
            service_team_id=team.team_id,
            role=role,
            source="outage_routing_policy",
            reason="Explicit outage coordination route",
            metadata={
                "routing_policy_id": str(team.policy_id),
                "status": incident.status,
                "severity": incident.severity,
                "affected_count": incident.affected_count,
            },
        )

    room_name = f"OUTAGE-{_scope_name(session, incident)}-{str(incident.id)[:8]}"
    operational_escalation.link_room(
        session,
        entity_type=OperationalEntityType.outage,
        entity_id=entity_id,
        provider=OperationalRoomProvider.nextcloud_talk,
        room_id=room_name.upper().replace(" ", "-"),
        room_name=room_name,
        metadata={
            "provisioning_status": "planned",
            "scope": _scope_label(incident),
            "status": incident.status,
            "severity": incident.severity,
            "affected_count": incident.affected_count,
        },
    )


def ensure_outage_customer_watchers(
    session,
    incident: OutageIncident,
    *,
    impact: dict | None = None,
) -> list[OperationalWatcher]:
    """Attach affected subscribers as watchers only for customer-targeted policies."""

    policies = _matching_outage_policies(session, incident)
    customer_limits = [
        _customer_watcher_limit(policy)
        for policy in policies
        if _policy_targets_subscribers(policy)
    ]
    if not customer_limits:
        return []
    max_watchers = max(customer_limits)
    resolved_impact = impact or affected_customers(
        session,
        node=session.get(NetworkDevice, incident.root_node_id)
        if incident.root_node_id
        else None,
        basestation=session.get(PopSite, incident.basestation_id)
        if incident.basestation_id
        else None,
        fdh=session.get(FdhCabinet, incident.fdh_cabinet_id)
        if incident.fdh_cabinet_id
        else None,
    )
    subscriber_ids = {
        subscription.subscriber_id
        for subscription in resolved_impact.get("subscriptions", [])
        if subscription.subscriber_id is not None
    }
    if len(subscriber_ids) > max_watchers:
        return []

    watchers = []
    for subscriber_id in sorted(subscriber_ids, key=str):
        watchers.append(
            operational_escalation.add_watcher(
                session,
                entity_type=OperationalEntityType.outage,
                entity_id=incident.id,
                subscriber_id=subscriber_id,
                source="outage_impact",
                reason="Affected customer",
                metadata={
                    "status": incident.status,
                    "severity": incident.severity,
                    "affected_count": incident.affected_count,
                },
            )
        )
    return watchers


def plan_outage_escalations(
    session,
    incident: OutageIncident,
    *,
    trigger: str,
) -> list[OperationalEscalationEvent]:
    """Create escalation events and pending deliveries for matching outage policies."""

    policies = _matching_outage_policies(session, incident, trigger=trigger)
    result = operational_escalation.emit_sla_event(
        session,
        entity_type=OperationalEntityType.outage,
        entity_id=incident.id,
        trigger=trigger,
        severity=incident.severity,
        affected_customer_count=incident.affected_count,
        metadata={
            "status": incident.status,
            "scope": _scope_label(incident),
        },
        policies=policies,
    )
    return list(result.events)


def _matching_outage_policies(
    session,
    incident: OutageIncident,
    *,
    trigger: str | None = None,
) -> list[OperationalEscalationPolicy]:
    policies = (
        operational_escalation.matching_policies(
            session,
            entity_type=OperationalEntityType.outage,
            trigger=trigger,
            severity=incident.severity,
            affected_customer_count=incident.affected_count,
        )
        if trigger
        else (
            session.query(OperationalEscalationPolicy)
            .filter(OperationalEscalationPolicy.is_active.is_(True))
            .filter(
                or_(
                    OperationalEscalationPolicy.entity_type
                    == OperationalEntityType.outage,
                    OperationalEscalationPolicy.entity_type.is_(None),
                )
            )
            .order_by(OperationalEscalationPolicy.level.asc())
            .all()
        )
    )
    return [policy for policy in policies if _policy_matches_incident(policy, incident)]


def _policy_matches_incident(
    policy: OperationalEscalationPolicy,
    incident: OutageIncident,
) -> bool:
    if not _scope_matches(policy, incident):
        return False
    if (
        policy.min_affected_customers is not None
        and (incident.affected_count or 0) < policy.min_affected_customers
    ):
        return False
    if policy.min_severity and not _severity_at_least(
        incident.severity,
        policy.min_severity,
    ):
        return False
    return True


def _scope_matches(
    policy: OperationalEscalationPolicy,
    incident: OutageIncident,
) -> bool:
    if not policy.scope_type or not policy.scope_id:
        return True
    scope_ids = {
        "network_device": incident.root_node_id,
        "node": incident.root_node_id,
        "root_node": incident.root_node_id,
        "pop_site": incident.basestation_id,
        "site": incident.basestation_id,
        "basestation": incident.basestation_id,
        "fdh_cabinet": incident.fdh_cabinet_id,
    }
    return str(scope_ids.get(policy.scope_type) or "") == policy.scope_id


def _severity_at_least(actual: str | None, minimum: str) -> bool:
    if actual is None:
        return False
    actual_key = actual.lower()
    minimum_key = minimum.lower()
    if actual_key not in SEVERITY_RANK or minimum_key not in SEVERITY_RANK:
        return actual_key == minimum_key
    return SEVERITY_RANK[actual_key] >= SEVERITY_RANK[minimum_key]


def _policy_targets_subscribers(policy: OperationalEscalationPolicy) -> bool:
    defaults = (policy.metadata_ or {}).get("delivery_defaults") or {}
    for raw_channel in policy.channels or []:
        if isinstance(raw_channel, dict):
            participant_types = raw_channel.get("participant_types") or defaults.get(
                "participant_types"
            )
        else:
            participant_types = defaults.get("participant_types")
        if participant_types and OperationalParticipantType.subscriber in set(
            participant_types
        ):
            return True
    return False


def _customer_watcher_limit(policy: OperationalEscalationPolicy) -> int:
    settings = (policy.metadata_ or {}).get("customer_watchers") or {}
    if isinstance(settings, dict) and settings.get("enabled") is False:
        return 0
    raw_limit = settings.get("max_watchers") if isinstance(settings, dict) else None
    try:
        return max(0, int(raw_limit or 100))
    except (TypeError, ValueError):
        return 100
