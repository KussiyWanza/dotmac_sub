from __future__ import annotations

import ipaddress
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.network import (
    DeviceStatus,
    DeviceType,
    FiberCableType,
    FiberEndpointType,
    FiberSegmentType,
    FiberStrandStatus,
    HardwareUnitStatus,
    IpProtocol,
    IPVersion,
    ODNEndpointType,
    OltPortType,
    PortStatus,
    PortType,
    SplitterPortType,
    VlanMode,
    WanConnectionType,
    WanServiceProvisioningStatus,
    WanServiceType,
)


class CPEDeviceBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias="account_id",
        serialization_alias="account_id",
        description="Optional caller-supplied owner reference; not required for standalone OLT/ONT operation.",
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    device_type: DeviceType = DeviceType.router
    status: DeviceStatus = DeviceStatus.active
    serial_number: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    mac_address: str | None = Field(default=None, max_length=64)
    installed_at: datetime | None = None
    notes: str | None = None


class CPEDeviceCreate(CPEDeviceBase):
    pass


class CPEDeviceUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    device_type: DeviceType | None = None
    status: DeviceStatus | None = None
    serial_number: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    mac_address: str | None = Field(default=None, max_length=64)
    installed_at: datetime | None = None
    notes: str | None = None


class CPEDeviceRead(CPEDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class DeviceGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="manual", max_length=40)
    description: str | None = None


class DeviceGroupRead(DeviceGroupCreate):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime


class DeviceGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


class DeviceGroupMemberCreate(BaseModel):
    device_type: Literal["ont", "cpe"]
    device_id: UUID


class DeviceGroupActionRequest(BaseModel):
    action: Literal[
        "reboot",
        "factory_reset",
        "speed_update",
        "catv_toggle",
        "wifi_update",
        "voip_toggle",
        "provision",
    ]
    params: dict | None = None


class PortFields(BaseModel):
    olt_id: UUID | None = None
    port_number: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    port_type: PortType = PortType.ethernet
    status: PortStatus = PortStatus.down
    notes: str | None = None


class PortBase(PortFields):
    device_id: UUID


class PortCreate(PortFields):
    device_id: UUID | None = None

    @model_validator(mode="after")
    def _resolve_device(self) -> PortCreate:
        if not self.device_id and not self.olt_id:
            raise ValueError("device_id or olt_id is required.")
        if not self.device_id and self.olt_id:
            self.device_id = self.olt_id
        return self


class PortUpdate(BaseModel):
    device_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=80)
    port_type: PortType | None = None
    status: PortStatus | None = None
    notes: str | None = None


class PortRead(PortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class VlanBase(BaseModel):
    region_id: UUID
    tag: int = Field(ge=1, le=4094)
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    purpose: str | None = None
    dhcp_snooping: bool = False
    olt_device_id: UUID | None = None
    is_active: bool = True


class VlanCreate(VlanBase):
    pass


class VlanUpdate(BaseModel):
    region_id: UUID | None = None
    tag: int | None = Field(default=None, ge=1, le=4094)
    name: str | None = Field(default=None, max_length=120)
    description: str | None = None
    purpose: str | None = None
    dhcp_snooping: bool | None = None
    olt_device_id: UUID | None = None
    is_active: bool | None = None


class VlanRead(VlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PortVlanBase(BaseModel):
    port_id: UUID
    vlan_id: UUID
    is_tagged: bool = True


class PortVlanCreate(PortVlanBase):
    pass


class PortVlanUpdate(BaseModel):
    port_id: UUID | None = None
    vlan_id: UUID | None = None
    is_tagged: bool | None = None


class PortVlanRead(PortVlanBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID


class IPAssignmentBase(BaseModel):
    # Accept BOTH the field name (subscriber_id) and the alias (account_id) on
    # input. Without this, constructing by field name — e.g.
    # IPAssignmentCreate(subscriber_id=...) in provisioning_helpers — silently
    # dropped the value (validation_alias requires the alias), producing an
    # IP assignment with subscriber_id=None and a NOT NULL violation that
    # poisoned the event-dispatch session.
    model_config = ConfigDict(populate_by_name=True)

    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias="account_id",
        serialization_alias="account_id",
        description=(
            "Optional caller-supplied ownership reference; not required for "
            "standalone OLT/ONT operation."
        ),
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    ip_version: IPVersion = IPVersion.ipv4
    ipv4_address_id: UUID | None = None
    ipv6_address_id: UUID | None = None
    prefix_length: int | None = Field(default=None, ge=0, le=128)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool = True
    is_primary: bool = Field(
        default=False,
        description=(
            "Whether this is the address RADIUS serves as Framed-IP-Address. "
            "A service may hold several addresses but is served on one; "
            "additional held addresses are projected as Framed-Route."
        ),
    )


class IPAssignmentCreate(IPAssignmentBase):
    @model_validator(mode="after")
    def _validate_ip_version(self) -> IPAssignmentCreate:
        if self.ip_version == IPVersion.ipv4:
            if not self.ipv4_address_id or self.ipv6_address_id is not None:
                raise ValueError("ipv4 assignments require ipv4_address_id only.")
        elif self.ip_version == IPVersion.ipv6:
            if not self.ipv6_address_id or self.ipv4_address_id is not None:
                raise ValueError("ipv6 assignments require ipv6_address_id only.")
        return self


class IPAssignmentUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subscriber_id: UUID | None = Field(
        default=None, validation_alias="account_id", serialization_alias="account_id"
    )
    subscription_id: UUID | None = None
    service_address_id: UUID | None = None
    ip_version: IPVersion | None = None
    ipv4_address_id: UUID | None = None
    ipv6_address_id: UUID | None = None
    prefix_length: int | None = Field(default=None, ge=0, le=128)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    is_primary: bool | None = None

    @model_validator(mode="after")
    def _validate_ip_version(self) -> IPAssignmentUpdate:
        fields_set = self.model_fields_set
        if {"ip_version", "ipv4_address_id", "ipv6_address_id"} & fields_set:
            if self.ip_version is None:
                if (
                    self.ipv4_address_id is not None
                    and self.ipv6_address_id is not None
                ):
                    raise ValueError(
                        "Provide only one of ipv4_address_id or ipv6_address_id."
                    )
                return self
            if self.ip_version == IPVersion.ipv4:
                if not self.ipv4_address_id or self.ipv6_address_id is not None:
                    raise ValueError("ipv4 assignments require ipv4_address_id only.")
            elif self.ip_version == IPVersion.ipv6:
                if not self.ipv6_address_id or self.ipv4_address_id is not None:
                    raise ValueError("ipv6 assignments require ipv6_address_id only.")
        return self


class IPAssignmentRead(IPAssignmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IpPoolBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ip_version: IPVersion = IPVersion.ipv4
    cidr: str = Field(min_length=1, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool = True
    nas_device_id: UUID | None = None
    olt_device_id: UUID | None = None
    vlan_id: UUID | None = None
    notes: str | None = None
    # IPv6 pools: per-customer prefix-delegation size carved from ``cidr``.
    delegation_prefix_length: int | None = Field(default=None, ge=1, le=128)


class IpPoolCreate(IpPoolBase):
    pass


class IpPoolUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    ip_version: IPVersion | None = None
    cidr: str | None = Field(default=None, min_length=1, max_length=64)
    gateway: str | None = Field(default=None, max_length=64)
    dns_primary: str | None = Field(default=None, max_length=64)
    dns_secondary: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    nas_device_id: UUID | None = None
    olt_device_id: UUID | None = None
    vlan_id: UUID | None = None
    notes: str | None = None
    delegation_prefix_length: int | None = Field(default=None, ge=1, le=128)


class IpPoolRead(IpPoolBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IpBlockBase(BaseModel):
    pool_id: UUID
    cidr: str = Field(min_length=1, max_length=64)
    is_active: bool = True
    notes: str | None = None


class IpBlockCreate(IpBlockBase):
    pass


class IpBlockUpdate(BaseModel):
    pool_id: UUID | None = None
    cidr: str | None = Field(default=None, min_length=1, max_length=64)
    is_active: bool | None = None
    notes: str | None = None


class IpBlockRead(IpBlockBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


def _canonical_ipv4(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ValueError as exc:
        raise ValueError("address must be a valid IPv4 address") from exc


def _canonical_ipv6(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        # Compressed form so 2001:db8::1 and 2001:0db8:0000::1 are the same row.
        return str(ipaddress.IPv6Address(value.strip()))
    except ValueError as exc:
        raise ValueError("address must be a valid IPv6 address") from exc


class IPv4AddressBase(BaseModel):
    address: str = Field(min_length=1, max_length=15)
    pool_id: UUID | None = None
    is_reserved: bool = False
    notes: str | None = None

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        return _canonical_ipv4(value)  # type: ignore[return-value]


class IPv4AddressCreate(IPv4AddressBase):
    pass


class IPv4AddressUpdate(BaseModel):
    address: str | None = Field(default=None, min_length=1, max_length=15)
    pool_id: UUID | None = None
    is_reserved: bool | None = None
    notes: str | None = None

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str | None) -> str | None:
        return _canonical_ipv4(value)


class IPv4AddressRead(IPv4AddressBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class IPv6AddressBase(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    pool_id: UUID | None = None
    is_reserved: bool = False
    notes: str | None = None

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        return _canonical_ipv6(value)  # type: ignore[return-value]


class IPv6AddressCreate(IPv6AddressBase):
    pass


class IPv6AddressUpdate(BaseModel):
    address: str | None = Field(default=None, min_length=1, max_length=64)
    pool_id: UUID | None = None
    is_reserved: bool | None = None
    notes: str | None = None

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str | None) -> str | None:
        return _canonical_ipv6(value)


class IPv6AddressRead(IPv6AddressBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OLTDeviceBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=120)
    software_version: str | None = Field(default=None, max_length=120)
    ssh_username: str | None = Field(default=None, max_length=120)
    ssh_password: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = None
    snmp_enabled: bool = False
    snmp_port: int | None = None
    snmp_version: str | None = Field(default="v2c", max_length=10)
    snmp_ro_community: str | None = Field(default=None, max_length=255)
    snmp_rw_community: str | None = Field(default=None, max_length=255)
    tr069_acs_server_id: UUID | None = None
    supported_pon_types: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    status: str | None = "active"
    is_active: bool = True

    # OLT Config Pack JSON (source of truth for ONT provisioning defaults)
    config_pack: dict | None = None

    # Management IP pool for auto-allocation (FK, not in config_pack)
    mgmt_ip_pool_id: UUID | None = None

    # OMCI Capability Flags (firmware-specific command support)
    supports_ont_internet_config: bool = True
    supports_ont_wan_config: bool = True
    supports_ont_home_gateway_config: bool = False
    wan_provisioning_mode: Literal[
        "tr069_only", "home_gateway_config", "omci_wan_config"
    ] = "omci_wan_config"
    capabilities_source: Literal["auto", "manual"] = "auto"


class OLTDeviceCreate(OLTDeviceBase):
    is_active: bool = False


class OLTDeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    hostname: str | None = Field(default=None, max_length=160)
    mgmt_ip: str | None = Field(default=None, max_length=64)
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=120)
    software_version: str | None = Field(default=None, max_length=120)
    ssh_username: str | None = Field(default=None, max_length=120)
    ssh_password: str | None = Field(default=None, max_length=255)
    ssh_port: int | None = None
    snmp_enabled: bool | None = None
    snmp_port: int | None = None
    snmp_version: str | None = Field(default=None, max_length=10)
    snmp_ro_community: str | None = Field(default=None, max_length=255)
    snmp_rw_community: str | None = Field(default=None, max_length=255)
    tr069_acs_server_id: UUID | None = None
    supported_pon_types: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    status: str | None = None
    is_active: bool | None = None

    # OLT Config Pack JSON (source of truth for ONT provisioning defaults)
    config_pack: dict | None = None

    # Management IP pool for auto-allocation (FK, not in config_pack)
    mgmt_ip_pool_id: UUID | None = None

    # OMCI Capability Flags (firmware-specific command support)
    supports_ont_internet_config: bool | None = None
    supports_ont_wan_config: bool | None = None
    supports_ont_home_gateway_config: bool | None = None
    wan_provisioning_mode: (
        Literal["tr069_only", "home_gateway_config", "omci_wan_config"] | None
    ) = None
    capabilities_source: Literal["auto", "manual"] | None = None


class OLTDeviceRead(OLTDeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PonPortFields(BaseModel):
    olt_id: UUID
    olt_card_port_id: UUID | None = None
    port_number: int | None = None
    max_ont_capacity: int | None = Field(default=None, ge=1, le=1024)
    notes: str | None = None
    is_active: bool = True


class PonPortBase(PonPortFields):
    name: str = Field(min_length=1, max_length=120)


class PonPortCreate(PonPortFields):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    card_id: UUID | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _ensure_name(self) -> PonPortCreate:
        if not self.name:
            if self.port_number is None:
                raise ValueError("name or port_number is required.")
            self.name = f"pon-{self.port_number}"
        return self


class PonPortUpdate(BaseModel):
    olt_id: UUID | None = None
    olt_card_port_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    port_number: int | None = None
    max_ont_capacity: int | None = Field(default=None, ge=1, le=1024)
    notes: str | None = None
    is_active: bool | None = None


class PonPortRead(PonPortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OntUnitBase(BaseModel):
    serial_number: str = Field(min_length=1, max_length=120)
    vendor_serial_number: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    hardware_version: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=120)
    software_version: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True
    # Imported / external provisioning fields
    onu_type_id: UUID | None = None
    olt_device_id: UUID | None = None
    pon_type: str | None = None
    gpon_channel: str | None = None
    board: str | None = Field(default=None, max_length=60)
    port: str | None = Field(default=None, max_length=60)
    onu_mode: str | None = None
    user_vlan_id: UUID | None = None
    zone_id: UUID | None = None
    splitter_id: UUID | None = None
    splitter_port_id: UUID | None = None
    download_speed_profile_id: UUID | None = None
    upload_speed_profile_id: UUID | None = None
    name: str | None = Field(default=None, max_length=200)
    address_or_comment: str | None = None
    contact: str | None = Field(default=None, max_length=255)
    external_id: str | None = Field(default=None, max_length=120)
    use_gps: bool = False
    gps_latitude: float | None = None
    gps_longitude: float | None = None


class OntUnitCreate(OntUnitBase):
    pass


class OntUnitUpdate(BaseModel):
    serial_number: str | None = Field(default=None, min_length=1, max_length=120)
    vendor_serial_number: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    vendor: str | None = Field(default=None, max_length=120)
    hardware_version: str | None = Field(default=None, max_length=120)
    firmware_version: str | None = Field(default=None, max_length=120)
    software_version: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None
    # Imported / external provisioning fields
    onu_type_id: UUID | None = None
    olt_device_id: UUID | None = None
    pon_type: str | None = None
    gpon_channel: str | None = None
    board: str | None = Field(default=None, max_length=60)
    port: str | None = Field(default=None, max_length=60)
    onu_mode: str | None = None
    user_vlan_id: UUID | None = None
    zone_id: UUID | None = None
    splitter_id: UUID | None = None
    splitter_port_id: UUID | None = None
    download_speed_profile_id: UUID | None = None
    upload_speed_profile_id: UUID | None = None
    name: str | None = Field(default=None, max_length=200)
    address_or_comment: str | None = None
    contact: str | None = Field(default=None, max_length=255)
    external_id: str | None = Field(default=None, max_length=120)
    use_gps: bool | None = None
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    # ONU mode configuration fields
    wan_mode: str | None = None
    config_method: str | None = None
    ip_protocol: str | None = None
    pppoe_username: str | None = Field(default=None, max_length=120)
    pppoe_password: str | None = Field(default=None, max_length=120)
    # Management IP configuration fields
    tr069_acs_server_id: UUID | None = None
    mgmt_ip_mode: str | None = None
    mgmt_ip_address: str | None = Field(default=None, max_length=64)
    voip_enabled: bool | None = None


class OntUnitRead(OntUnitBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    onu_rx_signal_dbm: float | None = None
    olt_rx_signal_dbm: float | None = None
    distance_meters: int | None = None
    signal_updated_at: datetime | None = None
    olt_status: str | None = None
    acs_last_inform_at: datetime | None = None
    last_seen_at: datetime | None = None
    offline_reason: str | None = None
    mac_address: str | None = None
    observed_wan_ip: str | None = None
    observed_pppoe_status: str | None = None
    observed_lan_mode: str | None = None
    observed_wifi_clients: int | None = None
    observed_lan_hosts: int | None = None
    observed_runtime_updated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OntAssignmentBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ont_unit_id: UUID
    pon_port_id: UUID | None = None  # Optional for TR-069-only devices
    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias="account_id",
        serialization_alias="account_id",
        description=(
            "Optional caller-supplied ownership reference; not required for "
            "standalone OLT/ONT operation."
        ),
    )
    subscription_id: UUID | None = Field(
        default=None,
        description="Catalog subscription whose access service is delivered by this ONT.",
    )
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool = True
    notes: str | None = None

    # Service configuration (subscriber settings for this ONT)
    wan_mode: str | None = Field(
        default="routing", description="WAN mode: routing or bridging"
    )
    ip_mode: str | None = Field(
        default="dhcp", description="IP mode: dhcp or static_ip"
    )
    static_ip: str | None = Field(default=None, max_length=64)
    static_gateway: str | None = Field(default=None, max_length=64)
    static_subnet: str | None = Field(default=None, max_length=64)
    static_dns: str | None = Field(default=None, max_length=200)
    pppoe_username: str | None = Field(default=None, max_length=200)
    pppoe_password: str | None = Field(default=None, max_length=512)
    wifi_ssid: str | None = Field(default=None, max_length=64)
    wifi_password: str | None = Field(default=None, max_length=512)
    wifi_enabled: bool | None = None
    wifi_security_mode: str | None = Field(default=None, max_length=40)
    wifi_channel: str | None = Field(default=None, max_length=10)

    # Management IP configuration (ONT-side management IP)
    mgmt_ip_mode: str | None = Field(
        default="inactive",
        description="Management IP mode: inactive, dhcp, or static_ip",
    )
    mgmt_ip_address: str | None = Field(default=None, max_length=64)
    mgmt_subnet: str | None = Field(default=None, max_length=64)
    mgmt_gateway: str | None = Field(default=None, max_length=64)
    lan_ip: str | None = Field(default=None, max_length=64)
    lan_subnet: str | None = Field(default=None, max_length=64)
    lan_dhcp_enabled: bool | None = None
    lan_dhcp_start: str | None = Field(default=None, max_length=64)
    lan_dhcp_end: str | None = Field(default=None, max_length=64)


class OntAssignmentCreate(OntAssignmentBase):
    pass


class OntAssignmentUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ont_unit_id: UUID | None = None
    pon_port_id: UUID | None = None
    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias="account_id",
        serialization_alias="account_id",
        description=(
            "Optional caller-supplied ownership reference; not required for "
            "standalone OLT/ONT operation."
        ),
    )
    subscription_id: UUID | None = Field(
        default=None,
        description=(
            "Optional caller-supplied ownership reference; not required for "
            "standalone OLT/ONT operation."
        ),
    )
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool | None = None
    notes: str | None = None

    # Service configuration (subscriber settings for this ONT)
    wan_mode: str | None = None
    ip_mode: str | None = None
    static_ip: str | None = None
    static_gateway: str | None = None
    static_subnet: str | None = None
    static_dns: str | None = None
    pppoe_username: str | None = None
    pppoe_password: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    wifi_enabled: bool | None = None
    wifi_security_mode: str | None = None
    wifi_channel: str | None = None

    # Management IP configuration
    mgmt_ip_mode: str | None = None
    mgmt_ip_address: str | None = None
    mgmt_subnet: str | None = None
    mgmt_gateway: str | None = None
    lan_ip: str | None = None
    lan_subnet: str | None = None
    lan_dhcp_enabled: bool | None = None
    lan_dhcp_start: str | None = None
    lan_dhcp_end: str | None = None


class OntAssignmentRead(OntAssignmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltShelfBase(BaseModel):
    olt_id: UUID
    shelf_number: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class OltShelfCreate(OltShelfBase):
    pass


class OltShelfUpdate(BaseModel):
    olt_id: UUID | None = None
    shelf_number: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class OltShelfRead(OltShelfBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltCardBase(BaseModel):
    shelf_id: UUID
    slot_number: int = Field(ge=1)
    card_type: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool = True


class OltCardCreate(OltCardBase):
    pass


class OltCardUpdate(BaseModel):
    shelf_id: UUID | None = None
    slot_number: int | None = Field(default=None, ge=1)
    card_type: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    notes: str | None = None
    is_active: bool | None = None


class OltCardRead(OltCardBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltCardPortBase(BaseModel):
    card_id: UUID
    port_number: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=120)
    port_type: OltPortType = OltPortType.pon
    is_active: bool = True
    notes: str | None = None


class OltCardPortCreate(OltCardPortBase):
    pass


class OltCardPortUpdate(BaseModel):
    card_id: UUID | None = None
    port_number: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, max_length=120)
    port_type: OltPortType | None = None
    is_active: bool | None = None
    notes: str | None = None


class OltCardPortRead(OltCardPortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FdhCabinetBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    region_id: UUID | None = None
    notes: str | None = None
    is_active: bool = True


class FdhCabinetCreate(FdhCabinetBase):
    pass


class FdhCabinetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    region_id: UUID | None = None
    notes: str | None = None
    is_active: bool | None = None


class FdhCabinetRead(FdhCabinetBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SplitterBase(BaseModel):
    fdh_id: UUID | None = None
    name: str = Field(min_length=1, max_length=160)
    splitter_ratio: str = Field(min_length=3, max_length=40)
    input_ports: int = Field(default=1, ge=1)
    output_ports: int = Field(default=8, ge=1)
    notes: str | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _validate_declared_capacity(self) -> SplitterBase:
        parts = self.splitter_ratio.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("splitter_ratio must use the exact input:output form")
        ratio_inputs, ratio_outputs = (int(part) for part in parts)
        if "input_ports" not in self.model_fields_set:
            self.input_ports = ratio_inputs
        if "output_ports" not in self.model_fields_set:
            self.output_ports = ratio_outputs
        expected = f"{self.input_ports}:{self.output_ports}"
        if self.is_active and self.splitter_ratio.strip() != expected:
            raise ValueError(
                f"splitter_ratio must equal declared input/output capacity {expected}"
            )
        return self


class SplitterCreate(SplitterBase):
    pass


class SplitterUpdate(BaseModel):
    fdh_id: UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    splitter_ratio: str | None = Field(default=None, max_length=40)
    input_ports: int | None = Field(default=None, ge=1)
    output_ports: int | None = Field(default=None, ge=1)
    notes: str | None = None
    is_active: bool | None = None


class SplitterRead(SplitterBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    insertion_loss_db: Decimal | None = None
    created_at: datetime
    updated_at: datetime


class SplitterPortBase(BaseModel):
    splitter_id: UUID
    port_number: int = Field(ge=1)
    port_type: SplitterPortType = SplitterPortType.output
    is_active: bool = True
    notes: str | None = None


class SplitterPortCreate(SplitterPortBase):
    pass


class SplitterPortUpdate(BaseModel):
    splitter_id: UUID | None = None
    port_number: int | None = Field(default=None, ge=1)
    port_type: SplitterPortType | None = None
    is_active: bool | None = None
    notes: str | None = None


class SplitterPortRead(SplitterPortBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class SplitterPortAssignmentBase(BaseModel):
    splitter_port_id: UUID
    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias="account_id",
        serialization_alias="account_id",
        description=(
            "Optional caller-supplied ownership reference; not required for "
            "standalone OLT/ONT operation."
        ),
    )
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool = True
    notes: str | None = None


class SplitterPortAssignmentCreate(SplitterPortAssignmentBase):
    pass


class SplitterPortAssignmentUpdate(BaseModel):
    splitter_port_id: UUID | None = None
    subscriber_id: UUID | None = Field(
        default=None,
        validation_alias="account_id",
        serialization_alias="account_id",
        description=(
            "Optional caller-supplied ownership reference; not required for "
            "standalone OLT/ONT operation."
        ),
    )
    service_address_id: UUID | None = None
    assigned_at: datetime | None = None
    active: bool | None = None
    notes: str | None = None


class SplitterPortAssignmentRead(SplitterPortAssignmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberStrandFields(BaseModel):
    strand_number: int = Field(ge=1)
    label: str | None = Field(default=None, max_length=160)
    status: FiberStrandStatus = FiberStrandStatus.available
    upstream_type: FiberEndpointType | None = None
    upstream_id: UUID | None = None
    downstream_type: FiberEndpointType | None = None
    downstream_id: UUID | None = None
    notes: str | None = None
    is_active: bool = True


class FiberStrandBase(FiberStrandFields):
    cable_name: str = Field(min_length=1, max_length=160)


class FiberStrandCreate(FiberStrandFields):
    cable_name: str | None = Field(default=None, min_length=1, max_length=160)
    segment_id: UUID | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _resolve_cable_name(self) -> FiberStrandCreate:
        if not self.cable_name:
            if not self.segment_id:
                raise ValueError("cable_name or segment_id is required.")
            self.cable_name = f"segment-{self.segment_id}"
        return self


class FiberStrandUpdate(BaseModel):
    cable_name: str | None = Field(default=None, min_length=1, max_length=160)
    strand_number: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=160)
    status: FiberStrandStatus | None = None
    upstream_type: FiberEndpointType | None = None
    upstream_id: UUID | None = None
    downstream_type: FiberEndpointType | None = None
    downstream_id: UUID | None = None
    notes: str | None = None
    is_active: bool | None = None


class FiberStrandRead(FiberStrandBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    segment_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class FiberSpliceClosureBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    notes: str | None = None
    is_active: bool = True


class FiberSpliceClosureCreate(FiberSpliceClosureBase):
    pass


class FiberSpliceClosureUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = None
    is_active: bool | None = None


class FiberSpliceClosureRead(FiberSpliceClosureBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSpliceTrayBase(BaseModel):
    closure_id: UUID
    tray_number: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=160)
    notes: str | None = None


class FiberSpliceTrayCreate(FiberSpliceTrayBase):
    pass


class FiberSpliceTrayUpdate(BaseModel):
    closure_id: UUID | None = None
    tray_number: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, max_length=160)
    notes: str | None = None


class FiberSpliceTrayRead(FiberSpliceTrayBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSpliceFields(BaseModel):
    tray_id: UUID | None = None
    splice_type: str | None = Field(default=None, max_length=80)
    loss_db: float | None = None
    notes: str | None = None


class FiberSpliceBase(FiberSpliceFields):
    closure_id: UUID
    from_strand_id: UUID
    to_strand_id: UUID


class FiberSpliceCreate(FiberSpliceFields):
    closure_id: UUID | None = None
    from_strand_id: UUID | None = None
    to_strand_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_requirements(self) -> FiberSpliceCreate:
        has_full = self.closure_id and self.from_strand_id and self.to_strand_id
        has_tray = self.tray_id and self.position
        if not has_full and not has_tray:
            raise ValueError(
                "Provide closure_id/from_strand_id/to_strand_id or tray_id/position."
            )
        return self


class FiberSpliceUpdate(BaseModel):
    closure_id: UUID | None = None
    from_strand_id: UUID | None = None
    to_strand_id: UUID | None = None
    tray_id: UUID | None = None
    position: int | None = Field(default=None, ge=1)
    splice_type: str | None = Field(default=None, max_length=80)
    loss_db: float | None = None
    notes: str | None = None


class FiberSpliceRead(FiberSpliceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime


class FiberTerminationPointBase(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    endpoint_type: ODNEndpointType = ODNEndpointType.other
    ref_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool = True


class FiberTerminationPointCreate(FiberTerminationPointBase):
    pass


class FiberTerminationPointUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    endpoint_type: ODNEndpointType | None = None
    ref_id: UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = None
    is_active: bool | None = None


class FiberTerminationPointRead(FiberTerminationPointBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class FiberSegmentBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    segment_type: FiberSegmentType = FiberSegmentType.distribution
    cable_type: FiberCableType | None = None
    fiber_count: int | None = Field(default=None, ge=1)
    from_point_id: UUID | None = None
    to_point_id: UUID | None = None
    fiber_strand_id: UUID | None = None
    length_m: float | None = None
    notes: str | None = None
    is_active: bool = True


class FiberSegmentCreate(FiberSegmentBase):
    pass


class FiberSegmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    segment_type: FiberSegmentType | None = None
    cable_type: FiberCableType | None = None
    fiber_count: int | None = Field(default=None, ge=1)
    from_point_id: UUID | None = None
    to_point_id: UUID | None = None
    fiber_strand_id: UUID | None = None
    length_m: float | None = None
    notes: str | None = None
    is_active: bool | None = None


class FiberSegmentRead(FiberSegmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class PonPortSplitterLinkBase(BaseModel):
    pon_port_id: UUID
    splitter_port_id: UUID
    active: bool = True
    notes: str | None = None


class PonPortSplitterLinkCreate(PonPortSplitterLinkBase):
    pass


class PonPortSplitterLinkUpdate(BaseModel):
    pon_port_id: UUID | None = None
    splitter_port_id: UUID | None = None
    active: bool | None = None
    notes: str | None = None


class PonPortSplitterLinkRead(PonPortSplitterLinkBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltPowerUnitBase(BaseModel):
    olt_id: UUID
    slot: str = Field(min_length=1, max_length=40)
    status: HardwareUnitStatus | None = None
    notes: str | None = None
    is_active: bool = True


class OltPowerUnitCreate(OltPowerUnitBase):
    pass


class OltPowerUnitUpdate(BaseModel):
    olt_id: UUID | None = None
    slot: str | None = Field(default=None, min_length=1, max_length=40)
    status: HardwareUnitStatus | None = None
    notes: str | None = None
    is_active: bool | None = None


class OltPowerUnitRead(OltPowerUnitBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


class OltSfpModuleBase(BaseModel):
    olt_card_port_id: UUID
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    wavelength_nm: int | None = None
    rx_power_dbm: float | None = None
    tx_power_dbm: float | None = None
    installed_at: datetime | None = None
    is_active: bool = True
    notes: str | None = None


class OltSfpModuleCreate(OltSfpModuleBase):
    pass


class OltSfpModuleUpdate(BaseModel):
    olt_card_port_id: UUID | None = None
    vendor: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    wavelength_nm: int | None = None
    rx_power_dbm: float | None = None
    tx_power_dbm: float | None = None
    installed_at: datetime | None = None
    is_active: bool | None = None
    notes: str | None = None


class OltSfpModuleRead(OltSfpModuleBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# OntWanServiceInstance Schemas
# ---------------------------------------------------------------------------


class OntWanServiceInstanceBase(BaseModel):
    """Base schema for WAN service instance."""

    ont_id: UUID
    source_profile_service_id: UUID | None = None
    service_type: WanServiceType = WanServiceType.internet
    name: str | None = Field(default=None, max_length=120)
    priority: int = Field(default=1, ge=1)
    is_active: bool = True
    # L2 VLAN
    vlan_mode: VlanMode = VlanMode.tagged
    vlan_id: UUID | None = None
    s_vlan: int | None = Field(default=None, ge=1, le=4094)
    c_vlan: int | None = Field(default=None, ge=1, le=4094)
    cos_priority: int | None = Field(default=None, ge=0, le=7)
    mtu: int = Field(default=1500, ge=576, le=9216)
    # L3 connection
    connection_type: WanConnectionType = WanConnectionType.pppoe
    nat_enabled: bool = True
    ip_mode: IpProtocol | None = None
    # PPPoE
    pppoe_username: str | None = Field(default=None, max_length=200)
    pppoe_password: str | None = Field(default=None, max_length=500)
    # Static IP
    static_ip: str | None = Field(default=None, max_length=64)
    static_gateway: str | None = Field(default=None, max_length=64)
    static_dns: str | None = Field(default=None, max_length=200)
    static_ip_source: str | None = Field(default=None, max_length=200)
    # Binding / OMCI metadata
    bind_lan_ports: dict | None = None
    bind_ssid_index: int | None = Field(default=None, ge=0)
    gem_port_id: int | None = Field(default=None, ge=0)
    t_cont_profile: str | None = Field(default=None, max_length=120)


class OntWanServiceInstanceCreate(OntWanServiceInstanceBase):
    """Schema for creating a WAN service instance."""

    pass


class OntWanServiceInstanceUpdate(BaseModel):
    """Schema for updating a WAN service instance."""

    ont_id: UUID | None = None
    source_profile_service_id: UUID | None = None
    service_type: WanServiceType | None = None
    name: str | None = Field(default=None, max_length=120)
    priority: int | None = Field(default=None, ge=1)
    is_active: bool | None = None
    # L2 VLAN
    vlan_mode: VlanMode | None = None
    vlan_id: UUID | None = None
    s_vlan: int | None = Field(default=None, ge=1, le=4094)
    c_vlan: int | None = Field(default=None, ge=1, le=4094)
    cos_priority: int | None = Field(default=None, ge=0, le=7)
    mtu: int | None = Field(default=None, ge=576, le=9216)
    # L3 connection
    connection_type: WanConnectionType | None = None
    nat_enabled: bool | None = None
    ip_mode: IpProtocol | None = None
    # PPPoE
    pppoe_username: str | None = Field(default=None, max_length=200)
    pppoe_password: str | None = Field(default=None, max_length=500)
    # Static IP
    static_ip: str | None = Field(default=None, max_length=64)
    static_gateway: str | None = Field(default=None, max_length=64)
    static_dns: str | None = Field(default=None, max_length=200)
    static_ip_source: str | None = Field(default=None, max_length=200)
    # Binding / OMCI metadata
    bind_lan_ports: dict | None = None
    bind_ssid_index: int | None = Field(default=None, ge=0)
    gem_port_id: int | None = Field(default=None, ge=0)
    t_cont_profile: str | None = Field(default=None, max_length=120)
    # Provisioning state (typically updated by system, not user)
    provisioning_status: WanServiceProvisioningStatus | None = None
    last_error: str | None = Field(default=None, max_length=500)


class OntWanServiceInstanceRead(OntWanServiceInstanceBase):
    """Schema for reading a WAN service instance."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provisioning_status: WanServiceProvisioningStatus
    last_provisioned_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class StrandColorRead(BaseModel):
    """Derived EIA/TIA-598 field color identity for one exact strand."""

    strand_number: int
    color_standard: str
    tube_number: int | None = None
    tube_color: str | None = None
    core_number_in_tube: int
    core_color: str


class FiberSplicePlanCreate(BaseModel):
    work_order_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)


class FiberSplicePlanItemCreate(BaseModel):
    closure_id: UUID
    from_strand_id: UUID
    from_strand_end: Literal["a", "b"]
    to_strand_id: UUID
    to_strand_end: Literal["a", "b"]
    splice_type: str = Field(min_length=1, max_length=80)
    tray_id: UUID | None = None
    tray_position: int | None = Field(default=None, ge=1)
    expected_loss_db: float | None = Field(default=None, ge=0, le=5)
    notes: str | None = Field(default=None, max_length=2000)


class FiberSplicePlanItemExecutionRead(BaseModel):
    change_request_id: UUID
    status: str


class FiberSplicePlanItemRead(BaseModel):
    item_id: UUID
    position_index: int
    closure_id: UUID
    tray_id: UUID | None = None
    tray_position: int | None = None
    from_strand_id: UUID
    from_strand_end: Literal["a", "b"]
    to_strand_id: UUID
    to_strand_end: Literal["a", "b"]
    splice_type: str
    expected_loss_db: float | None = None
    notes: str | None = None
    from_strand_colors: StrandColorRead | None = None
    to_strand_colors: StrandColorRead | None = None
    execution: FiberSplicePlanItemExecutionRead | None = None
    executed: bool


class FiberSplicePlanRead(BaseModel):
    plan_id: UUID
    work_order_id: UUID
    work_order_public_id: str
    name: str
    status: str
    notes: str | None = None
    issued_at: datetime | None = None
    cancelled_at: datetime | None = None
    item_count: int
    executed_count: int
    unexecuted_count: int
    items: list[FiberSplicePlanItemRead] = Field(default_factory=list)


class FiberSplicePlanDiffRead(BaseModel):
    plan_id: UUID
    executed_items: list[UUID] = Field(default_factory=list)
    pending_review_items: list[UUID] = Field(default_factory=list)
    unexecuted_items: list[UUID] = Field(default_factory=list)
    unplanned_change_requests: list[UUID] = Field(default_factory=list)
    complete: bool
