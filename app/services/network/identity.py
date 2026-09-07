"""Network identity resolver.

This is the base SOT layer for cross-model links. It answers "what network
object does this row point at?" without deciding access, outage, or event
policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription
from app.models.network import CPEDevice, OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.radius_active_session import RadiusActiveSession
from app.services.common import coerce_uuid
from app.services.nas._mikrotik import (
    MikrotikLiveBandwidthTarget,
    MikrotikLiveTargetError,
    build_mikrotik_live_bandwidth_target,
)


@dataclass(frozen=True)
class NetworkIdentity:
    kind: str
    id: object
    name: str | None = None
    network_device: NetworkDevice | None = None
    pop_site: PopSite | None = None
    source: str | None = None


@dataclass(frozen=True)
class LiveBandwidthNetworkIdentity:
    """Detached subscription and NAS identity for a live diagnostic read."""

    subscriber_id: UUID
    target: MikrotikLiveBandwidthTarget | None
    configuration_error: str | None = None


def live_bandwidth_identity_for_subscription(
    db: Session,
    subscription_id: UUID,
) -> LiveBandwidthNetworkIdentity | None:
    """Resolve typed live-read inputs without exporting catalog ORM entities."""

    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        return None
    device = subscription.provisioning_nas_device
    if device is None:
        return LiveBandwidthNetworkIdentity(
            subscriber_id=subscription.subscriber_id,
            target=None,
            configuration_error="missing_nas",
        )
    try:
        target = build_mikrotik_live_bandwidth_target(
            device,
            login=subscription.login or "",
        )
    except MikrotikLiveTargetError as exc:
        return LiveBandwidthNetworkIdentity(
            subscriber_id=subscription.subscriber_id,
            target=None,
            configuration_error=str(exc),
        )
    return LiveBandwidthNetworkIdentity(
        subscriber_id=subscription.subscriber_id,
        target=target,
    )


def network_device_for_matched_entity(
    db: Session,
    *,
    device_type: str,
    device_id,
) -> NetworkDevice | None:
    return (
        db.query(NetworkDevice)
        .filter(
            NetworkDevice.matched_device_type == device_type,
            NetworkDevice.matched_device_id == coerce_uuid(device_id),
        )
        .first()
    )


def identity_for_network_device(
    db: Session,
    network_device: NetworkDevice | str,
) -> NetworkIdentity | None:
    network_device_obj = (
        network_device
        if isinstance(network_device, NetworkDevice)
        else db.get(NetworkDevice, coerce_uuid(network_device))
    )
    if network_device_obj is None:
        return None
    pop = (
        db.get(PopSite, network_device_obj.pop_site_id)
        if network_device_obj.pop_site_id
        else None
    )
    return NetworkIdentity(
        kind="network_device",
        id=network_device_obj.id,
        name=network_device_obj.name,
        network_device=network_device_obj,
        pop_site=pop,
        source=network_device_obj.source,
    )


def identity_for_subscription(
    db: Session,
    subscription: Subscription | str,
) -> NetworkIdentity | None:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        return None
    if subscription_obj.provisioning_nas_device_id is not None:
        nas = db.get(NasDevice, subscription_obj.provisioning_nas_device_id)
        node = network_device_for_matched_entity(
            db,
            device_type="nas",
            device_id=subscription_obj.provisioning_nas_device_id,
        )
        return NetworkIdentity(
            kind="nas",
            id=subscription_obj.provisioning_nas_device_id,
            name=getattr(nas, "name", None),
            network_device=node,
            pop_site=db.get(PopSite, node.pop_site_id)
            if node and node.pop_site_id
            else None,
            source="subscription.provisioning_nas_device_id",
        )
    return None


def identity_for_radius_session(
    db: Session,
    session: RadiusActiveSession | str,
) -> NetworkIdentity | None:
    session_obj = (
        session
        if isinstance(session, RadiusActiveSession)
        else db.get(RadiusActiveSession, coerce_uuid(session))
    )
    if session_obj is None or session_obj.nas_device_id is None:
        return None
    nas = db.get(NasDevice, session_obj.nas_device_id)
    node = network_device_for_matched_entity(
        db, device_type="nas", device_id=session_obj.nas_device_id
    )
    return NetworkIdentity(
        kind="nas",
        id=session_obj.nas_device_id,
        name=getattr(nas, "name", None),
        network_device=node,
        pop_site=db.get(PopSite, node.pop_site_id)
        if node and node.pop_site_id
        else None,
        source="radius_active_sessions.nas_device_id",
    )


def identity_for_ont_assignment(
    db: Session,
    assignment: OntAssignment | str,
) -> NetworkIdentity | None:
    assignment_obj = (
        assignment
        if isinstance(assignment, OntAssignment)
        else db.get(OntAssignment, coerce_uuid(assignment))
    )
    if assignment_obj is None:
        return None
    ont = db.get(OntUnit, assignment_obj.ont_unit_id)
    if ont is None or ont.olt_device_id is None:
        return None
    olt = db.get(OLTDevice, ont.olt_device_id)
    node = network_device_for_matched_entity(
        db, device_type="olt", device_id=ont.olt_device_id
    )
    return NetworkIdentity(
        kind="olt",
        id=ont.olt_device_id,
        name=getattr(olt, "name", None),
        network_device=node,
        pop_site=db.get(PopSite, node.pop_site_id)
        if node and node.pop_site_id
        else None,
        source="ont_assignments.ont_unit_id",
    )


def identity_for_cpe(db: Session, cpe: CPEDevice | str) -> NetworkIdentity | None:
    cpe_obj = cpe if isinstance(cpe, CPEDevice) else db.get(CPEDevice, coerce_uuid(cpe))
    if cpe_obj is None or cpe_obj.parent_network_device_id is None:
        return None
    node = db.get(NetworkDevice, cpe_obj.parent_network_device_id)
    if node is None:
        return None
    return NetworkIdentity(
        kind="ap",
        id=node.id,
        name=node.name,
        network_device=node,
        pop_site=db.get(PopSite, node.pop_site_id) if node.pop_site_id else None,
        source="cpe_devices.parent_network_device_id",
    )
