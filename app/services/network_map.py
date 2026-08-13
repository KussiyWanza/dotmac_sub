from __future__ import annotations

import json
import logging
import math
from collections import Counter
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.fiber_support import FiberSupportStructure
from app.models.gis import ServiceBuilding
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberTerminationPoint,
    OLTDevice,
    OntUnit,
    Splitter,
)
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Address, Subscriber
from app.services import settings_spec
from app.services.device_operational_status import (
    DeviceOperationalState,
    annotate_operational_status,
    derive_ont_operational_status,
)
from app.services.network.radius_sessions import (
    SubscriptionSessionBinding,
    SubscriptionSessionSnapshot,
    SubscriptionSessionState,
    subscription_session_snapshots,
)
from app.services.network.signal_thresholds import (
    classify_signal,
    get_signal_thresholds,
)
from app.services.network_map_contracts import (
    NetworkMapCustomerConnectivity,
    NetworkMapCustomerLayer,
    NetworkMapCustomerRouteKind,
    NetworkMapFeature,
    NetworkMapFeatureProperties,
    NetworkMapFeatureType,
    NetworkMapInspectionStatus,
    NetworkMapLineGeometry,
    NetworkMapLink,
    NetworkMapPermission,
    NetworkMapPlantLayer,
    NetworkMapPlantProjection,
    NetworkMapPointGeometry,
    NetworkMapProjection,
    NetworkMapSignalQuality,
    NetworkMapStats,
    NetworkMapStatusPresentation,
    NetworkMapSupportLifecycle,
    NetworkMapSupportType,
    NetworkMapV2Endpoint,
    NetworkMapV2GeometryStatus,
    NetworkMapV2Layer,
    NetworkMapV2Projection,
    NetworkMapV2SegmentTopology,
    NetworkMapV2TopologyStatus,
    NetworkMapV2UnavailableLayer,
)
from app.services.status_presentation import access_session_status_presentation

logger = logging.getLogger(__name__)


def _point(longitude: object, latitude: object) -> NetworkMapPointGeometry:
    return NetworkMapPointGeometry(
        longitude=float(str(longitude)),
        latitude=float(str(latitude)),
    )


def _line_geometry(geojson: str) -> NetworkMapLineGeometry:
    """Normalize PostGIS GeoJSON once at the persistence boundary."""

    decoded: object = json.loads(geojson)
    if not isinstance(decoded, dict) or decoded.get("type") != "LineString":
        raise ValueError("Fiber segment geometry must be a GeoJSON LineString")
    raw_coordinates = decoded.get("coordinates")
    if not isinstance(raw_coordinates, list):
        raise ValueError("Fiber segment LineString coordinates must be a list")
    coordinates: list[tuple[float, float]] = []
    for coordinate in raw_coordinates:
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            raise ValueError(
                "Fiber segment coordinate must be a longitude/latitude pair"
            )
        longitude, latitude = coordinate
        if not isinstance(longitude, (int, float)) or not isinstance(
            latitude, (int, float)
        ):
            raise ValueError("Fiber segment coordinates must be numeric")
        coordinates.append((float(longitude), float(latitude)))
    return NetworkMapLineGeometry(coordinates=tuple(coordinates))


def _plant_segment_rows(db: Session) -> list[tuple[FiberSegment, str | None]]:
    """Load authoritative active route geometry in one PostGIS query."""

    if db.bind is None or db.bind.dialect.name == "sqlite":
        return []
    return (
        db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom))
        .filter(
            FiberSegment.is_active.is_(True),
            FiberSegment.route_geom.isnot(None),
        )
        .all()
    )


def resolve_customer_connectivity(
    snapshots: Sequence[SubscriptionSessionSnapshot],
) -> NetworkMapCustomerConnectivity:
    """Compose exact session observations into one customer marker state."""

    priority = {
        SubscriptionSessionState.inactive: 0,
        SubscriptionSessionState.offline: 1,
        SubscriptionSessionState.stale: 2,
        SubscriptionSessionState.connected: 3,
    }
    if snapshots:
        selected = max(
            snapshots,
            key=lambda snapshot: (
                priority[snapshot.state],
                snapshot.observed_at.timestamp() if snapshot.observed_at else -1.0,
            ),
        )
        state = selected.state
        observed_at = selected.observed_at
        binding = selected.binding
    else:
        state = SubscriptionSessionState.inactive
        observed_at = None
        binding = SubscriptionSessionBinding.none
    presentation = access_session_status_presentation(state.value)
    return NetworkMapCustomerConnectivity(
        state=state,
        layer=(
            NetworkMapCustomerLayer.connected
            if state is SubscriptionSessionState.connected
            else NetworkMapCustomerLayer.not_connected
        ),
        presentation=NetworkMapStatusPresentation.from_contract(presentation),
        observed_at=observed_at,
        binding=binding,
    )


def build_network_map_projection(*, db: Session) -> NetworkMapProjection:
    """Build the typed comprehensive-map projection from named read owners."""

    features: list[NetworkMapFeature] = []

    # POP Sites
    pop_sites = (
        db.query(PopSite)
        .filter(PopSite.is_active.is_(True))
        .filter(PopSite.latitude.isnot(None))
        .filter(PopSite.longitude.isnot(None))
        .all()
    )
    pop_ids = [site.id for site in pop_sites]
    pop_device_counts = {}
    if pop_ids:
        pop_device_counts = {
            row[0]: row[1]
            for row in db.query(NetworkDevice.pop_site_id, func.count(NetworkDevice.id))
            .filter(NetworkDevice.pop_site_id.in_(pop_ids))
            .filter(NetworkDevice.is_active.is_(True))
            .group_by(NetworkDevice.pop_site_id)
            .all()
        }
    for pop in pop_sites:
        features.append(
            NetworkMapFeature(
                geometry=_point(pop.longitude, pop.latitude),
                properties=NetworkMapFeatureProperties(
                    id=pop.id,
                    feature_type=NetworkMapFeatureType.pop_site,
                    name=pop.name,
                    code=pop.code,
                    city=pop.city,
                    device_count=pop_device_counts.get(pop.id, 0),
                    customer_cohort_link=NetworkMapLink(
                        href=(
                            "/admin/customers?infrastructure_type=location"
                            f"&infrastructure_id={pop.id}"
                        ),
                        label="Associated customers",
                        permission=NetworkMapPermission.customer_read,
                    ),
                ),
            )
        )

    # Network Devices (anchored at POP coordinates with slight spread)
    network_devices = (
        db.query(NetworkDevice, PopSite)
        .outerjoin(PopSite, NetworkDevice.pop_site_id == PopSite.id)
        .filter(NetworkDevice.is_active.is_(True))
        .filter(PopSite.latitude.isnot(None))
        .filter(PopSite.longitude.isnot(None))
        .order_by(PopSite.id.asc(), NetworkDevice.name.asc())
        .all()
    )
    annotate_operational_status([device for device, _ in network_devices])
    for idx, (device, pop_site) in enumerate(network_devices):
        # Spread markers around the POP to avoid complete overlap.
        angle = (idx % 12) * (math.pi / 6.0)
        radius = 0.00008 * (1 + (idx % 3))
        latitude = float(pop_site.latitude or 0.0) + (math.sin(angle) * radius)
        longitude = float(pop_site.longitude or 0.0) + (math.cos(angle) * radius)
        features.append(
            NetworkMapFeature(
                geometry=NetworkMapPointGeometry(
                    longitude=longitude,
                    latitude=latitude,
                ),
                properties=NetworkMapFeatureProperties(
                    id=device.id,
                    feature_type=NetworkMapFeatureType.network_device,
                    name=device.name,
                    status=DeviceOperationalState(device.operational_status),
                    status_reason=device.operational_reason,
                    status_presentation=NetworkMapStatusPresentation.from_contract(
                        device.status_presentation
                    ),
                    role=device.role,
                    device_type=device.device_type,
                    vendor=device.vendor,
                    model=device.model,
                    management_ip=device.mgmt_ip,
                    pop_site_name=pop_site.name if pop_site else None,
                ),
            )
        )

    # FDH Cabinets
    fdhs = (
        db.query(FdhCabinet)
        .filter(FdhCabinet.is_active.is_(True))
        .filter(FdhCabinet.latitude.isnot(None))
        .filter(FdhCabinet.longitude.isnot(None))
        .all()
    )
    splitter_counts: dict[UUID | None, int] = {}
    if fdhs:
        fdh_ids = [fdh.id for fdh in fdhs]
        splitter_counts = {
            row[0]: row[1]
            for row in db.query(Splitter.fdh_id, func.count(Splitter.id))
            .filter(Splitter.fdh_id.in_(fdh_ids))
            .group_by(Splitter.fdh_id)
            .all()
        }
    for fdh in fdhs:
        features.append(
            NetworkMapFeature(
                geometry=_point(fdh.longitude, fdh.latitude),
                properties=NetworkMapFeatureProperties(
                    id=fdh.id,
                    feature_type=NetworkMapFeatureType.fdh_cabinet,
                    name=fdh.name,
                    code=fdh.code,
                    splitter_count=splitter_counts.get(fdh.id, 0),
                    customer_cohort_link=NetworkMapLink(
                        href=(
                            "/admin/customers?infrastructure_type=cabinet"
                            f"&infrastructure_id={fdh.id}"
                        ),
                        label="Associated customers",
                        permission=NetworkMapPermission.customer_read,
                    ),
                ),
            )
        )

    # Splice Closures
    closures = (
        db.query(FiberSpliceClosure)
        .filter(FiberSpliceClosure.is_active.is_(True))
        .filter(FiberSpliceClosure.latitude.isnot(None))
        .filter(FiberSpliceClosure.longitude.isnot(None))
        .all()
    )
    for closure in closures:
        features.append(
            NetworkMapFeature(
                geometry=_point(closure.longitude, closure.latitude),
                properties=NetworkMapFeatureProperties(
                    id=closure.id,
                    feature_type=NetworkMapFeatureType.splice_closure,
                    name=closure.name,
                ),
            )
        )

    # Fiber Access Points
    access_points = (
        db.query(FiberAccessPoint)
        .filter(FiberAccessPoint.is_active.is_(True))
        .filter(FiberAccessPoint.latitude.isnot(None))
        .filter(FiberAccessPoint.longitude.isnot(None))
        .all()
    )
    for ap in access_points:
        features.append(
            NetworkMapFeature(
                geometry=_point(ap.longitude, ap.latitude),
                properties=NetworkMapFeatureProperties(
                    id=ap.id,
                    feature_type=NetworkMapFeatureType.access_point,
                    name=ap.name,
                    code=ap.code,
                    access_point_type=ap.access_point_type,
                    placement=ap.placement,
                ),
            )
        )

    # Fiber Support Structures (poles / ducts) with coordinates
    support_structures = (
        db.query(FiberSupportStructure)
        .filter(FiberSupportStructure.latitude.isnot(None))
        .filter(FiberSupportStructure.longitude.isnot(None))
        .all()
    )
    for ss in support_structures:
        features.append(
            NetworkMapFeature(
                geometry=_point(ss.longitude, ss.latitude),
                properties=NetworkMapFeatureProperties(
                    id=ss.id,
                    feature_type=NetworkMapFeatureType.support_structure,
                    name=ss.name,
                    code=ss.code,
                    support_type=NetworkMapSupportType(ss.support_type),
                    lifecycle_status=NetworkMapSupportLifecycle(ss.lifecycle_status),
                    inspection_status=NetworkMapInspectionStatus(ss.inspection_status),
                ),
            )
        )

    # Fiber Segments
    segments = db.query(FiberSegment).filter(FiberSegment.is_active.is_(True)).all()
    segment_geoms: list[tuple[FiberSegment, str | None]] = []
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        segment_geoms = (
            db.query(FiberSegment, func.ST_AsGeoJSON(FiberSegment.route_geom))
            .filter(
                FiberSegment.is_active.is_(True),
                FiberSegment.route_geom.isnot(None),
            )
            .all()
        )
    for segment, geojson_str in segment_geoms:
        if not geojson_str:
            continue
        features.append(
            NetworkMapFeature(
                geometry=_line_geometry(geojson_str),
                properties=NetworkMapFeatureProperties(
                    id=segment.id,
                    feature_type=NetworkMapFeatureType.fiber_segment,
                    name=segment.name,
                    segment_type=segment.segment_type,
                    cable_type=segment.cable_type,
                    fiber_count=segment.fiber_count,
                    length_m=segment.length_m,
                ),
            )
        )

    # ONT Units with GPS coordinates
    ont_units = (
        db.query(OntUnit)
        .filter(
            OntUnit.is_active.is_(True),
            OntUnit.use_gps.is_(True),
            OntUnit.gps_latitude.isnot(None),
            OntUnit.gps_longitude.isnot(None),
        )
        .all()
    )
    ont_working = 0
    ont_not_working = 0
    ont_warning = 0
    warn_threshold, crit_threshold = get_signal_thresholds(db)
    for ont in ont_units:
        operational = derive_ont_operational_status(ont)
        if operational.status == "working":
            ont_working += 1
        else:
            ont_not_working += 1
        if ont.gps_longitude is None or ont.gps_latitude is None:
            continue
        olt_rx_dbm = ont.olt_rx_signal_dbm
        onu_rx_dbm = ont.onu_rx_signal_dbm
        signal_quality = classify_signal(
            olt_rx_dbm,
            warn_threshold=warn_threshold,
            crit_threshold=crit_threshold,
        )
        if signal_quality == "warning":
            ont_warning += 1
        features.append(
            NetworkMapFeature(
                geometry=_point(ont.gps_longitude, ont.gps_latitude),
                properties=NetworkMapFeatureProperties(
                    id=ont.id,
                    feature_type=NetworkMapFeatureType.ont,
                    name=ont.name or ont.serial_number or "ONT",
                    serial_number=ont.serial_number,
                    status=DeviceOperationalState(operational.status),
                    status_reason=operational.reason,
                    status_presentation=NetworkMapStatusPresentation.from_contract(
                        operational.presentation
                    ),
                    signal_quality=NetworkMapSignalQuality(signal_quality),
                    olt_rx_dbm=olt_rx_dbm,
                    onu_rx_dbm=onu_rx_dbm,
                    vendor=ont.vendor,
                    model=ont.model,
                ),
            )
        )

    # Customers with mapped addresses. Session state comes only from the
    # registered network.radius_sessions resolver; this projection does not
    # inspect accounting transport rows or invent freshness semantics.
    map_limit_raw = settings_spec.resolve_value(
        db, SettingDomain.gis, "map_customer_limit"
    )
    try:
        map_limit = int(str(map_limit_raw)) if map_limit_raw is not None else None
    except (TypeError, ValueError):
        map_limit = None
    if map_limit is not None and map_limit <= 0:
        map_limit = None

    mapped_addresses = (
        db.query(Address.id, Address.subscriber_id)
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .filter(
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            Subscriber.is_active.is_(True),
        )
        .order_by(Address.id)
        .all()
    )
    customer_total = len(mapped_addresses)
    mapped_subscriber_ids = {
        row.subscriber_id for row in mapped_addresses if row.subscriber_id is not None
    }
    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id.in_(mapped_subscriber_ids))
        .order_by(Subscription.id)
        .all()
        if mapped_subscriber_ids
        else []
    )
    snapshot_by_subscription = subscription_session_snapshots(db, subscriptions)
    snapshots_by_subscriber: dict[UUID, list[SubscriptionSessionSnapshot]] = {
        subscriber_id: [] for subscriber_id in mapped_subscriber_ids
    }
    for subscription in subscriptions:
        snapshot = snapshot_by_subscription.get(subscription.id)
        if snapshot is not None:
            snapshots_by_subscriber.setdefault(subscription.subscriber_id, []).append(
                snapshot
            )
    connectivity_by_subscriber = {
        subscriber_id: resolve_customer_connectivity(snapshots)
        for subscriber_id, snapshots in snapshots_by_subscriber.items()
    }
    inactive_connectivity = resolve_customer_connectivity(())
    connected_count = sum(
        1
        for row in mapped_addresses
        if connectivity_by_subscriber.get(
            row.subscriber_id,
            inactive_connectivity,
        ).layer
        is NetworkMapCustomerLayer.connected
    )
    not_connected_count = customer_total - connected_count

    customer_addresses_query = (
        db.query(
            Address.id,
            Address.address_line1,
            Address.city,
            Address.latitude,
            Address.longitude,
            Subscriber.id.label("subscriber_id"),
            Subscriber.company_name,
            Subscriber.display_name,
            Subscriber.metadata_["subscriber_category"]
            .as_string()
            .label("subscriber_category"),
            Subscriber.first_name,
            Subscriber.last_name,
        )
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .filter(
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            Subscriber.is_active.is_(True),
        )
        .order_by(Address.id)
    )
    if map_limit is not None:
        customer_addresses_query = customer_addresses_query.limit(map_limit)
    customer_addresses = customer_addresses_query.all()

    for addr in customer_addresses:
        is_business = str(addr.subscriber_category or "").lower() == "business"
        subscriber_name = (
            (
                (addr.company_name or "").strip()
                if is_business
                else f"{addr.first_name or ''} {addr.last_name or ''}".strip()
            )
            or (addr.display_name or "").strip()
            or "Unknown Customer"
        )
        connectivity = connectivity_by_subscriber.get(
            addr.subscriber_id,
            inactive_connectivity,
        )
        features.append(
            NetworkMapFeature(
                geometry=_point(addr.longitude, addr.latitude),
                properties=NetworkMapFeatureProperties(
                    id=addr.id,
                    feature_type=NetworkMapFeatureType.customer,
                    name=subscriber_name,
                    address=addr.address_line1,
                    city=addr.city or "",
                    subscriber_id=addr.subscriber_id,
                    customer_route_kind=(
                        NetworkMapCustomerRouteKind.business
                        if is_business
                        else NetworkMapCustomerRouteKind.person
                    ),
                    connectivity=connectivity,
                    customer_detail_link=NetworkMapLink(
                        href=(
                            "/admin/customers/business/"
                            if is_business
                            else "/admin/customers/person/"
                        )
                        + str(addr.subscriber_id),
                        label="View customer and network path",
                        permission=NetworkMapPermission.customer_read,
                    ),
                ),
            )
        )

    stats = NetworkMapStats(
        pop_sites=db.query(func.count(PopSite.id))
        .filter(PopSite.is_active.is_(True))
        .scalar()
        or 0,
        fdh_cabinets=db.query(func.count(FdhCabinet.id))
        .filter(FdhCabinet.is_active.is_(True))
        .scalar()
        or 0,
        splice_closures=db.query(func.count(FiberSpliceClosure.id))
        .filter(FiberSpliceClosure.is_active.is_(True))
        .scalar()
        or 0,
        access_points=db.query(func.count(FiberAccessPoint.id))
        .filter(FiberAccessPoint.is_active.is_(True))
        .scalar()
        or 0,
        support_structures=db.query(func.count(FiberSupportStructure.id)).scalar() or 0,
        fiber_segments=len(segments),
        customers=customer_total,
        customers_connected=connected_count,
        customers_not_connected=not_connected_count,
        network_devices=len(network_devices),
        network_devices_working=sum(
            1 for device, _ in network_devices if device.operational_status == "working"
        ),
        network_devices_not_working=sum(
            1
            for device, _ in network_devices
            if device.operational_status == "not_working"
        ),
        onts=len(ont_units),
        onts_working=ont_working,
        onts_not_working=ont_not_working,
        onts_warning=ont_warning,
    )

    return NetworkMapProjection(
        features=tuple(features),
        stats=stats,
        customer_count=customer_total,
        customer_map_count=len(customer_addresses),
    )


def build_network_map_plant_projection(*, db: Session) -> NetworkMapPlantProjection:
    """Return dispatch-visible plant only, without customer, ONT, or session reads.

    This is intentionally a separate query boundary: it does not call the
    comprehensive projection because that projection resolves customer session
    observations. OLTs inherit their marker position only through the approved
    matched NetworkDevice -> PopSite inventory relationship.
    """
    features: list[NetworkMapFeature] = []
    counts = dict.fromkeys(NetworkMapPlantLayer, 0)
    sites = (
        db.query(PopSite)
        .filter(
            PopSite.is_active.is_(True),
            PopSite.latitude.isnot(None),
            PopSite.longitude.isnot(None),
        )
        .all()
    )
    for site in sites:
        features.append(
            NetworkMapFeature(
                geometry=_point(site.longitude, site.latitude),
                properties=NetworkMapFeatureProperties(
                    id=site.id,
                    feature_type=NetworkMapFeatureType.pop_site,
                    name=site.name,
                    code=site.code,
                    city=site.city,
                    notes=site.notes,
                ),
            )
        )
        counts[NetworkMapPlantLayer.sites] += 1

    devices = (
        db.query(NetworkDevice, PopSite)
        .join(PopSite, NetworkDevice.pop_site_id == PopSite.id)
        .filter(
            NetworkDevice.is_active.is_(True),
            PopSite.is_active.is_(True),
            PopSite.latitude.isnot(None),
            PopSite.longitude.isnot(None),
        )
        .order_by(PopSite.id, NetworkDevice.name)
        .all()
    )
    annotate_operational_status([device for device, _ in devices])
    for index, (device, site) in enumerate(devices):
        angle = (index % 12) * (math.pi / 6.0)
        radius = 0.00008 * (1 + (index % 3))
        features.append(
            NetworkMapFeature(
                geometry=NetworkMapPointGeometry(
                    longitude=float(site.longitude) + math.cos(angle) * radius,
                    latitude=float(site.latitude) + math.sin(angle) * radius,
                ),
                properties=NetworkMapFeatureProperties(
                    id=device.id,
                    feature_type=NetworkMapFeatureType.network_device,
                    name=device.name,
                    status=DeviceOperationalState(device.operational_status),
                    status_reason=device.operational_reason,
                    status_presentation=NetworkMapStatusPresentation.from_contract(
                        device.status_presentation
                    ),
                    role=device.role,
                    device_type=device.device_type,
                    vendor=device.vendor,
                    model=device.model,
                    management_ip=device.mgmt_ip,
                    pop_site_name=site.name,
                ),
            )
        )
        counts[NetworkMapPlantLayer.sites] += 1

    matched_olt_ids = {
        node.matched_device_id
        for node, _ in devices
        if node.matched_device_type == "olt" and node.matched_device_id is not None
    }
    olts_by_id = {
        olt.id: olt
        for olt in (
            db.query(OLTDevice)
            .filter(
                OLTDevice.id.in_(matched_olt_ids),
                OLTDevice.is_active.is_(True),
            )
            .all()
        )
    }
    mapped_olt_ids: set[UUID] = set()
    for index, (node, site) in enumerate(devices):
        if node.matched_device_type != "olt" or node.matched_device_id is None:
            continue
        olt = olts_by_id.get(node.matched_device_id)
        if olt is None:
            continue
        mapped_olt_ids.add(olt.id)
        angle = (index % 12) * (math.pi / 6.0)
        features.append(
            NetworkMapFeature(
                geometry=NetworkMapPointGeometry(
                    longitude=float(site.longitude) + math.cos(angle) * 0.00026,
                    latitude=float(site.latitude) + math.sin(angle) * 0.00026,
                ),
                properties=NetworkMapFeatureProperties(
                    id=olt.id,
                    feature_type=NetworkMapFeatureType.olt_device,
                    name=olt.name,
                    status=DeviceOperationalState(node.operational_status),
                    status_reason=node.operational_reason,
                    status_presentation=NetworkMapStatusPresentation.from_contract(
                        node.status_presentation
                    ),
                    vendor=olt.vendor,
                    model=olt.model,
                    management_ip=olt.mgmt_ip,
                    pop_site_name=site.name,
                    notes=olt.notes,
                ),
            )
        )
        counts[NetworkMapPlantLayer.sites] += 1
    unmatched_olt_query = db.query(func.count(OLTDevice.id)).filter(
        OLTDevice.is_active.is_(True)
    )
    if mapped_olt_ids:
        unmatched_olt_query = unmatched_olt_query.filter(
            ~OLTDevice.id.in_(mapped_olt_ids)
        )
    unmatched_olt_count = int(unmatched_olt_query.scalar() or 0)

    fdhs = (
        db.query(FdhCabinet)
        .filter(
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.isnot(None),
            FdhCabinet.longitude.isnot(None),
        )
        .all()
    )
    fdh_ids = [fdh.id for fdh in fdhs]
    splitter_counts = (
        {
            fdh_id: count
            for fdh_id, count in (
                db.query(Splitter.fdh_id, func.count(Splitter.id))
                .filter(Splitter.fdh_id.in_(fdh_ids))
                .group_by(Splitter.fdh_id)
                .all()
            )
        }
        if fdh_ids
        else {}
    )
    for fdh in fdhs:
        features.append(
            NetworkMapFeature(
                geometry=_point(fdh.longitude, fdh.latitude),
                properties=NetworkMapFeatureProperties(
                    id=fdh.id,
                    feature_type=NetworkMapFeatureType.fdh_cabinet,
                    name=fdh.name,
                    code=fdh.code,
                    splitter_count=splitter_counts.get(fdh.id, 0),
                    notes=fdh.notes,
                ),
            )
        )
        counts[NetworkMapPlantLayer.osp] += 1

    closures = (
        db.query(FiberSpliceClosure)
        .filter(
            FiberSpliceClosure.is_active.is_(True),
            FiberSpliceClosure.latitude.isnot(None),
            FiberSpliceClosure.longitude.isnot(None),
        )
        .all()
    )
    closure_ids = [closure.id for closure in closures]
    splice_counts = (
        {
            closure_id: count
            for closure_id, count in (
                db.query(FiberSplice.closure_id, func.count(FiberSplice.id))
                .filter(FiberSplice.closure_id.in_(closure_ids))
                .group_by(FiberSplice.closure_id)
                .all()
            )
        }
        if closure_ids
        else {}
    )
    tray_counts = (
        {
            closure_id: count
            for closure_id, count in (
                db.query(FiberSpliceTray.closure_id, func.count(FiberSpliceTray.id))
                .filter(FiberSpliceTray.closure_id.in_(closure_ids))
                .group_by(FiberSpliceTray.closure_id)
                .all()
            )
        }
        if closure_ids
        else {}
    )
    for closure in closures:
        features.append(
            NetworkMapFeature(
                geometry=_point(closure.longitude, closure.latitude),
                properties=NetworkMapFeatureProperties(
                    id=closure.id,
                    feature_type=NetworkMapFeatureType.splice_closure,
                    name=closure.name,
                    splice_count=splice_counts.get(closure.id, 0),
                    tray_count=tray_counts.get(closure.id, 0),
                    notes=closure.notes,
                ),
            )
        )
        counts[NetworkMapPlantLayer.osp] += 1

    access_points = (
        db.query(FiberAccessPoint)
        .filter(
            FiberAccessPoint.is_active.is_(True),
            FiberAccessPoint.latitude.isnot(None),
            FiberAccessPoint.longitude.isnot(None),
        )
        .all()
    )
    for access_point in access_points:
        features.append(
            NetworkMapFeature(
                geometry=_point(access_point.longitude, access_point.latitude),
                properties=NetworkMapFeatureProperties(
                    id=access_point.id,
                    feature_type=NetworkMapFeatureType.access_point,
                    name=access_point.name,
                    code=access_point.code,
                    access_point_type=access_point.access_point_type,
                    placement=access_point.placement,
                ),
            )
        )
        counts[NetworkMapPlantLayer.customer_edge] += 1

    buildings = (
        db.query(ServiceBuilding)
        .filter(
            ServiceBuilding.is_active.is_(True),
            ServiceBuilding.latitude.isnot(None),
            ServiceBuilding.longitude.isnot(None),
        )
        .all()
    )
    for building in buildings:
        features.append(
            NetworkMapFeature(
                geometry=_point(building.longitude, building.latitude),
                properties=NetworkMapFeatureProperties(
                    id=building.id,
                    feature_type=NetworkMapFeatureType.service_building,
                    name=building.name,
                    code=building.code,
                    street=building.street,
                    city=building.city,
                    notes=building.notes,
                ),
            )
        )
        counts[NetworkMapPlantLayer.customer_edge] += 1
    for segment, geometry in _plant_segment_rows(db):
        if geometry and segment.segment_type in {
            FiberSegmentType.feeder,
            FiberSegmentType.distribution,
            FiberSegmentType.drop,
        }:
            features.append(
                NetworkMapFeature(
                    geometry=_line_geometry(geometry),
                    properties=NetworkMapFeatureProperties(
                        id=segment.id,
                        feature_type=NetworkMapFeatureType.fiber_segment,
                        name=segment.name,
                        segment_type=segment.segment_type,
                        cable_type=segment.cable_type,
                        fiber_count=segment.fiber_count,
                        length_m=segment.length_m,
                        notes=segment.notes,
                    ),
                )
            )
            counts[
                NetworkMapPlantLayer.backbone
                if segment.segment_type is FiberSegmentType.feeder
                else NetworkMapPlantLayer.osp
            ] += 1
    return NetworkMapPlantProjection(
        features=tuple(features),
        layer_counts=counts,
        unmatched_olt_count=unmatched_olt_count,
    )


def _v2_layer_for_feature(feature: NetworkMapFeature) -> NetworkMapV2Layer | None:
    properties = feature.properties
    feature_type = properties.feature_type
    point_layers = {
        NetworkMapFeatureType.pop_site: NetworkMapV2Layer.pop,
        NetworkMapFeatureType.fdh_cabinet: NetworkMapV2Layer.fdh,
        NetworkMapFeatureType.splice_closure: NetworkMapV2Layer.closures,
        NetworkMapFeatureType.access_point: NetworkMapV2Layer.access_points,
        NetworkMapFeatureType.support_structure: NetworkMapV2Layer.support_structures,
        NetworkMapFeatureType.network_device: NetworkMapV2Layer.network_devices,
        NetworkMapFeatureType.ont: NetworkMapV2Layer.onts,
        NetworkMapFeatureType.olt_device: NetworkMapV2Layer.olt,
        NetworkMapFeatureType.service_building: NetworkMapV2Layer.service_buildings,
    }
    if feature_type is NetworkMapFeatureType.customer:
        if (
            properties.connectivity
            and properties.connectivity.layer is NetworkMapCustomerLayer.connected
        ):
            return NetworkMapV2Layer.customers_connected
        return NetworkMapV2Layer.customers_not_connected
    if feature_type is NetworkMapFeatureType.fiber_segment:
        if properties.segment_type is None:
            return None
        return {
            FiberSegmentType.feeder: NetworkMapV2Layer.feeder,
            FiberSegmentType.distribution: NetworkMapV2Layer.distribution,
            FiberSegmentType.drop: NetworkMapV2Layer.drop,
        }.get(properties.segment_type)
    return point_layers.get(feature_type)


def _v2_endpoint(
    endpoint: FiberTerminationPoint | None,
    *,
    attached_segment_counts: Counter[UUID],
) -> NetworkMapV2Endpoint:
    if endpoint is None:
        return NetworkMapV2Endpoint(
            id=None,
            name=None,
            endpoint_type=None,
            reference_id=None,
            longitude=None,
            latitude=None,
            attached_segment_count=0,
        )
    endpoint_type = endpoint.endpoint_type
    return NetworkMapV2Endpoint(
        id=endpoint.id,
        name=endpoint.name,
        endpoint_type=(endpoint_type.value if endpoint_type is not None else None),
        reference_id=endpoint.ref_id,
        longitude=(
            float(endpoint.longitude) if endpoint.longitude is not None else None
        ),
        latitude=(float(endpoint.latitude) if endpoint.latitude is not None else None),
        attached_segment_count=attached_segment_counts[endpoint.id],
    )


def _v2_segment_topology(
    *,
    segments: Sequence[FiberSegment],
    rendered_route_ids: set[UUID],
    geometry_available: bool,
) -> tuple[NetworkMapV2SegmentTopology, ...]:
    """Project only explicit segment/termination relationships.

    Coordinate proximity is deliberately absent from this resolver. Two endpoint
    markers can occupy the same pixels and remain disconnected unless they share
    a canonical termination point or reference an authoritative endpoint owner.
    """

    attached_segment_counts: Counter[UUID] = Counter()
    for segment in segments:
        if segment.from_point_id is not None:
            attached_segment_counts[segment.from_point_id] += 1
        if segment.to_point_id is not None:
            attached_segment_counts[segment.to_point_id] += 1

    topology: list[NetworkMapV2SegmentTopology] = []
    for segment in segments:
        from_endpoint = _v2_endpoint(
            segment.from_point,
            attached_segment_counts=attached_segment_counts,
        )
        to_endpoint = _v2_endpoint(
            segment.to_point,
            attached_segment_counts=attached_segment_counts,
        )
        if segment.route_geom is None:
            geometry_status = NetworkMapV2GeometryStatus.missing
        elif segment.id in rendered_route_ids:
            geometry_status = NetworkMapV2GeometryStatus.stored_valid
        elif not geometry_available:
            geometry_status = NetworkMapV2GeometryStatus.unavailable
        else:
            geometry_status = NetworkMapV2GeometryStatus.invalid

        if (
            from_endpoint.id is None
            or to_endpoint.id is None
            or geometry_status is not NetworkMapV2GeometryStatus.stored_valid
        ):
            topology_status = NetworkMapV2TopologyStatus.incomplete
        elif (
            from_endpoint.has_explicit_connection
            and to_endpoint.has_explicit_connection
        ):
            topology_status = NetworkMapV2TopologyStatus.connected
        else:
            topology_status = NetworkMapV2TopologyStatus.disconnected

        topology.append(
            NetworkMapV2SegmentTopology(
                id=segment.id,
                name=segment.name,
                segment_type=segment.segment_type,
                geometry_status=geometry_status,
                topology_status=topology_status,
                from_endpoint=from_endpoint,
                to_endpoint=to_endpoint,
            )
        )
    return tuple(topology)


def build_network_map_v2_projection(
    *, db: Session, base_projection: NetworkMapProjection
) -> NetworkMapV2Projection:
    """Compose the isolated V2 parity overlay from authoritative map owners."""

    plant_projection = build_network_map_plant_projection(db=db)
    additional_features = tuple(
        feature
        for feature in plant_projection.features
        if feature.properties.feature_type
        in {
            NetworkMapFeatureType.olt_device,
            NetworkMapFeatureType.service_building,
        }
    )

    all_counted_features = (*base_projection.features, *additional_features)
    counts = dict.fromkeys(NetworkMapV2Layer, 0)
    seen_features: set[tuple[NetworkMapFeatureType, UUID]] = set()
    for feature in all_counted_features:
        identity = (feature.properties.feature_type, feature.properties.id)
        if identity in seen_features:
            continue
        seen_features.add(identity)
        layer = _v2_layer_for_feature(feature)
        if layer is not None:
            counts[layer] += 1

    segments = (
        db.query(FiberSegment)
        .options(
            selectinload(FiberSegment.from_point),
            selectinload(FiberSegment.to_point),
        )
        .filter(FiberSegment.is_active.is_(True))
        .all()
    )
    rendered_route_ids = {
        feature.properties.id
        for feature in plant_projection.features
        if feature.properties.feature_type is NetworkMapFeatureType.fiber_segment
    }
    geometry_available = db.bind is not None and db.bind.dialect.name != "sqlite"
    segment_topology = _v2_segment_topology(
        segments=segments,
        rendered_route_ids=rendered_route_ids,
        geometry_available=geometry_available,
    )
    endpoint_ids_with_coordinates = {
        endpoint.id
        for segment in segment_topology
        for endpoint in (segment.from_endpoint, segment.to_endpoint)
        if endpoint.id is not None
        and endpoint.longitude is not None
        and endpoint.latitude is not None
    }
    counts[NetworkMapV2Layer.topology_endpoints] = len(endpoint_ids_with_coordinates)

    return NetworkMapV2Projection(
        additional_features=additional_features,
        layer_counts=counts,
        segment_topology=segment_topology,
        unavailable_layers=(
            NetworkMapV2UnavailableLayer(
                layer=NetworkMapV2Layer.base_stations,
                reason=(
                    "No authoritative Selfcare base-station projection is exposed "
                    "to the Network Map."
                ),
            ),
            NetworkMapV2UnavailableLayer(
                layer=NetworkMapV2Layer.live_technicians,
                reason=(
                    "Live technician presence has a separate owner and permission "
                    "contract; it is not part of network:map:read."
                ),
            ),
        ),
        unmatched_olt_count=plant_projection.unmatched_olt_count,
    )
