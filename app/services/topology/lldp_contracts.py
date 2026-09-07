"""Detached LLDP read, collection, and reconciliation contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from time import monotonic
from uuid import UUID

from app.models.network_monitoring import TopologyLinkMedium
from app.models.router_management import RouterAccessMethod
from app.services.owner_commands import CommandContext


@dataclass(frozen=True)
class LldpDevice:
    id: UUID
    name: str
    hostname: str | None
    mgmt_ip: str | None
    is_active: bool


@dataclass(frozen=True)
class LldpJumpHost:
    id: UUID
    hostname: str
    port: int
    username: str = field(repr=False)
    ssh_key: str | None = field(repr=False)
    ssh_password: str | None = field(repr=False)
    is_active: bool


@dataclass(frozen=True)
class LldpRouter:
    id: UUID
    name: str
    management_ip: str
    rest_api_port: int
    rest_api_username: str = field(repr=False)
    rest_api_password: str = field(repr=False)
    use_ssl: bool
    verify_tls: bool
    access_method: RouterAccessMethod
    jump_host: LldpJumpHost | None
    network_device_id: UUID | None


@dataclass(frozen=True)
class LldpLinkState:
    id: UUID
    source_device_id: UUID
    target_device_id: UUID
    source_interface_id: UUID | None
    target_interface_id: UUID | None
    source: str | None
    medium: TopologyLinkMedium
    is_active: bool
    last_seen_at: datetime | None
    metadata_json: str


@dataclass(frozen=True)
class LldpSnapshot:
    observed_at: datetime
    routers: tuple[LldpRouter, ...]
    devices: tuple[LldpDevice, ...]
    links: tuple[LldpLinkState, ...]
    started_at: float = field(default_factory=monotonic, compare=False, repr=False)


@dataclass(frozen=True)
class LldpReadQuery:
    observed_at: datetime


@dataclass(frozen=True)
class LldpStats:
    routers_polled: int = 0
    routers_failed: int = 0
    via_binary_api: int = 0
    via_rest: int = 0
    skipped_no_device: int = 0
    skipped_time_budget: int = 0
    neighbors_seen: int = 0
    created: int = 0
    updated: int = 0
    pruned: int = 0
    edges: int = 0
    matched_by_identity: int = 0
    matched_by_address: int = 0
    matched_by_stripped_identity: int = 0
    skipped_manual_dup: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class LldpEdge:
    source_device_id: UUID
    target_device_id: UUID
    medium: TopologyLinkMedium
    observed_from: UUID
    local_interface: str
    remote_identity: str
    remote_board: str | None


@dataclass(frozen=True)
class LldpPoll:
    snapshot: LldpSnapshot
    edges: tuple[LldpEdge, ...]
    polled_device_ids: frozenset[UUID]
    stats: LldpStats


@dataclass(frozen=True)
class ReconcileLldpCommand:
    context: CommandContext
    poll: LldpPoll
