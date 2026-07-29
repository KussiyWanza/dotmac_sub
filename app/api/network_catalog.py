"""Network catalog API — ONU types, speed profiles, zones, vendor capabilities."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import (
    GponChannel,
    OnuCapability,
    PonType,
    SpeedProfileDirection,
    SpeedProfileType,
)
from app.schemas.network_catalog import (
    NetworkZoneCreate,
    NetworkZoneRead,
    NetworkZoneUpdate,
    OnuTypeCreate,
    OnuTypeRead,
    OnuTypeUpdate,
    SpeedProfileCreate,
    SpeedProfileRead,
    SpeedProfileUpdate,
    Tr069ParameterMapCreate,
    Tr069ParameterMapRead,
    Tr069ParameterMapUpdate,
    VendorCapabilityCreate,
    VendorCapabilityRead,
    VendorCapabilityUpdate,
)
from app.services.auth_dependencies import require_permission
from app.services.network.onu_types import onu_types
from app.services.network.speed_profiles import SpeedProfiles, format_speed
from app.services.network.vendor_capabilities import (
    Tr069ParameterMaps,
    VendorCapabilities,
)
from app.services.network.zones import NetworkZones

logger = logging.getLogger(__name__)

router = APIRouter(tags=["network-catalog"])


def _enum_or_422(enum_cls, value, field: str):
    """Coerce a string to an enum, returning HTTP 422 (not 500) on a bad value."""
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid {field}: {value!r}",
        ) from exc


# ── ONU Types ──────────────────────────────────────────────────────────


@router.get(
    "/onu-types",
    response_model=list[OnuTypeRead],
    dependencies=[Depends(require_permission("network:onu_type:read"))],
)
def list_onu_types(
    pon_type: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    order_by: str = "name",
    order_dir: str = "asc",
    limit: int = Query(default=200, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[OnuTypeRead]:
    items = onu_types.list(
        db,
        pon_type=pon_type,
        is_active=is_active,
        search=search,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    return [OnuTypeRead.model_validate(i) for i in items]


@router.post(
    "/onu-types",
    response_model=OnuTypeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:onu_type:write"))],
)
def create_onu_type(
    payload: OnuTypeCreate, db: Session = Depends(get_db)
) -> OnuTypeRead:
    item = onu_types.create(
        db,
        name=payload.name,
        pon_type=_enum_or_422(PonType, payload.pon_type, "pon_type"),
        gpon_channel=_enum_or_422(GponChannel, payload.gpon_channel, "gpon_channel"),
        ethernet_ports=payload.ethernet_ports,
        wifi_ports=payload.wifi_ports,
        voip_ports=payload.voip_ports,
        catv_ports=payload.catv_ports,
        allow_custom_profiles=payload.allow_custom_profiles,
        capability=OnuCapability(payload.capability),
        notes=payload.notes,
        adapter_name=payload.adapter_name,
        tr069_data_model=payload.tr069_data_model,
        config_method_preference=payload.config_method_preference,
        wifi_ssid_path=payload.wifi_ssid_path,
        wifi_password_path=payload.wifi_password_path,
        wifi_enabled_path=payload.wifi_enabled_path,
        wifi_channel_path=payload.wifi_channel_path,
        wifi_security_mode_path=payload.wifi_security_mode_path,
        wan_pppoe_username_path=payload.wan_pppoe_username_path,
        wan_pppoe_password_path=payload.wan_pppoe_password_path,
        wan_connection_type_path=payload.wan_connection_type_path,
        lan_ip_address_path=payload.lan_ip_address_path,
        lan_subnet_mask_path=payload.lan_subnet_mask_path,
        lan_dhcp_enabled_path=payload.lan_dhcp_enabled_path,
        lan_dhcp_start_path=payload.lan_dhcp_start_path,
        lan_dhcp_end_path=payload.lan_dhcp_end_path,
        remote_access_enabled_path=payload.remote_access_enabled_path,
        http_management_enabled_path=payload.http_management_enabled_path,
        default_wifi_security_mode=payload.default_wifi_security_mode,
        default_wifi_channel=payload.default_wifi_channel,
        min_firmware_version=payload.min_firmware_version,
    )
    return OnuTypeRead.model_validate(item)


@router.get(
    "/onu-types/{onu_type_id}",
    response_model=OnuTypeRead,
    dependencies=[Depends(require_permission("network:onu_type:read"))],
)
def get_onu_type(onu_type_id: str, db: Session = Depends(get_db)) -> OnuTypeRead:
    return OnuTypeRead.model_validate(onu_types.get(db, onu_type_id))


@router.patch(
    "/onu-types/{onu_type_id}",
    response_model=OnuTypeRead,
    dependencies=[Depends(require_permission("network:onu_type:write"))],
)
def update_onu_type(
    onu_type_id: str, payload: OnuTypeUpdate, db: Session = Depends(get_db)
) -> OnuTypeRead:
    update_data = payload.model_dump(exclude_unset=True)
    # Convert enum string values to actual enums
    if "pon_type" in update_data and update_data["pon_type"] is not None:
        update_data["pon_type"] = PonType(update_data["pon_type"])
    if "gpon_channel" in update_data and update_data["gpon_channel"] is not None:
        update_data["gpon_channel"] = GponChannel(update_data["gpon_channel"])
    if "capability" in update_data and update_data["capability"] is not None:
        update_data["capability"] = OnuCapability(update_data["capability"])
    item = onu_types.update(db, onu_type_id, **update_data)
    return OnuTypeRead.model_validate(item)


@router.delete(
    "/onu-types/{onu_type_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("network:onu_type:write"))],
)
def delete_onu_type(onu_type_id: str, db: Session = Depends(get_db)):  # type: ignore[no-untyped-def]
    onu_types.delete(db, onu_type_id)


# ── Speed Profiles ─────────────────────────────────────────────────────


@router.get(
    "/speed-profiles",
    response_model=list[SpeedProfileRead],
    dependencies=[Depends(require_permission("network:speed_profile:read"))],
)
def list_speed_profiles(
    direction: str | None = None,
    speed_type: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    order_by: str = "name",
    order_dir: str = "asc",
    limit: int = Query(default=200, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[SpeedProfileRead]:
    items = SpeedProfiles.list(
        db,
        direction=direction,
        speed_type=speed_type,
        is_active=is_active,
        search=search,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    results = []
    for item in items:
        read = SpeedProfileRead.model_validate(item)
        read.formatted_speed = (
            format_speed(item.speed_kbps) if item.speed_kbps else None
        )
        results.append(read)
    return results


@router.post(
    "/speed-profiles",
    response_model=SpeedProfileRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:speed_profile:write"))],
)
def create_speed_profile(
    payload: SpeedProfileCreate, db: Session = Depends(get_db)
) -> SpeedProfileRead:
    item = SpeedProfiles.create(
        db,
        name=payload.name,
        direction=_enum_or_422(SpeedProfileDirection, payload.direction, "direction"),
        speed_kbps=payload.speed_kbps,
        speed_type=_enum_or_422(SpeedProfileType, payload.speed_type, "speed_type"),
        use_prefix_suffix=payload.use_prefix_suffix,
        is_default=payload.is_default,
        notes=payload.notes,
    )
    read = SpeedProfileRead.model_validate(item)
    read.formatted_speed = format_speed(item.speed_kbps) if item.speed_kbps else None
    return read


@router.get(
    "/speed-profiles/{profile_id}",
    response_model=SpeedProfileRead,
    dependencies=[Depends(require_permission("network:speed_profile:read"))],
)
def get_speed_profile(
    profile_id: str, db: Session = Depends(get_db)
) -> SpeedProfileRead:
    item = SpeedProfiles.get(db, profile_id)
    read = SpeedProfileRead.model_validate(item)
    read.formatted_speed = format_speed(item.speed_kbps) if item.speed_kbps else None
    return read


@router.patch(
    "/speed-profiles/{profile_id}",
    response_model=SpeedProfileRead,
    dependencies=[Depends(require_permission("network:speed_profile:write"))],
)
def update_speed_profile(
    profile_id: str, payload: SpeedProfileUpdate, db: Session = Depends(get_db)
) -> SpeedProfileRead:
    update_data = payload.model_dump(exclude_unset=True)
    if "direction" in update_data and update_data["direction"] is not None:
        update_data["direction"] = SpeedProfileDirection(update_data["direction"])
    if "speed_type" in update_data and update_data["speed_type"] is not None:
        update_data["speed_type"] = SpeedProfileType(update_data["speed_type"])
    item = SpeedProfiles.update(db, profile_id, **update_data)
    read = SpeedProfileRead.model_validate(item)
    read.formatted_speed = format_speed(item.speed_kbps) if item.speed_kbps else None
    return read


@router.delete(
    "/speed-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("network:speed_profile:write"))],
)
def delete_speed_profile(profile_id: str, db: Session = Depends(get_db)):  # type: ignore[no-untyped-def]
    SpeedProfiles.delete(db, profile_id)


# ── Network Zones ─────────────────────────────────────────────────────


@router.get(
    "/network-zones",
    response_model=list[NetworkZoneRead],
    dependencies=[Depends(require_permission("network:zone:read"))],
)
def list_network_zones(
    is_active: bool | None = None,
    parent_id: str | None = None,
    order_by: str = "name",
    order_dir: str = "asc",
    limit: int = Query(default=200, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[NetworkZoneRead]:
    items = NetworkZones.list(
        db,
        is_active=is_active,
        parent_id=parent_id,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    return [NetworkZoneRead.model_validate(i) for i in items]


@router.post(
    "/network-zones",
    response_model=NetworkZoneRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:zone:write"))],
)
def create_network_zone(
    payload: NetworkZoneCreate, db: Session = Depends(get_db)
) -> NetworkZoneRead:
    item = NetworkZones.create(
        db,
        name=payload.name,
        description=payload.description,
        parent_id=payload.parent_id,
        geo_area_id=payload.geo_area_id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        is_active=payload.is_active,
    )
    return NetworkZoneRead.model_validate(item)


@router.get(
    "/network-zones/{zone_id}",
    response_model=NetworkZoneRead,
    dependencies=[Depends(require_permission("network:zone:read"))],
)
def get_network_zone(zone_id: str, db: Session = Depends(get_db)) -> NetworkZoneRead:
    return NetworkZoneRead.model_validate(NetworkZones.get(db, zone_id))


@router.patch(
    "/network-zones/{zone_id}",
    response_model=NetworkZoneRead,
    dependencies=[Depends(require_permission("network:zone:write"))],
)
def update_network_zone(
    zone_id: str, payload: NetworkZoneUpdate, db: Session = Depends(get_db)
) -> NetworkZoneRead:
    update_data = payload.model_dump(exclude_unset=True)
    item = NetworkZones.update(db, zone_id, **update_data)
    return NetworkZoneRead.model_validate(item)


@router.delete(
    "/network-zones/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("network:zone:write"))],
)
def delete_network_zone(zone_id: str, db: Session = Depends(get_db)):  # type: ignore[no-untyped-def]
    NetworkZones.delete(db, zone_id)


# ── Vendor Capabilities ───────────────────────────────────────────────


@router.get(
    "/vendor-capabilities",
    response_model=list[VendorCapabilityRead],
    dependencies=[Depends(require_permission("network:vendor_capability:read"))],
)
def list_vendor_capabilities(
    vendor: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    order_by: str = "vendor",
    order_dir: str = "asc",
    limit: int = Query(default=200, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[VendorCapabilityRead]:
    items = VendorCapabilities.list(
        db,
        vendor=vendor,
        is_active=is_active,
        search=search,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
        offset=offset,
    )
    return [VendorCapabilityRead.model_validate(i) for i in items]


@router.post(
    "/vendor-capabilities",
    response_model=VendorCapabilityRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def create_vendor_capability(
    payload: VendorCapabilityCreate, db: Session = Depends(get_db)
) -> VendorCapabilityRead:
    item = VendorCapabilities.create(
        db,
        vendor=payload.vendor,
        model=payload.model,
        firmware_pattern=payload.firmware_pattern,
        tr069_root=payload.tr069_root,
        supported_features=payload.supported_features,
        max_wan_services=payload.max_wan_services,
        max_lan_ports=payload.max_lan_ports,
        max_ssids=payload.max_ssids,
        supports_vlan_tagging=payload.supports_vlan_tagging,
        supports_qinq=payload.supports_qinq,
        supports_ipv6=payload.supports_ipv6,
        notes=payload.notes,
    )
    return VendorCapabilityRead.model_validate(item)


@router.get(
    "/vendor-capabilities/{capability_id}",
    response_model=VendorCapabilityRead,
    dependencies=[Depends(require_permission("network:vendor_capability:read"))],
)
def get_vendor_capability(
    capability_id: str, db: Session = Depends(get_db)
) -> VendorCapabilityRead:
    return VendorCapabilityRead.model_validate(
        VendorCapabilities.get(db, capability_id)
    )


@router.patch(
    "/vendor-capabilities/{capability_id}",
    response_model=VendorCapabilityRead,
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def update_vendor_capability(
    capability_id: str,
    payload: VendorCapabilityUpdate,
    db: Session = Depends(get_db),
) -> VendorCapabilityRead:
    update_data = payload.model_dump(exclude_unset=True)
    item = VendorCapabilities.update(db, capability_id, **update_data)
    return VendorCapabilityRead.model_validate(item)


@router.delete(
    "/vendor-capabilities/{capability_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("network:vendor_capability:write"))],
)
def delete_vendor_capability(capability_id: str, db: Session = Depends(get_db)):  # type: ignore[no-untyped-def]
    VendorCapabilities.delete(db, capability_id)


# ── TR-069 Parameter Maps ─────────────────────────────────────────────


@router.get(
    "/vendor-capabilities/{capability_id}/parameter-maps",
    response_model=list[Tr069ParameterMapRead],
    dependencies=[Depends(require_permission("network:tr069:read"))],
)
def list_parameter_maps(
    capability_id: str, db: Session = Depends(get_db)
) -> list[Tr069ParameterMapRead]:
    items = Tr069ParameterMaps.list_for_capability(db, capability_id)
    return [Tr069ParameterMapRead.model_validate(i) for i in items]


@router.post(
    "/vendor-capabilities/{capability_id}/parameter-maps",
    response_model=Tr069ParameterMapRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("network:tr069:write"))],
)
def create_parameter_map(
    capability_id: str,
    payload: Tr069ParameterMapCreate,
    db: Session = Depends(get_db),
) -> Tr069ParameterMapRead:
    item = Tr069ParameterMaps.create(
        db,
        capability_id=capability_id,
        canonical_name=payload.canonical_name,
        tr069_path=payload.tr069_path,
        writable=payload.writable,
        value_type=payload.value_type,
        notes=payload.notes,
    )
    return Tr069ParameterMapRead.model_validate(item)


@router.get(
    "/vendor-capabilities/{capability_id}/parameter-maps/{map_id}",
    response_model=Tr069ParameterMapRead,
    dependencies=[Depends(require_permission("network:tr069:read"))],
)
def get_parameter_map(
    capability_id: str, map_id: str, db: Session = Depends(get_db)
) -> Tr069ParameterMapRead:
    return Tr069ParameterMapRead.model_validate(Tr069ParameterMaps.get(db, map_id))


@router.patch(
    "/vendor-capabilities/{capability_id}/parameter-maps/{map_id}",
    response_model=Tr069ParameterMapRead,
    dependencies=[Depends(require_permission("network:tr069:write"))],
)
def update_parameter_map(
    capability_id: str,
    map_id: str,
    payload: Tr069ParameterMapUpdate,
    db: Session = Depends(get_db),
) -> Tr069ParameterMapRead:
    update_data = payload.model_dump(exclude_unset=True)
    item = Tr069ParameterMaps.update(db, map_id, **update_data)
    return Tr069ParameterMapRead.model_validate(item)


@router.delete(
    "/vendor-capabilities/{capability_id}/parameter-maps/{map_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission("network:tr069:write"))],
)
def delete_parameter_map(
    capability_id: str, map_id: str, db: Session = Depends(get_db)
):  # type: ignore[no-untyped-def]
    Tr069ParameterMaps.delete(db, map_id)
