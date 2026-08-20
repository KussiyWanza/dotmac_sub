"""Typed transport-neutral contracts for the comprehensive network map."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias
from uuid import UUID

from app.models.network import FiberCableType, FiberSegmentType
from app.models.network_monitoring import DeviceRole, DeviceType
from app.schemas.status_presentation import StatusIcon, StatusPresentation, StatusTone
from app.services.device_operational_status import DeviceOperationalState
from app.services.network.radius_sessions import (
    SubscriptionSessionBinding,
    SubscriptionSessionState,
)


class NetworkMapFeatureType(StrEnum):
    pop_site = "pop_site"
    network_device = "network_device"
    fdh_cabinet = "fdh_cabinet"
    splice_closure = "splice_closure"
    access_point = "access_point"
    support_structure = "support_structure"
    fiber_segment = "fiber_segment"
    ont = "ont"
    customer = "customer"
    olt_device = "olt_device"
    service_building = "service_building"


class NetworkMapCustomerRouteKind(StrEnum):
    person = "person"
    business = "business"


class NetworkMapCustomerLayer(StrEnum):
    connected = "connected"
    not_connected = "not_connected"


class NetworkMapSignalQuality(StrEnum):
    good = "good"
    warning = "warning"
    critical = "critical"
    unknown = "unknown"


class NetworkMapStatusOwner(StrEnum):
    radius_sessions = "network.radius_sessions"


class NetworkMapPermission(StrEnum):
    customer_read = "customer:read"


class NetworkMapSupportType(StrEnum):
    pole = "pole"
    tower = "tower"
    building_attachment = "building_attachment"
    other = "other"


class NetworkMapSupportLifecycle(StrEnum):
    planned = "planned"
    active = "active"
    suspended = "suspended"
    retired = "retired"


class NetworkMapInspectionStatus(StrEnum):
    uninspected = "uninspected"
    due = "due"
    passed = "passed"
    conditional = "conditional"
    failed = "failed"


@dataclass(frozen=True, slots=True)
class NetworkMapStatusPresentation:
    value: str
    label: str
    tone: StatusTone
    icon: StatusIcon

    @classmethod
    def from_contract(
        cls, presentation: StatusPresentation
    ) -> NetworkMapStatusPresentation:
        return cls(
            value=presentation.value,
            label=presentation.label,
            tone=presentation.tone,
            icon=presentation.icon,
        )

    def to_transport(self) -> dict[str, object]:
        return {
            "value": self.value,
            "label": self.label,
            "tone": self.tone.value,
            "icon": self.icon.value,
        }


@dataclass(frozen=True, slots=True)
class NetworkMapLink:
    href: str
    label: str
    permission: NetworkMapPermission

    def to_transport(self) -> dict[str, object]:
        return {
            "href": self.href,
            "label": self.label,
            "permission": self.permission.value,
        }


@dataclass(frozen=True, slots=True)
class NetworkMapCustomerConnectivity:
    state: SubscriptionSessionState
    layer: NetworkMapCustomerLayer
    presentation: NetworkMapStatusPresentation
    observed_at: datetime | None
    binding: SubscriptionSessionBinding
    source_owner: NetworkMapStatusOwner = NetworkMapStatusOwner.radius_sessions

    def to_transport(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "layer": self.layer.value,
            "presentation": self.presentation.to_transport(),
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "binding": self.binding.value,
            "source_owner": self.source_owner.value,
        }


@dataclass(frozen=True, slots=True)
class NetworkMapPointGeometry:
    longitude: float
    latitude: float

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "Point",
            "coordinates": [self.longitude, self.latitude],
        }


@dataclass(frozen=True, slots=True)
class NetworkMapLineGeometry:
    coordinates: tuple[tuple[float, float], ...]

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "LineString",
            "coordinates": [list(coordinate) for coordinate in self.coordinates],
        }


NetworkMapGeometry: TypeAlias = NetworkMapPointGeometry | NetworkMapLineGeometry


@dataclass(frozen=True, slots=True)
class NetworkMapFeatureProperties:
    id: UUID
    feature_type: NetworkMapFeatureType
    name: str
    code: str | None = None
    city: str | None = None
    street: str | None = None
    notes: str | None = None
    device_count: int | None = None
    splitter_count: int | None = None
    splice_count: int | None = None
    tray_count: int | None = None
    access_point_type: str | None = None
    placement: str | None = None
    support_type: NetworkMapSupportType | None = None
    lifecycle_status: NetworkMapSupportLifecycle | None = None
    inspection_status: NetworkMapInspectionStatus | None = None
    status: DeviceOperationalState | None = None
    status_reason: str | None = None
    status_presentation: NetworkMapStatusPresentation | None = None
    role: DeviceRole | None = None
    device_type: DeviceType | None = None
    vendor: str | None = None
    model: str | None = None
    management_ip: str | None = None
    pop_site_name: str | None = None
    segment_type: FiberSegmentType | None = None
    cable_type: FiberCableType | None = None
    fiber_count: int | None = None
    length_m: float | None = None
    serial_number: str | None = None
    signal_quality: NetworkMapSignalQuality | None = None
    olt_rx_dbm: float | None = None
    onu_rx_dbm: float | None = None
    address: str | None = None
    subscriber_id: UUID | None = None
    customer_route_kind: NetworkMapCustomerRouteKind | None = None
    connectivity: NetworkMapCustomerConnectivity | None = None
    customer_detail_link: NetworkMapLink | None = None
    customer_cohort_link: NetworkMapLink | None = None

    def to_transport(self) -> dict[str, object]:
        values: dict[str, object | None] = {
            "id": str(self.id),
            "type": self.feature_type.value,
            "name": self.name,
            "code": self.code,
            "city": self.city,
            "street": self.street,
            "notes": self.notes,
            "device_count": self.device_count,
            "splitter_count": self.splitter_count,
            "splice_count": self.splice_count,
            "tray_count": self.tray_count,
            "ap_type": self.access_point_type,
            "placement": self.placement,
            "support_type": self.support_type.value if self.support_type else None,
            "lifecycle_status": (
                self.lifecycle_status.value if self.lifecycle_status else None
            ),
            "inspection_status": (
                self.inspection_status.value if self.inspection_status else None
            ),
            "status": self.status.value if self.status else None,
            "status_reason": self.status_reason,
            "status_presentation": (
                self.status_presentation.to_transport()
                if self.status_presentation
                else None
            ),
            "role": self.role.value if self.role else None,
            "device_type": self.device_type.value if self.device_type else None,
            "vendor": self.vendor,
            "model": self.model,
            "mgmt_ip": self.management_ip,
            "pop_site_name": self.pop_site_name,
            "segment_type": self.segment_type.value if self.segment_type else None,
            "cable_type": self.cable_type.value if self.cable_type else None,
            "fiber_count": self.fiber_count,
            "length_m": self.length_m,
            "serial_number": self.serial_number,
            "signal_quality": self.signal_quality.value
            if self.signal_quality
            else None,
            "olt_rx_dbm": self.olt_rx_dbm,
            "onu_rx_dbm": self.onu_rx_dbm,
            "address": self.address,
            "subscriber_id": str(self.subscriber_id) if self.subscriber_id else None,
            "customer_type": (
                self.customer_route_kind.value if self.customer_route_kind else None
            ),
            "connectivity": (
                self.connectivity.to_transport() if self.connectivity else None
            ),
            "customer_detail_link": (
                self.customer_detail_link.to_transport()
                if self.customer_detail_link
                else None
            ),
            "customer_cohort_link": (
                self.customer_cohort_link.to_transport()
                if self.customer_cohort_link
                else None
            ),
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class NetworkMapFeature:
    geometry: NetworkMapGeometry
    properties: NetworkMapFeatureProperties

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "Feature",
            "geometry": self.geometry.to_transport(),
            "properties": self.properties.to_transport(),
        }


@dataclass(frozen=True, slots=True)
class NetworkMapStats:
    pop_sites: int
    fdh_cabinets: int
    splice_closures: int
    access_points: int
    support_structures: int
    fiber_segments: int
    customers: int
    customers_connected: int
    customers_not_connected: int
    network_devices: int
    network_devices_working: int
    network_devices_not_working: int
    onts: int
    onts_working: int
    onts_not_working: int
    onts_warning: int

    def to_transport(self) -> dict[str, int]:
        return {
            "pop_sites": self.pop_sites,
            "fdh_cabinets": self.fdh_cabinets,
            "splice_closures": self.splice_closures,
            "access_points": self.access_points,
            "support_structures": self.support_structures,
            "fiber_segments": self.fiber_segments,
            "customers": self.customers,
            "customers_connected": self.customers_connected,
            "customers_not_connected": self.customers_not_connected,
            "network_devices": self.network_devices,
            "network_devices_working": self.network_devices_working,
            "network_devices_not_working": self.network_devices_not_working,
            "onts": self.onts,
            "onts_working": self.onts_working,
            "onts_not_working": self.onts_not_working,
            "onts_warning": self.onts_warning,
        }


@dataclass(frozen=True, slots=True)
class NetworkMapProjection:
    features: tuple[NetworkMapFeature, ...]
    stats: NetworkMapStats
    customer_count: int
    customer_map_count: int

    def to_template_context(self) -> dict[str, object]:
        return {
            "map_data": {
                "type": "FeatureCollection",
                "features": [feature.to_transport() for feature in self.features],
            },
            "stats": self.stats.to_transport(),
            "customer_count": self.customer_count,
            "customer_map_count": self.customer_map_count,
        }


class NetworkMapPlantLayer(StrEnum):
    osp = "osp"
    backbone = "backbone"
    customer_edge = "customer_edge"
    sites = "sites"


class NetworkMapV2Layer(StrEnum):
    pop = "pop"
    fdh = "fdh"
    closures = "closures"
    access_points = "access_points"
    support_structures = "support_structures"
    network_devices = "network_devices"
    onts = "onts"
    customers_connected = "customers_connected"
    customers_not_connected = "customers_not_connected"
    olt = "olt"
    base_stations = "base_stations"
    service_buildings = "service_buildings"
    feeder = "feeder"
    distribution = "distribution"
    drop = "drop"
    topology_endpoints = "topology_endpoints"
    live_technicians = "live_technicians"


class NetworkMapV2GeometryStatus(StrEnum):
    stored_valid = "stored_valid"
    missing = "missing"
    invalid = "invalid"
    unavailable = "unavailable"


class NetworkMapV2TopologyStatus(StrEnum):
    connected = "connected"
    disconnected = "disconnected"
    incomplete = "incomplete"


@dataclass(frozen=True, slots=True)
class NetworkMapV2Endpoint:
    id: UUID | None
    name: str | None
    endpoint_type: str | None
    reference_id: UUID | None
    longitude: float | None
    latitude: float | None
    attached_segment_count: int

    @property
    def has_explicit_connection(self) -> bool:
        return self.reference_id is not None or self.attached_segment_count > 1

    def to_transport(self) -> dict[str, object]:
        return {
            "id": str(self.id) if self.id else None,
            "name": self.name,
            "endpoint_type": self.endpoint_type,
            "reference_id": str(self.reference_id) if self.reference_id else None,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "attached_segment_count": self.attached_segment_count,
            "has_explicit_connection": self.has_explicit_connection,
        }


@dataclass(frozen=True, slots=True)
class NetworkMapV2SegmentTopology:
    id: UUID
    name: str
    segment_type: FiberSegmentType
    geometry_status: NetworkMapV2GeometryStatus
    topology_status: NetworkMapV2TopologyStatus
    from_endpoint: NetworkMapV2Endpoint
    to_endpoint: NetworkMapV2Endpoint

    def to_transport(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "name": self.name,
            "segment_type": self.segment_type.value,
            "geometry_status": self.geometry_status.value,
            "topology_status": self.topology_status.value,
            "from_endpoint": self.from_endpoint.to_transport(),
            "to_endpoint": self.to_endpoint.to_transport(),
        }


@dataclass(frozen=True, slots=True)
class NetworkMapV2UnavailableLayer:
    layer: NetworkMapV2Layer
    reason: str

    def to_transport(self) -> dict[str, str]:
        return {"layer": self.layer.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class NetworkMapV2Projection:
    additional_features: tuple[NetworkMapFeature, ...]
    layer_counts: dict[NetworkMapV2Layer, int]
    segment_topology: tuple[NetworkMapV2SegmentTopology, ...]
    unavailable_layers: tuple[NetworkMapV2UnavailableLayer, ...]
    unmatched_olt_count: int

    def to_transport(self) -> dict[str, object]:
        return {
            "additional_features": [
                feature.to_transport() for feature in self.additional_features
            ],
            "layer_counts": {
                layer.value: count for layer, count in self.layer_counts.items()
            },
            "segment_topology": [
                segment.to_transport() for segment in self.segment_topology
            ],
            "unavailable_layers": [
                layer.to_transport() for layer in self.unavailable_layers
            ],
            "unmatched_olt_count": self.unmatched_olt_count,
        }


@dataclass(frozen=True, slots=True)
class NetworkMapPlantProjection:
    """Read-only dispatch plant subset; it deliberately has no customer state."""

    features: tuple[NetworkMapFeature, ...]
    layer_counts: dict[NetworkMapPlantLayer, int]
    unmatched_olt_count: int

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "FeatureCollection",
            "features": [feature.to_transport() for feature in self.features],
            "counts": {
                **{layer.value: count for layer, count in self.layer_counts.items()},
                "unmatched_olts": self.unmatched_olt_count,
            },
        }


class VendorRoutePlanningAssetType(StrEnum):
    fdh_cabinet = "fdh_cabinet"
    splice_closure = "splice_closure"
    fiber_access_point = "fiber_access_point"
    service_building = "service_building"
    fiber_segment = "fiber_segment"


@dataclass(frozen=True, slots=True)
class VendorRoutePlanningFeatureProperties:
    id: UUID
    asset_type: VendorRoutePlanningAssetType
    name: str
    segment_type: FiberSegmentType | None = None
    source_owner: str = "ui.network_map_projection"

    def to_transport(self) -> dict[str, object]:
        values: dict[str, object | None] = {
            "id": str(self.id),
            "type": self.asset_type.value,
            "name": self.name,
            "segment_type": self.segment_type.value if self.segment_type else None,
            "source_owner": self.source_owner,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class VendorRoutePlanningFeature:
    geometry: NetworkMapGeometry
    properties: VendorRoutePlanningFeatureProperties

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "Feature",
            "geometry": self.geometry.to_transport(),
            "properties": self.properties.to_transport(),
        }


@dataclass(frozen=True, slots=True)
class VendorRoutePlanningMapProjection:
    """Vendor-safe canonical plant context for proposed-route planning."""

    features: tuple[VendorRoutePlanningFeature, ...]

    def to_transport(self) -> dict[str, object]:
        return {
            "type": "FeatureCollection",
            "features": [feature.to_transport() for feature in self.features],
        }
