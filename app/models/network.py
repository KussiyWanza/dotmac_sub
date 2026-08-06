import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.db import Base


class DeviceType(enum.Enum):
    ont = "ont"
    router = "router"
    switch = "switch"
    hub = "hub"
    firewall = "firewall"
    inverter = "inverter"
    access_point = "access_point"
    bridge = "bridge"
    modem = "modem"
    server = "server"
    cpe = "cpe"
    wireless_radio = "wireless_radio"
    other = "other"


class DeviceStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    maintenance = "maintenance"
    draining = "draining"  # Blocks new ONT authorizations, preserves existing service
    retired = "retired"


class PollStatus(enum.Enum):
    """Status of the last monitoring attempt."""

    success = "success"
    failed = "failed"
    timeout = "timeout"


class PortType(enum.Enum):
    pon = "pon"
    ethernet = "ethernet"
    wifi = "wifi"
    mgmt = "mgmt"


class PortStatus(enum.Enum):
    up = "up"
    down = "down"
    disabled = "disabled"


class IPVersion(enum.Enum):
    ipv4 = "ipv4"
    ipv6 = "ipv6"


class Ipv6PrefixState(enum.Enum):
    """Lifecycle of a delegated IPv6 prefix carved from a pool."""

    available = "available"  # free to allocate
    reserved = "reserved"  # held (e.g. infra / manual) — not auto-allocatable
    assigned = "assigned"  # delegated to a subscriber


class HardwareUnitStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    failed = "failed"
    unknown = "unknown"


class OltPortType(enum.Enum):
    pon = "pon"
    uplink = "uplink"
    ethernet = "ethernet"
    mgmt = "mgmt"


class FiberEndpointType(enum.Enum):
    olt_port = "olt_port"
    splitter_port = "splitter_port"
    fdh = "fdh"
    ont = "ont"
    splice_closure = "splice_closure"
    other = "other"


class ODNEndpointType(enum.Enum):
    fdh = "fdh"
    fiber_access_point = "fiber_access_point"
    splitter = "splitter"
    splitter_port = "splitter_port"
    pon_port = "pon_port"
    olt_port = "olt_port"
    ont = "ont"
    terminal = "terminal"
    splice_closure = "splice_closure"
    other = "other"


class FiberSegmentType(enum.Enum):
    feeder = "feeder"
    distribution = "distribution"
    drop = "drop"


class FiberStrandStatus(enum.Enum):
    available = "available"
    in_use = "in_use"
    reserved = "reserved"
    damaged = "damaged"
    retired = "retired"


class SplitterPortType(enum.Enum):
    input = "input"
    output = "output"


class PonType(enum.Enum):
    gpon = "gpon"
    epon = "epon"


class GponChannel(enum.Enum):
    gpon = "gpon"
    xg_pon = "xg_pon"
    xgs_pon = "xgs_pon"


class OnuCapability(enum.Enum):
    bridging = "bridging"
    routing = "routing"
    bridging_routing = "bridging_routing"


class OnuMode(enum.Enum):
    routing = "routing"
    bridging = "bridging"


class WanMode(enum.Enum):
    dhcp = "dhcp"
    static_ip = "static_ip"
    pppoe = "pppoe"
    setup_via_onu = "setup_via_onu"


class ConfigMethod(enum.Enum):
    omci = "omci"
    tr069 = "tr069"


class OntAuthorizationStatus(enum.Enum):
    pending = "pending"
    authorized = "authorized"
    deauthorized = "deauthorized"
    failed = "failed"


class IpProtocol(enum.Enum):
    ipv4 = "ipv4"
    dual_stack = "dual_stack"


class MgmtIpMode(enum.Enum):
    inactive = "inactive"
    static_ip = "static_ip"
    dhcp = "dhcp"


class SpeedProfileDirection(enum.Enum):
    download = "download"
    upload = "upload"


class SpeedProfileType(enum.Enum):
    internet = "internet"
    management = "management"


class OnuOnlineStatus(enum.Enum):
    online = "online"
    offline = "offline"

    @classmethod
    def _missing_(cls, value: object) -> "OnuOnlineStatus":
        # Treat legacy "unknown" as offline
        if value == "unknown":
            return cls.offline
        return None  # type: ignore[return-value]


class OnuOfflineReason(enum.Enum):
    power_fail = "power_fail"
    los = "los"
    dying_gasp = "dying_gasp"
    unknown = "unknown"


class OntStatusSource(enum.Enum):
    olt = "olt"
    acs = "acs"
    zabbix = "zabbix"
    derived = "derived"


class OntSyncStatus(enum.Enum):
    """Per-ONT reconciler status (see app/services/network/reconcile).

    ``synced``      — last reconcile produced zero residual drift.
    ``reconciling`` — a reconcile is in flight, held under SELECT FOR UPDATE
                       on the OntUnit row so concurrent reconciles serialize.
    ``out_of_sync`` — last reconcile failed to converge. Blocks ``mode=sync``
                       writes; the sweeper keeps retrying.
    """

    synced = "synced"
    reconciling = "reconciling"
    out_of_sync = "out_of_sync"


class OntProvisioningEventStatus(enum.Enum):
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    waiting = "waiting"


class BulkProvisioningRunStatus(enum.Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"


class BulkProvisioningItemStatus(enum.Enum):
    pending = "pending"
    running = "running"
    waiting = "waiting"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class VlanPurpose(enum.Enum):
    internet = "internet"
    management = "management"
    tr069 = "tr069"
    iptv = "iptv"
    voip = "voip"
    other = "other"


class OntProfileType(enum.Enum):
    residential = "residential"
    business = "business"
    management = "management"


class WanServiceType(enum.Enum):
    internet = "internet"
    iptv = "iptv"
    voip = "voip"
    management = "management"
    data = "data"


class VlanMode(enum.Enum):
    tagged = "tagged"
    untagged = "untagged"
    transparent = "transparent"
    translate = "translate"


class WanConnectionType(enum.Enum):
    pppoe = "pppoe"
    dhcp = "dhcp"
    static = "static"
    bridged = "bridged"


class PppoePasswordMode(enum.Enum):
    from_credential = "from_credential"
    generate = "generate"
    static = "static"


class OntProvisioningStatus(enum.Enum):
    unprovisioned = "unprovisioned"
    partial = "partial"
    provisioned = "provisioned"
    drift_detected = "drift_detected"
    failed = "failed"
    pending_acs_registration = "pending_acs_registration"  # Waiting for ACS bootstrap
    pending_service_config = "pending_service_config"  # ACS registered, config pending


class WanServiceProvisioningStatus(enum.Enum):
    """Provisioning state of an individual WAN service instance."""

    pending = "pending"
    provisioned = "provisioned"
    failed = "failed"


class CPEDevice(Base):
    __tablename__ = "cpe_devices"
    __table_args__ = (
        # Stable UISP device id — the uisp_sync upsert key. Partial-unique so
        # rows not sourced from UISP stay NULL.
        Index(
            "uq_cpe_devices_uisp_device_id",
            "uisp_device_id",
            unique=True,
            postgresql_where=text("uisp_device_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NOT NULL: matches the enforced production schema (the squashed baseline
    # declares cpe_devices.subscriber_id NOT NULL; migration 026's relax to
    # nullable never took effect there — the first authenticated uisp_sync run
    # failed every INSERT with a NotNullViolation). Writers must resolve the
    # owning subscriber BEFORE creating a row; keeping the model NOT NULL makes
    # the test schema enforce the same invariant.
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="SET NULL"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    device_type: Mapped[DeviceType] = mapped_column(
        Enum(DeviceType), default=DeviceType.router
    )
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="cpe_devicestatus"), default=DeviceStatus.active
    )
    serial_number: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    vendor: Mapped[str | None] = mapped_column(String(120))
    firmware_version: Mapped[str | None] = mapped_column(String(120))
    mac_address: Mapped[str | None] = mapped_column(String(64))
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    tr069_data_model: Mapped[str | None] = mapped_column(String(40))

    # --- UISP topology sync (relationship layer) ---
    # Stable UISP device id; NULL for rows not sourced from UISP.
    uisp_device_id: Mapped[str | None] = mapped_column(String(64))
    # CPE -> AP edge: the network_devices row (basestation AP) this radio is
    # associated to, per UISP. Lets customer_path walk radio -> AP -> pop_site.
    parent_network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id", ondelete="SET NULL")
    )
    uisp_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last UISP overview.status: active/disconnected/unauthorized (or
    # 'vanished' when the device disappeared from UISP).
    last_uisp_status: Mapped[str | None] = mapped_column(String(20))
    # Current-value RF observation from the AP-side station listing.
    # uisp_sync is the sole writer: set while the station is associated,
    # cleared when it disconnects or vanishes. ``rf_signal_observed_at`` is the
    # sync-run timestamp (consistent with uisp_synced_at), not a UISP payload
    # time. No history is kept here — ont_signal_observations is the precedent
    # if a trend table is ever needed. Readers derive freshness via
    # app.services.network.radio_signal.resolve_effective_radio_signal.
    rf_signal_dbm: Mapped[float | None] = mapped_column(Float)
    rf_signal_source: Mapped[str | None] = mapped_column(String(32))
    rf_signal_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", back_populates="cpe_devices")
    subscription = relationship("Subscription")
    service_address = relationship("Address")
    parent_network_device = relationship("NetworkDevice")
    ports = relationship(
        "Port",
        back_populates="device",
        primaryjoin=lambda: CPEDevice.id == foreign(Port.device_id),
    )


class Port(Base):
    __tablename__ = "ports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Note: this is intentionally not a hard FK. Some legacy callers treat
    # `device_id` / `olt_id` as an identifier for non-CPE devices.
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    port_number: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    port_type: Mapped[PortType] = mapped_column(
        Enum(PortType), default=PortType.ethernet
    )
    status: Mapped[PortStatus] = mapped_column(
        Enum(PortStatus), default=PortStatus.down
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    device = relationship(
        "CPEDevice",
        back_populates="ports",
        primaryjoin=lambda: foreign(Port.device_id) == CPEDevice.id,
    )
    vlans = relationship("PortVlan", back_populates="port")

    @hybrid_property
    def is_active(self) -> bool:
        return self.status != PortStatus.disabled

    @property
    def olt_id(self) -> uuid.UUID:
        """Backwards-compat alias for legacy code/tests."""
        return self.device_id

    @olt_id.setter
    def olt_id(self, value: uuid.UUID) -> None:
        self.device_id = value


class Vlan(Base):
    __tablename__ = "vlans"
    __table_args__ = (
        UniqueConstraint(
            "region_id",
            "olt_device_id",
            "tag",
            name="uq_vlans_region_olt_tag",
        ),
        UniqueConstraint(
            "olt_device_id",
            "id",
            name="uq_vlans_olt_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    region_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id"), nullable=False
    )
    tag: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[VlanPurpose | None] = mapped_column(
        Enum(VlanPurpose, name="vlanpurpose", create_constraint=False),
        nullable=True,
    )
    dhcp_snooping: Mapped[bool] = mapped_column(Boolean, default=False)
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="SET NULL"),
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    port_links = relationship("PortVlan", back_populates="vlan")
    region = relationship("RegionZone")
    olt_device = relationship(
        "OLTDevice", back_populates="vlans", foreign_keys=[olt_device_id]
    )
    ip_pools = relationship("IpPool", back_populates="vlan")


class PortVlan(Base):
    __tablename__ = "port_vlans"
    __table_args__ = (
        UniqueConstraint("port_id", "vlan_id", name="uq_port_vlans_port_vlan"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ports.id"), nullable=False
    )
    vlan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlans.id"), nullable=False
    )
    is_tagged: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    port = relationship("Port", back_populates="vlans")
    vlan = relationship("Vlan", back_populates="port_links")


class IPAssignment(Base):
    __tablename__ = "ip_assignments"
    __table_args__ = (
        # Partial-unique: only ONE *active* assignment per address. Released
        # assignments stay (is_active=False) for history, and the address becomes
        # re-allocatable — without this a terminally-released IP was stranded
        # (counted free in the UI but un-assignable). See migration 177.
        Index(
            "uq_ip_assignments_ipv4_active",
            "ipv4_address_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
        Index(
            "uq_ip_assignments_ipv6_active",
            "ipv6_address_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
        # At most one active PRIMARY IPv4 assignment per exact service. This is
        # the constraint that could not be expressed before the marker existed:
        # a plain unique index on subscription_id would have forbidden holding
        # several addresses, which is a supported product shape.
        Index(
            "uq_ip_assignments_primary_ipv4_active",
            "subscription_id",
            unique=True,
            postgresql_where=text(
                "is_active AND is_primary AND ipv4_address_id IS NOT NULL "
                "AND subscription_id IS NOT NULL"
            ),
            sqlite_where=text(
                "is_active AND is_primary AND ipv4_address_id IS NOT NULL "
                "AND subscription_id IS NOT NULL"
            ),
        ),
        CheckConstraint(
            "(ip_version = 'ipv4' AND ipv4_address_id IS NOT NULL AND ipv6_address_id IS NULL) OR "
            "(ip_version = 'ipv6' AND ipv6_address_id IS NOT NULL AND ipv4_address_id IS NULL)",
            name="ck_ip_assignments_version_address",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="SET NULL"),
        nullable=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    ip_version: Mapped[IPVersion] = mapped_column(
        Enum(IPVersion), default=IPVersion.ipv4
    )
    ipv4_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ipv4_addresses.id")
    )
    ipv6_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ipv6_addresses.id")
    )
    # Which held address RADIUS serves as Framed-IP-Address. A service may hold
    # several addresses -- the admin form allocates one per selected block -- so
    # "holds" and "is served on" are different facts and needed separate
    # storage. Without this, a consumer facing two active assignments had no
    # answer and radius_population picked by unordered query position. An
    # additional address that must coexist with the primary belongs to
    # SubscriberAdditionalRoute as a Framed-Route; this marker does not make a
    # second IPAssignment legitimate, it makes the primary one identifiable.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    prefix_length: Mapped[int | None] = mapped_column(Integer)
    gateway: Mapped[str | None] = mapped_column(String(64))
    dns_primary: Mapped[str | None] = mapped_column(String(64))
    dns_secondary: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber", back_populates="ip_assignments")
    subscription = relationship("Subscription", back_populates="ip_assignments")
    service_address = relationship("Address")
    ipv4_address = relationship("IPv4Address", back_populates="assignment")
    ipv6_address = relationship("IPv6Address", back_populates="assignment")


class SubscriberAdditionalRoute(Base):
    """Extra routed IP blocks for a subscriber, emitted as RADIUS Framed-Route.

    The legacy billing platform auto-attached these via the ``ipv4_route`` field on its
    ``services_internet`` table; the BNG installed each as a route to the PPP
    interface on session-up. After the 2026-06-11 RADIUS cutover dotmac_sub is
    the answering server and must reproduce them, fleet-wide, automatically.

    Stored per-subscriber (not via IPAM ``IPAssignment``) on purpose: a routed
    subnet has no managed host-address row in ``ipv4_addresses`` — and the
    ``ip_assignments`` check constraint requires one — so a routed /29 doesn't
    fit that model without polluting host inventory. Imported duplicate services
    also collapse to a single dotmac_sub subscriber, so subscriber + CIDR is the
    natural unique grain (see uq constraint below; makes backfill idempotent).
    """

    __tablename__ = "subscriber_additional_routes"
    __table_args__ = (
        UniqueConstraint(
            "subscriber_id",
            "cidr",
            name="uq_subscriber_additional_routes_subscriber_cidr",
        ),
        Index(
            "ix_subscriber_additional_routes_subscriber_id",
            "subscriber_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="CASCADE"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Normalised network/prefix, e.g. "160.119.125.104/29" or "102.0.2.5/32".
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix_length: Mapped[int] = mapped_column(Integer, nullable=False)
    # Framed-Route metric (legacy default was 1).
    metric: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Provenance: "splynx_backfill" | "splynx_sync" | "manual".
    source: Mapped[str | None] = mapped_column(String(32))
    splynx_service_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    subscription = relationship("Subscription")


class IpPool(Base):
    __tablename__ = "ip_pools"
    __table_args__ = (UniqueConstraint("name", name="uq_ip_pools_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ip_version: Mapped[IPVersion] = mapped_column(
        Enum(IPVersion), default=IPVersion.ipv4
    )
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway: Mapped[str | None] = mapped_column(String(64))
    dns_primary: Mapped[str | None] = mapped_column(String(64))
    dns_secondary: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="SET NULL"),
        index=True,
    )
    vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vlans.id", ondelete="SET NULL"),
        index=True,
    )
    nas_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("nas_devices.id", ondelete="SET NULL"),
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    # Cached allocation tracking for faster IP allocation
    next_available_ip: Mapped[str | None] = mapped_column(String(64))
    available_count: Mapped[int | None] = mapped_column(Integer)
    # For IPv6 pools: the size of each customer prefix-delegation carved from
    # ``cidr`` (the parent prefix). Configurable per pool; defaults to /64.
    delegation_prefix_length: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    blocks = relationship("IpBlock", back_populates="pool")
    ipv4_addresses = relationship("IPv4Address", back_populates="pool")
    ipv6_addresses = relationship("IPv6Address", back_populates="pool")
    delegated_prefixes = relationship("Ipv6DelegatedPrefix", back_populates="pool")
    nas_device = relationship("NasDevice", foreign_keys=[nas_device_id])
    olt_device = relationship(
        "OLTDevice", back_populates="ip_pools", foreign_keys=[olt_device_id]
    )
    vlan = relationship("Vlan", back_populates="ip_pools")


class IpBlock(Base):
    __tablename__ = "ip_blocks"
    __table_args__ = (
        UniqueConstraint("pool_id", "cidr", name="uq_ip_blocks_pool_cidr"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id"), nullable=False
    )
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pool = relationship("IpPool", back_populates="blocks")


class IPv4Address(Base):
    __tablename__ = "ipv4_addresses"
    __table_args__ = (UniqueConstraint("address", name="uq_ipv4_addresses_address"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    address: Mapped[str] = mapped_column(String(15), nullable=False)
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id")
    )
    is_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # Management IP tracking: link to ONT that has this IP assigned
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="SET NULL"),
        index=True,
        doc="ONT with this IP as management address",
    )
    allocation_type: Mapped[str | None] = mapped_column(
        String(20),
        doc="Type of allocation: 'management', 'wan', 'static'",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Active-preferring: with the partial-unique index an address can carry past
    # (inactive) assignments plus at most one active one; order so the active row
    # wins this uselist=False accessor.
    assignment = relationship(
        "IPAssignment",
        back_populates="ipv4_address",
        uselist=False,
        order_by="IPAssignment.is_active.desc()",
    )
    pool = relationship("IpPool", back_populates="ipv4_addresses")
    ont_unit = relationship("OntUnit", foreign_keys=[ont_unit_id])


class IPv6Address(Base):
    __tablename__ = "ipv6_addresses"
    __table_args__ = (UniqueConstraint("address", name="uq_ipv6_addresses_address"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id")
    )
    is_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    assignment = relationship(
        "IPAssignment",
        back_populates="ipv6_address",
        uselist=False,
        order_by="IPAssignment.is_active.desc()",
    )
    pool = relationship("IpPool", back_populates="ipv6_addresses")


class Ipv6DelegatedPrefix(Base):
    """A delegated IPv6 prefix (e.g. a /64) carved from an IPv6 pool's parent
    prefix and handed to a subscriber.

    First-class PD record: the app — not FreeRADIUS dynamic pools — is the source
    of truth for who owns which prefix. One row per (pool, prefix); its ``state``
    transitions available -> reserved/assigned and back, so a released prefix
    stays for reuse. ``subscriber_id``/``subscription_id`` use SET NULL so deleting
    an owner unassigns the prefix rather than destroying the inventory record.
    """

    __tablename__ = "ipv6_delegated_prefixes"
    __table_args__ = (
        UniqueConstraint(
            "pool_id", "prefix", "prefix_length", name="uq_ipv6_pd_pool_prefix"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ip_pools.id"), nullable=False, index=True
    )
    # Network address of the delegated prefix, e.g. "2001:db8:0:1::".
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix_length: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[Ipv6PrefixState] = mapped_column(
        Enum(Ipv6PrefixState, name="ipv6prefixstate"),
        default=Ipv6PrefixState.available,
        nullable=False,
        index=True,
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pool = relationship("IpPool", back_populates="delegated_prefixes")


class IpPoolUtilizationSnapshot(Base):
    """Point-in-time utilization of an IP pool, captured periodically.

    Utilization is otherwise computed live (current counts only). A periodic
    Celery task writes one row per pool so the admin pool detail can chart
    usage over time rather than just the instantaneous bar.
    """

    __tablename__ = "ip_pool_utilization_snapshots"
    __table_args__ = (
        Index(
            "ix_ip_pool_util_snap_pool_time",
            "pool_id",
            "captured_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_pools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    pool = relationship("IpPool")


class OLTDevice(Base):
    __tablename__ = "olt_devices"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_olt_devices_hostname"),
        UniqueConstraint("mgmt_ip", name="uq_olt_devices_mgmt_ip"),
        CheckConstraint(
            "wan_provisioning_mode IN "
            "('tr069_only', 'home_gateway_config', 'omci_wan_config')",
            name="ck_olt_devices_wan_provisioning_mode",
        ),
        CheckConstraint(
            "capabilities_source IN ('auto', 'manual')",
            name="ck_olt_devices_capabilities_source",
        ),
        # Stable UISP device id — the uisp_sync upsert key (UF-OLTs).
        Index(
            "uq_olt_devices_uisp_device_id",
            "uisp_device_id",
            unique=True,
            postgresql_where=text("uisp_device_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(160))
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    firmware_version: Mapped[str | None] = mapped_column(String(120))
    software_version: Mapped[str | None] = mapped_column(String(120))
    ssh_username: Mapped[str | None] = mapped_column(String(120))
    ssh_password: Mapped[str | None] = mapped_column(String(512))
    ssh_port: Mapped[int | None] = mapped_column(Integer, default=22)
    snmp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    snmp_port: Mapped[int | None] = mapped_column(Integer, default=161)
    snmp_version: Mapped[str | None] = mapped_column(String(10), default="v2c")
    snmp_ro_community: Mapped[str | None] = mapped_column(String(512))
    snmp_rw_community: Mapped[str | None] = mapped_column(String(512))
    # Configurable SNMP performance settings
    snmp_timeout_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    snmp_bulk_enabled: Mapped[bool | None] = mapped_column(Boolean, default=True)
    snmp_bulk_max_repetitions: Mapped[int | None] = mapped_column(Integer, default=None)
    # Tiered polling state (persists across restarts)
    poll_cycle_number: Mapped[int | None] = mapped_column(Integer, default=0)
    tr069_acs_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_acs_servers.id")
    )
    tr069_profiles_snapshot: Mapped[dict | None] = mapped_column(JSON)
    tr069_profiles_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    supported_pon_types: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="devicestatus", create_constraint=False),
        default=DeviceStatus.active,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Polling health tracking
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_status: Mapped[PollStatus | None] = mapped_column(
        Enum(PollStatus, name="pollstatus", create_constraint=False)
    )
    last_poll_error: Mapped[str | None] = mapped_column(String(500))
    consecutive_poll_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Ping reachability (network layer)
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ping_ok: Mapped[bool | None] = mapped_column(Boolean)

    # Last successful SSH connection (telemetry). The Phase-4 OLT SSH
    # circuit-breaker columns (circuit_state / circuit_failure_count /
    # circuit_failure_threshold / backoff_until) were removed — the subsystem
    # was inert (no producers; writes bypassed it). OLT writes go direct and
    # cleanup is the ONT reconcile sweeper's job.
    last_successful_ssh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Zabbix monitoring integration
    zabbix_host_id: Mapped[str | None] = mapped_column(String(20))
    zabbix_last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # UISP topology sync: stable UISP device id (UF-OLTs); NULL otherwise.
    uisp_device_id: Mapped[str | None] = mapped_column(String(64))

    # Autofind sync deduplication (prevents redundant SSH queries during concurrent auths)
    autofind_last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # REST API configuration
    api_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    api_url: Mapped[str | None] = mapped_column(String(512))
    api_port: Mapped[int | None] = mapped_column(Integer, default=443)
    api_username: Mapped[str | None] = mapped_column(String(120))
    api_password: Mapped[str | None] = mapped_column(String(512))
    api_token: Mapped[str | None] = mapped_column(String(1024))
    api_auth_type: Mapped[str | None] = mapped_column(String(20))

    # Rate limiting (operations per minute)
    rate_limit_ops_per_minute: Mapped[int | None] = mapped_column(Integer, default=10)

    # -------------------------------------------------------------------------
    # OLT Capabilities: firmware-specific command support
    # -------------------------------------------------------------------------
    # These flags gate which OMCI commands are attempted during provisioning.
    # MA5608T V800R013 does NOT support ont internet-config / ont wan-config.
    # MA5800 V800R019+ supports both.
    supports_ont_internet_config: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        doc="False for MA5608T V800R013* - skip ont internet-config command",
    )
    supports_ont_wan_config: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        doc="False for MA5608T V800R013* - skip ont wan-config command",
    )
    supports_ont_home_gateway_config: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        doc="True for OLTs that use ont home-gateway-config instead of wan-config",
    )
    wan_provisioning_mode: Mapped[str] = mapped_column(
        String(40),
        default="omci_wan_config",
        doc="tr069_only, home_gateway_config, or omci_wan_config",
    )
    capabilities_source: Mapped[str] = mapped_column(
        String(20),
        default="auto",
        doc="auto when inferred from firmware, manual when operator overridden",
    )

    # -------------------------------------------------------------------------
    # OLT Config Pack: defaults inherited by all ONTs on this OLT
    # -------------------------------------------------------------------------

    # JSONB source of truth for config pack
    config_pack: Mapped[dict | None] = mapped_column(
        JSONB,
        default=dict,
        doc="OLT provisioning defaults. Source of truth for ONT config inheritance.",
    )

    # Management IP pool for auto-allocation during authorization (FK, not in config_pack)
    mgmt_ip_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_pools.id", ondelete="SET NULL"),
        doc="IP pool for auto-allocating management IPs to ONTs",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pon_ports = relationship("PonPort", back_populates="olt")
    power_units = relationship("OltPowerUnit", back_populates="olt")
    fan_units = relationship("OltFanUnit", back_populates="olt")
    shelves = relationship("OltShelf", back_populates="olt")
    config_backups = relationship("OltConfigBackup", back_populates="olt")
    tr069_acs_server = relationship("Tr069AcsServer")
    vlans = relationship(
        "Vlan", back_populates="olt_device", foreign_keys="[Vlan.olt_device_id]"
    )
    ip_pools = relationship(
        "IpPool", back_populates="olt_device", foreign_keys="[IpPool.olt_device_id]"
    )

    # Management IP pool relationship
    mgmt_ip_pool = relationship("IpPool", foreign_keys=[mgmt_ip_pool_id])

    @property
    def is_reachable(self) -> bool:
        """UI-friendly property: True if OLT responds to ping."""
        return self.last_ping_ok is True

    @property
    def is_snmp_ok(self) -> bool:
        """True if last SNMP poll succeeded."""
        return self.last_poll_status == PollStatus.success


class OltConfigBackupType(enum.Enum):
    auto = "auto"
    manual = "manual"


class OltLineProfile(Base):
    """Imported OLT-local GPON line profile."""

    __tablename__ = "olt_line_profiles"
    __table_args__ = (
        UniqueConstraint(
            "olt_id",
            "profile_id",
            name="uq_olt_line_profiles_olt_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(160))
    binding_count: Mapped[int] = mapped_column(Integer, default=0)
    tr069_management_enabled: Mapped[bool | None] = mapped_column(Boolean)
    raw_config: Mapped[str | None] = mapped_column(Text)
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice")


class OltLineProfileGemMapping(Base):
    """Imported GEM mapping rows for an OLT line profile."""

    __tablename__ = "olt_line_profile_gem_mappings"
    __table_args__ = (
        UniqueConstraint(
            "olt_id",
            "line_profile_id",
            "source_key",
            name="uq_olt_line_profile_gem_mappings_source",
        ),
        ForeignKeyConstraint(
            ["olt_id", "line_profile_id"],
            ["olt_line_profiles.olt_id", "olt_line_profiles.profile_id"],
            name="fk_olt_line_profile_gem_mapping_line_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    line_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    gem_index: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_index: Mapped[int | None] = mapped_column(Integer)
    tcont_index: Mapped[int | None] = mapped_column(Integer)
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int | None] = mapped_column(Integer)
    eth_port: Mapped[int | None] = mapped_column(Integer)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_config: Mapped[str | None] = mapped_column(Text)
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice")


class OltServiceProfile(Base):
    """Imported OLT-local GPON service profile."""

    __tablename__ = "olt_service_profiles"
    __table_args__ = (
        UniqueConstraint(
            "olt_id", "profile_id", name="uq_olt_service_profiles_olt_profile"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(160))
    binding_count: Mapped[int] = mapped_column(Integer, default=0)
    ethernet_ports: Mapped[int | None] = mapped_column(Integer)
    voip_ports: Mapped[int | None] = mapped_column(Integer)
    catv_ports: Mapped[int | None] = mapped_column(Integer)
    raw_config: Mapped[str | None] = mapped_column(Text)
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice")


class DeviceGroup(Base):
    """Operator-defined group for batch device operations and audits."""

    __tablename__ = "device_groups"
    __table_args__ = (
        UniqueConstraint("name", name="uq_device_groups_name"),
        Index("ix_device_groups_kind_active", "kind", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    criteria: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    members = relationship(
        "DeviceGroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
    )


class DeviceGroupMember(Base):
    """Device membership row for a device group."""

    __tablename__ = "device_group_members"
    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "device_type",
            "device_id",
            name="uq_device_group_members_group_device",
        ),
        Index("ix_device_group_members_group_type", "group_id", "device_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_type: Mapped[str] = mapped_column(String(40), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    added_by: Mapped[str | None] = mapped_column(String(120))
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    group = relationship("DeviceGroup", back_populates="members")


class OltProfileBundle(Base):
    """Persisted offer-to-OLT profile bundle generated from catalog speed data."""

    __tablename__ = "olt_profile_bundles"
    __table_args__ = (
        UniqueConstraint(
            "olt_id",
            "offer_id",
            name="uq_olt_profile_bundles_olt_offer",
        ),
        Index("ix_olt_profile_bundles_olt_active", "olt_id", "is_active"),
        Index("ix_olt_profile_bundles_checksum", "checksum"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    download_kbps: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_kbps: Mapped[int] = mapped_column(Integer, nullable=False)
    dba_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    download_traffic_table_id: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_traffic_table_id: Mapped[int] = mapped_column(Integer, nullable=False)
    line_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    service_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    wan_profile_id: Mapped[int | None] = mapped_column(Integer)
    tr069_profile_id: Mapped[int | None] = mapped_column(Integer)
    gem_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tcont_id: Mapped[int] = mapped_column(Integer, nullable=False)
    command_plan: Mapped[dict | None] = mapped_column(JSON)
    drift_status: Mapped[str] = mapped_column(String(40), default="pending")
    drift_details: Mapped[dict | None] = mapped_column(JSON)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt = relationship("OLTDevice")
    offer = relationship("CatalogOffer")


class OltProfileSyncTask(Base):
    """Reviewable task for syncing a catalog offer to an OLT profile bundle."""

    __tablename__ = "olt_profile_sync_tasks"
    __table_args__ = (
        Index("ix_olt_profile_sync_tasks_status", "status"),
        Index(
            "ix_olt_profile_sync_tasks_olt_offer_status", "olt_id", "offer_id", "status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_offers.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    trigger: Mapped[str] = mapped_column(String(80), default="manual", nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(120))
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preview_payload: Mapped[dict | None] = mapped_column(JSON)
    result_payload: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt = relationship("OLTDevice")
    offer = relationship("CatalogOffer")


class OltOntRegistration(Base):
    """Imported ONT registration observed on an OLT."""

    __tablename__ = "olt_ont_registrations"
    __table_args__ = (
        UniqueConstraint(
            "olt_id",
            "fsp",
            "ont_id_on_olt",
            name="uq_olt_ont_registrations_olt_fsp_ont",
        ),
        Index(
            "uq_olt_ont_registrations_active_serial",
            "olt_id",
            "serial_number",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        ForeignKeyConstraint(
            ["olt_id", "line_profile_id"],
            ["olt_line_profiles.olt_id", "olt_line_profiles.profile_id"],
            name="fk_olt_ont_registration_line_profile",
        ),
        ForeignKeyConstraint(
            ["olt_id", "service_profile_id"],
            ["olt_service_profiles.olt_id", "olt_service_profiles.profile_id"],
            name="fk_olt_ont_registration_service_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fsp: Mapped[str] = mapped_column(String(32), nullable=False)
    ont_id_on_olt: Mapped[int] = mapped_column(Integer, nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(120))
    equipment_id: Mapped[str | None] = mapped_column(String(120))
    line_profile_id: Mapped[int | None] = mapped_column(Integer)
    service_profile_id: Mapped[int | None] = mapped_column(Integer)
    tr069_profile_id: Mapped[int | None] = mapped_column(Integer)
    match_state: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text)
    raw_config: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice")


class OltOnuTypeProfileMapping(Base):
    """Imported per-OLT mapping from equipment ID to provisioning behavior."""

    __tablename__ = "olt_onu_type_profile_mappings"
    __table_args__ = (
        UniqueConstraint(
            "olt_id",
            "equipment_id",
            name="uq_olt_onu_type_profile_mappings_olt_equipment",
        ),
        ForeignKeyConstraint(
            ["olt_id", "line_profile_id"],
            ["olt_line_profiles.olt_id", "olt_line_profiles.profile_id"],
            name="fk_olt_onu_mapping_line_profile",
        ),
        ForeignKeyConstraint(
            ["olt_id", "service_profile_id"],
            ["olt_service_profiles.olt_id", "olt_service_profiles.profile_id"],
            name="fk_olt_onu_mapping_service_profile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    equipment_id: Mapped[str] = mapped_column(String(120), nullable=False)
    onu_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("onu_types.id", ondelete="SET NULL")
    )
    line_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    service_profile_id: Mapped[int] = mapped_column(Integer, nullable=False)
    wan_provisioning_mode: Mapped[str | None] = mapped_column(String(40))
    internet_config_ip_index: Mapped[int | None] = mapped_column(Integer)
    wan_config_profile_id: Mapped[int | None] = mapped_column(Integer)
    pppoe_wcd_index: Mapped[int | None] = mapped_column(Integer)
    mgmt_wcd_index: Mapped[int | None] = mapped_column(Integer)
    voip_wcd_index: Mapped[int | None] = mapped_column(Integer)
    primary_wan_service: Mapped[str | None] = mapped_column(String(40))
    source_registration_count: Mapped[int] = mapped_column(Integer, default=0)
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice")
    onu_type = relationship("OnuType")


class OltConfigBackup(Base):
    """OLT running-config backup snapshot."""

    __tablename__ = "olt_config_backups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False, index=True
    )
    backup_type: Mapped[OltConfigBackupType] = mapped_column(
        Enum(OltConfigBackupType, name="oltconfigbackuptype", create_constraint=False),
        nullable=False,
        default=OltConfigBackupType.auto,
    )
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    olt = relationship("OLTDevice", back_populates="config_backups")


class OltShelf(Base):
    __tablename__ = "olt_shelves"
    __table_args__ = (
        UniqueConstraint(
            "olt_id", "shelf_number", name="uq_olt_shelves_olt_shelf_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    shelf_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[HardwareUnitStatus | None] = mapped_column(
        Enum(HardwareUnitStatus, values_callable=lambda x: [e.value for e in x]),
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt = relationship("OLTDevice", back_populates="shelves")
    cards = relationship("OltCard", back_populates="shelf")


class OltCard(Base):
    __tablename__ = "olt_cards"
    __table_args__ = (
        UniqueConstraint(
            "shelf_id", "slot_number", name="uq_olt_cards_shelf_slot_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    shelf_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_shelves.id"), nullable=False
    )
    slot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    card_type: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    hardware_version: Mapped[str | None] = mapped_column(String(80))
    firmware_version: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[HardwareUnitStatus | None] = mapped_column(
        Enum(HardwareUnitStatus, values_callable=lambda x: [e.value for e in x]),
    )
    temperature: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    shelf = relationship("OltShelf", back_populates="cards")
    ports = relationship("OltCardPort", back_populates="card")


class OltCardPort(Base):
    __tablename__ = "olt_card_ports"
    __table_args__ = (
        UniqueConstraint(
            "card_id", "port_number", name="uq_olt_card_ports_card_port_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_cards.id"), nullable=False
    )
    port_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    port_type: Mapped[OltPortType] = mapped_column(
        Enum(OltPortType), default=OltPortType.pon
    )
    status: Mapped[HardwareUnitStatus | None] = mapped_column(
        Enum(HardwareUnitStatus, values_callable=lambda x: [e.value for e in x]),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    card = relationship("OltCard", back_populates="ports")
    pon_port = relationship("PonPort", back_populates="olt_card_port", uselist=False)
    sfp_modules = relationship("OltSfpModule", back_populates="olt_card_port")


class PonPort(Base):
    __tablename__ = "pon_ports"
    __table_args__ = (UniqueConstraint("olt_id", "name", name="uq_pon_ports_olt_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    olt_card_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_card_ports.id")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    port_number: Mapped[int | None] = mapped_column(Integer)
    # Structural identity, owned exclusively by network.pon_port_identity.
    # `name` is display text; these are what identify the port, and what the
    # partial unique indexes constrain. Never write them from a UI path.
    # identity_frame NULL is a positive statement that the platform has no
    # frame (single-box OLT), not "unknown"; identity_port NULL is "identity
    # not established".
    identity_frame: Mapped[int | None] = mapped_column(Integer)
    identity_slot: Mapped[int | None] = mapped_column(Integer)
    identity_port: Mapped[int | None] = mapped_column(Integer)
    max_ont_capacity: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    admin_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt = relationship("OLTDevice", back_populates="pon_ports")
    olt_card_port = relationship("OltCardPort", back_populates="pon_port")
    ont_assignments = relationship("OntAssignment", back_populates="pon_port")
    splitter_link = relationship(
        "PonPortSplitterLink", back_populates="pon_port", uselist=False
    )

    @property
    def card_id(self) -> uuid.UUID | None:
        """Backwards-compat: expose card_id via the linked OLT card port."""
        if self.olt_card_port:
            return self.olt_card_port.card_id
        return None


class OltPowerUnit(Base):
    __tablename__ = "olt_power_units"
    __table_args__ = (
        UniqueConstraint("olt_id", "slot", name="uq_olt_power_units_olt_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    slot: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[HardwareUnitStatus | None] = mapped_column(
        Enum(HardwareUnitStatus, values_callable=lambda x: [e.value for e in x]),
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt = relationship("OLTDevice", back_populates="power_units")


class OltFanUnit(Base):
    __tablename__ = "olt_fan_units"
    __table_args__ = (
        UniqueConstraint("olt_id", "slot", name="uq_olt_fan_units_olt_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id"), nullable=False
    )
    slot: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[HardwareUnitStatus | None] = mapped_column(
        Enum(HardwareUnitStatus, values_callable=lambda x: [e.value for e in x]),
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt = relationship("OLTDevice", back_populates="fan_units")


class OltSfpModule(Base):
    __tablename__ = "olt_sfp_modules"
    __table_args__ = (
        UniqueConstraint(
            "olt_card_port_id", "serial_number", name="uq_olt_sfp_modules_port_serial"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_card_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_card_ports.id"), nullable=False
    )
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    wavelength_nm: Mapped[int | None] = mapped_column(Integer)
    rx_power_dbm: Mapped[float | None] = mapped_column(Float)
    tx_power_dbm: Mapped[float | None] = mapped_column(Float)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt_card_port = relationship("OltCardPort", back_populates="sfp_modules")


class OntUnit(Base):
    __tablename__ = "ont_units"
    __table_args__ = (
        UniqueConstraint(
            "olt_device_id",
            "serial_number",
            name="uq_ont_units_olt_serial_number",
        ),
        Index(
            "uq_ont_units_olt_external_id",
            "olt_device_id",
            "external_id",
            unique=True,
            postgresql_where=text(
                "olt_device_id IS NOT NULL AND external_id IS NOT NULL"
            ),
        ),
        # Stable UISP device id — the uisp_sync upsert key (UFiber ONUs).
        Index(
            "uq_ont_units_uisp_device_id",
            "uisp_device_id",
            unique=True,
            postgresql_where=text("uisp_device_id IS NOT NULL"),
        ),
        Index(
            "uq_ont_units_active_splitter_port",
            "splitter_port_id",
            unique=True,
            postgresql_where=text("is_active AND splitter_port_id IS NOT NULL"),
            sqlite_where=text("is_active AND splitter_port_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    serial_number: Mapped[str] = mapped_column(String(120), nullable=False)
    vendor_serial_number: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    vendor: Mapped[str | None] = mapped_column(String(120))
    hardware_version: Mapped[str | None] = mapped_column(String(120))
    firmware_version: Mapped[str | None] = mapped_column(String(120))
    software_version: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Optical signal monitoring fields
    onu_rx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    olt_rx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    distance_meters: Mapped[int | None] = mapped_column(Integer)

    # ONT DDM health telemetry (SNMP-polled)
    onu_tx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    ont_temperature_c: Mapped[float | None] = mapped_column(Float)
    ont_voltage_v: Mapped[float | None] = mapped_column(Float)
    ont_bias_current_ma: Mapped[float | None] = mapped_column(Float)

    signal_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    olt_status: Mapped[OnuOnlineStatus] = mapped_column(
        Enum(OnuOnlineStatus, name="onuonlinestatus", create_constraint=False),
        default=OnuOnlineStatus.offline,
        server_default="offline",
    )
    olt_status_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offline_reason: Mapped[OnuOfflineReason | None] = mapped_column(
        Enum(OnuOfflineReason, name="onuofflinereason", create_constraint=False),
    )
    acs_last_inform_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    onu_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("onu_types.id")
    )
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id")
    )
    pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id", ondelete="SET NULL"), index=True
    )
    pon_type: Mapped[PonType | None] = mapped_column(
        Enum(PonType, name="pontype", create_constraint=False),
    )
    gpon_channel: Mapped[GponChannel | None] = mapped_column(
        Enum(GponChannel, name="gponchannel", create_constraint=False),
    )
    board: Mapped[str | None] = mapped_column(String(60))
    port: Mapped[str | None] = mapped_column(String(60))
    onu_mode: Mapped[OnuMode | None] = mapped_column(
        Enum(OnuMode, name="onumode", create_constraint=False),
    )
    user_vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vlans.id")
    )
    splitter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitters.id")
    )
    splitter_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id")
    )
    download_speed_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speed_profiles.id")
    )
    upload_speed_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("speed_profiles.id")
    )
    name: Mapped[str | None] = mapped_column(String(200))
    address_or_comment: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(120))
    use_gps: Mapped[bool] = mapped_column(Boolean, default=False)
    gps_latitude: Mapped[float | None] = mapped_column(Float)
    gps_longitude: Mapped[float | None] = mapped_column(Float)
    # Observed/runtime identity and access metrics (SNMP/TR-069 sourced)
    mac_address: Mapped[str | None] = mapped_column(String(64))
    observed_wan_ip: Mapped[str | None] = mapped_column(String(64))
    observed_pppoe_status: Mapped[str | None] = mapped_column(String(60))
    observed_lan_mode: Mapped[str | None] = mapped_column(String(60))
    observed_wifi_clients: Mapped[int | None] = mapped_column(Integer)
    observed_lan_hosts: Mapped[int | None] = mapped_column(Integer)
    observed_runtime_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    desired_config: Mapped[dict | None] = mapped_column(
        # JSONB rather than JSON so the ``#-`` / ``||`` / ``->`` operators
        # used by the desired_config migration chain work uniformly. SA's
        # ``dict``-mapped surface is the same for both column types.
        JSONB,
        default=dict,
        doc="Per-ONT desired configuration intent. OLT defaults are resolved from OltConfigPack.",
    )
    tr069_last_snapshot: Mapped[dict | None] = mapped_column(JSON)
    tr069_last_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    olt_observed_snapshot: Mapped[dict | None] = mapped_column(JSON)
    olt_observed_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    tr069_acs_server_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tr069_acs_servers.id")
    )
    desired_tr069_profile_id: Mapped[int | None] = mapped_column(
        Integer,
        doc="Per-ONT OLT TR-069 profile override; NULL inherits OltConfigPack.",
    )
    voip_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    provisioning_status: Mapped[OntProvisioningStatus | None] = mapped_column(
        Enum(
            OntProvisioningStatus,
            name="ontprovisioningstatus_v2",
            create_constraint=False,
        ),
    )
    authorization_status: Mapped[OntAuthorizationStatus | None] = mapped_column(
        Enum(
            OntAuthorizationStatus,
            name="ontauthorizationstatus",
            create_constraint=False,
        ),
    )
    last_provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # Async verification tracking
    last_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        doc="When provisioning commands were last applied to OLT",
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        doc="When provisioning state was last verified against OLT",
    )
    verification_status: Mapped[str | None] = mapped_column(
        String(20),
        doc="pending, verified, drift_detected, failed",
    )

    # TR-069 data model root (detected from GenieACS device record)
    tr069_data_model: Mapped[str | None] = mapped_column(
        String(40), doc="'Device' (TR-181) or 'InternetGatewayDevice' (TR-098)"
    )
    # Sync tracking — which external source last modified this ONT, and when
    last_sync_source: Mapped[str | None] = mapped_column(String(40))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # UISP topology sync: stable UISP device id (UFiber ONUs); NULL otherwise.
    uisp_device_id: Mapped[str | None] = mapped_column(String(64))

    # Reconciler bookkeeping (see app/services/network/reconcile/). These columns
    # are set exclusively by reconcile_ont; UI write paths must never mutate them
    # directly. last_reconcile_started_at lets a fresh reconcile detect a crashed
    # prior pass: if it acquires the SELECT FOR UPDATE lock and finds
    # sync_status='reconciling', the previous reconcile died and the row is
    # cleared to out_of_sync with a crash error.
    sync_status: Mapped[OntSyncStatus] = mapped_column(
        Enum(OntSyncStatus, name="ontsyncstatus", create_constraint=False),
        default=OntSyncStatus.synced,
        server_default="synced",
        nullable=False,
        index=True,
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_reconcile_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_sweep_unreachable: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    assignments = relationship("OntAssignment", back_populates="ont_unit")
    zone = relationship("NetworkZone", back_populates="ont_units")
    onu_type = relationship("OnuType", back_populates="ont_units")
    olt_device = relationship("OLTDevice")
    pon_port = relationship("PonPort")
    user_vlan = relationship("Vlan", foreign_keys=[user_vlan_id])
    splitter = relationship("Splitter")
    splitter_port_rel = relationship("SplitterPort")
    download_speed_profile = relationship(
        "SpeedProfile", foreign_keys=[download_speed_profile_id]
    )
    upload_speed_profile = relationship(
        "SpeedProfile", foreign_keys=[upload_speed_profile_id]
    )
    tr069_acs_server = relationship("Tr069AcsServer")
    # 1:1 observation row holding OLT + ACS-side observed fields. Sweeper and
    # sync reconciles both upsert this; the planner reads it. Cascade-deleted
    # so removing an ONT cleans up its observation.
    observation = relationship(
        "OntObservation",
        back_populates="ont_unit",
        cascade="all, delete-orphan",
        uselist=False,
    )
    wan_service_instances = relationship(
        "OntWanServiceInstance",
        back_populates="ont",
        cascade="all, delete-orphan",
        order_by="OntWanServiceInstance.priority",
    )
    provisioning_events = relationship(
        "OntProvisioningEvent",
        back_populates="ont",
        order_by="OntProvisioningEvent.created_at",
    )

    def _desired_section(self, section: str) -> dict:
        config = self.desired_config if isinstance(self.desired_config, dict) else {}
        value = config.get(section)
        return value if isinstance(value, dict) else {}

    def _get_desired_value(self, section: str, key: str):
        return self._desired_section(section).get(key)

    def _set_desired_value(self, section: str, key: str, value) -> None:
        config = dict(self.desired_config or {})
        section_values = dict(config.get(section) or {})
        if value in (None, ""):
            section_values.pop(key, None)
        else:
            section_values[key] = value
        if section_values:
            config[section] = section_values
        else:
            config.pop(section, None)
        self.desired_config = config

    @property
    def pppoe_username(self):
        return self._get_desired_value("wan", "pppoe_username")

    @pppoe_username.setter
    def pppoe_username(self, value) -> None:
        self._set_desired_value("wan", "pppoe_username", value)

    @property
    def pppoe_password(self):
        return self._get_desired_value("wan", "pppoe_password")

    @pppoe_password.setter
    def pppoe_password(self, value) -> None:
        self._set_desired_value("wan", "pppoe_password", value)

    @property
    def mgmt_ip_address(self):
        return self._get_desired_value("management", "ip_address")

    @mgmt_ip_address.setter
    def mgmt_ip_address(self, value) -> None:
        self._set_desired_value("management", "ip_address", value)


class OntProvisioningEvent(Base):
    """Append-only audit trail for ONT provisioning step outcomes."""

    __tablename__ = "ont_provisioning_events"
    __table_args__ = (
        Index("ix_ont_provisioning_events_ont_unit", "ont_unit_id"),
        Index("ix_ont_provisioning_events_step_name", "step_name"),
        Index("ix_ont_provisioning_events_action", "action"),
        Index("ix_ont_provisioning_events_status", "status"),
        Index("ix_ont_provisioning_events_correlation_key", "correlation_key"),
        Index("ix_ont_provisioning_events_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id"), nullable=False
    )
    step_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[OntProvisioningEventStatus] = mapped_column(
        Enum(
            OntProvisioningEventStatus,
            name="ontprovisioningeventstatus",
            create_constraint=False,
        ),
        nullable=False,
    )
    message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    event_data: Mapped[dict | None] = mapped_column(JSON)
    compensation_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    correlation_key: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    ont = relationship("OntUnit", back_populates="provisioning_events")


class BulkProvisioningRun(Base):
    """Audit record for one bulk ONT provisioning operation."""

    __tablename__ = "bulk_provisioning_runs"
    __table_args__ = (
        Index("ix_bulk_provisioning_runs_status", "status"),
        Index("ix_bulk_provisioning_runs_correlation_key", "correlation_key"),
        Index("ix_bulk_provisioning_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_provisioning_profiles.id"), nullable=True
    )
    status: Mapped[BulkProvisioningRunStatus] = mapped_column(
        Enum(
            BulkProvisioningRunStatus,
            name="bulkprovisioningrunstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=BulkProvisioningRunStatus.pending,
    )
    correlation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(128))
    max_workers: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    run_metadata: Mapped[dict | None] = mapped_column(JSON)

    profile = relationship("OntProvisioningProfile")
    items = relationship(
        "BulkProvisioningItem",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="BulkProvisioningItem.created_at",
    )


class BulkProvisioningItem(Base):
    """Per-ONT audit record inside a bulk provisioning run."""

    __tablename__ = "bulk_provisioning_items"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "requested_ont_id",
            name="uq_bulk_provisioning_items_run_requested_ont",
        ),
        Index("ix_bulk_provisioning_items_run", "run_id"),
        Index("ix_bulk_provisioning_items_ont_unit", "ont_unit_id"),
        Index("ix_bulk_provisioning_items_status", "status"),
        Index("ix_bulk_provisioning_items_correlation_key", "correlation_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bulk_provisioning_runs.id"), nullable=False
    )
    requested_ont_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id"), nullable=True
    )
    status: Mapped[BulkProvisioningItemStatus] = mapped_column(
        Enum(
            BulkProvisioningItemStatus,
            name="bulkprovisioningitemstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=BulkProvisioningItemStatus.pending,
    )
    correlation_key: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    result_data: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    run = relationship("BulkProvisioningRun", back_populates="items")
    ont = relationship("OntUnit")


class OntAssignment(Base):
    __tablename__ = "ont_assignments"
    # Partial unique index for PostgreSQL only: ensures only one active assignment
    # per ONT. SQLite ignores postgresql_where, creating a full unique constraint
    # which breaks tests that create multiple assignments per ONT. Real deployments
    # use PostgreSQL which respects the partial index.
    __table_args__ = (
        Index(
            "ix_ont_assignments_active_unit",
            "ont_unit_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id"), nullable=False
    )
    pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id"), nullable=True
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    work_order_mirror_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_order.id", ondelete="SET NULL")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(64))
    # Map 'active' property to 'is_active' column in database
    active: Mapped[bool] = mapped_column("is_active", Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    # -------------------------------------------------------------------------
    # Service Configuration (subscriber's service settings for this ONT)
    # -------------------------------------------------------------------------
    wan_mode: Mapped[OnuMode | None] = mapped_column(
        Enum(OnuMode, name="onumode", create_constraint=False),
        default=OnuMode.routing,
        doc="WAN mode: routing (NAT) or bridging (transparent)",
    )
    ip_mode: Mapped[MgmtIpMode | None] = mapped_column(
        Enum(MgmtIpMode, name="mgmtipmode", create_constraint=False),
        default=MgmtIpMode.dhcp,
        doc="IP assignment mode: dhcp or static_ip",
    )
    static_ip: Mapped[str | None] = mapped_column(
        String(64), doc="Static IP address (when ip_mode=static_ip)"
    )
    static_gateway: Mapped[str | None] = mapped_column(
        String(64), doc="Static gateway (when ip_mode=static_ip)"
    )
    static_subnet: Mapped[str | None] = mapped_column(
        String(64), doc="Static subnet mask (when ip_mode=static_ip)"
    )
    static_dns: Mapped[str | None] = mapped_column(
        String(200), doc="Static DNS servers (when ip_mode=static_ip)"
    )
    pppoe_username: Mapped[str | None] = mapped_column(
        String(200), doc="PPPoE username for subscriber"
    )
    pppoe_password: Mapped[str | None] = mapped_column(
        String(512), doc="PPPoE password (encrypted)"
    )
    wifi_ssid: Mapped[str | None] = mapped_column(
        String(64), doc="WiFi SSID for subscriber"
    )
    wifi_password: Mapped[str | None] = mapped_column(
        String(512), doc="WiFi password (encrypted)"
    )

    # -------------------------------------------------------------------------
    # Management IP Configuration (ONT-side management IP)
    # -------------------------------------------------------------------------
    mgmt_ip_mode: Mapped[MgmtIpMode | None] = mapped_column(
        Enum(MgmtIpMode, name="mgmtipmode", create_constraint=False),
        default=MgmtIpMode.inactive,
        doc="Management IP mode: inactive, dhcp, or static_ip",
    )
    mgmt_ip_address: Mapped[str | None] = mapped_column(
        String(64), doc="Static management IP (when mgmt_ip_mode=static_ip)"
    )
    mgmt_subnet: Mapped[str | None] = mapped_column(
        String(64), doc="Static management subnet (when mgmt_ip_mode=static_ip)"
    )
    mgmt_gateway: Mapped[str | None] = mapped_column(
        String(64), doc="Static management gateway (when mgmt_ip_mode=static_ip)"
    )
    lan_ip: Mapped[str | None] = mapped_column(String(64))
    lan_subnet: Mapped[str | None] = mapped_column(String(64))
    lan_dhcp_enabled: Mapped[bool | None] = mapped_column(Boolean)
    lan_dhcp_start: Mapped[str | None] = mapped_column(String(64))
    lan_dhcp_end: Mapped[str | None] = mapped_column(String(64))
    wifi_enabled: Mapped[bool | None] = mapped_column(Boolean)
    wifi_security_mode: Mapped[str | None] = mapped_column(String(40))
    wifi_channel: Mapped[str | None] = mapped_column(String(10))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    ont_unit = relationship("OntUnit", back_populates="assignments")
    pon_port = relationship("PonPort", back_populates="ont_assignments")
    subscriber = relationship("Subscriber", back_populates="ont_assignments")
    subscription = relationship("Subscription")
    work_order_mirror = relationship("WorkOrder")
    service_address = relationship("Address")


class ForwardingObservation(Base):
    """A MAC-forwarding observation harvested from network hardware.

    General "hop-1" position -> MAC record: one row per (MAC, position) learned
    on a device's forwarding table. The Huawei OLT harvester
    (``app/services/topology/olt_mac_harvest.py``) parses ``display mac-address
    port <F/S/P>`` and emits one row per learned customer/router MAC, mapping it
    to the exact PON port (F/S/P) and ONT-ID (the VPI column). This is an
    ephemeral, periodically-refreshed table (rows are aged out) and the reusable
    foundation for ONT<->subscriber drift detection today and bridge-mode
    UF-Nano / wireless linking later. It is read-only toward assignments.
    """

    __tablename__ = "forwarding_observations"
    __table_args__ = (
        # Upsert identity: one row per learned MAC at a given (OLT, ONT-ID)
        # position. Re-harvesting refreshes observed_at/vlan/pon_port in place.
        UniqueConstraint(
            "olt_device_id",
            "mac",
            "ont_id_on_olt",
            name="uq_forwarding_observations_olt_mac_ont",
        ),
        Index("ix_forwarding_observations_mac", "mac"),
        Index("ix_forwarding_observations_observed_at", "observed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id", ondelete="CASCADE")
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id", ondelete="SET NULL")
    )
    pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id", ondelete="SET NULL")
    )
    # The ONT-ID on the OLT for the learned MAC (the VPI column in Huawei's
    # ``display mac-address port`` output).
    ont_id_on_olt: Mapped[int | None] = mapped_column(Integer)
    # Canonical MAC (uppercase colon-separated, matching subscriptions.mac_address).
    mac: Mapped[str] = mapped_column(Text, nullable=False)
    vlan: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    source: Mapped[str] = mapped_column(Text, nullable=False, default="huawei_olt_mac")


class OntSignalObservation(Base):
    """Append-only per-ONT status + Rx snapshot — the splice-inference substrate.

    The OLT sees every ONT's ``olt_status`` and ``onu_rx_signal_dbm``, but the
    passive splitter's internal sub-split branch/splice is unpollable and the
    manual ``SplitterPortAssignment`` plant records rot (design §4). This table
    keeps the TIME SERIES those live ``ont_units`` columns lack: one row per ONT
    per collection sweep. Splice inference (``app/services/topology/
    splice_inference.py``, design §6) derives the hidden sub-PON topology from it
    — ONTs that repeatedly go dark together (co-failure) or droop by the same dB
    (correlated Rx) share a branch.

    Append-only + periodically pruned (never updated in place); a snapshot is a
    fact at ``observed_at``. TODO(retention): prune rows older than the inference
    window (~90d) in the collector task once volume warrants it.
    """

    __tablename__ = "ont_signal_observations"
    __table_args__ = (
        Index(
            "ix_ont_signal_observations_ont_observed",
            "ont_unit_id",
            "observed_at",
        ),
        Index(
            "ix_ont_signal_observations_pon_observed",
            "pon_port_id",
            "observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="CASCADE"),
        nullable=False,
    )
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id", ondelete="SET NULL")
    )
    pon_port_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id", ondelete="SET NULL")
    )
    olt_status: Mapped[OnuOnlineStatus] = mapped_column(
        Enum(OnuOnlineStatus, name="onuonlinestatus", create_constraint=False),
        nullable=False,
    )
    rx_signal_dbm: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )


class NetworkZone(Base):
    """Geographic zone for organizing network infrastructure."""

    __tablename__ = "network_zones"
    __table_args__ = (UniqueConstraint("name", name="uq_network_zones_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    # Typed geographic binding: a zone may declare the GeoArea it belongs to.
    # Child zones without a binding inherit through the parent chain.
    geo_area_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "geo_areas.id",
            name="fk_network_zones_geo_area_id_geo_areas",
            ondelete="RESTRICT",
        ),
        index=True,
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    parent = relationship(
        "NetworkZone", remote_side="NetworkZone.id", backref="children"
    )
    ont_units = relationship("OntUnit", back_populates="zone")
    splitters = relationship("Splitter", back_populates="zone")
    fdh_cabinets = relationship("FdhCabinet", back_populates="zone")


class FdhCabinet(Base):
    __tablename__ = "fdh_cabinets"
    __table_args__ = (UniqueConstraint("code", name="uq_fdh_cabinets_code"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(80))
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("region_zones.id")
    )
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    splitters = relationship("Splitter", back_populates="fdh")
    region = relationship("RegionZone")
    zone = relationship("NetworkZone", back_populates="fdh_cabinets")


class Splitter(Base):
    __tablename__ = "splitters"
    __table_args__ = (
        UniqueConstraint("fdh_id", "name", name="uq_splitters_fdh_name"),
        CheckConstraint(
            "insertion_loss_db IS NULL OR "
            "(insertion_loss_db >= 0 AND insertion_loss_db <= 100)",
            name="ck_splitters_insertion_loss_db",
        ),
        CheckConstraint(
            "NOT is_active OR (input_ports > 0 AND output_ports > 0 "
            "AND splitter_ratio IS NOT NULL "
            "AND splitter_ratio = CAST(input_ports AS VARCHAR) || ':' || "
            "CAST(output_ports AS VARCHAR))",
            name="ck_splitters_active_declared_capacity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    fdh_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fdh_cabinets.id")
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    splitter_ratio: Mapped[str | None] = mapped_column(String(40))
    insertion_loss_db: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 3), nullable=True
    )
    input_ports: Mapped[int] = mapped_column(Integer, default=1)
    output_ports: Mapped[int] = mapped_column(Integer, default=8)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    fdh = relationship("FdhCabinet", back_populates="splitters")
    ports = relationship("SplitterPort", back_populates="splitter")
    zone = relationship("NetworkZone", back_populates="splitters")


class SplitterPort(Base):
    __tablename__ = "splitter_ports"
    __table_args__ = (
        UniqueConstraint(
            "splitter_id", "port_number", name="uq_splitter_ports_splitter_port_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitters.id"), nullable=False
    )
    port_number: Mapped[int] = mapped_column(Integer, nullable=False)
    port_type: Mapped[SplitterPortType] = mapped_column(
        Enum(SplitterPortType), default=SplitterPortType.output
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    splitter = relationship("Splitter", back_populates="ports")
    assignments = relationship("SplitterPortAssignment", back_populates="splitter_port")
    pon_links = relationship("PonPortSplitterLink", back_populates="splitter_port")


class SplitterPortAssignment(Base):
    __tablename__ = "splitter_port_assignments"
    __table_args__ = (
        UniqueConstraint(
            "splitter_port_id",
            "active",
            name="uq_splitter_port_assignments_port_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    splitter_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id"), nullable=False
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("addresses.id")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    splitter_port = relationship("SplitterPort", back_populates="assignments")
    subscriber = relationship("Subscriber")
    service_address = relationship("Address")


class FiberStrand(Base):
    __tablename__ = "fiber_strands"
    __table_args__ = (
        UniqueConstraint(
            "cable_name", "strand_number", name="uq_fiber_strands_cable_strand"
        ),
        UniqueConstraint(
            "segment_id", "strand_number", name="uq_fiber_strands_segment_strand"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cable_name: Mapped[str] = mapped_column(String(160), nullable=False)
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("fiber_segments.id", ondelete="RESTRICT"),
        nullable=True,
        comment="Exact cable-segment identity; cable_name is display metadata only",
    )
    strand_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[FiberStrandStatus] = mapped_column(
        Enum(FiberStrandStatus), default=FiberStrandStatus.available
    )
    upstream_type: Mapped[FiberEndpointType | None] = mapped_column(
        Enum(FiberEndpointType)
    )
    upstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    downstream_type: Mapped[FiberEndpointType | None] = mapped_column(
        Enum(FiberEndpointType)
    )
    downstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    splices_from = relationship(
        "FiberSplice",
        back_populates="from_strand",
        foreign_keys="FiberSplice.from_strand_id",
    )
    splices_to = relationship(
        "FiberSplice",
        back_populates="to_strand",
        foreign_keys="FiberSplice.to_strand_id",
    )
    segments = relationship(
        "FiberSegment",
        foreign_keys="FiberSegment.fiber_strand_id",
        back_populates="fiber_strand",
    )
    segment = relationship(
        "FiberSegment",
        foreign_keys=[segment_id],
        back_populates="strands",
    )


class FiberSpliceClosure(Base):
    __tablename__ = "fiber_splice_closures"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    splices = relationship("FiberSplice", back_populates="closure")
    trays = relationship("FiberSpliceTray", back_populates="closure")


class FiberAccessPoint(Base):
    """Fiber Access Point (NAP/FAP) for customer drop connections."""

    __tablename__ = "fiber_access_points"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    access_point_type: Mapped[str | None] = mapped_column(String(60))
    placement: Mapped[str | None] = mapped_column(String(60))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    street: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    county: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class FiberSpliceTray(Base):
    __tablename__ = "fiber_splice_trays"
    __table_args__ = (
        UniqueConstraint(
            "closure_id", "tray_number", name="uq_fiber_splice_trays_closure_tray"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_closures.id"), nullable=False
    )
    tray_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str | None] = mapped_column(String(160))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    closure = relationship("FiberSpliceClosure", back_populates="trays")
    splices = relationship("FiberSplice", back_populates="tray")


class FiberSplice(Base):
    __tablename__ = "fiber_splices"
    __table_args__ = (
        UniqueConstraint(
            "from_strand_id", "to_strand_id", name="uq_fiber_splices_from_to"
        ),
        UniqueConstraint("tray_id", "position", name="uq_fiber_splices_tray_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    closure_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_closures.id"), nullable=True
    )
    from_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id"), nullable=True
    )
    to_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id"), nullable=True
    )
    tray_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_splice_trays.id")
    )
    position: Mapped[int | None] = mapped_column(Integer)
    splice_type: Mapped[str | None] = mapped_column(String(80))
    loss_db: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    closure = relationship("FiberSpliceClosure", back_populates="splices")
    tray = relationship("FiberSpliceTray", back_populates="splices")
    from_strand = relationship(
        "FiberStrand", back_populates="splices_from", foreign_keys=[from_strand_id]
    )
    to_strand = relationship(
        "FiberStrand", back_populates="splices_to", foreign_keys=[to_strand_id]
    )


class FiberTerminationPoint(Base):
    __tablename__ = "fiber_termination_points"
    __table_args__ = (
        Index(
            "uq_fiber_termination_active_endpoint",
            "endpoint_type",
            "ref_id",
            unique=True,
            postgresql_where=text("is_active AND ref_id IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND ref_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str | None] = mapped_column(String(160))
    endpoint_type: Mapped[ODNEndpointType] = mapped_column(
        Enum(ODNEndpointType), default=ODNEndpointType.other
    )
    ref_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    segments_from = relationship(
        "FiberSegment",
        back_populates="from_point",
        foreign_keys="FiberSegment.from_point_id",
    )
    segments_to = relationship(
        "FiberSegment",
        back_populates="to_point",
        foreign_keys="FiberSegment.to_point_id",
    )


class FiberCableType(enum.Enum):
    """Types of fiber optic cables."""

    single_mode = "single_mode"
    multi_mode = "multi_mode"
    armored = "armored"
    aerial = "aerial"
    underground = "underground"
    direct_buried = "direct_buried"


class FiberColorStandard(enum.Enum):
    """Declared fiber color-code standard for a cable's construction.

    Persisted as a checked string column; this enum is the typed vocabulary
    for services. Extending the vocabulary requires a migration updating
    ``ck_fiber_segments_color_standard_known``.
    """

    eia_tia_598 = "eia_tia_598"


class FiberSegment(Base):
    __tablename__ = "fiber_segments"
    __table_args__ = (
        UniqueConstraint("name", name="uq_fiber_segments_name"),
        CheckConstraint(
            "NOT is_active OR (from_point_id IS NOT NULL "
            "AND to_point_id IS NOT NULL AND from_point_id <> to_point_id "
            "AND route_geom IS NOT NULL AND fiber_count IS NOT NULL "
            "AND fiber_count > 0)",
            name="ck_fiber_segments_active_operational_shape",
        ),
        CheckConstraint(
            "fibers_per_tube IS NULL OR fibers_per_tube > 0",
            name="ck_fiber_segments_fibers_per_tube_positive",
        ),
        CheckConstraint(
            "color_standard IS NULL OR color_standard IN ('eia_tia_598')",
            name="ck_fiber_segments_color_standard_known",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    segment_type: Mapped[FiberSegmentType] = mapped_column(
        Enum(FiberSegmentType), default=FiberSegmentType.distribution
    )
    cable_type: Mapped[FiberCableType | None] = mapped_column(
        Enum(FiberCableType), nullable=True
    )
    fiber_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Number of fiber cores in the cable"
    )
    fibers_per_tube: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Loose-tube construction: fiber cores per buffer tube",
    )
    color_standard: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        comment="Declared color-code standard (FiberColorStandard vocabulary)",
    )
    from_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_termination_points.id")
    )
    to_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_termination_points.id")
    )
    fiber_strand_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fiber_strands.id")
    )
    length_m: Mapped[float | None] = mapped_column(Float)
    route_geom = mapped_column(Geometry("LINESTRING", srid=4326), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    from_point = relationship(
        "FiberTerminationPoint",
        foreign_keys=[from_point_id],
        back_populates="segments_from",
    )
    to_point = relationship(
        "FiberTerminationPoint",
        foreign_keys=[to_point_id],
        back_populates="segments_to",
    )
    fiber_strand = relationship(
        "FiberStrand", foreign_keys=[fiber_strand_id], back_populates="segments"
    )
    strands = relationship(
        "FiberStrand",
        foreign_keys="FiberStrand.segment_id",
        back_populates="segment",
    )


class PonPortSplitterLink(Base):
    __tablename__ = "pon_port_splitter_links"
    __table_args__ = (
        UniqueConstraint("pon_port_id", name="uq_pon_port_splitter_links_pon_port"),
        Index(
            "uq_pon_port_splitter_links_active_input",
            "splitter_port_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pon_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pon_ports.id"), nullable=False
    )
    splitter_port_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("splitter_ports.id"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pon_port = relationship("PonPort", back_populates="splitter_link")
    splitter_port = relationship("SplitterPort", back_populates="pon_links")


class OnuType(Base):
    """Hardware catalog entry for ONU/ONT device types."""

    __tablename__ = "onu_types"
    __table_args__ = (UniqueConstraint("name", name="uq_onu_types_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    pon_type: Mapped[PonType] = mapped_column(
        Enum(PonType, name="pontype", create_constraint=False),
        nullable=False,
        default=PonType.gpon,
    )
    gpon_channel: Mapped[GponChannel] = mapped_column(
        Enum(GponChannel, name="gponchannel", create_constraint=False),
        nullable=False,
        default=GponChannel.gpon,
    )
    ethernet_ports: Mapped[int] = mapped_column(Integer, default=0)
    wifi_ports: Mapped[int] = mapped_column(Integer, default=0)
    voip_ports: Mapped[int] = mapped_column(Integer, default=0)
    catv_ports: Mapped[int] = mapped_column(Integer, default=0)
    allow_custom_profiles: Mapped[bool] = mapped_column(Boolean, default=True)
    capability: Mapped[OnuCapability] = mapped_column(
        Enum(OnuCapability, name="onucapability", create_constraint=False),
        nullable=False,
        default=OnuCapability.bridging_routing,
    )
    vendor_model_capability_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendor_model_capabilities.id", ondelete="SET NULL"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    # -------------------------------------------------------------------------
    # ACS Config Pack: TR-069 parameter paths and device-specific defaults
    # -------------------------------------------------------------------------
    # Link to code adapter for transforms (security mode mappings, etc.)
    adapter_name: Mapped[str | None] = mapped_column(
        String(80),
        doc="Code adapter name for transforms (e.g., 'huawei-hg8245h')",
    )
    # TR-069 data model variant (affects parameter paths)
    tr069_data_model: Mapped[str | None] = mapped_column(
        String(20),
        doc="TR-069 data model: 'tr181' (Device.) or 'tr098' (InternetGatewayDevice.)",
    )
    # Config method preference for this device type
    config_method_preference: Mapped[str | None] = mapped_column(
        String(20),
        doc="Preferred config method: 'tr069', 'omci', or 'both'",
    )

    # WiFi TR-069 parameter paths (model-specific)
    wifi_ssid_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for WiFi SSID parameter"
    )
    wifi_password_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for WiFi password/key parameter"
    )
    wifi_enabled_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for WiFi enable/disable"
    )
    wifi_channel_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for WiFi channel"
    )
    wifi_security_mode_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for WiFi security mode"
    )

    # WAN TR-069 parameter paths (model-specific)
    wan_pppoe_username_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for PPPoE username"
    )
    wan_pppoe_password_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for PPPoE password"
    )
    wan_connection_type_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for WAN connection type"
    )

    # LAN TR-069 parameter paths (model-specific)
    lan_ip_address_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for LAN IP address"
    )
    lan_subnet_mask_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for LAN subnet mask"
    )
    lan_dhcp_enabled_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for DHCP server enable"
    )
    lan_dhcp_start_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for DHCP pool start address"
    )
    lan_dhcp_end_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for DHCP pool end address"
    )

    # Access/management TR-069 parameter paths (model-specific)
    remote_access_enabled_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for remote access enable"
    )
    http_management_enabled_path: Mapped[str | None] = mapped_column(
        String(255), doc="TR-069 path for HTTP management enable"
    )

    # Default WiFi settings for this device type
    default_wifi_security_mode: Mapped[str | None] = mapped_column(
        String(50), default="WPA2-Personal", doc="Default WiFi security mode"
    )
    default_wifi_channel: Mapped[str | None] = mapped_column(
        String(10), default="auto", doc="Default WiFi channel"
    )

    # Firmware baseline
    min_firmware_version: Mapped[str | None] = mapped_column(
        String(50), doc="Minimum firmware version required for full feature support"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    ont_units = relationship("OntUnit", back_populates="onu_type")
    vendor_model_capability = relationship(
        "VendorModelCapability",
        foreign_keys=[vendor_model_capability_id],
    )


class SpeedProfile(Base):
    """OLT-level speed profile catalog entry (download or upload)."""

    __tablename__ = "speed_profiles"
    __table_args__ = (
        UniqueConstraint("name", "direction", name="uq_speed_profiles_name_direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[SpeedProfileDirection] = mapped_column(
        Enum(
            SpeedProfileDirection, name="speedprofiledirection", create_constraint=False
        ),
        nullable=False,
    )
    speed_kbps: Mapped[int] = mapped_column(Integer, nullable=False)
    speed_type: Mapped[SpeedProfileType] = mapped_column(
        Enum(SpeedProfileType, name="speedprofiletype", create_constraint=False),
        nullable=False,
        default=SpeedProfileType.internet,
    )
    use_prefix_suffix: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class OntProvisioningProfile(Base):
    """Reusable ONT configuration template (desired state).

    Defines what an ONT should look like when fully provisioned:
    WAN services, speed profiles, WiFi, management, VoIP config.
    Can be linked to a CatalogOffer as default for new subscriptions.
    """

    __tablename__ = "ont_provisioning_profiles"
    __table_args__ = (
        UniqueConstraint(
            "owner_subscriber_id", "name", name="uq_ont_prov_profiles_owner_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_type: Mapped[OntProfileType] = mapped_column(
        Enum(OntProfileType, name="ontprofiletype", create_constraint=False),
        nullable=False,
        default=OntProfileType.residential,
    )
    description: Mapped[str | None] = mapped_column(Text)
    ont_type_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("onu_types.id", ondelete="SET NULL"),
    )
    execution_policy: Mapped[dict | None] = mapped_column(JSON)
    required_capabilities: Mapped[dict | None] = mapped_column(JSON)
    supports_manual_override: Mapped[bool] = mapped_column(Boolean, default=True)

    # Device-level defaults (reuse existing enums)
    config_method: Mapped[ConfigMethod | None] = mapped_column(
        Enum(ConfigMethod, name="configmethod", create_constraint=False),
    )
    onu_mode: Mapped[OnuMode | None] = mapped_column(
        Enum(OnuMode, name="onumode", create_constraint=False),
    )
    ip_protocol: Mapped[IpProtocol | None] = mapped_column(
        Enum(IpProtocol, name="ipprotocol", create_constraint=False),
    )

    # Speed profiles (OLT-level enforcement)
    download_speed_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speed_profiles.id"),
    )
    upload_speed_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("speed_profiles.id"),
    )

    # Scope. Null means a global fallback; OLT-specific profiles should be used
    # for any VLAN/service-port/GEM settings that depend on site uplinks.
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="SET NULL"),
        index=True,
    )

    # Management plane
    mgmt_ip_mode: Mapped[MgmtIpMode | None] = mapped_column(
        Enum(MgmtIpMode, name="mgmtipmode", create_constraint=False),
    )
    mgmt_vlan_tag: Mapped[int | None] = mapped_column(Integer)
    mgmt_ip_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ip_pools.id", ondelete="SET NULL"),
        index=True,
        doc="IP pool for static management IP assignment",
    )
    mgmt_remote_access: Mapped[bool] = mapped_column(Boolean, default=False)

    # WiFi defaults
    wifi_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    wifi_ssid_template: Mapped[str | None] = mapped_column(String(120))
    wifi_security_mode: Mapped[str | None] = mapped_column(String(40))
    wifi_channel: Mapped[str | None] = mapped_column(String(10))
    wifi_band: Mapped[str | None] = mapped_column(String(20))

    # OLT-level provisioning knobs. Authorization line/service profiles are
    # resolved from imported OLT state via OltOnuTypeProfileMapping.
    internet_config_ip_index: Mapped[int | None] = mapped_column(
        Integer,
        doc="ip-index for ont internet-config command (activates TCP stack)",
    )
    wan_config_profile_id: Mapped[int | None] = mapped_column(
        Integer,
        doc="profile-id for ont wan-config command (sets route+NAT mode)",
    )
    pppoe_omci_vlan: Mapped[int | None] = mapped_column(
        Integer,
        doc="VLAN for PPPoE-over-OMCI (OLT-side, not TR-069); null = skip OMCI PPPoE",
    )

    # Connection request credentials (pushed after TR-069 bootstrap)
    cr_username: Mapped[str | None] = mapped_column(
        String(120), doc="Connection request username for on-demand ACS management"
    )
    cr_password: Mapped[str | None] = mapped_column(
        String(512), doc="Connection request password (encrypted at rest)"
    )

    # VoIP
    voip_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    owner_subscriber = relationship(
        "Subscriber",
        backref="ont_provisioning_profiles",
        foreign_keys=[owner_subscriber_id],
    )
    olt_device = relationship("OLTDevice", foreign_keys=[olt_device_id])
    ont_type = relationship("OnuType", foreign_keys=[ont_type_id])
    mgmt_ip_pool = relationship("IpPool", foreign_keys=[mgmt_ip_pool_id])
    download_speed_profile = relationship(
        "SpeedProfile", foreign_keys=[download_speed_profile_id]
    )
    upload_speed_profile = relationship(
        "SpeedProfile", foreign_keys=[upload_speed_profile_id]
    )
    wan_services = relationship(
        "OntProfileWanService",
        back_populates="profile",
        cascade="all, delete-orphan",
        order_by="OntProfileWanService.priority",
    )


class OntProfileWanService(Base):
    """A single WAN service within a provisioning profile.

    Each profile can have multiple WAN services (internet + IPTV + VoIP),
    each with its own L2 VLAN config and L3 connection type.
    """

    __tablename__ = "ont_profile_wan_services"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_provisioning_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Service identity
    service_type: Mapped[WanServiceType] = mapped_column(
        Enum(WanServiceType, name="wanservicetype", create_constraint=False),
        nullable=False,
        default=WanServiceType.internet,
    )
    name: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[int] = mapped_column(Integer, default=1)

    # L2: VLAN (plain integers — resolved to Vlan records at provisioning time)
    vlan_mode: Mapped[VlanMode] = mapped_column(
        Enum(VlanMode, name="vlanmode", create_constraint=False),
        nullable=False,
        default=VlanMode.tagged,
    )
    s_vlan: Mapped[int | None] = mapped_column(Integer)
    c_vlan: Mapped[int | None] = mapped_column(Integer)
    cos_priority: Mapped[int | None] = mapped_column(Integer)
    mtu: Mapped[int] = mapped_column(Integer, default=1500)

    # L3: Connection
    connection_type: Mapped[WanConnectionType] = mapped_column(
        Enum(WanConnectionType, name="wanconnectiontype", create_constraint=False),
        nullable=False,
        default=WanConnectionType.pppoe,
    )
    nat_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_mode: Mapped[IpProtocol | None] = mapped_column(
        Enum(IpProtocol, name="ipprotocol", create_constraint=False),
    )

    # PPPoE (when connection_type = pppoe)
    pppoe_username_template: Mapped[str | None] = mapped_column(String(200))
    pppoe_password_mode: Mapped[PppoePasswordMode | None] = mapped_column(
        Enum(PppoePasswordMode, name="pppoepasswordmode", create_constraint=False),
    )
    pppoe_static_password: Mapped[str | None] = mapped_column(String(512))

    # Static IP (when connection_type = static)
    static_ip_source: Mapped[str | None] = mapped_column(String(200))

    # LAN port binding
    bind_lan_ports: Mapped[dict | None] = mapped_column(JSON)
    bind_ssid_index: Mapped[int | None] = mapped_column(Integer)

    # OMCI-specific (when config_method = omci)
    gem_port_id: Mapped[int | None] = mapped_column(Integer)
    t_cont_profile: Mapped[str | None] = mapped_column(String(120))

    # Metadata
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    profile = relationship("OntProvisioningProfile", back_populates="wan_services")


class OntReconcileScope(enum.Enum):
    """Which reconciliation path a hold suppresses.

    Only ``automatic_sweep`` exists initially, and deliberately so: an operator
    doing reviewed work must still be able to drive a device explicitly. A hold
    that also blocked operator-initiated repair would leave a held ONT with no
    legitimate path back to convergence.
    """

    automatic_sweep = "automatic_sweep"


class OntReconcileHoldStatus(enum.Enum):
    """``active`` suppresses; ``released`` is history and never deleted."""

    active = "active"
    released = "released"


class OntReconcileHold(Base):
    """A reviewed decision that one ONT is excluded from automatic sweeps.

    Owner: ``network.ont_reconcile_eligibility``. The fleet-wide control
    ``network.ont_reconcile`` is the blunt instrument this replaces: it stops
    convergence for every ONT, and it also pauses expired remote-access
    cleanup and the dialer reconcile, because those run inside the same task
    after the gate.

    Evidence is mandatory, not decorative. A hold suppresses convergence on a
    customer device, so it records WHY (``reason_code`` plus a written
    ``explanation``), WHO asked (``actor``), WHO agreed (``reviewer``, which
    must differ from the actor), and WHEN it must next be looked at
    (``review_due_at``).

    There is deliberately NO automatic expiry. An expiring hold would silently
    hand a device back to the sweeper at an arbitrary moment -- precisely the
    surprise the hold exists to prevent. An overdue hold stays active and
    becomes visible for escalation; only an explicit release command ends it.
    """

    __tablename__ = "ont_reconcile_holds"
    __table_args__ = (
        # One ACTIVE hold per ONT and scope. Partial so released holds
        # accumulate as history without blocking a future hold.
        Index(
            "uq_ont_reconcile_holds_active_per_ont_scope",
            "ont_unit_id",
            "scope",
            unique=True,
            # Both dialects: without sqlite_where the test database gets a FULL
            # unique constraint, so a RELEASED hold would permanently block
            # re-holding the same ONT -- turning history into a lockout, and
            # hiding the difference from every test.
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_ont_reconcile_holds_status", "status"),
        Index("ix_ont_reconcile_holds_review_due_at", "review_due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ont_units.id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[OntReconcileScope] = mapped_column(
        Enum(OntReconcileScope, name="ontreconcilescope"),
        default=OntReconcileScope.automatic_sweep,
    )
    status: Mapped[OntReconcileHoldStatus] = mapped_column(
        Enum(OntReconcileHoldStatus, name="ontreconcileholdstatus"),
        default=OntReconcileHoldStatus.active,
    )

    #: Stable machine code; the operator-readable half is ``explanation``.
    reason_code: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(160))
    #: Must differ from ``actor``: a hold on a customer device is a two-person
    #: decision, and self-review is not review.
    reviewer: Mapped[str] = mapped_column(String(160))
    #: Idempotency for the placing command. MANDATORY: without it a retried
    #: request either creates a second decision or trips the partial unique
    #: index with an error that looks like a bug rather than a duplicate.
    idempotency_key: Mapped[str] = mapped_column(
        String(200), unique=True, nullable=False
    )

    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    #: When a human must look again. NOT an expiry -- see the class docstring.
    review_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String(160))
    release_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_overdue(self) -> bool:
        """Past its review date and still suppressing. Alerts, never releases."""
        if self.status is not OntReconcileHoldStatus.active:
            return False
        due = self.review_due_at
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        return due is not None and due <= datetime.now(UTC)


class OntWanServiceLifecycle(enum.Enum):
    """Lifecycle of a declared WAN service intent.

    ``planned`` and ``unverified`` are NOT authority. A row only authorises
    delivery once an owner command has moved it to ``active`` on evidence, and
    ``retired`` preserves history rather than deleting it.

    Existing rows begin at ``unverified`` regardless of ``is_active``: before
    this owner existed nothing maintained the table, so a pre-existing row
    records that something once wrote a value, not that anyone declared intent.
    """

    planned = "planned"
    unverified = "unverified"
    active = "active"
    retired = "retired"


class OntWanServiceInstance(Base):
    """Per-ONT WAN service instance with resolved credentials and VLANs.

    Bridges the gap between profile templates (OntProfileWanService) and
    actual device configuration. Each instance holds:
    - Resolved VLAN IDs (not just tags)
    - Actual PPPoE credentials (resolved from templates)
    - Per-service provisioning state

    This enables:
    - Multi-WAN support at instance level (internet + IPTV + VoIP)
    - Grouped L2/L3 provisioning (VLAN + connection + credentials together)
    - Independent service state tracking
    """

    __tablename__ = "ont_wan_service_instances"
    __table_args__ = (
        Index(
            "ix_ont_wan_service_instances_ont_type",
            "ont_id",
            "service_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Exact service grain. An ONT-grain intent row authorises "this device may
    # terminate PPP", which is not the same claim as "this SERVICE terminates
    # here" -- and a delivery ruling built on the weaker claim can hand one
    # service's credential to another.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # The one instance that carries the service's primary Internet termination.
    # ``priority`` may ORDER instances; it must never select authority, because
    # ordering is a display concern and authority is an ownership decision.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    lifecycle_state: Mapped[OntWanServiceLifecycle] = mapped_column(
        Enum(
            OntWanServiceLifecycle,
            name="ontwanservicelifecycle",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        server_default=OntWanServiceLifecycle.unverified.value,
        default=OntWanServiceLifecycle.unverified,
        index=True,
    )
    # Monotonic per-row revision. A delivery ruling binds it, so a ruling taken
    # before a replace cannot authorise a write after it.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )
    # Provenance for every lifecycle transition.
    declared_by: Mapped[str | None] = mapped_column(String(160))
    declared_reason: Mapped[str | None] = mapped_column(Text)
    evidence_ref: Mapped[str | None] = mapped_column(String(200))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(Text)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_wan_service_instances.id", ondelete="SET NULL"),
    )
    source_profile_service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_profile_wan_services.id", ondelete="SET NULL"),
        doc="Original profile service this was instantiated from (if any)",
    )

    # Service identity
    service_type: Mapped[WanServiceType] = mapped_column(
        Enum(WanServiceType, name="wanservicetype", create_constraint=False),
        nullable=False,
        default=WanServiceType.internet,
    )
    name: Mapped[str | None] = mapped_column(String(120))
    priority: Mapped[int] = mapped_column(Integer, default=1)
    # DERIVED from lifecycle_state, not a second authority. Retained so
    # existing readers keep working during the migration; the owner keeps it
    # in step with lifecycle_state and nothing else may set it. A row can be
    # is_active=True and still non-authorising, which is exactly the state
    # every pre-existing row starts in.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # L2: VLAN configuration (resolved at instantiation)
    vlan_mode: Mapped[VlanMode] = mapped_column(
        Enum(VlanMode, name="vlanmode", create_constraint=False),
        nullable=False,
        default=VlanMode.tagged,
    )
    vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vlans.id", ondelete="SET NULL"),
        doc="Resolved VLAN record (from profile s_vlan tag or explicit assignment)",
    )
    s_vlan: Mapped[int | None] = mapped_column(Integer, doc="Service VLAN tag")
    c_vlan: Mapped[int | None] = mapped_column(Integer, doc="Customer VLAN tag (QinQ)")
    cos_priority: Mapped[int | None] = mapped_column(Integer)
    mtu: Mapped[int] = mapped_column(Integer, default=1500)

    # L3: Connection configuration
    connection_type: Mapped[WanConnectionType] = mapped_column(
        Enum(WanConnectionType, name="wanconnectiontype", create_constraint=False),
        nullable=False,
        default=WanConnectionType.pppoe,
    )
    nat_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_mode: Mapped[IpProtocol | None] = mapped_column(
        Enum(IpProtocol, name="ipprotocol", create_constraint=False),
    )

    # PPPoE credentials (resolved from template, actual values)
    pppoe_username: Mapped[str | None] = mapped_column(String(200))
    pppoe_password: Mapped[str | None] = mapped_column(
        String(512), doc="Encrypted PPPoE password"
    )

    # Static IP (when connection_type = static)
    static_ip: Mapped[str | None] = mapped_column(String(64))
    static_gateway: Mapped[str | None] = mapped_column(String(64))
    static_dns: Mapped[str | None] = mapped_column(String(200))
    static_ip_source: Mapped[str | None] = mapped_column(String(200))

    # Binding / OMCI execution metadata for WAN service provisioning.
    bind_lan_ports: Mapped[dict | None] = mapped_column(JSON)
    bind_ssid_index: Mapped[int | None] = mapped_column(Integer)
    gem_port_id: Mapped[int | None] = mapped_column(Integer)
    t_cont_profile: Mapped[str | None] = mapped_column(String(120))

    # Provisioning state
    provisioning_status: Mapped[WanServiceProvisioningStatus] = mapped_column(
        Enum(
            WanServiceProvisioningStatus,
            name="wanserviceprovisioningstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=WanServiceProvisioningStatus.pending,
    )
    last_provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    ont = relationship("OntUnit", back_populates="wan_service_instances")
    source_profile_service = relationship("OntProfileWanService")
    vlan = relationship("Vlan", foreign_keys=[vlan_id])


class VendorModelCapability(Base):
    """Global hardware capability catalog for ONT/ONU vendor models.

    Stores what a specific device model can do: max WAN services,
    VLAN tagging support, QinQ, IPv6, LAN/SSID counts, etc.
    Not org-scoped — hardware facts are universal.
    """

    __tablename__ = "vendor_model_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "vendor", "model", "firmware_pattern", name="uq_vmc_vendor_model_fw"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    firmware_pattern: Mapped[str | None] = mapped_column(String(200))

    # TR-069 root object path (e.g. "InternetGatewayDevice" or "Device")
    tr069_root: Mapped[str | None] = mapped_column(String(200))

    # Structured capability flags
    supported_features: Mapped[dict | None] = mapped_column(JSON)
    max_wan_services: Mapped[int] = mapped_column(Integer, default=1)
    max_lan_ports: Mapped[int] = mapped_column(Integer, default=4)
    max_ssids: Mapped[int] = mapped_column(Integer, default=2)
    supports_vlan_tagging: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_qinq: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_ipv6: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    parameter_maps = relationship(
        "Tr069ParameterMap",
        back_populates="capability",
        cascade="all, delete-orphan",
        order_by="Tr069ParameterMap.canonical_name",
    )


class Tr069ParameterMap(Base):
    """Maps canonical parameter names to device-specific TR-069 CWMP paths.

    Example: canonical_name='wan.pppoe.username' maps to
    tr069_path='InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username'
    for a Huawei HG8245H.
    """

    __tablename__ = "tr069_parameter_maps"
    __table_args__ = (
        UniqueConstraint(
            "capability_id", "canonical_name", name="uq_tr069_param_cap_canonical"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    capability_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vendor_model_capabilities.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tr069_path: Mapped[str] = mapped_column(String(500), nullable=False)
    writable: Mapped[bool] = mapped_column(Boolean, default=True)
    value_type: Mapped[str | None] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    capability = relationship("VendorModelCapability", back_populates="parameter_maps")


class OltFirmwareImage(Base):
    """Catalog of available OLT firmware images for SSH-based upgrades."""

    __tablename__ = "olt_firmware_images"
    __table_args__ = (
        UniqueConstraint(
            "vendor", "model", "version", name="uq_olt_firmware_vendor_model_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    checksum: Mapped[str | None] = mapped_column(String(128))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    release_notes: Mapped[str | None] = mapped_column(Text)
    upgrade_method: Mapped[str | None] = mapped_column(String(60))  # sftp, tftp, ftp
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class OntFirmwareImage(Base):
    """Catalog of available ONT firmware images for TR-069 upgrades."""

    __tablename__ = "ont_firmware_images"
    __table_args__ = (
        UniqueConstraint(
            "vendor", "model", "version", name="uq_ont_firmware_vendor_model_version"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    checksum: Mapped[str | None] = mapped_column(String(128))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class OntConfigSnapshot(Base):
    """Immutable point-in-time configuration evidence for an ONT."""

    __tablename__ = "ont_config_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(60), nullable=False, default="composite")
    label: Mapped[str | None] = mapped_column(String(255))
    device_info: Mapped[dict | None] = mapped_column(JSON)
    wan: Mapped[dict | None] = mapped_column(JSON)
    optical: Mapped[dict | None] = mapped_column(JSON)
    wifi: Mapped[dict | None] = mapped_column(JSON)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_config: Mapped[dict | None] = mapped_column(JSON)
    observed_state: Mapped[dict | None] = mapped_column(JSON)
    provenance: Mapped[dict | None] = mapped_column(JSON)
    payload_checksum: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class VendorSnmpConfig(Base):
    """Per-vendor/model SNMP configuration for OLT polling.

    Allows customization of SNMP walk strategy, timeouts, and OID overrides
    on a per-vendor or per-model basis. Supports priority-based resolution
    where more specific configurations (vendor+model) take precedence.
    """

    __tablename__ = "vendor_snmp_configs"
    __table_args__ = (
        UniqueConstraint("vendor", "model", name="uq_vendor_snmp_config"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))

    # Walk strategy: "single" (snmpwalk) or "bulk" (snmpbulkwalk)
    walk_strategy: Mapped[str] = mapped_column(String(20), default="single")
    walk_timeout_seconds: Mapped[int] = mapped_column(Integer, default=90)
    walk_max_repetitions: Mapped[int] = mapped_column(Integer, default=50)

    # OID overrides (JSON dict mapping metric name to OID)
    oid_overrides: Mapped[dict | None] = mapped_column(JSON)

    # Signal scale factor override
    signal_scale: Mapped[float | None] = mapped_column(Float)

    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class SignalThresholdOverride(Base):
    """Per-OLT or per-model signal threshold overrides.

    Allows customization of warning/critical thresholds on a per-OLT
    or per-model basis. Either olt_device_id or model_pattern can be
    set, but not both (enforced by check constraint).
    """

    __tablename__ = "signal_threshold_overrides"
    __table_args__ = (
        CheckConstraint(
            "NOT (olt_device_id IS NOT NULL AND model_pattern IS NOT NULL)",
            name="ck_threshold_override_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Scope: either specific OLT OR model pattern (not both)
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("olt_devices.id", ondelete="CASCADE")
    )
    model_pattern: Mapped[str | None] = mapped_column(String(120))

    # Threshold values (NULL = inherit from global)
    warning_threshold_dbm: Mapped[float | None] = mapped_column(Float)
    critical_threshold_dbm: Mapped[float | None] = mapped_column(Float)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt_device = relationship("OLTDevice")


# =============================================================================
# Service-port allocator models
# =============================================================================


class OltServicePortPool(Base):
    """Per-OLT service-port index pool.

    Tracks the available service-port indices for an OLT, enabling DB-based
    allocation rather than SSH discovery. Similar pattern to IpPool.
    """

    __tablename__ = "olt_service_port_pools"
    __table_args__ = (
        UniqueConstraint("olt_device_id", name="uq_olt_service_port_pools_olt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    min_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_index: Mapped[int] = mapped_column(Integer, nullable=False, default=65535)
    reserved_indices: Mapped[list[int] | None] = mapped_column(
        JSON, doc="JSON array of indices reserved for special use (e.g., management)"
    )

    # Cached allocation tracking for faster index allocation (mirrors IpPool pattern)
    next_available_index: Mapped[int | None] = mapped_column(Integer)
    available_count: Mapped[int | None] = mapped_column(Integer)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt_device = relationship("OLTDevice")
    allocations = relationship(
        "ServicePortAllocation",
        back_populates="pool",
        cascade="all, delete-orphan",
    )


class ServicePortAllocation(Base):
    """Tracks allocated service-port indices per ONT.

    Each row represents a single service-port allocated from an OLT pool.
    Similar to IP address allocation but for service-port indices.
    """

    __tablename__ = "service_port_allocations"
    __table_args__ = (
        UniqueConstraint(
            "pool_id", "port_index", name="uq_service_port_allocations_pool_index"
        ),
        UniqueConstraint(
            "correlation_key",
            name="uq_service_port_allocations_correlation_key",
        ),
        Index("ix_service_port_allocations_ont", "ont_unit_id"),
        Index("ix_service_port_allocations_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_service_port_pools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="RESTRICT"),
        nullable=False,
    )
    port_index: Mapped[int] = mapped_column(Integer, nullable=False)
    vlan_id: Mapped[int | None] = mapped_column(Integer)
    gem_index: Mapped[int | None] = mapped_column(Integer)
    service_type: Mapped[str | None] = mapped_column(
        String(40), doc="internet, management, tr069, iptv, voip"
    )
    correlation_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pool = relationship("OltServicePortPool", back_populates="allocations")
    ont_unit = relationship("OntUnit")


class OltServicePort(Base):
    """Imported service-port state observed on an OLT."""

    __tablename__ = "olt_service_ports"
    __table_args__ = (
        UniqueConstraint(
            "olt_device_id",
            "port_index",
            name="uq_olt_service_ports_olt_port_index",
        ),
        Index("ix_olt_service_ports_olt_fsp", "olt_device_id", "fsp"),
        Index("ix_olt_service_ports_ont", "ont_unit_id"),
        Index("ix_olt_service_ports_vlan_gem", "vlan_id", "gem_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    olt_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ont_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="SET NULL"),
        nullable=True,
    )
    port_index: Mapped[int] = mapped_column(Integer, nullable=False)
    fsp: Mapped[str] = mapped_column(String(32), nullable=False)
    ont_id_on_olt: Mapped[int] = mapped_column(Integer, nullable=False)
    vlan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gem_index: Mapped[int] = mapped_column(Integer, nullable=False)
    user_vlan: Mapped[str | None] = mapped_column(String(32))
    tag_transform: Mapped[str | None] = mapped_column(String(40))
    flow_type: Mapped[str | None] = mapped_column(String(40))
    flow_para: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_entry: Mapped[dict | None] = mapped_column(JSON)
    last_imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    olt_device = relationship("OLTDevice")
    ont_unit = relationship("OntUnit")


# =============================================================================
# Verification status for asynchronous drift detection
# =============================================================================


class VerificationStatus(enum.Enum):
    """Status of ONT provisioning verification."""

    pending = "pending"
    verified = "verified"
    drift_detected = "drift_detected"
    failed = "failed"


# =============================================================================
# Authorization Presets for Quick ONT Authorization
# =============================================================================


class AuthorizationPreset(Base):
    """Reusable preset for ONT authorization workflow.

    Allows NOC technicians to quickly authorize ONTs with pre-configured
    settings. Can optionally auto-authorize ONTs matching a serial pattern.
    """

    __tablename__ = "authorization_presets"
    __table_args__ = (UniqueConstraint("name", name="uq_authorization_presets_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Default VLAN for service-port creation
    default_vlan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vlans.id", ondelete="SET NULL"),
    )

    # Auto-authorization settings
    auto_authorize: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        doc="Automatically authorize matching ONTs without manual intervention",
    )
    serial_pattern: Mapped[str | None] = mapped_column(
        String(120),
        doc="Regex pattern for auto-matching serial numbers (e.g., 'HWTC.*', 'ZTEG.*')",
    )

    # OLT scope (null = all OLTs)
    olt_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("olt_devices.id", ondelete="CASCADE"),
        index=True,
        doc="Scope to specific OLT; null means global preset",
    )

    # Priority for auto-matching (higher = checked first)
    priority: Mapped[int] = mapped_column(Integer, default=0)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, doc="Default preset shown first in selection dropdown"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    default_vlan = relationship("Vlan")
    olt_device = relationship("OLTDevice", foreign_keys=[olt_device_id])
