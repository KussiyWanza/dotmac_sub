import enum
import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.catalog import HealthStatus


class DeviceRole(enum.Enum):
    core = "core"
    distribution = "distribution"
    access = "access"
    aggregation = "aggregation"
    edge = "edge"
    cpe = "cpe"


class DeviceStatus(enum.Enum):
    online = "online"
    offline = "offline"
    degraded = "degraded"
    maintenance = "maintenance"


class DeviceType(enum.Enum):
    router = "router"
    switch = "switch"
    hub = "hub"
    firewall = "firewall"
    inverter = "inverter"
    access_point = "access_point"
    bridge = "bridge"
    modem = "modem"
    server = "server"
    other = "other"


class InterfaceStatus(enum.Enum):
    up = "up"
    down = "down"
    unknown = "unknown"


class MetricType(enum.Enum):
    cpu = "cpu"
    memory = "memory"
    temperature = "temperature"
    rx_bps = "rx_bps"
    tx_bps = "tx_bps"
    uptime = "uptime"
    custom = "custom"


class SpeedTestSource(enum.Enum):
    manual = "manual"
    scheduled = "scheduled"
    api = "api"


class AlertSeverity(enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class AlertStatus(enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class AlertOperator(enum.Enum):
    gt = "gt"
    gte = "gte"
    lt = "lt"
    lte = "lte"
    eq = "eq"


class DnsThreatSeverity(enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class DnsThreatAction(enum.Enum):
    blocked = "blocked"
    allowed = "allowed"
    monitored = "monitored"


class PopSite(Base):
    __tablename__ = "pop_sites"
    __table_args__ = (
        Index("ix_pop_sites_owner_subscriber_id", "owner_subscriber_id"),
        # A pop_site mapped to a Zabbix "X BTS" host group carries its groupid.
        # Partial-unique so the (many) non-BTS / region rows stay NULL and unconstrained.
        Index(
            "uq_pop_sites_zabbix_group_id",
            "zabbix_group_id",
            unique=True,
            postgresql_where=text("zabbix_group_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), unique=True)
    address_line1: Mapped[str | None] = mapped_column(String(120))
    address_line2: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    region: Mapped[str | None] = mapped_column(String(80))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str | None] = mapped_column(String(2))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geom = mapped_column(Geometry("POINT", srid=4326), nullable=True)
    zone_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_zones.id")
    )
    owner_subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    reseller_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resellers.id")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set by the topology reconcile when this pop_site is matched to a Zabbix
    # "X BTS" host group (so a BTS rename in Zabbix updates in place).
    zabbix_group_id: Mapped[str | None] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    devices = relationship("NetworkDevice", back_populates="pop_site")
    nas_devices = relationship("NasDevice", back_populates="pop_site")
    contacts = relationship(
        "PopSiteContact",
        back_populates="pop_site",
        cascade="all, delete-orphan",
        order_by="PopSiteContact.created_at.desc()",
    )
    zone = relationship("NetworkZone")
    owner_subscriber = relationship("Subscriber", foreign_keys=[owner_subscriber_id])
    reseller = relationship("Reseller")


class PopSiteContact(Base):
    __tablename__ = "pop_site_contacts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pop_site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pop_site = relationship("PopSite", back_populates="contacts")


class NetworkDevice(Base):
    __tablename__ = "network_devices"
    __table_args__ = (
        UniqueConstraint("hostname", name="uq_network_devices_hostname"),
        UniqueConstraint("mgmt_ip", name="uq_network_devices_mgmt_ip"),
        Index(
            "uq_network_devices_active_splynx_monitoring_id",
            "splynx_monitoring_id",
            unique=True,
            postgresql_where=text("is_active AND splynx_monitoring_id IS NOT NULL"),
        ),
        # Stable Zabbix host id is the reconcile key; partial-unique so rows not
        # yet linked to Zabbix (e.g. orphaned imports) stay NULL.
        Index(
            "uq_network_devices_zabbix_hostid",
            "zabbix_hostid",
            unique=True,
            postgresql_where=text("zabbix_hostid IS NOT NULL"),
        ),
        # Stable UISP device id (wireless APs / infra) stamped by the UISP
        # topology sync; partial-unique so non-UISP rows stay NULL.
        Index(
            "uq_network_devices_uisp_device_id",
            "uisp_device_id",
            unique=True,
            postgresql_where=text("uisp_device_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    parent_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(160))
    mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    vendor: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120))
    device_type: Mapped[DeviceType | None] = mapped_column(Enum(DeviceType))
    role: Mapped[DeviceRole] = mapped_column(Enum(DeviceRole), default=DeviceRole.edge)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="monitoring_devicestatus"), default=DeviceStatus.offline
    )
    ping_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    snmp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    send_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    notification_delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    snmp_port: Mapped[int | None] = mapped_column(Integer)
    snmp_version: Mapped[str | None] = mapped_column(String(10))
    snmp_community: Mapped[str | None] = mapped_column(String(512))
    snmp_rw_community: Mapped[str | None] = mapped_column(String(512))
    snmp_username: Mapped[str | None] = mapped_column(String(120))
    snmp_auth_protocol: Mapped[str | None] = mapped_column(String(16))
    snmp_auth_secret: Mapped[str | None] = mapped_column(String(512))
    snmp_priv_protocol: Mapped[str | None] = mapped_column(String(16))
    snmp_priv_secret: Mapped[str | None] = mapped_column(String(512))
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ping_ok: Mapped[bool | None] = mapped_column(Boolean)
    ping_down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snmp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_snmp_ok: Mapped[bool | None] = mapped_column(Boolean)
    snmp_down_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    splynx_monitoring_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Topology reconcile (Zabbix linkage) ---
    # Stable Zabbix host id; the reconcile key. NULL until merged to a Zabbix host.
    zabbix_hostid: Mapped[str | None] = mapped_column(String(20))
    # Stable UISP device id, stamped by the UISP topology sync when this node
    # is matched to a UISP AP/infra device. NULL until matched.
    uisp_device_id: Mapped[str | None] = mapped_column(String(64))
    # Provenance of this row: 'zabbix_reconcile', 'splynx', manual, etc.
    source: Mapped[str | None] = mapped_column(String(40))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 'inferred' (role derived from Zabbix group) vs 'manual' (operator-set; the
    # reconcile must not stomp a manually-set role).
    role_source: Mapped[str | None] = mapped_column(String(20))
    # Link to the matched provisioning device: matched_device_type in
    # {'olt','nas'} + the OLTDevice/NasDevice id. Set by the matcher; powers
    # resolve_customer_path and the topology-gaps report.
    matched_device_type: Mapped[str | None] = mapped_column(String(20))
    matched_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Topology live status, warmed from Zabbix into this cache and read
    # by the Network Path panel — never fetched on the request path. Distinct
    # from the ping/snmp `status` column (different writer). One of
    # up/down/problem/unknown.
    live_status: Mapped[str | None] = mapped_column(String(20))
    # When live_status entered its current value (transition/dwell time), not
    # when reachability was last observed. Collector freshness is carried by
    # last_ping_at / last_snmp_at.
    live_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    topology_x: Mapped[float | None] = mapped_column(Float)
    topology_y: Mapped[float | None] = mapped_column(Float)

    # Capacity tracking
    max_concurrent_subscribers: Mapped[int | None] = mapped_column(Integer)
    current_subscriber_count: Mapped[int] = mapped_column(Integer, default=0)

    # Health tracking
    health_status: Mapped[HealthStatus] = mapped_column(
        Enum(HealthStatus, values_callable=lambda x: [e.value for e in x]),
        default=HealthStatus.unknown,
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
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

    pop_site = relationship("PopSite", back_populates="devices")
    parent_device = relationship(
        "NetworkDevice",
        remote_side="NetworkDevice.id",
        back_populates="child_devices",
    )
    child_devices = relationship(
        "NetworkDevice",
        back_populates="parent_device",
    )
    interfaces = relationship("DeviceInterface", back_populates="device")
    metrics = relationship("DeviceMetric", back_populates="device")
    snmp_oids = relationship("NetworkDeviceSnmpOid", back_populates="device")
    bandwidth_graphs = relationship(
        "NetworkDeviceBandwidthGraph", back_populates="device"
    )
    graph_sources = relationship(
        "NetworkDeviceBandwidthGraphSource", back_populates="source_device"
    )
    alerts = relationship("Alert", back_populates="device")
    alert_rules = relationship("AlertRule", back_populates="device")
    nas_device = relationship(
        "NasDevice", back_populates="network_device", uselist=False
    )


class DeviceInterface(Base):
    __tablename__ = "device_interfaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[InterfaceStatus] = mapped_column(
        Enum(InterfaceStatus), default=InterfaceStatus.unknown
    )
    speed_mbps: Mapped[int | None] = mapped_column(Integer)
    mac_address: Mapped[str | None] = mapped_column(String(64))
    snmp_index: Mapped[int | None] = mapped_column(BigInteger)
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)

    # Counter state retained for historical bps data.
    last_in_octets: Mapped[float | None] = mapped_column(Float)
    last_out_octets: Mapped[float | None] = mapped_column(Float)
    last_counter_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    device = relationship("NetworkDevice", back_populates="interfaces")
    alerts = relationship("Alert", back_populates="interface")
    alert_rules = relationship("AlertRule", back_populates="interface")
    metrics = relationship("DeviceMetric", back_populates="interface")


class DeviceMetric(Base):
    __tablename__ = "device_metrics"
    __table_args__ = (
        Index(
            "ix_device_metrics_rx_bps_recorded_at",
            "recorded_at",
            postgresql_where=text("metric_type = 'rx_bps' AND value > 0"),
            postgresql_include=["value"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    metric_type: Mapped[MetricType] = mapped_column(
        Enum(MetricType), default=MetricType.custom
    )
    value: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    device = relationship("NetworkDevice", back_populates="metrics")
    interface = relationship("DeviceInterface", back_populates="metrics")


class NetworkDeviceSnmpOid(Base):
    __tablename__ = "network_device_snmp_oids"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    oid: Mapped[str] = mapped_column(String(160), nullable=False)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    rrd_data_source_type: Mapped[str] = mapped_column(String(16), default="gauge")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_poll_status: Mapped[str | None] = mapped_column(String(16))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    device = relationship("NetworkDevice", back_populates="snmp_oids")
    graph_sources = relationship(
        "NetworkDeviceBandwidthGraphSource", back_populates="snmp_oid"
    )


class NetworkDeviceBandwidthGraph(Base):
    __tablename__ = "network_device_bandwidth_graphs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    vertical_axis_title: Mapped[str] = mapped_column(String(80), default="Bandwidth")
    height_px: Mapped[int] = mapped_column(Integer, default=150)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    public_token: Mapped[str | None] = mapped_column(String(64), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    device = relationship("NetworkDevice", back_populates="bandwidth_graphs")
    sources = relationship(
        "NetworkDeviceBandwidthGraphSource",
        back_populates="graph",
        order_by="NetworkDeviceBandwidthGraphSource.sort_order.asc()",
    )


class NetworkDeviceBandwidthGraphSource(Base):
    __tablename__ = "network_device_bandwidth_graph_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    graph_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_device_bandwidth_graphs.id"),
        nullable=False,
    )
    source_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    snmp_oid_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_device_snmp_oids.id"), nullable=False
    )
    factor: Mapped[float] = mapped_column(Float, default=1.0)
    color_hex: Mapped[str] = mapped_column(String(7), default="#22c55e")
    draw_type: Mapped[str] = mapped_column(String(16), default="LINE1")
    stack_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    value_unit: Mapped[str] = mapped_column(String(12), default="Bps")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    graph = relationship("NetworkDeviceBandwidthGraph", back_populates="sources")
    source_device = relationship("NetworkDevice", back_populates="graph_sources")
    snmp_oid = relationship("NetworkDeviceSnmpOid", back_populates="graph_sources")


class SpeedTestResult(Base):
    __tablename__ = "speed_test_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id")
    )
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    source: Mapped[SpeedTestSource] = mapped_column(
        Enum(SpeedTestSource), default=SpeedTestSource.manual
    )
    target_label: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str | None] = mapped_column(String(120))
    server_name: Mapped[str | None] = mapped_column(String(160))
    external_ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    download_mbps: Mapped[float] = mapped_column(Float, default=0)
    upload_mbps: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    jitter_ms: Mapped[float | None] = mapped_column(Float)
    packet_loss_pct: Mapped[float | None] = mapped_column(Float)
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

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
    network_device = relationship("NetworkDevice")
    pop_site = relationship("PopSite")


class DnsThreatEvent(Base):
    __tablename__ = "dns_threat_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subscribers.id")
    )
    network_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    queried_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    query_type: Mapped[str | None] = mapped_column(String(16))
    source_ip: Mapped[str | None] = mapped_column(String(64))
    destination_ip: Mapped[str | None] = mapped_column(String(64))
    threat_category: Mapped[str | None] = mapped_column(String(80))
    threat_feed: Mapped[str | None] = mapped_column(String(120))
    severity: Mapped[DnsThreatSeverity] = mapped_column(
        Enum(DnsThreatSeverity), default=DnsThreatSeverity.medium
    )
    action: Mapped[DnsThreatAction] = mapped_column(
        Enum(DnsThreatAction), default=DnsThreatAction.blocked
    )
    confidence_score: Mapped[float | None] = mapped_column(Float)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    subscriber = relationship("Subscriber")
    network_device = relationship("NetworkDevice")
    pop_site = relationship("PopSite")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    metric_type: Mapped[MetricType] = mapped_column(Enum(MetricType), nullable=False)
    operator: Mapped[AlertOperator] = mapped_column(
        Enum(AlertOperator), default=AlertOperator.gt
    )
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
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

    device = relationship("NetworkDevice", back_populates="alert_rules")
    interface = relationship("DeviceInterface", back_populates="alert_rules")
    alerts = relationship("Alert", back_populates="rule")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id"), nullable=False
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    metric_type: Mapped[MetricType] = mapped_column(Enum(MetricType), nullable=False)
    measured_value: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    rule = relationship("AlertRule", back_populates="alerts")
    device = relationship("NetworkDevice")
    interface = relationship("DeviceInterface")
    events = relationship("AlertEvent", back_populates="alert")


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=False
    )
    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus), default=AlertStatus.open
    )
    message: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    alert = relationship("Alert", back_populates="events")


# ── Topology ─────────────────────────────────────────────────────────


class TopologyLinkRole(enum.Enum):
    uplink = "uplink"
    backhaul = "backhaul"
    peering = "peering"
    lag_member = "lag_member"
    crossconnect = "crossconnect"
    access = "access"
    distribution = "distribution"
    core = "core"
    unknown = "unknown"


class TopologyLinkMedium(enum.Enum):
    fiber = "fiber"
    wireless = "wireless"
    ethernet = "ethernet"
    virtual = "virtual"
    unknown = "unknown"


class TopologyLinkAdminStatus(enum.Enum):
    enabled = "enabled"
    disabled = "disabled"
    maintenance = "maintenance"


class NetworkWeathermapView(Base):
    """Saved operational weather-map layout and display settings."""

    __tablename__ = "network_weathermap_views"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_network_weathermap_views_slug"),
        Index("ix_network_weathermap_views_pop_site", "pop_site_id"),
        Index("ix_network_weathermap_views_default", "is_default"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    topology_group: Mapped[str | None] = mapped_column(String(80))
    pop_site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    layout: Mapped[dict | None] = mapped_column(JSON)
    settings: Mapped[dict | None] = mapped_column(JSON)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    pop_site = relationship("PopSite")


class NetworkTopologyLink(Base):
    """Observed or manually recorded adjacency between device interfaces.

    LLDP-sourced rows are observations. They can verify a reviewed forwarding
    declaration, but do not establish official upstream path, role, site,
    outage blast radius, or customer-path consequences on their own.
    """

    __tablename__ = "network_topology_links"
    __table_args__ = (
        UniqueConstraint(
            "source_device_id",
            "source_interface_id",
            "target_device_id",
            "target_interface_id",
            name="uq_topology_link_endpoints",
        ),
        Index("ix_topology_link_source_device", "source_device_id"),
        Index("ix_topology_link_target_device", "target_device_id"),
        Index("ix_topology_link_source_iface", "source_interface_id"),
        Index("ix_topology_link_target_iface", "target_interface_id"),
        Index("ix_topology_link_bundle", "bundle_key"),
        Index("ix_topology_link_group", "topology_group"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Endpoints
    source_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    source_interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )
    target_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id"), nullable=False
    )
    target_interface_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_interfaces.id")
    )

    # Classification
    link_role: Mapped[TopologyLinkRole] = mapped_column(
        Enum(TopologyLinkRole), default=TopologyLinkRole.unknown
    )
    medium: Mapped[TopologyLinkMedium] = mapped_column(
        Enum(TopologyLinkMedium), default=TopologyLinkMedium.unknown
    )
    capacity_bps: Mapped[int | None] = mapped_column(BigInteger)

    # Grouping
    bundle_key: Mapped[str | None] = mapped_column(String(80))
    topology_group: Mapped[str | None] = mapped_column(String(80))

    # Status
    admin_status: Mapped[TopologyLinkAdminStatus] = mapped_column(
        Enum(TopologyLinkAdminStatus), default=TopologyLinkAdminStatus.enabled
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Discovery / reconciliation
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    # Provenance: which reconciler owns this edge (e.g. 'lldp_neighbor'). The
    # LLDP poller only ever touches its own rows (upsert + soft-prune); never
    # manual/other-sourced links. last_seen_at bumps each poll the edge is seen.
    source: Mapped[str | None] = mapped_column(String(40))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Metadata
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    source_device = relationship(
        "NetworkDevice", foreign_keys=[source_device_id], lazy="joined"
    )
    source_interface = relationship(
        "DeviceInterface", foreign_keys=[source_interface_id], lazy="joined"
    )
    target_device = relationship(
        "NetworkDevice", foreign_keys=[target_device_id], lazy="joined"
    )
    target_interface = relationship(
        "DeviceInterface", foreign_keys=[target_interface_id], lazy="joined"
    )


class OutageIncident(Base):
    """An outage against a node, basestation, or FDH cabinet (Phase 4b/5b/§7.6).

    Two provenances share this table, distinguished by ``detection_source``:

    - ``operator`` — declared from the console (or the Phase-5b auto-detect
      scan, which reuses the operator declare path). Lifecycle ``open`` ->
      ``resolved``; treated as already-confirmed, no debounce.
    - ``classifier`` — driven by the outage classifier's reconcile loop
      (§7.6). Debounced lifecycle ``suspected`` -> ``confirmed`` ->
      ``clearing`` -> ``resolved`` (plus ``discarded`` for false positives).

    ``status`` stays a free-form String (NOT a DB enum — the enum route caused
    a prod migration collision in #876) and is validated in code. The
    ``*_at`` lifecycle stamps make MTTR derivable as
    ``resolved_at - confirmed_at``. ``affected_count`` is snapshotted from
    affected_customers at declare time. ``crm_ticket_id`` is a placeholder for
    the future CRM ticket integration — nothing fires on it yet.
    """

    __tablename__ = "outage_incidents"
    __table_args__ = (
        Index("ix_outage_incidents_status", "status"),
        Index("ix_outage_incidents_root_node", "root_node_id"),
        Index("ix_outage_incidents_basestation", "basestation_id"),
        Index("ix_outage_incidents_fdh_cabinet", "fdh_cabinet_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    root_node_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("network_devices.id")
    )
    basestation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pop_sites.id")
    )
    fdh_cabinet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fdh_cabinets.id")
    )
    declared_by: Mapped[str | None] = mapped_column(String(120))
    # operator: open/resolved ; classifier: suspected/confirmed/clearing/
    # resolved/discarded. Kept String and validated in code (see #876).
    status: Mapped[str] = mapped_column(String(20), default="open")
    # 'operator' (console/auto-detect declare) | 'classifier' (§7.6 reconcile).
    detection_source: Mapped[str] = mapped_column(
        String(20), default="operator", server_default="operator", nullable=False
    )
    severity: Mapped[str | None] = mapped_column(String(20))
    affected_count: Mapped[int] = mapped_column(Integer, default=0)
    # Classifier ladder verdict (node_outage / service_fault / ...) + coarse
    # confidence, snapshotted on each reconcile pass. NULL for operator rows.
    classification: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    # Placeholder for the future CRM ticket link (§7.6 firing stays gated).
    crm_ticket_id: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # §7.6 debounce lifecycle stamps (classifier incidents only).
    suspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class OutageNotificationDispatch(Base):
    """Persisted audit + debounce record for a customer outage notification
    (outage classifier P4, design docs/designs/OUTAGE_CLASSIFIER.md §P4).

    One row per dispatch *attempt* (or a boundary-level skip). It is the durable,
    cross-worker source for BOTH:
      - **debounce** — a boundary is muted while it has a recent ``sent`` row
        (replaces the old in-memory dict, which didn't survive restarts or span
        Celery workers), and
      - **audit** — who was notified, when, by which operator, and the outcome.

    ``status='sent'`` means the notification was **emitted to the notification
    system** (which owns channel selection + final delivery); this table does not
    track downstream delivery. ``channel`` stores the outage notification *type*
    (area / last_mile) — the concrete channels are the notification system's
    config-driven concern, not the outage notifier's. No FKs: the audit must
    outlive a deleted node/subscriber (same rationale as AvailabilitySnapshot).
    """

    __tablename__ = "outage_notification_dispatches"
    __table_args__ = (
        Index(
            "ix_outage_notif_dispatch_boundary",
            "boundary_node_id",
            "status",
            "created_at",
        ),
        Index("ix_outage_notif_dispatch_dedup", "dedup_key", "created_at"),
        Index("ix_outage_notif_dispatch_subscriber", "subscriber_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 'area' | 'per_customer'
    scope: Mapped[str] = mapped_column(String(16))
    # The assess() boundary key: an access-node id (inferred) OR an operator
    # OutageIncident id (declared). Used as the debounce/group key.
    boundary_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Outage notification *type* (channels are resolved by the notification
    # system, so we record the type/category, not a hardcoded channel).
    channel: Mapped[str | None] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(200))
    # sent | failed | suppressed_optout | skipped_debounce |
    # skipped_low_confidence | skipped_cap | skipped_no_recipient
    status: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class AvailabilitySnapshot(Base):
    """Daily rolled-up availability for an infrastructure element.

    The SLA/uptime report computes uptime % live by merging downtime intervals,
    which is fine for a single ad-hoc window but too heavy for 365-day trend
    charts. A daily Celery task writes one row per element per day so the
    performance dashboard can chart availability over time without re-merging
    the whole alert history on every render (same pattern as
    ``IpPoolUtilizationSnapshot`` / ``MrrSnapshot``).

    ``element_type`` is one of ``device`` (covers OLT and access-point, both
    NetworkDevice-backed), ``pop_site`` (BTS), or ``pon_port``. ``element_id``
    holds the id within that type — stored as id+type rather than a polymorphic
    FK so a deleted element's history survives.
    """

    __tablename__ = "availability_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "element_type",
            "element_id",
            "snapshot_date",
            name="uq_availability_snapshots_element_day",
        ),
        Index(
            "ix_availability_snapshots_type_date",
            "element_type",
            "snapshot_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    element_type: Mapped[str] = mapped_column(String(20), nullable=False)
    element_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    snapshot_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    uptime_percent: Mapped[float | None] = mapped_column(Float)
    downtime_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    incident_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    affected_subscribers_peak: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class DeviceProjection(Base):
    """Materialized, unified projection of every network device type.

    A derived-state projection: one row per device across the OLT, core
    ``NetworkDevice``, ONT and CPE tables, with the operational status
    pre-derived, so the admin device list can search, filter, sort and
    paginate in SQL instead of loading every device and deriving status in
    memory on each request.

    ``network.device_projection`` (the reconciler) is the sole canonical
    writer. It rebuilds this table idempotently from the authoritative device
    tables, so a row here is a rebuildable cache — never the only copy of
    truth. ``refreshed_at`` is repair evidence, not a public device state;
    divergent rows are repaired and orphans pruned on the next reconcile.
    """

    __tablename__ = "device_projections"
    __table_args__ = (
        UniqueConstraint(
            "device_type", "source_id", name="uq_device_projection_source"
        ),
        CheckConstraint(
            "operational_status IN ('working', 'not_working')",
            name="ck_device_projection_binary_operational_status",
        ),
        Index("ix_device_projection_type_status", "device_type", "operational_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Source identity — (device_type, source_id) is the natural key the
    # reconciler upserts on. device_type is one of olt/core/ont/cpe.
    device_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Display / search columns, denormalised from the source row.
    name: Mapped[str | None] = mapped_column(String(255), index=True)
    serial_number: Mapped[str | None] = mapped_column(String(120), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), index=True)
    vendor: Mapped[str | None] = mapped_column(String(120), index=True)
    model: Mapped[str | None] = mapped_column(String(120))

    # Pre-derived operational status (the whole point of materialising).
    operational_status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="not_working",
        server_default="not_working",
        index=True,
    )
    operational_reason: Mapped[str | None] = mapped_column(String(160))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Linked subscriber, when the device is customer-premises equipment.
    subscriber_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )

    # Per-class operational facts (display-only), denormalised by the
    # reconciler: ONT signal, OLT PON rollup, core site/role, NAS health,
    # router RouterOS version. Rendered by the drawer/ledger; not filtered.
    class_facts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Freshness of this projected row (set on every reconcile pass).
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
