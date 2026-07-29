"""Schemas for network catalog API — ONU types, speed profiles, zones, vendor capabilities."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ── ONU Type ───────────────────────────────────────────────────────────


class OnuTypeBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    pon_type: str  # gpon, epon, xgpon, xgspon
    gpon_channel: str  # veip, omci, tr069
    ethernet_ports: int = 0
    wifi_ports: int = 0
    voip_ports: int = 0
    catv_ports: int = 0
    allow_custom_profiles: bool = True
    capability: str  # bridge, route, bridge_route
    notes: str | None = None

    # ACS Config Pack: TR-069 configuration
    adapter_name: str | None = None
    tr069_data_model: str | None = None  # tr181 or tr098
    config_method_preference: str | None = None  # tr069, omci, both

    # WiFi TR-069 parameter paths
    wifi_ssid_path: str | None = None
    wifi_password_path: str | None = None
    wifi_enabled_path: str | None = None
    wifi_channel_path: str | None = None
    wifi_security_mode_path: str | None = None

    # WAN TR-069 parameter paths
    wan_pppoe_username_path: str | None = None
    wan_pppoe_password_path: str | None = None
    wan_connection_type_path: str | None = None

    # LAN TR-069 parameter paths
    lan_ip_address_path: str | None = None
    lan_subnet_mask_path: str | None = None
    lan_dhcp_enabled_path: str | None = None
    lan_dhcp_start_path: str | None = None
    lan_dhcp_end_path: str | None = None

    # Access/management TR-069 parameter paths
    remote_access_enabled_path: str | None = None
    http_management_enabled_path: str | None = None

    # Default WiFi settings
    default_wifi_security_mode: str | None = "WPA2-Personal"
    default_wifi_channel: str | None = "auto"

    # Firmware baseline
    min_firmware_version: str | None = None


class OnuTypeCreate(OnuTypeBase):
    pass


class OnuTypeUpdate(BaseModel):
    name: str | None = None
    pon_type: str | None = None
    gpon_channel: str | None = None
    ethernet_ports: int | None = None
    wifi_ports: int | None = None
    voip_ports: int | None = None
    catv_ports: int | None = None
    allow_custom_profiles: bool | None = None
    capability: str | None = None
    notes: str | None = None

    # ACS Config Pack fields
    adapter_name: str | None = None
    tr069_data_model: str | None = None
    config_method_preference: str | None = None
    wifi_ssid_path: str | None = None
    wifi_password_path: str | None = None
    wifi_enabled_path: str | None = None
    wifi_channel_path: str | None = None
    wifi_security_mode_path: str | None = None
    wan_pppoe_username_path: str | None = None
    wan_pppoe_password_path: str | None = None
    wan_connection_type_path: str | None = None
    lan_ip_address_path: str | None = None
    lan_subnet_mask_path: str | None = None
    lan_dhcp_enabled_path: str | None = None
    lan_dhcp_start_path: str | None = None
    lan_dhcp_end_path: str | None = None
    remote_access_enabled_path: str | None = None
    http_management_enabled_path: str | None = None
    default_wifi_security_mode: str | None = None
    default_wifi_channel: str | None = None
    min_firmware_version: str | None = None


class OnuTypeRead(OnuTypeBase):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── Speed Profile ──────────────────────────────────────────────────────


class SpeedProfileBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    direction: str  # download, upload
    speed_kbps: int = Field(ge=0)
    speed_type: str = "internet"  # internet, voip, iptv, management
    use_prefix_suffix: bool = False
    is_default: bool = False
    notes: str | None = None


class SpeedProfileCreate(SpeedProfileBase):
    pass


class SpeedProfileUpdate(BaseModel):
    name: str | None = None
    direction: str | None = None
    speed_kbps: int | None = Field(default=None, ge=0)
    speed_type: str | None = None
    use_prefix_suffix: bool | None = None
    is_default: bool | None = None
    notes: str | None = None


class SpeedProfileRead(SpeedProfileBase):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
    formatted_speed: str | None = None


# ── Network Zone ───────────────────────────────────────────────────────


class NetworkZoneBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    parent_id: str | None = None
    # Typed geographic binding; must reference an active GeoArea.
    geo_area_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool = True


class NetworkZoneCreate(NetworkZoneBase):
    pass


class NetworkZoneUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parent_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_active: bool | None = None
    clear_parent: bool = False
    geo_area_id: str | None = None
    clear_geo_area: bool = False


class NetworkZoneRead(NetworkZoneBase):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
    updated_at: datetime


# ── Vendor Capability ──────────────────────────────────────────────────


class VendorCapabilityBase(BaseModel):
    vendor: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)
    firmware_pattern: str | None = None
    tr069_root: str | None = None
    supported_features: dict[str, Any] = {}
    max_wan_services: int = 1
    max_lan_ports: int = 4
    max_ssids: int = 2
    supports_vlan_tagging: bool = True
    supports_qinq: bool = False
    supports_ipv6: bool = False
    notes: str | None = None


class VendorCapabilityCreate(VendorCapabilityBase):
    pass


class VendorCapabilityUpdate(BaseModel):
    vendor: str | None = None
    model: str | None = None
    firmware_pattern: str | None = None
    tr069_root: str | None = None
    supported_features: dict[str, Any] | None = None
    max_wan_services: int | None = None
    max_lan_ports: int | None = None
    max_ssids: int | None = None
    supports_vlan_tagging: bool | None = None
    supports_qinq: bool | None = None
    supports_ipv6: bool | None = None
    notes: str | None = None


class VendorCapabilityRead(VendorCapabilityBase):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── TR-069 Parameter Map ──────────────────────────────────────────────


class Tr069ParameterMapBase(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=200)
    tr069_path: str = Field(min_length=1, max_length=500)
    writable: bool = True
    value_type: str | None = None
    notes: str | None = None


class Tr069ParameterMapCreate(Tr069ParameterMapBase):
    pass


class Tr069ParameterMapUpdate(BaseModel):
    canonical_name: str | None = None
    tr069_path: str | None = None
    writable: bool | None = None
    value_type: str | None = None
    notes: str | None = None


class Tr069ParameterMapRead(Tr069ParameterMapBase):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    capability_id: UUID
    created_at: datetime
    updated_at: datetime


# ── Provisioning Profile (read-only) ──────────────────────────────────


class OntProvisioningProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    owner_subscriber_id: UUID | None = None
    olt_device_id: UUID | None = None
    name: str
    profile_type: str | None = None
    config_method: str | None = None
    onu_mode: str | None = None
    is_active: bool
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
