"""1:1 observation row for the OLT/ACS reconciler.

Holds last-seen reality from OLT (SSH ``display`` commands) and ACS (GenieACS
CWMP cache). Written exclusively by ``app.services.network.reconcile``. Other
code may read it; nothing else writes it.

Lives in its own table rather than on ``OntUnit`` to keep the hot ``OntUnit``
row small and to make the observation lifecycle (potentially absent until first
reconcile) explicit. The 1:1 relationship is enforced by the unique constraint
on ``ont_unit_id``.

Fields here mirror ``OntObservedState`` /  ``OltObservedFields`` /
``AcsObservedFields`` in ``app/services/network/reconcile/state.py``. Adapter
code that converts between this row and the in-memory dataclass lands in the
follow-up integration commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OntObservation(Base):
    """Last-seen OLT + ACS state for one ONT.

    Sweeper and sync reconciles both upsert this row at the end of every
    reconcile pass that completed a final read. Never authoritative for desired
    state — the planner compares ``OntDesiredState`` to this row to decide what
    to push, but proposed changes mutate the desired side only.
    """

    __tablename__ = "ont_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ont_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ont_units.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # ── Reconcile metadata ──────────────────────────────────────────────────
    last_reconciled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_reconcile_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    mgmt_ip_pingable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # ── OLT-observed (from `display ont info / ipconfig / optical-info`) ────
    olt_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # "present" / "absent" / "unavailable" — the tri-state outcome of the most
    # RECENT read attempt, updated every pass regardless of whether it moved
    # ``olt_observed_at`` forward. Nullable because rows written before this
    # column existed carry no history to backfill (expand-only migration).
    olt_read_status: Mapped[str | None] = mapped_column(String(20))
    # When the OLT-observed columns above were last genuinely refreshed by a
    # "present" or "absent" read. Distinct from ``updated_at`` (bumped on
    # every write, including an "unavailable" pass that leaves these columns
    # untouched) — this is the freshness signal for the OLT half specifically.
    olt_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # match/mismatch/initial — kept as plain strings to avoid coupling this
    # table to OLT-vendor-specific enum churn.
    olt_match_state: Mapped[str | None] = mapped_column(String(20))
    olt_run_state: Mapped[str | None] = mapped_column(String(20))
    olt_distance_m: Mapped[int | None] = mapped_column(Integer)
    olt_rx_dbm: Mapped[float | None] = mapped_column(Float)
    olt_tx_dbm: Mapped[float | None] = mapped_column(Float)
    olt_temperature_c: Mapped[int | None] = mapped_column(Integer)
    olt_description: Mapped[str | None] = mapped_column(String(128))
    olt_mgmt_ip: Mapped[str | None] = mapped_column(String(64))
    olt_mgmt_vlan: Mapped[int | None] = mapped_column(Integer)
    olt_line_profile_id: Mapped[int | None] = mapped_column(Integer)
    olt_service_profile_id: Mapped[int | None] = mapped_column(Integer)
    olt_tr069_profile_id: Mapped[int | None] = mapped_column(Integer)
    # List of {index, vlan, gem, state} dicts — JSON because the count is
    # bounded but variable, and we don't query into the structure.
    olt_service_ports: Mapped[list[dict] | None] = mapped_column(JSON)

    # ── ACS-observed (from the GenieACS CWMP cache) ─────────────────────────
    acs_present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    acs_last_inform_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acs_last_boot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acs_last_bootstrap_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    acs_observed_software_version: Mapped[str | None] = mapped_column(String(120))
    acs_observed_pppoe_username: Mapped[str | None] = mapped_column(String(120))
    acs_observed_pppoe_enable: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_wan_vlan: Mapped[int | None] = mapped_column(Integer)
    acs_observed_wan_external_ip: Mapped[str | None] = mapped_column(String(64))
    acs_observed_wan_connection_status: Mapped[str | None] = mapped_column(String(40))
    acs_observed_nat_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_dhcp_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_ssid: Mapped[str | None] = mapped_column(String(64))
    acs_observed_wifi_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_wifi_channel: Mapped[int | None] = mapped_column(Integer)
    acs_observed_wifi_security_mode: Mapped[str | None] = mapped_column(String(40))
    acs_observed_wifi_instance_index: Mapped[int | None] = mapped_column(Integer)
    acs_observed_remote_ssh_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_remote_ssh_port: Mapped[int | None] = mapped_column(Integer)
    acs_observed_remote_telnet_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_remote_telnet_port: Mapped[int | None] = mapped_column(Integer)
    acs_observed_periodic_inform_interval_sec: Mapped[int | None] = mapped_column(
        Integer
    )
    # CR credentials are write-only on the device; we record only their presence,
    # never the values themselves.
    acs_observed_cr_username_set: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_cr_password_set: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_wan_wcd_index: Mapped[int | None] = mapped_column(Integer)
    acs_observed_wan_instance_index: Mapped[int | None] = mapped_column(Integer)
    acs_data_model_root: Mapped[str | None] = mapped_column(String(40))
    acs_observed_ipv6_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_wan_ip_enable: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_wan_addressing_type: Mapped[str | None] = mapped_column(String(20))
    acs_observed_wan_ip_address: Mapped[str | None] = mapped_column(String(64))
    acs_observed_wan_subnet_mask: Mapped[str | None] = mapped_column(String(64))
    acs_observed_wan_gateway: Mapped[str | None] = mapped_column(String(64))
    acs_observed_wan_dns_servers: Mapped[str | None] = mapped_column(String(200))
    acs_observed_dhcpv6_enabled: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_dhcpv6_request_prefixes: Mapped[bool | None] = mapped_column(Boolean)
    acs_observed_ra_enabled: Mapped[bool | None] = mapped_column(Boolean)

    # ── Audit timestamps ────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    ont_unit = relationship("OntUnit", back_populates="observation")
